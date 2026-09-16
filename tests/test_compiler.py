"""Compiler 契约测试（unittest，零依赖；pytest 亦可发现运行）。

覆盖 eval/gold/finance/gold-101~103 的 Plan → SQL 确定性编译链路。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from sqlglot import parse_one

from agent.compiler import (
    ComparisonSpec,
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

_FINANCE_YAML = (
    Path(__file__).resolve().parent.parent / "semantic" / "ossie" / "atlas_finance.ossie.yaml"
)

# 最小 ossie 骨架：time_dimension 与 policy 拆在两个 ATLAS 扩展块（去 break 回归锁）
_SPLIT_BLOCKS_YAML = """\
semantic_model:
  - name: tmp_split_blocks
    datasets: []
    relationships: []
    metrics: []
    custom_extensions:
      - vendor_name: ATLAS
        data: '{"time_dimension": {"table": "dim_date", "mode": "single", "columns": {}}}'
      - vendor_name: ATLAS
        data: '{"policy": {"default_row_policy": "rp_branch_visible"}}'
"""


def _load_model_from_text(text: str) -> SemanticModel:
    """YAML 文本 → 临时文件 → SemanticModel（纯文本构造，不依赖仓库路径）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tmp_model.ossie.yaml"
        path.write_text(text, encoding="utf-8")
        return SemanticModel(path)


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
                "s_store_sk": ("门店", "店铺", "分店"),
            },
        )
        # d_year/d_moy/d_qoy is_time: true → 不入维度同义词（金融 L24-26 同款缺陷修复）
        self.assertNotIn("d_year", RETAIL_MODEL.dimension_synonyms)
        self.assertNotIn("d_moy", RETAIL_MODEL.dimension_synonyms)
        self.assertNotIn("d_qoy", RETAIL_MODEL.dimension_synonyms)


class TestDefaultRowPolicyDeclaration(unittest.TestCase):
    """default_row_policy 声明解析（域 → 策略的唯一事实源，ADR-0021 决策 ②）。"""

    def test_finance_declaration(self) -> None:
        self.assertEqual(MODEL.default_row_policy, "rp_branch_visible")

    def test_retail_declaration(self) -> None:
        self.assertEqual(RETAIL_MODEL.default_row_policy, "rp_dept_visible")

    def test_missing_declaration_is_none(self) -> None:
        """缺 policy 声明的模型 → None（缺失即拒绝的事实依据，不给静默默认值）。"""
        source = _FINANCE_YAML.read_text(encoding="utf-8")
        stripped = source.replace('"default_row_policy": "rp_branch_visible",', "", 1)
        self.assertNotIn("default_row_policy", stripped, "剥离失败：请检查 YAML 原文")
        model = _load_model_from_text(stripped)
        self.assertIsNone(model.default_row_policy)
        self.assertIsNotNone(model.time_dimension)  # 其余声明不受影响

    def test_policy_parsed_from_separate_extension_block(self) -> None:
        """去 break 回归锁：policy 声明在第二个 ATLAS 块也必须解析到。

        旧实现解析到 time_dimension 即 break——policy 在块 2 时会静默丢失（None）。
        """
        model = _load_model_from_text(_SPLIT_BLOCKS_YAML)
        self.assertEqual(
            model.time_dimension, {"table": "dim_date", "mode": "single", "columns": {}}
        )
        self.assertEqual(model.default_row_policy, "rp_branch_visible")


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


