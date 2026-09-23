"""ADR-0031 T05d：最小反馈采集与显式捕获（D13）的 HTTP 级验收（真实 API/Guard/事件盘）。

口径（与 D07/D13 逐条对应）：

- `POST /feedback` 201 恒 9 键；未审核固定 pending_review / training_eligible=False
  （T12 前不开放训练）；`GET /feedback` 只列本人（新→旧）；越权关联一律 404
  （与「不存在」同一投影，不泄露存在性）；
- feedback.submit 能力先于对象判定：无该能力一律 403（operator/outsider）；
  retailer 有该能力但 scope 不含 finance → 404（对象不可见）；
- capture 三要素 `{purpose, fields, retain_until}`：字段白名单是内容硬边界
  （artifact 内容键 ⊆ 授权字段）；期限 ≤ 7 天，服务层以注入时钟校验 → 422
  `capture_invalid`；被拒提交零运行事实、零制品；
- 能力与数据授权独立：请求里填了 capture 不等于获得权限（retailer 403，D13）；
- 授权审计（D13「服务校验并审计授权」）：创建成功后写 capture_authorized 行；
  审计写失败 fail-closed（D07）——queued 直接封 failed、零执行、零制品、503；
- `STATE_SNAPSHOT` 只带 artifact 引用（脱敏；无 capture 时为空列表）；
- 幂等键绑定 capture：同键不同 grant → 409；同 body 重试不重复审计、不重复执行。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

from serving.api import API_PREFIX
from tests.workbench_support import RUNS_PATH, WorkbenchHarness

FEEDBACK_PATH = f"{API_PREFIX}/feedback"

FEEDBACK_KEYS = frozenset(
    {
        "feedback_id",
        "run_id",
        "owner",
        "verdict",
        "comment",
        "correction",
        "status",
        "training_eligible",
        "created_at",
        "attribution_node",  # T12 新增：节点级归因
        "reviewed_by",  # T12 新增：审核者
        "reviewed_at",  # T12 新增：审核时间
    }
)


def _capture_grant(
    h: WorkbenchHarness,
    *,
    fields: tuple[str, ...] = ("question", "result"),
    days: int = 3,
    purpose: str = "debug-q2",
) -> dict[str, object]:
    """显式捕获授权（D13）：期限由注入时钟推导并带显式时区（裸时间被合同拒绝）。"""
    return {
        "purpose": purpose,
        "fields": list(fields),
        "retain_until": (h.now + timedelta(days=days)).isoformat(),
    }


def _submit(
    h: WorkbenchHarness,
    *,
    client_request_id: str,
    capture: dict[str, object] | None = None,
    actor: str | None = "viewer",
):
    """经真实 POST /runs 提交（capture 可选）；返回原始响应供状态码断言。"""
    body: dict[str, object] = {
        "deployment_id": "finance",
        "mode": "ask",
        "question": "2013 年第二季度总交易额",
        "client_request_id": client_request_id,
    }
    if capture is not None:
        body["capture"] = capture
    return h.request("POST", RUNS_PATH, actor=actor, json=body)


def _run(
    h: WorkbenchHarness,
    *,
    client_request_id: str = "fb-run",
    capture: dict[str, object] | None = None,
) -> str:
    """提交并执行到终态；返回 run_id。"""
    response = _submit(h, client_request_id=client_request_id, capture=capture)
    assert response.status_code == 202, response.text
    run_id = str(response.json()["run_id"])
    h.drain()
    return run_id


def _feedback(h: WorkbenchHarness, run_id: str, **body: object):
    payload: dict[str, object] = {"run_id": run_id, **body}
    return h.request("POST", FEEDBACK_PATH, json=payload)


def _rows(h: WorkbenchHarness, table: str) -> list[dict[str, object]]:
    """直读控制库（零信任断言：不经过服务层投影）。"""
    with sqlite3.connect(h.control_store.path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]


def _audit_rows(h: WorkbenchHarness) -> list[dict[str, object]]:
    """读业务审计 JSONL（不存在的文件 = 零行）。"""
    if not h.audit_path.exists():
        return []
    return [
        json.loads(line) for line in h.audit_path.read_text(encoding="utf-8").splitlines() if line
    ]


def _artifacts_of(h: WorkbenchHarness, run_id: str) -> list[dict[str, object]]:
    return [row for row in _rows(h, "artifacts") if row["run_id"] == run_id]


def _snapshot_artifacts(h: WorkbenchHarness, run_id: str) -> list[str]:
    """STATE_SNAPSHOT 的 artifact 引用列表（脱敏事件通道，D07）。"""
    snapshot = next(e for e in h.events(run_id) if e.event_type == "STATE_SNAPSHOT")
    raw = snapshot.payload["artifacts"]
    assert isinstance(raw, list)
    return [str(item) for item in raw]


def test_feedback_submit_and_list_own(tmp_path: Path) -> None:
    """201 恒 12 键（T12 新增 attribution_node/reviewed_by/reviewed_at）、固定 pending_review/False；列表只列本人且新→旧。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        run_id = _run(h)
        first = _feedback(h, run_id, verdict="up")
        assert first.status_code == 201, first.text
        payload = first.json()
        assert set(payload) == FEEDBACK_KEYS
        assert payload["run_id"] == run_id
        assert payload["verdict"] == "up"
        assert payload["comment"] is None
        assert payload["correction"] is None
        assert payload["owner"] == {"issuer": "atlas-local", "subject": "viewer"}
        assert payload["status"] == "pending_review"
        assert payload["training_eligible"] is False
        assert payload["feedback_id"] and payload["created_at"]
        # T12 新增字段：未审核反馈的归因/审核证据为 None
        assert payload["attribution_node"] is None
        assert payload["reviewed_by"] is None
        assert payload["reviewed_at"] is None
        second = _feedback(
            h, run_id, verdict="corrected", comment="口径应为佣金", correction={"rows": [[2]]}
        )
        assert second.status_code == 201, second.text
        assert second.json()["correction"] == {"rows": [[2]]}
        assert second.json()["comment"] == "口径应为佣金"
        listed = h.request("GET", FEEDBACK_PATH)
        assert listed.status_code == 200, listed.text
        items = listed.json()["items"]
        assert set(items[0]) == FEEDBACK_KEYS
        assert [item["feedback_id"] for item in items] == [
            second.json()["feedback_id"],
            payload["feedback_id"],
        ]
        assert [item["verdict"] for item in items] == ["corrected", "up"]


