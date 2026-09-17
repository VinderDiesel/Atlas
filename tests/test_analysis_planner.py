"""AnalysisPlanner 契约测试（ADR-0026 T03；unittest，pytest 亦可发现运行）。

覆盖 ADR-0026 T03 验收矩阵（task-3-brief 逐条对应）：
- 无分析意图返回 None（None 仅表示没有分析意图）；完整四槽返回 AnalysisPlan；
- "为什么/帮我分析"缺槽不回落普通问数（必须 ClarificationRequest）；
- 两期拆分：严格匹配连接词决定角色（先出现 = 本期 current，后出现 = 基期
  baseline，与绑定样例「分析 2013Q4 相对 2013Q3 …」一致）；消除年/月/季度
  内部重叠命中；两种词序、跨年、同期间、混粒度、三个期间、省略年份、
  相对时间全部锁定；
- 校验：恰一个 Metric/Dimension、attribution_dimensions 资格声明、四步同
  WHERE、无 HAVING（阈值）/TopN、未识别过滤片段必须澄清（未知条件不静默丢弃）；
- 模板形态：总量步 limit=1（不分组不排序）、分组步 limit=group_limit 且
  OrderSpec(D) 升序、四步 comparison=None、共享 metric/filters；
- 编译预检四个 Plan（不执行 SQL）；同输入输出相等（plan_projection）；
  不以重新拼写的子问句再走 Planner.plan。
"""

from __future__ import annotations

import unittest
from unittest import mock

from agent.analysis import (
    AnalysisPlan,
    AnalysisPlanner,
    plan_projection,
    validate_analysis_plan,
)
from agent.compiler import Compiler, Filter, OrderSpec, SemanticModel, TimeSpec
from agent.planner import ClarificationRequest, Planner, ValueNotice

MODEL = SemanticModel()
PLANNER = AnalysisPlanner(MODEL, group_limit=50)

CANONICAL_ZH = "分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献"
CANONICAL_EN = "Analyze commission revenue in 2013Q4 compared with 2013Q3 by branch"


def assert_plan(result: object) -> AnalysisPlan:
    """断言结果为 AnalysisPlan 并收窄类型（unittest 断言不改变静态类型）。"""
    assert isinstance(result, AnalysisPlan)
    return result


def assert_clarify(result: object) -> ClarificationRequest:
    """断言结果为 ClarificationRequest 并收窄类型（缺槽绝不回落 Plan/None）。"""
    assert isinstance(result, ClarificationRequest)
    return result


class TestNoAnalysisIntent(unittest.TestCase):
    """无分析意图 → None（决策①：None 仅表示没有分析意图）。"""

    def test_plain_question_returns_none_zh(self) -> None:
        self.assertIsNone(PLANNER.plan("查一下 2013Q4 的佣金收入是多少"))

    def test_plain_question_returns_none_en(self) -> None:
        self.assertIsNone(PLANNER.plan("Show me commission revenue in 2013Q4"))

    def test_relative_word_without_intent_returns_none(self) -> None:
        """含相对时间词但无分析意图 → None（意图检测先于期间解析）。"""
        self.assertIsNone(PLANNER.plan("上季度的佣金收入是多少"))


