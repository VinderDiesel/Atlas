"""运行服务（ADR-0031 D07/D13）：幂等提交、单业务队列、真实事件、内存结果窗口与显式捕获。

职责边界
--------
- `/runs` 的服务内核：控制库（ControlStore）持久化运行事实与真实事件；执行在
  **单业务队列**（进程内单工作线程）上串行——API 提交路径只持久化 + 入队，
  不在请求内执行 SQL（D13）。
- 结果正文**只在进程内存**保留：窗口 15 分钟、按会话最近合会计时（D07）。重启、
  登出、权限撤销即不可读——不落盘、不进事件 payload、不伪装可重放。
- 事件先提交再通知（D07②）：本模块保证事件按真序写入控制库；推送（SSE）属 T06。
- 显式捕获（D13）：授权审计在建单时同步执行（写失败 fail-closed：queued 直接
  封 failed、下次 SQL 不得启动）；正文制品在执行完成后按白名单字段写私有
  artifact（T05d），引用随 STATE_SNAPSHOT 落事件（脱敏）。

诚实边界
--------
- `succeeded` 只代表处理完成，不代表答对：具体种类取 `result.kind`（D07）；
  内容不可用时仅 `trace_summary.result_kind` 保留枚举，不编造答案。
- `analyze` 模式在 T05c 显式拒绝（`RunModeNotSupported` → 422）：分析面接通属
  T05d/T07——不静默降级成 ask，也不伪装已支持。
- 崩溃恢复不重跑（D07）：重启时 queued/running 一律由 `recover_interrupted()`
  封为 interrupted——旧登录态与授权可能已失效，重跑会以旧身份执行新数据。
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from uuid import uuid4

from pydantic import JsonValue

from agent.compiler import Filter, OrderSpec, Plan, TimeSpec
from agent.graph import SessionIdentityConflict
from serving.control.artifacts import capture_content
from serving.control.auth import ControlForbidden, Principal, authorize
from serving.control.contracts import (
    TERMINAL_RUN_STATUSES,
    ArtifactRecord,
    CaptureGrant,
    Owner,
    PlanSpec,
    ResultAvailability,
    RunRecord,
    RunRequest,
    RunSummary,
    RunTraceSummary,
    RunView,
    SessionSummary,
    content_digest,
    validate_retain_until,
)
from serving.control.store import RunConflict

if TYPE_CHECKING:
    from agent.graph import DataAgent
    from agent.runtime.identity import DataIdentity
    from agent.state import TurnResult
    from serving.control.store import ControlStore

logger = logging.getLogger(__name__)

SESSION_WINDOW = timedelta(minutes=15)
"""内存结果/会话上下文窗口（D07）：按会话最近合会计时，不按单条结果计时。"""

MAX_PAGE_SIZE = 100
"""列表最大页大小（D13：GET 分页默认 50、最多 100）。"""


def encode_page_cursor(sort_key: str, object_id: str) -> str:
    """keyset 游标 = `排序键|对象 ID`（排序键 = 服务端时间戳，对象 ID 末位去重）。"""
    return f"{sort_key}|{object_id}"


def decode_page_cursor(value: str) -> tuple[str, str]:
    """解析 keyset 游标；形态/时间非法抛 ValueError（路由层投影 422）。

    只规范化时间字符串，不校验对象是否存在——避免把游标变成「对象是否
    存在」的探针（D13：不暴露未授权对象是否存在）。
    """
    sort_key, sep, object_id = value.partition("|")
    if not sep or not object_id or len(object_id) > 128:
        raise ValueError("cursor 形态非法")
    try:
        normalized = datetime.fromisoformat(sort_key).isoformat()
    except ValueError as exc:
        raise ValueError("cursor 排序键不是合法时间") from exc
    return normalized, object_id


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RunNotFound(Exception):
    """运行不存在或对当前主体不可见（HTTP 404；不泄露存在性）。"""


class SessionContextExpired(Exception):
    """会话上下文已过期（HTTP 409 session_context_expired）：续问须换新会话。"""


class RunModeNotSupported(Exception):
    """运行模式尚未接通（HTTP 422 mode_not_supported；不静默降级）。"""


class CaptureInvalid(Exception):
    """显式捕获授权不合法（HTTP 422 capture_invalid）：被拒提交零运行事实。"""


class CaptureAuditFailed(Exception):
    """捕获授权审计失败（HTTP 503 audit_unavailable）：运行已封 failed（见 submit）。"""


class ArtifactExpired(Exception):
    """捕获正文已过保留期（HTTP 410 artifact_expired）：墓碑仍在，正文不回。"""


class EventGap(Exception):
    """续读游标无法补齐（HTTP 410 event_gap）：事件已过保留期或游标越界。"""


@dataclass(frozen=True)
class RunAgent:
    """解析出的运行执行体：会话 agent + 数据身份（D03）。

    `data_identity` None = 未绑定数据身份（测试桩/未解析快照）——视图如实回显
    null，不假称「绑在快照上」；真实链由 `_live_run_agent` 注入。
    """

    agent: DataAgent
    data_identity: DataIdentity | None = None


class TaskRunner(Protocol):
    """后台任务接缝（D13 单业务队列）：生产 ThreadTaskRunner，测试注入同步实现。"""

    def submit(self, task: Callable[[], None]) -> None: ...


class ThreadTaskRunner:
    """单业务队列：一个 daemon 工作线程 FIFO 串行执行（绝不并发跑两个运行）。

    任务内异常不杀死工作线程（运行失败已由 RunService 落账为 failed/interrupted）；
    进程退出即放弃未执行任务——**不自动重跑**（D07：重启恢复封存为 interrupted）。
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._thread = threading.Thread(target=self._work, name="atlas-run-worker", daemon=True)
        self._thread.start()

    def submit(self, task: Callable[[], None]) -> None:
        """入队（不阻塞、不执行）；任务的最终状态由服务层落账。"""
        self._queue.put(task)

    def _work(self) -> None:
        while True:
            task = self._queue.get()
            try:
                task()
            except Exception:  # noqa: BLE001 - 失败已落账；线程必须活着接下一任务
                logger.exception("运行任务异常（已由 RunService 落账；线程继续）")


