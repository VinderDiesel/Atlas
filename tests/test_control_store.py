"""T02 控制库：真实 SQLite 迁移、事务与草稿修订，不连接业务源。"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest


def _latest_migration_version() -> int:
    """真实迁移目录的最大编号（断言与实现同口径，新增迁移不硬编码版本）。"""
    from serving.control.store import MIGRATIONS_DIR

    return max(int(path.stem) for path in MIGRATIONS_DIR.glob("*.sql"))


def test_migrations_survive_reopen_and_reject_future_schema(tmp_path: Path) -> None:
    from serving.control.store import ControlStore, MigrationError

    path = tmp_path / "state" / "control.sqlite"
    store = ControlStore(path)
    store.migrate()
    store.migrate()
    assert ControlStore(path).schema_version() == _latest_migration_version()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        conn.execute("UPDATE schema_version SET version = 999")
    with pytest.raises(MigrationError):
        ControlStore(path).migrate()


def test_failed_migration_rolls_back_and_keeps_backup(tmp_path: Path) -> None:
    from serving.control.store import ControlStore

    path = tmp_path / "state" / "control.sqlite"
    store = ControlStore(path)
    store.migrate()
    base = _latest_migration_version()
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / f"{base + 1:03d}.sql").write_text(
        "CREATE TABLE incomplete (id TEXT);\nINSERT INTO missing_table VALUES (1);"
    )
    with pytest.raises(sqlite3.DatabaseError):
        store.migrate(migrations_dir=migrations)
    assert ControlStore(path).schema_version() == base
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name = 'incomplete'").fetchall()
            == []
        )
    backups = list(path.parent.glob("control.sqlite.pre-*.bak"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("SELECT version FROM schema_version").fetchone()[0] == base


def test_linked_control_database_is_rejected(tmp_path: Path) -> None:
    from serving.control.store import ControlStore

    target = tmp_path / "other.sqlite"
    target.write_bytes(b"unrelated")
    (tmp_path / "control.sqlite").symlink_to(target)
    with pytest.raises(ValueError):
        ControlStore(tmp_path / "control.sqlite")
    assert target.read_bytes() == b"unrelated"


def test_draft_edit_invalidates_review_and_rejects_stale_revision(tmp_path: Path) -> None:
    from serving.control.contracts import Owner
    from serving.control.store import ControlStore, RevisionConflict

    store = ControlStore(tmp_path / "state" / "control.sqlite")
    store.migrate()
    owner = Owner(issuer="https://issuer.example", subject="editor")
    draft = store.create_draft(
        kind="node_config",
        owner=owner,
        scope="finance",
        base_git_sha="a" * 40,
        content={"mode": "rules"},
    )
    assert draft.revision == 1
    assert draft.status == "draft"
    with pytest.raises(RevisionConflict):
        store.advance_draft(draft.draft_id, "reviewed", expected=1, actor=owner, evidence_id="r1")
    store.advance_draft(draft.draft_id, "validated", expected=1, actor=owner, evidence_id="v1")
    store.advance_draft(draft.draft_id, "reviewed", expected=1, actor=owner, evidence_id="r1")
    edited = store.update_draft(draft.draft_id, {"mode": "template"}, expected=1)
    assert edited.revision == 2
    assert edited.status == "draft"
    assert edited.content_digest != draft.content_digest
    with pytest.raises(RevisionConflict):
        store.update_draft(draft.draft_id, {"mode": "stale"}, expected=1)
    with pytest.raises(RevisionConflict):
        store.advance_draft(draft.draft_id, "reviewed", expected=1, actor=owner, evidence_id="r2")
    reopened = ControlStore(store.path).get_draft(draft.draft_id)
    assert reopened.content == {"mode": "template"}
    assert reopened.revision == 2
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT revision, status, evidence_id FROM draft_actions ORDER BY action_id"
        ).fetchall() == [(1, "validated", "v1"), (1, "reviewed", "r1")]


def test_draft_actions_require_evidence_and_cannot_skip_states(tmp_path: Path) -> None:
    from serving.control.contracts import Owner
    from serving.control.store import ControlStore, RevisionConflict

    store = ControlStore(tmp_path / "state" / "control.sqlite")
    store.migrate()
    owner = Owner(issuer="local", subject="editor")
    draft = store.create_draft(
        kind="semantic",
        owner=owner,
        scope="finance",
        base_git_sha="b" * 40,
        content={"semantic": "draft only"},
    )
    with pytest.raises(ValueError):
        store.advance_draft(draft.draft_id, "validated", expected=1, actor=owner, evidence_id="")
    with pytest.raises(RevisionConflict):
        store.advance_draft(draft.draft_id, "published", expected=1, actor=owner, evidence_id="p1")
    assert store.get_draft(draft.draft_id).status == "draft"


def test_release_compare_and_swap_is_atomic_and_reopens(tmp_path: Path) -> None:
    from agent.runtime.bundle import load_bundle
    from serving.control.contracts import Owner
    from serving.control.store import ControlStore, ReleaseConflict
    from tests.workbench_support import bundle_files, write_bundle

    store = ControlStore(tmp_path / "state" / "control.sqlite")
    store.migrate()
    owner = Owner(issuer="local", subject="publisher")
    root = tmp_path / "bundles"
    ids = [write_bundle(root, bundle_files(), revision=str(i)) for i in (1, 2)]
    for release_id in ids:
        store.register_release(
            load_bundle(root, release_id), owner=owner, scope="finance", source_id="doris"
        )
    store.create_deployment("finance", owner=owner, scope="finance", source_id="doris")
    store.activate("finance", ids[0], expected=None)
    pinned = store.active_manifest("finance")
    barrier = Barrier(2)

    def activate(release_id: str) -> str:
        barrier.wait()
        try:
            store.activate("finance", release_id, expected=ids[0])
            return "activated"
        except ReleaseConflict:
            return "conflict"

    # 两次尝试相同候选也须以旧指针为 CAS，不能当作无条件幂等写。
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(activate, [ids[1], ids[1]]))
    assert sorted(results) == ["activated", "conflict"]
    assert pinned.release_id == ids[0]
    assert ControlStore(store.path).active_manifest("finance").release_id == ids[1]
    with pytest.raises(ReleaseConflict):
        store.activate("finance", ids[0], expected=ids[0])
    store.activate("finance", ids[0], expected=ids[1])
    assert store.active_manifest("finance").release_id == ids[0]


def test_release_foreign_keys_and_immutable_rows(tmp_path: Path) -> None:
    from agent.runtime.bundle import load_bundle
    from serving.control.contracts import Owner
    from serving.control.store import ControlStore, ReleaseConflict
    from tests.workbench_support import bundle_files, write_bundle

    store = ControlStore(tmp_path / "state" / "control.sqlite")
    store.migrate()
    owner = Owner(issuer="local", subject="publisher")
    root = tmp_path / "bundles"
    release_id = write_bundle(root, bundle_files())
    store.register_release(
        load_bundle(root, release_id), owner=owner, scope="finance", source_id="doris"
    )
    store.create_deployment("retail", owner=owner, scope="retail", source_id="other")
    with pytest.raises(ReleaseConflict):
        store.activate("retail", release_id, expected=None)
    with pytest.raises(ReleaseConflict):
        store.activate("retail", "f" * 64, expected=None)
    assert store.active_manifest("retail") is None
    with sqlite3.connect(store.path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE releases SET manifest_json = '{}' WHERE release_id = ?",
                (release_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM releases WHERE release_id = ?", (release_id,))
    with pytest.raises(sqlite3.IntegrityError):
        store.create_deployment("retail", owner=owner, scope="retail", source_id="other")
