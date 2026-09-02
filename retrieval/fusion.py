"""多路召回融合：RRF（Reciprocal Rank Fusion，确定性、无训练参数）。

用法：把各路的 top-k 排名（doc_id 列表，靠前 = 更相关）合并为单一排名::

    merged = rrf([bm25_top20, milvus_top20], k=60)
    top5 = merged[:5]

RRF 分数 = Σ 1/(k + rank)，只依赖排名不依赖原始分数，因此
BM25 与 Milvus(IP 内积)两种量纲不同的分数可以直接融合。
"""

from __future__ import annotations

from collections.abc import Sequence


def rrf(rankings: Sequence[Sequence[str]], k: int = 60) -> list[str]:
    """RRF 融合多路排名。返回全局 doc_id 排名（分数降序，并列按首次出现序）。

    参数
    ----
    rankings : 多路召回列表，每路是 doc_id 排名列表（靠前 = 更相关）。
    k        : RRF 平滑常数（经典取值 60；对排名深度不敏感）。

    返回
    ----
    融合后的 doc_id 排名（与各路内 doc_id 去重无关，doc 按字典累积分数）。
    """
    scores: dict[str, float] = {}
    order: dict[str, int] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            if doc_id not in order:
                order[doc_id] = len(order)
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda d: (-scores[d], order[d]))