def _plan_from_spec(spec: PlanSpec) -> Plan:
    """PlanSpec（合同镜像）→ agent.compiler.Plan（与 serving/api._compile_plan 同构）。

    控制面与 HTTP v1 面共享同一执行链（Plan 直执 → 编译器 → Guard）；镜像字段的
    同源性由 test_run_api 的 execute_plan 用例锁定，不在此复制校验逻辑。
    """
    return Plan(
        metric=spec.metric,
        dimensions=tuple(spec.dimensions),
        time=(TimeSpec(spec.time.granularity, spec.time.value) if spec.time is not None else None),
        filters=tuple(Filter(f.column, f.op, f.value) for f in spec.filters),
        order_by=tuple(OrderSpec(o.column, o.desc) for o in spec.order_by),
        limit=spec.limit,
    )


def _status_for(kind: str) -> Literal["succeeded", "blocked", "failed"]:
    """TurnResult.kind → 运行终态（D07）：blocked→blocked、error→failed，其余完成。

    answer/clarify/handoff 都是「处理成功完成」——succeeded 不代表答对（D07），
    clarify（反问）与 handoff（转人工）是合法完成形态而非失败。
    """
    if kind == "blocked":
        return "blocked"
    if kind == "error":
        return "failed"
    return "succeeded"


def _identity_payload(identity: DataIdentity) -> dict[str, JsonValue]:
    """数据身份 dataclass → JSON 投影（字段取自 D03 定义，不手抄第二份）。"""
    return cast("dict[str, JsonValue]", asdict(identity))


