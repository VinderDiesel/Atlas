"""ADR-0031 控制对象合同；草稿非权威，控制身份不代表数据授权。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

ObjectId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")]
GitSha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
DraftKind = Literal["semantic", "flow", "node_config"]
DraftStatus = Literal[
    "draft", "validated", "reviewed", "source_imported", "release_ready", "published", "retired"
]


class Contract(BaseModel):
    """拒绝未知字段；读取值对象不会直接改变持久状态。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Owner(Contract):
    """稳定所有者（issuer、subject），不是旧会话 claims 指纹。"""

    issuer: str = Field(min_length=1, max_length=2048)
    subject: str = Field(min_length=1, max_length=512)


class Draft(Contract):
    """非权威配置副本；每次内容变更递增 revision 并撤销当前放行状态。"""

    draft_id: ObjectId
    kind: DraftKind
    owner: Owner
    scope: ObjectId
    base_git_sha: GitSha
    revision: int = Field(ge=1)
    status: DraftStatus
    content: dict[str, JsonValue]
    content_digest: Digest
    created_at: str
    updated_at: str


def canonical_json(value: object) -> str:
    """返回可摘要的规范 JSON；非 JSON 值或非有限浮点数抛 ValueError/TypeError。"""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def content_digest(value: object) -> str:
    """返回 JSON 内容的 SHA-256；非法 JSON 值的异常向上传播。"""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# ---------- 运行事实与事件（ADR-0031 D07③） ----------

RunMode = Literal["ask", "analyze", "execute_plan"]
RunStatus = Literal["queued", "running", "succeeded", "blocked", "failed", "interrupted"]
ResultAvailability = Literal["pending", "available", "not_retained", "expired", "restricted"]
ResultKind = Literal["answer", "clarify", "handoff", "blocked", "error"]
EventType = Literal[
    "RUN_ACCEPTED",
    "RUN_STARTED",
    "NODE_STARTED",
    "NODE_FINISHED",
    "NODE_FAILED",
    "NODE_SKIPPED",
    "EDGE_TAKEN",
    "TOOL_STARTED",
    "TOOL_FINISHED",
    "FALLBACK",
    "STATE_SNAPSHOT",
    "RUN_FINISHED",
    "RUN_INTERRUPTED",
]
CaptureField = Literal["question", "node_io", "result"]
FeedbackVerdict = Literal["up", "down", "corrected"]
FeedbackStatus = Literal["pending_review", "approved", "rejected"]

TERMINAL_RUN_STATUSES: frozenset[str] = frozenset({"succeeded", "blocked", "failed", "interrupted"})
CAPTURE_RETENTION_DAYS = 7
ClientRequestId = Annotated[str, Field(min_length=1, max_length=128)]


class RunRecord(Contract):
    """运行事实（非正文）：状态机 queued → running → 终态，终态唯一且不可覆写。"""

    run_id: ObjectId
    owner: Owner
    deployment_id: ObjectId
    scope: ObjectId
    mode: RunMode
    session_id: str = Field(min_length=1, max_length=128)
    client_request_id: ClientRequestId
    request_digest: Digest
    release_id: str | None = None
    status: RunStatus
    result_kind: ResultKind | None = None
    result_availability: ResultAvailability
    replay_of: ObjectId | None = None
    last_seq: int = Field(ge=0)
    created_at: str
    updated_at: str


class RunEventRecord(Contract):
    """单一真实事件；seq 在同 run 内事务递增，occurred_at 由服务端时钟生成。"""

    schema_version: Literal[1] = 1
    run_id: ObjectId
    seq: int = Field(ge=1)
    event_id: ObjectId
    occurred_at: str
    node_id: str | None = None
    node_run_id: str | None = None
    parent_node_run_id: str | None = None
    attempt: int | None = Field(default=None, ge=0)
    event_type: EventType
    release_id: str | None = None
    payload: dict[str, JsonValue]


class CaptureGrant(Contract):
    """显式内容保留授权（D13）：字段白名单 + 带时区的保留期限；默认不捕获。"""

    purpose: str = Field(min_length=1, max_length=256)
    fields: frozenset[CaptureField] = Field(min_length=1)
    retain_until: str

    @model_validator(mode="after")
    def _explicit_timezone(self) -> CaptureGrant:
        """保留期限必须可解析且带显式时区——裸时间一律拒绝（AGENTS.md §7.3）。"""
        parsed = datetime.fromisoformat(self.retain_until)
        if parsed.tzinfo is None:
            raise ValueError("retain_until 必须带显式时区")
        return self


