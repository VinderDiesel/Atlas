"""两阶段 schema linking：问句 → 受限候选指标 + 表级可达性（Day 29）。

定位
----
确定性检索与 Planner 之间的候选收窄层（不放宽：输出仍是**候选指标集**，
最终口径由 Planner/Compiler 唯一路由与编译决定）。解决 Day 24 发现的单路退化
（KL#18：「市值合计」问句被 average_holding_value 抢 top-1——同域词法重叠，
全量域打分无法区分）。

阶段
----
1. **图域约束粗筛**（retrieval/graph_store.SemanticGraph）：问句维度词命中 →
   对全部指标做可达性预检（filter_candidates），跨实体错配（现金域指标 ×
   证券维度等）在打分前剔除——与 Compiler._join_chain 同源，剔除即"必然编译
   失败"的候选，不做无用召回。
2. **受限域召回**：图粗筛后的候选域上重建 BM25 子索引打分（同口径子域 idf），
   engine="fuse" 时并入 Milvus 稀疏向量 RRF。
3. **元数据重排**（retrieval/rerank.MetaReranker）：同义词置信度主键的词典序
   重排 + 热度/owner 同层裁决；热度为 gold 留一计数（与 retrieval_eval 同口径）。
4. **表级展开**：top-K 候选按图可达性展开为表集合（reachable_tables）——schema
   linking 的表级输出，供评测与 Agent 解释使用。

口径（诚实声明）
----------------
- 输出对象是**指标**；表是伴随展开（库域仅 6 张 dwd 表且图全连通，表集合
  无区分度，见 eval/schema_link_eval.py 的 coverage 口径说明）。
- 无维度信号（detect_dimensions 空）时阶段 1 不过滤，退化为全量域召回。
- **默认全链确定性**：同一问句 → 同一输出（Bm25Index 参数固定 k1/b、词典序重排）。
  注入 `chooser`（ADR-0030 ⑤）后阶段 3 引入判别式建议：**仅在置信度达阈值时**
  提前选中项，且判别失败/低置信一律退回确定性序——默认不注入时行为逐字不变。
- 注入 `concept_index`（ADR-0032 ③ 检索缝）后**仅阶段 2 打分查询词**扩展为
  原问句 + 概念闭包术语（自身 + 后代 + 祖先 1 层，S1+S2+S3 全源、match_type
  不过滤）；阶段 1 图粗筛、阶段 3 重排与 `SchemaLinkResult.question` 仍是原问句
  ——只扩展检索查询词、不注入对象。无索引（None）时行为逐字不变。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.compiler import SemanticModel
from agent.concept_index import ConceptIndex, concept_terms, detect_locale, match_terms
from retrieval.bm25 import Bm25Index
from retrieval.graph_store import SemanticGraph
from retrieval.metric_docs import MetricDoc, build_metric_docs
from retrieval.rerank import MetaReranker, MetricChooser


@dataclass(frozen=True)
class SchemaLinkResult:
    """一次 schema linking 的结构化输出（供 Agent/评测/解释消费）。"""

    question: str
    candidates: tuple[str, ...]  # top-K 指标（阶段 3 重排后）
    dims: tuple[str, ...]  # 问句检出的维度字段（阶段 1 输入）
    kept: tuple[str, ...]  # 图粗筛后保留的候选（打分域）
    scores: tuple[tuple[str, float], ...]  # 阶段 2 原始分数（受限域 BM25）
    tables_top1: tuple[str, ...]  # top1 候选的可达表（表级展开）
    tables_topk: tuple[str, ...]  # top-K 候选可达表并集（评测 coverage 用）


class SchemaLinker:
    """确定性两阶段 schema linking（图域粗筛 → 受限召回 → 元数据重排）。"""

    def __init__(
        self,
        model: SemanticModel,
        popularity: dict[str, int] | None = None,
        chooser: MetricChooser | None = None,
        concept_index: ConceptIndex | None = None,
    ) -> None:
        """构造链接器。

        参数
        ----
        model      : SemanticModel。
        popularity : 热度映射（阶段 3 重排用；与 retrieval_eval 同口径）。
        chooser    : 可选判别引擎（ADR-0030 ⑤，如 `JevEngine`）。传 `None`
                     （默认）时阶段 3 与 ADR-0030 之前**逐字一致**；注入后仅在
                     高置信判别时提前选中项，异常/低置信退回确定性词典序。
        concept_index : 可选概念索引（ADR-0032 ③ 检索缝）。传 `None`（默认）时
                     全链与引入概念层前**逐字一致**（无索引 = 旁路，契约）；
                     注入后仅阶段 2 打分查询词追加概念闭包术语。
        """
        self._model = model
        self._concept_index = concept_index
        self._graph = SemanticGraph(model)
        self._docs = {d.doc_id: d.text for d in build_metric_docs(model)}
        self._reranker = MetaReranker(model, popularity, chooser=chooser)
        self._all_metrics = tuple(model.metrics)

    def link(self, question: str, k: int = 5, engine: str = "bm25") -> SchemaLinkResult:
        """问句 → 受限候选指标（两阶段，见模块 docstring）。

        参数
        ----
        question : 自然语言问句。
        k        : 返回 top-K 指标数。
        engine   : "bm25"（零依赖）或 "fuse"（双路 RRF，需 atlas-milvus 在跑）。

        返回
        ----
        SchemaLinkResult；图粗筛后候选为空（维度约束无任何指标可达）时
        candidates 为空元组，调用方应走澄清/降级（不猜测）。
        """
        dims = tuple(self._graph.detect_dimensions(question))
        kept = self._graph.filter_candidates(list(self._all_metrics), list(dims))

        # 阶段 2：在受限候选域内打分（子索引保证 idf 属于该域；空查询词返回空）。
        # 概念层注入时查询词扩展为 原问句 + 闭包术语（ADR-0032 ③ 检索缝）
        if not kept:
            return SchemaLinkResult(question, (), dims, (), (), (), ())
        query = self._augment(question)
        if engine == "fuse":
            ranked, scores = self._search_fuse(query, kept)
        else:
            ranked, scores = self._search_bm25(query, kept)

        # 阶段 3：元数据重排（词法序分层，见 rerank.py 口径）。问句仍传原句：
        # 同义词置信度信号语义是「问句直接用了指标名」，机器追加术语不计入
        final = tuple(self._reranker.rerank(ranked, question)[:k])
        topk = final[:k]
        tables_top1 = self._expand_tables(topk[:1])
        tables_topk = self._expand_tables(topk)
        return SchemaLinkResult(
            question=question,
            candidates=topk,
            dims=dims,
            kept=tuple(kept),
            scores=tuple(scores),
            tables_top1=tables_top1,
            tables_topk=tables_topk,
        )

    # -- 内部实现 ----------------------------------------------------------

    def _augment(self, question: str) -> str:
        """概念术语扩展（ADR-0032 ③ 检索缝）：命中术语 → 概念闭包 → 追加未见术语。

        只扩展**检索查询词**（阶段 2 打分），不注入对象；输出 = 原问句 +
        空格连接的新术语（闭包按索引文件序，确定序）；无索引/无命中/无新
        术语时原样返回（旁路不变量）。
        """
        index = self._concept_index
        if index is None:
            return question
        locale = detect_locale(question)
        hits = match_terms(index, question, locale)
        if not hits:
            return question
        concepts = tuple(dict.fromkeys(hit.concept for hit in hits))
        extras = [
            term
            for term in concept_terms(index, concepts, locale, ancestors=1)
            if term not in question
        ]
        if not extras:
            return question
        return question + " " + " ".join(extras)

    def _search_bm25(
        self, question: str, kept: list[str]
    ) -> tuple[list[str], list[tuple[str, float]]]:
        """受限候选域上的 BM25 打分（与全量域检索同 tokenize/参数口径）。"""
        sub = Bm25Index()
        for metric in kept:
            sub.add(metric, self._docs[metric])
        hits = sub.search(question, k=max(len(kept), 1))
        return [h.doc_id for h in hits], [(h.doc_id, h.score) for h in hits]

    def _search_fuse(
        self, question: str, kept: list[str]
    ) -> tuple[list[str], list[tuple[str, float]]]:
        """双路 RRF：BM25（受限域）+ Milvus 稀疏向量（受限语料重建）。"""
        from retrieval.fusion import rrf
        from retrieval.milvus_client import MilvusIndex

        sub = Bm25Index()
        for metric in kept:
            sub.add(metric, self._docs[metric])
        bm25_top = [h.doc_id for h in sub.search(question, k=20)]
        mv = MilvusIndex()
        mv.rebuild([MetricDoc(doc_id=m, text=self._docs[m]) for m in kept])
        mv_top = [h.doc_id for h in mv.search(question, k=20)]
        fused = rrf([bm25_top, mv_top])
        # RRF 无分数语义，scores 记 bm25 原始分（分析用；排序以融合序为准）
        score_map = dict((h.doc_id, h.score) for h in sub.search(question, k=max(len(kept), 1)))
        return fused, [(m, score_map.get(m, 0.0)) for m in fused]

    def _expand_tables(self, metrics: tuple[str, ...]) -> tuple[str, ...]:
        """候选指标 → 可达表并集（保指标序；排序输出供评测/解释）。"""
        seen: list[str] = []
        for metric in metrics:
            for table in sorted(self._graph.reachable_tables(metric)):
                if table not in seen:
                    seen.append(table)
        return tuple(seen)
