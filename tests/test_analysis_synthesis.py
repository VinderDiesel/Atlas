"""ADR-0026 T04 synthesize 综合器测试（TDD：本文件先于实现编写并确认红灯）。

覆盖面（对应 task-4-brief 检查单 + ADR-0026 决策②/⑤ + 预评审更正裁定）：
1. 绑定基准（brief 原文）：baseline 总量 100、current 总量 80；A 组 60→30、B 组 40→50；
   断言 delta A=-30/B=+10，contribution_pct A=150.000000/B=-50.000000（6 位小数量化）；
2. 按列名定位指标/维度列（不依赖列位置）；总量步恰 1 行；列不存在拒绝；
3. 数值类型闭包（决策⑤ L201 + 预评审更正②）：度量单元格仅接受 int（不含 bool）
   与有限 Decimal；bool（True/False）、float（含有限值如 60.5）、字符串一律拒绝，
   不补零不跳过；列名（columns 中的 str）与维度值列（str/None）不受影响；
4. NULL 维度独立桶（与字符串"未知"不合并）；并集对齐；单期缺失组在完整性通过后按零处理；
5. 截断门禁：row_count 触及有效 LIMIT（== L 或 > L）→ possible_truncation——含
   "丢行正负抵消后两期对账都通过但行数 == L"反例（对账门禁不能替代截断门禁）；
6. 两期分别对账（分组和 == 各自总量）：含"仅核对 delta 会被正负抵消蒙混"的反例；
7. 精确算术：全局 Decimal context prec 改小不改变结果；量化 6 位 ROUND_HALF_EVEN
   （半位平局舍到偶数：保留位末位偶数舍去、奇数进位，预评审更正③）；大 Decimal（10**30 级）；
8. 覆盖矩阵：负值、净零总变化（unavailable + zero_total_delta，顶层保留已验证两期值、
   items 置空——预评审更正①）、零基线、方向不符；
9. 排序（预评审更正④ = 决策⑤ L211）：|delta| 降序，平局按带类型的规范化维度键升序
   （("null",) / ("str", value)，而非并集首现序）；
10. 固定措辞：ok/unavailable（含净零）文本逐字锁定；unavailable 文本无原因臆测、无百分数；
11. 步骤防御（预评审更正⑤ = 决策③）：步数≠4 或意外 clarify/handoff → raise ValueError
    （内部契约违约不容错）；blocked→guard_blocked、error→execution_error 保留；
    步 metric 与 plan 不符 → unavailable + column_mismatch（docstring 说明选码理由）。

所有数值均为合成测试输入（AGENTS.md N1：非实测业务数字，仅证纯函数行为）。
"""

from __future__ import annotations

import decimal
import unittest
from decimal import Decimal

from agent.analysis import (
    AnalysisDirection,
    AnalysisPlan,
    Attribution,
    AttributionItem,
    synthesize,
)
from agent.compiler import OrderSpec, Plan, TimeSpec
from agent.state import TurnKind, TurnResult

METRIC = "commission_revenue"
DIMENSION = "Branch"
BASELINE = TimeSpec("quarter", "2013Q3")
CURRENT = TimeSpec("quarter", "2013Q4")
TOTAL_COLUMNS = (METRIC,)
GROUP_COLUMNS = (DIMENSION, METRIC)


# ---------------------------------------------------------------------------
# 合成构造器（测试输入，非业务实测值）
# ---------------------------------------------------------------------------


def _plan(
    direction: AnalysisDirection = "change",
    group_limit: int = 10000,
    dimension: str | None = DIMENSION,
) -> AnalysisPlan:
    """符合固定四步模板的 AnalysisPlan（总量步 limit=1，分组步 limit=group_limit）。"""
    grouped = (dimension,) if dimension is not None else ()
    order = (OrderSpec(dimension),) if dimension is not None else ()
    return AnalysisPlan(
        intent="change_contribution",
        metric=METRIC,
        dimension=dimension,
        baseline=BASELINE,
        current=CURRENT,
        filters=(),
        direction=direction,
        sub_plans=(
            Plan(METRIC, time=BASELINE, limit=1),
            Plan(METRIC, time=CURRENT, limit=1),
            Plan(METRIC, dimensions=grouped, time=CURRENT, order_by=order, limit=group_limit),
            Plan(METRIC, dimensions=grouped, time=BASELINE, order_by=order, limit=group_limit),
        ),
    )


def _step(
    role: str,
    columns: tuple[str, ...],
    rows: tuple[tuple[object, ...], ...],
    *,
    kind: TurnKind = "answer",
    metric: str | None = METRIC,
) -> TurnResult:
    """合成 answer 子步骤（rows 为测试输入，非快照实测值）。"""
    return TurnResult(
        kind=kind,
        session_id="s-analysis-synthesis",
        question=f"step:{role}",
        metric=metric,
        sql=f"SELECT 1 -- {role}",
        columns=columns,
        rows=rows,
        row_count=len(rows),
    )


def _std_steps(
    baseline_total: Decimal,
    current_total: Decimal,
    current_rows: tuple[tuple[object, ...], ...],
    baseline_rows: tuple[tuple[object, ...], ...],
    *,
    total_columns: tuple[str, ...] = TOTAL_COLUMNS,
    group_columns: tuple[str, ...] = GROUP_COLUMNS,
    metric: str | None = METRIC,
) -> tuple[TurnResult, ...]:
    """按 ANALYSIS_ROLES 顺序构造四个 answer 步骤。"""
    return (
        _step("baseline_total", total_columns, ((baseline_total,),), metric=metric),
        _step("current_total", total_columns, ((current_total,),), metric=metric),
        _step("current_by_dimension", group_columns, current_rows, metric=metric),
        _step("baseline_by_dimension", group_columns, baseline_rows, metric=metric),
    )


