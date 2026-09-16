"""Planner 契约测试（unittest，零依赖；pytest 亦可发现运行）。

覆盖 eval/gold/finance/gold-101~104 的「问句 → Plan」确定性解析链路：
- gold-101/102/103：与黄金集 expected_metric / expected_dimensions / expected_time 对照（Plan Acc）
- gold-104：歧义问句必须返回 ClarificationRequest（反问，不猜）
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent.compiler import (
    Filter,
    OrderSpec,
    Plan,
    SemanticModel,
    TimeSpec,
    load_locale_synonyms,
)
from agent.planner import ClarificationRequest, Planner

REPO = Path(__file__).resolve().parent.parent
MODEL = SemanticModel()
PLANNER = Planner(MODEL)
RETAIL_MODEL = SemanticModel(
    REPO / "semantic" / "ossie" / "atlas_retail.ossie.yaml"
)
RETAIL_PLANNER = Planner(RETAIL_MODEL)


def load_gold(gold_id: str) -> dict:
    """按 id 段路由到域目录（gold-1xx → finance/，gold-0xx → retail/）。"""
    domain = "finance" if gold_id.startswith("gold-1") else "retail"
    return json.loads(
        (REPO / "eval" / "gold" / domain / f"{gold_id}.json").read_text(encoding="utf-8")
    )


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


class TestRetailGoldPlanner(unittest.TestCase):
    """零售黄金集 retail/gold-001~062 的 Plan 解析（P4 落库，2026-09-04）。

    覆盖中文问句全形态：无时间基础聚合（001）、绝对时间四粒度（051/061/062）、
    分组（品类 056 / 门店城市 057）、TopN（058）、维度等值 filter（059）、
    度量阈值 HAVING filter（060）、相对时间反问（047）。
    """

    def assert_plan(self, question: str, gold_id: str) -> Plan:
        plan = RETAIL_PLANNER.plan(question)
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)  # 供类型收窄（unittest 断言不改变类型）
        gold = load_gold(gold_id)
        self.assertEqual(plan.metric, gold["expected_metric"])
        return plan

    def test_gold_001_no_time_total_sales(self) -> None:
        plan = self.assert_plan("总销售额是多少？", "gold-001")
        self.assertEqual(plan.dimensions, ())
        self.assertIsNone(plan.time)
        self.assertEqual(plan.filters, ())

    def test_gold_051_year_total_sales(self) -> None:
        plan = self.assert_plan("2000 年总销售额是多少？", "gold-051")
        self.assertEqual(plan.time, TimeSpec("year", 2000))
        self.assertEqual(plan.dimensions, ())

    def test_gold_054_year_avg_order_value(self) -> None:
        # 派生指标同义词「客单价」唯一命中，不得歧义反问
        plan = self.assert_plan("2000 年客单价是多少？", "gold-054")
        self.assertEqual(plan.time, TimeSpec("year", 2000))
        self.assertEqual(plan.dimensions, ())

    def test_gold_056_year_by_category(self) -> None:
        plan = self.assert_plan(
            "2000 年按品类统计的销售额是多少？", "gold-056"
        )
        self.assertEqual(plan.time, TimeSpec("year", 2000))
        self.assertEqual(plan.dimensions, ("i_category",))

    def test_gold_057_year_by_city(self) -> None:
        # 州维度降级城市（SF0.1 全库单州，见 eval/gold/retail/data-profile.md）
        plan = self.assert_plan(
            "2000 年按门店城市统计的销售额是多少？", "gold-057"
        )
        self.assertEqual(plan.dimensions, ("s_city",))

    def test_gold_058_top3_categories(self) -> None:
        plan = self.assert_plan(
            "2000 年按品类统计销售额，列出前 3 名", "gold-058"
        )
        self.assertEqual(plan.dimensions, ("i_category",))
        self.assertEqual(plan.order_by, (OrderSpec("total_sales_price", desc=True),))
        self.assertEqual(plan.limit, 3)

    def test_gold_059_city_equality_filter(self) -> None:
        plan = self.assert_plan(
            "只看城市 Midway 的 2000 年销售额是多少？", "gold-059"
        )
        self.assertEqual(
            plan.filters, (Filter("s_city", "=", "Midway"),)
        )

    def test_gold_060_threshold_having_filter(self) -> None:
        plan = self.assert_plan(
            "2000 年按品类统计销售额超过 900 万的品类有哪些？", "gold-060"
        )
        self.assertEqual(plan.dimensions, ("i_category",))
        self.assertEqual(
            plan.filters, (Filter("total_sales_price", ">", 9000000),)
        )

    def test_gold_061_quarter(self) -> None:
        plan = self.assert_plan(
            "1999 年第一季度销售额是多少？", "gold-061"
        )
        self.assertEqual(plan.time, TimeSpec("quarter", "1999Q1"))

    def test_gold_062_month(self) -> None:
        plan = self.assert_plan(
            "2000 年 5 月的销售额是多少？", "gold-062"
        )
        self.assertEqual(plan.time, TimeSpec("month", 200005))

    def test_gold_047_relative_time_clarifies(self) -> None:
        """「最近卖得怎么样」→ 反问（零售歧义样本，与金融 gold-104 对称）。

        实测路径：口语「卖得怎么样」不命中任何指标同义词 → metric 步骤先于时间
        检查返回 unmatched（反问文案见 gold-047.clarification，覆盖口径/时间/维度
        三问）；runner 对歧义样本只检查 ClarificationRequest，不区分 kind。
        """
        result = RETAIL_PLANNER.plan("最近卖得怎么样？")
        self.assertIsInstance(result, ClarificationRequest)
        assert isinstance(result, ClarificationRequest)
        self.assertEqual(result.kind, "unmatched")


class TestStoreDimensionResolution(unittest.TestCase):
    """门店维度解析（P-1 收口：gold-076 裁定——「门店」归 s_store_sk）。

    裁定落地两层：① `dim_store.s_store_sk` 同义词补「门店」（事实表
    `ss_store_sk` 早有该词，词义共识；原标注的 s_state 代理自承为妥协，
    实测 12 家门店可分组）；② 重叠词条按**最长词优先**归属——「门店城市」
    「门店州」不得因「门店」是其子串而双命中（gold-057 防误伤，
    「多命中全取」在重叠词条上是错误行为，见 ADR-0017 代价 ⑤）。
    """

    def test_gold_076_store_ranking(self) -> None:
        plan = RETAIL_PLANNER.plan("按门店统计 2001 年销售额排名")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)  # 供类型收窄（unittest 断言不改变类型）
        gold = load_gold("gold-076")
        self.assertEqual(plan.metric, gold["expected_metric"])
        # 锁裁定语义本身（与 gold 文件解耦）：门店 → s_store_sk
        self.assertEqual(plan.dimensions, ("s_store_sk",))
        self.assertEqual(tuple(gold["expected_dimensions"]), ("s_store_sk",))
        self.assertEqual(plan.time, TimeSpec("year", 2001))

    def test_store_city_not_double_hit(self) -> None:
        """「门店城市」只归 s_city：更长词条覆盖「门店」子串，不得双命中。"""
        plan = RETAIL_PLANNER.plan("2000 年按门店城市统计的销售额是多少？")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(plan.dimensions, ("s_city",))

    def test_store_state_not_double_hit(self) -> None:
        """「门店州」只归 s_state：同理防「门店」子串双命中。"""
        plan = RETAIL_PLANNER.plan("按门店州统计 2001 年销售额")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(plan.dimensions, ("s_state",))


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


class TestEnglishPlanner(unittest.TestCase):
    """英文形态契约（P6 locale 化，2026-09-05）：形态词表/正则按 locale 分片。

    时间语序（quarter 两向/月份名/ISO date 共享/裸年介词约束）、分组 by/
    grouped by、TopN top/best + 后短语提维度、过滤 only/excluding（含实体复数
    后缀裁剪）、阈值 over/under + million/K 量级、相对时间反问、指代追问
    what about/instead、歧义仍反问、双语隔离——与中文同构，中文零回归。
    """

    BRANCH_X = "IEMJHuQgCPDHCwwJkgQQeaqGvzMcVD"  # gold-149 既有实测值

    def assert_plan(self, planner: Planner, question: str, **expected) -> Plan:
        plan = planner.plan(question, locale="en")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        if "metric" in expected:
            self.assertEqual(plan.metric, expected["metric"])
        if "time" in expected:
            self.assertEqual(plan.time, TimeSpec(*expected["time"]))
        if "dims" in expected:
            self.assertEqual(plan.dimensions, tuple(expected["dims"]))
        if "filters" in expected:
            self.assertEqual(plan.filters, tuple(expected["filters"]))
        if "limit" in expected:
            self.assertEqual(plan.limit, expected["limit"])
        return plan

    # -- 时间语序（4）----------------------------------------------------

    def test_en_year_preposition(self) -> None:
        plan = self.assert_plan(
            PLANNER, "What was the total trade value in 2015?",
            metric="total_trade_value", time=("year", 2015),
        )
        self.assertEqual(plan.dimensions, ())

    def test_en_quarter_word_order_a(self) -> None:
        """语序 A：Q2 2013（quarter 在年前）。"""
        self.assert_plan(
            PLANNER, "What was the total trade value in Q2 2013?",
            metric="total_trade_value", time=("quarter", "2013Q2"),
        )

    def test_en_quarter_word_order_b(self) -> None:
        """语序 B：2013 Q2（年在前）与 A 同值。"""
        self.assert_plan(
            PLANNER, "What was the total trade value in 2013 Q2?",
            metric="total_trade_value", time=("quarter", "2013Q2"),
        )

    def test_en_month_name(self) -> None:
        """月份名 May 2014 → month 编码 201405（与中文"2014 年 5 月"同值）。"""
        self.assert_plan(
            PLANNER, "How many trades were there in May 2014?",
            metric="trade_count", time=("month", 201405),
        )

    def test_en_iso_date_shared(self) -> None:
        """ISO date 语言无关，与 zh 共享同一正则。"""
        self.assert_plan(
            PLANNER, "What was the total cash balance on 2014-03-31?",
            metric="cash_balance", time=("date", "2014-03-31"),
        )

    # -- 分组形态（3）----------------------------------------------------

    def test_en_group_by_branch(self) -> None:
        self.assert_plan(
            PLANNER, "Show commission revenue by branch in 2013",
            metric="commission_revenue", dims=("Branch",), time=("year", 2013),
        )

    def test_en_group_by_category_retail(self) -> None:
        self.assert_plan(
            RETAIL_PLANNER, "What were the total sales by category in 1999?",
            metric="total_sales_price", dims=("i_category",), time=("year", 1999),
        )

    def test_en_grouped_by_variant(self) -> None:
        """grouped by 长形（与 by 同构）。"""
        self.assert_plan(
            PLANNER, "What was the commission revenue grouped by branch in 2013?",
            metric="commission_revenue", dims=("Branch",), time=("year", 2013),
        )

    # -- TopN（2）--------------------------------------------------------

    def test_en_top_n_word(self) -> None:
        """top N + by 分组（直译中文"列出前 5 名"形态）。"""
        plan = self.assert_plan(
            PLANNER,
            "Show commission revenue by branch in 2013 and list the top 5 branches",
            metric="commission_revenue", dims=("Branch",),
            time=("year", 2013), limit=5,
        )
        self.assertEqual(
            plan.order_by, (OrderSpec("commission_revenue", desc=True),)
        )

    def test_en_best_n_phrase_lifts_dimension(self) -> None:
        """best/top N 后短语提维度（top 3 categories by sales → i_category）。"""
        plan = self.assert_plan(
            RETAIL_PLANNER, "What were the top 3 categories by sales in 1999?",
            metric="total_sales_price", dims=("i_category",),
            time=("year", 1999), limit=3,
        )
        self.assertEqual(plan.order_by, (OrderSpec("total_sales_price", desc=True),))

    # -- 过滤（2+，only/excluding/修饰反问）-------------------------------

    def test_en_only_for_branch(self) -> None:
        self.assert_plan(
            PLANNER,
            f"What was the commission revenue only for branch {self.BRANCH_X} in 2013?",
            metric="commission_revenue", time=("year", 2013),
            filters=(Filter("Branch", "=", self.BRANCH_X),),
        )

    def test_en_only_entity_suffix_cropped(self) -> None:
        """值后裸实体复数词作后缀裁剪：only for tier 3 customers → Tier=3。"""
        self.assert_plan(
            PLANNER,
            "Show commission revenue by branch in 2014, only for tier 3 customers, "
            "list the top 5",
            metric="commission_revenue", dims=("Branch",), time=("year", 2014),
            filters=(Filter("Tier", "=", 3),), limit=5,
        )

    def test_en_excluding_branch(self) -> None:
        self.assert_plan(
            PLANNER,
            "What was the total trade quantity excluding branch "
            f"{self.BRANCH_X} in 2013?",
            metric="total_trade_quantity", time=("year", 2013),
            filters=(Filter("Branch", "!=", self.BRANCH_X),),
        )

    def test_en_only_qualified_value_clarifies(self) -> None:
        """only the main branch：维度词前带修饰 → 反问（与中文 gold-155 同构）。"""
        result = PLANNER.plan(
            "What was the commission revenue only for the main branch in 2013?",
            locale="en",
        )
        self.assertIsInstance(result, ClarificationRequest)

    def test_en_emphasis_only_no_filter(self) -> None:
        """强调语 only（短语无维度词）不产生 filter（与中文"只看"同构）。"""
        plan = PLANNER.plan("Only show commission revenue in 2013", locale="en")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(plan.filters, ())

    # -- 阈值（2+）-------------------------------------------------------

    def test_en_threshold_over_million(self) -> None:
        """over 10 million → 10000000（量级词换算，HAVING 语义）。"""
        self.assert_plan(
            PLANNER,
            "List the top 5 branches by commission revenue over 10 million in 2013",
            metric="commission_revenue", dims=("Branch",), time=("year", 2013),
            filters=(Filter("commission_revenue", ">", 10000000),), limit=5,
        )

    def test_en_threshold_under_million(self) -> None:
        self.assert_plan(
            PLANNER,
            "commission revenue by branch under 5 million in 2014, top 5 branches",
            metric="commission_revenue", dims=("Branch",), time=("year", 2014),
            filters=(Filter("commission_revenue", "<", 5000000),), limit=5,
        )

    def test_en_threshold_4digit_not_read_as_year(self) -> None:
        """4 位阈值数字不误读成年份：over 5000 in 2000 → year=2000 且 filter>5000。"""
        self.assert_plan(
            RETAIL_PLANNER, "Show sales by category over 5000 in 2000",
            metric="total_sales_price", dims=("i_category",), time=("year", 2000),
            filters=(Filter("total_sales_price", ">", 5000),),
        )

    def test_en_bare_year_without_threshold_word(self) -> None:
        """无介词裸年兑底：句内无阈值词时 1999 可作年份。"""
        self.assert_plan(
            RETAIL_PLANNER, "What were total sales 1999?",
            metric="total_sales_price", time=("year", 1999),
        )

    # -- 相对时间反问（2）------------------------------------------------

    def test_en_relative_time_clarifies(self) -> None:
        result = PLANNER.plan(
            "What was the commission revenue last month?", locale="en"
        )
        self.assertIsInstance(result, ClarificationRequest)
        assert isinstance(result, ClarificationRequest)
        self.assertEqual(result.kind, "relative_time")

    def test_en_recent_clarifies(self) -> None:
        result = RETAIL_PLANNER.plan("How were total sales recently?", locale="en")
        self.assertIsInstance(result, ClarificationRequest)
        assert isinstance(result, ClarificationRequest)
        self.assertEqual(result.kind, "relative_time")

    # -- 指代追问 en（4）-------------------------------------------------

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
        filters=(Filter("Branch", "=", BRANCH_X),),
    )

    def test_en_followup_what_about_time(self) -> None:
        """what about 2014? → 同 metric/维度/排序，仅换时间。"""
        merged = PLANNER.followup("what about 2014?", self.PREV, locale="en")
        self.assertIsInstance(merged, Plan)
        assert isinstance(merged, Plan)
        self.assertEqual(merged.time, TimeSpec("year", 2014))
        self.assertEqual(merged.dimensions, ("Branch",))
        self.assertEqual(merged.limit, 5)

    def test_en_followup_how_about_dimension(self) -> None:
        """how about by tier? → 替换分组维度（时间保持）。"""
        merged = PLANNER.followup("how about by tier?", self.PREV, locale="en")
        self.assertIsInstance(merged, Plan)
        assert isinstance(merged, Plan)
        self.assertEqual(merged.dimensions, ("Tier",))
        self.assertEqual(merged.time, TimeSpec("year", 2013))

    def test_en_followup_instead_swap(self) -> None:
        """by office instead? → 换维壳 instead 形态。"""
        merged = PLANNER.followup("by office instead?", self.PREV, locale="en")
        self.assertIsInstance(merged, Plan)
        assert isinstance(merged, Plan)
        self.assertEqual(merged.dimensions, ("Office",))

    def test_en_followup_relative_clarifies(self) -> None:
        """what about last year? → 透传 relative_time 反问。"""
        result = PLANNER.followup("what about last year?", self.PREV, locale="en")
        self.assertIsInstance(result, ClarificationRequest)
        assert isinstance(result, ClarificationRequest)
        self.assertEqual(result.kind, "relative_time")

    def test_en_followup_free_pronoun_clarifies(self) -> None:
        """what about those?（自由代词）→ 反问不猜。"""
        result = PLANNER.followup("what about those?", self.PREV, locale="en")
        self.assertIsInstance(result, ClarificationRequest)

    def test_en_followup_no_shell_returns_none(self) -> None:
        self.assertIsNone(PLANNER.followup("Show me trades", self.PREV, locale="en"))

    def test_en_followup_change_dim_with_prev_filter_clarifies(self) -> None:
        """换维 + 上轮带维度值过滤 → 作用域二义反问（与中文同构）。"""
        result = PLANNER.followup(
            "what about by tier?", self.PREV_FILTER, locale="en"
        )
        self.assertIsInstance(result, ClarificationRequest)

    # -- 歧义仍反问（2）/ 双语隔离（2）/ 防漂移（1）------------------------

    def test_en_dual_metric_ambiguity_clarifies(self) -> None:
        result = PLANNER.plan(
            "What were the trade value and commission revenue in 2013?", locale="en"
        )
        self.assertIsInstance(result, ClarificationRequest)
        assert isinstance(result, ClarificationRequest)
        self.assertIn("指标口径歧义", result.reasons[0])

    def test_en_unmatched_clarifies(self) -> None:
        result = PLANNER.plan("What is the weather today?", locale="en")
        self.assertIsInstance(result, ClarificationRequest)
        assert isinstance(result, ClarificationRequest)
        self.assertEqual(result.kind, "unmatched")

    def test_auto_detection_zh_en(self) -> None:
        """无 locale → 中文字符启发式：纯英文问句自动 en，中文问句自动 zh。"""
        plan_en = PLANNER.plan("What was the total trade value in 2015?")
        self.assertIsInstance(plan_en, Plan)
        assert isinstance(plan_en, Plan)
        self.assertEqual(plan_en.metric, "total_trade_value")
        plan_zh = PLANNER.plan("2015 年总交易额是多少？")
        self.assertIsInstance(plan_zh, Plan)
        assert isinstance(plan_zh, Plan)
        self.assertEqual(plan_zh.metric, "total_trade_value")

    def test_zh_never_uses_en_forms(self) -> None:
        """双语隔离：中文问句带英文值词不误入 en 形态；英文句显式 zh 不命中。"""
        plan = PLANNER.plan("只看分支 IEMJHuQgCPDHCwwJkgQQeaqGvzMcVD 的 2013 年佣金收入是多少？")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)
        self.assertEqual(
            plan.filters, (Filter("Branch", "=", self.BRANCH_X),)
        )
        result = PLANNER.plan("What was the total trade value in 2015?", locale="zh")
        self.assertIsInstance(result, ClarificationRequest)
        assert isinstance(result, ClarificationRequest)
        self.assertEqual(result.kind, "unmatched")

    def test_en_synonym_keys_within_model(self) -> None:
        """英文同义词表（已外置 en_us.yml，ADR-0015）防漂移三锁：

        1. 条数基线：17 个指标 + 15 个维度字段（与外置前代码常量逐字对应，
           任何新增/删除都需连同评测复验，不默许漂）；
        2. 键必须在语义模型（金融 ∪ 零售）已注册名字集内（YAML 改名即报红）；
        3. 措辞不得与模型注记重叠（重叠=双源）；唯一例外是中英同形的
           缩写词 AOV（模型注记与本表都有，外置前已如此，不顺手改）。
        """
        known_overlap = {("avg_order_value", "AOV")}
        en = load_locale_synonyms("en_us")
        metrics, dims = en["metric_synonyms"], en["dimension_synonyms"]
        self.assertEqual(len(metrics), 17, "英文指标表条数基线")
        self.assertEqual(len(dims), 15, "英文维度表条数基线")

        finance_keys = set(MODEL.metric_synonyms) | set(MODEL.dimension_synonyms)
        retail_keys = (
            set(RETAIL_MODEL.metric_synonyms) | set(RETAIL_MODEL.dimension_synonyms)
        )
        model_words = {
            w
            for syns in list(MODEL.metric_synonyms.values())
            + list(MODEL.dimension_synonyms.values())
            + list(RETAIL_MODEL.metric_synonyms.values())
            + list(RETAIL_MODEL.dimension_synonyms.values())
            for w in syns
        }
        for name, syns in {**metrics, **dims}.items():
            self.assertIn(name, finance_keys | retail_keys, f"{name} 未注册于任何模型")
            for word in syns:
                if (name, word) in known_overlap:
                    continue
                self.assertNotIn(word, model_words, f"{name}: {word!r} 与模型注记重叠")
        # 零售键必须挂在零售模型（金融键挂在金融模型）
        for name in ("total_sales_price", "total_quantity", "net_profit",
                     "avg_order_value", "order_count"):
            self.assertIn(name, RETAIL_MODEL.metric_synonyms)
        for name in ("i_category", "i_brand", "s_state", "s_city"):
            self.assertIn(name, RETAIL_MODEL.dimension_synonyms)

    def test_locale_synonym_files_registered(self) -> None:
        """locale 注册表封闭：zh 表恒空（中文注记的唯一权威源是模型）。"""
        zh = load_locale_synonyms("zh_cn")
        self.assertEqual(zh["metric_synonyms"], {})
        self.assertEqual(zh["dimension_synonyms"], {})
        # zh 轨不并入英文表（防中文问句里的英文片段误命中）
        with self.assertRaises(ValueError):
            load_locale_synonyms("fr_fr")

    def test_invalid_locale_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PLANNER.plan("whatever", locale="fr")


class TestEnglishGoldPlanner(unittest.TestCase):
    """英文黄金集（P6 双语样本 13 条，2026-09-05）Plan 解析对照。

    全部为既有中文锚定样本的机械直译（同 Plan → 同 SQL → 同 result_hash
    可预期）：金融 gold-163~170（←123/101/101/115/114/102/149/151）与
    零售 gold-063~067（←051/056/058/061/059，年份 1999）。评测 runner 按
    tags lang_en 显式传 locale="en"，此处与 runner 同口径对照 load_gold
    标注（Plan Acc 评测载体，与 TestRetailGoldPlanner 中文侧对称）。
    """

    def assert_plan(self, planner: Planner, question: str, gold_id: str) -> Plan:
        """locale=en 解析并与 gold 标注逐项对照（口径同 eval/runner.plan_acc）。"""
        gold = load_gold(gold_id)
        plan = planner.plan(question, locale="en")
        self.assertIsInstance(plan, Plan)
        assert isinstance(plan, Plan)  # 供类型收窄（unittest 断言不改变类型）
        self.assertEqual(plan.metric, gold["expected_metric"])
        self.assertEqual(plan.dimensions, tuple(gold["expected_dimensions"]))
        expected_time = gold.get("expected_time")
        if expected_time is None:
            self.assertIsNone(plan.time)
        else:
            self.assertIsNotNone(plan.time)
            assert plan.time is not None
            self.assertEqual(str(plan.time.value), expected_time)
        expected_filters = gold.get("expected_filters")
        if expected_filters is not None:
            actual = [
                {"column": f.column, "op": f.op, "value": f.value}
                for f in plan.filters
            ]
            self.assertEqual(actual, expected_filters)
        return plan

    # -- 金融 8 条（gold-163~170）----------------------------------------

    def test_gold_163_year_total_trade_value(self) -> None:
        self.assert_plan(
            PLANNER, "What was the total trade value in 2015?", "gold-163"
        )

    def test_gold_164_quarter_q_before_year(self) -> None:
        """Q2 2013 语序（←gold-101 中文样本直译）。"""
        self.assert_plan(
            PLANNER, "What was the total trade value in Q2 2013?", "gold-164"
        )

    def test_gold_165_quarter_year_before_q(self) -> None:
        """2013 Q2 语序两向与 164 同值（expected_time 均为 2013Q2）。"""
        self.assert_plan(
            PLANNER, "What was the total trade value in 2013 Q2?", "gold-165"
        )

    def test_gold_166_month_name_trade_count(self) -> None:
        self.assert_plan(
            PLANNER, "How many trades were there in May 2014?", "gold-166"
        )

    def test_gold_167_iso_date_cash_balance(self) -> None:
        self.assert_plan(
            PLANNER, "What was the total cash balance on 2014-03-31?", "gold-167"
        )

    def test_gold_168_commission_by_branch_top5(self) -> None:
        plan = self.assert_plan(
            PLANNER,
            "Show commission revenue by branch in 2013 and list the top 5 branches",
            "gold-168",
        )
        self.assertEqual(
            plan.order_by, (OrderSpec("commission_revenue", desc=True),)
        )
        self.assertEqual(plan.limit, 5)

    def test_gold_169_only_for_branch(self) -> None:
        plan = self.assert_plan(
            PLANNER,
            "What was the commission revenue only for branch "
            "IEMJHuQgCPDHCwwJkgQQeaqGvzMcVD in 2013?",
            "gold-169",
        )
        self.assertEqual(
            plan.filters, (Filter("Branch", "=", "IEMJHuQgCPDHCwwJkgQQeaqGvzMcVD"),)
        )

    def test_gold_170_top5_over_threshold(self) -> None:
        self.assert_plan(
            PLANNER,
            "List the top 5 branches by commission revenue over 10 million in 2013",
            "gold-170",
        )

    # -- 零售 5 条（gold-063~067，SF0.1 窗口 1999~2002 实测值域）---------

    def test_gold_063_year_total_sales(self) -> None:
        self.assert_plan(
            RETAIL_PLANNER, "What were total sales in 1999?", "gold-063"
        )

    def test_gold_064_year_by_category(self) -> None:
        self.assert_plan(
            RETAIL_PLANNER,
            "What were the total sales by category in 1999?", "gold-064"
        )

    def test_gold_065_top3_categories(self) -> None:
        plan = self.assert_plan(
            RETAIL_PLANNER,
            "What were the top 3 categories by sales in 1999?", "gold-065"
        )
        self.assertEqual(plan.limit, 3)

    def test_gold_066_quarter_q1_1999(self) -> None:
        self.assert_plan(
            RETAIL_PLANNER, "What were the total sales in Q1 1999?", "gold-066"
        )

    def test_gold_067_only_for_city(self) -> None:
        """only for city Midway → s_city 等值（SF0.1 无 CA 州，降级城市值域）。"""
        plan = self.assert_plan(
            RETAIL_PLANNER,
            "What was the total sales only for city Midway in 1999?", "gold-067"
        )
        self.assertEqual(plan.filters, (Filter("s_city", "=", "Midway"),))


class TestPlannerValueDomain(unittest.TestCase):
    """planner 值域校验集成（ADR-0016 §②）：用 tmp fixture 注入值域，验证归一/澄清。

    真实仓库测试：若 semantic/values/ 已生成（B4 产物已入库），gold-171/gold-068
    必须通过；否则跳过（完整性检查在 tests/test_value_domain.py）。
    """

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        # ExchangeID: NASDAQ, NYSE, AMEX, PCX + 别名 NSDQ → NASDAQ
        self._write(
            "atlas_finance_analytics.ExchangeID.json",
            {
                "model": "atlas_finance_analytics",
                "field": "ExchangeID",
                "status": "registered",
                "snapshot_sha": "abc1234",
                "generated_at": "2026-09-09T12:00:00+08:00",
                "bound_dataset": "dim_security",
                "source_table": "atlas.dwd.dim_security",
                "source_column": "ExchangeID",
                "distinct_count": 4,
                "row_count": 1000,
                "null_count": 0,
                "max_cardinality": 200,
                "skip_reason": None,
                "values": [
                    {"value": "NASDAQ", "count": 100},
                    {"value": "NYSE", "count": 90},
                    {"value": "AMEX", "count": 50},
                    {"value": "PCX", "count": 30},
                ],
                "aliases": {"nsdq": "NASDAQ"},
                "note": "",
            },
        )
        # s_city: Midway, Fairview（用于 case 归一）
        self._write(
            "atlas_retail_analytics.s_city.json",
            {
                "model": "atlas_retail_analytics",
                "field": "s_city",
                "status": "registered",
                "snapshot_sha": "abc1234",
                "generated_at": "2026-09-09T12:00:00+08:00",
                "bound_dataset": "dim_store",
                "source_table": "atlas.dwd.dim_store",
                "source_column": "s_city",
                "distinct_count": 2,
                "row_count": 100,
                "null_count": 0,
                "max_cardinality": 200,
                "skip_reason": None,
                "values": [{"value": "Midway", "count": 200}, {"value": "Fairview", "count": 100}],
                "aliases": {},
                "note": "",
            },
        )
        # i_category: 10 个真实值（用于 unknown 反问）
        self._write(
            "atlas_retail_analytics.i_category.json",
            {
                "model": "atlas_retail_analytics",
                "field": "i_category",
                "status": "registered",
                "snapshot_sha": "abc1234",
                "generated_at": "2026-09-09T12:00:00+08:00",
                "bound_dataset": "dim_item",
                "source_table": "atlas.dwd.dim_item",
                "source_column": "i_category",
                "distinct_count": 10,
                "row_count": 1000,
                "null_count": 0,
                "max_cardinality": 200,
                "skip_reason": None,
                "values": [
                    {"value": "Books", "count": 100},
                    {"value": "Sports", "count": 90},
                    {"value": "Music", "count": 80},
                    {"value": "Electronics", "count": 70},
                    {"value": "Home", "count": 60},
                    {"value": "Women", "count": 50},
                    {"value": "Men", "count": 40},
                    {"value": "Children", "count": 30},
                    {"value": "Shoes", "count": 20},
                    {"value": "Jewelry", "count": 10},
                ],
                "aliases": {},
                "note": "",
            },
        )
        # Branch: skipped（高基数，不校验）
        self._write(
            "atlas_finance_analytics.Branch.json",
            {
                "model": "atlas_finance_analytics",
                "field": "Branch",
                "status": "skipped",
                "snapshot_sha": "abc1234",
                "generated_at": "2026-09-09T12:00:00+08:00",
                "bound_dataset": "dim_broker",
                "source_table": "atlas.dwd.dim_broker",
                "source_column": "Branch",
                "distinct_count": 2715,
                "row_count": 5000,
                "null_count": 0,
                "max_cardinality": 200,
                "skip_reason": "distinct=2715 超过阈值 200",
                "values": [],
                "aliases": {},
                "note": "",
            },
        )
        self.finance_planner = Planner(MODEL, values_dir=self.dir)
        self.retail_planner = Planner(RETAIL_MODEL, values_dir=self.dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, filename: str, payload: dict) -> None:
        path = self.dir / filename
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_alias_normalization_with_notice(self) -> None:
        """gold-171：NSDQ → NASDAQ（别名归一 + ValueNotice）。"""
        from agent.planner import ValueNotice
        result, notices = self.finance_planner.plan_with_notices(
            "只看交易所 NSDQ 的 2013 年佣金收入是多少？"
        )
        self.assertIsInstance(result, Plan)
        assert isinstance(result, Plan)
        self.assertEqual(result.filters, (Filter("ExchangeID", "=", "NASDAQ"),))
        self.assertEqual(len(notices), 1)
        self.assertIsInstance(notices[0], ValueNotice)
        self.assertEqual(notices[0].field, "ExchangeID")
        self.assertEqual(notices[0].raw, "NSDQ")
        self.assertEqual(notices[0].value, "NASDAQ")
        self.assertEqual(notices[0].kind, "alias")

    def test_unknown_value_clarification(self) -> None:
        """gold-068：Toys 不在 i_category 值域 → 反问并附候选值样例。"""
        result = self.retail_planner.plan("只看类别 Toys 的 1999 年销售额是多少？")
        self.assertIsInstance(result, ClarificationRequest)
        assert isinstance(result, ClarificationRequest)
        self.assertIn("Toys", result.reasons[0])
        self.assertIn("i_category", result.reasons[0])
        self.assertGreater(len(result.candidates), 0)

    def test_case_normalization(self) -> None:
        """MIDWAY → Midway（case 归一 + ValueNotice）。"""
        from agent.planner import ValueNotice
        result, notices = self.retail_planner.plan_with_notices(
            "只看城市 MIDWAY 的 2000 年销售额是多少？"
        )
        self.assertIsInstance(result, Plan)
        assert isinstance(result, Plan)
        self.assertEqual(result.filters, (Filter("s_city", "=", "Midway"),))
        self.assertEqual(len(notices), 1)
        self.assertIsInstance(notices[0], ValueNotice)
        self.assertEqual(notices[0].raw, "MIDWAY")
        self.assertEqual(notices[0].value, "Midway")
        self.assertEqual(notices[0].kind, "case")

    def test_skipped_column_passthrough(self) -> None:
        """Branch 高基数跳过：planner 不做值校验，原值透传。"""
        result = self.finance_planner.plan("只看分支 HQ 的 2013 年佣金收入是多少？")
        self.assertIsInstance(result, Plan)
        assert isinstance(result, Plan)
        self.assertEqual(result.filters, (Filter("Branch", "=", "HQ"),))

    def test_metric_threshold_unaffected(self) -> None:
        """度量阈值（"超过 1000 万"）不参与维度值域校验，原样通过。"""
        result = self.finance_planner.plan("2013 年佣金收入超过 1000 万的账户有多少？")
        self.assertIsInstance(result, Plan)
        assert isinstance(result, Plan)
        self.assertEqual(result.filters, (Filter("commission_revenue", ">", 10000000),))


if __name__ == "__main__":
    unittest.main()
