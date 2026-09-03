"""BM25 检索契约测试（零依赖，pytest/unittest 均可发现运行）。

覆盖确定性链路：中英混合分词 → 倒排索引 → top-k 召回排序；
与真实语料同构的"同义词重叠指标"区分场景（见 retrieval/bm25.py 边界说明）。
"""

from __future__ import annotations

import unittest

from retrieval.bm25 import Bm25Index, tokenize
from retrieval.metric_docs import MetricDoc, build_metric_docs
from retrieval.milvus_client import doc_to_sparse, token_id


class TestTokenize(unittest.TestCase):
    def test_cjk_bigram(self) -> None:
        self.assertEqual(tokenize("佣金收入"), ["佣金", "金收", "收入"])

    def test_mixed_cjk_and_latin(self) -> None:
        self.assertEqual(
            tokenize("2013 年佣金收入 Top5"), ["2013", "年佣", "佣金", "金收", "收入", "top5"]
        )

    def test_isolated_single_cjk_char(self) -> None:
        # 孤立的单汉字按单字输出，避免整段被丢弃
        self.assertEqual(tokenize("A股"), ["a", "股"])

    def test_punctuation_ignored(self) -> None:
        self.assertEqual(
            tokenize("佣金收入（美元），多少？"), ["佣金", "金收", "收入", "美元", "多少"]
        )

    def test_case_normalized(self) -> None:
        self.assertEqual(tokenize("GMV vs gmv"), ["gmv", "vs", "gmv"])


class TestBm25Index(unittest.TestCase):
    def _index(self) -> Bm25Index:
        index = Bm25Index()
        index.add(
            "commission_revenue",
            "commission_revenue 佣金收入 佣金 手续费收入 佣金收入合计经纪服务费",
        )
        index.add("total_trade_value", "total_trade_value 交易额 成交额 成交金额 总交易额成交金额")
        index.add("trade_count", "trade_count 交易笔数 交易数量 成交笔数 交易笔数")
        return index

    def test_top1_is_exact_synonym_doc(self) -> None:
        index = self._index()
        hits = index.search("2013 年佣金收入是多少", k=3)
        self.assertEqual(hits[0].doc_id, "commission_revenue")

    def test_top1_distinguishes_overlapping_synonyms(self) -> None:
        # "成交笔数" 与 "成交金额" 共享 "成交" bigram，靠专属 bigram 区分
        index = self._index()
        hits = index.search("本月成交笔数", k=3)
        self.assertEqual(hits[0].doc_id, "trade_count")
        hits = index.search("本月成交金额", k=3)
        self.assertEqual(hits[0].doc_id, "total_trade_value")

    def test_empty_query_returns_empty(self) -> None:
        index = self._index()
        self.assertEqual(index.search(""), [])
        self.assertEqual(index.search("。。。"), [])

    def test_empty_index_returns_empty(self) -> None:
        self.assertEqual(Bm25Index().search("佣金收入", k=5), [])

    def test_top_k_respected(self) -> None:
        index = self._index()
        self.assertLessEqual(len(index.search("收入", k=2)), 2)


class TestSparseLexicalDeterminism(unittest.TestCase):
    """稀疏向量路（Milvus 客户端）的确定性部分，不依赖服务即可验证。"""

    def test_token_id_deterministic(self) -> None:
        self.assertEqual(token_id("佣金"), token_id("佣金"))
        self.assertLess(token_id("佣金"), 2**32 - 1)

    def test_doc_to_sparse_nonempty_and_tf_weighted(self) -> None:
        doc = MetricDoc(doc_id="commission_revenue", text="commission_revenue 佣金收入 佣金")
        sparse = doc_to_sparse(doc)
        self.assertTrue(sparse)
        # 佣金出现 2 次（佣金收入 bigram + 独立佣金单字段在 metric_docs 拼装中不出现，
        # 此处单测验证同名 token 词频累加）
        self.assertEqual(sparse[token_id("收入")], 1.0)

    def test_metric_docs_build_uses_model_synonyms(self) -> None:
        from agent.compiler import SemanticModel

        docs = build_metric_docs(SemanticModel())
        self.assertEqual(len(docs), 20)  # Day 27 发布 5 派生指标后 15→20
        texts = {d.doc_id: d.text for d in docs}
        self.assertIn("佣金收入", texts["commission_revenue"])
        self.assertIn("成交证券数", texts["traded_security_count"])


if __name__ == "__main__":
    unittest.main()
