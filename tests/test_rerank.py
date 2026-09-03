"""元数据 Rerank 契约测试（Day 24：同义词置信度 / 留一热度 / owner 优先级）。

关键约定：
- 排序为词典序分层 (synonym, popularity, owner, 原召回序)，规则固定不调参；
  查询相关信号（同义词）做主键，全局信号（热度/owner）只做同层裁决——
  加权线性混合版本实测会把恒在的热门指标系统性推高（Recall@1 34/44），
  词典序是修正后的设计（Recall@1 44/44，见 retrieval/rerank.py docstring）；
- 相互包含的同义词只按字符并集计分（不重复计分）；
- 完全同分保持原召回序，排序完全确定。
"""

from __future__ import annotations

import unittest

from agent.compiler import SemanticModel
from retrieval.rerank import MetaReranker, synonym_confidence

MODEL = SemanticModel()


class TestSynonymConfidence(unittest.TestCase):
    def test_hit_covers_question_chars(self) -> None:
        # “2014 年总成交量是多少？”命中 “成交量”+“总成交量”，并集 4 字
        conf = synonym_confidence("2014 年总成交量是多少？", ("成交量", "总成交量"))
        q_len = 13  # 2014(4) 年(1) 总成交量(4) 是多少(3) ？(1)，仅空白不计
        self.assertAlmostEqual(conf, 4 / q_len)

    def test_no_hit_zero(self) -> None:
        self.assertEqual(synonym_confidence("2016 年平均成交价是多少", ("交易额", "成交额")), 0.0)

    def test_overlapping_synonyms_counted_once(self) -> None:
        conf = synonym_confidence("总成交量", ("成交量", "总成交量"))
        self.assertEqual(conf, 1.0)  # 并集 {成,交,总,量} = 4/4

    def test_empty_synonyms_zero(self) -> None:
        self.assertEqual(synonym_confidence("任何问句", ()), 0.0)


class TestOwnerSignal(unittest.TestCase):
    def test_all_metrics_have_registered_owner(self) -> None:
        """20 指标治理补齐后 owner 全覆盖（无 owner 会在此报警）。"""
        self.assertEqual(len(MODEL.metric_owners), 20)
        for metric in MODEL.metrics:
            self.assertTrue(
                MODEL.metric_owners[metric],
                f"{metric} 缺 owner（治理扩展未补齐）",
            )


class TestRerankSignals(unittest.TestCase):
    def setUp(self) -> None:
        self.reranker = MetaReranker(MODEL, popularity={})

    def test_synonym_signal_outranks_lexical_only(self) -> None:
        """问句直接命中同义词的指标应胜过仅靠词法重叠的候选。"""
        ranked = ["trade_count", "total_trade_quantity"]
        # trade_count 同义词（交易笔数/交易数量/成交笔数）不含 "总成交量"
        top = self.reranker.rerank(ranked, "2014 年总成交量是多少？")
        self.assertEqual(top[0], "total_trade_quantity")

    def test_popularity_breaks_synonym_tie(self) -> None:
        """同义词都不命中（syn 同层）时，热度（留一计数代理）高者靠前。"""
        ranked = ["holdings_value", "cash_balance"]  # 两指标同义词均不命中该问句
        hot = {"cash_balance": 3, "holdings_value": 0}
        reranker = MetaReranker(MODEL, popularity=hot)
        top = reranker.rerank(ranked, "2016 年总成交量是多少？")
        self.assertEqual(top[0], "cash_balance")

    def test_popularity_cannot_override_synonym_evidence(self) -> None:
        """词典序原则：热度再高也不能压过同义词命中的证据（加权版 34/44 教训）。"""
        ranked = ["total_trade_value", "holdings_value"]  # 热门在前（词法靠前）
        hot = {"total_trade_value": 6, "holdings_value": 0}  # 热门 + syn=0
        reranker = MetaReranker(MODEL, popularity=hot)
        # 问句命中 holdings_value 的同义词（“持仓市值”）而不命中 total_trade_value
        top = reranker.rerank(ranked, "2017 年 7 月 7 日全账户持仓市值合计是多少？")
        self.assertEqual(top[0], "holdings_value")

    def test_deterministic_stable_order_on_tie(self) -> None:
        """信号全同时保持原召回序（tiebreak = 原序，排序确定）。"""
        ranked = ["a", "b", "c", "d"]  # 未知 doc：syn/own 均 0，热度 0
        self.assertEqual(self.reranker.rerank(ranked, "任意问句"), ranked)
        self.assertEqual(self.reranker.rerank([], "任意问句"), [])
        self.assertEqual(self.reranker.rerank(["x"], "任意问句"), ["x"])


if __name__ == "__main__":
    unittest.main()
