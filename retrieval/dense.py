"""稠密向量检索（CPU 可用，零强制依赖；AGENTS.md Medium6）。

设计
----
- **无 GPU 也可运行**：默认用哈希词袋（hashing trick）把 token 序列投影到固定
  维向量并做余弦相似，纯 Python、确定性、可复现——作为稠密召回的兜底路。
- **可选真实 embedding**：若环境装了 sentence-transformers（CPU 推理，无需 GPU），
  设 ``model_name`` 即启用轻量句向量（如 BAAI/bge-small-zh），召回质量更高；
  缺失则自动回落哈希路（lazy import，不污染核心依赖）。
- 与词法路（retrieval/bm25.py）量纲不同，融合走 retrieval/fusion.py 的 RRF
  （HybridRetriever 封装），不依赖原始分数绝对值。

已知边界（诚实）
--------------
- 哈希路是弱语义（同义词不同 token 仍可能串扰，如 bm25 已注记的「佣金/手续费」）；
  它是「有稠密召回」的占位实现，真实语义召回需 sentence-transformers 路。
- 不引入额外依赖：sentence-transformers 为可选能力，未列入 pyproject 核心依赖。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from retrieval.bm25 import tokenize

DEFAULT_DIM = 512


@dataclass(frozen=True)
class DenseHit:
    """稠密召回命中（与 Bm25Index.Hit 同形态，便于 RRF 融合）。"""

    doc_id: str
    score: float


class DenseIndex:
    """内存稠密索引：add 建向量，search 返回 top-k（余弦降序）。

    用法（与 Bm25Index 同接口）::

        idx = DenseIndex()                       # 默认哈希路（CPU、零依赖）
        idx = DenseIndex(model_name="BAAI/bge-small-zh")  # 真实句向量（需 sentence-transformers）
        for doc_id, text in docs:
            idx.add(doc_id, text)
        hits = idx.search("2013 年佣金收入", k=5)
    """

    def __init__(self, dim: int = DEFAULT_DIM, model_name: str | None = None) -> None:
        self._dim = dim
        self._docs: list[str] = []
        self._vecs: list[list[float]] = []
        self._model = self._load_model(model_name) if model_name else None
        self._model_name = model_name

    @staticmethod
    def _load_model(name: str):
        """lazy import sentence-transformers；缺失即回落由调用方处理。"""
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except Exception as exc:  # noqa: BLE001 - 可选依赖缺失，由 search 回落哈希路
            raise RuntimeError(
                f"sentence-transformers 未安装，无法加载 {name!r}（回落哈希路请用 model_name=None）"
            ) from exc
        return SentenceTransformer(name)

    # -- 嵌入 ----------------------------------------------------------
    def _embed(self, text: str) -> list[float]:
        if self._model is not None:
            vec = self._model.encode(text, normalize_embeddings=True)
            return [float(x) for x in vec]
        return self._hash_embed(text)

    def _hash_embed(self, text: str) -> list[float]:
        """哈希词袋嵌入：token → dim 上的带符号累加（tf 加权），L2 归一。

        确定性、CPU、零依赖；向量方向近似反映词重叠（弱语义，非句向量）。
        """
        vec = [0.0] * self._dim
        tokens = tokenize(text)
        if not tokens:
            return vec
        tf: dict[int, int] = {}
        for tok in tokens:
            h = int.from_bytes(hashlib.md5(tok.encode("utf-8")).digest()[:8], "big") % self._dim
            tf[h] = tf.get(h, 0) + 1
        for h, c in tf.items():
            # 符号位由 token 哈希次高位决定，制造正负方向（近似语义正负）
            sign = (
                1.0
                if (int.from_bytes(hashlib.md5(tok.encode("utf-8")).digest()[8:9], "big") & 1)
                else -1.0
            )
            vec[h] += sign * math.log(1.0 + c)
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]

    # -- 接口 ----------------------------------------------------------
    def add(self, doc_id: str, text: str) -> None:
        self._docs.append(doc_id)
        self._vecs.append(self._embed(text))

    def search(self, query: str, k: int = 5) -> list[DenseHit]:
        if not self._docs or k <= 0:
            return []
        q = self._embed(query)
        if all(v == 0.0 for v in q):
            return []
        scored: list[tuple[int, float]] = []
        for i, v in enumerate(self._vecs):
            dot = sum(a * b for a, b in zip(q, v, strict=False))
            scored.append((i, dot))
        scored.sort(key=lambda kv: -kv[1])
        return [DenseHit(self._docs[i], round(s, 6)) for i, s in scored[:k]]
