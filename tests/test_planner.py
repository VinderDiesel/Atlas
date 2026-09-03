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


if __name__ == "__main__":
    unittest.main()