def _binding_steps() -> tuple[TurnResult, ...]:
    """brief 绑定基准：总量 100→80；A 组 60→30、B 组 40→50（合成测试输入）。"""
    return _std_steps(
        Decimal("100"),
        Decimal("80"),
        (("A", Decimal("30")), ("B", Decimal("50"))),
        (("A", Decimal("60")), ("B", Decimal("40"))),
    )


def _by_value(attr: Attribution) -> dict[str, AttributionItem]:
    """brief 的测试侧辅助：items → value→item 映射（不是 Attribution 的新字段）。"""
    return {item.value: item for item in attr.items}


class _SynthesisTestCase(unittest.TestCase):
    def plan(self, **kwargs: object) -> AnalysisPlan:
        return _plan(**kwargs)  # type: ignore[arg-type]

    def assert_unavailable(self, attr: Attribution, reason: str) -> None:
        """unavailable 形状断言：空 items、空 totals、原因码与固定文本。"""
        self.assertEqual(attr.status, "unavailable")
        self.assertEqual(attr.reason_code, reason)
        self.assertEqual(attr.items, ())
        self.assertIsNone(attr.baseline)
        self.assertIsNone(attr.current)
        self.assertIsNone(attr.delta)
        self.assertNotIn("%", attr.text)
        self.assertTrue(attr.text.startswith(f"无法给出 {METRIC} 的变化贡献分解"), attr.text)


# ---------------------------------------------------------------------------
# 绑定基准与 happy path
# ---------------------------------------------------------------------------


class TestBindingBaseline(_SynthesisTestCase):
    def test_binding_deltas_and_contributions(self) -> None:
        """brief 绑定断言：delta A=-30/B=+10；pct A=150.000000/B=-50.000000。"""
        attr = synthesize(self.plan(), _binding_steps())
        self.assertEqual(attr.status, "ok")
        self.assertIsNone(attr.reason_code)
        self.assertEqual(attr.baseline, Decimal("100"))
        self.assertEqual(attr.current, Decimal("80"))
        self.assertEqual(attr.delta, Decimal("-20"))
        by_value = _by_value(attr)
        self.assertEqual(by_value["A"].delta, Decimal("-30"))
        self.assertEqual(by_value["A"].contribution_pct, Decimal("150.000000"))
        self.assertEqual(by_value["A"].baseline, Decimal("60"))
        self.assertEqual(by_value["A"].current, Decimal("30"))
        self.assertEqual(by_value["B"].delta, Decimal("10"))
        self.assertEqual(by_value["B"].contribution_pct, Decimal("-50.000000"))
        # items 按 |delta| 降序：A(|30|) 在 B(|10|) 前
        self.assertEqual([item.value for item in attr.items], ["A", "B"])

    def test_contribution_pct_quantized_to_six_places(self) -> None:
        """百分数量化到 6 位小数（exponent == -6），非仅数值相等。"""
        attr = synthesize(self.plan(), _binding_steps())
        for item in attr.items:
            assert item.contribution_pct is not None
            self.assertEqual(item.contribution_pct.as_tuple().exponent, -6)

    def test_column_location_by_name_not_position(self) -> None:
        """分组步列序颠倒（指标在前）仍按列名定位，结果与绑定基准一致。"""
        steps = (
            _step("baseline_total", TOTAL_COLUMNS, ((Decimal("100"),),)),
            _step("current_total", TOTAL_COLUMNS, ((Decimal("80"),),)),
            _step(
                "current_by_dimension",
                (METRIC, DIMENSION),
                ((Decimal("30"), "A"), (Decimal("50"), "B")),
            ),
            _step(
                "baseline_by_dimension",
                (METRIC, DIMENSION),
                ((Decimal("60"), "A"), (Decimal("40"), "B")),
            ),
        )
        attr = synthesize(self.plan(), steps)
        self.assertEqual(attr.status, "ok")
        by_value = _by_value(attr)
        self.assertEqual(by_value["A"].delta, Decimal("-30"))
        self.assertEqual(by_value["B"].contribution_pct, Decimal("-50.000000"))

    def test_group_missing_in_one_period_zero_filled(self) -> None:
        """完整性通过后，单期缺失的组按零处理（delta = 有值期 - 0）。"""
        steps = _std_steps(
            Decimal("100"),
            Decimal("30"),
            (("A", Decimal("30")),),
            (("A", Decimal("60")), ("B", Decimal("40"))),
        )
        attr = synthesize(self.plan(), steps)
        self.assertEqual(attr.status, "ok")
        self.assertEqual(attr.delta, Decimal("-70"))
        by_value = _by_value(attr)
        self.assertEqual(by_value["A"].delta, Decimal("-30"))
        self.assertEqual(by_value["A"].contribution_pct, Decimal("42.857143"))
        self.assertEqual(by_value["B"].baseline, Decimal("40"))
        self.assertEqual(by_value["B"].current, Decimal("0"))
        self.assertEqual(by_value["B"].delta, Decimal("-40"))
        self.assertEqual(by_value["B"].contribution_pct, Decimal("57.142857"))
        # |B delta|=40 > |A delta|=30 → B 在前
        self.assertEqual([item.value for item in attr.items], ["B", "A"])

    def test_null_dimension_independent_bucket(self) -> None:
        """NULL 维度是独立桶，不与字符串"未知"合并；两桶各自成项。"""
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("未知", Decimal("50")), (None, Decimal("30"))),
            ((None, Decimal("60")), ("未知", Decimal("40"))),
        )
        attr = synthesize(self.plan(), steps)
        self.assertEqual(attr.status, "ok")
        self.assertEqual(attr.delta, Decimal("-20"))
        by_value = _by_value(attr)
        self.assertEqual(len(attr.items), 2)
        self.assertEqual(by_value["(null)"].baseline, Decimal("60"))
        self.assertEqual(by_value["(null)"].current, Decimal("30"))
        self.assertEqual(by_value["(null)"].delta, Decimal("-30"))
        self.assertEqual(by_value["(null)"].contribution_pct, Decimal("150.000000"))
        self.assertEqual(by_value["未知"].delta, Decimal("10"))
        self.assertEqual(by_value["未知"].contribution_pct, Decimal("-50.000000"))
        self.assertEqual([item.value for item in attr.items], ["(null)", "未知"])

    def test_large_decimal_exact(self) -> None:
        """10**30 级大 Decimal：加减精确（int→Decimal 构造不经上下文）。"""
        steps = _std_steps(
            Decimal(2 * 10**30),
            Decimal(2 * 10**30 + 10**15),
            ((("A"), Decimal(10**30 + 10**15)), (("B"), Decimal(10**30))),
            ((("A"), Decimal(10**30)), (("B"), Decimal(10**30))),
        )
        attr = synthesize(self.plan(), steps)
        self.assertEqual(attr.status, "ok")
        self.assertEqual(attr.delta, Decimal(10**15))
        by_value = _by_value(attr)
        self.assertEqual(by_value["A"].delta, Decimal(10**15))
        self.assertEqual(by_value["A"].contribution_pct, Decimal("100.000000"))
        self.assertEqual(by_value["B"].delta, Decimal("0"))
        self.assertEqual(by_value["B"].contribution_pct, Decimal("0.000000"))

    def test_negative_values_preserved(self) -> None:
        """负度量与零 delta 项原样保留，不裁剪不重归一。"""
        steps = _std_steps(
            Decimal("10"),
            Decimal("0"),
            (("A", Decimal("-30")), ("B", Decimal("30"))),
            (("A", Decimal("-20")), ("B", Decimal("30"))),
        )
        attr = synthesize(self.plan(), steps)
        self.assertEqual(attr.status, "ok")
        self.assertEqual(attr.delta, Decimal("-10"))
        by_value = _by_value(attr)
        self.assertEqual(by_value["A"].delta, Decimal("-10"))
        self.assertEqual(by_value["A"].contribution_pct, Decimal("100.000000"))
        self.assertEqual(by_value["B"].delta, Decimal("0"))
        self.assertEqual(by_value["B"].contribution_pct, Decimal("0.000000"))


