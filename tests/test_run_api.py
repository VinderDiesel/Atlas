"""ADR-0031 T05c/T06a/T06b：/runs 提交、历史目录与真 SSE 的 HTTP 级验收
（真实 API/Guard/事件盘）。

口径（与 D07/D13 逐条对应）：

- 创建运行先持久化 queued、后台单业务队列执行、API 不在提交路径阻塞（旗舰
  用例：提交时 executor 零调用，drain 后恰好一次，且调用次数能与事件核对）；
- `client_request_id` 在同 owner/部署内幂等：同键同内容返回原 run，内容不同
  409，并发不双执行；
- `RunView` 固定 10 键；result 仅终态 + 内容可用 + 有权限时给出（安全 TurnPayload
  投影），否则 null；
- `result_availability` 五态动态投影：pending/available/not_retained/restricted
  （对象不可见仍 404）；
- 15 分钟内存窗口按会话最近合会计时；重启封存 interrupted 且不重跑；
- 过期会话续问 409 `session_context_expired`，不悄悄丢弃上下文执行；
- 未知部署/未知 run 均 404；错误体统一 `{"error":{code,message,request_id}}`；
- T06a 历史目录：GET /runs 游标分页/过滤/ACL；GET /sessions 控制库事实聚合；
  artifact 内容 ACL（200/403/404）与清理后 410 保墓碎；
- T06b 真 SSE：GET /runs/{run_id}/events 从持久事实续读（id/event/data 三行
  帧）；断线续读 after_seq/Last-Event-ID；无法补齐 410 event_gap；阻塞期首帧
  可读且读流不重跑（不重复执行）；建立连接即重新授权。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from serving.control.contracts import Owner
from serving.control.runs import ThreadTaskRunner
from tests.workbench_support import RUNS_PATH, SESSIONS_PATH, WorkbenchHarness

RUN_VIEW_KEYS = frozenset(
    {
        "run_id",
        "session_id",
        "release_id",
        "status",
        "result",
        "result_availability",
        "data_identity",
        "replay_of",
        "last_seq",
        "trace_summary",
    }
)

_OWNER = Owner(issuer="atlas-local", subject="viewer")


def _ask_body(question: str, *, client_request_id: str, **extra: object) -> dict[str, object]:
    body: dict[str, object] = {
        "deployment_id": "finance",
        "mode": "ask",
        "question": question,
        "client_request_id": client_request_id,
    }
    body.update(extra)
    return body


def _view(h: WorkbenchHarness, run_id: str, *, actor: str = "viewer") -> dict[str, object]:
    response = h.request("GET", f"{RUNS_PATH}/{run_id}", actor=actor)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def test_submission_retry_runs_once(tmp_path: Path) -> None:
    """旗舰（T05 退出门）：重试同键同一 run、提交不阻塞、执行恰好一次、调用可核对。"""
    with WorkbenchHarness(tmp_path) as h:
        release_id = h.seed_release()
        body = _ask_body("2013 年第二季度总交易额", client_request_id="retry-key")
        first = h.request("POST", RUNS_PATH, json=body)
        assert first.status_code == 202, first.text
        receipt = first.json()
        assert set(receipt) == {"run_id", "status", "release_id"}
        assert receipt["status"] == "queued"
        assert receipt["release_id"] == release_id
        # 提交不阻塞：后台单业务队列尚未执行（API 只入队）
        assert h.executor_spy.calls == []
        queued = _view(h, receipt["run_id"])
        assert queued["status"] == "queued"
        assert queued["result"] is None
        assert queued["result_availability"] == "pending"
        # 重试同键同内容：同一 run、不重复入队
        retry = h.request("POST", RUNS_PATH, json=body)
        assert retry.status_code == 202, retry.text
        assert retry.json()["run_id"] == receipt["run_id"]
        h.drain()
        assert len(h.executor_spy.calls) == 1
        view = _view(h, receipt["run_id"])
        assert view["status"] == "succeeded"
        assert view["result_availability"] == "available"
        result = view["result"]
        assert result is not None
        assert result["kind"] == "answer"
        assert result["sql"] == h.executor_spy.calls[0]
        # 调用次数与真实事件核对：TOOL_FINISHED execute_plan 恰一条（不漏调用）
        events = h.events(receipt["run_id"])
        assert events[0].event_type == "RUN_ACCEPTED"
        assert events[-1].event_type == "RUN_FINISHED"
        executed = [
            e
            for e in events
            if e.event_type == "TOOL_FINISHED" and e.payload.get("name") == "execute_plan"
        ]
        assert len(executed) == 1


def test_same_client_request_id_with_different_body_conflicts(tmp_path: Path) -> None:
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        key = "conflict-key"
        first = h.request(
            "POST", RUNS_PATH, json=_ask_body("2013 年第二季度总交易额", client_request_id=key)
        )
        assert first.status_code == 202, first.text
        second = h.request(
            "POST", RUNS_PATH, json=_ask_body("2013 年全年总交易额", client_request_id=key)
        )
        assert second.status_code == 409, second.text
        error = second.json()["error"]
        assert error["code"] == "idempotency_conflict"
        assert error["request_id"]
        # 原 run 未被覆写，冲突重试不产生第二个运行
        runs = h.control_store.list_runs(owner=_OWNER)
        assert [r.run_id for r in runs] == [first.json()["run_id"]]


def test_invalid_body_combinations_rejected_with_unified_error(tmp_path: Path) -> None:
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        cases: list[tuple[dict[str, object], str]] = [
            (
                {"deployment_id": "finance", "mode": "ask", "client_request_id": "k1"},
                "ask 缺 question",
            ),
            (
                {
                    "deployment_id": "finance",
                    "mode": "ask",
                    "question": "Q",
                    "plan": {"metric": "gmv"},
                    "client_request_id": "k2",
                },
                "ask 不接受 plan",
            ),
            (
                {"deployment_id": "finance", "mode": "execute_plan", "client_request_id": "k3"},
                "execute_plan 缺 plan",
            ),
            (
                {
                    "deployment_id": "finance",
                    "mode": "analyze",
                    "question": "Q",
                    "client_request_id": "k4",
                },
                "analyze 未接通（T05d/T07）",
            ),
            (
                {
                    "deployment_id": "finance",
                    "mode": "unknown",
                    "question": "Q",
                    "client_request_id": "k5",
                },
                "未知 mode",
            ),
            ({"deployment_id": "finance", "mode": "ask", "question": "Q"}, "缺幂等键"),
            (
                {
                    "deployment_id": "finance",
                    "mode": "ask",
                    "question": "Q",
                    "client_request_id": "",
                },
                "空幂等键",
            ),
        ]
        for body, label in cases:
            response = h.request("POST", RUNS_PATH, json=body)
            assert response.status_code == 422, f"{label}：{response.text}"
            error = response.json()["error"]
            assert error["code"] and error["message"] and error["request_id"], label
        # 被拒提交不产生运行事实
        assert h.control_store.list_runs(owner=_OWNER) == []


def test_run_authorization_requires_capability_and_scope(tmp_path: Path) -> None:
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        body = _ask_body("2013 年第二季度总交易额", client_request_id="authz-key")
        assert h.request("POST", RUNS_PATH, actor=None, json=body).status_code == 401
        outsider = h.request("POST", RUNS_PATH, actor="outsider", json=body)
        assert outsider.status_code == 403, outsider.text
        assert outsider.json()["error"]["code"] == "forbidden"
        # 有 run.create 但 scope=retail：目标部署（finance）作用域未授权
        retailer = h.request("POST", RUNS_PATH, actor="retailer", json=body)
        assert retailer.status_code == 403, retailer.text
        # 视图同受 ACL：非 owner 且无 read_summary → 404（对象不可见）
        run_id = h.submit_query("2013 年第二季度总交易额")
        h.drain()
        assert h.request("GET", f"{RUNS_PATH}/{run_id}", actor="outsider").status_code == 404


def test_run_view_fixed_keys_and_available_result(tmp_path: Path) -> None:
    with WorkbenchHarness(tmp_path) as h:
        release_id = h.seed_release()
        run_id = h.submit_query("2013 年第二季度总交易额")
        h.drain()
        view = _view(h, run_id)
        assert set(view) == RUN_VIEW_KEYS
        assert view["run_id"] == run_id
        assert view["release_id"] == release_id
        assert view["status"] == "succeeded"
        assert view["result_availability"] == "available"
        assert view["replay_of"] is None
        assert view["last_seq"] >= 2
        assert view["trace_summary"]["result_kind"] == "answer"
        result = view["result"]
        assert result is not None
        assert result["kind"] == "answer"
        assert result["session_id"] == view["session_id"]
        assert result["columns"] == ["v"]
        assert result["rows"] == [[1]]
        # 测试代理未绑定运行时快照：回显 None 而不是假称绑定（D03/0019 决策 ⑥）
        assert result["snapshot_sha"] is None
        assert view["data_identity"] is None


def test_result_window_expires_after_quiet_period(tmp_path: Path) -> None:
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        run_id = h.submit_query("2013 年第二季度总交易额")
        h.drain()
        assert _view(h, run_id)["result_availability"] == "available"
        h.now += timedelta(minutes=16)
        view = _view(h, run_id)
        assert view["result"] is None
        assert view["result_availability"] == "not_retained"
        # 内容不可用时仍保留结果种类枚举（不伪造答案，D07）
        assert view["trace_summary"]["result_kind"] == "answer"


def test_session_window_counts_from_latest_terminal_turn(tmp_path: Path) -> None:
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        first = h.submit_query("2013 年第二季度总交易额", session_id="session-a")
        h.drain()
        h.now += timedelta(minutes=10)
        h.submit_query("2013 年第二季度总交易额", session_id="session-a")
        h.drain()
        # 首轮距其终态已 20 分钟，但会话最近回合只过 10 分钟 → 内容仍可读
        h.now += timedelta(minutes=10)
        assert _view(h, first)["result_availability"] == "available"
        # 会话静默超窗（16 分钟）→ 全部内容不可读
        h.now += timedelta(minutes=6)
        assert _view(h, first)["result_availability"] == "not_retained"


def test_cross_owner_hidden_but_operator_gets_restricted_summary(tmp_path: Path) -> None:
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        run_id = h.submit_query("2013 年第二季度总交易额")
        h.drain()
        hidden = h.request("GET", f"{RUNS_PATH}/{run_id}", actor="other")
        assert hidden.status_code == 404  # 对象不可见：不泄露存在性
        view = _view(h, run_id, actor="operator")
        assert set(view) == RUN_VIEW_KEYS
        assert view["result"] is None
        assert view["result_availability"] == "restricted"
        # 摘要视图保留结果种类枚举（对象可见但正文无权，D07）
        assert view["trace_summary"]["result_kind"] == "answer"


def test_active_release_switch_does_not_rebind_run(tmp_path: Path) -> None:
    with WorkbenchHarness(tmp_path) as h:
        first_release = h.seed_release()
        run_id = h.submit_query("2013 年第二季度总交易额")
        # 运行在队列期间切换激活发布：已建 run 的绑定不变
        second_release = h.seed_release()
        assert second_release != first_release
        h.drain()
        assert _view(h, run_id)["release_id"] == first_release
        started = [e for e in h.events(run_id) if e.event_type == "RUN_STARTED"]
        assert started and started[0].release_id == first_release


def test_restart_seals_queued_run_without_rerun(tmp_path: Path) -> None:
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        run_id = h.submit_query("2013 年第二季度总交易额")  # 不 drain：任务在队列
        h.restart()
        view = _view(h, run_id)
        assert view["status"] == "interrupted"
        assert view["result"] is None
        assert view["result_availability"] == "not_retained"
        h.drain()  # 新进程队列为空：旧任务不得重跑
        assert h.executor_spy.calls == []
        assert any(e.event_type == "RUN_INTERRUPTED" for e in h.events(run_id))


def test_expired_session_continuation_requires_new_session(tmp_path: Path) -> None:
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        h.submit_query("2013 年第二季度总交易额", session_id="session-old")
        h.drain()
        h.now += timedelta(minutes=16)  # 会话上下文（进程内存）已过期
        expired = h.request(
            "POST",
            RUNS_PATH,
            json=_ask_body(
                "2013 年第二季度总交易额",
                client_request_id="continue-key",
                session_id="session-old",
            ),
        )
        assert expired.status_code == 409, expired.text
        assert expired.json()["error"]["code"] == "session_context_expired"
        # 新会话放行（不悄悄丢弃上下文执行，也不阻塞全新会话）
        fresh = h.request(
            "POST",
            RUNS_PATH,
            json=_ask_body(
                "2013 年第二季度总交易额", client_request_id="fresh-key", session_id="session-new"
            ),
        )
        assert fresh.status_code == 202, fresh.text


def test_failed_execution_projects_error_without_faking_success(tmp_path: Path) -> None:
    """处理完成 ≠ 业务答对：失败按 failed/error 如实投影，不伪装 succeeded。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        h.executor_spy.failure = RuntimeError("executor 故障（测试注入）")
        run_id = h.submit_query("2013 年第二季度总交易额")
        h.drain()
        view = _view(h, run_id)
        assert view["status"] == "failed"
        assert view["trace_summary"]["result_kind"] == "error"
        result = view["result"]
        assert result is not None
        assert result["kind"] == "error"
        assert result["error"]