class TestCanonicalPlans(unittest.TestCase):
    """完整四槽 → 固定四步模板 AnalysisPlan（绑定样例逐字锁定）。"""

    def test_canonical_zh_binding(self) -> None:
        """task-3-brief 的绑定样例：先出现的期间 = 本期，后出现 = 基期。"""
        plan = assert_plan(PLANNER.plan(CANONICAL_ZH))
        self.assertEqual(
            [p.time.value for p in plan.sub_plans],
            ["2013Q3", "2013Q4", "2013Q4", "2013Q3"],
        )
        self.assertEqual(
            [p.dimensions for p in plan.sub_plans],
            [(), (), ("Branch",), ("Branch",)],
        )
        self.assertTrue(all(p.comparison is None for p in plan.sub_plans))
        self.assertEqual(plan.metric, "commission_revenue")
        self.assertEqual(plan.dimension, "Branch")
        self.assertEqual(plan.baseline, TimeSpec("quarter", "2013Q3"))
        self.assertEqual(plan.current, TimeSpec("quarter", "2013Q4"))

    def test_canonical_en(self) -> None:
        plan = assert_plan(PLANNER.plan(CANONICAL_EN))
        self.assertEqual(
            [p.time.value for p in plan.sub_plans],
            ["2013Q3", "2013Q4", "2013Q4", "2013Q3"],
        )
        self.assertEqual(plan.metric, "commission_revenue")
        self.assertEqual(plan.dimension, "Branch")
        self.assertEqual(plan.direction, "change")

    def test_metric_before_period_word_order_zh(self) -> None:
        """词序二：指标在前、期间在后，角色仍由连接词两侧先后决定。"""
        plan = assert_plan(PLANNER.plan("分析 佣金收入 2013Q4 相比 2013Q3 按分支的变化贡献"))
        self.assertEqual(
            [p.time.value for p in plan.sub_plans],
            ["2013Q3", "2013Q4", "2013Q4", "2013Q3"],
        )

    def test_dimension_before_period_word_order_en(self) -> None:
        """词序二（en）：by branch 在期间前，槽位解析与角色不受词序影响。"""
        plan = assert_plan(
            PLANNER.plan("Analyze commission revenue by branch for 2013Q4 compared with 2013Q3")
        )
        self.assertEqual(
            [p.time.value for p in plan.sub_plans],
            ["2013Q3", "2013Q4", "2013Q4", "2013Q3"],
        )
        self.assertEqual(plan.dimension, "Branch")

    def test_cross_year_periods(self) -> None:
        plan = assert_plan(PLANNER.plan("分析 2014Q1 相对 2013Q4 的佣金收入按分支的变化贡献"))
        self.assertEqual(plan.baseline, TimeSpec("quarter", "2013Q4"))
        self.assertEqual(plan.current, TimeSpec("quarter", "2014Q1"))
        self.assertEqual(
            [p.time.value for p in plan.sub_plans],
            ["2013Q4", "2014Q1", "2014Q1", "2013Q4"],
        )

    def test_identical_periods_still_valid_plan(self) -> None:
        """同期间：validate_analysis_plan 不禁止（零差值是 T04 综合门的事）。"""
        plan = assert_plan(PLANNER.plan("分析 2013Q4 相对 2013Q4 的佣金收入按分支的变化贡献"))
        self.assertEqual(plan.baseline, plan.current)
        self.assertEqual(validate_analysis_plan(plan), ())

    def test_overlap_elimination_chinese_quarter_words(self) -> None:
        """中文季度命中不得再触发年/月形态（"2013 年第四季度"只算一个季度期间）。

        词形与 test_locale_patterns 的既有锁定一致（"2013 年第三季度"）；
        年份形态「2013 年」在季度区间内命中，必须让位于季度（重叠消除）。
        """
        plan = assert_plan(
            PLANNER.plan("分析 2013 年第四季度 相对 2013 年第三季度 的佣金收入按分支的变化贡献")
        )
        self.assertEqual(
            [p.time.value for p in plan.sub_plans],
            ["2013Q3", "2013Q4", "2013Q4", "2013Q3"],
        )
        self.assertEqual([p.time.granularity for p in plan.sub_plans], ["quarter"] * 4)

    def test_single_char_connector_standalone_still_works_zh(self) -> None:
        """独立单字连接词「比/较」仍派角色（修复不得伤及既有措辞）。"""
        for connector in ("比", "较"):
            plan = assert_plan(
                PLANNER.plan(f"分析 2013Q4 {connector} 2013Q3 的佣金收入按分支的变化贡献")
            )
            self.assertEqual(
                [p.time.value for p in plan.sub_plans],
                ["2013Q3", "2013Q4", "2013Q4", "2013Q3"],
            )


