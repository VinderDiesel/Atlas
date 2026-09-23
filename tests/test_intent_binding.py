"""T10c 红测：槽位完整绑定（D09）——每个意图槽位映射到 Plan 字段，未绑定只能澄清。

锁定行为
--------
- 全绑 → `plan_candidate` 产出，`slot_bindings` 覆盖全部槽位，`reason_code="bound"`；
- 未绑定条件（filters/groups/time/sort）→ `plan_candidate is None` + `unresolved_slots`
  带槽位下标（设计页原例：`("filters.0",)`）；
- 未知 Metric / 候选集外的注册指标 → 不创造、不越权（unresolved("metric")）；
- 候选集空 → unresolved("metric")、reason_code="no_candidates"（handoff 依据）；
- 否定（neq/not_in 单值）不丢；多值 in/not_in 首版无力绑定 → 澄清（Compiler 只支持标量 op）；
- 维度与过滤字段走**关系可达性硬检查**（`can_group_by` 同源）；
- 时间只绑首个（Plan.time 单值），其余进 unresolved；day→date 粒度映射；
- 歧义声明一律进 unresolved（不强猜）；
- 默认值：无 limit → 100，order_by/comparison 保 Plan 默认。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from agent.compiler import Filter, OrderSpec, SemanticModel, TimeSpec
from agent.intent.bind import IntentBinder, bind_intent
from agent.intent.contracts import (
    Ambiguity,
    Candidate,
    CandidateSet,
    EvidenceSpan,
    IntentFilter,
    Mention,
    NormalizedIntent,
    QuestionContext,
    RetrievalQuery,
    SemanticIntent,
    SortSpec,
)
from agent.intent.normalize import normalize_intent

CATALOG_DIGEST = "0" * 64
REFERENCE = "2013-07-15T10:00:00+08:00"


# --- 夹具 ---------------------------------------------------------------------


def _field(name: str, *, synonyms: tuple[str, ...] = (), is_time: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": name}]},
        "dimension": {"is_time": True} if is_time else {},
        "ai_context": {"synonyms": list(synonyms)},
    }


def _dataset(name: str, source: str, fields: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    return {"name": name, "source": source, "fields": list(fields)}


def _metric(
    name: str, *, table: str, column: str, synonyms: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "name": name,
        "expression": {
            "dialects": [{"dialect": "ANSI_SQL", "expression": f"SUM({table}.{column})"}]
        },
        "description": "",
        "ai_context": {"synonyms": list(synonyms)},
    }


def _workshop_doc() -> dict[str, Any]:
    """轻量两表模型：fact_trades ↔ dim_branch（dim_region 孤立 = 不可达）。"""
    return {
        "semantic_model": [
            {
                "name": "atlas_bind_test",
                "datasets": [
                    _dataset(
                        "fact_trades",
                        "atlas.dwd.fact_trades",
                        (_field("client_id"), _field("commission"), _field("quantity")),
                    ),
                    _dataset(
                        "dim_branch",
                        "atlas.dwd.dim_branch",
                        (_field("branch", synonyms=("分支", "分公司")), _field("client_id")),
                    ),
                    _dataset(
                        "dim_region",
                        "atlas.dwd.dim_region",
                        (_field("region", synonyms=("地区",)),),
                    ),
                ],
                "relationships": [
                    {
                        "name": "trade_branch",
                        "from": "fact_trades",
                        "to": "dim_branch",
                        "from_columns": ["client_id"],
                        "to_columns": ["client_id"],
                    }
                ],
                "metrics": [
                    _metric(
                        "commission_income",
                        table="fact_trades",
                        column="commission",
                        synonyms=("佣金收入", "佣金"),
                    ),
                    _metric(
                        "trade_count",
                        table="fact_trades",
                        column="quantity",
                        synonyms=("交易笔数",),
                    ),
                ],
            }
        ]
    }


WORKSHOP = SemanticModel(doc=_workshop_doc())


def _context(question: str) -> QuestionContext:
    return QuestionContext(
        question=question,
        locale="zh_cn",
        catalog_digest=CATALOG_DIGEST,
        reference_time=datetime.fromisoformat(REFERENCE),
        timezone="Asia/Shanghai",
    )


def _mention(question: str, text: str) -> Mention:
    start = question.index(text)
    span = EvidenceSpan(source_id="question", start=start, end=start + len(text))
    return Mention(text=text, evidence=(span,))


def _intent(
    question: str,
    *,
    metrics: tuple[str, ...] = (),
    groups: tuple[str, ...] = (),
    filters: tuple[tuple[str, str, tuple[str, ...]], ...] = (),
    times: tuple[str, ...] = (),
    sort: tuple[str, str] | None = None,
    limit: int | None = None,
    ambiguities: tuple[tuple[str, str, str], ...] = (),
    expansions: tuple[str, ...] = (),
) -> NormalizedIntent:
    semantic = SemanticIntent(
        schema_version=1,
        task="query",
        metric_mentions=tuple(_mention(question, t) for t in metrics),
        groups=tuple(_mention(question, t) for t in groups),
        filters=tuple(
            IntentFilter(
                subject=_mention(question, subject),
                op=op,  # type: ignore[arg-type]
                values=tuple(_mention(question, v) for v in values),
            )
            for subject, op, values in filters
        ),
        time_mentions=tuple(_mention(question, t) for t in times),
        sort=(
            SortSpec(direction=sort[0], by=_mention(question, sort[1]))  # type: ignore[arg-type]
            if sort is not None
            else None
        ),
        limit=limit,
        ambiguities=tuple(
            Ambiguity(slot=slot, reason_code=code, evidence=(_mention(question, text).evidence[0],))
            for slot, code, text in ambiguities
        ),
        retrieval_queries=tuple(
            RetrievalQuery(text=t, evidence=(_mention(question, t).evidence[0],))
            for t in expansions
        ),
    )
    return normalize_intent(semantic, _context(question))


def _candidates(*names: str) -> CandidateSet:
    return CandidateSet(
        catalog_digest=CATALOG_DIGEST,
        candidates=tuple(
            Candidate(semantic_id=name, rank=i + 1, sources=("q",)) for i, name in enumerate(names)
        ),
    )


def _binder_workshop() -> IntentBinder:
    return IntentBinder(WORKSHOP)


def _lifecycle_bundle(tmp_path: Path, doc: dict[str, Any], *, name: str) -> Any:
    """轻量制品：语义模型 + 真实默认流程模板（load_bundle 的装配要求）。"""
    from agent.runtime.bundle import load_bundle
    from tests.workbench_support import REPO_ROOT, write_bundle

    path = f"semantic/ossie/{name}.ossie.yaml"
    files = {
        path: yaml.safe_dump(doc, sort_keys=False, allow_unicode=True).encode("utf-8"),
        "agent/flows/templates/query.json": (
            REPO_ROOT / "agent/flows/templates/query.json"
        ).read_bytes(),
    }
    root = tmp_path / "releases"
    release_id = write_bundle(root, files)
    return load_bundle(root, release_id), path


# --- 全绑定与 Plan 产出 ---------------------------------------------------------


def test_full_binding_produces_plan_candidate():
    question = "2013 年第二季度各分支佣金收入"
    intent = _intent(question, metrics=("佣金收入",), groups=("分支",), times=("2013 年第二季度",))
    result = _binder_workshop().bind(intent, _candidates("commission_income"))
    assert result.plan_candidate is not None
    plan = result.plan_candidate
    assert plan.metric == "commission_income"
    assert plan.dimensions == ("branch",)
    assert plan.time == TimeSpec(granularity="quarter", value="2013Q2")
    assert result.unresolved_slots == ()
    assert result.reason_code == "bound"
    slots = {b.slot for b in result.slot_bindings}
    assert {"metric", "groups.0", "time.0"} <= slots


def test_filter_binding_produces_plan_filter():
    question = "TN 分支的佣金收入"
    intent = _intent(question, metrics=("佣金收入",), filters=(("分支", "eq", ("TN",)),))
    result = _binder_workshop().bind(intent, _candidates("commission_income"))
    assert result.plan_candidate is not None
    assert result.plan_candidate.filters == (Filter(column="branch", op="=", value="TN"),)
    assert {b.slot for b in result.slot_bindings} >= {"filters.0"}


def test_sort_limit_and_time_plan_fields():
    question = "上月中各分支佣金收入按分支降序前 10"
    intent = _intent(
        question,
        metrics=("佣金收入",),
        groups=("分支",),
        times=("上月",),
        sort=("desc", "分支"),
        limit=10,
    )
    result = _binder_workshop().bind(intent, _candidates("commission_income"))
    assert result.plan_candidate is not None
    plan = result.plan_candidate
    assert plan.dimensions == ("branch",)
    assert plan.time == TimeSpec(granularity="month", value=201306)
    assert plan.order_by == (OrderSpec(column="branch", desc=True),)
    assert plan.limit == 10


def test_defaults_when_optional_slots_absent():
    question = "佣金收入"
    intent = _intent(question, metrics=("佣金收入",))
    result = _binder_workshop().bind(intent, _candidates("commission_income"))
    assert result.plan_candidate is not None
    assert result.plan_candidate.order_by == ()
    assert result.plan_candidate.limit == 100
    assert result.plan_candidate.comparison is None


# --- 未绑定只能澄清 -------------------------------------------------------------


def test_unresolved_filter_requires_clarification():
    question = "上季度线上渠道的佣金收入"
    intent = _intent(
        question,
        metrics=("佣金收入",),
        times=("上季度",),
        filters=(("渠道", "eq", ("线上",)),),
    )
    result = _binder_workshop().bind(intent, _candidates("commission_income"))
    assert result.plan_candidate is None
    assert result.unresolved_slots == ("filters.0",)
    assert result.reason_code == "clarification_required"


def test_unknown_metric_is_not_invented():
    question = "区块链挖矿收入"
    intent = _intent(question, metrics=("区块链挖矿收入",))
    result = _binder_workshop().bind(intent, _candidates("commission_income"))
    assert result.plan_candidate is None
    assert result.unresolved_slots == ("metric",)


def test_metric_outside_candidates_is_not_bound():
    """注册目录内但不在候选集的指标：不越权绑定（候选是检索预算产物）。"""
    question = "佣金收入"
    intent = _intent(question, metrics=("佣金收入",))
    result = _binder_workshop().bind(intent, _candidates("trade_count"))
    assert result.plan_candidate is None
    assert result.unresolved_slots == ("metric",)


def test_empty_candidates_yield_no_candidates_reason():
    question = "佣金收入"
    intent = _intent(question, metrics=("佣金收入",))
    result = _binder_workshop().bind(intent, _candidates())
    assert result.plan_candidate is None
    assert result.unresolved_slots == ("metric",)
    assert result.reason_code == "no_candidates"


def test_unreachable_group_dimension_is_unresolved():
    question = "上月按地区佣金收入"
    intent = _intent(question, metrics=("佣金收入",), groups=("地区",), times=("上月",))
    result = _binder_workshop().bind(intent, _candidates("commission_income"))
    assert result.plan_candidate is None
    assert result.unresolved_slots == ("groups.0",)


def test_sort_target_outside_dimensions_is_unresolved():
    question = "上月佣金收入按地区降序"
    intent = _intent(question, metrics=("佣金收入",), times=("上月",), sort=("desc", "地区"))
    result = _binder_workshop().bind(intent, _candidates("commission_income"))
    assert result.plan_candidate is None
    assert "sort" in result.unresolved_slots


def test_second_time_mention_is_unresolved():
    question = "上月与去年佣金收入"
    intent = _intent(question, metrics=("佣金收入",), times=("上月", "去年"))
    result = _binder_workshop().bind(intent, _candidates("commission_income"))
    assert result.plan_candidate is None
    assert result.unresolved_slots == ("time.1",)


def test_ambiguity_forces_clarification():
    question = "上月佣金收入"
    intent = _intent(
        question,
        metrics=("佣金收入",),
        times=("上月",),
        ambiguities=(("metric", "metric_ambiguous", "佣金收入"),),
    )
    result = _binder_workshop().bind(intent, _candidates("commission_income"))
    assert result.plan_candidate is None
    assert "metric" in result.unresolved_slots


# --- 否定与算子映射 -------------------------------------------------------------


def test_neq_operator_is_preserved():
    question = "除 TN 分支外佣金收入"
    intent = _intent(question, metrics=("佣金收入",), filters=(("分支", "neq", ("TN",)),))
    result = _binder_workshop().bind(intent, _candidates("commission_income"))
    assert result.plan_candidate is not None
    assert result.plan_candidate.filters == (Filter(column="branch", op="!=", value="TN"),)


def test_single_value_not_in_maps_to_ne():
    question = "非 TN 分支的佣金收入"
    intent = _intent(question, metrics=("佣金收入",), filters=(("分支", "not_in", ("TN",)),))
    result = _binder_workshop().bind(intent, _candidates("commission_income"))
    assert result.plan_candidate is not None
    assert result.plan_candidate.filters == (Filter(column="branch", op="!=", value="TN"),)


def test_multivalue_in_filter_is_unresolved():
    """Compiler 只支持标量 op：多值 IN 首版无力绑定 → 澄清而非改写语义。"""
    question = "TN 和 CA 分支的佣金收入"
    intent = _intent(question, metrics=("佣金收入",), filters=(("分支", "in", ("TN", "CA")),))
    result = _binder_workshop().bind(intent, _candidates("commission_income"))
    assert result.plan_candidate is None
    assert result.unresolved_slots == ("filters.0",)


# --- 检索驱动与制品装载 ---------------------------------------------------------


def test_rank_one_metric_bound_without_mention():
    """无 metric 提及：候选 rank1 是检索的确定性选择（注册目录内、受校验）。"""
    question = "情况如何"
    intent = _intent(question)
    result = _binder_workshop().bind(intent, _candidates("commission_income", "trade_count"))
    assert result.plan_candidate is not None
    assert result.plan_candidate.metric == "commission_income"


def test_bind_intent_loads_bundle_and_binds(tmp_path: Path):
    bundle, path = _lifecycle_bundle(tmp_path, _workshop_doc(), name="atlas_bind_test")
    question = "2013 年第二季度各分支佣金收入"
    intent = _intent(question, metrics=("佣金收入",), groups=("分支",), times=("2013 年第二季度",))
    result = bind_intent(intent, _candidates("commission_income"), bundle, model_path=path)
    assert result.plan_candidate is not None
    assert result.plan_candidate.metric == "commission_income"
    assert result.plan_candidate.dimensions == ("branch",)


def test_bind_intent_with_real_finance_bundle(tmp_path: Path):
    from tests.workbench_support import bundle_files, write_bundle

    root = tmp_path / "releases"
    release_id = write_bundle(root, bundle_files())
    from agent.runtime.bundle import load_bundle

    bundle = load_bundle(root, release_id)
    question = "2013 年第二季度总交易额"
    intent = _intent(question, metrics=("总交易额",), times=("2013 年第二季度",))
    result = bind_intent(intent, _candidates("total_trade_value"), bundle)
    assert result.plan_candidate is not None
    assert result.plan_candidate.metric == "total_trade_value"
    assert result.plan_candidate.time == TimeSpec(granularity="quarter", value="2013Q2")
    assert result.unresolved_slots == ()
