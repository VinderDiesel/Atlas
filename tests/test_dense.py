"""稠密检索 + 混合融合测试（哈希兜底路，零依赖、可 CPU 运行）。

仅验证兜底哈希路（无 sentence-transformers 时仍可用）；真实句向量路为可选能力，
由 HybridRetriever(dense_model=...) 启用，本测试不假设其存在。
"""

from __future__ import annotations

import unittest

from retrieval.dense import DenseIndex
from retrieval.retriever import HybridRetriever

DOCS = [
    ("m1", "总交易额 成交额 成交金额 股票买卖金额"),
    ("m2", "佣金 手续费 经纪服务费 券商抽成"),
    ("m3", "持仓市值 持仓数量 证券持有规模"),
]


class TestDenseIndex(unittest.TestCase):
    def test_related_doc_ranks_first(self) -> None:
        idx = DenseIndex()
        for doc_id, text in DOCS:
            idx.add(doc_id, text)
        hits = idx.search("佣金收入", k=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0].doc_id, "m2")

    def test_empty_query_returns_empty(self) -> None:
        idx = DenseIndex()
        idx.add("m1", "任意文本")
        self.assertEqual(idx.search("   ", k=3), [])


class TestHybridRetriever(unittest.TestCase):
    def test_bm25_and_dense_fuse_to_relevant(self) -> None:
        retriever = HybridRetriever(DOCS)
        ranked = retriever.search("佣金手续费", k=3)
        self.assertEqual(ranked[0], "m2")

    def test_route_rankings_exposed(self) -> None:
        retriever = HybridRetriever(DOCS)
        routes = retriever.route_rankings("持仓市值", k=3)
        self.assertIn("m3", routes["bm25"])
        self.assertIn("m3", routes["dense"])


if __name__ == "__main__":
    unittest.main()
