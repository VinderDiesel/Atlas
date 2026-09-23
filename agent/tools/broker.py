"""T11 节点工具代理（ToolBroker）：节点类型 → 工具权限、预算追踪、敏感度传播。

设计口径
--------
- ToolBroker 是节点与底层 DeterministicTools 之间的**受保护中间层**：
  每个节点类型只能调用其被授权的工具子集。understand 节点不能执行 SQL
  （只允许目录查询和编译预览）。
- 预算追踪：每次调用消耗 tool_calls 配额；超出即 blocked，不无限重试。
- 敏感度传播：高敏感度问句标记后，所有工具调用被拒绝（防止数据泄漏到云）。
- 参数校验失败返回 error（工具被调用但参数有误）；权限拒绝返回 blocked
  （工具根本不可达）。两者 reason_code 不同，审计可区分。

边界（诚实声明）
----------------
- 本模块不做 LLM 调用——LLM 节点（`agent/flows/llm_node.py`）消费本模块。
- 工具输出不含 SQL 被拒信息（纵深防御，与 DeterministicTools 口径一致）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.compiler import SemanticModel
from agent.security.sql_guard import Budget
from agent.tools.registry import DeterministicTools, ToolError

# ---------------------------------------------------------------------------
# 节点类型 → 允许工具白名单（D05 端口合同的工具面）
# ---------------------------------------------------------------------------
# 原则：只有真正需要执行能力的节点才授予 execute_readonly；understand/retrieve
# 等「理解/检索」节点只给目录查询和编译预览，杜绝模型请求裸 SQL 时被执行。
_NODE_TOOL_ALLOWLIST: dict[str, frozenset[str]] = {
    "rule_plan": frozenset({"list_metrics", "describe_metric"}),
    "understand": frozenset({"list_metrics", "describe_metric", "compile_sql"}),
    "retrieve": frozenset({"list_metrics", "describe_metric"}),
    "bind_plan": frozenset({"list_metrics", "describe_metric", "compile_sql"}),
    "execute_plan": frozenset(
        {"list_metrics", "describe_metric", "compile_sql", "execute_readonly"}
    ),
    "analysis": frozenset({"list_metrics", "describe_metric", "compile_sql"}),
    "explain": frozenset({"list_metrics", "describe_metric"}),
    "chart": frozenset({"list_metrics", "describe_metric"}),
    "clarify": frozenset({"list_metrics"}),
    "handoff": frozenset(),
}

_ALL_TOOLS = frozenset({"list_metrics", "describe_metric", "compile_sql", "execute_readonly"})


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolResult:
    """工具调用结果（不可变；审计友好）。

    status:
        ok      = 调用成功，result 有值
        blocked = 权限/预算/敏感度拒绝，result 为 None
        error   = 工具被授权但参数校验失败或执行故障，result 为 None
    """

    status: str  # "ok" | "blocked" | "error"
    result: dict[str, Any] | None = None
    reason_code: str | None = None
    error_message: str | None = None


@dataclass
class ToolBroker:
    """节点工具代理：按节点类型授权、预算追踪、敏感度门控。

    Parameters
    ----------
    model : 语义模型（传递给 DeterministicTools）。
    executor : 查询执行器（传递给 DeterministicTools）。
    budget : Guard 预算（传递给 DeterministicTools）。
    node_type : 当前节点类型（决定工具白名单）。
    max_tool_calls : 本次运行的工具调用预算上限；默认取 BudgetSpec.tool_calls。
    sensitivity : 输入敏感度（"high" 时拒绝全部工具调用）。
    """

    model: SemanticModel
    executor: Any
    budget: Budget
    node_type: str
    max_tool_calls: int = 3
    sensitivity: str = "normal"
    max_input_tokens: int = 2048
    max_output_tokens: int = 512
    max_total_tokens: int = 10240
    _tool_calls_used: int = field(default=0, init=False, repr=False)
    _input_tokens_used: int = field(default=0, init=False, repr=False)
    _output_tokens_used: int = field(default=0, init=False, repr=False)
    _tools: DeterministicTools | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._tools = DeterministicTools(
            model=self.model, executor=self.executor, budget=self.budget
        )

    @property
    def tool_calls_used(self) -> int:
        return self._tool_calls_used

    @property
    def input_tokens_used(self) -> int:
        return self._input_tokens_used

    @property
    def output_tokens_used(self) -> int:
        return self._output_tokens_used

    def record_token_usage(self, *, input_tokens: int, output_tokens: int) -> None:
        """记录 token 消耗（LLM 节点调用后汇报）。"""
        self._input_tokens_used += input_tokens
        self._output_tokens_used += output_tokens

    def call(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        """调用工具（受节点权限、预算、敏感度三重门控）。"""
        # 1. 敏感度门控（最高优先级：防止数据泄漏）
        if self.sensitivity == "high":
            return ToolResult(status="blocked", reason_code="sensitivity_denied")

        # 2. token 预算检查
        if (
            self._input_tokens_used >= self.max_input_tokens
            or self._output_tokens_used >= self.max_output_tokens
            or (self._input_tokens_used + self._output_tokens_used) >= self.max_total_tokens
        ):
            return ToolResult(status="blocked", reason_code="token_budget_exceeded")

        # 3. 工具调用预算检查
        if self._tool_calls_used >= self.max_tool_calls:
            return ToolResult(status="blocked", reason_code="budget_exceeded")

        # 4. 工具存在性检查
        if tool_name not in _ALL_TOOLS:
            return ToolResult(status="blocked", reason_code="unknown_tool")

        # 5. 节点权限检查
        allowed = _NODE_TOOL_ALLOWLIST.get(self.node_type, frozenset())
        if tool_name not in allowed:
            return ToolResult(status="blocked", reason_code="tool_not_allowed")

        # 6. 实际调用（委托 DeterministicTools）
        assert self._tools is not None  # __post_init__ 保证
        try:
            method = getattr(self._tools, tool_name)
            result = method(**arguments)
            self._tool_calls_used += 1
            return ToolResult(status="ok", result=result)
        except ToolError as exc:
            self._tool_calls_used += 1
            return ToolResult(
                status="error",
                reason_code="tool_error",
                error_message=str(exc),
            )
        except TypeError as exc:
            # 参数签名不匹配（如缺必填参数）
            return ToolResult(
                status="error",
                reason_code="tool_error",
                error_message=str(exc),
            )
