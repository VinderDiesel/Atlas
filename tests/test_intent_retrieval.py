"""T10b 红测：意图检索（D09）——整个授权且已注册目录上的确定性召回。

锁定行为
--------
- 原句路径始终参与（扩展不命中时原句结果仍在）；
- 扩展表达可召回原句 top-5 之外的注册指标（检索域 = 全目录，不受先前召回
  top-K 收窄）；
- 每表达召回 ≤5、并列按插入序；多路 RRF 融合去重后最终 K=5，rank 从 1 连续；
- 候选只能来自【授权 ∧ 已注册】目录：权限硬过滤、未知指标不创造；
- 候选 sources 去重且按表达序；catalog_digest 与上下文绑定；
- 空检索表达 fail-closed（retrieval_queries_missing）；无命中返回空候选（不猜测）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from agent.compiler import SemanticModel
from agent.intent.contracts import (
    CandidateSet,
    EvidenceSpan,
    IntentContractError,
    NormalizedIntent,
    QuestionContext,
    RetrievalQuery,
    SemanticIntent,
)
from agent.intent.normalize import normalize_intent
from agent.intent.retrieve import IntentRetriever, retrieve_intent

CATALOG_DIGEST = "0" * 64
REFERENCE = "2013-07-15T10:00:00+08:00"


# --- 夹具 ---------------------------------------------------------------------


def _metric(name: str, *, synonyms: tuple[str, ...] = (), description: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": f"SUM({name})"}]},
        "description": description,
        "ai_context": {"synonyms": list(synonyms)},
    }


def _model(*metrics: dict[str, Any]) -> SemanticModel:
    return SemanticModel(
        doc={
            "semantic_model": [
                {
                    "name": "atlas_test",
                    "datasets": [],
                    "relationships": [],
                    "metrics": list(metrics),
                }
            ]
        }
    )


def _context(question: str, **overrides: Any) -> QuestionContext:
    base: dict[str, Any] = {
        "question": question,
        "locale": "zh_cn",
        "catalog_digest": CATALOG_DIGEST,
        "reference_time": datetime.fromisoformat(REFERENCE),
        "timezone": "Asia/Shanghai",
    }
    base.update(overrides)
    return QuestionContext(**base)


def _intent(question: str, *, expansions: tuple[str, ...] = ()) -> NormalizedIntent:
    """经确定性归一的意图（贴近真实链路：normalize → retrieve）。"""
    span = EvidenceSpan(source_id="question", start=0, end=len(question))
    queries = tuple(RetrievalQuery(text=text, evidence=(span,)) for text in expansions)
    semantic = SemanticIntent(schema_version=1, task="query", retrieval_queries=queries)
    return normalize_intent(semantic, _context(question))


HEAD_METRICS = (
    _metric("commission_income", synonyms=("佣金收入",), description="经纪人佣金收入合计"),
    _metric("trade_amount", synonyms=("交易额",), description="证券交易成交金额"),
    _metric("holding_value", synonyms=("持仓市值",), description="客户持仓总市值"),
    _metric("client_count", synonyms=("客户数",), description="活跃客户数量"),
    _metric("fee_income", synonyms=("费用收入",), description="各类费用收入合计"),
    _metric("interest_income", synonyms=("利息收入",), description="利息收入合计"),
)

# 8 篇等长候选（词面同分，BM25 并列按插入序），用于锁定"每表达 ≤5"。
FILLER = tuple(_metric(f"income_{i}", synonyms=("收入",)) for i in range(1, 9))
TARGET = _metric("net_profit", synonyms=("净利润",), description="净利润合计")


# --- 原句路径与扩展召回 ---------------------------------------------------------


def test_original_question_path_always_contributes():
    retriever = IntentRetriever(_model(*HEAD_METRICS))
    question = "上月佣金收入"
    out = retriever.retrieve(_intent(question), _context(question))
    assert out.candidates[0].semantic_id == "commission_income"


def test_expansion_recalls_beyond_original_top5():
    """原句 top-5 外的注册指标可由扩展表达召回（检索域 = 全目录）。"""
    retriever = IntentRetriever(_model(*FILLER, TARGET))
    question = "上月收入"
    bare = retriever.retrieve(_intent(question), _context(question))
    assert "net_profit" not in {c.semantic_id for c in bare.candidates}
    expanded = retriever.retrieve(_intent(question, expansions=("上月净利润",)), _context(question))
    assert "net_profit" in {c.semantic_id for c in expanded.candidates}


def test_no_hit_returns_empty_candidates():
    retriever = IntentRetriever(_model(*HEAD_METRICS))
    question = "zzz 无匹配"
    out = retriever.retrieve(_intent(question), _context(question))
    assert out.candidates == ()


# --- 预算：每表达 ≤5、合并 K=5、rank 连续 --------------------------------------


def test_each_expression_recall_capped_at_five():
    retriever = IntentRetriever(_model(*FILLER))
    question = "上月收入"
    out = retriever.retrieve(_intent(question), _context(question))
    assert [c.semantic_id for c in out.candidates] == [f"income_{i}" for i in range(1, 6)]


def test_rank_is_one_based_contiguous_and_final_k_is_five():
    retriever = IntentRetriever(_model(*FILLER, TARGET))
    question = "上月收入"
    out = retriever.retrieve(_intent(question, expansions=("上月净利润",)), _context(question))
    assert [c.rank for c in out.candidates] == list(range(1, len(out.candidates) + 1))
    assert len(out.candidates) <= 5


# --- 权限硬过滤与注册目录 -------------------------------------------------------


def test_authorized_set_is_hard_filter():
    retriever = IntentRetriever(_model(*HEAD_METRICS), authorized=frozenset({"commission_income"}))
    question = "上月交易额与佣金收入"
    out = retriever.retrieve(_intent(question), _context(question))
    assert {c.semantic_id for c in out.candidates} == {"commission_income"}


def test_empty_authorized_yields_no_candidates():
    retriever = IntentRetriever(_model(*HEAD_METRICS), authorized=frozenset())
    question = "上月佣金收入"
    out = retriever.retrieve(_intent(question), _context(question))
    assert out.candidates == ()


def test_unknown_metric_is_never_invented():
    model = _model(*HEAD_METRICS)
    retriever = IntentRetriever(model)
    question = "上月区块链挖矿收入"
    out = retriever.retrieve(_intent(question), _context(question))
    assert {c.semantic_id for c in out.candidates} <= set(model.metrics)


# --- 来源、证据与目录绑定 -------------------------------------------------------


def test_candidate_sources_deduped_in_expression_order():
    retriever = IntentRetriever(_model(*HEAD_METRICS))
    question = "上月佣金收入"
    expansion = "上月佣金收入合计"
    out = retriever.retrieve(_intent(question, expansions=(expansion,)), _context(question))
    top = out.candidates[0]
    assert top.semantic_id == "commission_income"
    assert top.sources == (question, expansion)


def test_candidate_evidence_refs_carry_span_evidence():
    retriever = IntentRetriever(_model(*HEAD_METRICS))
    question = "上月佣金收入"
    out = retriever.retrieve(_intent(question), _context(question))
    top = out.candidates[0]
    assert top.evidence_refs
    assert top.evidence_refs[0].source_id == "question"


def test_candidate_set_binds_catalog_digest():
    retriever = IntentRetriever(_model(*HEAD_METRICS))
    question = "上月佣金收入"
    out = retriever.retrieve(_intent(question), _context(question))
    assert out.catalog_digest == CATALOG_DIGEST
    assert out.schema_version == 1


# --- fail-closed 与函数接口 -----------------------------------------------------


def test_missing_retrieval_queries_is_rejected():
    retriever = IntentRetriever(_model(*HEAD_METRICS))
    bare = NormalizedIntent(schema_version=1, task="query")
    with pytest.raises(IntentContractError) as exc:
        retriever.retrieve(bare, _context("上月佣金收入"))
    assert exc.value.reason_code == "retrieval_queries_missing"


def test_retrieve_intent_function_wraps_retriever():
    retriever = IntentRetriever(_model(*HEAD_METRICS))
    question = "上月佣金收入"
    out = retrieve_intent(_intent(question), _context(question), retriever=retriever)
    assert isinstance(out, CandidateSet)
    assert out.candidates[0].semantic_id == "commission_income"
