"""确定性 BM25 检索（零第三方依赖，AGENTS.md：确定性优先）。

设计决策
--------
1. **无词典中文分词**：CJK 连续段按相邻 2-gram 切分（字符 bigram 是免分词
   检索的常用稳健方案），英文/数字按小写整词。不引入 jieba 等依赖，
   保证同一输入 → 同一 token 序列（可复现，评测友好）。
2. **BM25 参数固定**：k1=1.5、b=0.75（Robertson 经典取值），不在评测集上
   调参——调参会污染 Recall@5 的可信度（AGENTS.md N1）。
3. 用途：指标检索评测（见 eval/retrieval_eval.py 与 README 检索章节），
   top-N 召回的候选指标后续进入 Planner 消歧，而非绕过编译器直接执行。

已知边界（MVP，诚实声明）
------------------------
- 同义词子串重叠的指标（如"成交证券数"与"持仓证券数"共享 bigram）靠
  idf 区分，极端措辞下可能互相串扰；歧义问句不在检索评测集内
  （gold ambiguous 样本由 Planner 的 ClarificationRequest 承接）。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

# CJK 统一表意文字区间（含扩展 A 的主要部分；够用即可，无需全 Unicode 表）
_CJK_START = 0x4E00
_CJK_END = 0x9FFF
# 分段：CJK 连续段 或 非 CJK 非空白段（空白直接丢弃）
_SEGMENT_RE = re.compile(r"[\u4e00-\u9fff]+|[^\u4e00-\u9fff\s]+")
_NON_CJK_TOKEN = re.compile(r"[a-z0-9]+")

K1 = 1.5  # 词频饱和参数（Robertson 经典值）
B = 0.75  # 文档长度归一化参数（Robertson 经典值）


def tokenize(text: str) -> list[str]:
    """中英混合文本 → token 列表（CJK bigram + 小写英文/数字词）。

    参数
    ----
    text : 原始文本（问句或指标文档字段）。

    返回
    ----
    token 列表；CJK 连续段内产生相邻双字 token，长度 1 的孤立汉字
    按单字输出（避免整段被丢弃）；非 CJK 字母数字连续段按整词小写，
    标点段丢弃。
    """
    tokens: list[str] = []
    for seg in _SEGMENT_RE.findall(text):
        if _CJK_START <= ord(seg[0]) <= _CJK_END:
            tokens.extend(_cjk_tokens(seg))
        else:
            tokens.extend(_NON_CJK_TOKEN.findall(seg.lower()))
    return tokens


def _cjk_tokens(seg: str) -> list[str]:
    if len(seg) == 1:
        return [seg]
    return [seg[i] + seg[i + 1] for i in range(len(seg) - 1)]


@dataclass(frozen=True)
class Hit:
    """检索命中文档：doc_id 与 BM25 分数（供召回排序与评测计数）。"""

    doc_id: str
    score: float


class Bm25Index:
    """内存 BM25 索引：add 建倒排，search 返回 top-k Hit。

    用法（指标检索场景，语料量 ≤ 数百文档，无持久化需求）::

        index = Bm25Index()
        for doc in metric_docs:
            index.add(doc.doc_id, doc.text)
        hits = index.search("2013 年佣金收入", k=5)
    """

    def __init__(self) -> None:
        self._docs: list[str] = []  # doc_id 按插入序（评分并列时保持插入序）
        self._lengths: list[int] = []
        self._avgdl = 0.0
        self._postings: dict[str, dict[int, int]] = {}  # term -> {doc_idx: freq}

    def add(self, doc_id: str, text: str) -> None:
        """添加一篇文档（重复 doc_id 允许：按独立文档计分）。"""
        tokens = tokenize(text)
        if not tokens:
            return
        idx = len(self._docs)
        self._docs.append(doc_id)
        self._lengths.append(len(tokens))
        n = len(self._docs)
        self._avgdl = sum(self._lengths) / n
        for term, freq in Counter(tokens).items():
            self._postings.setdefault(term, {})[idx] = freq

    def search(self, query: str, k: int = 5) -> list[Hit]:
        """query → top-k Hit（分数降序，并列按插入序）。

        参数
        ----
        query : 自然语言问句（与文档同一 tokenize 口径）。
        k     : 返回条数。

        返回
        ----
        命中文档列表；索引为空或 query 无任何可匹配 token 时返回空列表。
        """
        if not self._docs or k <= 0:
            return []
        query_terms = list(dict.fromkeys(tokenize(query)))
        if not query_terms:
            return []
        n = len(self._docs)
        scores: dict[int, float] = {}
        for term in query_terms:
            postings = self._postings.get(term)
            if not postings:
                continue
            df = len(postings)
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            for doc_idx, freq in postings.items():
                norm = K1 * (1.0 - B + B * self._lengths[doc_idx] / self._avgdl)
                scores[doc_idx] = scores.get(doc_idx, 0.0) + idf * freq * (K1 + 1.0) / (freq + norm)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return [Hit(self._docs[idx], round(score, 6)) for idx, score in ranked[:k]]