# ---------------------------------------------------------------------------
# 净零总变化（预评审更正① = ADR-0026 决策⑤ L207：unavailable + zero_total_delta，
# 顶层保留已验证 baseline/current/delta，items 置空；与 T01 闭合矩阵调和——
# ok 的 items 字段必须完整，unavailable 的 items 必须为空）
# ---------------------------------------------------------------------------


class TestNetZero(_SynthesisTestCase):
    def _net_zero_steps(self) -> tuple[TurnResult, ...]:
        return _std_steps(
            Decimal("100"),
            Decimal("100"),
            (("A", Decimal("50")), ("B", Decimal("50"))),
            (("A", Decimal("60")), ("B", Decimal("40"))),
        )

    def test_net_zero_unavailable_keeps_verified_totals(self) -> None:
        attr = synthesize(self.plan(), self._net_zero_steps())
        self.assertEqual(attr.status, "unavailable")
        self.assertEqual(attr.reason_code, "zero_total_delta")
        self.assertEqual(attr.items, ())
        # 顶层保留已验证两期值与净零 delta（决策⑤"保留已验证 delta"）
        self.assertEqual(attr.baseline, Decimal("100"))
        self.assertEqual(attr.current, Decimal("100"))
        self.assertEqual(attr.delta, Decimal("0"))
        self.assertIn("净零", attr.text)
        self.assertNotIn("%", attr.text)

    def test_net_zero_precedes_direction_expectation(self) -> None:
        """delta=0 时方向门放行（0 既非上升也非下降），净零门给出 zero_total_delta。"""
        attr = synthesize(self.plan(direction="decrease"), self._net_zero_steps())
        self.assertEqual(attr.status, "unavailable")
        self.assertEqual(attr.reason_code, "zero_total_delta")

    def test_net_zero_text_has_no_speculation(self) -> None:
        """净零 unavailable 文本：固定说明净零变化，不含百分数与原因臆测。"""
        attr = synthesize(self.plan(), self._net_zero_steps())
        self.assertNotIn("%", attr.text)
        self.assertNotIn("因为", attr.text)
        self.assertNotIn("可能因为", attr.text)


# ---------------------------------------------------------------------------
# 平局排序（预评审更正④ = ADR-0026 决策⑤ L211：|delta| 降序，同值按带类型的
# 规范化维度键升序，而非并集首现序）
# ---------------------------------------------------------------------------


