"""单进程私有控制库原语；调用方负责授权与证据验证，不提供业务 SQL 工具。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter

from agent.runtime.bundle import ReleaseManifest, RuntimeBundle
from serving.control.contracts import (
    TERMINAL_RUN_STATUSES,
    ArtifactRecord,
    CaptureGrant,
    ClientRequestId,
    DeploymentRecord,
    Digest,
    Draft,
    DraftKind,
    DraftReview,
    DraftStatus,
    DraftValidation,
    EventType,
    FeedbackRecord,
    FeedbackVerdict,
    ObjectId,
    Owner,
    ProbeBlockedReason,
    ProbeStatus,
    ReleaseRecord,
    ResultAvailability,
    ResultKind,
    ReviewDecision,
    RunEventRecord,
    RunRecord,
    SessionSummary,
    SourceConnectorKind,
    SourceProbeSummary,
    SourceRevisionRecord,
    TlsPolicy,
    ValidationFinding,
    ValidationStatus,
    canonical_json,
    content_digest,
    validate_retain_until,
)

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_NEXT_STATUS = {
    "draft": "validated",
    "validated": "reviewed",
    "reviewed": "source_imported",
    "source_imported": "release_ready",
    "release_ready": "published",
    "published": "retired",
}

_EVENT_TYPES: frozenset[str] = frozenset(
    {
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
    }
)
# 与合同 Literal 单一来源：取值集合从类型参数派生，不手抄第二份词表。
_AVAILABILITIES: frozenset[str] = frozenset(get_args(ResultAvailability))
_RESULT_KINDS: frozenset[str] = frozenset(get_args(ResultKind))
# 事件 payload 是脱敏摘要通道，不携带问句/SQL/结果行正文（D07）。
_MAX_EVENT_PAYLOAD_BYTES = 64 * 1024


class MigrationError(RuntimeError):
    """控制库版本或迁移序列不兼容。"""


class RevisionConflict(RuntimeError):
    """草稿修订或生命周期已变化。"""


class ReleaseConflict(RuntimeError):
    """活动发布冲突或制品不属于部署作用域。"""


class RunConflict(RuntimeError):
    """运行幂等键冲突、非法状态迁移或重复终结（HTTP 层投影为 409）。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _private_file(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("私有状态路径不得是符号链接")
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("私有状态文件权限必须为 0600")


class ControlStore:
    """SQLite 控制状态；每次操作独占事务连接，不持有可供 Agent 使用的执行器。"""

    def __init__(self, path: Path) -> None:
        """绑定本机私有文件；不合规路径抛 ValueError，文件系统异常向上传播。"""
        self.path = path.absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink() or self.path.parent.stat().st_mode & 0o077:
            raise ValueError("控制库目录必须是权限 0700 的普通目录")
        _private_file(self.path)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        _private_file(self.path)
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _version(conn: sqlite3.Connection) -> int:
        found = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone()
        if found is None:
            return 0
        rows = conn.execute("SELECT version FROM schema_version WHERE singleton = 1").fetchall()
        if len(rows) != 1:
            raise MigrationError("schema_version 损坏")
        return int(rows[0][0])

    def schema_version(self) -> int:
        """读取已提交迁移版本；缺版本表返回 0，损坏表抛 MigrationError。"""
        with self._connection() as conn:
            return self._version(conn)

    def migrate(self, *, migrations_dir: Path = MIGRATIONS_DIR) -> None:
        """事务应用服务端编号 SQL；升级前使用 backup API，失败回滚并保留备份。

        migrations_dir 是部署代码路径，不接受 HTTP 上传或发布制品中的 SQL。
        版本不兼容抛 MigrationError，SQL/备份异常向上传播。

        并发安全：executescript 会隐式 COMMIT，因此版本检查与迁移不在同一 SQLite 事务。
        采用「检查-执行-验证」模式：执行前重新检查版本，若已被其他进程迁移则跳过；
        迁移后验证版本符合预期。多进程同时迁移时，SQLite 的 BEGIN IMMEDIATE 保证
        只有一个进程能成功创建表，其他进程会失败并重试，发现版本已更新后正常返回。
        """
        scripts = sorted(p for p in migrations_dir.iterdir() if re.fullmatch(r"\d{3}\.sql", p.name))
        if not scripts or any(p.is_symlink() for p in scripts):
            raise MigrationError("迁移目录为空或含符号链接")
        latest = int(scripts[-1].stem)
        with self._connection() as conn:
            current = self._version(conn)
            if current > latest:
                raise MigrationError("控制库版本高于当前代码")
            pending = [p for p in scripts if int(p.stem) > current]
            if not pending:
                return
            if [int(p.stem) for p in pending] != list(range(current + 1, latest + 1)):
                raise MigrationError("迁移编号不连续")
            if current:
                backup_path = self.path.with_name(
                    f"{self.path.name}.pre-{current}-{uuid4().hex}.bak"
                )
                _private_file(backup_path)
                backup = sqlite3.connect(backup_path)
                try:
                    conn.backup(backup)
                finally:
                    backup.close()
            conn.execute("PRAGMA journal_mode = WAL")
            # executescript 会隐式 COMMIT，因此用显式事务包裹迁移 SQL
            # BEGIN IMMEDIATE 获取写锁：多进程同时迁移时，只有一个能成功
            migration_sql = "\n".join(p.read_text(encoding="utf-8") for p in pending)
            script = f"BEGIN IMMEDIATE;\n{migration_sql}\nUPDATE schema_version SET version = {latest} WHERE singleton = 1;\nCOMMIT;"
            try:
                conn.executescript(script)
            except sqlite3.OperationalError as exc:
                # 可能是并发迁移导致（如 table already exists）
                # 重新检查版本：若已被其他进程迁移到目标版本，则成功返回
                new_version = self._version(conn)
                if new_version >= latest:
                    return  # 其他进程已完成迁移
                # 否则是真正的错误，继续抛出
                raise

    def register_release(
        self,
        bundle: RuntimeBundle,
        *,
        owner: Owner,
        scope: str,
        source_id: str,
    ) -> None:
        """登记已校验制品，不激活；调用方验证 Git/审核/源能力，重复 ID 拒绝。"""
        scope = TypeAdapter(ObjectId).validate_python(scope)
        source_id = TypeAdapter(ObjectId).validate_python(source_id)
        manifest = bundle.manifest
        with self._connection(write=True) as conn:
            conn.execute(
                "INSERT INTO releases VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    manifest.release_id,
                    manifest.model_dump_json(),
                    owner.issuer,
                    owner.subject,
                    scope,
                    source_id,
                    _now(),
                ),
            )

    def create_deployment(
        self,
        deployment_id: str,
        *,
        owner: Owner,
        scope: str,
        source_id: str,
    ) -> None:
        """创建无活动发布的部署；非法 ID 拒绝，重复部署抛 IntegrityError。"""
        for value in (deployment_id, scope, source_id):
            TypeAdapter(ObjectId).validate_python(value)
        now = _now()
        with self._connection(write=True) as conn:
            conn.execute(
                "INSERT INTO deployments VALUES (?, ?, ?, ?, ?, NULL, 1, ?, ?)",
                (deployment_id, owner.issuer, owner.subject, scope, source_id, now, now),
            )

    def activate(self, deployment_id: str, release_id: str, expected: str | None) -> None:
        """原子 CAS 发布/回退指针；不改制品/业务库，冲突抛 ReleaseConflict。

        仅为受信控制服务原语；当前权限、兼容性、源能力与审核门禁由调用方验证。
        """
        try:
            with self._connection(write=True) as conn:
                changed = conn.execute(
                    "UPDATE deployments SET active_release_id = ?, revision = revision + 1, "
                    "updated_at = ? WHERE deployment_id = ? AND active_release_id IS ?",
                    (release_id, _now(), deployment_id, expected),
                ).rowcount
                if changed != 1:
                    raise ReleaseConflict("活动发布已变化或部署不存在")
        except sqlite3.IntegrityError as exc:
            raise ReleaseConflict("发布未注册或部署作用域不匹配") from exc

    def active_manifest(self, deployment_id: str) -> ReleaseManifest | None:
        """单次读取并固定活动 Manifest；未发布返回 None，不存在的部署抛 KeyError。"""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT r.manifest_json FROM deployments d LEFT JOIN releases r "
                "ON r.release_id = d.active_release_id WHERE d.deployment_id = ?",
                (deployment_id,),
            ).fetchone()
            if row is None:
                raise KeyError(deployment_id)
            return ReleaseManifest.model_validate_json(row[0]) if row[0] is not None else None

    def get_deployment(self, deployment_id: str) -> DeploymentRecord | None:
        """读取部署指针；不存在返回 None（可见性由服务层裁决）。"""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM deployments WHERE deployment_id = ?", (deployment_id,)
            ).fetchone()
        return None if row is None else self._deployment_from_row(row)

    def list_deployments(self) -> list[DeploymentRecord]:
        """全部部署（按 deployment_id 排序稳定）；无部署返回空表。"""
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM deployments ORDER BY deployment_id").fetchall()
        return [self._deployment_from_row(row) for row in rows]

    @staticmethod
    def _deployment_from_row(row: sqlite3.Row) -> DeploymentRecord:
        return DeploymentRecord(
            deployment_id=row["deployment_id"],
            scope=row["scope"],
            source_id=row["source_id"],
            active_release_id=row["active_release_id"],
            revision=int(row["revision"]),
            created_by=Owner(issuer=row["owner_issuer"], subject=row["owner_subject"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_release(self, release_id: str) -> ReleaseRecord | None:
        """读取发布登记行；不存在返回 None（可见性由服务层裁决）。"""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM releases WHERE release_id = ?", (release_id,)
            ).fetchone()
        return None if row is None else self._release_from_row(row)

    def list_releases(self) -> list[ReleaseRecord]:
        """全部发布登记（按登记时间 + ID 排序稳定）；无发布返回空表。"""
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM releases ORDER BY created_at, release_id").fetchall()
        return [self._release_from_row(row) for row in rows]

    @staticmethod
    def _release_from_row(row: sqlite3.Row) -> ReleaseRecord:
        """行 → 脱敏发布视图：内容身份与源修订取自 Manifest，不含模型正文。"""
        manifest = ReleaseManifest.model_validate_json(row["manifest_json"])
        return ReleaseRecord(
            release_id=row["release_id"],
            content_digest=manifest.content_digest,
            scope=row["scope"],
            source_id=row["source_id"],
            source_revision=manifest.source_revision,
            manifest=manifest.model_dump(mode="json"),
            created_by=Owner(issuer=row["owner_issuer"], subject=row["owner_subject"]),
            created_at=row["created_at"],
        )

    def append_source_revision(
        self,
        *,
        source_id: str,
        revision: str,
        connector_kind: SourceConnectorKind,
        secret_ref: str,
        allowed_catalogs: frozenset[str],
        allowed_tables: frozenset[str],
        timezone: str,
        tls_policy: TlsPolicy,
        query_budget: int,
        owner: Owner,
    ) -> SourceRevisionRecord:
        """追加源修订：version 在写事务内分配（MAX+1），旧版本不可变。

        调用方（控制服务）已验证能力、白名单自洽与引用形态；本原语只做
        source_id 形态校验与持久化（合同层拒绝的输入不会到达这里）。
        """
        source_id = TypeAdapter(ObjectId).validate_python(source_id)
        created_at = _now()
        with self._connection(write=True) as conn:
            found = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM source_revisions WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            version = int(found[0]) + 1
            conn.execute(
                "INSERT INTO source_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    version,
                    revision,
                    connector_kind,
                    secret_ref,
                    json.dumps(sorted(allowed_catalogs)),
                    json.dumps(sorted(allowed_tables)),
                    timezone,
                    tls_policy,
                    query_budget,
                    owner.issuer,
                    owner.subject,
                    created_at,
                ),
            )
        return SourceRevisionRecord(
            source_id=source_id,
            version=version,
            revision=revision,
            connector_kind=connector_kind,
            secret_ref=secret_ref,
            allowed_catalogs=allowed_catalogs,
            allowed_tables=allowed_tables,
            timezone=timezone,
            tls_policy=tls_policy,
            query_budget=query_budget,
            created_by=owner,
            created_at=created_at,
        )

    @staticmethod
    def _source_revision_from_row(row: sqlite3.Row) -> SourceRevisionRecord:
        return SourceRevisionRecord(
            source_id=row["source_id"],
            version=int(row["version"]),
            revision=row["revision"],
            connector_kind=row["connector_kind"],
            secret_ref=row["secret_ref"],
            allowed_catalogs=frozenset(json.loads(row["allowed_catalogs_json"])),
            allowed_tables=frozenset(json.loads(row["allowed_tables_json"])),
            timezone=row["timezone"],
            tls_policy=row["tls_policy"],
            query_budget=int(row["query_budget"]),
            created_by=Owner(
                issuer=row["created_by_issuer"], subject=row["created_by_subject"]
            ),
            created_at=row["created_at"],
        )

    def get_source_revision(self, source_id: str, version: int) -> SourceRevisionRecord:
        """读取指定源修订；不存在抛 KeyError（不泄露其他源的信息）。"""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM source_revisions WHERE source_id = ? AND version = ?",
                (source_id, version),
            ).fetchone()
        if row is None:
            raise KeyError((source_id, version))
        return self._source_revision_from_row(row)

    def latest_source_revision(self, source_id: str) -> SourceRevisionRecord | None:
        """该源最新修订；无任何修订返回 None。"""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM source_revisions WHERE source_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        return None if row is None else self._source_revision_from_row(row)

    def list_latest_source_revisions(self) -> list[SourceRevisionRecord]:
        """每个源的最新修订（按 source_id 排序稳定）；无源返回空表。"""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT r.* FROM source_revisions r JOIN ("
                " SELECT source_id, MAX(version) AS version FROM source_revisions"
                " GROUP BY source_id) m"
                " ON m.source_id = r.source_id AND m.version = r.version"
                " ORDER BY r.source_id"
            ).fetchall()
        return [self._source_revision_from_row(row) for row in rows]

    def insert_source_probe(
        self,
        *,
        probe_id: str,
        source_id: str,
        version: int,
        status: ProbeStatus,
        blocked_reason: ProbeBlockedReason | None,
        observed_at: str,
        engine_version: str | None,
        schema_digest: str | None,
        capabilities: dict[str, JsonValue],
        findings: dict[str, JsonValue],
        owner: Owner,
    ) -> None:
        """追加探测证据行（每次探测一行；旧证据不变，摘要读取取最新）。

        调用方（控制服务）已完成判定与脱敏；本原语只持久已判定的事实——
        findings 只允许脱敏事实（表清单、查询名），凭据/连接串永不入库。
        """
        probe_id = TypeAdapter(ObjectId).validate_python(probe_id)
        with self._connection(write=True) as conn:
            conn.execute(
                "INSERT INTO source_probes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    probe_id,
                    source_id,
                    version,
                    status,
                    blocked_reason,
                    observed_at,
                    engine_version,
                    schema_digest,
                    json.dumps(capabilities, sort_keys=True),
                    json.dumps(findings, sort_keys=True),
                    owner.issuer,
                    owner.subject,
                ),
            )

    def latest_source_probe(self, source_id: str, version: int) -> SourceProbeSummary | None:
        """该源修订的最近一次探测证据（按观察时间 + 插入序）；无证据返回 None。"""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT probe_id, status, blocked_reason, observed_at FROM source_probes"
                " WHERE source_id = ? AND version = ?"
                " ORDER BY observed_at DESC, rowid DESC LIMIT 1",
                (source_id, version),
            ).fetchone()
        if row is None:
            return None
        return SourceProbeSummary(
            probe_id=row["probe_id"],
            status=row["status"],
            blocked_reason=row["blocked_reason"],
            observed_at=row["observed_at"],
        )

    @staticmethod
    def _draft_from_row(row: sqlite3.Row) -> Draft:
        return Draft(
            draft_id=row["draft_id"],
            kind=row["kind"],
            owner=Owner(issuer=row["owner_issuer"], subject=row["owner_subject"]),
            scope=row["scope"],
            base_git_sha=row["base_git_sha"],
            revision=row["revision"],
            status=row["status"],
            content=json.loads(row["content_json"]),
            content_digest=row["content_digest"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _draft(conn: sqlite3.Connection, draft_id: str) -> Draft:
        row = conn.execute("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
        if row is None:
            raise KeyError(draft_id)
        return ControlStore._draft_from_row(row)

    def get_draft(self, draft_id: str) -> Draft:
        """返回独立草稿值对象；不存在抛 KeyError。调用方必须先做对象授权。"""
        with self._connection() as conn:
            return self._draft(conn, draft_id)

    def list_drafts(self) -> list[Draft]:
        """全部草稿（按 draft_id 排序稳定）；无草稿返回空表。调用方按域裁剪。"""
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM drafts ORDER BY draft_id").fetchall()
        return [self._draft_from_row(row) for row in rows]

    def create_draft(
        self,
        *,
        kind: DraftKind,
        owner: Owner,
        scope: str,
        base_git_sha: str,
        content: dict[str, JsonValue],
    ) -> Draft:
        """创建非权威草稿并返回 revision=1；非法合同/内容拒绝且不落库。"""
        now = _now()
        draft = Draft(
            draft_id=uuid4().hex,
            kind=kind,
            owner=owner,
            scope=scope,
            base_git_sha=base_git_sha,
            revision=1,
            status="draft",
            content=content,
            content_digest=content_digest(content),
            created_at=now,
            updated_at=now,
        )
        raw = canonical_json(draft.content)
        if len(raw.encode("utf-8")) > 1024 * 1024:
            raise ValueError("草稿超过 1 MiB 上限")
        with self._connection(write=True) as conn:
            conn.execute(
                "INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    draft.draft_id,
                    kind,
                    owner.issuer,
                    owner.subject,
                    scope,
                    base_git_sha,
                    1,
                    "draft",
                    raw,
                    draft.content_digest,
                    now,
                    now,
                ),
            )
        return draft

    def update_draft(
        self,
        draft_id: str,
        content: dict[str, JsonValue],
        *,
        expected: int,
    ) -> Draft:
        """CAS 修改草稿并使旧审核失效；冲突抛 RevisionConflict，历史证据保留。"""
        raw = canonical_json(content)
        if len(raw.encode("utf-8")) > 1024 * 1024:
            raise ValueError("草稿超过 1 MiB 上限")
        with self._connection(write=True) as conn:
            changed = conn.execute(
                "UPDATE drafts SET content_json = ?, content_digest = ?, revision = revision + 1, "
                "status = 'draft', updated_at = ? WHERE draft_id = ? AND revision = ?",
                (raw, content_digest(content), _now(), draft_id, expected),
            ).rowcount
            if changed != 1:
                raise RevisionConflict("草稿修订冲突")
            return self._draft(conn, draft_id)

    def advance_draft(
        self,
        draft_id: str,
        target: DraftStatus,
        *,
        expected: int,
        actor: Owner,
        evidence_id: str,
    ) -> Draft:
        """记录已由上层验证的显式动作；绑定当前摘要/修订，禁止跳级或空证据。

        本原语不执行 Schema/审核/发布门禁；这些由 T08 服务验证，不暴露为 HTTP 直通。
        状态或修订冲突抛 RevisionConflict，无证据抛 ValueError。
        """
        if not evidence_id.strip() or len(evidence_id) > 256:
            raise ValueError("动作必须关联有效证据 ID")
        with self._connection(write=True) as conn:
            draft = self._draft(conn, draft_id)
            if draft.revision != expected:
                raise RevisionConflict("草稿状态或修订冲突")
            self._advance_draft_in(
                conn, draft, target, actor=actor, evidence_id=evidence_id, now=_now()
            )
            return self._draft(conn, draft_id)

    def _advance_draft_in(
        self,
        conn: sqlite3.Connection,
        draft: Draft,
        target: DraftStatus,
        *,
        actor: Owner,
        evidence_id: str,
        now: str,
    ) -> None:
        """事务内追加动作证据并推进状态；调用方已完成门禁（本函数只挡跳级）。"""
        if _NEXT_STATUS.get(draft.status) != target:
            raise RevisionConflict("草稿状态不允许该动作")
        conn.execute(
            "INSERT INTO draft_actions (draft_id, revision, content_digest, status, "
            "actor_issuer, actor_subject, evidence_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                draft.draft_id,
                draft.revision,
                draft.content_digest,
                target,
                actor.issuer,
                actor.subject,
                evidence_id,
                now,
            ),
        )
        conn.execute(
            "UPDATE drafts SET status = ?, updated_at = ? WHERE draft_id = ?",
            (target, now, draft.draft_id),
        )

    def advance_draft_chain(
        self,
        draft_id: str,
        targets: Sequence[DraftStatus],
        *,
        expected: int,
        actor: Owner,
        evidence_id: str,
    ) -> Draft:
        """同一事务内按顺序推进多级状态（导入链：reviewed → release_ready）。

        每一步都追加动作证据（同一证据 ID 与时间戳），`_advance_draft_in` 只允许
        逐级推进；调用方（T08b 发布服务）已完成全部发布门禁。状态或修订冲突抛
        RevisionConflict；空链或无证据抛 ValueError。
        """
        if not evidence_id.strip() or len(evidence_id) > 256:
            raise ValueError("动作必须关联有效证据 ID")
        if not targets:
            raise ValueError("推进链必须至少包含一个目标状态")
        with self._connection(write=True) as conn:
            draft = self._draft(conn, draft_id)
            if draft.revision != expected:
                raise RevisionConflict("草稿状态或修订冲突")
            now = _now()
            for target in targets:
                self._advance_draft_in(
                    conn, draft, target, actor=actor, evidence_id=evidence_id, now=now
                )
                draft = draft.model_copy(update={"status": target})
            return self._draft(conn, draft_id)

    def record_validation(
        self,
        draft_id: str,
        *,
        expected: int,
        status: ValidationStatus,
        findings: list[ValidationFinding],
        actor: Owner,
        advance: bool,
    ) -> DraftValidation:
        """记录校验证据（绑定事务内读到的 revision/摘要，不采信调用方声称）。

        上层对指定 revision 跑完确定性校验后调用；事务内修订已变化抛
        RevisionConflict（证据不得绑定到未校验的内容）。`advance` 且草稿仍在
        draft 时，同事务推进 validated（重复校验只追加证据，不滞涨状态）。
        """
        now = _now()
        with self._connection(write=True) as conn:
            draft = self._draft(conn, draft_id)
            if draft.revision != expected:
                raise RevisionConflict("草稿修订已变化，校验证据作废")
            validation = DraftValidation(
                validation_id=uuid4().hex,
                draft_id=draft_id,
                revision=draft.revision,
                content_digest=draft.content_digest,
                status=status,
                findings=findings,
                actor=actor,
                created_at=now,
            )
            conn.execute(
                "INSERT INTO draft_validations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    validation.validation_id,
                    draft_id,
                    draft.revision,
                    draft.content_digest,
                    status,
                    canonical_json([finding.model_dump() for finding in findings]),
                    actor.issuer,
                    actor.subject,
                    now,
                ),
            )
            if advance and draft.status == "draft":
                self._advance_draft_in(
                    conn,
                    draft,
                    "validated",
                    actor=actor,
                    evidence_id=validation.validation_id,
                    now=now,
                )
            return validation

    def record_review(
        self,
        draft_id: str,
        *,
        decision: ReviewDecision,
        comment: str | None,
        actor: Owner,
    ) -> DraftReview:
        """记录人工审核证据；approved 同事务推进 reviewed，rejected 只落证据。

        状态非 validated 抛 RevisionConflict（HTTP 409）——审核必须紧跟同修订的
        通过校验；编辑使 revision+1 并回 draft，旧审核自然失效（不可复用）。
        """
        now = _now()
        with self._connection(write=True) as conn:
            draft = self._draft(conn, draft_id)
            if draft.status != "validated":
                raise RevisionConflict("草稿未处于 validated 状态：先通过校验再审核")
            review = DraftReview(
                review_id=uuid4().hex,
                draft_id=draft_id,
                revision=draft.revision,
                content_digest=draft.content_digest,
                decision=decision,
                comment=comment,
                actor=actor,
                created_at=now,
            )
            conn.execute(
                "INSERT INTO draft_reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review.review_id,
                    draft_id,
                    draft.revision,
                    draft.content_digest,
                    decision,
                    comment,
                    actor.issuer,
                    actor.subject,
                    now,
                ),
            )
            if decision == "approved":
                self._advance_draft_in(
                    conn,
                    draft,
                    "reviewed",
                    actor=actor,
                    evidence_id=review.review_id,
                    now=now,
                )
            return review

    # ---------- 运行、事件与反馈（ADR-0031 T05；D07③） ----------

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            owner=Owner(issuer=row["owner_issuer"], subject=row["owner_subject"]),
            deployment_id=row["deployment_id"],
            scope=row["scope"],
            mode=row["mode"],
            session_id=row["session_id"],
            client_request_id=row["client_request_id"],
            request_digest=row["request_digest"],
            release_id=row["release_id"],
            status=row["status"],
            result_kind=row["result_kind"],
            result_availability=row["result_availability"],
            replay_of=row["replay_of"],
            last_seq=row["last_seq"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _run(conn: sqlite3.Connection, run_id: str) -> RunRecord:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return ControlStore._run_from_row(row)

    def create_run(
        self,
        *,
        owner: Owner,
        deployment_id: str,
        mode: str,
        session_id: str,
        client_request_id: str,
        request_digest: str,
        replay_of: str | None = None,
    ) -> tuple[RunRecord, bool]:
        """幂等创建 queued 运行；返回（记录, 是否新建）。

        同（owner, 部署, client_request_id）重试：摘要一致返回原 run、不重跑；
        摘要不同抛 RunConflict（409 语义）。并发同键在 BEGIN IMMEDIATE 下串行化，
        UNIQUE 约束兼作终极防线。deployment 不存在抛 KeyError；字段形态错误抛
        pydantic ValidationError。
        """
        TypeAdapter(ObjectId).validate_python(deployment_id)
        TypeAdapter(ClientRequestId).validate_python(client_request_id)
        TypeAdapter(Digest).validate_python(request_digest)
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 128:
            raise ValueError("session_id 必须是 1–128 非空字符串")
        with self._connection(write=True) as conn:
            existing = conn.execute(
                "SELECT run_id, request_digest FROM runs WHERE owner_issuer = ? "
                "AND owner_subject = ? AND deployment_id = ? AND client_request_id = ?",
                (owner.issuer, owner.subject, deployment_id, client_request_id),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise RunConflict("client_request_id 已用于不同请求内容")
                return self._run(conn, existing["run_id"]), False
            deployment = conn.execute(
                "SELECT scope, active_release_id FROM deployments WHERE deployment_id = ?",
                (deployment_id,),
            ).fetchone()
            if deployment is None:
                raise KeyError(deployment_id)
            now = _now()
            run_id = uuid4().hex
            conn.execute(
                "INSERT INTO runs (run_id, owner_issuer, owner_subject, deployment_id, scope, "
                "mode, session_id, client_request_id, request_digest, release_id, status, "
                "result_kind, result_availability, replay_of, last_seq, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', NULL, 'pending', ?, 0, ?, ?)",
                (
                    run_id,
                    owner.issuer,
                    owner.subject,
                    deployment_id,
                    deployment["scope"],
                    mode,
                    session_id,
                    client_request_id,
                    request_digest,
                    deployment["active_release_id"],
                    replay_of,
                    now,
                    now,
                ),
            )
            return self._run(conn, run_id), True

    def get_run(self, run_id: str) -> RunRecord:
        """返回运行事实；不存在抛 KeyError（调用方先做对象授权，404 口径）。"""
        with self._connection() as conn:
            return self._run(conn, run_id)

    def list_runs(self, *, owner: Owner, limit: int = 50) -> list[RunRecord]:
        """本人运行（新→旧）；关键词/游标分页经 list_runs_filtered（T06）。"""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE owner_issuer = ? AND owner_subject = ? "
                "ORDER BY created_at DESC, run_id DESC LIMIT ?",
                (owner.issuer, owner.subject, limit),
            ).fetchall()
            return [self._run_from_row(row) for row in rows]

    def list_runs_filtered(
        self,
        *,
        owner: Owner | None,
        scopes: frozenset[str],
        scope: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        deployment_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        cursor: tuple[str, str] | None = None,
        limit: int = 50,
    ) -> tuple[list[RunRecord], bool]:
        """keyset 分页（created_at DESC, run_id DESC）；返回（行, 是否还有下一页）。

        owner=None 表示跨所有者（仅摘要能力调用方使用，能力检查在服务层）；
        scopes 为空集 fail closed 返回空页。cursor=(created_at, run_id) 为继续键，
        取 limit+1 行判「还有下一页」，不依赖 OFFSET（新写入不弄脏游标）。
        """
        if not scopes:
            return [], False
        where: list[str] = []
        params: list[object] = []
        if owner is not None:
            where.append("owner_issuer = ? AND owner_subject = ?")
            params.extend([owner.issuer, owner.subject])
        where.append("scope IN (" + ",".join("?" * len(scopes)) + ")")
        params.extend(sorted(scopes))
        if scope is not None:
            where.append("scope = ?")
            params.append(scope)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        if deployment_id is not None:
            where.append("deployment_id = ?")
            params.append(deployment_id)
        if since is not None:
            where.append("created_at >= ?")
            params.append(since)
        if until is not None:
            where.append("created_at <= ?")
            params.append(until)
        if cursor is not None:
            where.append("(created_at < ? OR (created_at = ? AND run_id < ?))")
            params.extend([cursor[0], cursor[0], cursor[1]])
        sql = (
            "SELECT * FROM runs WHERE "
            + " AND ".join(where)
            + " ORDER BY created_at DESC, run_id DESC LIMIT ?"
        )
        with self._connection() as conn:
            rows = conn.execute(sql, (*params, limit + 1)).fetchall()
        records = [self._run_from_row(row) for row in rows]
        return records[:limit], len(records) > limit

    def list_sessions(
        self,
        *,
        owner: Owner,
        scopes: frozenset[str],
        scope: str | None = None,
        cursor: tuple[str, str] | None = None,
        limit: int = 50,
    ) -> tuple[list[SessionSummary], bool]:
        """本人会话目录（按 (last_run_at, session_id) 逆序 keyset；含回合数与末状态）。

        只聚合控制库运行事实（D13：非 checkpoint dump）；scopes 为空集 fail closed。
        """
        if not scopes:
            return [], False
        where = [
            "r.owner_issuer = ? AND r.owner_subject = ?",
            "r.scope IN (" + ",".join("?" * len(scopes)) + ")",
        ]
        params: list[object] = [owner.issuer, owner.subject, *sorted(scopes)]
        if scope is not None:
            where.append("r.scope = ?")
            params.append(scope)
        having = ""
        if cursor is not None:
            having = "HAVING MAX(r.created_at) < ? OR (MAX(r.created_at) = ? AND r.session_id < ?)"
            params.extend([cursor[0], cursor[0], cursor[1]])
        sql = (
            "SELECT r.session_id, r.deployment_id, MAX(r.scope) AS scope, COUNT(*) AS run_count, "
            "MAX(r.created_at) AS last_run_at, "
            "(SELECT r2.status FROM runs r2 WHERE r2.owner_issuer = r.owner_issuer "
            "AND r2.owner_subject = r.owner_subject AND r2.session_id = r.session_id "
            "AND r2.deployment_id = r.deployment_id "
            "ORDER BY r2.created_at DESC, r2.run_id DESC LIMIT 1) AS last_status "
            "FROM runs r WHERE "
            + " AND ".join(where)
            + " GROUP BY r.session_id, r.deployment_id "
            + having
            + " ORDER BY last_run_at DESC, r.session_id DESC LIMIT ?"
        )
        with self._connection() as conn:
            rows = conn.execute(sql, (*params, limit + 1)).fetchall()
        records = [
            SessionSummary(
                session_id=row["session_id"],
                deployment_id=row["deployment_id"],
                scope=row["scope"],
                run_count=int(row["run_count"]),
                last_run_at=row["last_run_at"],
                last_status=row["last_status"],
            )
            for row in rows
        ]
        return records[:limit], len(records) > limit

    def deployment_scope(self, deployment_id: str) -> str:
        """部署的领域 scope；不存在抛 KeyError（HTTP 层投影 404）。

        授权需要 scope 作为资源维度（authorize 的 resource_scope），先取 scope
        再判能力——部署不存在与服务层不可见合流为同一 404，不泄露存在性。
        """
        with self._connection() as conn:
            row = conn.execute(
                "SELECT scope FROM deployments WHERE deployment_id = ?", (deployment_id,)
            ).fetchone()
            if row is None:
                raise KeyError(deployment_id)
            return str(row["scope"])

    def session_run_state(
        self, *, owner: Owner, deployment_id: str, session_id: str
    ) -> Literal["active", "terminal"] | None:
        """会话在本控制库的归约状态：None=无记录 / active=有在飞 run / terminal=全终态。

        供续问窗口检查（D07）：进程内存上下文过期后，控制库如实回答该会话是否
        已有运行事实——不猜、不静默丢弃上下文执行。
        """
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN status IN ('queued', 'running') "
                "THEN 1 ELSE 0 END) AS active FROM runs WHERE owner_issuer = ? "
                "AND owner_subject = ? AND deployment_id = ? AND session_id = ?",
                (owner.issuer, owner.subject, deployment_id, session_id),
            ).fetchone()
            if int(row["total"]) == 0:
                return None
            return "active" if int(row["active"] or 0) > 0 else "terminal"

    def mark_running(self, run_id: str) -> RunRecord:
        """queued → running（原子）；不存在抛 KeyError，非法迁移抛 RunConflict。"""
        with self._connection(write=True) as conn:
            changed = conn.execute(
                "UPDATE runs SET status = 'running', updated_at = ? "
                "WHERE run_id = ? AND status = 'queued'",
                (_now(), run_id),
            ).rowcount
            if changed != 1:
                self._run(conn, run_id)  # 不存在 → KeyError，与「非法迁移」区分
                raise RunConflict("运行不在 queued 状态")
            return self._run(conn, run_id)

    def finalize_run(
        self,
        run_id: str,
        *,
        status: str,
        result_kind: str | None,
        availability: str,
    ) -> RunRecord:
        """写唯一终态 + RUN_FINISHED 终态事件（同一事务）。

        只允许 queued/running → 终态；非终态参数、重复终结抛 RunConflict，不存在抛
        KeyError。终态事件 payload 只含状态与结果种类（脱敏；不内嵌问句/SQL/结果行）。
        """
        if status not in TERMINAL_RUN_STATUSES:
            raise RunConflict("终态必须是 succeeded/blocked/failed/interrupted")
        if availability not in _AVAILABILITIES:
            raise RunConflict(f"未知 result_availability：{availability!r}")
        if result_kind is not None and result_kind not in _RESULT_KINDS:
            raise RunConflict(f"未知 result_kind：{result_kind!r}")
        with self._connection(write=True) as conn:
            changed = conn.execute(
                "UPDATE runs SET status = ?, result_kind = ?, result_availability = ?, "
                "updated_at = ? WHERE run_id = ? AND status IN ('queued', 'running')",
                (status, result_kind, availability, _now(), run_id),
            ).rowcount
            if changed != 1:
                self._run(conn, run_id)  # 不存在 → KeyError
                raise RunConflict("运行已终结，不可重复写入终态")
            self._append_event(
                conn,
                run_id,
                "RUN_FINISHED",
                {"status": status, "result_kind": result_kind},
            )
            return self._run(conn, run_id)

    def mark_interrupted(self) -> int:
        """启动恢复（D07）：把 queued/running 全部封为 interrupted 并各写事件。

        不重跑 SQL、不自动恢复排队任务（旧登录态与授权可能已失效）；返回本次封存
        数，0 表示无残留（可重复调用）。
        """
        with self._connection(write=True) as conn:
            rows = conn.execute(
                "SELECT run_id FROM runs WHERE status IN ('queued', 'running') "
                "ORDER BY created_at, run_id"
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE runs SET status = 'interrupted', result_availability = "
                    "'not_retained', updated_at = ? WHERE run_id = ?",
                    (_now(), row["run_id"]),
                )
                self._append_event(
                    conn, row["run_id"], "RUN_INTERRUPTED", {"reason": "process_restart"}
                )
            return len(rows)

    @staticmethod
    def _append_event(
        conn: sqlite3.Connection,
        run_id: str,
        event_type: EventType,
        payload: dict[str, JsonValue],
        *,
        node_id: str | None = None,
        node_run_id: str | None = None,
        parent_node_run_id: str | None = None,
        attempt: int | None = None,
    ) -> RunEventRecord:
        """写事务内追加事件：seq 由 runs.last_seq 事务递增（唯一、无洞）。"""
        row = conn.execute(
            "SELECT last_seq, release_id FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        if len(canonical_json(payload).encode("utf-8")) > _MAX_EVENT_PAYLOAD_BYTES:
            raise ValueError("事件 payload 超过 64 KiB 上限（脱敏摘要，不是正文通道）")
        seq = int(row["last_seq"]) + 1
        event = RunEventRecord(
            run_id=run_id,
            seq=seq,
            event_id=uuid4().hex,
            occurred_at=_now(),
            node_id=node_id,
            node_run_id=node_run_id,
            parent_node_run_id=parent_node_run_id,
            attempt=attempt,
            event_type=event_type,
            release_id=row["release_id"],
            payload=payload,
        )
        conn.execute(
            "INSERT INTO run_events (run_id, seq, event_id, occurred_at, node_id, node_run_id, "
            "parent_node_run_id, attempt, event_type, release_id, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.run_id,
                event.seq,
                event.event_id,
                event.occurred_at,
                event.node_id,
                event.node_run_id,
                event.parent_node_run_id,
                event.attempt,
                event.event_type,
                event.release_id,
                canonical_json(event.payload),
            ),
        )
        conn.execute(
            "UPDATE runs SET last_seq = ?, updated_at = ? WHERE run_id = ?",
            (seq, _now(), run_id),
        )
        return event

    def append_event(
        self,
        run_id: str,
        *,
        event_type: EventType,
        payload: dict[str, JsonValue],
        node_id: str | None = None,
        node_run_id: str | None = None,
        parent_node_run_id: str | None = None,
        attempt: int | None = None,
    ) -> RunEventRecord:
        """追加真实事件（图/时间线共同事实源）；未知类型 ValueError，无此 run
        KeyError；时间由服务端时钟生成（不接收客户端时间）。"""
        if event_type not in _EVENT_TYPES:
            raise ValueError(f"未知事件类型：{event_type!r}")
        with self._connection(write=True) as conn:
            return self._append_event(
                conn,
                run_id,
                event_type,
                payload,
                node_id=node_id,
                node_run_id=node_run_id,
                parent_node_run_id=parent_node_run_id,
                attempt=attempt,
            )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> RunEventRecord:
        return RunEventRecord(
            schema_version=1,
            run_id=row["run_id"],
            seq=row["seq"],
            event_id=row["event_id"],
            occurred_at=row["occurred_at"],
            node_id=row["node_id"],
            node_run_id=row["node_run_id"],
            parent_node_run_id=row["parent_node_run_id"],
            attempt=row["attempt"],
            event_type=row["event_type"],
            release_id=row["release_id"],
            payload=json.loads(row["payload_json"]),
        )

    def list_events(self, run_id: str, *, after_seq: int = 0) -> list[RunEventRecord]:
        """按 seq 升序返回事件（SSE 续读基线）；不存在的 run 抛 KeyError。"""
        with self._connection() as conn:
            if conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                raise KeyError(run_id)
            rows = conn.execute(
                "SELECT * FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
            return [self._event_from_row(row) for row in rows]

    def first_event_seq(self, run_id: str) -> int | None:
        """现存事件的最小 seq；无事件（含已过保留期清理）返回 None。

        SSE 续读的洞检测（T06b）：客户端游标 after_seq 与最小现存 seq 之间存在
        缺口时无法补齐，由服务层以 410 拒绝（不外抛 KeyError——调用方已验对象）。
        """
        with self._connection() as conn:
            row = conn.execute(
                "SELECT MIN(seq) AS first FROM run_events WHERE run_id = ?", (run_id,)
            ).fetchone()
            return int(row["first"]) if row["first"] is not None else None

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=row["artifact_id"],
            run_id=row["run_id"],
            kind=row["kind"],
            purpose=row["purpose"],
            fields=frozenset(json.loads(row["fields_json"])),
            content=json.loads(row["content_json"]) if row["content_json"] is not None else None,
            content_digest=row["content_digest"],
            retain_until=row["retain_until"],
            cleaned_at=row["cleaned_at"],
            created_at=row["created_at"],
        )

    def create_artifact(
        self,
        run_id: str,
        *,
        grant: CaptureGrant,
        content: dict[str, JsonValue],
        now: datetime | None = None,
    ) -> ArtifactRecord:
        """显式授权捕获（D13）：content 键必须是 grant.fields 子集，期限不超上限。

        正文只在显式授权时落库；越权字段/超期抛 ValueError，run 不存在抛 KeyError。
        """
        validate_retain_until(grant.retain_until, now=now or datetime.now(UTC))
        extra = set(content) - set(grant.fields)
        if extra:
            raise ValueError(f"捕获内容超出授权字段：{sorted(extra)}")
        with self._connection(write=True) as conn:
            if conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                raise KeyError(run_id)
            artifact_id = uuid4().hex
            digest = content_digest(content)
            created_at = _now()
            conn.execute(
                "INSERT INTO artifacts (artifact_id, run_id, kind, purpose, fields_json, "
                "content_json, content_digest, retain_until, cleaned_at, created_at) "
                "VALUES (?, ?, 'capture', ?, ?, ?, ?, ?, NULL, ?)",
                (
                    artifact_id,
                    run_id,
                    grant.purpose,
                    canonical_json(sorted(grant.fields)),
                    canonical_json(content),
                    digest,
                    grant.retain_until,
                    created_at,
                ),
            )
            return ArtifactRecord(
                artifact_id=artifact_id,
                run_id=run_id,
                kind="capture",
                purpose=grant.purpose,
                fields=grant.fields,
                content=dict(content),
                content_digest=digest,
                retain_until=grant.retain_until,
                created_at=created_at,
            )

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        """读取捕获制品（含清理墓碑）；不存在抛 KeyError；对象授权由调用方完成。"""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise KeyError(artifact_id)
            return self._artifact_from_row(row)

    def clean_expired_artifacts(self, *, now: datetime | None = None) -> int:
        """删除已过保留期的捕获正文，保留删除标记（cleaned_at）；返回清理数。

        正文被清理的运行如实投影 expired（D07「曾捕获但已清理」）——摘要仍在，
        正文不因 checkpoint 或内存窗口被找回；只改终态行，不动在飞运行。
        """
        cutoff = (now or datetime.now(UTC)).isoformat()
        with self._connection(write=True) as conn:
            expired_run_ids = [
                str(row["run_id"])
                for row in conn.execute(
                    "SELECT DISTINCT run_id FROM artifacts WHERE content_json IS NOT NULL "
                    "AND datetime(retain_until) <= datetime(?)",
                    (cutoff,),
                ).fetchall()
            ]
            changed = conn.execute(
                "UPDATE artifacts SET content_json = NULL, content_digest = NULL, "
                "cleaned_at = ? WHERE content_json IS NOT NULL "
                "AND datetime(retain_until) <= datetime(?)",
                (_now(), cutoff),
            ).rowcount
            if expired_run_ids:
                placeholders = ",".join("?" * len(expired_run_ids))
                conn.execute(
                    "UPDATE runs SET result_availability = 'expired', updated_at = ? "
                    "WHERE run_id IN (" + placeholders + ") "
                    "AND status IN ('succeeded', 'blocked', 'failed', 'interrupted')",
                    (_now(), *expired_run_ids),
                )
            return changed

    def insert_feedback(
        self,
        run_id: str,
        *,
        owner: Owner,
        verdict: FeedbackVerdict,
        comment: str | None = None,
        correction: dict[str, JsonValue] | None = None,
        attribution_node: str | None = None,
    ) -> FeedbackRecord:
        """采集最小反馈；固定 pending_review / training_eligible=False（T12 审核前不开放训练）。

        run 归属与内容 ACL 由调用方校验（越权关联拒绝在服务层）；run 不存在抛 KeyError。
        `attribution_node` 是可选的节点级归因（T12）。
        """
        with self._connection(write=True) as conn:
            if conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                raise KeyError(run_id)
            now = _now()
            feedback_id = uuid4().hex
            conn.execute(
                "INSERT INTO feedback (feedback_id, run_id, owner_issuer, owner_subject, "
                "verdict, comment, correction_json, status, training_eligible, created_at, "
                "attribution_node) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_review', 0, ?, ?)",
                (
                    feedback_id,
                    run_id,
                    owner.issuer,
                    owner.subject,
                    verdict,
                    comment,
                    canonical_json(correction) if correction is not None else None,
                    now,
                    attribution_node,
                ),
            )
            return FeedbackRecord(
                feedback_id=feedback_id,
                run_id=run_id,
                owner=owner,
                verdict=verdict,
                comment=comment,
                correction=correction,
                status="pending_review",
                training_eligible=False,
                created_at=now,
                attribution_node=attribution_node,
            )

    def get_feedback(self, feedback_id: str) -> FeedbackRecord:
        """按 feedback_id 取单条反馈记录；不存在抛 KeyError（T12 审核/归因查询）。"""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM feedback WHERE feedback_id = ?", (feedback_id,)
            ).fetchone()
            if row is None:
                raise KeyError(feedback_id)
            return self._feedback_from_row(row)

    def review_feedback(
        self,
        feedback_id: str,
        *,
        decision: Literal["approved", "rejected"],
        reviewer: Owner,
    ) -> FeedbackRecord:
        """审核反馈：approved → training_eligible=True；rejected → 保持 False。

        已审核的反馈不可重复审核（抛 ValueError）；审核动作写入 feedback_reviews 审计轨迹。
        """
        with self._connection(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM feedback WHERE feedback_id = ?", (feedback_id,)
            ).fetchone()
            if row is None:
                raise KeyError(feedback_id)
            if row["status"] != "pending_review":
                raise ValueError(f"反馈已审核（{row['status']}），不可重复审核")
            now = _now()
            training_eligible = 1 if decision == "approved" else 0
            conn.execute(
                "UPDATE feedback SET status = ?, training_eligible = ?, "
                "reviewed_by_issuer = ?, reviewed_by_subject = ?, reviewed_at = ? "
                "WHERE feedback_id = ?",
                (decision, training_eligible, reviewer.issuer, reviewer.subject, now, feedback_id),
            )
            # 审计轨迹（追加式）
            review_id = uuid4().hex
            conn.execute(
                "INSERT INTO feedback_reviews (review_id, feedback_id, decision, "
                "actor_issuer, actor_subject, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (review_id, feedback_id, decision, reviewer.issuer, reviewer.subject, now),
            )
            updated = conn.execute(
                "SELECT * FROM feedback WHERE feedback_id = ?", (feedback_id,)
            ).fetchone()
            return self._feedback_from_row(updated)

    def revoke_feedback(self, feedback_id: str, *, revoker: Owner) -> FeedbackRecord:
        """撤销审核：approved/rejected → pending_review，training_eligible 回退 False。

        审计轨迹保留（decision='revoked' 行）；原审核证据由 feedback_reviews 历史保留。
        """
        with self._connection(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM feedback WHERE feedback_id = ?", (feedback_id,)
            ).fetchone()
            if row is None:
                raise KeyError(feedback_id)
            if row["status"] == "pending_review":
                raise ValueError("反馈尚未审核，无需撤销")
            now = _now()
            conn.execute(
                "UPDATE feedback SET status = 'pending_review', training_eligible = 0, "
                "reviewed_by_issuer = NULL, reviewed_by_subject = NULL, reviewed_at = NULL "
                "WHERE feedback_id = ?",
                (feedback_id,),
            )
            review_id = uuid4().hex
            conn.execute(
                "INSERT INTO feedback_reviews (review_id, feedback_id, decision, "
                "actor_issuer, actor_subject, created_at) VALUES (?, ?, 'revoked', ?, ?, ?)",
                (review_id, feedback_id, revoker.issuer, revoker.subject, now),
            )
            updated = conn.execute(
                "SELECT * FROM feedback WHERE feedback_id = ?", (feedback_id,)
            ).fetchone()
            return self._feedback_from_row(updated)

    def list_pending_feedback(self) -> list[FeedbackRecord]:
        """审核队列：返回所有 pending_review 反馈（新→旧；T12 跨用户审核队列）。"""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE status = 'pending_review' "
                "ORDER BY created_at DESC, feedback_id DESC"
            ).fetchall()
            return [self._feedback_from_row(row) for row in rows]

    def list_feedback(self, *, owner: Owner) -> list[FeedbackRecord]:
        """本人反馈列表（新→旧）；审核队列（跨用户）由 list_pending_feedback 暴露。"""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE owner_issuer = ? AND owner_subject = ? "
                "ORDER BY created_at DESC, feedback_id DESC",
                (owner.issuer, owner.subject),
            ).fetchall()
            return [self._feedback_from_row(row) for row in rows]

    @staticmethod
    def _feedback_from_row(row: sqlite3.Row) -> FeedbackRecord:
        """从 SQLite 行构造 FeedbackRecord（含 T12 新增字段）。"""
        reviewed_by = None
        if row["reviewed_by_issuer"] is not None and row["reviewed_by_subject"] is not None:
            reviewed_by = Owner(
                issuer=row["reviewed_by_issuer"], subject=row["reviewed_by_subject"]
            )
        return FeedbackRecord(
            feedback_id=row["feedback_id"],
            run_id=row["run_id"],
            owner=Owner(issuer=row["owner_issuer"], subject=row["owner_subject"]),
            verdict=row["verdict"],
            comment=row["comment"],
            correction=(
                json.loads(row["correction_json"]) if row["correction_json"] is not None else None
            ),
            status=row["status"],
            training_eligible=bool(row["training_eligible"]),
            created_at=row["created_at"],
            attribution_node=row["attribution_node"] if "attribution_node" in row.keys() else None,
            reviewed_by=reviewed_by,
            reviewed_at=row["reviewed_at"] if "reviewed_at" in row.keys() else None,
        )
