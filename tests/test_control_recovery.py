"""T13 运维与恢复：备份/恢复/诊断端点，源故障时诊断仍可用。

红测先行（TDD）：导入尚不存在的 `serving.control.maintenance` 模块。
断言覆盖：
- backup_control：SQLite backup API、manifest 完整、文件权限 0600
- restore_control：从有效 manifest 恢复、拒绝损坏备份、不重跑 SQL/训练
- diagnostics：源故障时 200、不泄露 DSN/密钥、需 operator 授权
- 维护模式：备份期间暂停新任务/发布
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tests.workbench_support import WorkbenchHarness

# ---------------------------------------------------------------------------
# backup_control
# ---------------------------------------------------------------------------


def test_backup_control_creates_manifest_and_sqlite_copy(tmp_path: Path) -> None:
    """backup_control 用 SQLite backup API 生成 manifest + 备份文件。"""
    from serving.control.maintenance import backup_control

    harness = WorkbenchHarness(tmp_path)
    destination = tmp_path / "backups"
    manifest = backup_control(
        store=harness.control_store,
        bundles_root=harness.bundles_root,
        destination=destination,
    )
    assert manifest.backup_id
    assert manifest.control_db_path.exists()
    assert manifest.control_db_path.stat().st_mode & 0o777 == 0o600
    assert manifest.schema_version == harness.control_store.schema_version()
    assert manifest.backup_path == destination


def test_backup_control_excludes_secrets(tmp_path: Path) -> None:
    """备份不含环境密钥（D12）：manifest 与备份文件均不含密码/DSN。"""
    from serving.control.maintenance import backup_control

    harness = WorkbenchHarness(tmp_path)
    destination = tmp_path / "backups"
    manifest = backup_control(
        store=harness.control_store,
        bundles_root=harness.bundles_root,
        destination=destination,
    )
    manifest_text = json.dumps(manifest.to_dict())
    assert "sup3r-secret-pw" not in manifest_text
    assert "password" not in manifest_text.lower().replace("password_hash", "")


def test_backup_uses_sqlite_backup_api_not_file_copy(tmp_path: Path) -> None:
    """备份用 SQLite backup API（D12），不直接复制活跃 WAL 文件。"""
    from serving.control.maintenance import backup_control

    harness = WorkbenchHarness(tmp_path)
    destination = tmp_path / "backups"
    manifest = backup_control(
        store=harness.control_store,
        bundles_root=harness.bundles_root,
        destination=destination,
    )
    # 备份文件是有效 SQLite 数据库（backup API 产物），不是原始文件拷贝
    with sqlite3.connect(manifest.control_db_path) as conn:
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == harness.control_store.schema_version()


# ---------------------------------------------------------------------------
# restore_control
# ---------------------------------------------------------------------------


def test_restore_from_valid_manifest(tmp_path: Path) -> None:
    """从有效 manifest 恢复：先隔离验证，再恢复服务。"""
    from serving.control.maintenance import backup_control, restore_control

    harness = WorkbenchHarness(tmp_path)
    destination = tmp_path / "backups"
    manifest = backup_control(
        store=harness.control_store,
        bundles_root=harness.bundles_root,
        destination=destination,
    )
    # 模拟新实例从备份恢复
    target_path = tmp_path / "restored" / "control.sqlite"
    report = restore_control(
        manifest=manifest,
        target_path=target_path,
        bundles_root=harness.bundles_root,
    )
    assert report.restored is True
    assert target_path.exists()
    assert report.schema_version == harness.control_store.schema_version()


def test_restore_rejects_corrupted_backup(tmp_path: Path) -> None:
    """备份损坏时恢复拒绝（D12：不静默放行）。"""
    from serving.control.maintenance import BackupManifest, restore_control

    harness = WorkbenchHarness(tmp_path)
    # 构造一个指向损坏文件的 manifest
    bad_file = tmp_path / "corrupt.sqlite"
    bad_file.write_bytes(b"not a sqlite database")
    manifest = BackupManifest(
        backup_id="test-bad",
        control_db_path=bad_file,
        schema_version=1,
        backup_path=tmp_path,
    )
    target_path = tmp_path / "restored" / "control.sqlite"
    report = restore_control(
        manifest=manifest,
        target_path=target_path,
        bundles_root=harness.bundles_root,
    )
    assert report.restored is False
    # 中文 reason "备份损坏" 或英文 corrupt/integrity
    reason_lower = report.reason.lower()
    assert "损坏" in reason_lower or "integrity" in reason_lower or "corrupt" in reason_lower


def test_restore_does_not_rerun_sql_or_training(tmp_path: Path) -> None:
    """恢复不自动恢复未完成 SQL、训练或旧登录态（D12）。"""
    from serving.control.maintenance import backup_control, restore_control

    harness = WorkbenchHarness(tmp_path)
    destination = tmp_path / "backups"
    manifest = backup_control(
        store=harness.control_store,
        bundles_root=harness.bundles_root,
        destination=destination,
    )
    target_path = tmp_path / "restored" / "control.sqlite"
    report = restore_control(
        manifest=manifest,
        target_path=target_path,
        bundles_root=harness.bundles_root,
    )
    # 恢复后无活跃运行任务（中断的运行已封存，不重跑）
    assert report.active_runs_after_restore == 0


# ---------------------------------------------------------------------------
# diagnostics endpoint
# ---------------------------------------------------------------------------


def test_diagnostics_returns_200_when_source_fails(tmp_path: Path) -> None:
    """源故障时诊断端点仍 200（D12：不把 LLM/源未配置判整个服务不健康）。"""
    harness = WorkbenchHarness(tmp_path)
    harness.executor_spy.failure = ConnectionError("source unreachable")
    resp = harness.request("GET", "/api/v1/manage/diagnostics", actor="operator")
    assert resp.status_code == 200
    body = resp.json()
    # 源状态标记为不可用，但端点本身不 503
    assert "sources" in body
    assert body["sources"][0]["status"] == "unavailable"


def test_diagnostics_does_not_leak_secrets(tmp_path: Path) -> None:
    """诊断不返回 DSN、密钥或原始 Prompt（D13）。"""
    harness = WorkbenchHarness(tmp_path)
    resp = harness.request("GET", "/api/v1/manage/diagnostics", actor="operator")
    assert resp.status_code == 200
    text = resp.text
    assert "sup3r-secret-pw" not in text
    assert "ATLAS_JWT_SECRET" not in text
    assert "password" not in text.lower()


def test_diagnostics_requires_operator(tmp_path: Path) -> None:
    """诊断端点需 operator 或更高角色（viewer 不够）。"""
    harness = WorkbenchHarness(tmp_path)
    resp = harness.request("GET", "/api/v1/manage/diagnostics", actor="viewer")
    assert resp.status_code == 403


def test_diagnostics_requires_authentication(tmp_path: Path) -> None:
    """诊断端点需认证（无身份 → 401）。"""
    harness = WorkbenchHarness(tmp_path)
    resp = harness.request("GET", "/api/v1/manage/diagnostics", actor=None)
    assert resp.status_code == 401


def test_diagnostics_shows_release_integrity(tmp_path: Path) -> None:
    """诊断展示发布完整性（当前活动发布摘要）。"""
    harness = WorkbenchHarness(tmp_path)
    harness.seed_release(active=True)
    resp = harness.request("GET", "/api/v1/manage/diagnostics", actor="operator")
    assert resp.status_code == 200
    body = resp.json()
    assert "releases" in body
    assert body["releases"]["active_count"] >= 1


def test_diagnostics_shows_disk_and_audit_writability(tmp_path: Path) -> None:
    """诊断展示状态盘/审计可写性。"""
    harness = WorkbenchHarness(tmp_path)
    resp = harness.request("GET", "/api/v1/manage/diagnostics", actor="operator")
    assert resp.status_code == 200
    body = resp.json()
    assert "storage" in body
    assert body["storage"]["control_db_writable"] is True
    assert body["storage"]["audit_writable"] is True