def test_unknown_deployment_and_missing_run_are_404(tmp_path: Path) -> None:
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        unknown = h.request(
            "POST",
            RUNS_PATH,
            json={
                "deployment_id": "no-such-deployment",
                "mode": "ask",
                "question": "Q",
                "client_request_id": "deploy-key",
            },
        )
        assert unknown.status_code == 404, unknown.text
        assert unknown.json()["error"]["code"] == "not_found"
        missing = h.request("GET", f"{RUNS_PATH}/no-such-run")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"


def test_production_runner_executes_serially_in_order() -> None:
    """生产队列（ThreadTaskRunner）：单工作线程 FIFO，绝不并发执行。"""
    runner = ThreadTaskRunner()
    lock = threading.Lock()
    order: list[str] = []
    inflight = 0
    peak = 0
    done = threading.Event()

    def make(name: str) -> Callable[[], None]:
        def task() -> None:
            nonlocal inflight, peak
            with lock:
                inflight += 1
                peak = max(peak, inflight)
            time.sleep(0.01)
            order.append(name)
            with lock:
                inflight -= 1

        return task

    for name in ("a", "b", "c"):
        runner.submit(make(name))
    runner.submit(done.set)
    assert done.wait(timeout=5), "单业务队列未在期限内排空"
    assert order == ["a", "b", "c"]
    assert peak == 1