class TestClarifyNotFallback(unittest.TestCase):
    """命中分析意图但缺槽/不支持 → ClarificationRequest，绝不回落普通问数。"""

    def test_why_missing_slots_clarifies_zh(self) -> None:
        result = PLANNER.plan("为什么佣金收入下降了")
        clarify = assert_clarify(result)
        self.assertNotEqual(clarify.kind, "unmatched")

    def test_help_analyze_missing_slots_clarifies(self) -> None:
        self.assertIsInstance(PLANNER.plan("帮我分析一下佣金收入"), ClarificationRequest)

    def test_why_missing_slots_clarifies_en(self) -> None:
        self.assertIsInstance(PLANNER.plan("Why did commission revenue drop"), ClarificationRequest)

    def test_missing_dimension_clarifies(self) -> None:
        result = PLANNER.plan("分析 2013Q4 相对 2013Q3 的佣金收入变化贡献")
        assert_clarify(result)

    def test_relative_time_clarifies_zh(self) -> None:
        result = PLANNER.plan("分析 2013Q4 相对上季度的佣金收入变化贡献")
        self.assertEqual(assert_clarify(result).kind, "relative_time")

    def test_relative_time_clarifies_en(self) -> None:
        result = PLANNER.plan("Analyze why commission revenue dropped last quarter by branch")
        self.assertEqual(assert_clarify(result).kind, "relative_time")

    def test_three_periods_clarifies(self) -> None:
        result = PLANNER.plan("分析 2013Q4 相对 2013Q3 相对 2013Q2 的佣金收入按分支的变化贡献")
        assert_clarify(result)

    def test_mixed_granularity_clarifies(self) -> None:
        result = PLANNER.plan("分析 2013 年 12 月 相对 2013Q4 的佣金收入按分支的变化贡献")
        assert_clarify(result)

    def test_omitted_year_clarifies(self) -> None:
        """省略年份（时间形态全部要求四位年）→ 0 个期间 → 澄清。"""
        result = PLANNER.plan("分析 第四季度 相对 第三季度 的佣金收入按分支的变化贡献")
        assert_clarify(result)

    def test_single_period_clarifies(self) -> None:
        result = PLANNER.plan("分析 2013Q4 相对 第三季度 的佣金收入按分支的变化贡献")
        assert_clarify(result)

    def test_missing_connector_clarifies(self) -> None:
        """两期之间无对比连接词 → 时间角色不清 → 澄清（不猜角色）。"""
        result = PLANNER.plan("分析 2013Q4 和 2013Q3 的佣金收入按分支的变化贡献")
        assert_clarify(result)

    def test_connector_outside_span_pair(self) -> None:
        """连接词在两期跨度之外（句首/句尾）不派角色：必须落在两期之间才算。"""
        # 句首的「对比」在第一期间之前，两期间隙（" 和 "）无连接词
        assert_clarify(PLANNER.plan("对比 2013Q4 和 2013Q3 的佣金收入按分支的变化贡献"))
        # 句尾的「对比」在第二期间之后（落入维度短语区），两期间隙（" 与 "）无连接词
        assert_clarify(PLANNER.plan("分析 2013Q4 与 2013Q3 的佣金收入按分支对比的变化贡献"))

    def test_single_char_connector_inside_noun_does_not_derive_roles_zh(self) -> None:
        """「比值」中的「比」是名词词素，不构成对比连接词 → 角色不清 → 澄清。"""
        result = PLANNER.plan("分析 2013Q4 的比值与 2013Q3 的佣金收入按分支的变化贡献")
        assert_clarify(result)

    def test_metric_ambiguity_clarifies_en(self) -> None:
        result = PLANNER.plan(
            "Analyze trade value and commission revenue in 2013Q4 compared with 2013Q3 by branch"
        )
        clarify = assert_clarify(result)
        self.assertTrue(clarify.candidates)

    def test_metric_unmatched_clarifies(self) -> None:
        result = PLANNER.plan("分析 2013Q4 相对 2013Q3 的某某指标按分支的变化贡献")
        assert_clarify(result)

    def test_ambiguous_dimension_clarifies_zh(self) -> None:
        result = PLANNER.plan("分析 2013Q4 相对 2013Q3 的佣金收入按分支和客户等级的变化贡献")
        assert_clarify(result)

    def test_ambiguous_dimension_clarifies_en(self) -> None:
        result = PLANNER.plan(
            "Analyze commission revenue in 2013Q4 compared with 2013Q3 by branch and tier"
        )
        assert_clarify(result)

    def test_direction_conflict_clarifies(self) -> None:
        result = PLANNER.plan("分析 2013Q4 相对 2013Q3 的佣金收入下降按分支的变化贡献上涨")
        assert_clarify(result)


