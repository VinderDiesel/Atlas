"""T05 运行生命周期存储原语：幂等创建、终态封存、启动中断与反馈（真实 SQLite，不连接业务源）。

HTTP 层（202/409/404 投影、单业务队列、15 分钟窗口）由 test_run_api 系列在
T05c 追加；本文件只锁存储契约，不经过 FastAPI。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest


def _store(tmp_path: Path):
    from serving.control.store import ControlStore

    store = ControlStore(tmp_path / "state" / "control.sqlite")
    store.migrate()
    return store


def _owner(subject: str = "viewer"):
    from serving.control.contracts import Owner

    return Owner(issuer="https://issuer.example", subject=subject)


def _deployment(store) -> None:
    store.create_deployment(
        "finance", owner=_owner("publisher"), scope="finance", source_id="atlas_finance"
    )


def _submit(
    store,
    *,
    owner=None,
    client_request_id: str = "req-1",
    question: str = "2013 年第二季度总交易额",
):
    from serving.control.contracts import content_digest

    return store.create_run(
        owner=owner or _owner(),
        deployment_id="finance",
        mode="ask",
        session_id="session-1",
        client_request_id=client_request_id,
        request_digest=content_digest({"question": question}),
    )


def test_same_key_same_body_returns_original_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _deployment(store)
    first, created_first = _submit(store)
    second, created_second = _submit(store)
    assert created_first is True
    assert created_second is False
    assert first.run_id == second.run_id
    assert first.status == "queued"
    assert first.result_availability == "pending"
    assert first.release_id is None  # 部署尚未发布：不假称绑定了发布
    assert store.get_run(first.run_id).last_seq == 0


def test_same_key_different_body_raises_conflict(tmp_path: Path) -> None:
    from serving.control.store import RunConflict

    store = _store(tmp_path)
    _deployment(store)
    _submit(store)
    with pytest.raises(RunConflict):
        _submit(store, question="换成另一个问题")


def test_concurrent_same_key_creates_exactly_one_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _deployment(store)
    barrier = Barrier(8)

    def attempt(_: int) -> bool:
        barrier.wait()
        _, created = _submit(store)
        return created

    with ThreadPoolExecutor(max_workers=8) as pool:
        created_flags = list(pool.map(attempt, range(8)))
    assert created_flags.count(True) == 1
    assert len(store.list_runs(owner=_owner())) == 1


def test_same_key_for_different_owners_creates_distinct_runs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _deployment(store)
    first, _ = _submit(store, owner=_owner("viewer"))
    second, created = _submit(store, owner=_owner("other"))
    assert created is True
    assert first.run_id != second.run_id


def test_unknown_deployment_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.migrate()  # 无 deployment 行
    from serving.control.contracts import Owner, content_digest

    with pytest.raises(KeyError):
        store.create_run(
            owner=Owner(issuer="https://issuer.example", subject="viewer"),
            deployment_id="finance",
            mode="ask",
            session_id="session-1",
            client_request_id="req-1",
            request_digest=content_digest({"question": "x"}),
        )


def test_terminal_run_is_sealed_and_retry_returns_original(tmp_path: Path) -> None:
    from serving.control.store import RunConflict

    store = _store(tmp_path)
    _deployment(store)
    run, _ = _submit(store)
    store.finalize_run(
        run.run_id, status="succeeded", result_kind="answer", availability="not_retained"
    )
    sealed = store.get_run(run.run_id)
    assert sealed.status == "succeeded"
    assert sealed.result_kind == "answer"
    assert sealed.result_availability == "not_retained"
    again, created = _submit(store)
    assert created is False
    assert again.status == "succeeded"  # 重试不重跑、不覆写
    with pytest.raises(RunConflict):
        store.finalize_run(
            run.run_id, status="failed", result_kind="error", availability="not_retained"
        )


def test_finalize_requires_queued_or_running_state(tmp_path: Path) -> None:
    from serving.control.store import RunConflict

    store = _store(tmp_path)
    _deployment(store)
    run, _ = _submit(store)
    with pytest.raises(RunConflict):
        store.finalize_run(run.run_id, status="queued", result_kind=None, availability="pending")


def test_recovery_interrupts_non_terminal_runs_and_spares_terminal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _deployment(store)
    queued, _ = _submit(store, client_request_id="a")
    running, _ = _submit(store, client_request_id="b")
    store.mark_running(running.run_id)
    done, _ = _submit(store, client_request_id="c")
    store.finalize_run(
        done.run_id, status="failed", result_kind="error", availability="not_retained"
    )
    assert store.mark_interrupted() == 2
    assert store.get_run(queued.run_id).status == "interrupted"
    assert store.get_run(running.run_id).status == "interrupted"
    assert store.get_run(done.run_id).status == "failed"
    # 恢复不重跑：中断后的重试仍返回原 run（新提交是新 run）
    assert store.mark_interrupted() == 0


def test_capture_grant_rejects_field_outside_whitelist() -> None:
    from serving.control.contracts import CaptureGrant

    with pytest.raises(ValueError):
        CaptureGrant(
            purpose="调试捕获",
            fields=frozenset({"question", "prompt"}),
            retain_until="2026-09-29T00:00:00+08:00",
        )
    with pytest.raises(ValueError):
        CaptureGrant(
            purpose="调试捕获", fields=frozenset(), retain_until="2026-09-29T00:00:00+08:00"
        )


def test_artifact_content_cannot_exceed_granted_fields(tmp_path: Path) -> None:
    from serving.control.contracts import CaptureGrant

    store = _store(tmp_path)
    _deployment(store)
    run, _ = _submit(store)
    grant = CaptureGrant(
        purpose="调试捕获",
        fields=frozenset({"question"}),
        retain_until="2026-09-29T00:00:00+08:00",
    )
    with pytest.raises(ValueError):
        store.create_artifact(
            run.run_id,
            grant=grant,
            content={"question": "总交易额", "result": {"rows": []}},
            now=datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
        )


def test_artifact_retention_cannot_exceed_capture_window(tmp_path: Path) -> None:
    from serving.control.contracts import CaptureGrant

    store = _store(tmp_path)
    _deployment(store)
    run, _ = _submit(store)
    grant = CaptureGrant(
        purpose="调试捕获",
        fields=frozenset({"question"}),
        retain_until="2026-10-22T00:00:00+08:00",  # now + 30 天，超出 7 天捕获保留期
    )
    with pytest.raises(ValueError):
        store.create_artifact(
            run.run_id,
            grant=grant,
            content={"question": "总交易额"},
            now=datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
        )


def test_expired_artifact_content_is_cleaned_but_tombstone_remains(tmp_path: Path) -> None:
    from serving.control.contracts import CaptureGrant

    store = _store(tmp_path)
    _deployment(store)
    run, _ = _submit(store)
    now = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    grant = CaptureGrant(
        purpose="调试捕获",
        fields=frozenset({"question"}),
        retain_until=(now + timedelta(hours=1)).isoformat(),
    )
    artifact = store.create_artifact(
        run.run_id, grant=grant, content={"question": "总交易额"}, now=now
    )
    assert artifact.content == {"question": "总交易额"}
    assert store.clean_expired_artifacts(now=now + timedelta(hours=2)) == 1
    cleaned = store.get_artifact(artifact.artifact_id)
    assert cleaned.content is None  # 正文删除，保留删除标记（D07）
    assert cleaned.cleaned_at is not None


def test_feedback_defaults_pending_review_and_list_is_owner_scoped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _deployment(store)
    run, _ = _submit(store)
    feedback = store.insert_feedback(run.run_id, owner=_owner(), verdict="down", comment="数字不对")
    assert feedback.status == "pending_review"
    assert feedback.training_eligible is False
    assert [f.feedback_id for f in store.list_feedback(owner=_owner())] == [feedback.feedback_id]
    assert store.list_feedback(owner=_owner("other")) == []
