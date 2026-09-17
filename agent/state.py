"""LangGraph 会话状态与回合输出定义（Day 43）。

设计口径（与 P2 结论对齐，AGENTS.md 决策优先级）
------------------------------------------------
- 状态 = 单次问答的完整事实轨迹（question / plan / sql / rows / 归因…），
  每轮由 checkpointer 按 session_id 持久化，可回溯可审计。
- **多轮（ADR-0014 ②）**：同一会话支持「连续提问 + 每轮事实留痕
  + 同构追问补全」——last_plan（最近成功轮采纳的 Plan，explain 回写）在 plan
  节点做指代预检：残句（无指标词）命中链接词形态（"那 2014 年呢 / 换成 X /
  按 X 呢"）时复用上轮 metric/维度/过滤/排序，仅替换本轮解析出的时间/维度
  片段，合并 Plan 仍走编译预检；其余指代（自由代词"它/这些"、无法归属碎片、
  换维遇上轮维度值过滤）→ 澄清不猜（见 agent/planner.py followup docstring）。
- **跨轮记账也在状态里（ADR-0020 决策 ⑤⑥）**：轮数（turns，plan 节点 +1）与
  身份指纹（session_fingerprint，首轮由 DataAgent.ask 随输入写入）同属
  checkpoint 状态——没有进程内会话表，重启后两者与 last_plan 一起存活。
- **多期间分析记账（ADR-0026 决策 ④）**：analysis_record 是分析父轮的有界事实
  （版本/状态/问句/身份指纹/子步证据），随 checkpoint 持久化、可中断恢复；
  analysis_followup_blocked 在分析期间挂起残句追问（不偷接分析前的旧 Plan），
  一次明确的普通 Plan 执行成功（explain 回写）才解除。记录里的 rows 标量用
  本模块的 encode_row_value / encode_rows 带类型编码（Decimal/bool 等不经编码
  会在序列化里丢精度或丢类型），不存贡献率、叙事、claims 本体；贡献率与叙事
  计算产物（AnalysisResult / Attribution）不进 checkpoint。
- 回合输出 TurnResult 按 kind 分类：answer（执行成功）/ clarify（反问，
  不猜）/ blocked（Guard 拒绝）/ error（执行期故障）/ handoff（人工接管，
  Day 48）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
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

# analysis_record 的形态版本（ADR-0026 决策 ④ ⑦）：记录里显式带版本，读取方
# （中断恢复、评测器）按版本判定兼容性——无版本的记录无从谈起「迁移」。
ANALYSIS_RECORD_VERSION = 1


class TurnState(TypedDict, total=False):
    """LangGraph 状态：单轮问答轨迹（checkpointer 按 session_id 持久化）。

    节点间仅通过这些字段通信；字段语义见 graph.py 各节点 docstring。
    rows 用 tuple 而非 list：LangGraph checkpoint 序列化与不可变审计语义。
    """

    question: str
    session_id: str
    turns: int  # 已连续轮数（plan 节点 +1；ADR-0020 决策 ⑤ 的单一事实源）
    session_fingerprint: str  # 首轮写入的身份指纹哈希（决策 ⑥；校验在 DataAgent.ask）
    plan_override: Plan  # `/plan/execute` 注入的直执计划（ADR-0022 决策 ③）：
    # plan 节点首行短路优先于 Planner；用后即焚（同轮写回 None，不残留到下一轮）
    plan: Plan  # Planner 命中（deterministic）或候选链 validate 通过（candidate）
    last_plan: Plan  # 最近成功轮采纳的 Plan（explain 回写；同构追问补全基线，ADR-0014 ②）
    clarification: ClarificationRequest  # 反问输出（歧义直出 / clarify 终端组装）
    candidates: tuple[str, ...]  # retrieve 输出：schema linking top-K 候选
    unmatched: bool  # plan 判定未命中指标同义词（可进候选链/澄清增强）
    reason: str  # 反问原因链：plan 默认文案 → retrieve/generate/validate 失败覆盖
    attempts: int  # 候选链 generate 总调用次数（首轮 + validate 内重试）
    validated: bool  # validate 节点成功标记（候选链；失败重试期间旧 plan 留在状态）
    sql: str  # Guard enforce 之后的最终执行 SQL（execute 产物，供解释与审计）
    rows: tuple[tuple[Any, ...], ...]
    columns: tuple[str, ...]
    time_column: str | None  # 编译声明的时间轴列别名（ADR-0025 决策 ①3）；
    # execute 写入、plan 冲刷，渲染 chart 的轴选择依据（非执行器列名推断）
    row_count: int
    latency_ms: float  # 仅执行耗时（compile/guard 为确定性本地计算，不计入）
    usage: dict[str, int]  # 候选链 LLM token 用量（deterministic 链为空）
    validation_issues: tuple[str, ...]  # ExecutionValidator 结果形态注记
    path: PathKind  # 链路来源（explain 归因）
    engine: EngineName  # 实际生成引擎（answer 归因展示）
    explanation: dict[str, Any]  # explain 节点归因（Day 46 扩展字段）
    policy_effect: str  # identity 注入轮生效句（execute 写入 → explain 挂入归因；
    # 只含角色 + 策略名，不含条件值——0011 不外泄细节；每轮由 plan 冲刷）
    block_reason: str  # kind=blocked：Guard 拒绝原因（不携带被拒 SQL）
    error: str  # kind=error：执行期故障描述
    handoff_reason: str  # kind=handoff（Day 48）：人工接管原因（候选链素材空）
    # 多期间分析父轮记账（ADR-0026 决策 ④⑦）：只存有界原始事实（版本/状态/
    # 问句/身份指纹/子步证据，rows 标量经 encode_rows 带类型编码），不存贡献率、
    # 叙事、claims 本体；新用户轮由 plan 节点冲刷为 None（旧 running 被普通轮
    # 覆盖即该轮无终态，历史快照仍留证）。AnalysisResult / Attribution 不进此字段。
    analysis_record: dict[str, Any] | None
    # 分析期间挂起残句追问：True 时 plan 节点拒绝对残句承接 last_plan（ADR-0026
    # 决策 ⑥），明确提示完整重述；一次明确的普通 Plan 执行成功（explain 回写）才解除。
    analysis_followup_blocked: bool


# ---------------------------------------------------------------------------
# analysis_record.rows 标量的带类型编码（ADR-0026 决策 ④⑦）
# ---------------------------------------------------------------------------
# rows 里的标量可能是 Decimal / str / int / float / bool / None。它们直接进
# LangGraph msgpack 序列化时：Decimal 不在 JsonPlusSerializer 白名单（会崩或丢），
# bool 是 int 的子类（True 与 1 往返后不可区分）。编码成 JSON 原生形态的带标记
# dict（{"t": tag, "v": payload}）后可安全过 checkpointer，decode 回原类型与值，
# 供中断恢复后的贡献率复算（T07/T09 复用本组工具）。
# 消费方约束：float 仅为存储保真而保留（repr() 文本往返），下游不得将其作为
# ADR-0026 T04 综合算术的输入——综合运算必须先把 float 转回 Decimal（str() 文本
# 精确转换）再参与加减与贡献率计算，避免二进制浮点误差污染精确分解。


def encode_row_value(value: Any) -> dict[str, Any]:
    """把 rows 单个标量编码成 JSON 原生形态的带类型 dict。

    支持 None / bool / int / float / str / Decimal；bool 判定必须先于 int
    （bool 是 int 的子类，否则 True 会被错编成 int 而丢类型）。float 用
    repr() 文本保精度往返；Decimal 用 str() 文本。其余类型直接拒绝（宁可
    显式失败，不让静默丢类型进 checkpoint）。
    消费方约束：float 仅存储保真，不得进 T04 综合算术——综合前必须转回
    Decimal（str() 文本精确转换），二进制浮点值不得直接参与精确分解运算。
    """
    if value is None:
        return {"t": "null"}
    if isinstance(value, bool):  # bool 先于 int：bool 是 int 子类
        return {"t": "bool", "v": value}
    if isinstance(value, int):
        return {"t": "int", "v": value}
    if isinstance(value, float):
        return {"t": "float", "v": repr(value)}
    if isinstance(value, str):
        return {"t": "str", "v": value}
    if isinstance(value, Decimal):
        return {"t": "decimal", "v": str(value)}
    raise TypeError(
        f"rows 标量编码不支持 {type(value).__name__}：只接受 None/bool/int/float/str/Decimal"
    )


def decode_row_value(encoded: Any) -> Any:
    """把 encode_row_value 的产物解码回原类型与原值。

    非法形态（非 dict、缺 "t" 标记、未知标记、str 标记载荷不是字符串）抛
    ValueError——损坏的记录宁可显式报错，不做静默猜测。
    """
    if not isinstance(encoded, dict) or "t" not in encoded:
        raise ValueError(f"rows 标量编码形态非法：{encoded!r}")
    tag = encoded["t"]
    if tag == "null":
        return None
    if tag == "bool":
        return bool(encoded["v"])
    if tag == "int":
        return int(encoded["v"])
    if tag == "float":
        return float(encoded["v"])
    if tag == "str":
        v = encoded["v"]
        if not isinstance(v, str):
            raise ValueError(f"str 标记的载荷不是字符串：{v!r}")
        return v
    if tag == "decimal":
        return Decimal(encoded["v"])
    raise ValueError(f"rows 标量编码未知标记：{tag!r}")


def encode_rows(rows: Iterable[Iterable[Any]]) -> list[list[Any]]:
    """把整个 rows 逐行逐格编码成 list[list[带类型 dict]]（JSON 原生可序列化）。"""
    return [[encode_row_value(v) for v in row] for row in rows]


def decode_rows(encoded: Any) -> tuple[tuple[Any, ...], ...]:
    """把 encode_rows 的产物解码回 tuple[tuple[Any, ...], ...]（TurnState.rows 形态）。"""
    return tuple(tuple(decode_row_value(v) for v in row) for row in encoded)


@dataclass(frozen=True)
class TurnResult:
    """单轮问答的对外结构化结果（graph 无状态函数 ask 的组装产物）。

    kind 与字段的对应关系：
    - answer：metric / sql / columns / rows / row_count / latency_ms / engine /
      usage / explanation / validation_issues / time_column / chart
    - clarify：clarification（reasons + candidates）
    - blocked：block_reason
    - error：error
    - handoff：handoff_reason（Day 48）

    chart 是渲染派生数据（sql/rows/columns/time_column 的确定性函数，ADR-0025
    决策 ②）：不落 checkpoint、不反噬回答（渲染拒绝 → None），非 answer 轮恒 None。
    """

    kind: TurnKind
    session_id: str
    question: str
    turns_in_session: int = 1  # 同一 session 已连续轮数（来自状态 turns，决策 ⑤）
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
    time_column: str | None = None  # 编译声明的时间轴列别名（ADR-0025 决策 ①3）
    chart: dict[str, Any] | None = None  # 图表 spec（turn_from_state 渲染，决策 ②）
    # clarify / blocked / error / handoff
    clarification: ClarificationRequest | None = None
    block_reason: str | None = None
    error: str | None = None
    handoff_reason: str | None = None