class TestTypedKeyOrdering(_SynthesisTestCase):
    def test_ties_ordered_by_string_key_not_first_appearance(self) -> None:
        """两个 str 同 |delta|：按 ("str", value) 升序，即使并集首现序相反。"""
        steps = _std_steps(
            Decimal("50"),
            Decimal("70"),
            (("B", Decimal("40")), ("A", Decimal("30"))),
            (("A", Decimal("20")), ("B", Decimal("30"))),
        )
        attr = synthesize(self.plan(), steps)
        self.assertEqual(attr.status, "ok")
        by_value = _by_value(attr)
        self.assertEqual(by_value["A"].delta, Decimal("10"))
        self.assertEqual(by_value["B"].delta, Decimal("10"))
        # 并集首现序是 (B, A)；类型化键 ("str","A") < ("str","B") → A 在前
        self.assertEqual([item.value for item in attr.items], ["A", "B"])

    def test_ties_null_bucket_before_any_string_key(self) -> None:
        """NULL 桶 (("null",)) 与任意 str 键同 |delta|：类型标签保证 NULL 恒在前。"""
        steps = _std_steps(
            Decimal("50"),
            Decimal("70"),
            (("未知", Decimal("40")), (None, Decimal("30"))),
            ((None, Decimal("20")), ("未知", Decimal("30"))),
        )
        attr = synthesize(self.plan(), steps)
        self.assertEqual(attr.status, "ok")
        by_value = _by_value(attr)
        self.assertEqual(by_value["(null)"].delta, Decimal("10"))
        self.assertEqual(by_value["未知"].delta, Decimal("10"))
        # 并集首现序是 (未知, (null))；类型化键 ("null",) < ("str","未知") → 反转
        self.assertEqual([item.value for item in attr.items], ["(null)", "未知"])

    def test_primary_order_still_abs_delta_descending(self) -> None:
        """平局键只影响同 |delta| 的项：不同 |delta| 仍按绝对值降序（绑定基准）。"""
        attr = synthesize(self.plan(), _binding_steps())
        self.assertEqual(attr.status, "ok")
        self.assertEqual([item.value for item in attr.items], ["A", "B"])


# ---------------------------------------------------------------------------
# 方向门禁（direction_mismatch；direction="change" 永不不符）
# ---------------------------------------------------------------------------


class TestDirectionGate(_SynthesisTestCase):
    def test_direction_increase_with_negative_delta_rejected(self) -> None:
        attr = synthesize(self.plan(direction="increase"), _binding_steps())
        self.assert_unavailable(attr, "direction_mismatch")

    def test_direction_decrease_with_positive_delta_rejected(self) -> None:
        steps = _std_steps(
            Decimal("80"),
            Decimal("100"),
            (("A", Decimal("60")), ("B", Decimal("40"))),
            (("A", Decimal("30")), ("B", Decimal("50"))),
        )
        attr = synthesize(self.plan(direction="decrease"), steps)
        self.assert_unavailable(attr, "direction_mismatch")

    def test_direction_change_accepts_any_sign(self) -> None:
        attr = synthesize(self.plan(direction="change"), _binding_steps())
        self.assertEqual(attr.status, "ok")

    def test_direction_matching_sign_ok(self) -> None:
        attr = synthesize(self.plan(direction="decrease"), _binding_steps())
        self.assertEqual(attr.status, "ok")
        self.assertEqual(attr.delta, Decimal("-20"))


# ---------------------------------------------------------------------------
# 列定位门禁（column_mismatch）
# ---------------------------------------------------------------------------


class TestColumnGate(_SynthesisTestCase):
    def test_missing_metric_column_in_total_step(self) -> None:
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", Decimal("30")),),
            (("A", Decimal("60")),),
            total_columns=("wrong_total",),
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "column_mismatch")

    def test_missing_dimension_column_in_grouped_step(self) -> None:
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", Decimal("30")),),
            (("A", Decimal("60")),),
            group_columns=("Region", METRIC),
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "column_mismatch")

    def test_missing_metric_column_in_grouped_step(self) -> None:
        """行宽与列数一致（仅缺指标列），确保只有列名定位门能拦截。"""
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A",),),
            (("A",),),
            group_columns=(DIMENSION,),
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "column_mismatch")

    def test_dimension_none_plan_rejected(self) -> None:
        """plan.dimension 为 None（无效计划）时防御性拒绝，不抛异常。"""
        steps = _binding_steps()
        attr = synthesize(self.plan(dimension=None), steps)
        self.assert_unavailable(attr, "column_mismatch")


# ---------------------------------------------------------------------------
# 总量步形状门禁（empty_result / multi_row_total）
# ---------------------------------------------------------------------------


class TestTotalShapeGate(_SynthesisTestCase):
    def test_empty_baseline_total_rows(self) -> None:
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", Decimal("30")),),
            (("A", Decimal("60")),),
        )
        steps = (
            _step("baseline_total", TOTAL_COLUMNS, ()),
            steps[1],
            steps[2],
            steps[3],
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "empty_result")

    def test_empty_current_total_rows(self) -> None:
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", Decimal("30")),),
            (("A", Decimal("60")),),
        )
        steps = (steps[0], _step("current_total", TOTAL_COLUMNS, ()), steps[2], steps[3])
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "empty_result")

    def test_multi_row_baseline_total(self) -> None:
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", Decimal("30")),),
            (("A", Decimal("60")),),
        )
        steps = (
            _step("baseline_total", TOTAL_COLUMNS, ((Decimal("100"),), (Decimal("99"),))),
            steps[1],
            steps[2],
            steps[3],
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "multi_row_total")

    def test_multi_row_current_total(self) -> None:
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", Decimal("30")),),
            (("A", Decimal("60")),),
        )
        steps = (
            steps[0],
            _step("current_total", TOTAL_COLUMNS, ((Decimal("80"),), (Decimal("1"),))),
            steps[2],
            steps[3],
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "multi_row_total")


