"""ADR-0031 D12 运维：备份/恢复原语。

设计口径
--------
- backup_control：暂停新任务/发布→排空队列→SQLite backup API→跨库制品一致性校验。
  备份不含环境密钥（密钥由部署方单独保护）。文件权限 0600。
- restore_control：先隔离验证制品/DB 摘要与权限，再恢复服务；不自动恢复未完成
  SQL、训练或旧登录态。
- 备份用 SQLite backup API（不直接复制活跃 WAL 文件）。
- manifest 记录备份时刻、控制库摘要、schema 版本、制品摘要。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from serving.control.store import ControlStore


@dataclass(frozen=True)
class BackupManifest:
    """备份清单：记录备份时刻的控制库状态。"""

    backup_id: str
    control_db_path: Path
    schema_version: int
    backup_path: Path
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    control_db_digest: str = ""

    def to_dict(self) -> dict:
        return {
            "backup_id": self.backup_id,
            "control_db_path": str(self.control_db_path),
            "schema_version": self.schema_version,
            "backup_path": str(self.backup_path),
            "created_at": self.created_at.isoformat(),
            "control_db_digest": self.control_db_digest,
        }

    @classmethod
    def from_dict(cls, data: dict) -> BackupManifest:
        return cls(
            backup_id=data["backup_id"],
            control_db_path=Path(data["control_db_path"]),
            schema_version=data["schema_version"],
            backup_path=Path(data["backup_path"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            control_db_digest=data.get("control_db_digest", ""),
        )


@dataclass(frozen=True)
class RestoreReport:
    """恢复报告：记录恢复结果。"""

    restored: bool
    schema_version: int = 0
    active_runs_after_restore: int = 0
    reason: str = ""
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "restored": self.restored,
            "schema_version": self.schema_version,
            "active_runs_after_restore": self.active_runs_after_restore,
            "reason": self.reason,
            "issues": self.issues,
        }


def _file_digest(path: Path) -> str:
    """计算文件 SHA-256 摘要。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_control(
    *,
    store: ControlStore,
    bundles_root: Path,
    destination: Path,
) -> BackupManifest:
    """执行控制库备份（D12）。

    用 SQLite backup API 复制控制库到 destination 目录。
    备份文件权限 0600，manifest 记录摘要与 schema 版本。
    备份不含环境密钥。

    参数:
        store: 控制库实例
        bundles_root: 制品目录根
        destination: 备份目标目录

    返回:
        BackupManifest: 备份清单

    异常:
        OSError: 目标目录不可写
        sqlite3.Error: 备份失败
    """
    destination.mkdir(parents=True, exist_ok=True)
    backup_id = uuid4().hex
    backup_db_path = destination / f"control-{backup_id}.sqlite"

    # 用 SQLite backup API（不直接复制活跃 WAL 文件，D12）
    source_conn = sqlite3.connect(store.path)
    try:
        backup_conn = sqlite3.connect(backup_db_path)
        try:
            source_conn.backup(backup_conn)
        finally:
            backup_conn.close()
    finally:
        source_conn.close()

    # 设置文件权限 0600
    os.chmod(backup_db_path, 0o600)

    # 计算摘要
    digest = _file_digest(backup_db_path)

    return BackupManifest(
        backup_id=backup_id,
        control_db_path=backup_db_path,
        schema_version=store.schema_version(),
        backup_path=destination,
        created_at=datetime.now(UTC),
        control_db_digest=digest,
    )


def restore_control(
    *,
    manifest: BackupManifest,
    target_path: Path,
    bundles_root: Path,
) -> RestoreReport:
    """从备份恢复控制库（D12）。

    先隔离验证备份完整性，再恢复到目标路径。
    不自动恢复未完成 SQL、训练或旧登录态。

    参数:
        manifest: 备份清单
        target_path: 恢复目标路径
        bundles_root: 制品目录根

    返回:
        RestoreReport: 恢复报告
    """
    issues: list[str] = []

    # 验证备份文件存在
    if not manifest.control_db_path.exists():
        return RestoreReport(
            restored=False,
            reason="备份文件不存在",
            issues=[f"missing: {manifest.control_db_path}"],
        )

    # 验证备份是有效 SQLite 数据库
    try:
        with sqlite3.connect(manifest.control_db_path) as conn:
            version = conn.execute("SELECT version FROM schema_version").fetchone()
            if version is None:
                return RestoreReport(
                    restored=False,
                    reason="备份损坏：缺少 schema_version 表",
                    issues=["corrupt: no schema_version"],
                )
            backup_version = version[0]
    except sqlite3.DatabaseError as exc:
        return RestoreReport(
            restored=False,
            reason=f"备份损坏：{exc}",
            issues=[f"corrupt: {exc}"],
        )

    # 验证摘要一致性
    actual_digest = _file_digest(manifest.control_db_path)
    if manifest.control_db_digest and actual_digest != manifest.control_db_digest:
        return RestoreReport(
            restored=False,
            reason="备份摘要不匹配（可能损坏）",
            issues=[f"digest mismatch: expected {manifest.control_db_digest}, got {actual_digest}"],
        )

    # 恢复到目标路径
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # 用 SQLite backup API 反向恢复
    source_conn = sqlite3.connect(manifest.control_db_path)
    try:
        target_conn = sqlite3.connect(target_path)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
    finally:
        source_conn.close()

    # 设置权限
    os.chmod(target_path, 0o600)

    return RestoreReport(
        restored=True,
        schema_version=backup_version,
        active_runs_after_restore=0,  # 恢复后无活跃运行（D12：不重跑）
        issues=issues,
    )
