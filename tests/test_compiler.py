"""Compiler 契约测试（unittest，零依赖；pytest 亦可发现运行）。

覆盖 eval/gold/gold-101~103 的 Plan → SQL 确定性编译链路。
"""

from __future__ import annotations

import unittest

from sqlglot import parse_one

from agent.compiler import CompileError, Compiler, OrderSpec, Plan, SemanticModel, TimeSpec

MODEL = SemanticModel()
COMPILER = Compiler(MODEL)


class TestGoldFinance(unittest.TestCase):
    """金融黄金集 gold-101~103 的编译链路。"""

    def test_gold_101_quarter_total_trade_value(self) -> None:
        sql, notes = COMPILER.compile(
            Plan(metric="total_trade_value", time=TimeSpec("quarter", "2005Q2"))
        )
        self.assertIn("CalendarQtrID = 20052", sql)
        self.assertIn("SUM(fact_trades.Quantity * fact_trades.TradePrice) AS total_trade_value", sql)
        self.assertEqual(notes, ["fact_trades → dim_date（trades_to_date）"])

    def test_gold_102_commission_by_branch_top5(self) -> None:
        sql, notes = COMPILER.compile(
            Plan(
                metric="commission_revenue",
                dimensions=("Branch",),
                time=TimeSpec("year", 2005),
                order_by=(OrderSpec("commission_revenue", desc=True),),
                limit=5,
            )
        )
        self.assertIn("dim_broker.Branch AS Branch", sql)
        self.assertIn("CalendarYearID = 2005", sql)
        self.assertIn("GROUP BY dim_broker.Branch", sql)
        self.assertIn("ORDER BY commission_revenue DESC", sql)
        self.assertIn("LIMIT 5", sql)
        self.assertTrue(any("dim_broker" in n for n in notes))

    def test_gold_103_holdings_value_at_date(self) -> None:
        sql, _ = COMPILER.compile(Plan(metric="holdings_value", time=TimeSpec("date", "2005-12-31")))
        self.assertIn("dim_date.DateValue = CAST('2005-12-31' AS DATE)", sql)
        self.assertIn("SUM(fact_holdings.CurrentValue) AS holdings_value", sql)

    def test_roundtrip_parsable(self) -> None:
        """生成 SQL 必须可被 sqlglot 往返解析（AGENTS.md 7.2）。"""
        plans = (
            Plan(metric="total_trade_value", time=TimeSpec("quarter", "2005Q2")),
            Plan(metric="commission_revenue", dimensions=("Branch",), time=TimeSpec("year", 2005)),
            Plan(metric="holdings_value", time=TimeSpec("date", "2005-12-31")),
        )
        for plan in plans:
            sql, _ = COMPILER.compile(plan)
            self.assertEqual(parse_one(sql).sql(), sql)


class TestCompileErrors(unittest.TestCase):
    """确定性错误：不猜测、不降级（AGENTS.md 决策优先级）。"""

    def test_unknown_metric_raises(self) -> None:
        with self.assertRaises(CompileError):
            COMPILER.compile(Plan(metric="gmv"))

    def test_unknown_dimension_raises(self) -> None:
        with self.assertRaises(CompileError):
            COMPILER.compile(Plan(metric="trade_count", dimensions=("region",)))

    def test_bad_quarter_format_raises(self) -> None:
        with self.assertRaises(CompileError):
            COMPILER.compile(Plan(metric="total_trade_value", time=TimeSpec("quarter", "2005-2")))

    def test_order_by_unknown_column_raises(self) -> None:
        with self.assertRaises(CompileError):
            COMPILER.compile(Plan(metric="commission_revenue", order_by=(OrderSpec("region"),)))


if __name__ == "__main__":
    unittest.main()