# ---------------------------------------------------------------------------
# 数值门禁（null_metric_value / non_finite_value；不补零不跳过）
# ---------------------------------------------------------------------------


class TestValueGate(_SynthesisTestCase):
    def test_null_metric_in_baseline_total(self) -> None:
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", Decimal("30")),),
            (("A", Decimal("60")),),
        )
        steps = (_step("baseline_total", TOTAL_COLUMNS, ((None,),)), steps[1], steps[2], steps[3])
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "null_metric_value")

    def test_null_metric_in_current_total(self) -> None:
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", Decimal("30")),),
            (("A", Decimal("60")),),
        )
        steps = (steps[0], _step("current_total", TOTAL_COLUMNS, ((None,),)), steps[2], steps[3])
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "null_metric_value")

    def test_null_metric_in_grouped_row(self) -> None:
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", None), ("B", Decimal("50"))),
            (("A", Decimal("60")), ("B", Decimal("40"))),
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "null_metric_value")

    def test_nan_float_in_total_step(self) -> None:
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", Decimal("30")),),
            (("A", Decimal("60")),),
        )
        steps = (
            steps[0],
            _step("current_total", TOTAL_COLUMNS, ((float("nan"),),)),
            steps[2],
            steps[3],
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "non_finite_value")

    def test_inf_float_in_grouped_row(self) -> None:
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", float("inf")),),
            (("A", Decimal("60")),),
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "non_finite_value")

    def test_finite_float_rejected_in_total_step(self) -> None:
        """预评审更正②（决策⑤ L201 排除 float）：有限 float（60.5）也必须拒绝。"""
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", Decimal("30")),),
            (("A", Decimal("60")),),
        )
        steps = (
            steps[0],
            _step("current_total", TOTAL_COLUMNS, ((60.5,),)),
            steps[2],
            steps[3],
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "non_finite_value")

    def test_finite_float_rejected_in_grouped_row(self) -> None:
        """有限 float 在分组行度量列同样拒绝（拒绝面覆盖两步型）。"""
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", 30.25),),
            (("A", Decimal("60")),),
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "non_finite_value")

    def test_decimal_nan_in_total_step(self) -> None:
        steps = _std_steps(
            Decimal("NaN"),
            Decimal("80"),
            (("A", Decimal("30")),),
            (("A", Decimal("60")),),
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "non_finite_value")

    def test_undeclared_types_rejected(self) -> None:
        """决策⑤ L201 + 预评审更正②：bool（True/False）/字符串一律不接受
        （non_finite_value 拒绝）；列名（columns 中的 str）不在此列、维度值列
        允许 str/None（其他用例覆盖），此处只锁定 rows 数值列的类型闭包。"""
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", Decimal("30")),),
            (("A", Decimal("60")),),
        )
        # bool True / False 出现在数值列
        for flag in (True, False):
            steps_bool = (
                _step("baseline_total", TOTAL_COLUMNS, ((flag,),)),
                steps[1],
                steps[2],
                steps[3],
            )
            attr = synthesize(self.plan(), steps_bool)
            self.assert_unavailable(attr, "non_finite_value")
        # 字符串数值列（rows 中的 str 属未声明类型，区别于 columns 的列名 str）
        steps_str = (
            _step("baseline_total", TOTAL_COLUMNS, (("80",),)),
            steps[1],
            steps[2],
            steps[3],
        )
        attr2 = synthesize(self.plan(), steps_str)
        self.assert_unavailable(attr2, "non_finite_value")
        # 分组行度量列中的 bool
        steps_group = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", True),),
            (("A", Decimal("60")),),
        )
        attr3 = synthesize(self.plan(), steps_group)
        self.assert_unavailable(attr3, "non_finite_value")

    def test_int_metric_values_accepted(self) -> None:
        """正例控制：度量单元格为 int（bool 除外）时接受（决策⑤类型闭包的 int 分支）。"""
        steps = (
            _step("baseline_total", TOTAL_COLUMNS, ((100,),)),
            _step("current_total", TOTAL_COLUMNS, ((80,),)),
            _step("current_by_dimension", GROUP_COLUMNS, (("A", 30), ("B", 50))),
            _step("baseline_by_dimension", GROUP_COLUMNS, (("A", 60), ("B", 40))),
        )
        attr = synthesize(self.plan(), steps)
        self.assertEqual(attr.status, "ok")
        self.assertEqual(attr.delta, Decimal("-20"))
        self.assertEqual(_by_value(attr)["A"].delta, Decimal("-30"))


# ---------------------------------------------------------------------------
# 重复维度键门禁（duplicate_dimension_key，同期内）
# ---------------------------------------------------------------------------


class TestDuplicateKeyGate(_SynthesisTestCase):
    def test_duplicate_key_in_current_period(self) -> None:
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", Decimal("30")), ("A", Decimal("20"))),
            (("A", Decimal("60")), ("B", Decimal("40"))),
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "duplicate_dimension_key")

    def test_duplicate_key_in_baseline_period(self) -> None:
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", Decimal("30")), ("B", Decimal("50"))),
            (("A", Decimal("60")), ("A", Decimal("40"))),
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "duplicate_dimension_key")