class TestComparisonSqlShape(unittest.TestCase):
    """时间智能 SQL 形态（ADR-0017 判据 4 真链锁：2026-09-15 ccb4c8b 首次真跑发现）。"""

    def test_rank_window_order_by_is_expression_not_alias(self) -> None:
        """rank：窗口内 ORDER BY 必须是聚合表达式本体，不是同层 SELECT 别名。

        实测 Doris 在窗口函数内不认同层别名：`RANK() OVER (ORDER BY
        total_trade_value DESC)` 报 `Unknown column 'total_trade_value' in
        'table list' in AGGREGATE clause`（gold-177/178/076/077 四条 rank 样本
        真链全部失败）；展开为聚合表达式后同形实测通过。yoy/pop/cumulative
        不受影响，因其经 CTE 包裹后别名成为内层物化列。
        """
        sql, _ = COMPILER.compile(
            Plan(
                metric="total_trade_value",
                dimensions=("Branch",),
                time=TimeSpec("year", 2015),
                comparison=ComparisonSpec(kind="rank"),
            )
        )
        self.assertIn("AS total_trade_value", sql)
        self.assertIn("RANK() OVER (ORDER BY SUM(", sql)
        self.assertNotIn("ORDER BY total_trade_value", sql)

    def test_rank_default_order_by_rank_desc_with_dimension_tiebreaker(self) -> None:
        """rank 外层默认按名次降序 + 维度 tie-breaker（ADR-0017 判据 4 真链锁）。

        2026-09-15 ccb4c8b 真跑发现：rank 无外层 ORDER BY 时，200+ 分支打满
        LIMIT 100 的截断点取决于引擎扫描序（gold-177 实测 6 轮同 hash 属计划
        稳定巧合，非查询语义保证；且名次结果无序不可读）。默认
        `ORDER BY rank DESC, <dims>`：行序唯一（名次并列时维度名唯一）。
        """
        sql, _ = COMPILER.compile(
            Plan(
                metric="total_trade_value",
                dimensions=("Branch",),
                time=TimeSpec("year", 2015),
                comparison=ComparisonSpec(kind="rank"),
            )
        )
        self.assertIn("ORDER BY rank DESC, Branch", sql)

    def test_yoy_with_dimension_partitions_by_dimension(self) -> None:
        """yoy + 维度：外层必须按维度 PARTITION 并投影维度列（ADR-0017 判据 4 真链锁）。

        2026-09-15 ccb4c8b 真跑发现三重缺陷（gold-173 六轮六 hash、gold-073
        六轮五 hash）：① LAG 无 PARTITION 跨维度串算；② base 的
        `ORDER BY 时间 LIMIT 100` 把 200+ 分支 × 2 年截成单年（100 行全是
        2014，2015 被截光）；③ 外层 ORDER BY 仅时间列、同年全并列 → 行序随机。
        修复形态：base 去 ORDER BY/LIMIT；外层投影维度列、LAG
        `PARTITION BY 维度`、`ORDER BY 维度, 时间`（分组键+时间唯一，截断点确定）。
        """
        sql, _ = COMPILER.compile(
            Plan(
                metric="total_trade_value",
                dimensions=("Branch",),
                time=TimeSpec("year", 2015),
                comparison=ComparisonSpec(kind="yoy"),
            )
        )
        self.assertIn("PARTITION BY Branch", sql)
        self.assertIn("SELECT Branch, calendaryearid, total_trade_value", sql)
        self.assertIn("ORDER BY Branch, calendaryearid", sql)
        # base 不再带 ORDER BY/LIMIT（截断会丢年份），LIMIT 仅外层一次
        self.assertEqual(sql.count("LIMIT"), 1)
        self.assertNotIn("NULLS LAST", sql)

    def test_yoy_without_dimension_shape_unchanged(self) -> None:
        """回归锁：无维度 yoy 保持历史形态（base ORDER BY + 双层 LIMIT）。

        无维度路径已有 7 条已锚定样本（gold-172/174/175/176/072/074/075），
        维度修复不得触碰该路径的任何字节。
        """
        sql, _ = COMPILER.compile(
            Plan(
                metric="total_trade_value",
                time=TimeSpec("year", 2015),
                comparison=ComparisonSpec(kind="yoy"),
            )
        )
        self.assertIn("LAG(total_trade_value) OVER (ORDER BY calendaryearid)", sql)
        self.assertNotIn("PARTITION BY", sql)
        self.assertEqual(sql.count("LIMIT"), 2)

    def test_pop_composite_quarter_predicate_is_or_of_combos(self) -> None:
        """composite 季/月粒度：IN 谓词必须是逐期 (年 AND 季) 组合（ADR-0017 判据 4 真链锁）。

        2026-09-15 ccb4c8b 真跑发现：`_time_value_to_int` 对 composite 返回年值，
        而 IN 却落在季列上 → `d_qoy IN (2001, 2001)` 值域错位恒不命中，
        gold-074「2001 年第二季度环比」执行 0 行（空结果）。修复为各期携带
        自己的年：同年 (2001,Q1) OR (2001,Q2)；跨年 (2014,Q4) OR (2015,Q1)
        取数范围同样正确（prev 方向为已登记的未支持边界）。
        """
        sql, _ = RETAIL_COMPILER.compile(
            Plan(
                metric="total_sales_price",
                time=TimeSpec("quarter", "2001Q2"),
                comparison=ComparisonSpec(kind="pop"),
            )
        )
        self.assertIn("dim_date.d_year = 2001 AND dim_date.d_qoy = 1", sql)
        self.assertIn("dim_date.d_year = 2001 AND dim_date.d_qoy = 2", sql)
        self.assertNotIn("IN (2001, 2001)", sql)


