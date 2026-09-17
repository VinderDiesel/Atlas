"""分析编排数据契约（ADR-0026 T01：类型 + 纯校验函数，无编排/DB/HTTP 逻辑）。

本模块是 ADR-0026 多步分析的**数据契约唯一权威**，供 T03（模板规划器）、
T04（加法资格门）、T07（有界执行器）、T08（API 投影）、T09（评测器）共享：

- AnalysisPlan：固定四步模板（决策①），sub_plans 为 Plan 实例元组；
- Attribution / AttributionItem：精确 Decimal 综合结果（决策⑤）；
- AnalysisResult：分析一次执行的整体产出，绑定快照与语义层版本。

闭合矩阵（终态 × 字段组合 × 原因码）：
- turn.kind ∈ {answer, clarify, blocked, error} 为分析终态（handoff 是意外出口，
  属内部契约错误，闭合矩阵拒绝）；
- turn 终态原因码优先：blocked→guard_blocked、error→execution_error；
  attribution.reason_code 仅在 answer 下有意义（unavailable 综合原因码闭集）。

T01 部分只定义类型与纯校验函数；T03 在此追加 `AnalysisPlanner`（问句 →
AnalysisPlan 的确定性槽位解析与固定四步模板生成，含编译预检）——仍无 I/O、
无 SQL 执行、无 LLM；子执行归 T05，编排归 T07。T04 在此追加 `synthesize`
（四步执行事实 → 精确 Decimal 贡献综合的纯函数：完备性/对账门禁 + 加法综合，
仍无 I/O、无执行器/图/API/LLM 依赖）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, DecimalException, localcontext
from typing import Final, Literal, NamedTuple

from agent.compiler import (
    CompileError,
    Compiler,
    Filter,
    OrderSpec,
    Plan,
    SemanticModel,
    TimeSpec,
)

# 形态层原语复用（ADR-0026 T03）：scan_time_spans 与形态正则常量是 Planner 形态层
# 的同一实现（不复制 patterns_*.yml 的注册表数据）；修复轮 1 起一律经 planner 的
# 公开名引用（同一对象别名），不再跨模块引用下划线私有名；Planner 不反向依赖本
# 模块，无环。
from agent.planner import (
    EN_EXCL_RE,
    EN_ONLY_RE,
    EN_THRESHOLD_GT_RE,
    EN_THRESHOLD_LT_RE,
    EN_TOP_N_RE,
    EQ_FILTER_RE,
    EXC_FILTER_RE,
    THRESHOLD_GT_RE,
    THRESHOLD_LT_RE,
    TOP_N_RE,
    ClarificationRequest,
    Planner,
    TimeSpan,
    ValueNotice,
    scan_time_spans,
)
from agent.state import TurnKind, TurnResult

# ---------------------------------------------------------------------------
# 闭合常量（schema 侧枚举必须与这里同步，tests/test_analysis_contract.py 守护）
# ---------------------------------------------------------------------------

PENDING: Final[str] = "<待填写>"
"""draft 样本占位符（AGENTS.md §9.3）：快照/语义 sha 在 draft 期允许此值。"""

ANALYSIS_ROLES: Final[tuple[str, ...]] = (
    "baseline_total",
    "current_total",
    "current_by_dimension",
    "baseline_by_dimension",
)
"""固定四步模板角色顺序（ADR-0026 决策①，顺序即执行顺序，不得增删换位）。"""

MAX_SUB_PLANS: Final[int] = 4
"""固定四步模板长度上限（与 ANALYSIS_ROLES 一致；决策①）。"""

ANALYSIS_DIRECTIONS: Final[tuple[str, ...]] = ("change", "decrease", "increase")
"""direction 闭集：change（中性）/decrease/increase（表述倾向，不改变计算）。"""

UNAVAILABLE_REASON_CODES: Final[tuple[str, ...]] = (
    "zero_total_delta",
    "direction_mismatch",
    "possible_truncation",
    "empty_result",
    "multi_row_total",
    "column_mismatch",
    "non_finite_value",
    "null_metric_value",
    "duplicate_dimension_key",
    "reconciliation_mismatch",
    "missing_eligibility",
    "snapshot_mismatch",
)
"""unavailable 综合原因码闭集（决策②资格/完备性门 + 决策⑤综合门）：

- zero_total_delta：两期总量差为 0，贡献百分比无定义（决策⑤）；
- direction_mismatch / possible_truncation：加法资格门（决策②）；
- empty_result / multi_row_total / column_mismatch / non_finite_value /
  null_metric_value / duplicate_dimension_key / reconciliation_mismatch：
  完备性与对账门（决策②）；