class TestValidationAndGuards(unittest.TestCase):
    """单指标/单维度、资格声明、同 WHERE、无 HAVING/TopN、未知条件不丢弃。"""

    def test_threshold_having_clarifies_zh(self) -> None:
        result = PLANNER.plan("分析 2013Q4 相对 2013Q3 的佣金收入大于 100 万按分支的变化贡献")
        assert_clarify(result)

    def test_threshold_having_clarifies_en(self) -> None:
        result = PLANNER.plan(
            "Analyze commission revenue in 2013Q4 compared with 2013Q3 over 1000000 by branch"
        )
        assert_clarify(result)

    def test_topn_clarifies_zh(self) -> None:
        result = PLANNER.plan("分析 2013Q4 相对 2013Q3 的佣金收入前 10 名按分支的变化贡献")
        assert_clarify(result)

    def test_topn_clarifies_en(self) -> None:
        result = PLANNER.plan(
            "Analyze top 5 branches by commission revenue in 2013Q4 compared with 2013Q3"
        )
        assert_clarify(result)

    def test_unattributed_filter_fragment_clarifies_zh(self) -> None:
        """过滤触发词命中但短语归属不到维度字段 → 澄清（不静默丢弃）。"""
        result = PLANNER.plan("分析 2013Q4 相对 2013Q3 只看周末的佣金收入按分支的变化贡献")
        assert_clarify(result)

    def test_unattributed_filter_fragment_clarifies_en(self) -> None:
        result = PLANNER.plan(
            "Analyze commission revenue in 2013Q4 compared with 2013Q3 only for weekend by branch"
        )
        assert_clarify(result)

    def test_mixed_filter_fragment_clarifies_zh(self) -> None:
        """混合片段：已解析出 Branch=A 后仍有未消费的过滤触发命中 → 必须澄清。

        修复前：_parse_filters 只解析首个「只看」命中（分支A），plan() 的守卫只在
        **零 filter** 时触发，第二个片段「只看周末」被静默丢弃 → 错算贡献。
        """
        result = PLANNER.plan(
            "分析 2013Q4 相对 2013Q3 的佣金收入只看分支A，只看周末按分支的变化贡献"
        )
        assert_clarify(result)

    def test_mixed_filter_fragment_clarifies_en(self) -> None:
        """en 混合片段：only for branch A + only for weekend，后者不得静默丢弃。"""
        result = PLANNER.plan(
            "Analyze commission revenue in 2013Q4 compared with 2013Q3 "
            "only for branch A and only for weekend by branch"
        )
        assert_clarify(result)

    def test_mixed_filter_fragment_order_insensitive_zh(self) -> None:
        """片段顺序无关：两种顺序都必须澄清（未知条件不丢弃与出现顺序无关）。"""
        clarify = assert_clarify(
            PLANNER.plan("分析 2013Q4 相对 2013Q3 的佣金收入只看分支A，只看周末按分支的变化贡献")
        )
        swapped = assert_clarify(
            PLANNER.plan("分析 2013Q4 相对 2013Q3 的佣金收入只看周末，只看分支A按分支的变化贡献")
        )
        self.assertEqual(clarify.kind, swapped.kind)

    def test_undeclared_combo_clarifies_zh(self) -> None:
        """commission_revenue × Tier 未登记 attribution_dimensions → 澄清。"""
        result = PLANNER.plan("分析 2013Q4 相对 2013Q3 的佣金收入按客户等级的变化贡献")
        assert_clarify(result)

    def test_undeclared_combo_clarifies_en(self) -> None:
        result = PLANNER.plan("Analyze commission revenue in 2013Q4 compared with 2013Q3 by tier")
        assert_clarify(result)

    def test_same_where_shared_across_sub_plans(self) -> None:
        """可解析的维度值 filter 进四步共享的同一 WHERE。"""
        plan = assert_plan(
            PLANNER.plan("分析 2013Q4 相对 2013Q3 的佣金收入只看分支A，按分支的变化贡献")
        )
        self.assertEqual(plan.filters, (Filter("Branch", "=", "A"),))
        self.assertTrue(all(p.filters == plan.filters for p in plan.sub_plans))

    def test_alias_filter_value_notice_propagates(self) -> None:
        """值域别名归一（NSDQ→NASDAQ）记入 AnalysisPlan.notices，不静默丢弃。"""
        plan = assert_plan(
            PLANNER.plan("分析 2013Q4 相对 2013Q3 的佣金收入只看交易所NSDQ，按分支的变化贡献")
        )
        self.assertEqual(plan.filters, (Filter("ExchangeID", "=", "NASDAQ"),))
        self.assertEqual(
            plan.notices,
            (ValueNotice(field="ExchangeID", raw="NSDQ", value="NASDAQ", kind="alias"),),
        )