# ---------------------------------------------------------------------------
# 截断门禁（possible_truncation；对账门禁不能替代截断门禁）
# ---------------------------------------------------------------------------


class TestTruncationGate(_SynthesisTestCase):
    def test_row_count_equal_to_limit_rejected_despite_reconciliation(self) -> None:
        """反例：丢行正负抵消（A -30 / B +30）后两期对账都通过、行数 == L —— 仍须拒绝。

        若删除截断门禁，该 fixture 会经对账与净零路径产出 ok，测试即红。
        """
        steps = _std_steps(
            Decimal("100"),
            Decimal("100"),
            (("A", Decimal("30")), ("B", Decimal("70"))),
            (("A", Decimal("60")), ("B", Decimal("40"))),
        )
        attr = synthesize(self.plan(group_limit=2), steps)
        self.assert_unavailable(attr, "possible_truncation")

    def test_row_count_above_limit_rejected(self) -> None:
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", Decimal("30")), ("B", Decimal("50"))),
            (("A", Decimal("60")), ("B", Decimal("40"))),
        )
        steps = (
            steps[0],
            steps[1],
            _step(
                "current_by_dimension",
                GROUP_COLUMNS,
                (("A", Decimal("30")), ("B", Decimal("50")), ("C", Decimal("0"))),
            ),
            steps[3],
        )
        attr = synthesize(self.plan(group_limit=2), steps)
        self.assert_unavailable(attr, "possible_truncation")

    def test_row_count_below_limit_not_truncated(self) -> None:
        """对照：行数 < L 时不触发截断门禁（绑定数据正常综合）。"""
        attr = synthesize(self.plan(group_limit=3), _binding_steps())
        self.assertEqual(attr.status, "ok")


# ---------------------------------------------------------------------------
# 对账门禁（reconciliation_mismatch；两期分别核对，不能只核对 delta）
# ---------------------------------------------------------------------------


class TestReconciliationGate(_SynthesisTestCase):
    def test_current_period_group_sum_mismatch(self) -> None:
        steps = _std_steps(
            Decimal("100"),
            Decimal("90"),
            (("A", Decimal("30")), ("B", Decimal("50"))),  # 和 80 ≠ 总量 90
            (("A", Decimal("60")), ("B", Decimal("40"))),
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "reconciliation_mismatch")

    def test_baseline_period_group_sum_mismatch(self) -> None:
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", Decimal("30")), ("B", Decimal("50"))),
            (("A", Decimal("60")), ("B", Decimal("30"))),  # 和 90 ≠ 总量 100
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "reconciliation_mismatch")

    def test_offsetting_omission_fools_delta_only_check(self) -> None:
        """反例：两期分组和各多 10（正负抵消后 delta 恰好等于总 delta）——仍须拒绝。

        若只核对 delta（组 delta 之和 == 总 delta），该 fixture 会蒙混通过；
        两期分别对账必须拦截（baseline 110≠100、current 90≠80）。
        """
        steps = _std_steps(
            Decimal("100"),
            Decimal("80"),
            (("A", Decimal("40")), ("B", Decimal("50"))),  # 和 90 ≠ 80
            (("A", Decimal("70")), ("B", Decimal("40"))),  # 和 110 ≠ 100
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "reconciliation_mismatch")


# ---------------------------------------------------------------------------
# 精确算术：上下文独立 + ROUND_HALF_EVEN 量化 6 位（预评审更正③）
# ---------------------------------------------------------------------------