def _ten_cases() -> list[tuple[str, Compiler, Plan, str | None]]:
    """ADR-0025 判据 1/3 的 10 组合：{finance, retail} × {plain, rank, yoy, pop, cumulative}。

    第 4 元 = `emitted_time_column` 期望别名（plain/rank 为 None）；全部为无维度
    路径（有维度路径由 TestComparisonSqlShape 的 ADR-0017 真链锁用例覆盖）。
    """
    cases: list[tuple[str, Compiler, Plan, str | None]] = []
    domains = (
        (
            "finance",
            COMPILER,
            "total_trade_value",
            2015,
            "2015Q2",
            2014,
            {"yoy": "calendaryearid", "pop": "calendarqtrid", "cumulative": "calendarmonthid"},
        ),
        (
            "retail",
            RETAIL_COMPILER,
            "total_sales_price",
            2001,
            "2001Q2",
            2001,
            {"yoy": "d_year", "pop": "d_qoy", "cumulative": "d_moy"},
        ),
    )
    for domain, compiler, metric, year, quarter, cum_year, aliases in domains:
        for kind_name, kind, time in (
            ("plain", None, TimeSpec("year", year)),
            ("rank", "rank", TimeSpec("year", year)),
            ("yoy", "yoy", TimeSpec("year", year)),
            ("pop", "pop", TimeSpec("quarter", quarter)),
            ("cumulative", "cumulative", TimeSpec("year", cum_year)),
        ):
            comparison = ComparisonSpec(kind=kind) if kind else None
            cases.append(
                (
                    f"{domain}/{kind_name}",
                    compiler,
                    Plan(metric=metric, time=time, comparison=comparison),
                    aliases.get(kind_name),
                )
            )
    return cases


