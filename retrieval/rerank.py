"""元数据重排（Rerank，Day 24）：对召回候选按三个信号重排。

信号与口径（确定性、可审计、固定排序规则不调参）
---------------------------------------------
1. **同义词置信度**（synonym）：问句命中的该指标同义词覆盖字符数 / 问句有效
   字符数。编码“问句直接用了指标名/同义词”的强度——词法召回只看 token 共现，
   无法区分命中主名与命中长描述。
2. **指标热度**（popularity）：外部注入的频次表（生产为查询日志统计；本项目
   评测为 gold 问句留一计数——评测脚本排除当前查询，避免用目标自身做证据）。
3. **owner 优先级**（owner）：ATLAS 治理扩展声明 owner → 显式优先级表。
   当前指标均为 finance@atlas.local（最高档 1.0），故该项为常数、
   不改变排序——属“治理完备度”信号，多 owner 场景才产生区分度（诚实声明）。

排序规则（词典序分层）
----------------------
按 (synonym, popularity, owner, 原召回序) 逐层降序裁决：**查询相关信号做主键，
全局信号只做同层裁决**。加权线性混合版本曾实测 Recall@1 34/44——热度分对
top-20 内恒在的热门指标（total_trade_value 留一 6 次）是系统性加成，能压过
同义词命中但冷门的正确指标；词典序避免“与查询无关的加分项覆盖查询证据”。
（两版对比见 eval/reports/retrieval-rerank-7d48dcb.linear-weighted.json 与
 retrieval-rerank-7d48dcb.json，数字均实测。）

用法::

    reranker = MetaReranker(model, popularity=hotness)  # hotness: {metric: count}
    top5 = reranker.rerank(recall_top20, question)[:5]
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agent.compiler import SemanticModel

# owner → 优先级（0~1），显式配置。当前唯一 owner 最高档 1.0；
# 未注册治理块的指标 owner="" → 0.0（治理信号缺失不加分）。
OWNER_PRIORITY: Mapping[str, float] = {"finance@atlas.local": 1.0}

# 热度饱和阈值：频次 ≥ 10 视为饱和（当前 44 条语料上限 5，留一后 ≤ 5）
POPULARITY_CAP = 10


def synonym_confidence(question: str, synonyms: Sequence[str]) -> float:
    """问句对同义词集的字符覆盖比例（命中字符并集去重 / 问句有效字符数）。

    相互包含的同义词（"成交量" ⊂ "总成交量"）只贡献并集字符，不重复计分。
    """
    hits = [s for s in synonyms if s and s in question]
    if not hits:
        return 0.0
    covered = len({ch for s in hits for ch in s})
    q_len = max(1, sum(1 for ch in question if not ch.isspace()))
    return min(covered / q_len, 1.0)


def _popularity_norm(count: int) -> float:
    """热度归一化：min(count / 饱和阈值, 1)，保证热门指标不垄断分数。"""
    return min(count / POPULARITY_CAP, 1.0)


class MetaReranker:
    """在召回排名内按元数据信号重排的确定性重排器。

    参数
    ----
    model      : SemanticModel（提供 metric_synonyms / metric_owners）。
    popularity : 指标 → 热度频次的只读映射；不传或缺失的指标按 0 计。
                 评测口径见 retrieval_eval（gold 留一计数，排除当前查询）。
    """

    def __init__(self, model: SemanticModel, popularity: Mapping[str, int] | None = None) -> None:
        self._model = model
        self._popularity = dict(popularity or {})

    def signal_score(self, metric: str, question: str) -> tuple[float, float, float]:
        """返回 (syn, pop, own) 三个信号分（各 0~1，供分析与调试）。"""
        syn = synonym_confidence(question, self._model.metric_synonyms.get(metric, ()))
        pop = _popularity_norm(self._popularity.get(metric, 0))
        owner = self._model.metric_owners.get(metric, "")
        own = OWNER_PRIORITY.get(owner, 0.0) if owner else 0.0
        return syn, pop, own

    def rerank(self, ranked: Sequence[str], question: str) -> list[str]:
        """在候选排名内按词典序重排（见模块 docstring 的分层规则）。

        主键同义词置信度（逐查询证据），次键热度、再次 owner（全局偏好，
        仅在同层裁决）；完全同分保持原召回序（确定性）。
        """
        scored: list[tuple[float, float, float, int, str]] = []
        for idx, doc in enumerate(ranked):
            syn, pop, own = self.signal_score(doc, question)
            scored.append((syn, pop, own, idx, doc))
        scored.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
        return [doc for _, _, _, _, doc in scored]