def test_feedback_rejects_cross_owner_and_unknown_run(tmp_path: Path) -> None:
    """越权关联与未知 run 同一 404 投影；他人列表不反推存在性。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        run_id = _run(h)
        foreign = h.request(
            "POST", FEEDBACK_PATH, actor="other", json={"run_id": run_id, "verdict": "up"}
        )
        assert foreign.status_code == 404, foreign.text
        assert foreign.json()["error"]["code"] == "not_found"
        other_list = h.request("GET", FEEDBACK_PATH, actor="other")
        assert other_list.status_code == 200
        assert other_list.json()["items"] == []
        missing = _feedback(h, "no-such-run", verdict="up")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"
        # 被拒的越权/未知关联零落库
        assert _rows(h, "feedback") == []


def test_feedback_requires_capability_and_scope(tmp_path: Path) -> None:
    """能力先判（403）；有能力的跨域主体按对象不可见投影（404）。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        run_id = _run(h)
        operator = h.request(
            "POST", FEEDBACK_PATH, actor="operator", json={"run_id": run_id, "verdict": "up"}
        )
        assert operator.status_code == 403, operator.text
        assert operator.json()["error"]["code"] == "forbidden"
        assert h.request("GET", FEEDBACK_PATH, actor="outsider").status_code == 403
        retailer = h.request(
            "POST", FEEDBACK_PATH, actor="retailer", json={"run_id": run_id, "verdict": "up"}
        )
        assert retailer.status_code == 404, retailer.text
        assert (
            h.request(
                "POST", FEEDBACK_PATH, actor=None, json={"run_id": run_id, "verdict": "up"}
            ).status_code
            == 401
        )
        assert _rows(h, "feedback") == []


