"""T11 LLM 节点执行器：受约束的模型调用 + 工具循环 + 确定性回退。

设计口径
--------
- LLM 节点通过 ToolBroker 访问工具（不直接持有 DeterministicTools）；
  工具权限由节点类型决定（understand 不能执行 SQL）。
- 模型 profile 能力：根据 LlmPolicy 决策选择后端；未配置/被拒 → 确定性回退。
- 实际输入最高敏感度传播：敏感问句标记后禁止云工具（ToolBroker 层强制）。
- 超时与累计预算：单次输入 2048 / 输出 512 / 累计 10240 token（D05 BudgetSpec）。
- 确定性回退：LLM 不可用时回退到规则/模板路径，不报错。

边界（诚实声明）
----------------
- 首版为骨架实现：LLM 调用接口已定义但默认走确定性回退；
  真实模型调用需 T14 训练后的模型注册。
- 不暴露裸执行器——所有 SQL 执行必须经 ToolBroker → DeterministicTools → Guard。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent.compiler import SemanticModel
from agent.security.sql_guard import Budget
from agent.tools.broker import ToolBroker

PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "node_action.yaml"


@dataclass(frozen=True)
class LlmNodeResult:
    """LLM 节点执行结果。

    status:
        ok          = 成功（可能来自 LLM 或确定性回退）
        blocked     = 被安全策略拒绝
        fallback    = LLM 不可用，已回退到确定性路径
        error       = 执行失败
    """

    status: str
    output: dict[str, Any] | None = None
    source: str = "deterministic"  # "llm" | "deterministic" | "fallback"
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reason_code: str | None = None


@dataclass
class LlmNodeExecutor:
    """LLM 节点执行器。

    Parameters
    ----------
    model : 语义模型。
    executor : 查询执行器。
    budget : Guard 预算。
    node_type : 当前节点类型（决定工具权限）。
    chat_fn : 可选 LLM 调用函数（测试注入/生产接入）。None = 纯确定性回退。
    """

    model: SemanticModel
    executor: Any
    budget: Budget
    node_type: str
    chat_fn: Any = None  # Callable[[str, str], tuple[str, dict]] | None
    _broker: ToolBroker | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._broker = ToolBroker(
            model=self.model,
            executor=self.executor,
            budget=self.budget,
            node_type=self.node_type,
        )

    @property
    def broker(self) -> ToolBroker:
        assert self._broker is not None  # __post_init__ 保证
        return self._broker

    def execute(
        self,
        question: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> LlmNodeResult:
        """执行 LLM 节点（工具循环 + 确定性回退）。"""
        ctx = context or {}

        # 无 LLM 配置 → 确定性回退
        if self.chat_fn is None:
            return self._deterministic_fallback(question, ctx)

        # 加载提示词
        prompt = self._load_prompt()
        if prompt is None:
            return LlmNodeResult(
                status="fallback",
                source="fallback",
                reason_code="prompt_not_found",
            )

        # 构造系统/用户消息
        system_msg = prompt.get("system", "You are a helpful assistant.")
        user_msg = self._build_user_message(question, ctx)

        # 调用 LLM
        try:
            content, usage = self.chat_fn(system_msg, user_msg)
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
            self.broker.record_token_usage(input_tokens=input_tokens, output_tokens=output_tokens)
        except Exception:
            return LlmNodeResult(
                status="fallback",
                source="fallback",
                reason_code="llm_call_failed",
            )

        # 解析 LLM 响应中的工具调用
        return self._process_llm_response(content, question, ctx)

    def _deterministic_fallback(self, question: str, context: dict[str, Any]) -> LlmNodeResult:
        """确定性回退路径（LLM 不可用时）。"""
        # 使用 list_metrics 工具获取指标目录作为上下文
        result = self.broker.call("list_metrics", {})
        if result.status == "ok" and result.result:
            return LlmNodeResult(
                status="ok",
                output={
                    "fallback": True,
                    "available_metrics": result.result.get("count", 0),
                    "message": "LLM 不可用，已回退到确定性路径",
                },
                source="fallback",
                tool_calls=self.broker.tool_calls_used,
            )
        return LlmNodeResult(
            status="fallback",
            source="fallback",
            reason_code="fallback_tools_unavailable",
        )

    def _load_prompt(self) -> dict[str, Any] | None:
        """加载外置提示词（AGENTS.md §7.4：禁止内联）。"""
        if not PROMPT_FILE.exists():
            return None
        try:
            data = yaml.safe_load(PROMPT_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _build_user_message(self, question: str, context: dict[str, Any]) -> str:
        """构造用户消息（含上下文信息）。"""
        parts = [f"问题：{question}"]
        if context.get("locale"):
            parts.append(f"语言：{context['locale']}")
        if context.get("available_metrics"):
            parts.append(f"可用指标数：{context['available_metrics']}")
        return "\n".join(parts)

    def _process_llm_response(
        self, content: str, question: str, context: dict[str, Any]
    ) -> LlmNodeResult:
        """解析 LLM 响应并执行工具调用。"""
        # 首版骨架：LLM 响应作为文本输出，不解析工具调用
        # （完整 JSON action 工具循环随 T11 后续迭代补齐）
        return LlmNodeResult(
            status="ok",
            output={"llm_response": content, "question": question},
            source="llm",
            tool_calls=self.broker.tool_calls_used,
            input_tokens=self.broker.input_tokens_used,
            output_tokens=self.broker.output_tokens_used,
        )