# ---------------------------------------------------------------------------
# T06a 历史目录：GET /runs 游标分页/ACL/过滤、GET /sessions、artifact 内容 ACL
# ---------------------------------------------------------------------------

# RunSummary 固定 12 键（D13「GET /runs 游标分页」；不含正文，摘要专用）
RUN_LIST_KEYS = frozenset(
    {
        "run_id",
        "session_id",
        "deployment_id",
        "scope",
        "mode",
        "status",
        "result_availability",
        "result_kind",
        "replay_of",
        "last_seq",
        "created_at",
        "updated_at",
    }
)
# SessionSummary 固定 6 键（历史目录聚合行；回合经 GET /sessions/{id} 取）
SESSION_LIST_KEYS = frozenset(
    {"session_id", "deployment_id", "scope", "run_count", "last_run_at", "last_status"}
)
# ArtifactView 固定 10 键（内容只按内容 ACL 与保留期发放）
ARTIFACT_KEYS = frozenset(
    {
        "artifact_id",
        "run_id",
        "kind",
        "purpose",
        "fields",
        "content",
        "content_digest",
        "retain_until",
        "cleaned_at",
        "created_at",
    }
)


def _list_page(h: WorkbenchHarness, *, actor: str | None = "viewer", **params: object):
    response = h.request("GET", RUNS_PATH, actor=actor, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _sessions_page(h: WorkbenchHarness, *, actor: str | None = "viewer", **params: object):
    response = h.request("GET", SESSIONS_PATH, actor=actor, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_run_list_paginates_by_cursor_with_fixed_summary_keys(tmp_path: Path) -> None:
    """游标分页（新→旧）+ 本人可见：刷新后不靠标签页内存即可检索授权历史。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        first = h.submit_query("2013 年第二季度总交易额", session_id="s1")
        h.drain()
        h.now += timedelta(seconds=1)
        second = h.submit_query("2013 年第二季度总交易额", session_id="s1")
        h.drain()
        h.now += timedelta(seconds=1)
        third = h.submit_query("2013 年全年总交易额", session_id="s2")
        h.drain()

        page = _list_page(h)
        assert [item["run_id"] for item in page["items"]] == [third, second, first]
        assert page["next_cursor"] is None
        assert set(page["items"][0]) == RUN_LIST_KEYS
        assert page["items"][0]["scope"] == "finance"
        assert page["items"][0]["result_kind"] == "answer"

        first_page = _list_page(h, limit=2)
        assert [item["run_id"] for item in first_page["items"]] == [third, second]
        assert first_page["next_cursor"] is not None
        second_page = _list_page(h, limit=2, cursor=first_page["next_cursor"])
        assert [item["run_id"] for item in second_page["items"]] == [first]
        assert second_page["next_cursor"] is None

        # 默认分页（不传 limit）与显式 limit=50 同构（D13：默认 50、最多 100）
        assert _list_page(h, limit=50)["items"] == page["items"]
        # 跨人不可见：同能力另一用户目录为空（不泄露他人运行存在性）
        assert _list_page(h, actor="other")["items"] == []


def test_run_list_filters_scope_status_time_and_validates_params(tmp_path: Path) -> None:
    """过滤与参数校验：域/状态/时间；未授权域 403；非法参数统一 422 错误体。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        done = h.submit_query("2013 年第二季度总交易额", session_id="s-done")
        h.drain()
        h.now += timedelta(seconds=5)
        queued = h.submit_query("2013 年第二季度总交易额", session_id="s-queued")  # 不 drain

        assert [i["run_id"] for i in _list_page(h, status="queued")["items"]] == [queued]
        assert [i["run_id"] for i in _list_page(h, status="succeeded")["items"]] == [done]
        assert [i["run_id"] for i in _list_page(h, session_id="s-queued")["items"]] == [queued]
        # 时间过滤以服务端 created_at 为事实源（真时钟；测试假时钟只驱动窗口/保留期）：
        # 取最旧行的创建时刻做边界——since 含边界行（>=），until 只留 done（<=）。
        rows = _list_page(h)["items"]
        assert [i["run_id"] for i in rows] == [queued, done]
        boundary = rows[-1]["created_at"]
        assert [i["run_id"] for i in _list_page(h, since=boundary)["items"]] == [queued, done]
        assert [i["run_id"] for i in _list_page(h, until=boundary)["items"]] == [done]
        # 授权域命中；未授权域不泄露（403，不是空列表也不是 404）
        assert [i["run_id"] for i in _list_page(h, scope="finance")["items"]] == [queued, done]
        forbidden = h.request("GET", RUNS_PATH, params={"scope": "retail"})
        assert forbidden.status_code == 403, forbidden.text
        assert forbidden.json()["error"]["code"] == "forbidden"
        # operator（read_summary）：可见同域摘要行，无内容面
        summary = _list_page(h, actor="operator")
        assert [i["run_id"] for i in summary["items"]] == [queued, done]
        assert set(summary["items"][0]) == RUN_LIST_KEYS
        # 零授予 403；未认证 401
        assert h.request("GET", RUNS_PATH, actor="outsider").status_code == 403
        assert h.request("GET", RUNS_PATH, actor=None).status_code == 401
        # 非法参数：limit 越界/非整数、未知状态、坏时间、坏游标 一律 422 统一体
        for params in (
            {"limit": 0},
            {"limit": 101},
            {"limit": "x"},
            {"status": "bogus"},
            {"since": "not-a-time"},
            {"cursor": "no-such-run"},
        ):
            bad = h.request("GET", RUNS_PATH, params=params)
            assert bad.status_code == 422, (params, bad.text)
            assert bad.json()["error"]["code"] == "invalid_request"


def test_sessions_directory_and_turns_come_from_control_facts(tmp_path: Path) -> None:
    """会话目录与回合：控制库事实聚合（非 checkpoint dump）；本人可见、他人 404。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        a1 = h.submit_query("2013 年第二季度总交易额", session_id="session-a")
        h.drain()
        h.now += timedelta(seconds=1)
        a2 = h.submit_query("2013 年第二季度总交易额", session_id="session-a")
        h.drain()
        h.now += timedelta(seconds=1)
        h.submit_query("2013 年全年总交易额", session_id="session-b")
        h.drain()

        page = _sessions_page(h)
        rows = page["items"]
        assert [row["session_id"] for row in rows] == ["session-b", "session-a"]
        assert set(rows[0]) == SESSION_LIST_KEYS
        assert rows[0]["run_count"] == 1
        assert rows[1]["run_count"] == 2
        assert rows[1]["last_status"] == "succeeded"
        assert rows[1]["deployment_id"] == "finance" and rows[1]["scope"] == "finance"

        first_page = _sessions_page(h, limit=1)
        assert [row["session_id"] for row in first_page["items"]] == ["session-b"]
        assert first_page["next_cursor"] is not None
        second_page = _sessions_page(h, limit=1, cursor=first_page["next_cursor"])
        assert [row["session_id"] for row in second_page["items"]] == ["session-a"]
        assert second_page["next_cursor"] is None

        turns = h.request("GET", f"{SESSIONS_PATH}/session-a")
        assert turns.status_code == 200, turns.text
        payload = turns.json()
        assert [item["run_id"] for item in payload["items"]] == [a2, a1]
        assert set(payload["items"][0]) == RUN_LIST_KEYS
        assert payload["next_cursor"] is None

        # 未知会话 404；他人会话不可见（同 404）；跨人目录为空；operator 无本人目录
        assert h.request("GET", f"{SESSIONS_PATH}/session-none").status_code == 404
        assert h.request("GET", f"{SESSIONS_PATH}/session-a", actor="other").status_code == 404
        assert _sessions_page(h, actor="other")["items"] == []
        assert h.request("GET", SESSIONS_PATH, actor="operator").status_code == 403
        # 非法分页参数统一 422
        assert h.request("GET", SESSIONS_PATH, params={"limit": 0}).status_code == 422


def test_artifact_content_acl_and_cleaned_tombstone(tmp_path: Path) -> None:
    """内容 ACL：本人 200 / 摘要 403 / 他人 404；清理后 410 且 expired 投影。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        question = "2013 年第二季度总交易额"
        grant = {
            "purpose": "debug-q2",
            "fields": ["question"],
            "retain_until": (h.now + timedelta(days=3)).isoformat(),
        }
        response = h.request(
            "POST",
            RUNS_PATH,
            json=_ask_body(question, client_request_id="artifact-key", capture=grant),
        )
        assert response.status_code == 202, response.text
        run_id = str(response.json()["run_id"])
        h.drain()
        snapshot = [e for e in h.events(run_id) if e.event_type == "STATE_SNAPSHOT"][-1]
        artifact_id = str(snapshot.payload["artifacts"][0])
        path = f"{RUNS_PATH}/{run_id}/artifacts/{artifact_id}"

        ok = h.request("GET", path)
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert set(body) == ARTIFACT_KEYS
        assert body["content"] == {"question": question}
        assert body["fields"] == ["question"]
        assert body["cleaned_at"] is None and body["content_digest"]
        # 摘要身份无内容权限（403，不是 404：对象本身可见）；他人 404；未知/错配 404
        assert h.request("GET", path, actor="operator").status_code == 403
        assert h.request("GET", path, actor="other").status_code == 404
        assert h.request("GET", f"{RUNS_PATH}/{run_id}/artifacts/nope").status_code == 404
        other_run = h.submit_query(question, session_id="s-other")
        h.drain()
        assert (
            h.request("GET", f"{RUNS_PATH}/{other_run}/artifacts/{artifact_id}").status_code == 404
        )

        # 保留期到期：清理正文保墓碑 → 410（不从 checkpoint 绕过），availability=expired
        assert h.control_store.clean_expired_artifacts(now=h.now + timedelta(days=8)) == 1
        gone = h.request("GET", path)
        assert gone.status_code == 410, gone.text
        assert gone.json()["error"]["code"] == "artifact_expired"
        view = _view(h, run_id)
        assert view["result_availability"] == "expired"
        listed = [i for i in _list_page(h)["items"] if i["run_id"] == run_id][0]
        assert listed["result_availability"] == "expired"


def test_guard_rejected_sql_absent_from_artifact(tmp_path: Path) -> None:
    """被拒 SQL 不进 artifact/SSE（只在作答时物化 node_io）；执行器零调用。"""
    from agent.graph import DataAgent
    from agent.security.sql_guard import Budget
    from serving.control.events import StoreEventSink

    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        # 真实图 + 真实 Guard + 拒绝预算（允许表集不含本模型表）：SQL 被 Guard 拒绝
        deny = Budget(
            dialect="doris", max_rows=10_000, allowed_tables=frozenset({"atlas.dws.other"})
        )
        h._run_agents["finance"] = DataAgent(
            model=h._load_model("finance"),
            executor=h.executor_spy,
            budget=deny,
            event_sink=StoreEventSink(h.control_store),
        )
        question = "2013 年第二季度总交易额"
        grant = {
            "purpose": "deny-probe",
            "fields": ["question", "node_io", "result"],
            "retain_until": (h.now + timedelta(days=3)).isoformat(),
        }
        response = h.request(
            "POST",
            RUNS_PATH,
            json=_ask_body(question, client_request_id="deny-key", capture=grant),
        )
        assert response.status_code == 202, response.text
        run_id = str(response.json()["run_id"])
        h.drain()

        view = _view(h, run_id)
        assert view["status"] == "blocked"
        assert view["trace_summary"]["result_kind"] == "blocked"
        assert view["result"] is not None and view["result"]["sql"] is None
        assert h.executor_spy.calls == []  # Guard 拒绝在执行前

        snapshot = [e for e in h.events(run_id) if e.event_type == "STATE_SNAPSHOT"][-1]
        artifact_id = str(snapshot.payload["artifacts"][0])
        body = h.request("GET", f"{RUNS_PATH}/{run_id}/artifacts/{artifact_id}").json()
        content = body["content"]
        assert "node_io" not in content  # 只在作答时物化
        assert content["result"]["sql"] is None
        text = json.dumps(content, ensure_ascii=False)
        assert "select" not in text.lower(), "被拒 SQL 不得出现在 artifact"

        # 持久事件同样不含被拒 SQL/问句/结果行（摘要通道，D07）
        for event in h.events(run_id):
            event_text = json.dumps(event.payload, ensure_ascii=False)
            assert "select" not in event_text.lower()
            assert question not in event_text

        # SSE 面同源（同一持久事实）：被拒 SQL/问句原文不以任何形式出网
        stream = h.request("GET", f"{RUNS_PATH}/{run_id}/events")
        assert stream.status_code == 200, stream.text
        assert "select" not in stream.text.lower()
        assert question not in stream.text


# ---------------------------------------------------------------------------
# T06b：真 SSE 事件流（GET /runs/{run_id}/events；D07 持久事实续读）
# ---------------------------------------------------------------------------

RUN_EVENT_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "seq",
        "event_id",
        "occurred_at",
        "node_id",
        "node_run_id",
        "parent_node_run_id",
        "attempt",
        "event_type",
        "release_id",
        "payload",
    }
)