def test_feedback_validation_rejected(tmp_path: Path) -> None:
    """合同矩阵：被拒请求零落库（feedback 表零行）。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        run_id = _run(h)
        cases: list[dict[str, object]] = [
            {"run_id": run_id, "verdict": "maybe"},
            {"run_id": run_id, "verdict": "up", "comment": "x" * 2001},
            {"verdict": "up"},
            {"run_id": run_id},
            {"run_id": run_id, "verdict": "up", "extra": 1},
            {"run_id": "bad id!", "verdict": "up"},
        ]
        for index, body in enumerate(cases):
            response = h.request("POST", FEEDBACK_PATH, json=body)
            assert response.status_code == 422, f"case {index}：{response.text}"
            assert response.json()["error"]["code"] == "invalid_request", f"case {index}"
        assert _rows(h, "feedback") == []


def test_capture_writes_explicit_artifact(tmp_path: Path) -> None:
    """显式捕获：授权审计在建单时、正文制品在执行后；STATE_SNAPSHOT 只带引用。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        grant = _capture_grant(h, purpose="debug-q2")
        response = _submit(h, client_request_id="capture-key", capture=grant)
        assert response.status_code == 202, response.text
        run_id = str(response.json()["run_id"])
        # 授权审计发生在 create 之后（此刻制品尚不存在：正文要等执行完）
        assert len(_audit_rows(h)) == 1
        assert _rows(h, "artifacts") == []
        h.drain()
        artifact_ids = _snapshot_artifacts(h, run_id)
        assert len(artifact_ids) == 1
        assert [row["artifact_id"] for row in _artifacts_of(h, run_id)] == artifact_ids
        artifact = h.control_store.get_artifact(artifact_ids[0])
        assert artifact.run_id == run_id
        assert artifact.kind == "capture"
        assert artifact.purpose == "debug-q2"
        assert artifact.fields == frozenset({"question", "result"})
        assert set(artifact.content) == {"question", "result"}
        assert artifact.content["question"] == "2013 年第二季度总交易额"
        result = artifact.content["result"]
        assert result["kind"] == "answer"
        assert result["sql"] == h.executor_spy.calls[0]
        assert result["rows"] == [[1]]
        assert artifact.content_digest is not None
        assert artifact.retain_until == grant["retain_until"]
        assert artifact.cleaned_at is None


def test_capture_writes_only_authorized_fields(tmp_path: Path) -> None:
    """字段白名单是硬边界；非作答结果不物化 node_io（不落被拒 SQL，D07）。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        run_id = _run(
            h,
            client_request_id="subset-key",
            capture=_capture_grant(h, fields=("question", "node_io")),
        )
        artifact = h.control_store.get_artifact(_snapshot_artifacts(h, run_id)[0])
        assert set(artifact.content) == {"question", "node_io"}
        node_io = artifact.content["node_io"]
        assert set(node_io) == {"sql", "explanation"}
        assert node_io["sql"] == h.executor_spy.calls[0]
        assert "result" not in artifact.content  # 未授权字段不因「可用」而落盘
        # 执行失败（error，非 answer）：node_io 不物化 → 无可捕获内容，不产出空制品
        h.executor_spy.failure = RuntimeError("executor 故障（测试注入）")
        failed = _run(
            h, client_request_id="subset-key-2", capture=_capture_grant(h, fields=("node_io",))
        )
        assert _snapshot_artifacts(h, failed) == []
        assert _artifacts_of(h, failed) == []


def test_capture_rejects_invalid_grant(tmp_path: Path) -> None:
    """期限超上限 → capture_invalid；合同形态错 → invalid_request；均零运行事实。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        over = _submit(h, client_request_id="cap-over", capture=_capture_grant(h, days=8))
        assert over.status_code == 422, over.text
        assert over.json()["error"]["code"] == "capture_invalid"
        until = (h.now + timedelta(days=1)).isoformat()
        cases: list[dict[str, object]] = [
            {"purpose": "p", "fields": ["question"], "retain_until": "2026-09-25T12:00:00"},
            {"purpose": "p", "fields": ["prompt"], "retain_until": until},
            {"purpose": "p", "fields": [], "retain_until": until},
        ]
        for index, capture in enumerate(cases):
            rejected = _submit(h, client_request_id=f"cap-invalid-{index}", capture=capture)
            assert rejected.status_code == 422, f"case {index}：{rejected.text}"
            assert rejected.json()["error"]["code"] == "invalid_request", f"case {index}"
        assert _rows(h, "runs") == []
        assert _rows(h, "artifacts") == []


