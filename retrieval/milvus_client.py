"""Milvus 向量召回客户端（MVP：确定性词法稀疏向量，无嵌入模型）。

设计决策（与 bm25.py 同一确定性基线）
------------------------------------
- **向量 = 词法稀疏向量**：文档 token（CJK bigram + 英文词，同 bm25.tokenize）
  的 tf 权重映射为 {token_id: weight}，token_id = md5(token) 截断的整数。
  md5 保证跨进程/跨机器确定性（不依赖 Python hash 随机盐）。
  不引入嵌入模型（新依赖需 ADR + 预算评估，且违背确定性优先）。
- **承载**：Milvus 2.4 standalone（compose 服务 atlas-milvus），
  SPARSE_INVERTED_INDEX + IP。召回结果与 BM25 同源不同路，
  供 Day 23 RRF 融合评测（retrieval/graph_store.py 之后）。
- 集合按指标语义命名，写入前 drop 重建（语料以语义层为唯一事实源，
  幂等重建比增量同步更简单可靠，语料 ≤ 数百篇）。

已知边界（诚实声明）
--------------------
- 词法向量不编码语义相似（"佣金"与"手续费"不同 token）；语义嵌入
  列入 Known Limitations，待 LoRA/嵌入模型阶段评估（README 12 节）。
- 服务不可用时抛 MilvusUnavailable，由调用方决定降级（仅 BM25 路）。
"""

from __future__ import annotations

import contextlib
import hashlib
from dataclasses import dataclass
from typing import Any

from retrieval.bm25 import tokenize
from retrieval.metric_docs import MetricDoc

# 集合与索引约定（Milvus 2.4：SPARSE_INVERTED_INDEX 需 IP metric）
COLLECTION = "atlas_metric_sparse"
INDEX_PARAMS: dict[str, Any] = {
    "index_type": "SPARSE_INVERTED_INDEX",
    "metric_type": "IP",
}


class MilvusUnavailable(RuntimeError):
    """Milvus 服务不可达或协议不兼容（调用方捕获后走 BM25 单路降级）。"""


def token_id(token: str) -> int:
    """token → 确定性稀疏维度 id（md5 前 4 字节；须 < 2^32-1，Milvus 稀疏上限）。"""
    digest = hashlib.md5(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def doc_to_sparse(doc: MetricDoc) -> dict[int, float]:
    """指标文档 → 稀疏向量（词频权重，token 与 BM25 同一分词口径）。"""
    weights: dict[int, float] = {}
    for token in tokenize(doc.text):
        tid = token_id(token)
        weights[tid] = weights.get(tid, 0.0) + 1.0
    return weights


def query_to_sparse(query: str) -> dict[int, float]:
    """问句 → 稀疏向量（去重后词频 1，与检索一致性优于长文本频次放大）。"""
    return {token_id(token): 1.0 for token in dict.fromkeys(tokenize(query))}


@dataclass(frozen=True)
class VectorHit:
    """向量召回命中文档（doc_id 与相似度，供 RRF 与评测计数）。"""

    doc_id: str
    score: float


class MilvusIndex:
    """Milvus 稀疏向量索引封装（写入 + top-k 召回）。

    用法::

        with MilvusIndex(uri="http://localhost:19530") as index:
            index.rebuild(docs)              # 幂等重建
            hits = index.search("2013 年佣金收入", k=5)
    """

    def __init__(self, uri: str = "http://localhost:19530") -> None:
        self.uri = uri
        self._client: Any = None

    def _connect(self) -> Any:
        if self._client is None:
            try:
                from pymilvus import MilvusClient

                self._client = MilvusClient(uri=self.uri, timeout=15)
            except Exception as exc:  # 连接失败/缺依赖：统一转 MilvusUnavailable
                raise MilvusUnavailable(f"Milvus 连接失败（{self.uri}）：{exc}") from exc
        return self._client

    def rebuild(self, docs: list[MetricDoc]) -> None:
        """drop + 重建集合并写入全部文档（语料幂等重建，见模块 docstring）。"""
        client = self._connect()
        try:
            from pymilvus import DataType, MilvusClient, connections, utility

            client.drop_collection(COLLECTION)
            schema = MilvusClient.create_schema(auto_id=False)
            schema.add_field("doc_id", DataType.VARCHAR, is_primary=True, max_length=128)
            schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
            index_params = MilvusClient.prepare_index_params()
            index_params.add_index("sparse", **INDEX_PARAMS)
            client.create_collection(COLLECTION, schema=schema, index_params=index_params)
            rows = [
                {"doc_id": doc.doc_id, "sparse": doc_to_sparse(doc)}
                for doc in docs
                if doc_to_sparse(doc)
            ]
            if rows:
                client.insert(COLLECTION, rows)
            # insert 后数据停在 growing segment，未 flush 前 querynode 不可见（实测
            # 延迟达数十秒）；flush_all 强制落盘后 row_count 与检索立即就绪。
            # utility API 走 pymilvus.connections 的默认连接，与 MilvusClient 独立，
            # 需显式 connect（幂等，重复 connect 同 alias 会更新配置）。
            connections.connect(alias="default", uri=self.uri, timeout=15)
            utility.flush_all(use_alias="default")
        except MilvusUnavailable:
            raise
        except Exception as exc:
            raise MilvusUnavailable(f"Milvus 写入失败：{exc}") from exc

    def search(self, query: str, k: int = 5) -> list[VectorHit]:
        """query → top-k VectorHit（相似度降序）。"""
        client = self._connect()
        try:
            res = client.search(
                COLLECTION,
                data=[query_to_sparse(query)],
                limit=k,
                output_fields=["doc_id"],
            )
            hits: list[VectorHit] = []
            for row in res[0]:
                hits.append(VectorHit(str(row["entity"]["doc_id"]), float(row["distance"])))
            return hits
        except MilvusUnavailable:
            raise
        except Exception as exc:
            raise MilvusUnavailable(f"Milvus 检索失败：{exc}") from exc

    def close(self) -> None:
        """释放客户端连接（幂等）。"""
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def __enter__(self) -> MilvusIndex:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
