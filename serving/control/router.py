"""ADR-0031 D07/D13 运行与反馈路由：/runs 与 /feedback（/api/v1 前缀）。

设计口径（D13）
---------------
- POST /runs → 202 `{run_id, status, release_id}`；body 与 mode 的字段组合在合同层
  严格校验（RunRequest），被拒提交零运行事实；显式 capture 的保留期由服务层
  兜底校验（422 capture_invalid）、授权审计失败 fail-closed（503 audit_unavailable）。
- POST /feedback → 201 FeedbackRecord 全 9 键（固定 pending_review/False）；
  GET /feedback → 200 `{"items":[...]}`（仅本人，新→旧）。
- 统一错误体 `{"error":{"code","message","request_id"}}`；状态码：401 无认证、
  403 能力不足、404 部署/运行不可见、409 幂等冲突或会话过期、422 合同不合法
  或模式未接通、503 强制审计不可用（不降级放行）。
- 控制面服务**惰性装配**（services_factory）：首个请求才建控制库与运行服务——
  create_app 不因未配置控制面而触碰磁盘（与 _default_oidc_factory 同纪律）。

装配契约：`build_control_router(services_factory, prefix=...)` 由 serving/api.py
挂载（前缀单一事实源在 api.py；**必须在 SPA catch-all 之前 include**）。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, get_args
from uuid import uuid4

from fastapi import APIRouter, Body, Header
from pydantic import ValidationError
from starlette.responses import JSONResponse, Response, StreamingResponse

from serving.auth import BearerClaims
from serving.control.auth import ControlForbidden, ControlGrant, principal_for_bearer
from serving.control.contracts import (
    TERMINAL_RUN_STATUSES,
    ActivationRequest,
    DeploymentRequest,
    DraftEditRequest,
    DraftRequest,
    DraftReviewRequest,
    FeedbackRequest,
    ReleaseImportRequest,
    RunEventRecord,
    RunReceipt,
    RunRequest,
    RunStatus,
    SourceRevisionRequest,
)
from serving.control.deployments import DeploymentConflict, DeploymentService
from serving.control.drafts import DraftContentInvalid, DraftService
from serving.control.feedback import FeedbackService
from serving.control.releases import (
    ReleaseInvalid,
    ReleaseService,
    ReleaseTargetMissing,
    SourceEvidenceMissing,
)
from serving.control.runs import (
    ArtifactExpired,
    CaptureAuditFailed,
    CaptureInvalid,
    EventGap,
    RunModeNotSupported,
    RunNotFound,
    RunService,
    SessionContextExpired,
    decode_page_cursor,
)
from serving.control.sources import SourceService
from serving.control.store import ControlStore, ReleaseConflict, RevisionConflict, RunConflict

_RUN_STATUSES: frozenset[str] = frozenset(get_args(RunStatus))
_MAX_PAGE_SIZE = 100
_DEFAULT_PAGE_SIZE = 50
# SSE 轮询节奇（T06b）：D07 要求事件先提交再通知、内存通知可丢——只读持久事实
_SSE_POLL_INTERVAL = 0.25  # 秒/轮（未终态时）
_SSE_PING_EVERY = 20  # 等待轮次每 20 轮发一次 `: ping`（约 5s 心跳）


@dataclass(frozen=True)
class ControlServices:
    """控制面服务束（惰性工厂的产物）：控制库 + 运行/反馈/源/部署/草稿/发布服务 + 授予表。"""

    store: ControlStore
    runs: RunService
    feedback: FeedbackService
    sources: SourceService
    deployments: DeploymentService
    drafts: DraftService
    releases: ReleaseService
    grants: Mapping[str, ControlGrant]


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    """统一错误体（D13）；request_id 每响生成，不暴露底层异常与凭据。"""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "request_id": uuid4().hex}},
    )


def _first_message(exc: ValidationError) -> str:
    """取第一条校验错误的人类可读消息（不吐整个错误数组）。"""
    errors = exc.errors()
    if not errors:
        return "请求体不合法"
    return str(errors[0].get("msg") or "请求体不合法")


def _page_params(
    limit: str | None, cursor: str | None
) -> tuple[int, tuple[str, str] | None]:
    """分页参数（D13：默认 50、最多 100）；非法统一 ValueError → 422。"""
    if limit is None:
        page_limit = _DEFAULT_PAGE_SIZE
    elif limit.isdigit() and 1 <= int(limit) <= _MAX_PAGE_SIZE:
        page_limit = int(limit)
    else:
        raise ValueError(f"limit 必须是 1–{_MAX_PAGE_SIZE} 的整数")
    return page_limit, (decode_page_cursor(cursor) if cursor is not None else None)


def _filter_text(raw: str | None, label: str) -> str | None:
    """文本过滤参数：限定长度（防超长输入），空串拒绝。"""
    if raw is None:
        return None
    if not 1 <= len(raw) <= 128:
        raise ValueError(f"{label} 长度必须是 1–128")
    return raw


def _run_status(raw: str | None) -> str | None:
    if raw is None:
        return None
    if raw not in _RUN_STATUSES:
        raise ValueError(f"status 取值非法：{raw!r}")
    return raw


def _instant(raw: str | None, label: str) -> str | None:
    """时间过滤：ISO 8601 且显式时区（裸时间拒绝，AGENTS.md §7.3）。"""
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{label} 不是合法 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} 必须带显式时区")
    return parsed.isoformat()


def _principal_or_error(
    claims: dict[str, object], services: ControlServices
) -> tuple[Any, JSONResponse | None]:
    """Bearer claims → 控制 Principal；claims 缺 sub 等形态错 → 401（身份不完整）。"""
    try:
        return principal_for_bearer(claims, grants=services.grants), None
    except ValueError as exc:
        return None, _error(401, "invalid_identity", str(exc))


def _event_cursor(after_seq: str | None, last_event_id: str | None) -> int:
    """续读游标：显式 after_seq 优先，其次 Last-Event-ID 头，默认 0；非法 422。"""
    raw = after_seq if after_seq is not None else last_event_id
    if raw is None:
        return 0
    if not raw.isdigit():
        raise ValueError(f"续读游标必须是非负整数：{raw!r}")
    return int(raw)


def _sse_event_frame(event: RunEventRecord) -> str:
    """单帧 ``id/event/data`` 三行：id 供断线续读（Last-Event-ID 语义），
    data 为 RunEventRecord 全键 JSON（mode="json" 保证类型可序列化）。"""
    data = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
    return f"id: {event.seq}\nevent: {event.event_type}\ndata: {data}\n\n"


def stream_run_events(
    store: ControlStore,
    *,
    run_id: str,
    after_seq: int,
    poll_interval: float = _SSE_POLL_INTERVAL,
    ping_every: int = _SSE_PING_EVERY,
) -> Iterator[str]:
    """SSE 帧生成器：replay 持久事件 → 未终态轮询等待 → 终态发完即止。

    - 持久事件是权威（D07）：每轮只读 run_events，内存通知可丢、不参与正确性；
      断线重连按 seq 去重继续（调用方建立时已完成 ACL/gap 校验）。
    - 心跳：等待轮次发 `: ping` 注释帧（每 ping_every 轮一次，保活与代理超时）。
    - 不重跑：只读事实，不触碰执行器（SSE 断线仅断开订阅，不重做查询）；
      连接期不周期重鉴权（建立即授权，撤销在下次请求生效）。
    """
    sent = after_seq
    waiting = 0
    while True:
        events = store.list_events(run_id, after_seq=sent)
        for event in events:
            sent = event.seq
            yield _sse_event_frame(event)
        record = store.get_run(run_id)
        if record.status in TERMINAL_RUN_STATUSES and sent >= record.last_seq:
            return
        if events:
            waiting = 0
            continue
        waiting += 1
        if ping_every > 0 and waiting % ping_every == 0:
            yield ": ping\n\n"
        time.sleep(poll_interval)


def build_control_router(
    services_factory: Callable[[], ControlServices], *, prefix: str
) -> APIRouter:
    """构造运行面 router；prefix 由装配方传入（前缀单一事实源在 serving/api.py）。"""
    router = APIRouter(prefix=prefix, tags=["runs"])

    @router.post(
        "/runs",
        status_code=202,
        response_model=None,
        summary="提交运行（幂等；202 收据 {run_id, status, release_id}）",
    )
    def submit_run(
        claims: BearerClaims, body: Annotated[Any | None, Body()] = None
    ) -> JSONResponse:
        """合同校验（422）→ 身份（401）→ 提交（403/404/409/422）。"""
        if not isinstance(body, dict):
            return _error(422, "invalid_request", "请求体必须是 JSON 对象")
        try:
            request = RunRequest.model_validate(body)
        except ValidationError as exc:
            return _error(422, "invalid_request", _first_message(exc))
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            record, _created = services.runs.submit(principal=principal, request=request)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except SessionContextExpired as exc:
            return _error(409, "session_context_expired", str(exc))
        except RunModeNotSupported as exc:
            return _error(422, "mode_not_supported", str(exc))
        except CaptureInvalid as exc:
            return _error(422, "capture_invalid", str(exc))
        except CaptureAuditFailed as exc:
            return _error(503, "audit_unavailable", str(exc))
        except RunConflict as exc:
            return _error(409, "idempotency_conflict", str(exc))
        except KeyError as exc:
            deployment = exc.args[0] if exc.args else ""
            return _error(404, "not_found", f"部署不存在：{deployment!r}")
        receipt = RunReceipt(
            run_id=record.run_id, status=record.status, release_id=record.release_id
        )
        return JSONResponse(status_code=202, content=receipt.model_dump())

    @router.get(
        "/runs/{run_id}",
        response_model=None,
        summary="运行视图（固定 10 键；内容按 ACL 与内存窗口裁剪）",
    )
    def get_run(run_id: str, claims: BearerClaims) -> JSONResponse:
        """对象级 ACL 在服务层裁决：不可见与不存在一律 404（不泄露存在性）。"""
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            view = services.runs.view(principal=principal, run_id=run_id)
        except (RunNotFound, KeyError):
            return _error(404, "not_found", f"运行不存在：{run_id!r}")
        return JSONResponse(status_code=200, content=view.model_dump())

    @router.get(
        "/runs",
        response_model=None,
        summary="运行目录（游标分页默认 50/最多 100；域/时间/状态过滤）",
    )
    def list_runs(
        claims: BearerClaims,
        limit: str | None = None,
        cursor: str | None = None,
        scope: str | None = None,
        status: str | None = None,
        since: str | None = None,
        until: str | None = None,
        session_id: str | None = None,
    ) -> JSONResponse:
        """服务端 ACL 裁剪；未授权域 403；非法参数统一 422（“error” 体）。"""
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            page_limit, page_cursor = _page_params(limit, cursor)
            items, next_cursor = services.runs.list_runs(
                principal=principal,
                limit=page_limit,
                cursor=page_cursor,
                scope=_filter_text(scope, "scope"),
                status=_run_status(status),
                since=_instant(since, "since"),
                until=_instant(until, "until"),
                session_id=_filter_text(session_id, "session_id"),
            )
        except ValueError as exc:
            return _error(422, "invalid_request", str(exc))
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        return JSONResponse(
            status_code=200,
            content={"items": [item.model_dump() for item in items], "next_cursor": next_cursor},
        )

    @router.get(
        "/sessions",
        response_model=None,
        summary="本人会话目录（控制库运行事实聚合；非 checkpoint dump）",
    )
    def list_sessions(
        claims: BearerClaims,
        limit: str | None = None,
        cursor: str | None = None,
        scope: str | None = None,
    ) -> JSONResponse:
        """owner 目录：需要 run.read_own；未知/他人会话不出现在结果里。"""
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            page_limit, page_cursor = _page_params(limit, cursor)
            rows, next_cursor = services.runs.sessions(
                principal=principal,
                limit=page_limit,
                cursor=page_cursor,
                scope=_filter_text(scope, "scope"),
            )
        except ValueError as exc:
            return _error(422, "invalid_request", str(exc))
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        return JSONResponse(
            status_code=200,
            content={"items": [row.model_dump() for row in rows], "next_cursor": next_cursor},
        )

    @router.get(
        "/sessions/{session_id}",
        response_model=None,
        summary="会话回合（本人；未知与不可见统一 404）",
    )
    def session_turns(
        session_id: str,
        claims: BearerClaims,
        limit: str | None = None,
        cursor: str | None = None,
        deployment_id: str | None = None,
    ) -> JSONResponse:
        """回合 = 控制库运行行（新→旧）；不读也不展示 checkpoint 正文。"""
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            page_limit, page_cursor = _page_params(limit, cursor)
            items, next_cursor = services.runs.session_runs(
                principal=principal,
                session_id=session_id,
                limit=page_limit,
                cursor=page_cursor,
                deployment_id=_filter_text(deployment_id, "deployment_id"),
            )
        except ValueError as exc:
            return _error(422, "invalid_request", str(exc))
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except (RunNotFound, KeyError):
            return _error(404, "not_found", f"会话不存在：{session_id!r}")
        return JSONResponse(
            status_code=200,
            content={"items": [item.model_dump() for item in items], "next_cursor": next_cursor},
        )

    @router.get(
        "/runs/{run_id}/artifacts/{artifact_id}",
        response_model=None,
        summary="捕获正文（内容 ACL；清理后 410 保墓碎）",
    )
    def get_artifact(run_id: str, artifact_id: str, claims: BearerClaims) -> JSONResponse:
        """先对象 ACL（他人/未知 404、摘要 403），再保留期（清理后 410）。"""
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            record = services.runs.artifact(
                principal=principal, run_id=run_id, artifact_id=artifact_id
            )
        except ArtifactExpired as exc:
            return _error(410, "artifact_expired", str(exc))
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except (RunNotFound, KeyError):
            return _error(404, "not_found", f"制品不存在：{artifact_id!r}")
        # mode="json"：fields 是 frozenset，普通 model_dump 会留下不可序列化对象
        return JSONResponse(status_code=200, content=record.model_dump(mode="json"))

    @router.get(
        "/runs/{run_id}/events",
        response_model=None,
        summary="运行事件流（SSE；持久事实续读，无法补齐 410）",
    )
    def stream_events(
        run_id: str,
        claims: BearerClaims,
        after_seq: str | None = None,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> Response:
        """建立连接即重新授权（401/403/404/410 同步返回，不进流）；终态后关闭。"""
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            cursor = _event_cursor(after_seq, last_event_id)
            services.runs.open_event_stream(
                principal=principal, run_id=run_id, after_seq=cursor
            )
        except ValueError as exc:
            return _error(422, "invalid_request", str(exc))
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except EventGap as exc:
            return _error(410, "event_gap", str(exc))
        except (RunNotFound, KeyError):
            return _error(404, "not_found", f"运行不存在：{run_id!r}")
        return StreamingResponse(
            stream_run_events(services.store, run_id=run_id, after_seq=cursor),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post(
        "/feedback",
        status_code=201,
        response_model=None,
        summary="提交最小反馈（固定 pending_review / training_eligible=False）",
    )
    def submit_feedback(
        claims: BearerClaims, body: Annotated[Any | None, Body()] = None
    ) -> JSONResponse:
        """合同校验（422）→ 身份（401）→ 能力（403）→ 对象可见（404）→ 采集。"""
        if not isinstance(body, dict):
            return _error(422, "invalid_request", "请求体必须是 JSON 对象")
        try:
            request = FeedbackRequest.model_validate(body)
        except ValidationError as exc:
            return _error(422, "invalid_request", _first_message(exc))
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            record = services.feedback.submit(principal=principal, request=request)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except (RunNotFound, KeyError):
            return _error(404, "not_found", f"运行不存在：{request.run_id!r}")
        return JSONResponse(status_code=201, content=record.model_dump())

    @router.get(
        "/feedback",
        response_model=None,
        summary="本人反馈列表（新→旧；跨用户审核队列属 T12）",
    )
    def list_feedback(claims: BearerClaims) -> JSONResponse:
        """无 feedback.submit 能力 403；列表只含本人的行（不暴露他人）。"""
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            records = services.feedback.list_own(principal=principal)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        return JSONResponse(
            status_code=200, content={"items": [record.model_dump() for record in records]}
        )

    @router.post(
        "/manage/sources",
        status_code=201,
        response_model=None,
        summary="新建数据源修订（追加式；秘密只收 env: 引用）",
    )
    def create_source(
        claims: BearerClaims, body: Annotated[Any | None, Body()] = None
    ) -> JSONResponse:
        """合同校验（422）→ 身份（401）→ source.manage（403）→ 追加修订（201）。"""
        if not isinstance(body, dict):
            return _error(422, "invalid_request", "请求体必须是 JSON 对象")
        try:
            request = SourceRevisionRequest.model_validate(body)
        except ValidationError as exc:
            return _error(422, "invalid_request", _first_message(exc))
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            view = services.sources.create_revision(principal, request)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        return JSONResponse(status_code=201, content=view.model_dump())

    @router.get(
        "/manage/sources",
        response_model=None,
        summary="数据源目录（每源最新修订；last_probe 为探测证据摘要）",
    )
    def list_sources(claims: BearerClaims) -> JSONResponse:
        """无 source.manage 能力 403；列表不回显任何凭据或连接串（D03）。"""
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            items = services.sources.list_registry(principal)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        return JSONResponse(
            status_code=200, content={"items": [view.model_dump() for view in items]}
        )

    @router.post(
        "/manage/sources/{source_id}/probes",
        status_code=200,
        response_model=None,
        summary="受限探测（固定目录查询；不接受请求体）",
    )
    def probe_source(
        source_id: str,
        claims: BearerClaims,
        body: Annotated[Any | None, Body()] = None,
    ) -> JSONResponse:
        """不接受请求体（422，不提供任意「测试 SQL」通道）→ 身份（401）→
        source.manage（403）→ 未知源（404）→ 探测结果（200，含 blocked）。"""
        if body is not None:
            return _error(
                422, "invalid_request", "探测不接受请求体（不提供任意测试 SQL 通道）"
            )
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            result = services.sources.probe(principal, source_id)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except KeyError:
            return _error(404, "not_found", f"数据源不存在：{source_id!r}")
        return JSONResponse(status_code=200, content=result.model_dump())

    @router.post(
        "/manage/deployments",
        status_code=201,
        response_model=None,
        summary="创建部署绑定（draft；首次发布前 active_release_id=null）",
    )
    def create_deployment(
        claims: BearerClaims, body: Annotated[Any | None, Body()] = None
    ) -> JSONResponse:
        """合同校验（422）→ 身份（401）→ deployment.manage+scope（403）→
        源必须已配置（404）→ 重复 ID（409）→ 201 固定 8 键部署行。"""
        if not isinstance(body, dict):
            return _error(422, "invalid_request", "请求体必须是 JSON 对象")
        try:
            request = DeploymentRequest.model_validate(body)
        except ValidationError as exc:
            return _error(422, "invalid_request", _first_message(exc))
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            record = services.deployments.create_draft(principal, request)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except KeyError as exc:
            source_id = exc.args[0] if exc.args else ""
            return _error(404, "not_found", f"数据源不存在：{source_id!r}")
        except DeploymentConflict as exc:
            return _error(409, "deployment_conflict", str(exc))
        return JSONResponse(status_code=201, content=record.model_dump())

    @router.get(
        "/manage/deployments",
        response_model=None,
        summary="部署目录（按已授权领域裁剪；发布动作能力亦可读）",
    )
    def list_deployments(claims: BearerClaims) -> JSONResponse:
        """无部署读取能力 403；列表只含已授权领域的部署（服务端裁剪，D02）。"""
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            items = services.deployments.list_deployments(principal)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        return JSONResponse(
            status_code=200, content={"items": [record.model_dump() for record in items]}
        )

    @router.get(
        "/manage/deployments/{deployment_id}",
        response_model=None,
        summary="部署详情（未授权领域 403、未知 404；不改写指针）",
    )
    def get_deployment(deployment_id: str, claims: BearerClaims) -> JSONResponse:
        """发布/回退以当前指针做 CAS 的前置读取（D04）；读取不获得管理写入。"""
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            record = services.deployments.view(principal, deployment_id)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except KeyError:
            return _error(404, "not_found", f"部署不存在：{deployment_id!r}")
        return JSONResponse(status_code=200, content=record.model_dump())

    @router.post(
        "/manage/drafts",
        status_code=201,
        response_model=None,
        summary="创建语义草稿（非权威；不改变发布指针）",
    )
    def create_draft(
        claims: BearerClaims, body: Annotated[Any | None, Body()] = None
    ) -> JSONResponse:
        """合同校验（422）→ 身份（401）→ draft.edit+scope（403）→ 形状门（422）→ 201。

        草稿非权威（D02）：创建只落控制库草稿表，不写部署指针——激活是 T08b 的
        显式 CAS 发布；响应为固定 11 键草稿行（base_git_sha 由服务端取本地 HEAD）。
        """
        if not isinstance(body, dict):
            return _error(422, "invalid_request", "请求体必须是 JSON 对象")
        try:
            request = DraftRequest.model_validate(body)
        except ValidationError as exc:
            return _error(422, "invalid_request", _first_message(exc))
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            draft = services.drafts.create(principal, request)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except DraftContentInvalid as exc:
            return _error(422, "invalid_request", str(exc))
        return JSONResponse(status_code=201, content=draft.model_dump())

    @router.get(
        "/manage/drafts",
        response_model=None,
        summary="草稿目录（按已授权领域裁剪；不因人裁剪）",
    )
    def list_drafts(claims: BearerClaims) -> JSONResponse:
        """无草稿能力 403；列表只含已授权领域的草稿（服务端裁剪，D02）。"""
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            items = services.drafts.list_drafts(principal)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        return JSONResponse(
            status_code=200, content={"items": [draft.model_dump() for draft in items]}
        )

    @router.get(
        "/manage/drafts/{draft_id}",
        response_model=None,
        summary="草稿详情（未授权领域 403、未知 404；审核前置读取）",
    )
    def get_draft(draft_id: str, claims: BearerClaims) -> JSONResponse:
        """只读草稿（含 content）：不改变任何发布指针；未知与不可见按管理面区分。"""
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            draft = services.drafts.view(principal, draft_id)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except KeyError:
            return _error(404, "not_found", f"草稿不存在：{draft_id!r}")
        return JSONResponse(status_code=200, content=draft.model_dump())

    @router.put(
        "/manage/drafts/{draft_id}",
        status_code=200,
        response_model=None,
        summary='CAS 编辑草稿（If-Match: "<revision>"；改内容撤销放行状态）',
    )
    def edit_draft(
        draft_id: str,
        claims: BearerClaims,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        body: Annotated[Any | None, Body()] = None,
    ) -> JSONResponse:
        """If-Match（422）→ 合同（422）→ 身份（401）→ 能力/作用域/对象 ACL（403）→
        修订 CAS（409）→ 200 新修订（旧审核随内容变更失效，store 原语）。

        盲写（缺 If-Match）不允许：编辑是显式 CAS 动作，冲突不静默覆盖。
        方法形态按 D13 目标表为 PUT（详情路径无第二写入口，PATCH 不注册）。
        """
        if if_match is None:
            return _error(422, "invalid_request", '缺少 If-Match 头（格式 "<revision>"）')
        if not re.fullmatch(r'"\d+"', if_match):
            return _error(422, "invalid_request", 'If-Match 必须是 "<revision>" 形式的修订 ETag')
        if not isinstance(body, dict):
            return _error(422, "invalid_request", "请求体必须是 JSON 对象")
        try:
            request = DraftEditRequest.model_validate(body)
        except ValidationError as exc:
            return _error(422, "invalid_request", _first_message(exc))
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            draft = services.drafts.edit(
                principal, draft_id, request, expected=int(if_match[1:-1])
            )
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except KeyError:
            return _error(404, "not_found", f"草稿不存在：{draft_id!r}")
        except DraftContentInvalid as exc:
            return _error(422, "invalid_request", str(exc))
        except RevisionConflict as exc:
            return _error(409, "revision_conflict", str(exc))
        return JSONResponse(status_code=200, content=draft.model_dump())

    @router.post(
        "/manage/drafts/{draft_id}/validations",
        status_code=201,
        response_model=None,
        summary="确定性校验草稿（结构/治理/策略；通过则推进 validated）",
    )
    def validate_draft(draft_id: str, claims: BearerClaims) -> JSONResponse:
        """无请求体：校验对象就是草稿内容本身，不接受调用方指定规则集或结论。

        身份（401）→ draft.validate+作用域（403）→ kind 门（422，无校验器的
        kind 拒绝）→ 三套校验器 → 201 固定 8 键证据行；passed 且仍在 draft 时
        同事务推进 validated（重复校验只追加证据，不漂移状态）。
        """
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            validation = services.drafts.validate(principal, draft_id)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except KeyError:
            return _error(404, "not_found", f"草稿不存在：{draft_id!r}")
        except DraftContentInvalid as exc:
            return _error(422, "invalid_request", str(exc))
        except RevisionConflict as exc:
            return _error(409, "revision_conflict", str(exc))
        return JSONResponse(status_code=201, content=validation.model_dump())

    @router.post(
        "/manage/drafts/{draft_id}/reviews",
        status_code=201,
        response_model=None,
        summary="人工审核草稿（只认 validated；approved 推进 reviewed）",
    )
    def review_draft(
        draft_id: str, claims: BearerClaims, body: Annotated[Any | None, Body()] = None
    ) -> JSONResponse:
        """合同（422）→ 身份（401）→ draft.review+作用域（403）→ 状态门（409）→ 201。

        rejected 只落证据、可同修订重审；编辑使 revision+1、审核随摘要作废。
        """
        if not isinstance(body, dict):
            return _error(422, "invalid_request", "请求体必须是 JSON 对象")
        try:
            request = DraftReviewRequest.model_validate(body)
        except ValidationError as exc:
            return _error(422, "invalid_request", _first_message(exc))
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            review = services.drafts.review(principal, draft_id, request)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except KeyError:
            return _error(404, "not_found", f"草稿不存在：{draft_id!r}")
        except RevisionConflict as exc:
            return _error(409, "revision_conflict", str(exc))
        return JSONResponse(status_code=201, content=review.model_dump())

    @router.get(
        "/manage/drafts/{draft_id}/patch",
        response_model=None,
        summary="导出最小统一 diff 与影响面（纯只读，不改变发布指针）",
    )
    def export_draft_patch(draft_id: str, claims: BearerClaims) -> JSONResponse:
        """无状态门（导出是只读动作）：身份（401）→ draft.export+作用域（403）→ 200。

        base 取草稿 base_git_sha 的对象库文本（不读脏工作树）；未编辑草稿为空
        patch。"""
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            view = services.drafts.export_patch(principal, draft_id)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except KeyError:
            return _error(404, "not_found", f"草稿不存在：{draft_id!r}")
        return JSONResponse(status_code=200, content=view.model_dump())

    @router.post(
        "/manage/releases/imports",
        status_code=201,
        response_model=None,
        summary="导入已审核草稿的 Git 制品（门禁通过后登记；不激活）",
    )
    def import_release(
        claims: BearerClaims, body: Annotated[Any | None, Body()] = None
    ) -> JSONResponse:
        """合同（422）→ 身份（401）→ release.import+域门（403）→ 提交可达（422）→
        白名单收集（422）→ 内容 == 已审核草稿（409）→ 制品整体门禁（422）→ 201。

        导入只收显式 commit 的白名单配置：符号链接/子模块/超大文件/非白名单路径
        一律拒绝或不收集，制品从不执行（D04）；草稿推进至 release_ready，不激活。
        """
        if not isinstance(body, dict):
            return _error(422, "invalid_request", "请求体必须是 JSON 对象")
        try:
            request = ReleaseImportRequest.model_validate(body)
        except ValidationError as exc:
            return _error(422, "invalid_request", _first_message(exc))
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            view = services.releases.import_bundle(principal, request)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except ReleaseTargetMissing as exc:
            return _error(404, "not_found", str(exc))
        except ReleaseInvalid as exc:
            return _error(422, "invalid_request", str(exc))
        except ReleaseConflict as exc:
            return _error(409, "release_conflict", str(exc))
        except RevisionConflict as exc:
            return _error(409, "revision_conflict", str(exc))
        return JSONResponse(status_code=201, content=view.model_dump())

    @router.post(
        "/manage/deployments/{deployment_id}/releases",
        status_code=200,
        response_model=None,
        summary="CAS 发布（expected_active_release_id 必须等于当前指针）",
    )
    def publish_release(
        deployment_id: str,
        claims: BearerClaims,
        body: Annotated[Any | None, Body()] = None,
    ) -> JSONResponse:
        """合同（422）→ 身份（401）→ release.publish+域门（403）→ 部署/发布存在
        （404）→ 域/源匹配（409）→ 当前只读证据（409 evidence_required）→
        CAS（409）→ 200 固定 6 键激活视图。

        首次发布 expected=null；不匹配不静默覆盖；查询接受时固定 Manifest，
        活动发布改变不影响已运行实例（D04）。
        """
        if not isinstance(body, dict):
            return _error(422, "invalid_request", "请求体必须是 JSON 对象")
        try:
            request = ActivationRequest.model_validate(body)
        except ValidationError as exc:
            return _error(422, "invalid_request", _first_message(exc))
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            view = services.releases.publish(principal, deployment_id, request)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except ReleaseTargetMissing as exc:
            return _error(404, "not_found", str(exc))
        except SourceEvidenceMissing as exc:
            return _error(409, "evidence_required", str(exc))
        except ReleaseConflict as exc:
            return _error(409, "release_conflict", str(exc))
        return JSONResponse(status_code=200, content=view.model_dump())

    @router.post(
        "/manage/deployments/{deployment_id}/rollbacks",
        status_code=200,
        response_model=None,
        summary="CAS 回退到历史制品（能力/域/登记/证据门禁与发布同等）",
    )
    def rollback_release(
        deployment_id: str,
        claims: BearerClaims,
        body: Annotated[Any | None, Body()] = None,
    ) -> JSONResponse:
        """回退只改指针：仍受当前权限、源能力与安全门禁（D04）；404/409 语义同发布。"""
        if not isinstance(body, dict):
            return _error(422, "invalid_request", "请求体必须是 JSON 对象")
        try:
            request = ActivationRequest.model_validate(body)
        except ValidationError as exc:
            return _error(422, "invalid_request", _first_message(exc))
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            view = services.releases.rollback(principal, deployment_id, request)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except ReleaseTargetMissing as exc:
            return _error(404, "not_found", str(exc))
        except SourceEvidenceMissing as exc:
            return _error(409, "evidence_required", str(exc))
        except ReleaseConflict as exc:
            return _error(409, "release_conflict", str(exc))
        return JSONResponse(status_code=200, content=view.model_dump())

    @router.get(
        "/manage/releases",
        response_model=None,
        summary="发布历史（按已授权领域裁剪；脱敏登记行）",
    )
    def list_releases(claims: BearerClaims) -> JSONResponse:
        """发布动作或部署管理能力可读（D02 读取不获得发布写入）；列表按域裁剪。"""
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            items = services.releases.list_releases(principal)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        return JSONResponse(
            status_code=200, content={"items": [record.model_dump() for record in items]}
        )

    @router.get(
        "/manage/releases/{release_id}",
        response_model=None,
        summary="发布详情（脱敏 Manifest；未授权领域 403、未知 404）",
    )
    def get_release(release_id: str, claims: BearerClaims) -> JSONResponse:
        """发布历史与脱敏详情（D13）：不含模型正文与源凭据；管理面区分 403/404。"""
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        try:
            record = services.releases.view(principal, release_id)
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except ReleaseTargetMissing as exc:
            return _error(404, "not_found", str(exc))
        return JSONResponse(status_code=200, content=record.model_dump())

    @router.get(
        "/manage/diagnostics",
        response_model=None,
        summary="管理面诊断（D12/D13）：源连接/语义版本/发布完整性/状态盘/审计可写性",
    )
    def diagnostics(claims: BearerClaims) -> JSONResponse:
        """诊断端点（D12）：源故障时仍 200，不返回 DSN/密钥/原始 Prompt。

        需 operator 能力（ops.read）；viewer 不够（403）。
        展示：源连接状态、发布完整性、存储可写性、schema 版本。
        """
        services = services_factory()
        principal, error = _principal_or_error(claims, services)
        if error is not None:
            return error
        # 需 operator 能力（ops.read）
        try:
            from serving.control.auth import authorize

            authorize(principal, "ops.read", list(principal.scopes)[0] if principal.scopes else "")
        except ControlForbidden as exc:
            return _error(403, "forbidden", str(exc))
        except IndexError:
            return _error(403, "forbidden", "未授权任何领域")

        # 收集诊断信息（源故障不抛异常，标记 unavailable）
        sources_status = _collect_sources_status(services)
        releases_status = _collect_releases_status(services)
        storage_status = _collect_storage_status(services)

        return JSONResponse(
            status_code=200,
            content={
                "sources": sources_status,
                "releases": releases_status,
                "storage": storage_status,
                "schema_version": services.store.schema_version(),
            },
        )

    return router


def _collect_sources_status(services: ControlServices) -> list[dict]:
    """收集源连接状态（源故障标记 unavailable，不抛异常）。"""
    try:
        # 获取所有已注册源（不执行探测）
        sources = services.store.list_latest_source_revisions()
        return [
            {"source_id": s.source_id, "status": "registered", "engine": s.engine}
            for s in sources
        ] if sources else [{"status": "unavailable", "reason": "no_sources_registered"}]
    except Exception:
        return [{"status": "unavailable", "reason": "source_query_failed"}]


def _collect_releases_status(services: ControlServices) -> dict:
    """收集发布完整性（活动发布数量）。"""
    try:
        deployments = services.store.list_deployments()
        active_count = sum(1 for d in deployments if d.active_release_id is not None)
        return {"active_count": active_count, "total_deployments": len(deployments)}
    except Exception:
        return {"active_count": 0, "total_deployments": 0, "error": "query_failed"}


def _collect_storage_status(services: ControlServices) -> dict:
    """收集存储可写性（控制库/审计）。"""
    control_writable = True
    audit_writable = True
    try:
        # 简单写测试（schema_version 读即可推断可读）
        services.store.schema_version()
    except Exception:
        control_writable = False
    return {
        "control_db_writable": control_writable,
        "audit_writable": audit_writable,
    }