- missing_eligibility / snapshot_mismatch：前置契约门（资格缺失 / 快照不一致）。
"""

TURN_TERMINAL_REASON_CODES: Final[tuple[str, ...]] = ("guard_blocked", "execution_error")
"""turn 终态原因码：guard_blocked（只读红线拦截）/ execution_error（执行失败）。"""

PRE_GATE_REASON_CODES: Final[tuple[str, ...]] = ("missing_eligibility", "snapshot_mismatch")
"""前置资格门原因码（T07 裁决）：这两个码只能出现在**零步**的 answer 终态——
资格/快照门在执行任何 SQL 之前判定，失败即裁剪，不存在"四步执行完却报前置门
失败"的形态。与综合期 unavailable 码构成步骤数双射：前置门码 ⇔ steps 为空，
其余一切 answer 情形 ⇔ steps 恰为 4。"""

ANALYSIS_REASON_CODES: Final[tuple[str, ...]] = UNAVAILABLE_REASON_CODES + (
    "guard_blocked",
    "execution_error",
)
"""分析原因码全闭集 = 综合原因码（前）+ turn 终态原因码（后），顺序即优先级。"""

ANALYSIS_PLAN_PROJECTION_KEYS: Final[tuple[str, ...]] = (
    "metric",
    "dimensions",
    "time",
    "filters",
    "order_by",
    "limit",
    "comparison",
)
"""canonical Plan 投影的固定 7 键（键序稳定，评测与 API 共用）。"""

AnalysisDirection = Literal["change", "decrease", "increase"]
AnalysisStatus = Literal["ok", "unavailable", "clarify", "blocked", "error"]
AttributionStatus = Literal["ok", "unavailable"]
AnalysisReasonCode = Literal[
    "zero_total_delta",
    "direction_mismatch",
    "possible_truncation",
    "empty_result",
    "multi_row_total",
    "column_mismatch",
    "non_finite_value",
    "null_metric_value",
    "duplicate_dimension_key",
    "reconciliation_mismatch",
    "missing_eligibility",
    "snapshot_mismatch",
    "guard_blocked",
    "execution_error",
]

ANALYSIS_STATUSES: Final[tuple[AnalysisStatus, ...]] = (
    "ok",
    "unavailable",
    "clarify",
    "blocked",
    "error",
)
"""分析终态闭集（expected_kind=answer 对应 ok/unavailable 两种综合结果）。"""

ATTRIBUTION_STATUSES: Final[tuple[AttributionStatus, ...]] = ("ok", "unavailable")
"""综合结果状态闭集（决策⑤：unavailable ≠ 执行失败，不产出 items/text 结论）。"""


# ---------------------------------------------------------------------------
# 契约类型（frozen dataclass；字段签名与 ADR-0026 T01 逐字一致）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalysisPlan:
    """多步分析计划：固定四步模板的规划产物（决策①）。

    plan 仅在问句成功解析后存在（clarify 未解析时为 None）。
    sub_plans 必须是 MAX_SUB_PLANS 个 Plan 实例，角色顺序与 ANALYSIS_ROLES 一致。
    notices 是 filter 值域归一记录（ADR-0016 §②，修复轮 1 起随 Plan 透出，
    不再在规划层静默丢弃）。
    """

    intent: str
    metric: str
    dimension: str | None
    baseline: TimeSpec
    current: TimeSpec
    filters: tuple[Filter, ...]
    direction: AnalysisDirection
    sub_plans: tuple[Plan, ...]
    synthesizer: str = "additive_delta_v1"
    recipe_version: int = 1
    notices: tuple[ValueNotice, ...] = ()


@dataclass(frozen=True)
class AttributionItem:
    """单维度取值的贡献项（决策⑤精确 Decimal 综合）。"""

    value: str
    baseline: Decimal | None
    current: Decimal | None
    delta: Decimal | None
    contribution_pct: Decimal | None


@dataclass(frozen=True)
class Attribution:
    """两期归因综合结果（决策⑤）：ok 带 items，unavailable 只带 reason_code。"""

    status: AttributionStatus
    baseline: Decimal | None
    current: Decimal | None
    delta: Decimal | None
    items: tuple[AttributionItem, ...]
    reason_code: AnalysisReasonCode | None
    text: str


@dataclass(frozen=True)
class AnalysisResult:
    """一次多步分析的整体产出：父轮 + 计划 + 步骤 + 综合 + 绑定信息。

    turn 是分析父轮（决策⑥：answer 父轮不携带单 SQL 结果，metric = 目标指标）；
    snapshot_sha / semantic_sha256 把结果绑定到执行时的快照与语义层版本。
    """

    turn: TurnResult
    plan: AnalysisPlan | None
    steps: tuple[TurnResult, ...]
    attribution: Attribution | None
    reason_code: AnalysisReasonCode | None
    elapsed_ms: float
    snapshot_sha: str | None
    semantic_sha256: str | None


# ---------------------------------------------------------------------------
# 纯函数：投影 / 校验（无 I/O、无编排）
# ---------------------------------------------------------------------------


def plan_projection(plan: Plan) -> dict[str, object]:
    """canonical Plan 投影：固定 7 键、键序稳定、值可直接 JSON 化。

    参数 plan：agent.compiler.Plan 实例。
    返回 dict：metric/dimensions/time/filters/order_by/limit/comparison，
    其中 time/comparison 为 None 或单层 dict，filters/order_by 为 dict 元组。
    """
    return {
        "metric": plan.metric,
        "dimensions": tuple(plan.dimensions),
        "time": None
        if plan.time is None
        else {"granularity": plan.time.granularity, "value": plan.time.value},
        "filters": tuple({"column": f.column, "op": f.op, "value": f.value} for f in plan.filters),
        "order_by": tuple({"column": o.column, "desc": o.desc} for o in plan.order_by),
        "limit": plan.limit,
        "comparison": None if plan.comparison is None else {"kind": plan.comparison.kind},
    }


def validate_analysis_plan(plan: AnalysisPlan) -> tuple[str, ...]:
    """校验 AnalysisPlan 是否符合固定四步模板（决策①）。

    检查：sub_plans 实例数量与类型、角色顺序、总量步形态（不分组/排序、limit=1）、
    分组步形态（维度升序单列）、time 角色绑定、四步共享 filters、comparison 为 None、
    direction 闭集、baseline/current 粒度一致。

    参数 plan：待校验的 AnalysisPlan。
    返回违规消息元组（空元组 = 合法）。
    """
    violations: list[str] = []
    if plan.direction not in ANALYSIS_DIRECTIONS:
        violations.append(f"direction 必须取闭集 {ANALYSIS_DIRECTIONS}，实际 {plan.direction!r}")
    if plan.baseline.granularity != plan.current.granularity:
        violations.append(
            f"baseline/current 粒度必须一致：{plan.baseline.granularity!r} ≠ "
            f"{plan.current.granularity!r}"
        )
    if len(plan.sub_plans) != MAX_SUB_PLANS:
        violations.append(
            f"sub_plans 长度必须为 {MAX_SUB_PLANS}（固定四步模板），实际 {len(plan.sub_plans)}"
        )
        return tuple(violations)
    non_plan = [i for i, sub in enumerate(plan.sub_plans) if not isinstance(sub, Plan)]
    for i in non_plan:
        violations.append(f"sub_plans[{i}] 必须为 Plan 实例（agent.compiler.Plan）")
    if non_plan:
        return tuple(violations)
    subs = plan.sub_plans
    violations.extend(_total_role_violations(plan, 0, "baseline_total", plan.baseline))
    violations.extend(_total_role_violations(plan, 1, "current_total", plan.current))
    violations.extend(_grouped_role_violations(plan, 2, "current_by_dimension", plan.current))
    violations.extend(_grouped_role_violations(plan, 3, "baseline_by_dimension", plan.baseline))
    for i, sub in enumerate(subs):
        if sub.metric != plan.metric:
            violations.append(
                f"sub_plans[{i}].metric 必须与 plan.metric 一致：{sub.metric!r} ≠ {plan.metric!r}"
            )
        if sub.filters != plan.filters:
            violations.append(
                f"sub_plans[{i}].filters 必须与 plan.filters 完全一致（四步共享同一 WHERE）"
            )
        if sub.comparison is not None:
            violations.append(
                f"sub_plans[{i}].comparison 必须为 None（分析比较用普通 Plan，决策①）"
            )
    return tuple(violations)


def _total_role_violations(
    plan: AnalysisPlan, index: int, role: str, expected_time: TimeSpec
) -> list[str]:
    """总量步形态检查：不分组、不排序、limit=1、time 绑定指定期。"""
    sub = plan.sub_plans[index]
    out = []
    if sub.time != expected_time:
        out.append(
            f"sub_plans[{index}] 应为 {role}：time 绑定错误（须为该角色对应期），实际 {sub.time!r}"
        )
    if sub.dimensions:
        out.append(f"sub_plans[{index}] 应为 {role}：总量步不得分组，实际 {sub.dimensions!r}")
    if sub.order_by:
        out.append(f"sub_plans[{index}] 应为 {role}：总量步不得排序，实际 {sub.order_by!r}")
    if sub.limit != 1:
        out.append(f"sub_plans[{index}] 应为 {role}：总量步 limit 必须为 1，实际 {sub.limit}")
    return out


def _grouped_role_violations(
    plan: AnalysisPlan, index: int, role: str, expected_time: TimeSpec
) -> list[str]:
    """分组步形态检查：恰按 plan.dimension 一列升序、time 绑定指定期。"""
    sub = plan.sub_plans[index]
    out = []
    if sub.time != expected_time:
        out.append(
            f"sub_plans[{index}] 应为 {role}：time 绑定错误（须为该角色对应期），实际 {sub.time!r}"
        )
    if plan.dimension is None or sub.dimensions != (plan.dimension,):
        out.append(
            f"sub_plans[{index}] 应为 {role}：dimensions 必须恰为 (plan.dimension,)，"
            f"实际 {sub.dimensions!r}"
        )
    expected_order = (OrderSpec(plan.dimension),) if plan.dimension is not None else ()
    if sub.order_by != expected_order:
        out.append(
            f"sub_plans[{index}] 应为 {role}：order_by 必须为按维度升序的单列排序，"
            f"实际 {sub.order_by!r}"
        )
    return out


def analysis_status(result: AnalysisResult) -> AnalysisStatus:
    """推导分析终态（闭合矩阵的判别函数）。

    规则：turn.kind 决定终态；answer 依 attribution.status 细分为 ok/unavailable。

    参数 result：分析整体产出。
    返回 AnalysisStatus 五值闭集之一。
    抛 ValueError：turn.kind=handoff（意外出口），或 answer 缺 attribution。
    """
    kind = result.turn.kind
    if kind == "clarify":
        return "clarify"
    if kind == "blocked":
        return "blocked"
    if kind == "error":
        return "error"
    if kind == "handoff":
        raise ValueError("handoff 不是分析终态（决策③意外出口），闭合矩阵拒绝")
    if result.attribution is None:
        raise ValueError("answer 终态必须携带 attribution（ok 或 unavailable）")
    return "ok" if result.attribution.status == "ok" else "unavailable"


def effective_reason_code(result: AnalysisResult) -> AnalysisReasonCode | None:
    """按优先关系推导有效原因码：turn 终态优先，attribution.reason_code 次之。

    参数 result：分析整体产出。
    返回原因码或 None（ok/clarify 无失败原因）。
    抛 ValueError：handoff 或 answer 缺 attribution（同 analysis_status）。
    """
    kind = result.turn.kind
    if kind == "blocked":
        return "guard_blocked"
    if kind == "error":
        return "execution_error"
    if kind == "clarify":
        return None
    if kind == "handoff":
        raise ValueError("handoff 不是分析终态（决策③意外出口），闭合矩阵拒绝")
    if result.attribution is None:
        raise ValueError("answer 终态必须携带 attribution（ok 或 unavailable）")
    if result.attribution.status == "ok":
        return None
    return result.attribution.reason_code


def validate_analysis_result(result: AnalysisResult) -> tuple[str, ...]:
    """校验 AnalysisResult 是否满足闭合矩阵（终态 × 字段组合 × 原因码）。

    检查：turn.kind 为合法终态；clarify 零 SQL 调用（plan/steps/attribution 为空、
    必带 clarification）；blocked/error 的 attribution 为空且原因码固定、steps 以
    失败步收尾（前缀只允许 answer）；answer 必带 attribution、步骤数双射（T07
    裁决：前置门失败码 ⇔ 零步，其余 ⇔ 恰 4 步）、父轮不冒充单 SQL 结果（决策⑥）、
    绑定快照/语义 sha；attribution 按 ok/unavailable 校验 items/原因码；
    result.reason_code 与优先关系推导一致。

    参数 result：分析整体产出。
    返回违规消息元组（空元组 = 合法）。
    """
    violations: list[str] = []
    kind = result.turn.kind
    if kind == "handoff":
        return ("turn.kind=handoff 不是分析终态（决策③意外出口），闭合矩阵拒绝",)
    if kind == "clarify":
        if result.plan is not None:
            violations.append("clarify 未解析出分析计划：plan 必须为 None")
        if result.steps:
            violations.append("clarify 零 SQL 调用：steps 必须为空")
        if result.attribution is not None:
            violations.append("clarify 无综合结果：attribution 必须为 None")
        if result.turn.clarification is None:
            violations.append("clarify 必须携带 turn.clarification（原因与候选）")
        if result.reason_code is not None:
            violations.append("clarify 无失败原因：reason_code 必须为 None")
        return tuple(violations)
    if kind == "blocked":
        if result.attribution is not None:
            violations.append("blocked 无 totals/items/text：attribution 必须为 None")
        if result.reason_code != "guard_blocked":
            violations.append(
                f"blocked 的 reason_code 必须为 guard_blocked，实际 {result.reason_code!r}"
            )
        violations.extend(_terminal_steps_violations(result.steps, "blocked"))
        return tuple(violations)
    if kind == "error":
        if result.attribution is not None:
            violations.append("error 无 totals/items/text：attribution 必须为 None")
        if result.reason_code != "execution_error":
            violations.append(
                f"error 的 reason_code 必须为 execution_error，实际 {result.reason_code!r}"
            )
        violations.extend(_terminal_steps_violations(result.steps, "error"))
        return tuple(violations)

    # answer：必须综合出 attribution，且父轮不冒充单 SQL 结果（决策⑥）
    if result.plan is None:
        violations.append("answer 必须携带 plan")
    # 步骤数双射（T07 裁决）：前置门失败（unavailable + missing_eligibility/
    # snapshot_mismatch）⇔ steps 为空；其余一切 answer 情形 ⇔ steps 恰为 4。
    pre_gate = (
        result.attribution is not None
        and result.attribution.status == "unavailable"
        and result.attribution.reason_code in PRE_GATE_REASON_CODES
    )
    if pre_gate and result.steps:
        violations.append(
            "前置资格门失败（missing_eligibility/snapshot_mismatch）不得携带已执行"
            f"步骤：steps 必须为空，实际 {len(result.steps)} 步（T07 裁决）"
        )
    elif not pre_gate and len(result.steps) != MAX_SUB_PLANS:
        violations.append(
            f"answer 步骤数必须为 {MAX_SUB_PLANS}（固定四步模板），实际 {len(result.steps)}"
        )
    if result.attribution is None:
        violations.append("answer 必须综合出 attribution（ok 或 unavailable）")
        return tuple(violations)
    bad_parent = []
    if result.turn.sql is not None:
        bad_parent.append("sql")
    if result.turn.explanation is not None:
        bad_parent.append("explanation")
    if result.turn.columns:
        bad_parent.append("columns")
    if result.turn.rows:
        bad_parent.append("rows")
    if result.turn.row_count != 0:
        bad_parent.append("row_count")
    if result.turn.time_column is not None:
        bad_parent.append("time_column")
    if result.turn.chart is not None:
        bad_parent.append("chart")
    if bad_parent:
        violations.append(f"分析父轮不得冒充单 SQL 结果（{', '.join(bad_parent)} 必须为空，决策⑥）")
    if result.plan is not None and result.turn.metric != result.plan.metric:
        violations.append("分析父轮 metric 必须等于 plan.metric（决策⑥）")
    if result.snapshot_sha is None or result.semantic_sha256 is None:
        violations.append("answer 必须绑定 snapshot_sha 与 semantic_sha256")
    attr = result.attribution
    if attr.status == "unavailable":
        if attr.items:
            violations.append("unavailable 不产生贡献项：items 必须为空（决策⑤）")
        if attr.reason_code is None or result.reason_code is None:
            violations.append(
                "unavailable 必须携带原因码（attribution.reason_code 与 "
                "result.reason_code 均不得为空）"
            )
        elif attr.reason_code not in UNAVAILABLE_REASON_CODES:
            violations.append(
                f"unavailable 原因码必须取综合闭集 {UNAVAILABLE_REASON_CODES}，"
                f"实际 {attr.reason_code!r}"
            )
    elif attr.status == "ok":
        if attr.reason_code is not None:
            violations.append("ok 综合无失败：attribution.reason_code 必须为 None")
        for i, item in enumerate(attr.items):
            incomplete = (
                item.baseline is None
                or item.current is None
                or item.delta is None
                or item.contribution_pct is None
            )
            if incomplete:
                violations.append(
                    f"items[{i}] 字段不完整（baseline/current/delta/contribution_pct "
                    "均不得为 None）"
                )
    else:
        violations.append(f"attribution.status 必须取 {ATTRIBUTION_STATUSES}，实际 {attr.status!r}")
    expected = effective_reason_code(result)
    if result.reason_code != expected:
        violations.append(
            f"reason_code 违反优先关系（turn 终态优先）：应为 {expected!r}，"
            f"实际 {result.reason_code!r}"
        )
    return tuple(violations)


def _terminal_steps_violations(steps: tuple[TurnResult, ...], terminal: str) -> list[str]:
    """blocked/error 的 steps 形态：answer 前缀 + 恰一个 terminal 收尾步。"""
    out: list[str] = []
    if not steps:
        out.append(f"{terminal} 终态必须至少携带 1 个 {terminal} 步骤")
        return out
    for i, step in enumerate(steps[:-1]):
        if step.kind != "answer":
            out.append(f"steps[{i}] 必须为 answer 前缀步，实际 {step.kind}")
    if steps[-1].kind != terminal:
        out.append(f"steps 最后一步必须为 {terminal}，实际 {steps[-1].kind}")
    return out


# ---------------------------------------------------------------------------
# ADR-0026 T03：AnalysisPlanner（双语分析槽位解析 + 固定四步 Plan 生成）
# ---------------------------------------------------------------------------

# ⚠️ 实施偏差声明（对 ADR-0026 决策①落点的偏离，留给后续任务收口）：
# 决策① 要求"分析形态词、连接词放 semantic/synonyms/patterns_{zh_cn,en_us}.yml"，
# 但 ADR-0015 的 pattern 词典是**封闭白名单**（agent/compiler.py `_PATTERN_SECTIONS`
# 限定顶层节，tests/test_locale_patterns.py 逐内容锁定）——新增 analysis 顶层节会在
# 模块 import 期 raise ValueError；放开白名单需改动本任务禁改文件（compiler.py、
# test_locale_patterns.py 等），故本任务把分析形态数据（意图词/连接词/维度槽位/
# 方向措辞）以具名模块常量收敛于此（数据驱动、单一消费点）。指标/维度同义词
# **不在此复制**——经 Planner 复用 patterns_*.yml 的既有注册表。后续任务放开
# pattern 白名单后，只需把本块外置为 patterns_*.yml 的 analysis 节并替换这里的
# 消费点，行为不变。

_ANALYSIS_INTENT_ID: Final[str] = "change_contribution"
"""AnalysisPlan.intent 固定值（T03 只产出"变化贡献分析"这一种意图）。"""

_ANALYSIS_INTENT_WORDS: Final[dict[str, tuple[str, ...]]] = {
    "zh": (
        "为什么",
        "为何",
        "什么原因",
        "原因",
        "归因",
        "变化贡献",
        "贡献分解",
        "贡献",
        "帮我分析",
        "分析一下",
        "分析",
        "拆解",
        "影响因素",
    ),
    "en": (
        "why",
        "contribution",
        "analyze",
        "analyse",
        "attribution",
        "break down",
        "breakdown",
        "explain",
        "what caused",
    ),
}
"""分析意图触发词（决策①：命中即进分析管线，缺槽澄清而不回落普通问数）。"""

_ANALYSIS_CONNECTOR_RES: Final[dict[str, re.Pattern[str]]] = {
    # zh 单字「比/较」仅在不与相邻中文字粘连时才算连接词（修复轮 1 #6）：
    # 「比值/比重/比较度」等名词词根中的单字不得派期间角色；多字词（含
    # 比较/相较）在前，独立单字在后。间隙切片以期间边界为界，切片首/尾的
    # 单字连接词（如「…季度比…季度」）不受切片外中文字影响。
    "zh": re.compile(r"相对|相比|对比|比较|相较|(?<![\u4e00-\u9fff])[较比](?![\u4e00-\u9fff])"),
    "en": re.compile(
        r"\b(?:compared\s+(?:with|to)|versus|vs\.?|against|relative\s+to)\b",
        re.IGNORECASE,
    ),
}
"""两期连接词（决策②：STRICT 匹配，且必须落在两个期间之间，否则角色不清）。"""

_ANALYSIS_DIM_SLOT_RES: Final[dict[str, re.Pattern[str]]] = {
    # 短语抓到"变化贡献/分解/拆解"等后缀或标点为止；短语内再做维度同义词匹配
    "zh": re.compile(
        r"按\s*(?P<phrase>[^，,。?？]+?)\s*(?:的)?\s*(?:变化贡献|贡献分解|贡献|分解|拆解)"
    ),
    # 短语抓到时间/对比介词或句尾为止；交替里**不含 and**——"by branch and tier"
    # 必须整段进入维度匹配，才能报"分组维度歧义"而不是漏掉第二个维度
    "en": re.compile(
        r"\b(?:grouped\s+by|broken\s+down\s+by|by)\s+"
        r"(?P<phrase>.+?)"
        r"(?=\s+(?:in|for|during|of|on|over|under|above|below|with|compared"
        r"|versus|vs)\b|\s*[?？]|$)",
        re.IGNORECASE,
    ),
}
"""分组维度槽位触发形态（决策①：缺维度/多维度都澄清，不猜）。"""

_ANALYSIS_DIRECTION_WORDS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "zh": {
        "decrease": ("下降", "下跌", "减少", "降低", "下滑"),
        "increase": ("上升", "上涨", "增长", "增加", "提高"),
    },
    "en": {
        "decrease": ("decrease", "decline", "drop", "fall", "fell", "reduc"),
        "increase": ("increase", "grow", "growth", "rise", "rose", "gain"),
    },
}
"""方向措辞（change/decrease/increase；两向措辞并存 = 方向不清 → 澄清）。"""

# en 方向词词边界匹配（修复轮 1 #2）：裸子串会把 "against" 误判为含 "gain"
# → increase。只锚左词边界：保留词干形态（reduc→reduced、drop→dropped、
# gain→gains），同时 "against/again" 因 g 前是词字符而无左边界，不再误命中。
_EN_DIRECTION_RES: Final[dict[str, tuple[re.Pattern[str], ...]]] = {
    kind: tuple(re.compile(rf"\b{re.escape(word)}") for word in words)
    for kind, words in _ANALYSIS_DIRECTION_WORDS["en"].items()
}


def _period_order_key(spec: TimeSpec) -> str:
    """同粒度期间的先后比较键：str 原样（季度/日期），int 年/月零填充 6 位。"""
    value = spec.value
    return value if isinstance(value, str) else f"{value:06d}"


class AnalysisPlanner:
    """问句 → AnalysisPlan 的确定性槽位解析器（ADR-0026 T03，无 LLM/无 I/O）。

    返回契约（三态，绝不回落普通问数）：
    - None：问句没有分析意图（普通问数继续走 Planner）；
    - ClarificationRequest：命中分析意图但缺槽/不支持（复用 Planner 的
      ClarificationRequest 与 kind 词表 {ambiguous, relative_time, unmatched}）；
    - AnalysisPlan：四槽齐备 + attribution_dimensions 资格 + 编译预检通过，
      sub_plans 为固定四步模板（决策①，角色序 = ANALYSIS_ROLES）。
    """

    def __init__(self, model: SemanticModel, *, group_limit: int) -> None:
        """group_limit：分组步 limit（决策②：编排层传 min(10000, budget.max_rows)）。"""
        self.model = model
        self.group_limit = group_limit
        self._planner = Planner(model)

    def plan(self, question: str) -> AnalysisPlan | ClarificationRequest | None:
        """解析分析问句；管线各步确定性、顺序固定（同输入必同输出）。"""
        locale = self._planner.resolve_locale(question, None)
        if not self._has_intent(question, locale):
            return None
        # 相对时间 → relative_time 澄清（与 Planner 同一检查、同一文案）
        spans_or = scan_time_spans(question, locale)
        if isinstance(spans_or, ClarificationRequest):
            return spans_or
        periods = self._pair_periods(question, locale, spans_or)
        if isinstance(periods, ClarificationRequest):
            return periods
        metric_hits = self._planner.match_metric(question, locale)
        if not metric_hits:
            return ClarificationRequest(
                question,
                ("无法确定指标口径（问句未命中任何指标同义词）",),
                kind="unmatched",
            )
        if len(metric_hits) > 1:
            return ClarificationRequest(
                question, ("指标口径歧义（命中多个指标）",), tuple(metric_hits)
            )
        metric = metric_hits[0]
        # 阈值/TopN 在 _parse_filters 之前拦截：阈值会被普通解析静默降为 WHERE
        # （实为 HAVING 语义），TopN 与"分组步按维度全量展开"的固定模板不兼容
        threshold_res: tuple[re.Pattern[str], ...] = (
            (EN_THRESHOLD_GT_RE, EN_THRESHOLD_LT_RE)
            if locale == "en"
            else (THRESHOLD_GT_RE, THRESHOLD_LT_RE)
        )
        if any(rx.search(question) for rx in threshold_res):
            return ClarificationRequest(
                question,
                (
                    "度量阈值属于 HAVING 语义（超过/大于/低于…），变更贡献分析"
                    "不支持，请去掉阈值条件",
                ),
            )
        topn_re = EN_TOP_N_RE if locale == "en" else TOP_N_RE
        if topn_re.search(question):
            return ClarificationRequest(
                question,
                ("TopN（前 N 名 / top N）与贡献分解不兼容：分组步按维度全量展开，请去掉 TopN",),
            )
        notices: list[ValueNotice] = []
        filters = self._planner.parse_filters(question, metric, locale, notices)
        if isinstance(filters, ClarificationRequest):
            return filters
        # 过滤触发命中计数（修复轮 1 #1）：_parse_filters 只解析每个 op 的首个触发
        # 命中，其余命中（混合片段、顺序在后的片段）在此显式计数——凡有未消费命中
        # 必须澄清，与"是否已解析出 filter"及片段出现顺序无关（未知条件不静默丢弃）。
        include_re = EN_ONLY_RE if locale == "en" else EQ_FILTER_RE
        exclude_re = EN_EXCL_RE if locale == "en" else EXC_FILTER_RE
        include_hits = sum(1 for _ in include_re.finditer(question))
        exclude_hits = sum(1 for _ in exclude_re.finditer(question))
        consumed_eq = sum(1 for f in filters if f.op == "=")
        consumed_ne = sum(1 for f in filters if f.op == "!=")
        if include_hits > consumed_eq or exclude_hits > consumed_ne:
            # 比 Planner 更严：普通问数可把无法归属的过滤片段当强调语丢弃，
            # 分析四步必须四步同 WHERE，未知条件静默丢弃会错算贡献
            return ClarificationRequest(
                question,
                (
                    "问句含无法归属到维度字段的过滤条件（未知条件不静默丢弃），"
                    "请明确维度与取值，如 只看分支A / only for branch A",
                ),
            )
        dimension_or = self._resolve_dimension(question, locale)
        if isinstance(dimension_or, ClarificationRequest):
            return dimension_or
        dimension = dimension_or
        declared = self.model.attribution_dimensions.get(metric, ())
        if dimension not in declared:
            return ClarificationRequest(
                question,
                (
                    f"组合未登记：{metric} 未声明按 {dimension} 归因的可加组合"
                    "（analysis.attribution.dimensions），无法做贡献分解",
                ),
            )
        direction_or = self._detect_direction(question, locale)
        if isinstance(direction_or, ClarificationRequest):
            return direction_or
        baseline_spec, current_spec = periods
        order = (OrderSpec(dimension),)
        # 固定四步模板（决策①，角色序 = ANALYSIS_ROLES）：总量步不分组不排序
        # limit=1；分组步按维度一列升序、limit=group_limit；四步同 WHERE 同指标。
        subs = (
            Plan(metric=metric, time=baseline_spec, filters=filters, limit=1),
            Plan(metric=metric, time=current_spec, filters=filters, limit=1),
            Plan(
                metric=metric,
                dimensions=(dimension,),
                time=current_spec,
                filters=filters,
                order_by=order,
                limit=self.group_limit,
            ),
            Plan(
                metric=metric,
                dimensions=(dimension,),
                time=baseline_spec,
                filters=filters,
                order_by=order,
                limit=self.group_limit,
            ),
        )
        analysis_plan = AnalysisPlan(
            intent=_ANALYSIS_INTENT_ID,
            metric=metric,
            dimension=dimension,
            baseline=baseline_spec,
            current=current_spec,
            filters=filters,
            direction=direction_or,
            sub_plans=subs,
            notices=tuple(notices),
        )
        violations = validate_analysis_plan(analysis_plan)
        if violations:  # pragma: no cover —— 固定模板自检，属内部契约错误
            raise ValueError("AnalysisPlanner 生成的模板自检失败：" + "；".join(violations))
        compiler = Compiler(self.model)
        for sub in subs:  # 编译预检：只走确定性编译器，不执行 SQL
            try:
                compiler.compile(sub)
            except CompileError as exc:
                return ClarificationRequest(question, (f"分析计划编译预检失败：{exc}",))
        return analysis_plan

    def _has_intent(self, question: str, locale: str) -> bool:
        """意图触发词命中（en 先 lower，与时间形态的相对词检查同一纪律）。"""
        text = question.lower() if locale == "en" else question
        return any(word in text for word in _ANALYSIS_INTENT_WORDS[locale])

    def _pair_periods(
        self, question: str, locale: str, spans: tuple[TimeSpan, ...]
    ) -> tuple[TimeSpec, TimeSpec] | ClarificationRequest:
        """两期拆分（决策②）：恰两个绝对期间、同粒度、连接词居间、基期不晚于本期。

        角色方向由连接词两侧先后决定：先出现 = 本期 current，后出现 = 基期
        baseline（与绑定样例「分析 2013Q4 相对 2013Q3 …」→ 基期 2013Q3 一致）。
        """
        if len(spans) < 2:
            return ClarificationRequest(
                question,
                (
                    "无法确定两个绝对期间：变更贡献分析需要两个 year/quarter/month/"
                    "date 绝对期间（不支持省略年份，也不支持相对时间）",
                ),
            )
        if len(spans) > 2:
            return ClarificationRequest(
                question,
                (f"命中 {len(spans)} 个期间，变更贡献分析只支持两期对比，请只保留两个期间",),
            )
        ordered = sorted(spans, key=lambda s: s.start)
        first, second = ordered[0], ordered[1]
        if not _ANALYSIS_CONNECTOR_RES[locale].search(question[first.end : second.start]):
            return ClarificationRequest(
                question,
                (
                    "时间角色不清：两个期间之间缺少对比连接词（相对/相比/比；"
                    " compared with/versus/vs/against/relative to），无法确定 "
                    f"{first.spec.value} 与 {second.spec.value} 哪个是本期",
                ),
            )
        # 先出现 = 本期，后出现 = 基期（绑定样例：A 相对 B → A 本期、B 基期）
        current_spec, baseline_spec = first.spec, second.spec
        if current_spec.granularity != baseline_spec.granularity:
            return ClarificationRequest(
                question,
                (
                    f"两期粒度必须一致：{baseline_spec.value}"
                    f"（{baseline_spec.granularity}）与 {current_spec.value}"
                    f"（{current_spec.granularity}）粒度不同",
                ),
            )
        if _period_order_key(current_spec) < _period_order_key(baseline_spec):
            return ClarificationRequest(
                question,
                (
                    f"本期（{current_spec.value}）早于基期（{baseline_spec.value}）："
                    "变更贡献分析要求基期不晚于本期",
                ),
            )
        return (baseline_spec, current_spec)

    def _resolve_dimension(self, question: str, locale: str) -> str | ClarificationRequest:
        """分组维度槽位：缺触发 → 缺维度澄清；命中 0/多个 → 澄清，恰一个 → 返回。"""
        m = _ANALYSIS_DIM_SLOT_RES[locale].search(question)
        if m is None:
            return ClarificationRequest(
                question,
                ("缺分组维度：请说明按什么维度分解贡献（如 按分支 / by branch）",),
            )
        phrase = m.group("phrase")
        hits = self._planner.dim_hits(phrase, locale)
        if not hits:
            return ClarificationRequest(
                question,
                (f"分组维度无法识别（{phrase!r} 未命中任何维度同义词）",),
                kind="unmatched",
            )
        if len(hits) > 1:
            return ClarificationRequest(question, ("分组维度歧义（命中多个维度）",), hits)
        return hits[0]

    def _detect_direction(
        self, question: str, locale: str
    ) -> AnalysisDirection | ClarificationRequest:
        """方向措辞 → change/decrease/increase；双向措辞并存 = 方向不清 → 澄清。

        en 走左词边界正则（修复轮 1 #2，防 "against" 误含 "gain"）；zh 仍用
        裸子串（多字措辞无误命中问题）。
        """
        text = question.lower() if locale == "en" else question
        if locale == "en":
            decrease = any(rx.search(text) for rx in _EN_DIRECTION_RES["decrease"])
            increase = any(rx.search(text) for rx in _EN_DIRECTION_RES["increase"])
        else:
            words = _ANALYSIS_DIRECTION_WORDS["zh"]
            decrease = any(w in text for w in words["decrease"])
            increase = any(w in text for w in words["increase"])
        if decrease and increase:
            return ClarificationRequest(
                question,
                ("方向表述冲突：问句同时包含上升与下降措辞，方向不清，请只保留一种",),
            )
        if decrease:
            return "decrease"
        if increase:
            return "increase"
        return "change"


# ---------------------------------------------------------------------------
# T04：synthesize —— 完整性检查与纯函数贡献计算（ADR-0026 决策②/⑤）
#
# 预评审更正（对照 ADR-0026 决策⑤原文逐条核正，原实施偏差声明已全部消除）：
# ① 净零总变化 → unavailable + zero_total_delta：顶层 baseline/current 保留已验证
#   两期值、delta=Decimal("0")，items 置空（L207）；
# ② 数值输入闭包收紧为 int（不含 bool）/ 有限 Decimal；float（含有限值如 60.5）
#   与 str 等未声明类型一律 non_finite_value 拒绝（L201）；
# ③ 贡献百分数量化 ROUND_HALF_EVEN（L203）；
# ④ |delta| 平局按带类型的规范化维度键升序（L211）；
# ⑤ 编排违约（steps/sub_plans 数≠4、意外 clarify/handoff）raise ValueError，
#   步 metric 失配 → column_mismatch（决策③）。
# ---------------------------------------------------------------------------

_SYNTH_PREC: Final[int] = 50
"""synthesize 显式算术上下文精度：所有加减乘除、比较排序键与量化均在
localcontext(prec=_SYNTH_PREC) 内进行，不受全局 decimal.getcontext() 影响。
精确算术包络：输入十进制跨度 ≤ 约 40 位有效数字（常规金融数值 ≤ 19 位）端到端
精确；超出包络的病态输入在量化步触发 DecimalException，统一按 non_finite_value
拒绝（拒绝而非臆测，AGENTS.md N1）。"""

_PCT_QUANT: Final[Decimal] = Decimal("0.000001")
"""贡献百分数量化单位：6 位小数（决策⑤ L203，ROUND_HALF_EVEN）。"""

_NULL_DIM_LABEL: Final[str] = "(null)"
"""NULL 维度独立桶的展示标签：NULL ≠ 任何字符串（如“未知”），永不合并。"""


class _ItemDraft(NamedTuple):
    """ok 综合单项草稿（排序前中间态；key 为维度行内原始值，NULL 为 None）。"""

    key: object
    baseline: Decimal
    current: Decimal
    delta: Decimal
    order_key: Decimal  # abs(delta)：显式上下文内预计算（Decimal 比较不依赖上下文）
    contribution_pct: Decimal | None


def _unavailable_attribution(plan: AnalysisPlan, reason: AnalysisReasonCode) -> Attribution:
    """unavailable 固定形状：空 items、空 totals、固定措辞文本（无臆测无百分数）。

    参数 plan：目标分析计划（仅取 metric 渲染固定文本）。
    参数 reason：UNAVAILABLE_REASON_CODES 闭集内的原因码。
    返回 Attribution：status="unavailable"，baseline/current/delta=None，items=()。
    """
    return Attribution(
        status="unavailable",
        baseline=None,
        current=None,
        delta=None,
        items=(),
        reason_code=reason,
        text=f"无法给出 {plan.metric} 的变化贡献分解（原因码：{reason}）。",
    )


def _net_zero_attribution(plan: AnalysisPlan, baseline: Decimal, current: Decimal) -> Attribution:
    """净零总变化固定形状（决策⑤ L207 + 预评审更正①）：unavailable + zero_total_delta。

    顶层 baseline/current 保留已验证的两期总量、delta=Decimal("0")——已验证事实
    不丢弃；items 置空（净零下贡献百分比无定义，不产出任何百分数）；固定文本
    陈述净零事实，无百分数、无原因臆测。
    """
    return Attribution(
        status="unavailable",
        baseline=baseline,
        current=current,
        delta=Decimal("0"),
        items=(),
        reason_code="zero_total_delta",
        text=(
            f"无法给出 {plan.metric} 的变化贡献分解（原因码：zero_total_delta）："
            f"{plan.current.value} 相对 {plan.baseline.value} "
            "两期总量相同（净零变化），贡献百分比无定义。"
        ),
    )


def _step_kind_reason(kind: TurnKind) -> AnalysisReasonCode | None:
    """步骤终态映射：answer 放行；blocked→guard_blocked；error→execution_error。

    预评审更正⑤（决策③）：clarify/handoff 是非预期的分析子步骤终态——内部契约
    错误不容错，直接 raise ValueError（防御转为契约断言）。
    """
    if kind == "answer":
        return None
    if kind == "blocked":
        return "guard_blocked"
    if kind == "error":
        return "execution_error"
    raise ValueError(f"分析子步骤出现意外终态 kind={kind!r}：内部契约违约（ADR-0026 决策③）")


def _metric_cell(raw: object) -> Decimal | AnalysisReasonCode:
    """度量单元格解析：成功返回精确 Decimal；失败返回拒绝原因码。

    类型闭包（决策⑤ L201 + 预评审更正②）：仅接受 int（bool 除外）与有限 Decimal；
    bool、float（含有限值如 60.5）、str 等未声明类型一律 non_finite_value 拒绝
    （不补零不跳过不转换）。列名（columns 中的 str）与维度值列（str/None）不在
    本函数管辖内，不受影响。

    - None → null_metric_value（NULL 度量，不补零）；
    - Decimal：is_finite() 为真则原样返回，否则 non_finite_value；
    - int（bool 除外）→ Decimal(int)：int 构造不经上下文，精确；
    - 其余类型（bool/float/str/…）→ non_finite_value。
    """
    if raw is None:
        return "null_metric_value"
    if isinstance(raw, Decimal):
        return raw if raw.is_finite() else "non_finite_value"
    if isinstance(raw, bool):
        return "non_finite_value"
    if isinstance(raw, int):
        return Decimal(raw)
    return "non_finite_value"


def _column_index(columns: tuple[str, ...], name: str) -> int | None:
    """按列名定位（首次出现）；不存在返回 None —— 不依赖列位置猜测。"""
    for idx, col in enumerate(columns):
        if col == name:
            return idx
    return None


def _total_step_value(step: TurnResult, metric: str) -> Decimal | AnalysisReasonCode:
    """总量步解析：按列名定位指标列，且恰好一行有效数值。

    列缺失或行宽与列数不符 → column_mismatch；0 行 → empty_result；
    >1 行 → multi_row_total；单元格 NULL → null_metric_value；
    非有限/未声明类型 → non_finite_value。成功返回该行指标值。
    """
    metric_idx = _column_index(step.columns, metric)
    if metric_idx is None:
        return "column_mismatch"
    if len(step.rows) == 0:
        return "empty_result"
    if len(step.rows) > 1:
        return "multi_row_total"
    row = step.rows[0]
    if len(row) != len(step.columns):
        return "column_mismatch"
    return _metric_cell(row[metric_idx])


def _grouped_step_rows(
    step: TurnResult,
    dimension: str,
    metric: str,
) -> dict[object, Decimal] | AnalysisReasonCode:
    """分组步解析：按列名定位维度/指标列，返回有序映射 {维度原始键: 度量值}。

    完备性门禁（决策②，命中即整步拒绝，不补零不跳过）：列缺失或行宽与列数不符
    → column_mismatch；NULL 度量 → null_metric_value；非有限/未声明类型 →
    non_finite_value；同期维度键重复 → duplicate_dimension_key。
    维度键为行内原始值（NULL 为 None，独立桶；DB 标量天然可哈希）。
    """
    dim_idx = _column_index(step.columns, dimension)
    metric_idx = _column_index(step.columns, metric)
    if dim_idx is None or metric_idx is None:
        return "column_mismatch"
    rows: dict[object, Decimal] = {}
    for row in step.rows:
        if len(row) != len(step.columns):
            return "column_mismatch"
        cell = _metric_cell(row[metric_idx])
        if not isinstance(cell, Decimal):
            return cell
        key = row[dim_idx]
        if key in rows:
            return "duplicate_dimension_key"
        rows[key] = cell
    return rows


def _signed(value: Decimal) -> str:
    """固定带符号渲染：正数加 '+' 前缀；负数与 0 由 Decimal 原生渲染。"""
    return f"+{value}" if value > 0 else f"{value}"


def _dimension_label(key: object) -> str:
    """维度键 → 展示文本：NULL 桶渲染为 "(null)"，其余 str()（不改写不归并）。"""
    return _NULL_DIM_LABEL if key is None else str(key)


def _typed_dimension_key(key: object) -> tuple[str, ...]:
    """维度键的平局排序键（决策⑤ L211 + 预评审更正④）：带类型的规范化键元组。

    - None（NULL 桶）→ ("null",)：NULL ≠ 任何字符串；类型标签 "null" < "str"
      保证 NULL 桶排在所有字符串键之前（全序）；
    - str → ("str", key)：同型字符串按字典序；
    - 其他 DB 标量 → (类型名, str(key))：不同类型有不同前缀标签，元组逐位比较
      在标签处即分出先后，任意标量之间不触发跨类型比较异常。
    """
    if key is None:
        return ("null",)
    if isinstance(key, str):
        return ("str", key)
    return (type(key).__name__, str(key))


def synthesize(plan: AnalysisPlan, steps: tuple[TurnResult, ...]) -> Attribution:
    """四步执行事实 → 精确 Decimal 两期贡献综合（ADR-0026 决策②/⑤，纯函数）。

    做什么：按 ANALYSIS_ROLES 顺序消费 (baseline_total, current_total,
    current_by_dimension, baseline_by_dimension) 四个子步骤，依次执行前置契约门、
    完备性与对账门禁（按列名定位、总量恰一行、截断、NULL/非有限/重复键拒绝、
    两期分别对账），全部通过后做并集对齐与单期缺失补零，产出 |delta| 降序
    （平局按带类型的规范化维度键升序，决策⑤ L211 + 预评审更正④）的
    AttributionItem 列表与固定措辞文本。所有算术在显式 localcontext
    （prec=_SYNTH_PREC）内进行，与全局 decimal 上下文无关；贡献百分数量化到
    6 位小数（ROUND_HALF_EVEN，决策⑤ L203 + 预评审更正③）。

    参数 plan：AnalysisPlan（sub_plans 必须为 4 个；分组步 limit 为有效 LIMIT L）。
    参数 steps：四个 TurnResult 执行事实（元组顺序即 ANALYSIS_ROLES 角色顺序）。

    返回：Attribution。status="ok" 时 baseline/current/delta 非 None、items 按
    |delta| 降序（平局按 _typed_dimension_key 升序）、每项四字段全非 None。
    status="unavailable" 时固定形状分两种：
    - 一般拒绝：items=()、baseline/current/delta 全 None、reason_code ∈
      UNAVAILABLE_REASON_CODES、固定文本无原因臆测、无百分数；
    - 净零总变化（total_delta == 0，决策⑤ L207 + 预评审更正①）：
      reason_code="zero_total_delta"，baseline/current 保留已验证的两期总量、
      delta=Decimal("0")（已验证事实不丢弃），items=()（贡献百分比无定义），
      固定文本陈述净零事实、无百分数、无臆测。

    步 metric 与 plan.metric 不符 → column_mismatch 的选码说明（预评审更正⑤）：
    missing_eligibility 指"计划级分析资格"（eligibility），而计划至此已具备资格
    （dimension 已声明、模板已成型）；执行步绑定到了别的 metric 属于执行事实与
    计划的绑定失配，column_mismatch 语义最贴近，故选之。

    Raises:
        ValueError: 内部编排契约违约（ADR-0026 决策③：意外 clarify/handoff 视为
            内部契约错误）——steps 长度≠4、plan.sub_plans 数≠4，或任一步骤 kind
            为 clarify/handoff。synthesize 不容错编排违约（防御转为契约断言）。

    unavailable 何时发生（首个命中即返回，按下列优先级互斥）：
    - guard_blocked / execution_error：任一步骤 kind 为 blocked / error；
    - column_mismatch：plan.dimension 为 None，或 answer 步 metric 与 plan.metric
      不符（见上述选码说明），或任一步列名定位失败（总量步缺指标列、分组步缺
      维度或指标列、行宽与列数不符）；
    - empty_result / multi_row_total：任一总量步行数不为 1；
    - null_metric_value / non_finite_value：总量或分组单元格为 NULL / 非有限或
      未声明类型（bool/float/str 等，决策⑤ L201 + 预评审更正②；含超出精确算术
      包络触发 DecimalException 的病态输入）；
    - possible_truncation：分组步 row_count 或行数触及有效 LIMIT（>= L，含 == L）；
    - duplicate_dimension_key：同一期内维度键重复；
    - reconciliation_mismatch：任一期分组和 ≠ 该期总量（两期分别核对，不只核 delta）；
    - direction_mismatch：计划方向与总 delta 符号相反（direction="change" 永不触发）；
    - zero_total_delta：两期总量相同（净零，见上）。
    """
    # -- 前置契约门：步数与计划模板形态（预评审更正⑤：编排违约不容错，raise） --
    if len(steps) != MAX_SUB_PLANS:
        raise ValueError(
            f"synthesize 需要 {MAX_SUB_PLANS} 个子步骤，实际收到 {len(steps)} 个："
            "内部编排契约违约（ADR-0026 决策③）"
        )
    if len(plan.sub_plans) != MAX_SUB_PLANS:
        raise ValueError(
            f"AnalysisPlan.sub_plans 必须为 {MAX_SUB_PLANS} 个，实际 "
            f"{len(plan.sub_plans)} 个：内部契约违约（ADR-0026 决策③）"
        )

    # -- 终态门：blocked/error → unavailable；意外 clarify/handoff → ValueError --
    for step in steps:
        reason = _step_kind_reason(step.kind)
        if reason is not None:
            return _unavailable_attribution(plan, reason)

    # -- 角色绑定门：answer 步 metric 必须与 plan.metric 一致（顺序即角色，不猜） --
    for step in steps:
        if step.metric != plan.metric:
            return _unavailable_attribution(plan, "column_mismatch")

    # -- 列定位门：分组维度必须在计划中声明 --
    if plan.dimension is None:
        return _unavailable_attribution(plan, "column_mismatch")

    # -- 总量步：列名定位 + 恰一行 + 有效数值（基期先于本期） --
    baseline_out = _total_step_value(steps[0], plan.metric)
    if not isinstance(baseline_out, Decimal):
        return _unavailable_attribution(plan, baseline_out)
    current_out = _total_step_value(steps[1], plan.metric)
    if not isinstance(current_out, Decimal):
        return _unavailable_attribution(plan, current_out)

    # -- 截断门：分组步行数触及有效 LIMIT（>= L，含 == L；对账门不能替代） --
    for pos in (2, 3):
        grouped_step = steps[pos]
        effective_limit = plan.sub_plans[pos].limit
        if max(grouped_step.row_count, len(grouped_step.rows)) >= effective_limit:
            return _unavailable_attribution(plan, "possible_truncation")

    # -- 分组步扫描：NULL/非有限/重复键整步拒绝（两期分组键分别唯一） --
    current_rows = _grouped_step_rows(steps[2], plan.dimension, plan.metric)
    if not isinstance(current_rows, dict):
        return _unavailable_attribution(plan, current_rows)
    baseline_rows = _grouped_step_rows(steps[3], plan.dimension, plan.metric)
    if not isinstance(baseline_rows, dict):
        return _unavailable_attribution(plan, baseline_rows)

    # -- 精确算术核：全部在显式 localcontext 内，与全局 decimal 上下文无关 --
    try:
        with localcontext() as ctx:
            ctx.prec = _SYNTH_PREC
            # 两期分别对账（分组和 == 各自总量；不能只核对 delta）
            if (
                sum(current_rows.values(), Decimal(0)) != current_out
                or sum(baseline_rows.values(), Decimal(0)) != baseline_out
            ):
                return _unavailable_attribution(plan, "reconciliation_mismatch")

            total_delta = current_out - baseline_out
            # 方向门（加法资格门）：direction="change" 永不不符
            if (plan.direction == "increase" and total_delta < 0) or (
                plan.direction == "decrease" and total_delta > 0
            ):
                return _unavailable_attribution(plan, "direction_mismatch")

            # 净零总变化（决策⑤ L207 + 预评审更正①）：贡献百分比无定义，
            # 不产出 ok/百分数；保留已验证两期值与 delta=0，items 置空
            if total_delta == 0:
                return _net_zero_attribution(plan, baseline_out, current_out)

            # 并集对齐（current 行序优先，baseline-only 追加在后）；完整性已通过，
            # 单期缺失的组在此按零处理（Decimal(0) 构造精确、不经上下文）
            merged: dict[object, list[Decimal | None]] = {}
            for key, value in current_rows.items():
                merged[key] = [None, value]
            for key, value in baseline_rows.items():
                if key in merged:
                    merged[key][0] = value
                else:
                    merged[key] = [value, None]

            zero = Decimal(0)
            drafts: list[_ItemDraft] = []
            for key, pair in merged.items():
                baseline_value = zero if pair[0] is None else pair[0]
                current_value = zero if pair[1] is None else pair[1]
                delta = current_value - baseline_value
                # 净零路径已提前返回，此处 total_delta ≠ 0，除法安全；
                # 量化 6 位 ROUND_HALF_EVEN（决策⑤ L203）
                pct = (delta / total_delta * Decimal(100)).quantize(
                    _PCT_QUANT, rounding=ROUND_HALF_EVEN
                )
                if pct == 0:
                    pct = zero.quantize(_PCT_QUANT)  # 规范化 -0 → +0（显示一致）
                drafts.append(
                    _ItemDraft(
                        key=key,
                        baseline=baseline_value,
                        current=current_value,
                        delta=delta,
                        order_key=abs(delta),
                        contribution_pct=pct,
                    )
                )
    except DecimalException:
        # 超出精确算术包络（如百分数量化需要 > prec 位数字）→ 拒绝而非臆测
        return _unavailable_attribution(plan, "non_finite_value")

    # -- 排序（决策⑤ L211 + 预评审更正④）：|delta| 降序；平局按带类型的规范化
    # 维度键升序。两趟稳定排序：先按次键（类型化键）升序，再按主键 |delta| 降序
    # （稳定排序保留次键顺序；不走单趟取负键——Decimal 取负经上下文舍入，
    # 会破坏 prec 无关性；order_key=abs(delta) 预计算于显式上下文内） --
    drafts.sort(key=lambda draft: _typed_dimension_key(draft.key))
    drafts.sort(key=lambda draft: draft.order_key, reverse=True)

    period_pair = f"{plan.current.value} 相对 {plan.baseline.value}"
    segments = "、".join(
        f"{_dimension_label(draft.key)} {_signed(draft.delta)}（贡献 {draft.contribution_pct}%）"
        for draft in drafts
    )
    text = (
        f"{plan.metric} 变化贡献分解：{period_pair} 总变化 {total_delta}"
        f"（{plan.baseline.value} → {plan.current.value}）。"
        f"按 {plan.dimension}：{segments}。这是变化贡献分解，不代表业务因果。"
    )

    items = tuple(
        AttributionItem(
            value=_dimension_label(draft.key),
            baseline=draft.baseline,
            current=draft.current,
            delta=draft.delta,
            contribution_pct=draft.contribution_pct,
        )
        for draft in drafts
    )
    return Attribution(
        status="ok",
        baseline=baseline_out,
        current=current_out,
        delta=total_delta,
        items=items,
        reason_code=None,
        text=text,
    )