class TestExactArithmetic(_SynthesisTestCase):
    def test_independent_of_global_decimal_context(self) -> None:
        """全局 prec=5 下大数差值会被吞掉；synthesize 用显式 localcontext，结果不变。"""
        steps = _std_steps(
            Decimal(10**30 + 100),
            Decimal(10**30 + 80),
            ((("A"), Decimal(10**30 + 30)), (("B"), Decimal(50))),
            ((("A"), Decimal(10**30 + 60)), (("B"), Decimal(40))),
        )
        old_prec = decimal.getcontext().prec
        decimal.getcontext().prec = 5
        try:
            attr = synthesize(self.plan(), steps)
        finally:
            decimal.getcontext().prec = old_prec
        self.assertEqual(attr.status, "ok")
        self.assertEqual(attr.delta, Decimal("-20"))
        by_value = _by_value(attr)
        self.assertEqual(by_value["A"].delta, Decimal("-30"))
        self.assertEqual(by_value["A"].contribution_pct, Decimal("150.000000"))
        self.assertEqual(by_value["B"].contribution_pct, Decimal("-50.000000"))

    def test_rounding_half_even_tie_at_even_digit_rounds_down(self) -> None:
        """半位平局（x.xxxxxx5）且保留位末位为偶数：HALF_EVEN 舍去（HALF_UP 会进位）。

        pct_A 精确值 = 100 * 10000005 / 10**9 = 1.0000005 → 1.000000（末位 0 为偶数）；
        若实现误用 ROUND_HALF_UP 会得 1.000001，本测试即红。
        """
        steps = _std_steps(
            Decimal(10000005),
            Decimal(1010000005),
            ((("A"), Decimal(20000010)), (("B"), Decimal(989999995))),
            ((("A"), Decimal(10000005)), (("B"), Decimal(0))),
        )
        attr = synthesize(self.plan(), steps)
        self.assertEqual(attr.status, "ok")
        self.assertEqual(attr.delta, Decimal(10**9))
        by_value = _by_value(attr)
        pct_a = by_value["A"].contribution_pct
        assert pct_a is not None
        self.assertEqual(pct_a, Decimal("1.000000"))
        self.assertEqual(pct_a.as_tuple().exponent, -6)
        # B：98.9999995 → 保留位末位 9 为奇数 → HALF_EVEN 进位 → 99.000000
        self.assertEqual(by_value["B"].contribution_pct, Decimal("99.000000"))

    def test_rounding_half_even_tie_at_odd_digit_rounds_up(self) -> None:
        """半位平局且保留位末位为奇数：HALF_EVEN 进位到偶数（HALF_UP 同值非判别，作对照）。

        总 delta = 2*10**9；X: 100*20000010/2e9 = 1.0000005（末位 0 偶 → 1.000000）；
        Y: 100*60000030/2e9 = 3.0000015（末位 1 奇 → 3.000002）；
        Z: 100*1919999960/2e9 = 95.999998（精确值，无平局）。
        """
        steps = _std_steps(
            Decimal(0),
            Decimal(2000000000),
            (
                (("X"), Decimal(20000010)),
                (("Y"), Decimal(60000030)),
                (("Z"), Decimal(1919999960)),
            ),
            ((("X"), Decimal(0)), (("Y"), Decimal(0)), (("Z"), Decimal(0))),
        )
        attr = synthesize(self.plan(), steps)
        self.assertEqual(attr.status, "ok")
        self.assertEqual(attr.delta, Decimal(2000000000))
        by_value = _by_value(attr)
        self.assertEqual(by_value["X"].contribution_pct, Decimal("1.000000"))
        self.assertEqual(by_value["Y"].contribution_pct, Decimal("3.000002"))
        self.assertEqual(by_value["Z"].contribution_pct, Decimal("95.999998"))

    def test_rounding_non_tie_repeating_decimal(self) -> None:
        """非平局循环小数：100/3 → 33.333333、200/3 → 66.666667（6 位截断）。"""
        steps = _std_steps(
            Decimal("3"),
            Decimal("6"),
            (("A", Decimal("2")), ("B", Decimal("4"))),
            (("A", Decimal("1")), ("B", Decimal("2"))),
        )
        attr = synthesize(self.plan(), steps)
        self.assertEqual(attr.status, "ok")
        by_value = _by_value(attr)
        self.assertEqual(by_value["A"].contribution_pct, Decimal("33.333333"))
        self.assertEqual(by_value["B"].contribution_pct, Decimal("66.666667"))


# ---------------------------------------------------------------------------
# 覆盖矩阵补遗：零基线
# ---------------------------------------------------------------------------


class TestZeroBaseline(_SynthesisTestCase):
    def test_zero_baseline_computes_normal_contributions(self) -> None:
        """baseline 总量 = 0 且分组和为 0：总 delta ≠ 0，百分数正常计算。"""
        steps = _std_steps(
            Decimal("0"),
            Decimal("50"),
            (("A", Decimal("30")), ("B", Decimal("20"))),
            (("A", Decimal("0")),),
        )
        attr = synthesize(self.plan(), steps)
        self.assertEqual(attr.status, "ok")
        self.assertEqual(attr.baseline, Decimal("0"))
        self.assertEqual(attr.delta, Decimal("50"))
        by_value = _by_value(attr)
        self.assertEqual(by_value["A"].delta, Decimal("30"))
        self.assertEqual(by_value["A"].contribution_pct, Decimal("60.000000"))
        self.assertEqual(by_value["B"].baseline, Decimal("0"))
        self.assertEqual(by_value["B"].delta, Decimal("20"))
        self.assertEqual(by_value["B"].contribution_pct, Decimal("40.000000"))


# ---------------------------------------------------------------------------
# 步骤防御（预评审更正⑤ = 决策③：编排违约不容错，防御转为契约断言）
# ---------------------------------------------------------------------------


class TestStepDefense(_SynthesisTestCase):
    def test_wrong_step_count_raises_value_error(self) -> None:
        """steps 长度≠4 = 编排违约：不再防御性返回 unavailable，而是 raise ValueError。"""
        with self.assertRaises(ValueError):
            synthesize(self.plan(), _binding_steps()[:3])

    def test_plan_with_wrong_sub_plans_count_raises_value_error(self) -> None:
        """模板本身违约（sub_plans 数≠4，T01 validate_analysis_plan 已挡）同样 raise。

        截断门禁需按四步模板索引 sub_plans[2]/[3]，畸形模板属于内部契约错误。
        """
        bad_plan = AnalysisPlan(
            intent="change_contribution",
            metric=METRIC,
            dimension=DIMENSION,
            baseline=BASELINE,
            current=CURRENT,
            filters=(),
            direction="change",
            sub_plans=(
                Plan(METRIC, time=BASELINE, limit=1),
                Plan(METRIC, time=CURRENT, limit=1),
            ),
        )
        with self.assertRaises(ValueError):
            synthesize(bad_plan, _binding_steps())

    def test_blocked_step_maps_to_guard_blocked(self) -> None:
        steps = _binding_steps()
        steps = (
            steps[0],
            _step("current_total", TOTAL_COLUMNS, (), kind="blocked"),
            steps[2],
            steps[3],
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "guard_blocked")

    def test_error_step_maps_to_execution_error(self) -> None:
        steps = _binding_steps()
        steps = (
            steps[0],
            steps[1],
            _step("current_by_dimension", GROUP_COLUMNS, (), kind="error"),
            steps[3],
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "execution_error")

    def test_unexpected_clarify_step_raises_value_error(self) -> None:
        """意外 clarify 是内部契约错误（ADR-0026 ③）：不容错，raise ValueError。"""
        steps = _binding_steps()
        steps = (
            steps[0],
            steps[1],
            steps[2],
            _step("baseline_by_dimension", GROUP_COLUMNS, (), kind="clarify"),
        )
        with self.assertRaises(ValueError):
            synthesize(self.plan(), steps)

    def test_unexpected_handoff_step_raises_value_error(self) -> None:
        """意外 handoff 同为内部契约错误（ADR-0026 ③）：raise ValueError。"""
        steps = _binding_steps()
        steps = (
            steps[0],
            _step("current_total", TOTAL_COLUMNS, (), kind="handoff"),
            steps[2],
            steps[3],
        )
        with self.assertRaises(ValueError):
            synthesize(self.plan(), steps)

    def test_answer_step_with_wrong_metric_maps_to_column_mismatch(self) -> None:
        """步 metric 与 plan.metric 不符 → unavailable + column_mismatch（选码理由见
        synthesize docstring：eligibility 指计划级 dimension 声明，此处是绑定失配）。"""
        steps = _binding_steps()
        steps = (
            steps[0],
            steps[1],
            _step(
                "current_by_dimension",
                GROUP_COLUMNS,
                (("A", Decimal("30")),),
                metric="other_metric",
            ),
            steps[3],
        )
        attr = synthesize(self.plan(), steps)
        self.assert_unavailable(attr, "column_mismatch")