def _events_path(run_id: str) -> str:
    return f"{RUNS_PATH}/{run_id}/events"


def _parse_run_frames(text: str) -> list[tuple[int, str, dict]]:
    """SSE 文本 → [(seq, event_type, data)]；`: ping` 注释帧不参与断言。"""
    frames: list[tuple[int, str, dict]] = []
    for frame in text.split("\n\n"):
        if not frame.strip() or frame.lstrip().startswith(":"):
            continue
        seq = event = data = None
        for line in frame.splitlines():
            if line.startswith("id: "):
                seq = int(line[len("id: ") :])
            elif line.startswith("event: "):
                event = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        assert seq is not None and event is not None and data is not None, frame
        frames.append((seq, event, data))
    return frames


def test_run_events_sse_replays_persisted_frames_with_seq_ids(tmp_path: Path) -> None:
    """终态后全量 replay：id/event/data 三行帧、seq 连续、末帧 RUN_FINISHED；重连不重跑。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        run_id = h.submit_query("2013 年第二季度总交易额", session_id="sse-1")
        h.drain()
        assert len(h.executor_spy.calls) == 1

        response = h.request("GET", _events_path(run_id))
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        frames = _parse_run_frames(response.text)
        assert [seq for seq, _, _ in frames] == list(range(1, len(frames) + 1))
        assert frames[0][1] == "RUN_ACCEPTED"
        assert frames[-1][1] == "RUN_FINISHED"
        assert set(frames[-1][2]) == RUN_EVENT_KEYS
        assert frames[-1][2]["schema_version"] == 1
        assert frames[-1][2]["payload"]["status"] == "succeeded"
        assert all(frame[2]["run_id"] == run_id for frame in frames)

        # 刷新/重连 = 再读一遍持久事实：不重做查询（D07：SSE 只断开订阅）
        again = h.request("GET", _events_path(run_id))
        assert _parse_run_frames(again.text) == frames
        assert len(h.executor_spy.calls) == 1


def test_run_events_sse_resumes_from_last_event_id_or_after_seq(tmp_path: Path) -> None:
    """断线续读：after_seq 优先于 Last-Event-ID；非法游标 422。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        run_id = h.submit_query("2013 年第二季度总交易额", session_id="sse-2")
        h.drain()

        tail = h.request("GET", _events_path(run_id), params={"after_seq": 2})
        assert tail.status_code == 200, tail.text
        frames = _parse_run_frames(tail.text)
        assert frames[0][0] == 3 and frames[-1][1] == "RUN_FINISHED"

        # Last-Event-ID：断线重连标准头（浏览器 EventSource 重发内容）
        resumed = h.request("GET", _events_path(run_id), headers={"Last-Event-ID": "3"})
        assert _parse_run_frames(resumed.text)[0][0] == 4

        # 两者并存：显式 after_seq 优先（头可能是浏览器残留的旧游标）
        both = h.request(
            "GET",
            _events_path(run_id),
            params={"after_seq": 2},
            headers={"Last-Event-ID": "1"},
        )
        assert _parse_run_frames(both.text)[0][0] == 3

        for bad in ("x", "-1", ""):
            invalid = h.request("GET", _events_path(run_id), params={"after_seq": bad})
            assert invalid.status_code == 422, (bad, invalid.text)


