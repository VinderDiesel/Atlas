"""T10a 意图合同与确定性归一（ADR-0031 D09）。

口径：
- 合同是**模型无关**的 Schema/证据校验：`schema_version=1`、`extra=forbid`、
  EvidenceSpan 为原文字符半开区间且必须可校验（来源不可达/越界/文本不符一律拒绝）；
- 时间数值换算、排序与 limit 规范化由确定性代码完成（D09）；相对时间依赖固定
  `reference_time` + IANA `timezone`（时区固定时钟），naive 时间拒绝；
- 检索预算：原句必保留（首位），扩展最多 2 条（超限拒绝，不静默截断）；
- 本层不解析 Metric 名、不接触 SemanticModel（归属 bind，T10c）。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from agent.intent.contracts import (
    EvidenceSpan,
    IntentContractError,
    IntentFilter,
    Mention,
    QuestionContext,
    RetrievalQuery,
    SemanticIntent,
)
from agent.intent.normalize import normalize_intent

QUESTION = "2013 年第二季度总交易额"
REFERENCE = "2013-07-15T10:00:00+08:00"


def _context(question: str = QUESTION, **overrides: Any) -> QuestionContext:
    doc: dict[str, Any] = {
        "question": question,
        "locale": "zh_cn",
        "authorized_context": {},
        "catalog_digest": "0" * 64,
        "catalog_summary": "atlas_finance",
        "reference_time": REFERENCE,
        "timezone": "Asia/Shanghai",
        "capabilities": ("query",),
    }
    doc.update(overrides)
    return QuestionContext.model_validate(doc)


def _span(question: str, text: str, *, source: str = "question") -> EvidenceSpan:
    start = question.index(text)
    return EvidenceSpan(source_id=source, start=start, end=start + len(text))


def _mention(question: str, text: str, *, source: str = "question") -> Mention:
    return Mention(text=text, evidence=(_span(question, text, source=source),))


def _query(question: str, text: str, *, evidence_text: str | None = None) -> RetrievalQuery:
    anchor = evidence_text if evidence_text is not None else text
    return RetrievalQuery(text=text, evidence=(_span(question, anchor),))


def _intent(question: str = QUESTION, **overrides: Any) -> SemanticIntent:
    doc: dict[str, Any] = {"schema_version": 1, "task": "query"}
    doc.update(overrides)
    return SemanticIntent.model_validate(doc)


# --- 合同形状（pydantic 层拒绝） ---------------------------------------------


def test_schema_version_locked_to_one():
    with pytest.raises(ValidationError):
        SemanticIntent.model_validate({"schema_version": 2, "task": "query"})


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        SemanticIntent.model_validate({"schema_version": 1, "task": "query", "sql": "SELECT 1"})


def test_task_vocabulary_locked():
    with pytest.raises(ValidationError):
        SemanticIntent.model_validate({"schema_version": 1, "task": "guess"})


def test_mentions_require_at_least_one_evidence():
    with pytest.raises(ValidationError):
        Mention(text="总交易额", evidence=())


def test_negative_limit_rejected():
    with pytest.raises(ValidationError):
        _intent(limit=-5)


# --- 证据校验（normalize 层拒绝，fail-closed） --------------------------------


def test_evidence_span_out_of_range_rejected():
    span = EvidenceSpan(source_id="question", start=len(QUESTION) - 2, end=len(QUESTION) + 5)
    intent = _intent(metric_mentions=(Mention(text="总交易额", evidence=(span,)),))
    with pytest.raises(IntentContractError) as exc:
        normalize_intent(intent, _context())
    assert exc.value.reason_code == "evidence_out_of_range"


def test_evidence_text_must_match_source():
    # "总交易额" 在句中的真实位置是 [10,14)；故意给错位区间。
    span = EvidenceSpan(source_id="question", start=5, end=9)
    intent = _intent(metric_mentions=(Mention(text="总交易额", evidence=(span,)),))
    with pytest.raises(IntentContractError) as exc:
        normalize_intent(intent, _context())
    assert exc.value.reason_code == "evidence_mismatch"


def test_evidence_source_must_be_reachable():
    span = EvidenceSpan(source_id="external", start=0, end=4)
    intent = _intent(metric_mentions=(Mention(text="2013", evidence=(span,)),))
    with pytest.raises(IntentContractError) as exc:
        normalize_intent(intent, _context())
    assert exc.value.reason_code == "evidence_source_unknown"


def test_authorized_context_source_allowed():
    # "上季度" 在 turn_1 中的区间为 [0,3)。
    span = EvidenceSpan(source_id="turn_1", start=0, end=3)
    intent = _intent(metric_mentions=(Mention(text="上季度", evidence=(span,)),))
    context = _context(authorized_context={"turn_1": "上季度换成今年第二季度"})
    out = normalize_intent(intent, context)
    assert out.metric_mentions[0].text == "上季度"


# --- 检索预算（原句必保留、扩展 ≤2） ------------------------------------------


def test_original_question_always_first_retrieval_query():
    out = normalize_intent(_intent(), _context())
    assert [q.text for q in out.retrieval_queries] == [QUESTION]


def test_retrieval_expansion_within_budget_kept():
    intent = _intent(
        retrieval_queries=(
            _query(QUESTION, QUESTION),
            _query(QUESTION, "Q2 交易额", evidence_text="第二季度总交易额"),
            _query(QUESTION, "2013Q2 GMV", evidence_text="2013"),
        )
    )
    out = normalize_intent(intent, _context())
    assert [q.text for q in out.retrieval_queries] == [QUESTION, "Q2 交易额", "2013Q2 GMV"]


def test_retrieval_expansion_limited_to_two():
    intent = _intent(
        retrieval_queries=(
            _query(QUESTION, QUESTION),
            _query(QUESTION, "Q2 交易额", evidence_text="第二季度总交易额"),
            _query(QUESTION, "2013Q2 GMV", evidence_text="2013"),
            _query(QUESTION, "transaction amount", evidence_text="总交易额"),
        )
    )
    with pytest.raises(IntentContractError) as exc:
        normalize_intent(intent, _context())
    assert exc.value.reason_code == "retrieval_budget_exceeded"


# --- 相对时间：月/季/年边界（固定参考时钟） -----------------------------------


@pytest.mark.parametrize(
    ("reference", "text", "granularity", "value", "start", "end"),
    [
        ("2013-07-15T10:00:00+08:00", "上月", "month", 201306, "2013-06-01", "2013-06-30"),
        ("2013-01-15T10:00:00+08:00", "上月", "month", 201212, "2012-12-01", "2012-12-31"),
        ("2013-03-31T23:30:00+08:00", "本月", "month", 201303, "2013-03-01", "2013-03-31"),
        ("2013-07-15T10:00:00+08:00", "上季度", "quarter", "2013Q2", "2013-04-01", "2013-06-30"),
        ("2013-01-15T10:00:00+08:00", "上季度", "quarter", "2012Q4", "2012-10-01", "2012-12-31"),
        ("2013-07-15T10:00:00+08:00", "去年", "year", 2012, "2012-01-01", "2012-12-31"),
        ("2013-07-15T10:00:00+08:00", "今年", "year", 2013, "2013-01-01", "2013-12-31"),
    ],
)
def test_relative_time_boundaries(
    reference: str, text: str, granularity: str, value: int | str, start: str, end: str
):
    question = f"{text}总交易额"
    intent = _intent(question=question, time_mentions=(_mention(question, text),))
    out = normalize_intent(intent, _context(question=question, reference_time=reference))
    assert len(out.times) == 1
    normalized = out.times[0]
    assert (normalized.granularity, normalized.value) == (granularity, value)
    assert (normalized.start, normalized.end) == (start, end)


def test_unknown_time_expression_rejected():
    question = "上个猴年总交易额"
    intent = _intent(question=question, time_mentions=(_mention(question, "上个猴年"),))
    with pytest.raises(IntentContractError) as exc:
        normalize_intent(intent, _context(question=question))
    assert exc.value.reason_code == "time_expression_unknown"


# --- 时区固定时钟 ------------------------------------------------------------


def test_naive_reference_time_rejected():
    with pytest.raises(IntentContractError) as exc:
        normalize_intent(_intent(), _context(reference_time="2013-07-15T10:00:00"))
    assert exc.value.reason_code == "reference_time_timezone_required"


def test_invalid_timezone_rejected():
    with pytest.raises(IntentContractError) as exc:
        normalize_intent(_intent(), _context(timezone="Mars/Olympus"))
    assert exc.value.reason_code == "timezone_invalid"


# --- 数值解析与槽位保留 ------------------------------------------------------


def test_filter_values_parsed_with_chinese_units():
    question = "总交易额超过 100 万的客户"
    filt = IntentFilter(
        subject=_mention(question, "总交易额"),
        op="gt",
        values=(_mention(question, "100 万"),),
    )
    intent = _intent(question=question, filters=(filt,), groups=(_mention(question, "客户"),))
    out = normalize_intent(intent, _context(question=question))
    assert [(v.text, v.number, v.unit) for v in out.values] == [("100 万", 1_000_000, "万")]


def test_filter_value_without_number_kept_unparsed():
    question = "总交易额超过某阈值的客户"
    filt = IntentFilter(
        subject=_mention(question, "总交易额"),
        op="gt",
        values=(_mention(question, "某阈值"),),
    )
    intent = _intent(question=question, filters=(filt,), groups=(_mention(question, "客户"),))
    out = normalize_intent(intent, _context(question=question))
    assert len(out.values) == 1
    assert out.values[0].text == "某阈值"
    assert out.values[0].number is None


def test_normalized_keeps_sort_limit_and_filters():
    question = "2013 年第二季度总交易额前十的客户"
    filt = IntentFilter(
        subject=_mention(question, "总交易额"),
        op="eq",
        values=(_mention(question, "总交易额"),),
    )
    intent = _intent(
        question=question,
        filters=(filt,),
        sort={"direction": "desc", "by": _mention(question, "总交易额")},
        limit=10,
    )
    out = normalize_intent(intent, _context(question=question))
    assert out.limit == 10
    assert out.sort is not None and out.sort.direction == "desc"
    assert len(out.filters) == 1 and out.filters[0].op == "eq"


def test_mentions_keep_original_evidence():
    span = _span(QUESTION, "总交易额")
    intent = _intent(metric_mentions=(Mention(text="总交易额", evidence=(span,)),))
    out = normalize_intent(intent, _context())
    assert out.metric_mentions[0].evidence == (span,)