def _request_digest(request: RunRequest) -> str:
    """请求摘要（幂等比较）：capture.fields 是集合，按排序规范化（跨进程稳定）。

    `model_dump(mode="json")` 把 frozenset 序列化为列表；列表顺序取决于集合迭代
    序（进程间不稳定）——不排序时，重启后的同 body 重试会被误判为内容冲突。
    """
    payload = request.model_dump(mode="json")
    capture = payload.get("capture")
    if isinstance(capture, dict) and isinstance(capture.get("fields"), list):
        normalized = dict(capture)
        normalized["fields"] = sorted(str(item) for item in capture["fields"])
        payload["capture"] = normalized
    return content_digest(payload)


class RunService:
    """运行提交/视图服务（ADR-0031 D07/D13）：授权、幂等、队列、窗口在此收敛。

    进程内单实例语义：内存结果窗口与会话计时表都是进程状态——内容只在处理它的
    进程内存里（多进程部署下视图可能随进程而异）。T05c 的部署形态是单进程
    （ADR-0031 D01）；不声称跨进程一致性。
    """

    def __init__(
        self,
        store: ControlStore,
        *,
        agent_resolver: Callable[[RunRecord], RunAgent],
        runner: TaskRunner,
        clock: Callable[[], datetime] | None = None,
        session_window: timedelta = SESSION_WINDOW,
        capture_audit: Callable[[Principal, RunRequest], None] | None = None,
    ) -> None:
        """装配运行服务；clock 可注入（测试用可变时钟驱动窗口断言）。

        `capture_audit` 是 D13 显式捕获的授权审计接缝：写失败向上抛，由 submit
        以 fail-closed 处理（D07：下一次 SQL 不得启动）；None = 未装配（不审计）。
        """
        self._store = store
        self._agent_resolver = agent_resolver
        self._runner = runner
        self._clock = clock if clock is not None else _utc_now
        self._session_window = session_window
        self._capture_audit = capture_audit
        # 结果正文/数据身份：只在进程内存（D07），键 = run_id；不上磁盘、不进事件
        self._results: dict[str, dict[str, Any]] = {}
        self._identities: dict[str, dict[str, JsonValue]] = {}
        # 会话最近合计时刻（D07：按最近合会计时）；键 = (issuer, subject, 部署, 会话)
        self._sessions: dict[tuple[str, str, str, str], datetime] = {}

    # -- 提交 ---------------------------------------------------------------

    def recover_interrupted(self) -> int:
        """启动恢复（D07）：封存遗留 queued/running 并返回封存数；不自动重跑。"""
        return self._store.mark_interrupted()

    def submit(self, *, principal: Principal, request: RunRequest) -> tuple[RunRecord, bool]:
        """幂等创建并（首次时）入队；返回（记录, 是否新建）。

        顺序：部署存在性（404）→ 控制授权（403）→ 模式支持（422）→ 捕获授权
        （422）→ 续问窗口（409）→ 幂等创建（同键同内容返回原 run；不同内容 409）
        → 捕获授权审计（D13；失败 fail-closed，503）。只在创建成功时入队；
        重试命中不重复入队（同键只执行一次，D07 旗舰口径）、不重复审计授权。
        """
        scope = self._store.deployment_scope(request.deployment_id)
        authorize(principal, "run.create", scope)
        if request.mode == "analyze":
            raise RunModeNotSupported(
                "analyze 模式尚未接通 /runs（T05d/T07 接通）：请先用 ask 或 execute_plan"
            )
        if request.capture is not None:
            self._validate_capture(request.capture)
        if request.session_id is not None and not self._continuation_allowed(
            principal, request.deployment_id, request.session_id
        ):
            minutes = int(self._session_window.total_seconds() // 60)
            raise SessionContextExpired(
                f"会话 {request.session_id!r} 的上下文已过期（超过 {minutes} 分钟）："
                "请换新 session_id 重述问题（不悄悄丢弃上下文执行）"
            )
        session_id = request.session_id or f"session-{uuid4().hex[:8]}"
        # 摘要基于请求原文（不含服务端生成的 session_id）：同键重试必须同摘要，
        # 否则「缺省会话」的幂等重试会被误判成内容冲突（D13 旗舰用例锁定）。
        digest = _request_digest(request)
        record, created = self._store.create_run(
            owner=Owner(issuer=principal.issuer, subject=principal.subject),
            deployment_id=request.deployment_id,
            mode=request.mode,
            session_id=session_id,
            client_request_id=request.client_request_id,
            request_digest=digest,
        )
        if created:
            if request.capture is not None:
                # 授权审计先于事件与入队（D13）：未留痕的捕获授权不允许执行
                self._audit_capture_grant(record, principal, request)
            # 事件先提交后入队（D07②）：写失败异常传播 → 运行不执行（fail closed）
            self._store.append_event(
                record.run_id,
                event_type="RUN_ACCEPTED",
                payload={"mode": record.mode, "deployment_id": record.deployment_id},
            )
            self._runner.submit(lambda: self._execute(record, request, principal))
        return record, created

    def _validate_capture(self, grant: CaptureGrant) -> None:
        """保留期不得超上限（D07 默认 7 天）：用服务时钟兜底，超限抛 CaptureInvalid。"""
        try:
            validate_retain_until(grant.retain_until, now=self._now())
        except ValueError as exc:
            raise CaptureInvalid(str(exc)) from exc

    def _audit_capture_grant(
        self, record: RunRecord, principal: Principal, request: RunRequest
    ) -> None:
        """D13 授权审计（fail-closed）：写失败 → queued 直接封 failed 并如实抛。

        审计是「下一次 SQL 不得启动」的强制事实（D07）：未留痕的捕获授权不允许
        执行；run 已在 queued 落账，封 failed/error 而不静默删除（不伪造不存在）。
        未装配审计接缝（None）同样拒绝执行——不把「没装审计」当成「不必审计」。
        """
        if self._capture_audit is None:
            self._finalize(record.run_id, "failed", "error")
            raise CaptureAuditFailed("捕获审计未装配：拒绝执行（fail-closed）")
        try:
            self._capture_audit(principal, request)
        except Exception as exc:  # noqa: BLE001 - 任何写失败都 fail closed，不降级继续
            logger.exception("捕获授权审计写失败（fail-closed）：%s", record.run_id)
            self._finalize(record.run_id, "failed", "error")
            raise CaptureAuditFailed("捕获授权审计写失败：运行已封 failed") from exc

    def _continuation_allowed(
        self, principal: Principal, deployment_id: str, session_id: str
    ) -> bool:
        """续问窗口检查（D07）：内存窗口内放行；窗口外以控制库事实裁决。

        - 内存有会话上下文（窗口内）→ 放行（续接同一 thread）；
        - 控制库无该会话记录 → 本进程视为全新会话 → 放行；
        - 控制库有在飞 run → 放行（其执行路径持有上下文）；
        - 只余终态记录且内存窗口已过 → 拒绝（409，不静默丢弃上下文执行）。
        """
        owner = Owner(issuer=principal.issuer, subject=principal.subject)
        key = self._session_key(owner, deployment_id, session_id)
        touched = self._sessions.get(key)
        if touched is not None and self._now() - touched <= self._session_window:
            return True
        state = self._store.session_run_state(
            owner=owner, deployment_id=deployment_id, session_id=session_id
        )
        if state is None:
            return True
        return state == "active"

    # -- 执行（单业务队列任务体） -------------------------------------------

    def _execute(self, record: RunRecord, request: RunRequest, principal: Principal) -> None:
        """队列任务：running → 执行 → 终态落账；任何失败都如实投影，不伪装成功。"""
        try:
            self._store.mark_running(record.run_id)
        except RunConflict:
            # 已被封存（重启恢复为 interrupted）：不执行、不覆写终态
            return
        try:
            self._store.append_event(
                record.run_id, event_type="RUN_STARTED", payload={"mode": record.mode}
            )
        except Exception:  # noqa: BLE001 - 强制事件盘写失败 → 下一次 SQL 不得启动
            logger.exception("RUN_STARTED 写入失败，运行不执行（fail closed）：%s", record.run_id)
            self._finalize(record.run_id, "failed", "error")
            return
        try:
            run_agent = self._agent_resolver(record)
        except Exception:  # noqa: BLE001 - 代理解析失败如实落账
            logger.exception("运行代理解析失败：%s", record.run_id)
            self._finalize(record.run_id, "failed", "error")
            return
        try:
            result = self._invoke(run_agent, record, request, principal)
        except SessionIdentityConflict:
            # 同会话换身份（ADR-0020 决策⑥ 的安全拒绝）：blocked 而非 failed
            self._finalize(record.run_id, "blocked", "blocked")
            return
        except Exception:  # noqa: BLE001 - 未预期失败如实落账，不伪装成功
            logger.exception("运行执行未预期失败：%s", record.run_id)
            self._finalize(record.run_id, "failed", "error")
            return
        # 结果正文只进内存窗口（D07）：不落盘、不进事件 payload（脱敏摘要通道）
        from serving.api import _turn_payload  # 延迟导入：api→router→runs 模块级成环

        payload = _turn_payload(result, run_agent.agent.snapshot)
        self._results[record.run_id] = payload
        if run_agent.data_identity is not None:
            self._identities[record.run_id] = _identity_payload(run_agent.data_identity)
        status = _status_for(result.kind)
        self._touch_session(record)
        artifacts = self._capture_artifacts(record, request, payload)
        try:
            # STATE_SNAPSHOT 只落脱敏状态、结果种类与 artifact 引用（D07：不内嵌
            # 问句/SQL/结果行/Prompt）
            self._store.append_event(
                record.run_id,
                event_type="STATE_SNAPSHOT",
                payload={
                    "status": status,
                    "result_kind": result.kind,
                    "artifacts": cast("list[JsonValue]", artifacts),
                },
            )
        except Exception:  # noqa: BLE001 - SQL 已执行完：终态仍必须落账（否则卡 running）
            logger.exception("STATE_SNAPSHOT 写入失败（终态照常落账）：%s", record.run_id)
        self._finalize(record.run_id, status, result.kind)

    def _capture_artifacts(
        self,
        record: RunRecord,
        request: RunRequest,
        payload: dict[str, Any],
    ) -> list[str]:
        """显式捕获（D13）：按授权字段写私有 artifact，返回事件引用列表。

        正文只在显式授权时落库（question/node_io/result 白名单，见 artifacts.py）；
        无可物化内容不建空制品。写入失败如实记日志并缺席引用——SQL 已执行完、
        答案仍是真实结果（终态照常落账），不把「捕获失败」混同成「答案失败」。
        """
        if request.capture is None:
            return []
        try:
            content = capture_content(request.capture, payload)
            if not content:
                return []
            artifact = self._store.create_artifact(
                record.run_id, grant=request.capture, content=content, now=self._now()
            )
        except Exception:  # noqa: BLE001 - 结果照常落账；缺席引用可由事件核对
            logger.exception("捕获正文写入失败（结果照常落账）：%s", record.run_id)
            return []
        return [artifact.artifact_id]

    def _invoke(
        self, run_agent: RunAgent, record: RunRecord, request: RunRequest, principal: Principal
    ) -> TurnResult:
        """真实调用（`/ask` 与 Plan 直执的同一执行链）：身份每轮显式下推（ADR-0020）。

        claims 取自 `principal.user_context`（已验证 Bearer claims 的副本）——与
        `/ask` 面同口径；`run_context` 带 run_id 交给图的事件接缝（T05b/T05c）。
        """
        run_context: dict[str, Any] = {"run_id": record.run_id}
        identity = dict(principal.user_context) if principal.user_context else None
        agent = run_agent.agent
        if request.mode == "execute_plan":
            assert request.plan is not None  # 合同层已保证（_field_combinations）
            from serving.api import _plan_text  # 同上：延迟导入防模块级成环

            plan = _plan_from_spec(request.plan)
            return agent.run_plan(
                plan,
                session_id=record.session_id,
                identity=identity,
                question=request.question or _plan_text(plan),
                run_context=run_context,
            )
        assert request.question is not None  # 合同层已保证
        return agent.ask(
            request.question,
            session_id=record.session_id,
            identity=identity,
            run_context=run_context,
        )

    # -- 视图 ---------------------------------------------------------------

    def view(self, *, principal: Principal, run_id: str) -> RunView:
        """RunView 投影（D07）：固定 10 键；对象不可见抛 RunNotFound（404）。"""
        try:
            record = self._store.get_run(run_id)
        except KeyError as exc:
            raise RunNotFound(run_id) from exc
        access = self._access(principal, record)
        if access == "hidden":
            raise RunNotFound(run_id)
        if access == "summary":
            availability: ResultAvailability = "restricted"
        else:
            availability = self._availability(record, has_payload=run_id in self._results)
        result = (
            self._results.get(run_id)
            if (access == "full" and availability == "available")
            else None
        )
        return RunView(
            run_id=record.run_id,
            session_id=record.session_id,
            release_id=record.release_id,
            status=record.status,
            result=result,
            result_availability=availability,
            data_identity=self._identities.get(run_id) if access == "full" else None,
            replay_of=record.replay_of,
            last_seq=record.last_seq,
            trace_summary=RunTraceSummary(result_kind=record.result_kind),
        )

    # -- 历史目录（T06a：GET /runs、GET /sessions） ---------------------------

    def list_runs(
        self,
        *,
        principal: Principal,
        limit: int = 50,
        cursor: tuple[str, str] | None = None,
        scope: str | None = None,
        status: str | None = None,
        since: str | None = None,
        until: str | None = None,
        session_id: str | None = None,
    ) -> tuple[list[RunSummary], str | None]:
        """可见运行目录（D13）：能力定可见集合、作用域先验授权，keyset 游标分页。"""
        owner = self._list_owner(principal, scope=scope)
        records, more = self._store.list_runs_filtered(
            owner=owner,
            scopes=frozenset(principal.scopes),
            scope=scope,
            status=status,
            session_id=session_id,
            since=since,
            until=until,
            cursor=cursor,
            limit=limit,
        )
        items = [self._summary(principal, record) for record in records]
        next_cursor = (
            encode_page_cursor(records[-1].created_at, records[-1].run_id) if more else None
        )
        return items, next_cursor

    def sessions(
        self,
        *,
        principal: Principal,
        limit: int = 50,
        cursor: tuple[str, str] | None = None,
        scope: str | None = None,
    ) -> tuple[list[SessionSummary], str | None]:
        """本人会话目录（D13：有权限的历史目录，非 checkpoint dump）。"""
        if "run.read_own" not in principal.capabilities:
            raise ControlForbidden("会话目录需要 run.read_own 能力")
        if scope is not None and scope not in principal.scopes:
            raise ControlForbidden(f"作用域未授权：{scope!r}")
        owner = Owner(issuer=principal.issuer, subject=principal.subject)
        rows, more = self._store.list_sessions(
            owner=owner,
            scopes=frozenset(principal.scopes),
            scope=scope,
            cursor=cursor,
            limit=limit,
        )
        next_cursor = (
            encode_page_cursor(rows[-1].last_run_at, rows[-1].session_id) if more else None
        )
        return rows, next_cursor

    def session_runs(
        self,
        *,
        principal: Principal,
        session_id: str,
        limit: int = 50,
        cursor: tuple[str, str] | None = None,
        deployment_id: str | None = None,
    ) -> tuple[list[RunSummary], str | None]:
        """会话回合（控制库运行事实）：未知会话与空会话统一 404（不泄露存在性）。"""
        if "run.read_own" not in principal.capabilities:
            raise ControlForbidden("会话回读需要 run.read_own 能力")
        owner = Owner(issuer=principal.issuer, subject=principal.subject)
        records, more = self._store.list_runs_filtered(
            owner=owner,
            scopes=frozenset(principal.scopes),
            session_id=session_id,
            deployment_id=deployment_id,
            cursor=cursor,
            limit=limit,
        )
        if not records and cursor is None:
            raise RunNotFound(session_id)
        items = [self._summary(principal, record) for record in records]
        next_cursor = (
            encode_page_cursor(records[-1].created_at, records[-1].run_id) if more else None
        )
        return items, next_cursor

    def artifact(
        self, *, principal: Principal, run_id: str, artifact_id: str
    ) -> ArtifactRecord:
        """捕获正文读取（D13）：先对象 ACL（他人/未知 404、摘要 403），
        再保留期（清理后 410）；不从 checkpoint 或内存窗口绕过保留期。"""
        try:
            record = self._store.get_run(run_id)
        except KeyError as exc:
            raise RunNotFound(run_id) from exc
        access = self._access(principal, record)
        if access == "hidden":
            raise RunNotFound(run_id)
        if access == "summary":
            raise ControlForbidden("内容读取需要 run.read_own 能力")
        try:
            artifact = self._store.get_artifact(artifact_id)
        except KeyError as exc:
            raise RunNotFound(artifact_id) from exc
        if artifact.run_id != run_id:
            raise RunNotFound(artifact_id)
        if artifact.content is None or artifact.cleaned_at is not None:
            raise ArtifactExpired(f"捕获正文已过保留期：{artifact_id!r}")
        return artifact

    def open_event_stream(
        self, *, principal: Principal, run_id: str, after_seq: int
    ) -> RunRecord:
        """SSE 连接建立检查：对象 ACL（hidden→404、summary→403）+ 续读可行性（410）。

        当前身份重新授权发生在建立时（D13）：hidden 一律 404（不泄露存在性），
        摘要身份无事件面（同 artifact 口径）。gap 判定（D07「无法补齐已过保留期
        事件返回 410」）：游标超出 last_seq（脏游标）、最早现存 seq > after_seq+1
        （中间已被清理）、事件已清空而游标 >0——三种都无法从持久事实补齐。
        """
        try:
            record = self._store.get_run(run_id)
        except KeyError as exc:
            raise RunNotFound(run_id) from exc
        access = self._access(principal, record)
        if access == "hidden":
            raise RunNotFound(run_id)
        if access != "full":
            raise ControlForbidden("运行事件需要内容面权限（run.read_own）")
        if after_seq <= 0:
            return record
        if after_seq > record.last_seq:
            raise EventGap(f"续读游标超出事件范围：{after_seq} > {record.last_seq}")
        first = self._store.first_event_seq(run_id)
        if first is None or first > after_seq + 1:
            raise EventGap(f"事件已过保留期，无法从游标 {after_seq} 补齐")
        return record

    @staticmethod
    def _list_owner(principal: Principal, *, scope: str | None) -> Owner | None:
        """列表所有者过滤（D13）：能力定可见集合（read_own/read_summary），
        域参数先验授权（不在本主体 scopes 内直接 403）。"""
        capabilities = principal.capabilities
        can_own = "run.read_own" in capabilities
        can_summary = "run.read_summary" in capabilities
        if not (can_own or can_summary):
            raise ControlForbidden("需要 run.read_own 或 run.read_summary 能力")
        if scope is not None and scope not in principal.scopes:
            raise ControlForbidden(f"作用域未授权：{scope!r}")
        if can_summary:
            return None  # 摘要能力：并集行（实际可见域仍受 scopes 硬过滤）
        return Owner(issuer=principal.issuer, subject=principal.subject)

    def _summary(self, principal: Principal, record: RunRecord) -> RunSummary:
        """摘要行投影（RunSummary 12 键）：availability 与 RunView 同口径动态计算。"""
        access = self._access(principal, record)
        availability: ResultAvailability = (
            "restricted"
            if access == "summary"
            else self._availability(record, has_payload=record.run_id in self._results)
        )
        return RunSummary(
            run_id=record.run_id,
            session_id=record.session_id,
            deployment_id=record.deployment_id,
            scope=record.scope,
            mode=record.mode,
            status=record.status,
            result_availability=availability,
            result_kind=record.result_kind,
            replay_of=record.replay_of,
            last_seq=record.last_seq,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def _availability(self, record: RunRecord, *, has_payload: bool) -> ResultAvailability:
        """可用性投影（D07 五态）：未终态 pending；终态 available / expired / not_retained。

        available 需要两个事实同时成立：正文在本进程内存里（has_payload），且会话
        未静默超窗（按最近合会计时）。窗口过期后按控制库基础值如实说 not_retained
        ——不冒充可复现（重启即失，D07）。
        """
        if record.status not in TERMINAL_RUN_STATUSES:
            return "pending"
        if record.result_availability == "expired":
            return "expired"
        if has_payload and self._session_fresh(record):
            return "available"
        return "not_retained"

    @staticmethod
    def _access(
        principal: Principal, record: RunRecord
    ) -> Literal["full", "summary", "hidden"]:
        """对象级内容 ACL（D07）：owner 读自己；operator 的 read_summary 只读摘要。

        能力与作用域同时检查（authorize 同口径）；hidden 一律 404——不泄露
        「存在但无权」与「不存在」的差别（对象不可见的统一投影）。
        """
        is_owner = (
            record.owner.issuer == principal.issuer and record.owner.subject == principal.subject
        )
        if is_owner:
            if "run.read_own" in principal.capabilities and record.scope in principal.scopes:
                return "full"
            return "hidden"
        if "run.read_summary" in principal.capabilities and record.scope in principal.scopes:
            return "summary"
        return "hidden"

    # -- 进程内窗口 ---------------------------------------------------------

    def _now(self) -> datetime:
        return self._clock()

    @staticmethod
    def _session_key(
        owner: Owner, deployment_id: str, session_id: str
    ) -> tuple[str, str, str, str]:
        return (owner.issuer, owner.subject, deployment_id, session_id)

    def _touch_session(self, record: RunRecord) -> None:
        """记会话最近合计时刻（D07：窗口按最近合会计时，不按单条结果计时）。"""
        key = self._session_key(record.owner, record.deployment_id, record.session_id)
        self._sessions[key] = self._now()

    def _session_fresh(self, record: RunRecord) -> bool:
        key = self._session_key(record.owner, record.deployment_id, record.session_id)
        touched = self._sessions.get(key)
        return touched is not None and self._now() - touched <= self._session_window

    def _finalize(self, run_id: str, status: str, result_kind: str | None) -> None:
        """终态落账；availability 基础值如实为 not_retained（正文只驻内存未持久化）。

        视图投影在内存窗口内且正文可读时覆写为 available；expired 来自清理标记
        （T06），restricted 来自内容 ACL（见 `view`）。
        """
        try:
            self._store.finalize_run(
                run_id, status=status, result_kind=result_kind, availability="not_retained"
            )
        except Exception:  # noqa: BLE001 - 终态写失败只能留日志（重启恢复会封存）
            logger.exception("运行终态写入失败：%s", run_id)