def test_run_events_sse_returns_410_when_unresumable(tmp_path: Path) -> None:
    """无法补齐 → 410 event_gap（脏游标 / 保留期清理后的洞）；从头读不受影响。"""
    import sqlite3

    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        run_id = h.submit_query("2013 年第二季度总交易额", session_id="sse-3")
        h.drain()

        # 脏游标：声称读过的 seq 超出该 run 的事件范围（无法补齐）
        over = h.request("GET", _events_path(run_id), params={"after_seq": 99})
        assert over.status_code == 410, over.text
        assert over.json()["error"]["code"] == "event_gap"

        # 模拟 30 天保留期清理：前 2 条已删除（append-only 由触发器守护，
        # 测试为构造「历史已清理」状态显式解除删除保护）
        with sqlite3.connect(h.control_store.path) as conn:
            conn.execute("DROP TRIGGER immutable_run_event_delete")
            conn.execute("DELETE FROM run_events WHERE seq <= 2")
        gap = h.request("GET", _events_path(run_id), params={"after_seq": 1})
        assert gap.status_code == 410, gap.text
        assert gap.json()["error"]["code"] == "event_gap"
        # 从头读（after_seq 省略 = 0）不构成 gap：从现存最早 seq 开始
        head = _parse_run_frames(h.request("GET", _events_path(run_id)).text)
        assert head[0][0] == 3