# 黄金字面量（2026-09-16 重构前实测产物逐字拷贝；判据 3 的漂移基线）
_GOLDEN_SQL: dict[str, str] = {
    "finance/plain": (
        "SELECT SUM(fact_trades.Quantity * fact_trades.TradePrice) AS total_trade_value FROM "
        "atlas.dwd.fact_trades AS fact_trades INNER JOIN atlas.dwd.dim_date AS dim_date ON "
        "fact_trades.SK_CreateDateID = dim_date.SK_DateID WHERE dim_date.CalendarYearID = 2015 "
        "LIMIT 100"
    ),
    "finance/rank": (
        "SELECT SUM(fact_trades.Quantity * fact_trades.TradePrice) AS total_trade_value, RANK() "
        "OVER (ORDER BY SUM(fact_trades.Quantity * fact_trades.TradePrice) DESC) AS rank FROM "
        "atlas.dwd.fact_trades AS fact_trades INNER JOIN atlas.dwd.dim_date AS dim_date ON "
        "fact_trades.SK_CreateDateID = dim_date.SK_DateID WHERE dim_date.CalendarYearID = 2015 "
        "ORDER BY rank DESC LIMIT 100"
    ),
    "finance/yoy": (
        "WITH base AS (SELECT dim_date.CalendarYearID AS calendaryearid, SUM(fact_trades.Quantity "
        "* fact_trades.TradePrice) AS total_trade_value FROM atlas.dwd.fact_trades AS fact_trades "
        "INNER JOIN atlas.dwd.dim_date AS dim_date ON fact_trades.SK_CreateDateID = "
        "dim_date.SK_DateID WHERE dim_date.CalendarYearID IN (2014, 2015) GROUP BY "
        "dim_date.CalendarYearID ORDER BY dim_date.CalendarYearID ASC NULLS LAST LIMIT 100) SELECT "
        "calendaryearid, total_trade_value, LAG(total_trade_value) OVER (ORDER BY calendaryearid) "
        "AS prev_period_value FROM base ORDER BY calendaryearid LIMIT 100"
    ),
    "finance/pop": (
        "WITH base AS (SELECT dim_date.CalendarQtrID AS calendarqtrid, SUM(fact_trades.Quantity * "
        "fact_trades.TradePrice) AS total_trade_value FROM atlas.dwd.fact_trades AS fact_trades "
        "INNER JOIN atlas.dwd.dim_date AS dim_date ON fact_trades.SK_CreateDateID = "
        "dim_date.SK_DateID WHERE dim_date.CalendarQtrID IN (20151, 20152) GROUP BY "
        "dim_date.CalendarQtrID ORDER BY dim_date.CalendarQtrID ASC NULLS LAST LIMIT 100) SELECT "
        "calendarqtrid, total_trade_value, LAG(total_trade_value) OVER (ORDER BY calendarqtrid) AS "
        "prev_period_value FROM base ORDER BY calendarqtrid LIMIT 100"
    ),
    "finance/cumulative": (
        "WITH monthly AS (SELECT dim_date.CalendarMonthID AS calendarmonthid, "
        "SUM(fact_trades.Quantity * fact_trades.TradePrice) AS total_trade_value FROM "
        "atlas.dwd.fact_trades AS fact_trades INNER JOIN atlas.dwd.dim_date AS dim_date ON "
        "fact_trades.SK_CreateDateID = dim_date.SK_DateID WHERE dim_date.CalendarYearID = 2014 "
        "GROUP BY dim_date.CalendarMonthID ORDER BY dim_date.CalendarMonthID ASC NULLS LAST LIMIT "
        "100) SELECT calendarmonthid, total_trade_value, SUM(total_trade_value) OVER (ORDER BY "
        "calendarmonthid ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative_value "
        "FROM monthly ORDER BY calendarmonthid LIMIT 100"
    ),
    "retail/plain": (
        "SELECT SUM(store_sales.ss_ext_sales_price) AS total_sales_price FROM "
        "atlas.dwd.store_sales AS store_sales INNER JOIN atlas.dwd.date_dim AS dim_date ON "
        "store_sales.ss_sold_date_sk = dim_date.d_date_sk WHERE dim_date.d_year = 2001 LIMIT 100"
    ),
    "retail/rank": (
        "SELECT SUM(store_sales.ss_ext_sales_price) AS total_sales_price, RANK() OVER (ORDER BY "
        "SUM(store_sales.ss_ext_sales_price) DESC) AS rank FROM atlas.dwd.store_sales AS "
        "store_sales INNER JOIN atlas.dwd.date_dim AS dim_date ON store_sales.ss_sold_date_sk = "
        "dim_date.d_date_sk WHERE dim_date.d_year = 2001 ORDER BY rank DESC LIMIT 100"
    ),
    "retail/yoy": (
        "WITH base AS (SELECT dim_date.d_year AS d_year, SUM(store_sales.ss_ext_sales_price) AS "
        "total_sales_price FROM atlas.dwd.store_sales AS store_sales INNER JOIN atlas.dwd.date_dim "
        "AS dim_date ON store_sales.ss_sold_date_sk = dim_date.d_date_sk WHERE dim_date.d_year IN "
        "(2000, 2001) GROUP BY dim_date.d_year ORDER BY dim_date.d_year ASC NULLS LAST LIMIT 100) "
        "SELECT d_year, total_sales_price, LAG(total_sales_price) OVER (ORDER BY d_year) AS "
        "prev_period_value FROM base ORDER BY d_year LIMIT 100"
    ),
    "retail/pop": (
        "WITH base AS (SELECT dim_date.d_qoy AS d_qoy, SUM(store_sales.ss_ext_sales_price) AS "
        "total_sales_price FROM atlas.dwd.store_sales AS store_sales INNER JOIN atlas.dwd.date_dim "
        "AS dim_date ON store_sales.ss_sold_date_sk = dim_date.d_date_sk WHERE (dim_date.d_year = "
        "2001 AND dim_date.d_qoy = 1) OR (dim_date.d_year = 2001 AND dim_date.d_qoy = 2) GROUP BY "
        "dim_date.d_qoy ORDER BY dim_date.d_qoy ASC NULLS LAST LIMIT 100) SELECT d_qoy, "
        "total_sales_price, LAG(total_sales_price) OVER (ORDER BY d_qoy) AS prev_period_value FROM "
        "base ORDER BY d_qoy LIMIT 100"
    ),
    "retail/cumulative": (
        "WITH monthly AS (SELECT dim_date.d_moy AS d_moy, SUM(store_sales.ss_ext_sales_price) AS "
        "total_sales_price FROM atlas.dwd.store_sales AS store_sales INNER JOIN atlas.dwd.date_dim "
        "AS dim_date ON store_sales.ss_sold_date_sk = dim_date.d_date_sk WHERE dim_date.d_year = "
        "2001 GROUP BY dim_date.d_moy ORDER BY dim_date.d_moy ASC NULLS LAST LIMIT 100) SELECT "
        "d_moy, total_sales_price, SUM(total_sales_price) OVER (ORDER BY d_moy ROWS BETWEEN "
        "UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative_value FROM monthly ORDER BY d_moy "
        "LIMIT 100"
    ),
}


