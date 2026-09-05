"""混合检索：词法 BM25 + 稠密向量，RRF 融合（AGENTS.md Medium6）。

封装两层索引，对外暴露单一 ``search``：两路各取 top-k 排名，RRF 融合后返回
全局排名。两路量纲不同（BM25 分数 vs 余弦），RRF 只依赖排名，天然兼容。

用法::

    retriever = HybridRetriever(docs)            # docs: list[(doc_id, text)]
    ranked = retriever.search("2013 年佣金收入", k=5)

``docs`` 通常来自 retrieval/metric_docs.py 的指标文档（与现有检索评测同语料）。
"""

from __future__ import annotations

from collections.abc import Sequence

from retrieval.bm25 import Bm25Index
from retrieval.dense import DenseIndex
from retrieval.fusion import rrf


class HybridRetriever:
    """BM25 + 稠密双路 RRF 融合检索器。"""

    def __init__(
        self,
        docs: Sequence[tuple[str, str]],
        dense_model: str | None = None,
        dense_dim: int = 512,
    ) -> None:
        self._bm25 = Bm25Index()
        self._dense = DenseIndex(dim=dense_dim, model_name=dense_model)
        for doc_id, text in docs:
            self._bm25.add(doc_id, text)
            self._dense.add(doc_id, text)

    def search(self, query: str, k: int = 5, route_k: int = 20) -> list[str]:
        """query → 融合排名（doc_id 列表，靠前 = 更相关）。"""
        bm25_rank = [h.doc_id for h in self._bm25.search(query, k=route_k)]
        dense_rank = [h.doc_id for h in self._dense.search(query, k=route_k)]
        return rrf([bm25_rank, dense_rank], k=60)[:k]

    def route_rankings(self, query: str, k: int = 20) -> dict[str, list[str]]:
        """返回各路原始排名（评测/可解释用，不融合）。"""
        return {
            "bm25": [h.doc_id for h in self._bm25.search(query, k=k)],
            "dense": [h.doc_id for h in self._dense.search(query, k=k)],
        }
