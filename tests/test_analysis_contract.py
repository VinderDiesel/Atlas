"""ADR-0026 T01 数据契约测试（TDD：本文件先于实现编写并确认红灯）。

覆盖面（对应 task-1-brief 复选框）：
1. agent/analysis.py frozen 类型与逐字字段签名（synthesizer/recipe_version 默认值）；
2. canonical Plan 投影：固定 7 键 metric/dimensions/time/filters/order_by/limit/comparison；
3. AnalysisPlan 模板契约断言：实例长度 / 角色顺序 / 全 Plan 类型 / 总量与分组形态；
4. AnalysisResult 闭合矩阵：五终态字段组合 + 原因码优先关系（turn 终态优先）；
5. eval/analysis/schema.json 负例击穿：缺第二期 / 未知字段 / 重复角色 / 错步序 /
   answer 缺期望结果 / clarify SQL 数非 0 / ready 占位 sha；blocked 不要求伪造结果；
6. 两个 draft 样本（attribution-001 / clarify-001）双重校验（schema + Python 契约）；
7. semantic.lint.check_analysis_schema 发现 analysis 目录内非法文件（不止扫 gold）。

所有数值均为合成测试输入（AGENTS.md N1：非实测业务数字，仅证契约形态）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path

import jsonschema

from agent.analysis import (
    ANALYSIS_PLAN_PROJECTION_KEYS,
    ANALYSIS_REASON_CODES,
    ANALYSIS_ROLES,
    ANALYSIS_STATUSES,
    ATTRIBUTION_STATUSES,
    MAX_SUB_PLANS,
    PENDING,
    UNAVAILABLE_REASON_CODES,
    AnalysisPlan,
    AnalysisResult,
    Attribution,
    AttributionItem,
    analysis_status,
    effective_reason_code,
    plan_projection,
    validate_analysis_plan,
    validate_analysis_result,
)
from agent.compiler import ComparisonSpec, Filter, OrderSpec, Plan, TimeSpec
from agent.planner import ClarificationRequest
from agent.state import TurnResult
from semantic.lint import check_analysis_schema

REPO = Path(__file__).resolve().parent.parent
ANALYSIS_SCHEMA_PATH = REPO / "eval" / "analysis" / "schema.json"
ANALYSIS_DIR = REPO / "eval" / "analysis"

METRIC = "commission_revenue"
DIMENSION = "Branch"
BASELINE = TimeSpec("quarter", "2013Q3")
CURRENT = TimeSpec("quarter", "2013Q4")


# ---------------------------------------------------------------------------
# 合成构造器（测试输入，非业务实测值）
# ---------------------------------------------------------------------------


def _baseline_plan() -> AnalysisPlan:
    """符合 ADR-0026 决策①固定模板的合法 AnalysisPlan。"""
    return AnalysisPlan(
        intent="attribution",
        metric=METRIC,
        dimension=DIMENSION,
        baseline=BASELINE,
        current=CURRENT,
        filters=(),
        direction="change",
        sub_plans=(
            Plan(METRIC, time=BASELINE, limit=1),
            Plan(METRIC, time=CURRENT, limit=1),
            Plan(
                METRIC,
                dimensions=(DIMENSION,),
                time=CURRENT,
                order_by=(OrderSpec(DIMENSION),),
                limit=10000,
            ),
            Plan(
                METRIC,
                dimensions=(DIMENSION,),
                time=BASELINE,
                order_by=(OrderSpec(DIMENSION),),
                limit=10000,
            ),
        ),
    )


def _answer_step(role_hint: str = "baseline_total") -> TurnResult:
    """合成 answer 子步骤（rows 为测试输入，非快照实测值）。"""
    return TurnResult(
        kind="answer",
        session_id="s-analysis-contract",
        question=f"step:{role_hint}",
        metric=METRIC,
        sql=f"SELECT 1 -- {role_hint}",
        columns=("total",),
        rows=((Decimal("100"),),),
        row_count=1,
        latency_ms=1.0,
    )


def _parent_turn(kind: str, **kwargs: object) -> TurnResult:
    return TurnResult(kind=kind, session_id="s-analysis-contract", question="q", **kwargs)  # type: ignore[arg-type]


def _ok_attribution() -> Attribution:
    """合成 ok 综合结果（数值为测试输入；两期分组对账成立）。"""
    return Attribution(
        status="ok",
        baseline=Decimal("300"),
        current=Decimal("260"),
        delta=Decimal("-40"),
        items=(
            AttributionItem(
                value="A",
                baseline=Decimal("180"),
                current=Decimal("90"),
                delta=Decimal("-90"),
                contribution_pct=Decimal("225.000000"),
            ),
            AttributionItem(
                value="B",
                baseline=Decimal("120"),
                current=Decimal("170"),
                delta=Decimal("50"),
                contribution_pct=Decimal("-125.000000"),
            ),
        ),
        reason_code=None,
        text="2013Q4 相对 2013Q3 的变化贡献分解（不代表业务因果）。",
    )


def _ok_result() -> AnalysisResult:
    return AnalysisResult(
        turn=_parent_turn("answer", metric=METRIC),
        plan=_baseline_plan(),
        steps=tuple(_answer_step(role) for role in ANALYSIS_ROLES),
        attribution=_ok_attribution(),
        reason_code=None,
        elapsed_ms=12.5,
        snapshot_sha="abc1234",
        semantic_sha256="deadbeef",
    )


def _unavailable_result(reason: str = "zero_total_delta") -> AnalysisResult:
    attribution = Attribution(
        status="unavailable",
        baseline=Decimal("100"),
        current=Decimal("100"),
        delta=Decimal("0"),
        items=(),
        reason_code=reason,  # type: ignore[arg-type]
        text="",
    )
    return AnalysisResult(
        turn=_parent_turn("answer", metric=METRIC),
        plan=_baseline_plan(),
        steps=tuple(_answer_step(role) for role in ANALYSIS_ROLES),
        attribution=attribution,
        reason_code=reason,  # type: ignore[arg-type]
        elapsed_ms=8.0,
        snapshot_sha="abc1234",
        semantic_sha256="deadbeef",
    )


def _clarify_result() -> AnalysisResult:
    return AnalysisResult(
        turn=_parent_turn(
            "clarify",
            clarification=ClarificationRequest(
                question="分析 2013Q4 相对上季度的佣金收入变化贡献",
                reasons=("期间含相对时间表达，需绝对期间",),
            ),
        ),
        plan=None,
        steps=(),
        attribution=None,
        reason_code=None,
        elapsed_ms=1.0,
        snapshot_sha=None,
        semantic_sha256=None,
    )


def _blocked_result(with_prefix: bool = True) -> AnalysisResult:
    failed = TurnResult(
        kind="blocked",
        session_id="s-analysis-contract",
        question="step:current_total",
        block_reason="非只读语句被拒绝",
    )
    steps: tuple[TurnResult, ...] = (_answer_step(), failed) if with_prefix else (failed,)
    return AnalysisResult(
        turn=_parent_turn("blocked", block_reason="非只读语句被拒绝"),
        plan=_baseline_plan(),
        steps=steps,
        attribution=None,
        reason_code="guard_blocked",
        elapsed_ms=3.0,
        snapshot_sha="abc1234",
        semantic_sha256="deadbeef",
    )


def _error_result() -> AnalysisResult:
    failed = TurnResult(
        kind="error",
        session_id="s-analysis-contract",
        question="step:baseline_total",
        error="数据库连接失败",
    )
    return AnalysisResult(
        turn=_parent_turn("error", error="数据库连接失败"),
        plan=_baseline_plan(),
        steps=(failed,),
        attribution=None,
        reason_code="execution_error",
        elapsed_ms=2.0,
        snapshot_sha="abc1234",
        semantic_sha256="deadbeef",
    )


# ---------------------------------------------------------------------------
# 1) 类型签名与 frozen
# ---------------------------------------------------------------------------


class TestTypeSignatures(unittest.TestCase):
    """brief 逐字签名：字段名与默认值必须与 ADR-0026 T01 完全一致。"""

    def test_analysis_plan_defaults(self) -> None:
        """仅 synthesizer/recipe_version 有默认值，且默认值逐字一致。"""
        plan = AnalysisPlan(
            intent="attribution",
            metric=METRIC,
            dimension=DIMENSION,
            baseline=BASELINE,
            current=CURRENT,
            filters=(),
            direction="change",
            sub_plans=(),
        )
        self.assertEqual(plan.synthesizer, "additive_delta_v1")
        self.assertEqual(plan.recipe_version, 1)

    def test_frozen(self) -> None:
        """四类契约类型全部 frozen dataclass（对任意属性赋值都拒绝）。"""
        for obj in (
            _baseline_plan(),
            _ok_attribution(),
            _ok_attribution().items[0],
            _ok_result(),
        ):
            with self.assertRaises(FrozenInstanceError):
                obj.status = "x"  # type: ignore[misc]

    def test_pending_placeholder(self) -> None:
        """draft 样本占位符常量与 AGENTS.md §9.3 约定一致。"""
        self.assertEqual(PENDING, "<待填写>")

    def test_role_constants(self) -> None:
        """固定四角色常量与 ADR-0026 决策①顺序逐字一致；MAX_SUB_PLANS=4。"""
        self.assertEqual(
            ANALYSIS_ROLES,
            (
                "baseline_total",
                "current_total",
                "current_by_dimension",
                "baseline_by_dimension",
            ),
        )
        self.assertEqual(MAX_SUB_PLANS, 4)

    def test_reason_code_closed_set(self) -> None:
        """原因码闭集 = turn 终态码 + 综合不可用码，无未登记成员。"""
        self.assertEqual(
            ANALYSIS_REASON_CODES,
            UNAVAILABLE_REASON_CODES + ("guard_blocked", "execution_error"),
        )


# ---------------------------------------------------------------------------
# 2) canonical Plan 投影
# ---------------------------------------------------------------------------


class TestPlanProjection(unittest.TestCase):
    """canonical Plan 投影：固定 7 键、键序稳定、值可复算。"""

    def test_projection_keys_fixed(self) -> None:
        proj = plan_projection(_baseline_plan().sub_plans[0])
        self.assertEqual(tuple(proj), ANALYSIS_PLAN_PROJECTION_KEYS)
        self.assertEqual(len(proj), 7)

    def test_projection_values(self) -> None:
        plan = Plan(
            METRIC,
            dimensions=(DIMENSION,),
            time=CURRENT,
            filters=(Filter("Branch", "=", "A"),),
            order_by=(OrderSpec(DIMENSION, True),),
            limit=50,
            comparison=ComparisonSpec("yoy"),
        )
        proj = plan_projection(plan)
        self.assertEqual(proj["metric"], METRIC)
        self.assertEqual(proj["dimensions"], (DIMENSION,))
        self.assertEqual(proj["time"], {"granularity": "quarter", "value": "2013Q4"})
        self.assertEqual(proj["filters"], ({"column": "Branch", "op": "=", "value": "A"},))
        self.assertEqual(proj["order_by"], ({"column": "Branch", "desc": True},))
        self.assertEqual(proj["limit"], 50)
        self.assertEqual(proj["comparison"], {"kind": "yoy"})

    def test_projection_none_fields(self) -> None:
        proj = plan_projection(Plan(METRIC))
        self.assertIsNone(proj["time"])
        self.assertIsNone(proj["comparison"])
        self.assertEqual(proj["filters"], ())
        self.assertEqual(proj["order_by"], ())


# ---------------------------------------------------------------------------
# 3) AnalysisPlan 模板契约（实例长度 / 角色顺序 / 全 Plan 类型）
# ---------------------------------------------------------------------------


class TestAnalysisPlanContract(unittest.TestCase):
    def test_valid_template_zero_violations(self) -> None:
        self.assertEqual(validate_analysis_plan(_baseline_plan()), ())

    def test_wrong_length_flagged(self) -> None:
        plan = replace(_baseline_plan(), sub_plans=_baseline_plan().sub_plans[:3])
        violations = validate_analysis_plan(plan)
        self.assertTrue(any("长度" in v for v in violations), violations)

    def test_role_order_flagged(self) -> None:
        """两个分组 Plan 顺序颠倒（当期分组在前）必须报角色顺序违规。"""
        subs = list(_baseline_plan().sub_plans)
        subs[2], subs[3] = subs[3], subs[2]
        violations = validate_analysis_plan(replace(_baseline_plan(), sub_plans=tuple(subs)))
        self.assertTrue(any("current_by_dimension" in v for v in violations), violations)

    def test_non_plan_element_flagged(self) -> None:
        """sub_plans 元素必须全为 Plan 实例（运行时断言，类型标注不设防）。"""
        subs = list(_baseline_plan().sub_plans)
        subs[1] = "not-a-plan"  # type: ignore[assignment]
        violations = validate_analysis_plan(replace(_baseline_plan(), sub_plans=tuple(subs)))
        self.assertTrue(any("Plan 实例" in v for v in violations), violations)

    def test_total_must_not_group_or_sort(self) -> None:
        bad_total = replace(
            _baseline_plan().sub_plans[0],
            dimensions=(DIMENSION,),
            limit=5,
            order_by=(OrderSpec(DIMENSION),),
        )
        subs = (bad_total,) + _baseline_plan().sub_plans[1:]
        violations = validate_analysis_plan(replace(_baseline_plan(), sub_plans=subs))
        self.assertTrue(any("baseline_total" in v for v in violations), violations)

    def test_grouped_order_descending_flagged(self) -> None:
        bad_grouped = replace(
            _baseline_plan().sub_plans[2],
            order_by=(OrderSpec(DIMENSION, True),),
        )
        subs = list(_baseline_plan().sub_plans)
        subs[2] = bad_grouped
        violations = validate_analysis_plan(replace(_baseline_plan(), sub_plans=tuple(subs)))
        self.assertTrue(any("升序" in v for v in violations), violations)

    def test_time_role_binding_flagged(self) -> None:
        """baseline_total 带当期时间 = 角色绑定错误。"""
        bad_total = replace(_baseline_plan().sub_plans[0], time=CURRENT)
        subs = (bad_total,) + _baseline_plan().sub_plans[1:]
        violations = validate_analysis_plan(replace(_baseline_plan(), sub_plans=subs))
        self.assertTrue(any("time" in v for v in violations), violations)

    def test_filters_must_match_plan(self) -> None:
        """四步共享同一 WHERE（两期过滤完全相同，ADR-0026 决策①）。"""
        subs = (
            replace(_baseline_plan().sub_plans[0], filters=(Filter("Branch", "=", "A"),)),
        ) + _baseline_plan().sub_plans[1:]
        violations = validate_analysis_plan(replace(_baseline_plan(), sub_plans=subs))
        self.assertTrue(any("filters" in v for v in violations), violations)

    def test_comparison_must_be_none(self) -> None:
        """分析比较用普通 Plan，不依赖 yoy/pop 窗口（ADR-0026 决策①）。"""
        subs = (
            replace(_baseline_plan().sub_plans[0], comparison=ComparisonSpec("yoy")),
        ) + _baseline_plan().sub_plans[1:]
        violations = validate_analysis_plan(replace(_baseline_plan(), sub_plans=subs))
        self.assertTrue(any("comparison" in v for v in violations), violations)

    def test_direction_closed_set(self) -> None:
        plan = replace(_baseline_plan(), direction="sideways")  # type: ignore[arg-type]
        violations = validate_analysis_plan(plan)
        self.assertTrue(any("direction" in v for v in violations), violations)

    def test_granularity_must_match(self) -> None:
        plan = replace(_baseline_plan(), current=TimeSpec("month", 201310))
        violations = validate_analysis_plan(plan)
        self.assertTrue(any("粒度" in v for v in violations), violations)


# ---------------------------------------------------------------------------
# 4) AnalysisResult 闭合矩阵与原因码优先关系
# ---------------------------------------------------------------------------


class TestClosureMatrix(unittest.TestCase):
    def test_ok_result_valid(self) -> None:
        self.assertEqual(analysis_status(_ok_result()), "ok")
        self.assertEqual(validate_analysis_result(_ok_result()), ())

    def test_unavailable_result_valid(self) -> None:
        self.assertEqual(analysis_status(_unavailable_result()), "unavailable")
        self.assertEqual(validate_analysis_result(_unavailable_result()), ())

    def test_clarify_result_valid(self) -> None:
        self.assertEqual(analysis_status(_clarify_result()), "clarify")
        self.assertEqual(validate_analysis_result(_clarify_result()), ())

    def test_blocked_result_valid(self) -> None:
        self.assertEqual(analysis_status(_blocked_result()), "blocked")
        self.assertEqual(validate_analysis_result(_blocked_result()), ())

    def test_blocked_without_prefix_valid(self) -> None:
        """第一步即被拒：无需伪造任何 answer 前缀步骤。"""
        self.assertEqual(validate_analysis_result(_blocked_result(with_prefix=False)), ())

    def test_error_result_valid(self) -> None:
        self.assertEqual(analysis_status(_error_result()), "error")
        self.assertEqual(validate_analysis_result(_error_result()), ())

    def test_answer_without_attribution_invalid(self) -> None:
        bad = replace(_ok_result(), attribution=None)
        self.assertTrue(validate_analysis_result(bad))
        with self.assertRaises(ValueError):
            analysis_status(bad)

    def test_answer_step_count_enforced(self) -> None:
        bad = replace(_ok_result(), steps=_ok_result().steps[:3])
        self.assertTrue(any("4" in v for v in validate_analysis_result(bad)))

    def test_unavailable_with_items_invalid(self) -> None:
        """unavailable 不产生贡献项或结论（ADR-0026 决策⑤）。"""
        bad = replace(_ok_result(), attribution=replace(_ok_attribution(), status="unavailable"))
        violations = validate_analysis_result(bad)
        self.assertTrue(any("unavailable" in v for v in violations), violations)

    def test_unavailable_requires_reason(self) -> None:
        bad = replace(_unavailable_result(), reason_code=None)
        bad = replace(bad, attribution=replace(bad.attribution, reason_code=None))  # type: ignore[union-attr]
        violations = validate_analysis_result(bad)
        self.assertTrue(any("原因码" in v for v in violations), violations)

    def test_ok_items_must_be_complete(self) -> None:
        incomplete = replace(_ok_attribution().items[0], contribution_pct=None)
        attribution = replace(_ok_attribution(), items=(incomplete, _ok_attribution().items[1]))
        bad = replace(_ok_result(), attribution=attribution)
        violations = validate_analysis_result(bad)
        self.assertTrue(any("items[0]" in v for v in violations), violations)

    def test_clarify_with_plan_invalid(self) -> None:
        bad = replace(_clarify_result(), plan=_baseline_plan())
        self.assertTrue(validate_analysis_result(bad))

    def test_clarify_with_steps_invalid(self) -> None:
        """clarify 零 SQL 调用：不得携带已执行步骤。"""
        bad = replace(_clarify_result(), steps=(_answer_step(),))
        self.assertTrue(validate_analysis_result(bad))

    def test_clarify_with_attribution_invalid(self) -> None:
        bad = replace(_clarify_result(), attribution=_ok_attribution())
        self.assertTrue(validate_analysis_result(bad))

    def test_clarify_requires_clarification_payload(self) -> None:
        bad = replace(_clarify_result(), turn=_parent_turn("clarify"))
        violations = validate_analysis_result(bad)
        self.assertTrue(any("clarification" in v for v in violations), violations)

    def test_blocked_with_attribution_invalid(self) -> None:
        """blocked/error 无 totals/items/text：attribution 必须为 None。"""
        bad = replace(_blocked_result(), attribution=_ok_attribution())
        self.assertTrue(validate_analysis_result(bad))

    def test_blocked_wrong_reason_invalid(self) -> None:
        bad = replace(_blocked_result(), reason_code="execution_error")
        violations = validate_analysis_result(bad)
        self.assertTrue(any("guard_blocked" in v for v in violations), violations)

    def test_error_wrong_reason_invalid(self) -> None:
        bad = replace(_error_result(), reason_code="guard_blocked")
        violations = validate_analysis_result(bad)
        self.assertTrue(any("execution_error" in v for v in violations), violations)

    def test_steps_must_end_with_terminal_kind(self) -> None:
        """steps 最后一步必须是失败步（blocked 前缀只允许 answer）。"""
        bad = replace(_blocked_result(), steps=(_answer_step(), _answer_step("x")))
        violations = validate_analysis_result(bad)
        self.assertTrue(violations)

    def test_handoff_not_a_terminal(self) -> None:
        """意外 handoff 属内部契约错误（ADR-0026 决策③），闭合矩阵拒绝。"""
        bad = AnalysisResult(
            turn=_parent_turn("handoff", handoff_reason="x"),
            plan=None,
            steps=(),
            attribution=None,
            reason_code=None,
            elapsed_ms=0.0,
            snapshot_sha=None,
            semantic_sha256=None,
        )
        self.assertTrue(validate_analysis_result(bad))
        with self.assertRaises(ValueError):
            analysis_status(bad)

    def test_reason_code_priority_turn_wins(self) -> None:
        """优先关系：turn 终态优先，attribution.reason_code 仅在 answer 下有意义。"""
        self.assertEqual(effective_reason_code(_blocked_result()), "guard_blocked")
        self.assertEqual(effective_reason_code(_error_result()), "execution_error")
        self.assertIsNone(effective_reason_code(_ok_result()))
        self.assertEqual(
            effective_reason_code(_unavailable_result()), "zero_total_delta"
        )
        # result.reason_code 与优先级推导不一致 → 违规
        bad = replace(_blocked_result(), reason_code=None)
        violations = validate_analysis_result(bad)
        self.assertTrue(violations)

    def test_parent_answer_turn_carries_no_single_sql(self) -> None:
        """分析父轮不冒充单 SQL 结果（ADR-0026 决策⑥）。"""
        bad_turn = _parent_turn("answer", metric=METRIC, sql="SELECT 1", rows=(("1",),))
        bad = replace(_ok_result(), turn=bad_turn)
        violations = validate_analysis_result(bad)
        self.assertTrue(any("父轮" in v for v in violations), violations)


# ---------------------------------------------------------------------------
# 5) schema 负例（fixture 走 TemporaryDirectory，不污染仓库）
# ---------------------------------------------------------------------------


def _expected_plan_block() -> dict:
    return {
        "intent": "attribution",
        "metric": METRIC,
        "dimension": DIMENSION,
        "baseline": {"granularity": "quarter", "value": "2013Q3"},
        "current": {"granularity": "quarter", "value": "2013Q4"},
        "direction": "change",
        "filters": [],
    }


def _sub_plans_block() -> list[dict]:
    return [
        {
            "role": "baseline_total",
            "metric": METRIC,
            "dimensions": [],
            "time": {"granularity": "quarter", "value": "2013Q3"},
            "filters": [],
            "order_by": [],
            "limit": 1,
            "comparison": None,
        },
        {
            "role": "current_total",
            "metric": METRIC,
            "dimensions": [],
            "time": {"granularity": "quarter", "value": "2013Q4"},
            "filters": [],
            "order_by": [],
            "limit": 1,
            "comparison": None,
        },
        {
            "role": "current_by_dimension",
            "metric": METRIC,
            "dimensions": [DIMENSION],
            "time": {"granularity": "quarter", "value": "2013Q4"},
            "filters": [],
            "order_by": [{"column": DIMENSION, "desc": False}],
            "limit": 10000,
            "comparison": None,
        },
        {
            "role": "baseline_by_dimension",
            "metric": METRIC,
            "dimensions": [DIMENSION],
            "time": {"granularity": "quarter", "value": "2013Q3"},
            "filters": [],
            "order_by": [{"column": DIMENSION, "desc": False}],
            "limit": 10000,
            "comparison": None,
        },
    ]


def _base_answer_sample() -> dict:
    return {
        "id": "attribution-001",
        "maturity": "draft",
        "question": "分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献",
        "expected_kind": "answer",
        "expected_sql_calls": 4,
        "snapshot_sha": PENDING,
        "semantic_sha256": PENDING,
        "expected_plan": _expected_plan_block(),
        "expected_sub_plans": _sub_plans_block(),
        "reference": {
            "sql": None,
            "results": None,
            "reviewed_by": None,
            "reviewed_at": None,
            "note": "draft：结构已定，参考 SQL/结果待 T09 独立计算后填入",
        },
        "tags": ["analysis", "draft"],
    }


def _base_clarify_sample() -> dict:
    return {
        "id": "clarify-001",
        "maturity": "draft",
        "question": "分析 2013Q4 相对上季度的佣金收入变化贡献",
        "expected_kind": "clarify",
        "expected_sql_calls": 0,
        "expected_clarification": {
            "reasons": (
                "期间含相对时间表达「上季度」，固定快照下会漂移，需绝对期间",
                "缺分组维度：按什么维度分解贡献",
            ),
            "kind": "relative_time",
        },
        "snapshot_sha": PENDING,
        "semantic_sha256": PENDING,
        "tags": ["analysis", "clarification", "draft"],
    }


def _base_blocked_sample() -> dict:
    sample = _base_answer_sample()
    sample["id"] = "blocked-001"
    sample["question"] = "分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献"
    sample["expected_kind"] = "blocked"
    sample["expected_sql_calls"] = 1
    sample["expected_reason_code"] = "guard_blocked"
    del sample["reference"]
    return sample


class TestAnalysisSchemaNegatives(unittest.TestCase):
    """schema 负例必须击穿（draft 形态约束 + additionalProperties:false + 判别字段）。"""

    def setUp(self) -> None:
        self.schema = json.loads(ANALYSIS_SCHEMA_PATH.read_text(encoding="utf-8"))

    def _assert_invalid(self, sample: dict) -> None:
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(sample, self.schema)

    def _assert_valid(self, sample: dict) -> None:
        jsonschema.validate(sample, self.schema)

    def test_draft_answer_baseline_valid(self) -> None:
        self._assert_valid(_base_answer_sample())

    def test_draft_clarify_baseline_valid(self) -> None:
        self._assert_valid(_base_clarify_sample())

    def test_draft_blocked_baseline_valid(self) -> None:
        """blocked/error 样本不要求伪造未执行步骤的结果（无 reference 通过）。"""
        self._assert_valid(_base_blocked_sample())

    def test_missing_current_period_fails(self) -> None:
        sample = _base_answer_sample()
        del sample["expected_plan"]["current"]
        self._assert_invalid(sample)

    def test_unknown_top_level_field_fails(self) -> None:
        sample = _base_answer_sample()
        sample["unexpected_field"] = 1
        self._assert_invalid(sample)

    def test_unknown_nested_field_fails(self) -> None:
        sample = _base_answer_sample()
        sample["expected_plan"]["extra"] = 1
        self._assert_invalid(sample)

    def test_duplicate_role_fails(self) -> None:
        sample = _base_answer_sample()
        sample["expected_sub_plans"][2]["role"] = "baseline_total"
        self._assert_invalid(sample)

    def test_wrong_step_order_fails(self) -> None:
        sample = _base_answer_sample()
        subs = sample["expected_sub_plans"]
        subs[1], subs[3] = subs[3], subs[1]
        self._assert_invalid(sample)

    def test_answer_without_reference_fails(self) -> None:
        sample = _base_answer_sample()
        del sample["reference"]
        self._assert_invalid(sample)

    def test_ready_without_results_fails(self) -> None:
        sample = _base_answer_sample()
        sample["maturity"] = "ready"
        self._assert_invalid(sample)

    def test_ready_with_placeholder_sha_fails(self) -> None:
        sample = _base_answer_sample()
        sample["maturity"] = "ready"
        sample["reference"] = {
            "sql": ["SELECT 1"] * 4,
            "results": [{"role": "baseline_total", "value": "100"}],
            "reviewed_by": "human",
            "reviewed_at": "2026-09-20T10:00:00+08:00",
        }
        sample["snapshot_sha"] = PENDING
        self._assert_invalid(sample)

    def test_clarify_without_zero_sql_calls_fails(self) -> None:
        sample = _base_clarify_sample()
        del sample["expected_sql_calls"]
        self._assert_invalid(sample)

    def test_clarify_with_nonzero_sql_calls_fails(self) -> None:
        sample = _base_clarify_sample()
        sample["expected_sql_calls"] = 1
        self._assert_invalid(sample)

    def test_clarify_with_plan_forbidden(self) -> None:
        """未解析时 plan 为 None：澄清样本不得声明 expected_plan。"""
        sample = _base_clarify_sample()
        sample["expected_plan"] = _expected_plan_block()
        self._assert_invalid(sample)

    def test_clarify_with_reference_forbidden(self) -> None:
        sample = _base_clarify_sample()
        sample["reference"] = {"sql": None}
        self._assert_invalid(sample)

    def test_answer_with_reason_code_forbidden(self) -> None:
        sample = _base_answer_sample()
        sample["expected_reason_code"] = "zero_total_delta"
        self._assert_invalid(sample)

    def test_unavailable_requires_synthesis_reason(self) -> None:
        sample = _base_answer_sample()
        sample["id"] = "unavailable-001"
        sample["expected_kind"] = "unavailable"
        self._assert_invalid(sample)
        sample["expected_reason_code"] = "guard_blocked"
        self._assert_invalid(sample)
        sample["expected_reason_code"] = "zero_total_delta"
        self._assert_valid(sample)


class TestSchemaPythonSync(unittest.TestCase):
    """schema 枚举与 Python 闭集常量必须同步（防两处漂移）。"""

    def setUp(self) -> None:
        self.schema = json.loads(ANALYSIS_SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_reason_code_enum_matches_constants(self) -> None:
        enum = self.schema["properties"]["expected_reason_code"]["enum"]
        self.assertEqual(len(enum), len(ANALYSIS_REASON_CODES) + 1)  # + null
        self.assertIsNone(next(v for v in enum if v is None))
        self.assertEqual(set(v for v in enum if v is not None), set(ANALYSIS_REASON_CODES))

    def test_unavailable_reason_enum_matches_constants(self) -> None:
        """unavailable 判别支路的 reason_code 枚举 = 综合原因码闭集（不混入 turn 终态码）。"""
        enums = [
            cond["then"]["properties"]["expected_reason_code"]["enum"]
            for cond in self.schema.get("allOf", [])
            if "expected_reason_code" in cond.get("then", {}).get("properties", {})
            and "enum" in cond["then"]["properties"]["expected_reason_code"]
        ]
        self.assertEqual(len(enums), 1)
        self.assertEqual(set(v for v in enums[0] if v is not None), set(UNAVAILABLE_REASON_CODES))
        self.assertNotIn("guard_blocked", enums[0])
        self.assertNotIn("execution_error", enums[0])

    def test_expected_kind_enum(self) -> None:
        self.assertEqual(
            self.schema["properties"]["expected_kind"]["enum"],
            ["answer", "clarify", "blocked", "error", "unavailable"],
        )

    def test_role_enum_matches_constants(self) -> None:
        roles = {
            self.schema["definitions"][d]["properties"]["role"]["const"]
            for d in self.schema["definitions"]
            if d.startswith("plan") and "role" in self.schema["definitions"][d]["properties"]
        }
        self.assertEqual(roles, set(ANALYSIS_ROLES))

    def test_status_constants(self) -> None:
        """终态/综合状态闭集（expected_kind=answer ↔ 终态 ok 的对应关系见
        analysis_status：turn.kind=answer + attribution.status ∈ ok/unavailable）。"""
        self.assertEqual(ANALYSIS_STATUSES, ("ok", "unavailable", "clarify", "blocked", "error"))
        self.assertEqual(ATTRIBUTION_STATUSES, ("ok", "unavailable"))


# ---------------------------------------------------------------------------
# 6) 仓库内 draft 样本双重校验
# ---------------------------------------------------------------------------


def _plan_from_sample(sample: dict) -> AnalysisPlan:
    """样本 JSON → AnalysisPlan（测试侧装配器；T09 评测器自行落位）。"""
    ep = sample["expected_plan"]

    def ts(d: dict) -> TimeSpec:
        return TimeSpec(d["granularity"], d["value"])

    def filters(fs: list[dict]) -> tuple[Filter, ...]:
        return tuple(Filter(f["column"], f["op"], f["value"]) for f in fs)

    subs = tuple(
        Plan(
            e["metric"],
            dimensions=tuple(e["dimensions"]),
            time=ts(e["time"]) if e["time"] else None,
            filters=filters(e["filters"]),
            order_by=tuple(OrderSpec(o["column"], o["desc"]) for o in e["order_by"]),
            limit=e["limit"],
            comparison=None if e["comparison"] is None else ComparisonSpec(e["comparison"]["kind"]),
        )
        for e in sample["expected_sub_plans"]
    )
    return AnalysisPlan(
        intent=ep["intent"],
        metric=ep["metric"],
        dimension=ep["dimension"],
        baseline=ts(ep["baseline"]),
        current=ts(ep["current"]),
        filters=filters(ep.get("filters", ())),
        direction=ep["direction"],
        sub_plans=subs,
    )


class TestRepoSamples(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = json.loads(ANALYSIS_SCHEMA_PATH.read_text(encoding="utf-8"))

    def _load(self, name: str) -> dict:
        return json.loads((ANALYSIS_DIR / "finance" / name).read_text(encoding="utf-8"))

    def test_attribution_001_valid_and_contract_consistent(self) -> None:
        sample = self._load("attribution-001.json")
        jsonschema.validate(sample, self.schema)
        self.assertEqual(sample["maturity"], "draft")
        self.assertEqual(sample["expected_kind"], "answer")
        self.assertEqual(sample["expected_sql_calls"], 4)
        self.assertEqual(sample["snapshot_sha"], PENDING)
        self.assertEqual(sample["semantic_sha256"], PENDING)
        plan = _plan_from_sample(sample)
        self.assertEqual(validate_analysis_plan(plan), ())
        # 角色顺序（ADR-0026 决策①固定顺序）与 T03 验收断言同形态
        self.assertEqual(
            [p.time and p.time.value for p in plan.sub_plans],
            ["2013Q3", "2013Q4", "2013Q4", "2013Q3"],
        )
        self.assertEqual(
            [p.dimensions for p in plan.sub_plans], [(), (), (DIMENSION,), (DIMENSION,)]
        )
        self.assertTrue(all(p.comparison is None for p in plan.sub_plans))

    def test_clarify_001_valid(self) -> None:
        sample = self._load("clarify-001.json")
        jsonschema.validate(sample, self.schema)
        self.assertEqual(sample["maturity"], "draft")
        self.assertEqual(sample["expected_kind"], "clarify")
        self.assertEqual(sample["expected_sql_calls"], 0)
        self.assertNotIn("expected_plan", sample)
        self.assertNotIn("expected_sub_plans", sample)
        reasons = sample["expected_clarification"]["reasons"]
        self.assertTrue(reasons)
        self.assertIn(sample["expected_clarification"]["kind"], ("ambiguous", "relative_time", "unmatched"))

    def test_sample_id_matches_filename(self) -> None:
        for name in ("attribution-001.json", "clarify-001.json"):
            sample = self._load(name)
            self.assertEqual(sample["id"], Path(name).stem)


# ---------------------------------------------------------------------------
# 7) lint 扩展：make lint 必须扫描 eval/analysis 目录
# ---------------------------------------------------------------------------


class TestLintAnalysisDir(unittest.TestCase):
    def test_real_repo_analysis_clean(self) -> None:
        """真实仓库 eval/analysis 零违规（含 schema.json 本身不被当样本扫描）。"""
        self.assertEqual(check_analysis_schema(), [])

    def test_lint_finds_illegal_file(self) -> None:
        """analysis 目录里的非法文件必须被 lint 发现，而不是只扫 gold-*.json。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "analysis"
            bad = base / "finance" / "bad-001.json"
            bad.parent.mkdir(parents=True)
            # 缺第二期 + 缺判别必填：必须击穿
            bad.write_text(
                json.dumps(
                    {
                        "id": "bad-001",
                        "maturity": "draft",
                        "question": "q",
                        "expected_kind": "answer",
                    }
                ),
                encoding="utf-8",
            )
            errors = check_analysis_schema(analysis_dir=base)
            self.assertEqual(len(errors), 1)
            self.assertIn("bad-001", errors[0])

    def test_lint_flags_id_filename_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "analysis"
            p = base / "finance" / "other-001.json"
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps(_base_clarify_sample()), encoding="utf-8")
            errors = check_analysis_schema(analysis_dir=base)
            self.assertEqual(len(errors), 1)
            self.assertIn("文件名", errors[0])


if __name__ == "__main__":
    unittest.main()
