"""LangGraph 会话状态与回合输出定义（Day 43）。

设计口径（与 P2 结论对齐，AGENTS.md 决策优先级）
------------------------------------------------
- 状态 = 单次问答的完整事实轨迹（question / plan / sql / rows / 归因…），
  每轮由 checkpointer 按 session_id 持久化，可回溯可审计。
- **多轮边界（诚实声明）**：MVP 支持「同一会话连续提问 + 每轮事实留痕」，
  不做指代消解（"上轮那个/它"类指代需要会话推断，MVP 不支持——问句每次
  全量解析，指代消解会引入编造风险，见 AGENTS.md 决策优先级）。
- 回合输出 TurnResult 按 kind 分类：answer（执行成功）/ clarify（反问，
  不猜）/ blocked（Guard 拒绝）/ error（执行期故障）/ handoff（人工接管，
  Day 48）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from agent.compiler import Plan
from agent.planner import ClarificationRequest

# 链路来源：deterministic = Planner 命中直达；candidate = 检索→生成候选链
# （仅 allow_candidate 时可达，见 agent/graph.py 路由口径）
PathKind = Literal["deterministic", "candidate"]

# 回合终端类型（kind=answer 时 path 区分链路来源）
TurnKind = Literal["answer", "clarify", "blocked", "error", "handoff"]

# 生成引擎（explain 归因用）：deterministic 链为固定值；候选链 = generator.engine
EngineName = Literal["deterministic", "stub", "openai"]


class TurnState(TypedDict, total=False):
    """LangGraph 状态：单轮问答轨迹（checkpointer 按 session_id 持久化）。

    节点间仅通过这些字段通信；字段语义见 graph.py 各节点 docstring。
    rows 用 tuple 而非 list：LangGraph checkpoint 序列化与不可变审计语义。
    """

    question: str
    session_id: str
    plan: Plan  # Planner 命中（deterministic）或候选链 validate 通过（candidate）
    clarification: ClarificationRequest  # 反问输出（歧义直出 / clarify 终端组装）
    candidates: tuple[str, ...]  # retrieve 输出：schema linking top-K 候选
    unmatched: bool  # plan 判定未命中指标同义词（可进候选链/澄清增强）
    reason: str  # 反问原因链：plan 默认文案 → retrieve/generate/validate 失败覆盖
    attempts: int  # 候选链 generate 总调用次数（首轮 + validate 内重试）
    validated: bool  # validate 节点成功标记（候选链；失败重试期间旧 plan 留在状态）
    sql: str  # Guard enforce 之后的最终执行 SQL（execute 产物，供解释与审计）
    rows: tuple[tuple[Any, ...], ...]
    columns: tuple[str, ...]
    row_count: int
    latency_ms: float  # 仅执行耗时（compile/guard 为确定性本地计算，不计入）
    usage: dict[str, int]  # 候选链 LLM token 用量（deterministic 链为空）
    validation_issues: tuple[str, ...]  # ExecutionValidator 结果形态注记
    path: PathKind  # 链路来源（explain 归因）
    engine: EngineName  # 实际生成引擎（answer 归因展示）
    explanation: dict[str, Any]  # explain 节点归因（Day 46 扩展字段）
    block_reason: str  # kind=blocked：Guard 拒绝原因（不携带被拒 SQL）
    error: str  # kind=error：执行期故障描述
    handoff_reason: str  # kind=handoff（Day 48）：人工接管原因（候选链素材空）


@dataclass(frozen=True)
class TurnResult:
    """单轮问答的对外结构化结果（graph 无状态函数 ask 的组装产物）。

    kind 与字段的对应关系：
    - answer：metric / sql / columns / rows / row_count / latency_ms / engine /
      usage / explanation / validation_issues
    - clarify：clarification（reasons + candidates）
    - blocked：block_reason
    - error：error
    - handoff：handoff_reason（Day 48）
    """

    kind: TurnKind
    session_id: str
    question: str
    turns_in_session: int = 1  # 同一 session 已连续轮数（Agent 层维护）
    # answer
    metric: str | None = None
    sql: str | None = None
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    row_count: int = 0
    latency_ms: float = 0.0
    engine: EngineName = "deterministic"
    path: PathKind | None = None
    usage: dict[str, int] = field(default_factory=dict)
    validation_issues: tuple[str, ...] = ()
    explanation: dict[str, Any] | None = None  # explain 节点归因（Day 46 扩展）
    # clarify / blocked / error / handoff
    clarification: ClarificationRequest | None = None
    block_reason: str | None = None
    error: str | None = None
    handoff_reason: str | None = None
