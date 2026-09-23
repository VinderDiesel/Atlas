"""ADR-0031 D09 检索：整个授权且已注册目录上的确定性召回。

职责边界
--------
- 检索域 = 【授权 ∧ 已注册】目录的**全量文档**（`build_metric_docs`），不受任何
  先前召回 top-K 收窄；权限硬过滤在语料构建时**物理排除**（未授权对象不进索引，
  不是事后过滤）。"最终关系可达性"是 bind/编译阶段的硬检查，不在本层收窄。
- 检索表达 = `NormalizedIntent.retrieval_queries`（normalize 保证原句首位且带
  证据）；每表达 BM25 召回 ≤ `MAX_CANDIDATES_PER_QUERY`，多路 RRF 融合去重后
  `FINAL_K` 截断（D09 检索预算）。
- 候选只能来自注册目录（未知指标不创造、不猜测）；无命中返回空候选集，由
  调用方（bind/clarify）决定澄清路径。
- 确定性：语料按指标定义序建索引、BM25 参数固定、RRF 只依赖排名——同一输入
  同一输出，无外部状态。
"""

from __future__ import annotations

from agent.compiler import SemanticModel
from agent.intent.contracts import (
    Candidate,
    CandidateSet,
    EvidenceSpan,
    IntentContractError,
    NormalizedIntent,
    QuestionContext,
)
from retrieval.bm25 import Bm25Index
from retrieval.fusion import rrf
from retrieval.metric_docs import build_metric_docs

# D09 检索预算：每表达召回上限 5；合并去重后最终 K=5。
MAX_CANDIDATES_PER_QUERY = 5
FINAL_K = 5


class IntentRetriever:
    """确定性检索器：构造期固定【授权 ∧ 已注册】语料，运行期纯函数。

    参数
    ----
    model      : 已注册目录的语义模型（`build_metric_docs` 的语料来源）。
    authorized : 权限硬过滤集合（授权可见的 semantic_id）；`None` = 全可见
                 （仅限系统内部全量目录场景，调用方需有授权依据）。
    """

    def __init__(self, model: SemanticModel, *, authorized: frozenset[str] | None = None) -> None:
        docs = {d.doc_id: d.text for d in build_metric_docs(model)}
        if authorized is not None:
            docs = {k: v for k, v in docs.items() if k in authorized}
        self._docs = docs
        self._index = Bm25Index()
        for doc_id, text in docs.items():
            self._index.add(doc_id, text)

    def retrieve(self, intent: NormalizedIntent, context: QuestionContext) -> CandidateSet:
        """多路召回并合并（预算见模块 docstring）。

        参数
        ----
        intent  : 已归一的意图（`retrieval_queries` 首位必须是原句）。
        context : 问题上下文（`catalog_digest` 复制进候选集绑定）。

        返回
        ----
        `CandidateSet`：候选按 RRF 融合序、rank 从 1 连续、最多 `FINAL_K` 条。

        抛出
        ----
        IntentContractError：检索表达缺失（原句必须保留，fail-closed）。
        """
        if not intent.retrieval_queries:
            raise IntentContractError("retrieval_queries_missing", "检索表达缺失（原句必须保留）")
        rankings: list[list[str]] = []
        evidence_by_text: dict[str, tuple[EvidenceSpan, ...]] = {}
        for query in intent.retrieval_queries:
            hits = self._index.search(query.text, k=MAX_CANDIDATES_PER_QUERY)
            rankings.append([h.doc_id for h in hits])
            evidence_by_text[query.text] = query.evidence

        fused = rrf(rankings)
        sources: dict[str, list[str]] = {}
        for query, ranking in zip(intent.retrieval_queries, rankings, strict=True):
            for doc_id in ranking:
                bucket = sources.setdefault(doc_id, [])
                if query.text not in bucket:
                    bucket.append(query.text)

        candidates = tuple(
            Candidate(
                semantic_id=doc_id,
                rank=rank,
                sources=tuple(sources[doc_id]),
                evidence_refs=self._evidence_of(sources[doc_id], evidence_by_text),
            )
            for rank, doc_id in enumerate(fused[:FINAL_K], start=1)
        )
        return CandidateSet(catalog_digest=context.catalog_digest, candidates=candidates)

    @staticmethod
    def _evidence_of(
        texts: list[str], evidence_by_text: dict[str, tuple[EvidenceSpan, ...]]
    ) -> tuple[EvidenceSpan, ...]:
        """命中表达的来源证据（按表达序去重；texts 必来自 evidence_by_text）。"""
        seen: list[EvidenceSpan] = []
        for text in texts:
            for span in evidence_by_text[text]:
                if span not in seen:
                    seen.append(span)
        return tuple(seen)


def retrieve_intent(
    intent: NormalizedIntent,
    context: QuestionContext,
    *,
    retriever: IntentRetriever,
) -> CandidateSet:
    """设计页接口（T10）：`retrieve_intent(intent, context) -> CandidateSet`。

    `retriever` 承载目录与授权（装配期注入，如由发布制品构造）；函数本身无状态。
    """
    return retriever.retrieve(intent, context)
