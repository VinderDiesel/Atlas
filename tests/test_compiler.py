"""Compiler 契约测试（unittest，零依赖；pytest 亦可发现运行）。

覆盖 eval/gold/finance/gold-101~103 的 Plan → SQL 确定性编译链路。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from sqlglot import parse_one

from agent.compiler import (
    CompileError,
    Compiler,
    Filter,
    OrderSpec,
    Plan,
    SemanticModel,
    TimeSpec,
)

MODEL = SemanticModel()
COMPILER = Compiler(MODEL)
RETAIL_MODEL = SemanticModel(
    Path(__file__).resolve().parent.parent / "semantic" / "ossie" / "atlas_retail.ossie.yaml"
)
RETAIL_COMPILER = Compiler(RETAIL_MODEL)


class TestGoldFinance(unittest.TestCase):
    """金融黄金集 gold-101~103 的编译链路。"""

    def test_gold_101_quarter_total_trade_value(self) -> None:
        sql, notes = COMPILER.compile(
            Plan(metric="total_trade_value", time=TimeSpec("quarter", "2013Q2"))
        )
        self.assertIn("CalendarQtrID = 20132", sql)
        self.assertIn(
            "SUM(fact_trades.Quantity * fact_trades.TradePrice) AS total_trade_value", sql
        )
        self.assertEqual(notes, ["fact_trades → dim_date（trades_to_date）"])

    def test_gold_102_commission_by_branch_top5(self) -> None:
        sql, notes = COMPILER.compile(
            Plan(
                metric="commission_revenue",
                dimensions=("Branch",),
                time=TimeSpec("year", 2013),
                order_by=(OrderSpec("commission_revenue", desc=True),),
                limit=5,
            )
        )
        self.assertIn("dim_broker.Branch AS Branch", sql)
        self.assertIn("CalendarYearID = 2013", sql)
        self.assertIn("GROUP BY dim_broker.Branch", sql)
        self.assertIn("ORDER BY commission_revenue DESC", sql)
        self.assertIn("LIMIT 5", sql)
        self.assertTrue(any("dim_broker" in n for n in notes))

    def test_gold_103_holdings_value_at_date(self) -> None:
        sql, _ = COMPILER.compile(
            Plan(metric="holdings_value", time=TimeSpec("date", "2017-07-07"))
        )
        self.assertIn("dim_date.DateValue = CAST('2017-07-07' AS DATE)", sql)
        self.assertIn("SUM(fact_holdings.CurrentValue) AS holdings_value", sql)

    def test_month_granularity_yyyym_encoding(self) -> None:
        """月粒度谓词必须匹配 TPC-DI YYYYM 编码（实测 201405 命中 0 行，回归防护）。"""
        sql, _ = COMPILER.compile(Plan(metric="trade_count", time=TimeSpec("month", 201405)))
        # TimeSpec 用 YYYYMM（201405）承载，物理列是 YYYYM 拼接（2014 年 5 月 = 20145）
        self.assertIn("CalendarMonthID = 20145", sql)

    def test_roundtrip_parsable(self) -> None:
        """生成 SQL 必须可被 sqlglot 往返解析（AGENTS.md 7.2）。"""
        plans = (
            Plan(metric="total_trade_value", time=TimeSpec("quarter", "2013Q2")),
            Plan(metric="commission_revenue", dimensions=("Branch",), time=TimeSpec("year", 2013)),
            Plan(metric="holdings_value", time=TimeSpec("date", "2017-07-07")),
        )
        for plan in plans:
            sql, _ = COMPILER.compile(plan)
            self.assertEqual(parse_one(sql).sql(), sql)


class TestTimeDimensionDeclaration(unittest.TestCase):
    """time_dimension 声明解析（compiler 时间列不再硬编码，P3 声明化契约）。"""

    def test_finance_single_declaration(self) -> None:
        """金融声明 = 原 TIME_COLUMNS 常量逐字（值不变是零回归的源头）。"""
        self.assertEqual(
            MODEL.time_dimension,
            {
                "table": "dim_date",
                "mode": "single",
                "columns": {
                    "year": "CalendarYearID",
                    "quarter": "CalendarQtrID",
                    "month": "CalendarMonthID",
                    "date": "DateValue",
                },
            },
        )

    def test_retail_composite_declaration(self) -> None:
        """零售声明 = TPC-DS 拆分列（year + qoy/moy，date 走实际日期列）。"""
        self.assertEqual(
            RETAIL_MODEL.time_dimension,
            {
                "table": "dim_date",
                "mode": "composite",
                "columns": {
                    "year": "d_year",
                    "quarter": "d_qoy",
                    "month": "d_moy",
                    "date": "d_date",
                },
            },
        )

    def test_retail_dimension_synonyms_collected(self) -> None:
        """dim_* 前缀改名后：非时间维度字段同义词入库，时间字段不混入。"""
        self.assertEqual(
            RETAIL_MODEL.dimension_synonyms,
            {
                "i_category": ("品类", "类别", "商品类别"),
                "i_brand": ("品牌",),
                "s_state": ("州", "省份", "门店州"),
                "s_city": ("城市", "门店城市"),
                "s_store_sk": ("店铺", "分店"),
            },
        )
        # d_year/d_moy/d_qoy is_time: true → 不入维度同义词（金融 L24-26 同款缺陷修复）
        self.assertNotIn("d_year", RETAIL_MODEL.dimension_synonyms)
        self.assertNotIn("d_moy", RETAIL_MODEL.dimension_synonyms)
        self.assertNotIn("d_qoy", RETAIL_MODEL.dimension_synonyms)


class TestRetailCompositeTime(unittest.TestCase):
    """零售 composite 时间谓词 4 形态（物理表 date_dim + 别名 dim_date）。"""

    def test_year_equality(self) -> None:
        sql, notes = RETAIL_COMPILER.compile(
            Plan(metric="total_sales_price", time=TimeSpec("year", 2000))
        )
        self.assertIn("dim_date.d_year = 2000", sql)
        self.assertIn("INNER JOIN atlas.dwd.date_dim AS dim_date", sql)
        self.assertEqual(notes, ["store_sales → dim_date（sales_to_date）"])

    def test_quarter_split(self) -> None:
        sql, _ = RETAIL_COMPILER.compile(
            Plan(metric="total_sales_price", time=TimeSpec("quarter", "1999Q1"))
        )
        self.assertIn("dim_date.d_year = 1999 AND dim_date.d_qoy = 1", sql)

    def test_month_split(self) -> None:
        sql, _ = RETAIL_COMPILER.compile(
            Plan(metric="total_sales_price", time=TimeSpec("month", 200005))
        )
        # composite：YYYYMM 拆 YYYY 与 M 双等值（无 single 的 YYYYM 拼接）
        self.assertIn("dim_date.d_year = 2000 AND dim_date.d_moy = 5", sql)
        self.assertNotIn("20005", sql)

    def test_date_cast(self) -> None:
        sql, _ = RETAIL_COMPILER.compile(
            Plan(metric="total_sales_price", time=TimeSpec("date", "2000-01-02"))
        )
        self.assertIn("dim_date.d_date = CAST('2000-01-02' AS DATE)", sql)

    def test_group_by_category_with_time(self) -> None:
        sql, _ = RETAIL_COMPILER.compile(
            Plan(
                metric="total_sales_price",
                dimensions=("i_category",),
                time=TimeSpec("year", 2000),
            )
        )
        self.assertIn("dim_item.i_category AS i_category", sql)
        self.assertIn("GROUP BY dim_item.i_category", sql)

    def test_bad_quarter_raises(self) -> None:
        with self.assertRaises(CompileError):
            RETAIL_COMPILER.compile(
                Plan(metric="total_sales_price", time=TimeSpec("quarter", "2000-1"))
            )


class TestFilterCompilation(unittest.TestCase):
    """filter → SQL 编译契约（ADR-0014 ①：维度值 WHERE + 度量阈值 HAVING）。"""

    def test_dimension_equality_where_with_auto_join(self) -> None:
        """维度过滤表不在查询中时自动补 join（gold-149 形态）。"""
        sql, _ = COMPILER.compile(
            Plan(
                metric="commission_revenue",
                time=TimeSpec("year", 2013),
                filters=(Filter("Branch", "=", "BR_X"),),
            )
        )
        self.assertIn("dim_broker.Branch = 'BR_X'", sql)
        self.assertIn("INNER JOIN atlas.dwd.dim_broker", sql)

    def test_dimension_exclusion_neq(self) -> None:
        sql, _ = COMPILER.compile(
            Plan(
                metric="total_trade_quantity",
                time=TimeSpec("year", 2013),
                filters=(Filter("Branch", "!=", "BR_X"),),
            )
        )
        self.assertIn("dim_broker.Branch <> 'BR_X'", sql)

    def test_metric_threshold_having(self) -> None:
        """度量阈值（column == metric）→ HAVING 聚合比较（gold-151/154 形态）。"""
        sql, _ = COMPILER.compile(
            Plan(
                metric="commission_revenue",
                dimensions=("Branch",),
                time=TimeSpec("year", 2013),
                filters=(Filter("commission_revenue", ">", 10000000),),
                order_by=(OrderSpec("commission_revenue", desc=True),),
                limit=5,
            )
        )
        self.assertIn("GROUP BY dim_broker.Branch", sql)
        self.assertIn("HAVING SUM(fact_trades.Commission) > 10000000", sql)
        self.assertNotIn("HAVING commission_revenue", sql)

    def test_threshold_join_auto_join_and_having(self) -> None:
        """维度过滤 + 度量阈值并存：WHERE 维度 + HAVING 阈值（gold-153 加阈值形态）。"""
        sql, _ = COMPILER.compile(
            Plan(
                metric="commission_revenue",
                dimensions=("Branch",),
                time=TimeSpec("year", 2014),
                filters=(Filter("commission_revenue", ">", 10000000), Filter("Tier", "=", 3)),
            )
        )
        self.assertIn("dim_customer.Tier = 3", sql)
        self.assertIn("INNER JOIN atlas.dwd.dim_customer", sql)
        self.assertIn("HAVING SUM(fact_trades.Commission) > 10000000", sql)

    def test_having_without_group_raises(self) -> None:
        """度量阈值无分组维度 → 确定性报错（不静默降级为 WHERE）。"""
        with self.assertRaises(CompileError):
            COMPILER.compile(
                Plan(metric="commission_revenue", filters=(Filter("commission_revenue", ">", 1),))
            )

    def test_unknown_filter_column_raises(self) -> None:
        with self.assertRaises(CompileError):
            COMPILER.compile(
                Plan(metric="commission_revenue", filters=(Filter("not_a_field", "=", 1),))
            )

    def test_filter_sql_roundtrip_parsable(self) -> None:
        """filter 生成的 SQL 必须可 sqlglot 往返（AGENTS.md 7.2）。"""
        plans = (
            Plan(
                metric="commission_revenue",
                dimensions=("Branch",),
                time=TimeSpec("year", 2013),
                filters=(Filter("commission_revenue", ">", 10000000),),
            ),
            Plan(
                metric="commission_revenue",
                time=TimeSpec("year", 2013),
                filters=(Filter("Branch", "=", "BR_X"),),
            ),
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
            COMPILER.compile(Plan(metric="total_trade_value", time=TimeSpec("quarter", "2013-2")))

    def test_order_by_unknown_column_raises(self) -> None:
        with self.assertRaises(CompileError):
            COMPILER.compile(Plan(metric="commission_revenue", order_by=(OrderSpec("region"),)))


if __name__ == "__main__":
    unittest.main()