def test_capture_fields_do_not_grant_permission(tmp_path: Path) -> None:
    """「不把请求中填了字段当成权限授予」（D13）：capture 不改变数据权限判定。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        denied = _submit(
            h, client_request_id="perm-key", capture=_capture_grant(h), actor="retailer"
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "forbidden"
        assert _rows(h, "runs") == []
        assert _rows(h, "artifacts") == []
        assert _audit_rows(h) == []  # 未授权请求不产生 capture 授权审计行


def test_capture_audit_fail_closed(tmp_path: Path) -> None:
    """审计写失败 → 下一次 SQL 不得启动（D07）：封 failed、零执行、零制品、503。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        h.capture_audit_failure = OSError("审计盘故障（测试注入）")
        blocked = _submit(h, client_request_id="audit-key", capture=_capture_grant(h))
        assert blocked.status_code == 503, blocked.text
        assert blocked.json()["error"]["code"] == "audit_unavailable"
        runs = _rows(h, "runs")
        assert len(runs) == 1
        assert runs[0]["status"] == "failed"
        assert runs[0]["result_kind"] == "error"
        h.drain()  # 未入队：队列为空，SQL 零启动
        assert h.executor_spy.calls == []
        assert _rows(h, "artifacts") == []
        # 审计恢复后：新 key 提交同一授权形态正常执行（幂等重试不会重新授权，故换 key）
        h.capture_audit_failure = None
        ok = _run(h, client_request_id="audit-key-2", capture=_capture_grant(h))
        assert _snapshot_artifacts(h, ok) != []
        assert len(_audit_rows(h)) == 1  # 仅恢复后的那次授权审计


def test_capture_audit_row_and_default_no_capture(tmp_path: Path) -> None:
    """无 capture：零审计行、零制品、STATE_SNAPSHOT 空引用；有 capture：恰一行。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        plain = _run(h, client_request_id="plain-key")
        assert _snapshot_artifacts(h, plain) == []
        assert _rows(h, "artifacts") == []
        assert _audit_rows(h) == []
        _run(h, client_request_id="cap-key", capture=_capture_grant(h, purpose="audit-probe"))
        rows = _audit_rows(h)
        assert len(rows) == 1
        assert rows[0]["endpoint"] == RUNS_PATH
        assert rows[0]["kind"] == "capture_authorized"
        assert rows[0]["claims"] == {"role": "hq_admin", "sub": "viewer"}
        assert rows[0]["bucket"] == "business"


def test_capture_idempotency_key_binds_grant(tmp_path: Path) -> None:
    """幂等键绑定 capture：同键不同 grant → 409；同 body 重试不重复审计/执行。"""
    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        grant = _capture_grant(h, purpose="purpose-a")
        first = _submit(h, client_request_id="cap-idem", capture=grant)
        assert first.status_code == 202, first.text
        run_id = str(first.json()["run_id"])
        changed = _submit(
            h, client_request_id="cap-idem", capture=_capture_grant(h, purpose="purpose-b")
        )
        assert changed.status_code == 409, changed.text
        assert changed.json()["error"]["code"] == "idempotency_conflict"
        retry = _submit(h, client_request_id="cap-idem", capture=grant)
        assert retry.status_code == 202, retry.text
        assert retry.json()["run_id"] == run_id
        h.drain()
        assert len(h.executor_spy.calls) == 1
        assert len(_audit_rows(h)) == 1
        artifacts = _artifacts_of(h, run_id)
        assert len(artifacts) == 1
        assert artifacts[0]["purpose"] == "purpose-a"