class TestPlanTemplateShape(unittest.TestCase):
    """固定四步模板形态（决策①）：总量 limit=1；分组 limit=group_limit 升序。"""

    def test_four_sub_plan_template(self) -> None:
        plan = assert_plan(PLANNER.plan(CANONICAL_ZH))
        self.assertEqual([s.metric for s in plan.sub_plans], ["commission_revenue"] * 4)
        # 角色顺序 = ANALYSIS_ROLES：baseline_total / current_total /
        # current_by_dimension / baseline_by_dimension
        self.assertEqual(
            [s.time for s in plan.sub_plans],
            [
                TimeSpec("quarter", "2013Q3"),
                TimeSpec("quarter", "2013Q4"),
                TimeSpec("quarter", "2013Q4"),
                TimeSpec("quarter", "2013Q3"),
            ],
        )
        # 总量步：不分组、不排序、limit=1
        for sub in plan.sub_plans[:2]:
            self.assertEqual(sub.dimensions, ())
            self.assertEqual(sub.order_by, ())
            self.assertEqual(sub.limit, 1)
        # 分组步：恰按维度一列升序、limit=group_limit
        for sub in plan.sub_plans[2:]:
            self.assertEqual(sub.dimensions, ("Branch",))
            self.assertEqual(sub.order_by, (OrderSpec("Branch"),))
            self.assertEqual(sub.limit, 50)
            self.assertIs(sub.order_by[0].desc, False)
        # 四步共享 metric/filters，comparison 全 None
        self.assertTrue(all(s.filters == () for s in plan.sub_plans))
        self.assertTrue(all(s.comparison is None for s in plan.sub_plans))
        # 头部字段
        self.assertEqual(plan.intent, "change_contribution")
        self.assertEqual(plan.direction, "change")
        self.assertEqual(plan.synthesizer, "additive_delta_v1")
        self.assertEqual(plan.recipe_version, 1)
        self.assertEqual(validate_analysis_plan(plan), ())

    def test_group_limit_is_honored(self) -> None:
        planner = AnalysisPlanner(MODEL, group_limit=7)
        plan = assert_plan(planner.plan(CANONICAL_ZH))
        self.assertEqual([s.limit for s in plan.sub_plans], [1, 1, 7, 7])

    def test_direction_decrease(self) -> None:
        plan = assert_plan(
            PLANNER.plan("为什么 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献下降了")
        )
        self.assertEqual(plan.direction, "decrease")

    def test_direction_increase(self) -> None:
        plan = assert_plan(
            PLANNER.plan("分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献上涨的原因")
        )
        self.assertEqual(plan.direction, "increase")

    def test_direction_against_is_not_increase_en(self) -> None:
        """「against」含「gain」子串，不得据此误判为 increase（须仍为 change）。"""
        plan = assert_plan(
            PLANNER.plan("Analyze commission revenue in 2013Q4 against 2013Q3 by branch")
        )
        self.assertEqual(plan.direction, "change")

    def test_direction_gains_word_still_increase_en(self) -> None:
        """真正的增益措辞「gains」仍判定为 increase（修复不得伤及既有措辞）。"""
        plan = assert_plan(
            PLANNER.plan(
                "Analyze commission revenue gains in 2013Q4 compared with 2013Q3 by branch"
            )
        )
        self.assertEqual(plan.direction, "increase")

    def test_direction_conflict_clarifies(self) -> None:
        result = PLANNER.plan("分析 2013Q4 相对 2013Q3 的佣金收入下降按分支的变化贡献上涨")
        assert_clarify(result)