def validate_retain_until(
    value: str, *, now: datetime, max_days: int = CAPTURE_RETENTION_DAYS
) -> str:
    """保留期限不得超过捕获保留期上限（D07 默认 7 天）；超限抛 ValueError。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("retain_until 必须带显式时区")
    if parsed - now > timedelta(days=max_days):
        raise ValueError(f"retain_until 超出捕获保留上限（{max_days} 天）")
    return value


class ArtifactRecord(Contract):
    """显式捕获的私有制品；清理后 content/content_digest 为 None 且保留 cleaned_at。"""

    artifact_id: ObjectId
    run_id: ObjectId
    kind: Literal["capture"]
    purpose: str
    fields: frozenset[CaptureField]
    content: dict[str, JsonValue] | None = None
    content_digest: Digest | None = None
    retain_until: str
    cleaned_at: str | None = None
    created_at: str


class FeedbackRecord(Contract):
    """最小反馈采集；T12 审核前固定 pending_review / training_eligible=False。

    `attribution_node` 是可选的节点级归因（T12）：反馈可关联到具体节点类型
    （如 'rule_plan' / 'execute_plan'），用于归因枚举与训练分流。
    `reviewed_by` / `reviewed_at` 在审核动作时填充（T12）；提交时为 None。
    """

    feedback_id: ObjectId
    run_id: ObjectId
    owner: Owner
    verdict: FeedbackVerdict
    comment: str | None = None
    correction: dict[str, JsonValue] | None = None
    status: FeedbackStatus
    training_eligible: bool
    created_at: str
    attribution_node: str | None = None
    reviewed_by: Owner | None = None
    reviewed_at: str | None = None


# ---------- 提交与视图（ADR-0031 D13/D07） ----------

PlanGranularity = Literal["year", "quarter", "month", "date"]


class PlanTimeSpec(Contract):
    """Plan 时间范围镜像（agent.compiler.TimeSpec 字段同构）。"""

    granularity: PlanGranularity
    value: int | str


class PlanFilterSpec(Contract):
    """Plan 筛选条件镜像（agent.compiler.Filter 字段同构）。"""

    column: str = Field(min_length=1)
    op: str = Field(min_length=1)
    value: JsonValue


class PlanOrderSpec(Contract):
    """Plan 排序键镜像（agent.compiler.OrderSpec 字段同构）。"""

    column: str = Field(min_length=1)
    desc: bool = False


class PlanSpec(Contract):
    """Plan JSON 镜像（与既有 /plan/execute 的 CompileBody 同构，但不共享类）。

    控制面不得 import 服务面模块（serving.api 的私有 DTO 是 v1 契约的一部分）；
    字段与语义等价由 test_run_api 的同源用例锁定（同一 Plan 两路得同一 SQL）。
    """

    metric: str = Field(min_length=1)
    dimensions: list[str] = []
    time: PlanTimeSpec | None = None
    filters: list[PlanFilterSpec] = []
    order_by: list[PlanOrderSpec] = []
    limit: int = Field(default=100, ge=1, le=10_000)


class RunRequest(Contract):
    """POST /runs 提交合同（D13）：mode 与字段组合严格校验，未知字段一律拒绝。

    `client_request_id` 是客户端幂等键（1–128 非空，浏览器默认 UUID）——前端
    不得指定 run_id；`question`/`plan` 按 mode 互斥（ask 只收 question、
    execute_plan 只收 plan，analyze 由服务层显式拒绝至 T07 接通）。
    `capture` 是显式内容保留授权（D13）：省略/null = 不持久化正文；白名单与
    时区形态在本层校验，保留期上限由服务层以当前时钟兜底校验（422
    capture_invalid）——合同层不持有时钟。
    """

    deployment_id: ObjectId
    mode: RunMode
    client_request_id: ClientRequestId
    question: str | None = Field(default=None, min_length=1, max_length=500)
    plan: PlanSpec | None = None
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    capture: CaptureGrant | None = None

    @model_validator(mode="after")
    def _field_combinations(self) -> RunRequest:
        """mode ↔ 字段组合：缺失/越界都在合同层拒绝（422，不进入服务路径）。"""
        if self.mode == "execute_plan":
            if self.plan is None:
                raise ValueError("execute_plan 模式必须提供 plan")
            return self
        if self.plan is not None:
            raise ValueError(f"{self.mode} 模式不接受 plan（Plan 直执用 execute_plan）")
        if self.question is None:
            raise ValueError(f"{self.mode} 模式必须提供 question")
        return self


class RunReceipt(Contract):
    """POST /runs 的 202 收据（D13）：只回身份与发布绑定，不回正文。"""

    run_id: ObjectId
    status: RunStatus
    release_id: str | None = None


class FeedbackRequest(Contract):
    """POST /feedback 提交合同（D13）：反馈类型与原文引用严格校验，未知字段拒绝。

    `comment`/`correction` 都是可选的补充说明；审核状态与训练资格由服务端固定
    （pending_review / training_eligible=False），客户端无权指定。
    """

    run_id: ObjectId
    verdict: FeedbackVerdict
    comment: str | None = Field(default=None, max_length=2000)
    correction: dict[str, JsonValue] | None = None


class RunTraceSummary(Contract):
    """内容不可用时的安全摘要（D07）：保留结果种类枚举，不伪造答案。"""

    result_kind: ResultKind | None = None


class RunView(Contract):
    """GET /runs/{id} 固定 10 键视图（D07）；result 的裁剪口径见 RunService.view。"""

    run_id: ObjectId
    session_id: str
    release_id: str | None = None
    status: RunStatus
    result: dict[str, JsonValue] | None = None
    result_availability: ResultAvailability
    data_identity: dict[str, JsonValue] | None = None
    replay_of: ObjectId | None = None
    last_seq: int = Field(ge=0)
    trace_summary: RunTraceSummary


class RunSummary(Contract):
    """GET /runs 列表行固定 12 键（D13）：只含摘要，不含正文/问句/结果。"""

    run_id: ObjectId
    session_id: str
    deployment_id: ObjectId
    scope: ObjectId
    mode: RunMode
    status: RunStatus
    result_availability: ResultAvailability
    result_kind: ResultKind | None = None
    replay_of: ObjectId | None = None
    last_seq: int = Field(ge=0)
    created_at: str
    updated_at: str


class SessionSummary(Contract):
    """GET /sessions 目录行固定 6 键（D13）：控制库运行事实聚合，非 checkpoint dump。"""

    session_id: str = Field(min_length=1, max_length=128)
    deployment_id: ObjectId
    scope: ObjectId
    run_count: int = Field(ge=0)
    last_run_at: str
    last_status: RunStatus


# ---------- 源接入与探测（ADR-0031 D03/T07） ----------

SourceConnectorKind = Literal["doris"]
TlsPolicy = Literal["required", "disabled"]
ProbeStatus = Literal["ok", "blocked"]
# 探测阻塞理由（T07 绿测清单逐条对应）：凭据缺失、云元数据/link-local/未授权目标、
# TLS、表越界（配置自检）、只读未确认、元数据缺失（白名单表不存在）、
# 目标授权撤销（数据侧账号被拒）、连接失败。
ProbeBlockedReason = Literal[
    "credential_missing",
    "target_forbidden",
    "target_not_allowlisted",
    "tls_error",
    "table_out_of_whitelist",
    "read_only_unconfirmed",
    "metadata_missing",
    "credential_rejected",
    "connect_failed",
]
_OFFSET_RE = re.compile(r"[+-]\d{2}:\d{2}")
_OBJECT_NAME_RE = re.compile(r"[a-zA-Z0-9_.-]+")
_SECRET_REF_RE = re.compile(r"env:[A-Za-z_][A-Za-z0-9_]{0,127}")


class SourceRevisionRequest(Contract):
    """POST /manage/sources 提交合同（D03 SourceSpec 字段逐字对应）。

    秘密只收 `env:<NAME>` 环境变量引用（N9）：明文密码 / 完整 DSN / 未知字段
    在合同层一律 422，不进入服务路径。表白名单必须是 catalog.db.table 三段
    全名且首段 ∈ allowed_catalogs——表越界在接入前被拒（D03「接入前验证」）；
    timezone 必须显式 UTC 偏移。
    """

    source_id: ObjectId
    revision: str = Field(min_length=1, max_length=128)
    connector_kind: SourceConnectorKind
    secret_ref: str
    allowed_catalogs: list[str] = Field(min_length=1, max_length=64)
    allowed_tables: list[str] = Field(min_length=1, max_length=4096)
    timezone: str
    tls_policy: TlsPolicy = "required"
    query_budget: int = Field(ge=1, le=10_000_000)

    @model_validator(mode="after")
    def _self_consistent(self) -> SourceRevisionRequest:
        """引用形态与白名单自洽性：越界/重复/非法名在接入前拒绝。"""
        if not _SECRET_REF_RE.fullmatch(self.secret_ref):
            raise ValueError(
                "secret_ref 必须是环境变量引用（env:<NAME>）：秘密只由环境变量解析（N9）"
            )
        if not _OFFSET_RE.fullmatch(self.timezone):
            raise ValueError("timezone 必须是显式 UTC 偏移（如 +08:00）")
        catalogs = set(self.allowed_catalogs)
        if len(catalogs) != len(self.allowed_catalogs) or any(
            not _OBJECT_NAME_RE.fullmatch(name) for name in self.allowed_catalogs
        ):
            raise ValueError("allowed_catalogs 含重复或不合法条目")
        if len(set(self.allowed_tables)) != len(self.allowed_tables):
            raise ValueError("allowed_tables 含重复条目")
        for table in self.allowed_tables:
            parts = table.split(".")
            if len(parts) != 3 or any(
                not _OBJECT_NAME_RE.fullmatch(part) for part in parts
            ):
                raise ValueError(
                    f"allowed_tables 必须是 catalog.db.table 三段全名：{table!r}"
                )
            if parts[0] not in catalogs:
                raise ValueError(f"allowed_tables 越界（{table!r} 不属于 allowed_catalogs）")
        return self


class SourceRevisionRecord(Contract):
    """源修订行（追加式）：version 由控制库事务内分配，旧版本不可变。"""

    source_id: ObjectId
    version: int = Field(ge=1)
    revision: str = Field(min_length=1, max_length=128)
    connector_kind: SourceConnectorKind
    secret_ref: str
    allowed_catalogs: frozenset[str]
    allowed_tables: frozenset[str]
    timezone: str
    tls_policy: TlsPolicy
    query_budget: int = Field(ge=1)
    created_by: Owner
    created_at: str


class SourceProbeSummary(Contract):
    """探测证据摘要（D03：有时间戳的证据，不是永久保证）；不回显凭据或连接串。"""

    probe_id: ObjectId
    status: ProbeStatus
    blocked_reason: ProbeBlockedReason | None = None
    observed_at: str


class SourceRevisionView(Contract):
    """GET/POST /manage/sources 行固定 13 键：引用名可回显，秘密与 DSN 永不回显。"""

    source_id: ObjectId
    version: int = Field(ge=1)
    revision: str
    connector_kind: SourceConnectorKind
    secret_ref: str
    allowed_catalogs: list[str]
    allowed_tables: list[str]
    timezone: str
    tls_policy: TlsPolicy
    query_budget: int
    created_by: Owner
    created_at: str
    last_probe: SourceProbeSummary | None = None


class ProbeCapabilities(Contract):
    """探测确认的能力（D03 证据化）：只声明本次探测证实的能力，其余一律 False。"""

    dialect: str = Field(min_length=1, max_length=32)
    read_only: bool
    metadata_probe: bool
    cancel_query: bool
    snapshot_read: bool
    consistent_analysis: bool


class ProbeResult(Contract):
    """探测结果（POST /manage/sources/{id}/probes；固定 10 键）。

    时间戳/摘要/能力都是**证据**而非永久保证（D03）：`reproducible` 恒为
    False——历史结论不当作本次事实（N2）。
    """

    probe_id: ObjectId
    source_id: ObjectId
    version: int = Field(ge=1)
    status: ProbeStatus
    blocked_reason: ProbeBlockedReason | None = None
    observed_at: str
    engine_version: str | None = None
    schema_digest: Digest | None = None
    capabilities: ProbeCapabilities
    reproducible: Literal[False] = False


# ---------- 部署绑定（ADR-0031 D04/D13；T07c 创建 draft 供 T08 发布绑定） ----------


class DeploymentRequest(Contract):
    """POST /manage/deployments 提交合同：只登记 `(deployment_id, scope, source_id)`。

    不允许夹带 `active_release_id`（未知字段 422）——首次发布是 T08 的显式
    CAS 动作，创建不是隐式发布（D04/D13）。
    """

    deployment_id: ObjectId
    scope: ObjectId
    source_id: ObjectId


class DeploymentRecord(Contract):
    """部署指针行（控制库事实，即读视图，固定 8 键）。

    `active_release_id` 在首次批准发布前为 None（D13 逐字）；`revision` 是
    CAS 指针版本（发布/回退递增），不是制品内容版本。
    """

    deployment_id: ObjectId
    scope: ObjectId
    source_id: ObjectId
    active_release_id: ObjectId | None = None
    revision: int = Field(ge=1)
    created_by: Owner
    created_at: str
    updated_at: str


# ---------- 语义草稿（ADR-0031 T08；草稿非权威，激活只能经 T08b 的 CAS 发布） ----------


class DraftRequest(Contract):
    """POST /manage/drafts 提交合同（D13）：content 形状（target/document）由服务层门禁。

    草稿是编辑中的非权威副本：创建/编辑不改变任何运行 Metric 与发布指针；
    `base_git_sha` 由服务端从本地 HEAD 取，客户端不得声称。
    """

    kind: DraftKind
    scope: ObjectId
    content: dict[str, JsonValue]


class DraftEditRequest(Contract):
    """PUT /manage/drafts/{id} 提交合同：内容整体替换，修订由 If-Match 头 CAS。"""

    content: dict[str, JsonValue]


ReviewDecision = Literal["approved", "rejected"]
ValidationStatus = Literal["passed", "failed"]
# 发现归因到三套确定性校验器之一（结构 ossie_validate / 治理 governance_validate /
# 策略一致性 check_policy_consistency）；不引入第四类模糊结论。
FindingCode = Literal["structure", "governance", "policy"]


class DraftReviewRequest(Contract):
    """POST /manage/drafts/{id}/reviews 提交合同：决定 + 可选意见（绑定当前摘要）。"""

    decision: ReviewDecision
    comment: str | None = Field(default=None, max_length=2000)


class ValidationFinding(Contract):
    """单条确定性校验发现：code 标明产生它的校验器，message 为校验器原文。"""

    code: FindingCode
    message: str = Field(min_length=1)


class DraftValidation(Contract):
    """校验证据行（固定 8 键）：绑定 draft 的 revision 与内容摘要，可重复审计。"""

    validation_id: ObjectId
    draft_id: ObjectId
    revision: int = Field(ge=1)
    content_digest: Digest
    status: ValidationStatus
    findings: list[ValidationFinding]
    actor: Owner
    created_at: str


class DraftReview(Contract):
    """人工审核证据行（固定 8 键）：只记录决定；approved 的状态推进由服务层绑定摘要。"""

    review_id: ObjectId
    draft_id: ObjectId
    revision: int = Field(ge=1)
    content_digest: Digest
    decision: ReviewDecision
    comment: str | None = None
    actor: Owner
    created_at: str


class PatchImpact(Contract):
    """导出补丁的影响面摘要（固定 6 键）：指标按全局 name、维度按 `dataset.field`。"""

    added_metrics: list[str]
    removed_metrics: list[str]
    changed_metrics: list[str]
    added_dimensions: list[str]
    removed_dimensions: list[str]
    changed_dimensions: list[str]


class DraftPatchView(Contract):
    """GET /manage/drafts/{id}/patch 视图（固定 7 键）：最小统一 diff + 影响面。"""

    draft_id: ObjectId
    revision: int = Field(ge=1)
    content_digest: Digest
    base_git_sha: GitSha
    target: str
    patch: str
    impact: PatchImpact


# ---------- 发布制品与激活（ADR-0031 T08b；只接受通过门禁的 Git 制品） ----------


class ReleaseImportRequest(Contract):
    """POST /manage/releases/imports 提交合同：显式 commit + 目标源。

    `source_git_sha` 必须是完整 40 hex 且对象库可达（服务层再验）；导入是"把已
    审核草稿变成不可变制品"的显式动作，不接受工作树/索引/引用作为发布源。
    """

    draft_id: ObjectId
    source_git_sha: GitSha
    source_id: ObjectId


class ReleaseImportView(Contract):
    """导入响应（固定 7 键）：制品身份 + 草稿推进后的状态（release_ready）。"""

    release_id: Digest
    content_digest: Digest
    draft_id: ObjectId
    draft_revision: int = Field(ge=1)
    status: Literal["release_ready"]
    target: str
    created_at: str


class ActivationRequest(Contract):
    """POST /manage/deployments/{id}/releases|rollbacks 提交合同（D04 CAS）。

    `expected_active_release_id` 是当前指针（首发为 null）；不匹配一律 409，
    不静默覆盖他人发布。
    """

    release_id: Digest
    expected_active_release_id: Digest | None = None


class ActivationView(Contract):
    """激活响应（固定 6 键）：action 标明发布/回退，previous 是切换前指针。"""

    deployment_id: ObjectId
    action: Literal["publish", "rollback"]
    previous_release_id: ObjectId | None = None
    active_release_id: ObjectId
    revision: int = Field(ge=1)
    updated_at: str


class ReleaseRecord(Contract):
    """发布登记行（固定 8 键，脱敏视图）：只有 Manifest 与身份，无模型正文/凭据。"""

    release_id: Digest
    content_digest: Digest
    scope: ObjectId
    source_id: ObjectId
    source_revision: str = Field(min_length=1, max_length=128)
    manifest: dict[str, JsonValue]
    created_by: Owner
    created_at: str
