"""ADR-0031 D09 检索前语义意图合同：模型只描述业务意图。

范围与边界
----------
- 合同是**模型无关**的 Schema/证据校验底座：`schema_version=1`、`extra=forbid`；
  `EvidenceSpan` 为原文字符半开区间，来源只能是原问题或已授权上下文（可校验性
  由 `agent.intent.normalize` 逐条执行）。
- 模型**只描述业务意图**：`Mention.text` 是用户原话片段，不是 Metric 名或物理列；
  Metric/Dimension 的取名与可达性由 `retrieve`/`bind`（T10b/T10c）承接，本模块
  不含任何 SemanticModel 引用。
- 时间数值换算、排序与 limit 规范化由确定性代码完成（D09）：相对时间依赖固定
  `reference_time` + IANA `timezone`（时区固定时钟），`NormalizedIntent.times`
  只装确定性区间；无法识别的表达式一律拒绝，不静默丢弃。
- `CandidateSet`/`BindingResult` 为服务端检索/绑定产物（非模型输出）；
  未绑定条件只能澄清，不执行（D09「有未绑定条件只能澄清」）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.compiler import Plan

SCHEMA_VERSION: Final[Literal[1]] = 1

# 语义身份统一 64 位小写 hex（与 serving/control、agent/runtime 同口径）。
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ReasonCode = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]

# D09 词表。
Task = Literal["query", "compare", "attribution", "clarify", "unsupported"]
FilterOp = Literal["eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte"]
SortDirection = Literal["asc", "desc"]
TimeGranularity = Literal["year", "quarter", "month", "day"]


class IntentContractError(ValueError):
    """意图合同/证据校验失败：fail-closed，不静默降级为普通查询。"""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        message = f"{reason_code}: {detail}" if detail else reason_code
        super().__init__(message)
        self.reason_code = reason_code
        self.detail = detail


class _Contract(BaseModel):
    """D09 公共形状：未知字段拒绝（extra=forbid），实例不可变。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceSpan(_Contract):
    """原文字符半开区间：`source_text[start:end]` 必须可校验。"""

    source_id: str = Field(min_length=1, max_length=64)
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.end <= self.start:
            raise ValueError("半开区间不能为空：end 必须大于 start")
        return self


class Mention(_Contract):
    """业务意图提及：text 为来源片段原文（不是 Metric 名）。"""

    text: str = Field(min_length=1, max_length=256)
    evidence: tuple[EvidenceSpan, ...] = Field(min_length=1)


class RetrievalQuery(_Contract):
    """检索表达：原句逐字保留；扩展文本是生成表达，evidence 指回触发片段。"""

    text: str = Field(min_length=1, max_length=512)
    evidence: tuple[EvidenceSpan, ...] = Field(min_length=1)


class IntentFilter(_Contract):
    """意图过滤条件：subject/values 均为来源片段提及。"""

    subject: Mention
    op: FilterOp
    values: tuple[Mention, ...] = Field(min_length=1)


class SortSpec(_Contract):
    """排序意图：by 为提及（Metric 或维度由 bind 解析）。"""

    direction: SortDirection
    by: Mention


class Ambiguity(_Contract):
    """歧义声明：必须带证据（歧义不强猜）。"""

    slot: str = Field(min_length=1, max_length=128)
    reason_code: ReasonCode
    evidence: tuple[EvidenceSpan, ...] = Field(min_length=1)


class SemanticIntent(_Contract):
    """模型输出的业务意图（D09）：不创造 Metric 名或物理列。"""

    schema_version: Literal[1]
    task: Task
    metric_mentions: tuple[Mention, ...] = ()
    groups: tuple[Mention, ...] = ()
    filters: tuple[IntentFilter, ...] = ()
    time_mentions: tuple[Mention, ...] = ()
    sort: SortSpec | None = None
    limit: int | None = Field(default=None, ge=1)
    ambiguities: tuple[Ambiguity, ...] = ()
    retrieval_queries: tuple[RetrievalQuery, ...] = ()


class QuestionContext(_Contract):
    """问题上下文：检索/归一/绑定的确定性输入（服务端构造，非模型输出）。"""

    schema_version: Literal[1] = SCHEMA_VERSION
    question: str = Field(min_length=1, max_length=1024)
    locale: str = Field(min_length=2, max_length=16)
    authorized_context: dict[str, str] = Field(default_factory=dict)
    catalog_digest: Digest
    catalog_summary: str = ""
    reference_time: datetime
    timezone: str = Field(min_length=1, max_length=64)
    capabilities: tuple[str, ...] = ()


class Candidate(_Contract):
    """候选指标：sources 记录命中的检索表达，evidence_refs 为命中证据。"""

    semantic_id: str = Field(min_length=1, max_length=128)
    rank: int = Field(ge=1)
    sources: tuple[str, ...] = Field(min_length=1)
    evidence_refs: tuple[EvidenceSpan, ...] = ()


class CandidateSet(_Contract):
    """检索产物：目录摘要绑定（catalog_digest），去重合并后 K=5（D09）。"""

    schema_version: Literal[1] = SCHEMA_VERSION
    catalog_digest: Digest
    candidates: tuple[Candidate, ...]


class SlotBinding(_Contract):
    """意图槽位 → Plan 字段的映射证据（unresolved_slots 用 "filters.0" 形态）。"""

    slot: str = Field(min_length=1, max_length=128)
    plan_field: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=512)


class BindingResult(_Contract):
    """绑定产物：有未绑定条件时 plan_candidate 必须为 None（只能澄清）。"""

    schema_version: Literal[1] = SCHEMA_VERSION
    plan_candidate: Plan | None
    slot_bindings: tuple[SlotBinding, ...] = ()
    unresolved_slots: tuple[str, ...] = ()
    reason_code: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _check_unresolved(self) -> Self:
        if self.unresolved_slots and self.plan_candidate is not None:
            raise ValueError("有未绑定条件时不允许携带 plan_candidate（只能澄清）")
        return self


class NormalizedTime(_Contract):
    """确定性的时间区间（闭区间，ISO 日期）：相对时间已按 fixed clock 换算。"""

    mention_text: str = Field(min_length=1, max_length=128)
    granularity: TimeGranularity
    value: int | str
    start: str = Field(min_length=10, max_length=10)
    end: str = Field(min_length=10, max_length=10)


class NormalizedValue(_Contract):
    """确定性的数值解析：无法数值化时 number=None（保留原文，不静默丢弃）。"""

    text: str = Field(min_length=1, max_length=256)
    number: int | float | None = None
    unit: str | None = None


class NormalizedIntent(_Contract):
    """归一产物（D09）：继承槽位 + 确定性时间/数值 + 证据保留。"""

    schema_version: Literal[1] = SCHEMA_VERSION
    task: Task
    metric_mentions: tuple[Mention, ...] = ()
    groups: tuple[Mention, ...] = ()
    filters: tuple[IntentFilter, ...] = ()
    time_mentions: tuple[Mention, ...] = ()
    sort: SortSpec | None = None
    limit: int | None = None
    ambiguities: tuple[Ambiguity, ...] = ()
    retrieval_queries: tuple[RetrievalQuery, ...] = ()
    times: tuple[NormalizedTime, ...] = ()
    values: tuple[NormalizedValue, ...] = ()