class TestComparisonSqlGoldenBytes(unittest.TestCase):
    """ADR-0025 判据 3：等价重构零漂移——10 组合 SQL 逐字基线。"""

    def test_golden_sql_covers_all_cases(self) -> None:
        self.assertEqual({name for name, *_ in _ten_cases()}, set(_GOLDEN_SQL))

    def test_compile_output_matches_golden_bytes(self) -> None:
        for name, compiler, plan, _ in _ten_cases():
            with self.subTest(case=name):
                self.assertEqual(compiler.compile(plan)[0], _GOLDEN_SQL[name])


class TestEmittedTimeColumn(unittest.TestCase):
    """ADR-0025 判据 1：时间列别名规则收敛到单一调用点。"""

    def test_matches_first_projection_alias(self) -> None:
        """10 组合：emitted == 首列别名；plain/rank 两侧均 None（防只测通过路径）。"""
        for name, compiler, plan, expected in _ten_cases():
            with self.subTest(case=name):
                first = parse_one(compiler.compile(plan)[0]).expressions[0].alias_or_name
                got = compiler.emitted_time_column(plan)
                if expected is None:
                    self.assertIsNone(got)
                    self.assertEqual(first, plan.metric)
                else:
                    self.assertEqual(got, expected)
                    self.assertEqual(first, got)

    def test_lower_is_single_call_site(self) -> None:
        """`.lower()` 时间列语义命中数 = 1（AST 静态断言，ADR-0025 判据 1）。

        `_months_map` 的两处 `.lower()` 属 locale 词典校验（月份名捕获组），
        与时间列别名无关；`_apply_lag`/`_apply_cumulative` 归零即收敛证据。
        """
        source = (Path(__file__).resolve().parent.parent / "agent" / "compiler.py").read_text(
            encoding="utf-8"
        )
        lower_counts: dict[str, int] = {}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef):
                lower_counts[node.name] = sum(
                    1
                    for sub in ast.walk(node)
                    if isinstance(sub, ast.Attribute) and sub.attr == "lower"
                )
        self.assertEqual(lower_counts.get("emitted_time_column"), 1)
        self.assertEqual(lower_counts.get("_apply_lag"), 0)
        self.assertEqual(lower_counts.get("_apply_cumulative"), 0)


if __name__ == "__main__":
    unittest.main()
