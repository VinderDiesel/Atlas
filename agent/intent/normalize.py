"""ADR-0031 D09 确定性归一：模型无关证据校验 + 时间/数值换算 + 检索预算。

职责边界
--------
- 只校验与换算，不补全：证据引用不忠实（来源不可达/越界/文本不符）一律
  `IntentContractError` 拒绝，不静默丢弃槽位；无法识别的相对/显式时间表达式
  同样拒绝（不猜测）。
- 时区固定时钟：`reference_time` 必须带时区，`timezone` 必须为合法 IANA 名；
  相对时间（上月/本季度/去年…）按 reference_time 在该时区的本地日历值换算。
- 检索预算（D09）：原句必保留且置于首位（由系统重建，带完整句证据），
  扩展表达最多 2 条；超限拒绝而不是截断。
- 数值解析只做确定性量级换算（"100 万" → 1000000，支持万/亿）；无法数值化
  时保留原文并置 `number=None`（是否可绑定由 bind 层判定，不在这里猜测）。
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent.intent.contracts import (
    EvidenceSpan,
    IntentContractError,
    Mention,
    NormalizedIntent,
    NormalizedTime,
    NormalizedValue,
    QuestionContext,
    RetrievalQuery,
    SemanticIntent,
)

# 检索预算（D09）：扩展表达上限（原句不计）。
MAX_RETRIEVAL_EXPANSIONS = 2

# 相对时间词表（偏移为自然月/季/年，基于 reference_time 的本地日历值）。
_MONTH_OFFSETS = {"上月": -1, "上个月": -1, "本月": 0, "这个月": 0}
_QUARTER_OFFSETS = {"上季度": -1, "上个季度": -1, "本季度": 0}
_YEAR_OFFSETS = {"去年": -1, "今年": 0}

# 显式时间表达（年/季/月/日）。
_EXPLICIT_YEAR = re.compile(r"^(\d{4}) ?年$")
_EXPLICIT_QUARTER_CN = re.compile(r"^(\d{4}) ?年第([一二三四])季度$")
_EXPLICIT_QUARTER_Q = re.compile(r"^(\d{4})Q([1-4])$")
_EXPLICIT_MONTH = re.compile(r"^(\d{4}) ?年(\d{1,2}) ?月$")
_EXPLICIT_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_CN_QUARTER_DIGIT = {"一": 1, "二": 2, "三": 3, "四": 4}

# 数值量级（与 agent/planner._cn_number 同口径的确定性换算；本模块不依赖
# LocaleRules 实例，供意图归一独立使用）。
_NUMERIC = re.compile(r"^(\d+(?:\.\d+)?)\s*(万|亿)?$")
_UNIT_FACTORS = {"万": 10_000, "亿": 100_000_000}


def normalize_intent(intent: SemanticIntent, context: QuestionContext) -> NormalizedIntent:
    """归一意图：证据校验 → 检索预算 → 时间/数值确定性换算。

    参数
    ----
    intent  : 模型输出的 `SemanticIntent`（合同形状已由 pydantic 保证）。
    context : 问题上下文（固定时钟 + 授权来源）。

    返回
    ----
    `NormalizedIntent`：槽位原样保留（含证据），检索表达规范化（原句首位），
    `times`/`values` 为确定性区间与数值。

    抛出
    ----
    IntentContractError：时钟非法、证据不可校验、检索扩展超限、时间表达式
    无法识别——fail-closed，不静默降级。
    """
    local_now = _check_clock(context)
    sources = _build_sources(context)
    _validate_evidence(intent, sources)
    queries = _normalize_queries(intent, context)
    times = tuple(_normalize_time(mention.text, local_now) for mention in intent.time_mentions)
    values = _normalize_values(intent)
    return NormalizedIntent(
        task=intent.task,
        metric_mentions=intent.metric_mentions,
        groups=intent.groups,
        filters=intent.filters,
        time_mentions=intent.time_mentions,
        sort=intent.sort,
        limit=intent.limit,
        ambiguities=intent.ambiguities,
        retrieval_queries=queries,
        times=times,
        values=values,
    )


# --- 时钟与来源 ---------------------------------------------------------------


def _check_clock(context: QuestionContext) -> datetime:
    """校验时区固定时钟，返回 reference_time 在声明时区的本地时刻。"""
    reference = context.reference_time
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise IntentContractError(
            "reference_time_timezone_required", "reference_time 必须带时区（时区固定时钟）"
        )
    try:
        timezone = ZoneInfo(context.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise IntentContractError(
            "timezone_invalid", f"未知 IANA 时区：{context.timezone}"
        ) from exc
    return reference.astimezone(timezone)


def _build_sources(context: QuestionContext) -> dict[str, str]:
    sources = {"question": context.question}
    sources.update(context.authorized_context)
    return sources


# --- 证据校验 -----------------------------------------------------------------


def _validate_evidence(intent: SemanticIntent, sources: dict[str, str]) -> None:
    for mention in (
        *intent.metric_mentions,
        *intent.groups,
        *intent.time_mentions,
        *(f.subject for f in intent.filters),
        *(v for f in intent.filters for v in f.values),
    ):
        _check_mention(mention, sources)
    if intent.sort is not None:
        _check_mention(intent.sort.by, sources)
    for ambiguity in intent.ambiguities:
        for span in ambiguity.evidence:
            _check_span(span, sources)
    for query in intent.retrieval_queries:
        for span in query.evidence:
            _check_span(span, sources)


def _check_span(span: EvidenceSpan, sources: dict[str, str]) -> str:
    """校验来源可达与范围；返回该来源文本（供文本忠实性比对）。"""
    if span.source_id not in sources:
        raise IntentContractError(
            "evidence_source_unknown",
            f"来源 {span.source_id!r} 不在原问题或已授权上下文中",
        )
    text = sources[span.source_id]
    if span.end > len(text):
        raise IntentContractError(
            "evidence_out_of_range",
            f"{span.source_id}[{span.start}:{span.end}] 越界（长度 {len(text)}）",
        )
    return text


def _check_mention(mention: Mention, sources: dict[str, str]) -> None:
    for span in mention.evidence:
        text = _check_span(span, sources)
        if text[span.start : span.end] != mention.text:
            raise IntentContractError(
                "evidence_mismatch",
                f"{span.source_id}[{span.start}:{span.end}] 与提及文本 {mention.text!r} 不一致",
            )


# --- 检索预算 -----------------------------------------------------------------


def _normalize_queries(
    intent: SemanticIntent, context: QuestionContext
) -> tuple[RetrievalQuery, ...]:
    question = context.question
    expansions: list[RetrievalQuery] = []
    seen: set[str] = {question}
    for query in intent.retrieval_queries:
        if query.text in seen:
            continue
        seen.add(query.text)
        expansions.append(query)
    if len(expansions) > MAX_RETRIEVAL_EXPANSIONS:
        raise IntentContractError(
            "retrieval_budget_exceeded",
            f"扩展检索表达最多 {MAX_RETRIEVAL_EXPANSIONS} 条，得到 {len(expansions)}",
        )
    original = RetrievalQuery(
        text=question,
        evidence=(EvidenceSpan(source_id="question", start=0, end=len(question)),),
    )
    return (original, *expansions)


# --- 时间换算 -----------------------------------------------------------------


def _normalize_time(text: str, local_now: datetime) -> NormalizedTime:
    if text in _MONTH_OFFSETS:
        year, month = _shift_month(local_now.year, local_now.month, _MONTH_OFFSETS[text])
        return _month_time(text, year, month)
    if text in _QUARTER_OFFSETS:
        quarter_index = (local_now.month - 1) // 3
        total = local_now.year * 4 + quarter_index + _QUARTER_OFFSETS[text]
        return _quarter_time(text, total // 4, total % 4 + 1)
    if text in _YEAR_OFFSETS:
        return _year_time(text, local_now.year + _YEAR_OFFSETS[text])

    match = _EXPLICIT_YEAR.match(text)
    if match:
        return _year_time(text, int(match.group(1)))
    match = _EXPLICIT_QUARTER_CN.match(text)
    if match:
        return _quarter_time(text, int(match.group(1)), _CN_QUARTER_DIGIT[match.group(2)])
    match = _EXPLICIT_QUARTER_Q.match(text)
    if match:
        return _quarter_time(text, int(match.group(1)), int(match.group(2)))
    match = _EXPLICIT_MONTH.match(text)
    if match:
        return _month_time(text, int(match.group(1)), int(match.group(2)))
    match = _EXPLICIT_DATE.match(text)
    if match:
        return NormalizedTime(
            mention_text=text, granularity="day", value=text, start=text, end=text
        )
    raise IntentContractError("time_expression_unknown", f"无法确定性归一的时间表达：{text!r}")


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + offset
    return total // 12, total % 12 + 1


def _month_time(text: str, year: int, month: int) -> NormalizedTime:
    last_day = monthrange(year, month)[1]
    return NormalizedTime(
        mention_text=text,
        granularity="month",
        value=year * 100 + month,
        start=f"{year:04d}-{month:02d}-01",
        end=f"{year:04d}-{month:02d}-{last_day:02d}",
    )


def _quarter_time(text: str, year: int, quarter: int) -> NormalizedTime:
    first_month = (quarter - 1) * 3 + 1
    last_month = quarter * 3
    last_day = monthrange(year, last_month)[1]
    return NormalizedTime(
        mention_text=text,
        granularity="quarter",
        value=f"{year:04d}Q{quarter}",
        start=f"{year:04d}-{first_month:02d}-01",
        end=f"{year:04d}-{last_month:02d}-{last_day:02d}",
    )


def _year_time(text: str, year: int) -> NormalizedTime:
    return NormalizedTime(
        mention_text=text,
        granularity="year",
        value=year,
        start=f"{year:04d}-01-01",
        end=f"{year:04d}-12-31",
    )


# --- 数值换算 -----------------------------------------------------------------


def _normalize_values(intent: SemanticIntent) -> tuple[NormalizedValue, ...]:
    return tuple(_normalize_value(mention.text) for f in intent.filters for mention in f.values)


def _normalize_value(text: str) -> NormalizedValue:
    match = _NUMERIC.match(text)
    if not match:
        return NormalizedValue(text=text)
    number = float(match.group(1)) * _UNIT_FACTORS.get(match.group(2), 1)
    return NormalizedValue(
        text=text,
        number=int(number) if number.is_integer() else number,
        unit=match.group(2),
    )