def test_run_events_sse_acl_and_expired_token(tmp_path: Path) -> None:
    """建立连接即重新授权：401/404/403；过期 Bearer 同样 401（不泄露细节）。"""
    from serving.auth import sign_token

    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        run_id = h.submit_query("2013 年第二季度总交易额", session_id="sse-4")
        h.drain()
        path = _events_path(run_id)

        assert h.request("GET", path, actor=None).status_code == 401
        assert h.request("GET", path, actor="other").status_code == 404  # 跨人原文拒绝
        assert h.request("GET", path, actor="operator").status_code == 403  # 摘要无事件面
        assert h.request("GET", path, actor="outsider").status_code == 404
        assert h.request("GET", _events_path("no-such-run")).status_code == 404

        # 过期授权：exp 已过的 Bearer 在建立时被拒（拿不到任何事件帧）
        expired = sign_token("hq_admin", {}, secret=h._secret, subject="viewer", ttl=-5)
        response = h.client.request("GET", path, headers={"Authorization": f"Bearer {expired}"})
        assert response.status_code == 401


def test_run_events_sse_first_frame_ready_while_execution_blocked(tmp_path: Path) -> None:
    """阻塞期首帧可读（不等执行完成）；drain 后补齐并关闭；读流不触发执行。"""
    from serving.control.router import stream_run_events

    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        run_id = h.submit_query("2013 年第二季度总交易额", session_id="sse-live")
        assert h.executor_spy.calls == []  # 任务仍在队列（connector 阻塞中）

        frames = stream_run_events(
            h.control_store,
            run_id=run_id,
            after_seq=0,
            poll_interval=0.005,
            ping_every=1,
        )
        first = next(frames)
        assert first.startswith("id: 1\nevent: RUN_ACCEPTED\n")
        assert h.executor_spy.calls == []  # 首帧来自持久事实，未触发执行

        assert next(frames) == ": ping\n\n"  # 等待期心跳（注释帧）

        h.drain()  # 释放 connector：真实执行
        rest = "".join(frames)  # 发完终态后生成器自行结束
        parsed = _parse_run_frames(first + rest)
        assert [seq for seq, _, _ in parsed] == list(range(1, len(parsed) + 1))
        assert parsed[-1][1] == "RUN_FINISHED"
        assert len(h.executor_spy.calls) == 1  # 恰好一次：读流不重复执行