# ---------------------------------------------------------------------------
# 固定措辞（仅从 Attribution/plan 字段渲染；逐字锁定）
# ---------------------------------------------------------------------------


class TestTextWording(_SynthesisTestCase):
    def test_ok_text_locked(self) -> None:
        attr = synthesize(self.plan(), _binding_steps())
        self.assertEqual(
            attr.text,
            "commission_revenue 变化贡献分解：2013Q4 相对 2013Q3 总变化 -20"
            "（2013Q3 → 2013Q4）。按 Branch：A -30（贡献 150.000000%）、"
            "B +10（贡献 -50.000000%）。这是变化贡献分解，不代表业务因果。",
        )

    def test_net_zero_text_locked(self) -> None:
        """净零 unavailable 固定措辞（预评审更正①）：陈述净零事实，无百分数、无臆测。"""
        steps = _std_steps(
            Decimal("100"),
            Decimal("100"),
            (("A", Decimal("50")), ("B", Decimal("50"))),
            (("A", Decimal("60")), ("B", Decimal("40"))),
        )
        attr = synthesize(self.plan(), steps)
        self.assertEqual(
            attr.text,
            "无法给出 commission_revenue 的变化贡献分解（原因码：zero_total_delta）："
            "2013Q4 相对 2013Q3 两期总量相同（净零变化），贡献百分比无定义。",
        )

    def test_unavailable_text_locked_without_speculation_or_pct(self) -> None:
        blocked = _binding_steps()
        blocked = (
            blocked[0],
            _step("current_total", TOTAL_COLUMNS, (), kind="blocked"),
            blocked[2],
            blocked[3],
        )
        attr = synthesize(self.plan(), blocked)
        self.assertEqual(
            attr.text,
            "无法给出 commission_revenue 的变化贡献分解（原因码：guard_blocked）。",
        )
        errored = _binding_steps()
        errored = (
            errored[0],
            errored[1],
            _step("current_by_dimension", GROUP_COLUMNS, (), kind="error"),
            errored[3],
        )
        attr_err = synthesize(self.plan(), errored)
        self.assertEqual(
            attr_err.text,
            "无法给出 commission_revenue 的变化贡献分解（原因码：execution_error）。",
        )
        steps = _std_steps(
            Decimal("100"),
            Decimal("100"),
            (("A", Decimal("30")), ("B", Decimal("70"))),
            (("A", Decimal("60")), ("B", Decimal("40"))),
        )
        attr2 = synthesize(self.plan(group_limit=2), steps)
        self.assertEqual(
            attr2.text,
            "无法给出 commission_revenue 的变化贡献分解（原因码：possible_truncation）。",
        )

    def test_all_unavailable_texts_contain_no_percent(self) -> None:
        """unavailable 文本一律不含百分数与原因臆测（固定措辞，含净零路径）。"""
        net_zero = _std_steps(
            Decimal("100"),
            Decimal("100"),
            (("A", Decimal("50")), ("B", Decimal("50"))),
            (("A", Decimal("60")), ("B", Decimal("40"))),
        )
        cases: tuple[tuple[AnalysisPlan, tuple[TurnResult, ...]], ...] = (
            (self.plan(direction="increase"), _binding_steps()),
            (
                self.plan(),
                _std_steps(
                    Decimal("100"),
                    Decimal("90"),
                    (("A", Decimal("30")),),
                    (("A", Decimal("60")),),
                ),
            ),
            (
                self.plan(),
                _std_steps(
                    Decimal("100"),
                    Decimal("80"),
                    (("A", Decimal("30")), ("A", Decimal("20"))),
                    (("A", Decimal("60")),),
                ),
            ),
            (self.plan(), net_zero),
        )
        for plan, steps in cases:
            attr = synthesize(plan, steps)
            self.assertEqual(attr.status, "unavailable")
            self.assertNotIn("%", attr.text)
            self.assertNotIn("因为", attr.text)
            self.assertNotIn("可能因为", attr.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