class TestDeterminismAndPrecheck(unittest.TestCase):
    """确定性门（同输入 → 同输出）与编译预检（不执行 SQL、不重走 Planner）。"""

    def test_same_input_equal_output(self) -> None:
        plan1 = assert_plan(PLANNER.plan(CANONICAL_ZH))
        plan2 = assert_plan(PLANNER.plan(CANONICAL_ZH))
        self.assertEqual(plan1, plan2)
        self.assertEqual(
            tuple(plan_projection(s) for s in plan1.sub_plans),
            tuple(plan_projection(s) for s in plan2.sub_plans),
        )

    def test_compile_precheck_produces_sql_without_execution(self) -> None:
        """四个子 Plan 都能被确定性编译器编译（预检通过）；本模块不执行 SQL。"""
        plan = assert_plan(PLANNER.plan(CANONICAL_ZH))
        compiler = Compiler(MODEL)
        for sub in plan.sub_plans:
            sql, _notes = compiler.compile(sub)
            self.assertIsInstance(sql, str)
            self.assertIn("SELECT", sql)

    def test_does_not_reparse_subquestions_through_planner(self) -> None:
        """不以重新拼写的子问句再走 Planner.plan（槽位级复用，非重问）。"""
        with mock.patch.object(
            PLANNER._planner,
            "plan",
            side_effect=AssertionError("AnalysisPlanner must not re-parse sub-questions"),
        ):
            assert_plan(PLANNER.plan(CANONICAL_ZH))


class TestPublicPrimitives(unittest.TestCase):
    """跨模块复用只经 planner 公开名（修复轮 1）：analysis.py 不引用下划线私有名。"""

    def test_planner_exports_public_pattern_aliases(self) -> None:
        """planner 为 11 个形态层原语提供公开别名（同一对象，零行为变化）。"""
        import agent.planner as planner_module

        for public, private in (
            ("EQ_FILTER_RE", "_EQ_FILTER_RE"),
            ("EXC_FILTER_RE", "_EXC_FILTER_RE"),
            ("THRESHOLD_GT_RE", "_THRESHOLD_GT_RE"),
            ("THRESHOLD_LT_RE", "_THRESHOLD_LT_RE"),
            ("TOP_N_RE", "_TOP_N_RE"),
            ("EN_ONLY_RE", "_EN_ONLY_RE"),
            ("EN_EXCL_RE", "_EN_EXCL_RE"),
            ("EN_THRESHOLD_GT_RE", "_EN_THRESHOLD_GT_RE"),
            ("EN_THRESHOLD_LT_RE", "_EN_THRESHOLD_LT_RE"),
            ("EN_TOP_N_RE", "_EN_TOP_N_RE"),
            ("scan_time_spans", "_scan_time_spans"),
        ):
            self.assertIs(getattr(planner_module, public), getattr(planner_module, private))

    def test_planner_exports_public_method_aliases(self) -> None:
        """Planner 为 4 个被 analysis.py 复用的方法提供公开别名。"""
        for public, private in (
            ("resolve_locale", "_resolve_locale"),
            ("match_metric", "_match_metric"),
            ("parse_filters", "_parse_filters"),
            ("dim_hits", "_dim_hits"),
        ):
            self.assertIs(getattr(Planner, public), getattr(Planner, private))

    def test_analysis_module_imports_no_private_planner_names(self) -> None:
        """agent.analysis 命名空间不得再出现 planner 的下划线私有名。"""
        import agent.analysis as analysis_module

        leaked = {
            "_EN_EXCL_RE",
            "_EN_ONLY_RE",
            "_EN_THRESHOLD_GT_RE",
            "_EN_THRESHOLD_LT_RE",
            "_EN_TOP_N_RE",
            "_EQ_FILTER_RE",
            "_EXC_FILTER_RE",
            "_THRESHOLD_GT_RE",
            "_THRESHOLD_LT_RE",
            "_TOP_N_RE",
            "_scan_time_spans",
        } & set(vars(analysis_module))
        self.assertEqual(leaked, set())


if __name__ == "__main__":
    unittest.main()
