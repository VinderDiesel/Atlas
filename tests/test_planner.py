"""Planner 契约测试（unittest，零依赖；pytest 亦可发现运行）。

覆盖 eval/gold/gold-101~104 的「问句 → Plan」确定性解析链路：
- gold-101/102/103：与黄金集 expected_metric / expected_dimensions / expected_time 对照（Plan Acc）
- gold-104：歧义问句必须返回 ClarificationRequest（反问，不猜）
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent.compiler import Filter, OrderSpec, Plan, SemanticModel, TimeSpec
from agent.planner import ClarificationRequest, Planner

REPO = Path(__file__).resolve().parent.parent
MODEL = SemanticModel()
PLANNER = Planner(MODEL)


def load_gold(gold_id: str) -> dict:
    return json.loads((REPO / "eval" / "gold" / f"{gold_id}.json").read_text(encoding="utf-8"))


class TestGoldFinancePlanner(unittest.TestCase):
    """金融黄金集 gold-101~104 的 Plan 解析（Plan Acc 评测载体）。"""

    def test_gold_101_quarter_total_trade_value(self) -> None:
        plan = PLANNER.plan("2013 年第二季度总交易额是多少？")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)  # 供类型收窄（unittest 断言不改变类型）
        gold = load_gold("gold-101")
        self.assertEqual(plan.metric, gold["expected_metric"])
        self.assertEqual(plan.time, TimeSpec("quarter", "2013Q2"))
        self.assertEqual(plan.dimensions, ())

    def test_gold_102_commission_by_branch_top5(self) -> None:
        plan = PLANNER.plan("按分支统计 2013 年佣金收入，列出前 5 名")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        gold = load_gold("gold-102")
        self.assertEqual(plan.metric, gold["expected_metric"])
        self.assertEqual(plan.dimensions, tuple(gold["expected_dimensions"]))
        self.assertEqual(plan.time, TimeSpec("year", 2013))
        self.assertEqual(plan.order_by, (OrderSpec("commission_revenue", desc=True),))
        self.assertEqual(plan.limit, 5)

    def test_gold_103_holdings_value_at_date(self) -> None:
        plan = PLANNER.plan("2017 年 7 月 7 日全账户持仓市值合计是多少？")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        gold = load_gold("gold-103")
        self.assertEqual(plan.metric, gold["expected_metric"])
        self.assertEqual(plan.time, TimeSpec("date", "2017-07-07"))
        self.assertEqual(plan.dimensions, ())

    def test_gold_104_ambiguous_asks_clarification(self) -> None:
        """歧义问句必须返回 ClarificationRequest，而不是猜一个 Plan。"""
        result = PLANNER.plan("最近交易情况怎么样？")
        self.assertIsInstance(result, ClarificationRequest)


class TestDerivedMetricDisambiguation(unittest.TestCase):
    """派生指标 vs 基础指标同义词子串歧义消解（gold-156~162 评测先行发现，2026-09-04）。

    派生指标同义词含基础指标词（"平均每笔成交金额" ⊃ "成交金额"、"佣金率" ⊃
    "佣金"）时按最长命中取更具体口径；互不为子串的多命中（gold-122/148 双指标
    问句）仍是真歧义，必须反问。
    """

    def test_gold_156_average_trade_value_year(self) -> None:
        plan = PLANNER.plan("2013 年平均每笔成交金额是多少？")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(plan.metric, "average_trade_value")
        self.assertEqual(plan.time, TimeSpec("year", 2013))

    def test_gold_157_derived_topn_by_branch(self) -> None:
        plan = PLANNER.plan("按分支统计 2013 年平均每笔成交金额，列出前 5 名")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(plan.metric, "average_trade_value")
        self.assertEqual(plan.dimensions, ("Branch",))
        self.assertEqual(plan.limit, 5)

    def test_gold_158_average_commission_per_trade(self) -> None:
        # 句内含冗余基础词 "佣金"（commission_revenue 命中），须消解为派生口径
        plan = PLANNER.plan("2013 年平均每笔佣金是多少？")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(plan.metric, "average_commission_per_trade")

    def test_gold_159_commission_rate(self) -> None:
        # "佣金率" ⊃ "佣金"：取长命中 commission_rate，而非歧义反问
        plan = PLANNER.plan("2013 年佣金率是多少？")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(plan.metric, "commission_rate")

    def test_gold_160_commission_rate_topn(self) -> None:
        plan = PLANNER.plan("按分支统计 2013 年佣金率，列出前 5 名")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(plan.metric, "commission_rate")
        self.assertEqual(plan.dimensions, ("Branch",))
        self.assertEqual(plan.limit, 5)

    def test_gold_161_average_holding_value(self) -> None:
        plan = PLANNER.plan("2013 年户均持仓市值是多少？")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(plan.metric, "average_holding_value")

    def test_gold_162_average_cash_balance(self) -> None:
        plan = PLANNER.plan("2013 年户均现金余额是多少？")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(plan.metric, "average_cash_balance")

    def test_true_dual_metric_ambiguity_still_clarifies(self) -> None:
        # gold-122："成交量"与"交易额"互不为子串 → 真歧义保持反问（不猜测）
        plan = PLANNER.plan("2013 年成交量和交易额分别是多少？")
        self.assertIsInstance(plan, ClarificationRequest)
        assert isinstance(plan, ClarificationRequest)
        self.assertIn("指标口径歧义", plan.reasons[0])

    def test_base_metric_phrase_unaffected(self) -> None:
        # 基础词单独出现仍解析基础指标（消歧只处理包含关系，不改变既有行为）
        plan = PLANNER.plan("2013 年成交金额是多少？")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(plan.metric, "total_trade_value")


class TestPlannerDeterminism(unittest.TestCase):
    """确定性错误与边界：不猜测、不降级。"""

    def test_unknown_metric_asks_clarification(self) -> None:
        result = PLANNER.plan("今天天气怎么样？")
        self.assertIsInstance(result, ClarificationRequest)

    def test_relative_time_asks_clarification(self) -> None:
        result = PLANNER.plan("上个月佣金收入是多少？")
        self.assertIsInstance(result, ClarificationRequest)

    def test_plan_to_sql_full_chain(self) -> None:
        """问句 → Plan → SQL 完整链路（确定性优先的核心闭环）。"""
        from agent.compiler import Compiler

        compiler = Compiler(MODEL)
        plan = PLANNER.plan("2013 年第二季度总交易额是多少？")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        sql, notes = compiler.compile(plan)
        self.assertIn("CalendarQtrID = 20132", sql)
        self.assertTrue(notes)


class TestFilterParsing(unittest.TestCase):
    """filter 解析契约（ADR-0014 ①，gold-149~155 评测载体）。"""

    def test_gold_149_dimension_equality(self) -> None:
        plan = PLANNER.plan(
            "只看分支 IEMJHuQgCPDHCwwJkgQQeaqGvzMcVD 的 2013 年佣金收入是多少？"
        )
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(
            plan.filters,
            (Filter("Branch", "=", "IEMJHuQgCPDHCwwJkgQQeaqGvzMcVD"),),
        )

    def test_gold_150_dimension_exclusion(self) -> None:
        plan = PLANNER.plan(
            "排除分支 IEMJHuQgCPDHCwwJkgQQeaqGvzMcVD 后，2013 年总成交量是多少？"
        )
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(
            plan.filters,
            (Filter("Branch", "!=", "IEMJHuQgCPDHCwwJkgQQeaqGvzMcVD"),),
        )

    def test_gold_151_metric_threshold_having(self) -> None:
        plan = PLANNER.plan("2013 年按分支统计佣金收入超过 1000 万的分支，列出前 5 名")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(plan.dimensions, ("Branch",))
        self.assertEqual(plan.filters, (Filter("commission_revenue", ">", 10000000),))

    def test_gold_152_threshold_with_multi_dimension(self) -> None:
        plan = PLANNER.plan("2015 年按分支和客户等级统计交易额，交易额超过 1 亿的取前 5 名")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(plan.dimensions, ("Tier", "Branch"))
        self.assertEqual(plan.filters, (Filter("total_trade_value", ">", 100000000),))

    def test_gold_153_dimension_equality_with_group(self) -> None:
        plan = PLANNER.plan("只看客户等级 3 的客户，按分支统计 2014 年佣金收入，列出前 5 名")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(plan.dimensions, ("Branch",))
        self.assertEqual(plan.filters, (Filter("Tier", "=", 3),))

    def test_gold_154_threshold_lower_bound(self) -> None:
        plan = PLANNER.plan("2014 年按分支统计佣金收入低于 50 万的分支，列出前 5 名")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(plan.filters, (Filter("commission_revenue", "<", 500000),))

    def test_gold_155_ambiguous_filter_value_asks_clarification(self) -> None:
        """维度词前带修饰（「核心分支」）→ 值无法唯一确定 → 反问（gold-155）。"""
        result = PLANNER.plan("只看核心分支的 2013 年佣金收入是多少？")
        self.assertIsInstance(result, ClarificationRequest)

    def test_emphasis_only_without_dimension_produces_no_filter(self) -> None:
        """「只看」作强调语（短语无维度词）时不产生 filter 也不反问。"""
        plan = PLANNER.plan("只看 2013 年佣金收入是多少？")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(plan.filters, ())

    def test_missing_filter_value_asks_clarification(self) -> None:
        """维度词命中但无取值（「只看分支的…」）→ 反问。"""
        result = PLANNER.plan("只看分支的 2013 年佣金收入是多少？")
        self.assertIsInstance(result, ClarificationRequest)


class TestFollowupParsing(unittest.TestCase):
    """ADR-0014 ② 指代消解 MVP：同构残句补全（复用上轮 Plan 结构，仅替换片段）。

    followup 由 graph 指代预检调用（仅 plan() 已 unmatched 的残句）；本层契约
    直接测补全形态的正反两侧：链接词命中 → 结构 Plan；自由代词/歧义 → 反问。
    """

    PREV = Plan(
        metric="commission_revenue",
        dimensions=("Branch",),
        time=TimeSpec("year", 2013),
        order_by=(OrderSpec("commission_revenue", desc=True),),
        limit=5,
    )
    PREV_FILTER = Plan(
        metric="commission_revenue",
        time=TimeSpec("year", 2013),
        filters=(Filter("Branch", "=", "IEMJHuQgCPDHCwwJkgQQeaqGvzMcVD"),),
    )

    def test_change_time_inherits_structure(self) -> None:
        """「那 2014 年呢」→ 同 metric/维度/排序，仅换时间（同构追问主形态）。"""
        merged = PLANNER.followup("那 2014 年呢", self.PREV)
        self.assertIsInstance(merged, Plan)
        assert isinstance(merged, Plan)
        self.assertEqual(merged.metric, "commission_revenue")
        self.assertEqual(merged.dimensions, ("Branch",))
        self.assertEqual(merged.time, TimeSpec("year", 2014))
        self.assertEqual(merged.order_by, self.PREV.order_by)
        self.assertEqual(merged.limit, 5)

    def test_change_time_keeps_prev_filters(self) -> None:
        """上轮带维度值过滤时纯换时间：过滤继承（同构口径不变）。"""
        merged = PLANNER.followup("那 2014 年呢", self.PREV_FILTER)
        self.assertIsInstance(merged, Plan)
        assert isinstance(merged, Plan)
        self.assertEqual(merged.filters, self.PREV_FILTER.filters)
        self.assertEqual(merged.time, TimeSpec("year", 2014))

    def test_change_dimension(self) -> None:
        """「那按客户等级统计呢 / 那换成客户等级呢」→ 替换分组维度。"""
        for q in ("那按客户等级统计呢", "那换成客户等级呢"):
            with self.subTest(question=q):
                merged = PLANNER.followup(q, self.PREV)
                self.assertIsInstance(merged, Plan)
                assert isinstance(merged, Plan)
                self.assertEqual(merged.dimensions, ("Tier",))
                self.assertEqual(merged.time, TimeSpec("year", 2013))  # 时间保持

    def test_swap_time_via_shell_word(self) -> None:
        """「换成 2014 年呢」：壳词吃时间短语 → 换时间而非误判换维。"""
        merged = PLANNER.followup("换成 2014 年呢", self.PREV)
        self.assertIsInstance(merged, Plan)
        assert isinstance(merged, Plan)
        self.assertEqual(merged.time, TimeSpec("year", 2014))
        self.assertEqual(merged.dimensions, ("Branch",))

    def test_change_dim_with_prev_filter_clarifies(self) -> None:
        """换维 + 上轮带维度值过滤 = 过滤作用域二义 → 反问不猜。"""
        result = PLANNER.followup("那按客户等级统计呢", self.PREV_FILTER)
        self.assertIsInstance(result, ClarificationRequest)

    def test_free_pronoun_clarifies(self) -> None:
        """自由代词（那它呢/这些呢）无片段可替换 → 反问。"""
        for q in ("那它呢", "那这些呢"):
            with self.subTest(question=q):
                result = PLANNER.followup(q, self.PREV)
                self.assertIsInstance(result, ClarificationRequest)

    def test_relative_time_clarifies(self) -> None:
        """「那去年呢」→ 透传 relative_time 反问（快照评测口径）。"""
        result = PLANNER.followup("那去年呢", self.PREV)
        self.assertIsInstance(result, ClarificationRequest)
        assert isinstance(result, ClarificationRequest)
        self.assertEqual(result.kind, "relative_time")

    def test_no_shell_returns_none(self) -> None:
        """无链接词形态（新问句/陈述）→ None，调用方维持原 unmatched 流程。"""
        self.assertIsNone(PLANNER.followup("随便看看", self.PREV))

    def test_bare_ne_suffix_residual(self) -> None:
        """「2014 年呢」裸呢字残句（无链接词前缀）也补全换时间。"""
        merged = PLANNER.followup("2014 年呢", self.PREV)
        self.assertIsInstance(merged, Plan)
        assert isinstance(merged, Plan)
        self.assertEqual(merged.time, TimeSpec("year", 2014))
        self.assertEqual(merged.dimensions, ("Branch",))


if __name__ == "__main__":
    unittest.main()
