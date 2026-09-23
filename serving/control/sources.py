"""T07 只读数据源接入服务（ADR-0031 D03/D13）。

设计口径
--------
- **追加式修订**：同 source 新建 revision 只追加新行（version 由控制库事务内
  分配）；旧版本不可变——既有发布绑定引用的修订保持可读（T08 发布链）。
- **秘密只由环境引用解析**：本层永不接收、永不回显明文密码或完整 DSN（D03）；
  `secret_ref` 只允许 `env:<NAME>` 形态（合同层已拒明文凭据与未知字段），
  环境变量值形态为 JSON `{"host","port","user","password"}`。
- **目标校验在连接之前**：解析 → 硬禁网络（云元数据/link-local/多播/未指定，
  即使误配进允许列表也拒）→ 允许列表命中，然后**用已校验的 IP 字面量建连**
  ——不给 DNS 重绑定留时间窗（D03）。
- **受限探测**：只执行固定目录查询（连接器实现），不提供任意「测试 SQL」；
  探测结果是带时间戳的**证据**（含能力声明），不是永久保证（N2/D03）。
- **能力门**：源接入是 operator 的 `source.manage`（D02 模板，不隐式继承）；
  缺失一律 ControlForbidden（403），被拒提交零持久化。

边界（诚实声明）
----------------
- 目标允许列表来自部署环境 `ATLAS_SOURCE_TARGET_ALLOWLIST`（`ip:port;ip:port`）；
  未配置 = 空列表 = 全体拒绝（fail-closed）；解析失败在装配期抛错（fail-fast）。
- TLS：`tls_policy=required` 要求握手成功并校验证书链（系统 CA / SSL_CERT_FILE）；
  主机名身份校验不启用——连接目标是已校验的 IP 字面量，身份由允许列表钉住。
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from pydantic import JsonValue

from agent.runtime.connectors.base import (
    ProbeAuthError,
    ProbeError,
    ProbeObservation,
    ProbeTarget,
    ProbeTlsError,
    SourceSpec,
)
from serving.control.auth import ControlForbidden, Principal
from serving.control.contracts import (
    Owner,
    ProbeBlockedReason,
    ProbeCapabilities,
    ProbeResult,
    SourceProbeSummary,
    SourceRevisionRecord,
    SourceRevisionRequest,
    SourceRevisionView,
    content_digest,
)
from serving.control.store import ControlStore

SOURCE_MANAGE_CAPABILITY = "source.manage"
TARGET_ALLOWLIST_ENV = "ATLAS_SOURCE_TARGET_ALLOWLIST"

ProbeRunner = Callable[[ProbeTarget, SourceSpec], ProbeObservation]
Resolver = Callable[[str], Sequence[str]]

# 云元数据硬禁补充名单：169.254.169.254 已被 link-local 段覆盖，此处列出
# 不在 link-local 段内的元数据服务地址（如阿里云 100.100.100.200）。
_METADATA_ADDRESSES = frozenset({"100.100.100.200"})
# 只读权限白名单：权限声明出现其他动词（含写权限）一律不确认只读。
_READ_ONLY_PRIVILEGES = frozenset({"SELECT_PRIV", "SELECT"})


class SourceAllowlist:
    """管理员目标允许列表：只有显式列出的 `ip:port` 可被探测连接（D03）。

    条目只接受 IP 字面量——域名在服务层解析后按 IP 匹配（列表约束的是连接
    目标，不是 DNS 名称）。空列表是合法配置 = 全部拒绝（fail-closed）。
    """

    def __init__(self, entries: frozenset[tuple[str, int]]) -> None:
        self._entries = entries

    @classmethod
    def parse(cls, raw: str) -> SourceAllowlist:
        """解析 `ip:port;ip:port` 配置文本；非法条目抛 ValueError（fail-fast）。

        解析期即校验 IP 字面量形态与端口范围——要求管理员钉住地址，不把
        「解析结果」当授权（域名条目一律拒绝）。
        """
        entries: set[tuple[str, int]] = set()
        for chunk in raw.split(";"):
            entry = chunk.strip()
            if not entry:
                continue
            address, _, port_text = entry.rpartition(":")
            if not address or not port_text.isdigit():
                raise ValueError(f"目标允许列表条目必须是 ip:port：{entry!r}")
            port = int(port_text)
            if not 1 <= port <= 65535:
                raise ValueError(f"目标允许列表端口越界：{entry!r}")
            try:
                normalized = str(ipaddress.ip_address(address))
            except ValueError as exc:
                raise ValueError(f"目标允许列表条目必须是 IP 字面量：{entry!r}") from exc
            entries.add((normalized, port))
        return cls(frozenset(entries))

    def allows(self, host: str, port: int) -> bool:
        """该 IP 字面量与端口是否被允许（仅精确匹配）。"""
        return (host, port) in self._entries


def _default_resolver(host: str) -> Sequence[str]:
    """系统 DNS 解析（测试注入替身；只在服务层校验阶段使用，建连不重解析）。"""
    infos = socket.getaddrinfo(host, None)
    return sorted({str(info[4][0]) for info in infos})


def _forbidden_address(address: str) -> bool:
    """硬禁目标判定（D03）：云元数据、link-local、多播、未指定一律拒绝。

    私网与环回**不**在此拒绝——它们只能靠允许列表显式放行（D03）。
    """
    parsed = ipaddress.ip_address(address)
    candidates = [parsed]
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        candidates.append(mapped)
    for candidate in candidates:
        if (
            candidate.is_link_local
            or candidate.is_multicast
            or candidate.is_unspecified
            or str(candidate) in _METADATA_ADDRESSES
        ):
            return True
    return False


def _grants_are_read_only(statements: Sequence[str]) -> bool:
    """权限声明只含只读动词才算确认只读；空声明/写权限/未知动词不确认。

    只看 GRANT 语句的权限列表（`GRANT <priv>[, <priv>] ON ...`）——不构造
    完整 SQL 解析，未知形态拒绝（fail-closed：宁可无法确认，不凭空声称只读）。
    """
    if not statements:
        return False
    for statement in statements:
        tokens = statement.upper().split()
        if len(tokens) < 3 or tokens[0] != "GRANT" or "ON" not in tokens:
            return False
        privileges = [token.rstrip(",") for token in tokens[1 : tokens.index("ON")]]
        if not privileges or any(
            privilege not in _READ_ONLY_PRIVILEGES for privilege in privileges
        ):
            return False
    return True


class _ProbeBlocked(Exception):
    """内部短路信号：目标校验未通过（只携带 blocked_reason，不携带被拒输入）。"""

    def __init__(self, reason: ProbeBlockedReason) -> None:
        super().__init__(reason)
        self.reason = reason


class SourceService:
    """源接入服务：能力门 + 合同转换 + 追加式持久化 + 受限探测。"""

    def __init__(
        self,
        store: ControlStore,
        *,
        probe_runner: ProbeRunner | None = None,
        environ: Mapping[str, str] | None = None,
        allowlist: SourceAllowlist | str | None = None,
        resolver: Resolver | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """绑定控制库与探测接缝；调用方（serving/api.py 或测试装配）惰性装配。

        `probe_runner` 为 None 时用生产实现（Doris 固定目录查询，探测时才导入
        `agent.runtime.connectors.doris.probe_source`）；`allowlist` 为 None 时
        读取 `ATLAS_SOURCE_TARGET_ALLOWLIST`（未配置 = 空列表 = 全拒）。
        """
        self._store = store
        self._probe_runner = probe_runner
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        if allowlist is None:
            allowlist = os.environ.get(TARGET_ALLOWLIST_ENV, "")
        self._allowlist = (
            SourceAllowlist.parse(allowlist) if isinstance(allowlist, str) else allowlist
        )
        self._resolver: Resolver = _default_resolver if resolver is None else resolver
        self._clock: Callable[[], datetime] = (
            (lambda: datetime.now(UTC)) if clock is None else clock
        )

    def create_revision(
        self, principal: Principal, request: SourceRevisionRequest
    ) -> SourceRevisionView:
        """新建源修订（追加式，版本自动递增）；返回固定 13 键视图。

        Raises
        ------
        ControlForbidden
            principal 缺 `source.manage` 能力（D02 fail-closed）。
        """
        self._require(principal)
        record = self._store.append_source_revision(
            source_id=request.source_id,
            revision=request.revision,
            connector_kind=request.connector_kind,
            secret_ref=request.secret_ref,
            allowed_catalogs=frozenset(request.allowed_catalogs),
            allowed_tables=frozenset(request.allowed_tables),
            timezone=request.timezone,
            tls_policy=request.tls_policy,
            query_budget=request.query_budget,
            owner=Owner(issuer=principal.issuer, subject=principal.subject),
        )
        return self._view(record)

    def list_registry(self, principal: Principal) -> list[SourceRevisionView]:
        """每源最新修订视图（按 source_id 排序）；last_probe 为最近证据摘要。

        Raises
        ------
        ControlForbidden
            principal 缺 `source.manage` 能力。
        """
        self._require(principal)
        return [self._view(record) for record in self._store.list_latest_source_revisions()]

    def probe(self, principal: Principal, source_id: str) -> ProbeResult:
        """受限探测：读最新修订 → 判定链 → 固定目录查询 → 证据化（固定 10 键）。

        判定链每一步都先于任何外部连接：白名单自检（表越界）→ 凭据解析 →
        目标硬禁/允许列表 → 探测执行（TLS/授权/连接错误映射）→ 元数据完整性
        → 只读确认。阻塞同样落探测证据（失败理由 + 时间戳），能力全 False。

        Raises
        ------
        ControlForbidden
            principal 缺 `source.manage` 能力。
        KeyError
            该源无任何修订（路由层投影为 404，不泄露存在性）。
        """
        self._require(principal)
        record = self._store.latest_source_revision(source_id)
        if record is None:
            raise KeyError(source_id)
        owner = Owner(issuer=principal.issuer, subject=principal.subject)
        observed_at = self._clock().isoformat()

        outside = sorted(
            table
            for table in record.allowed_tables
            if table.split(".")[0] not in record.allowed_catalogs
        )
        if outside:
            # 存储层防御：合同层已拒越界，但直写存储的越界行在此拦截（不建连）
            return self._blocked(
                record,
                "table_out_of_whitelist",
                observed_at,
                owner,
                findings={"tables_outside": cast("JsonValue", outside)},
            )

        try:
            target = self._resolve_target(record)
        except _ProbeBlocked as blocked:
            return self._blocked(record, blocked.reason, observed_at, owner)

        try:
            observation = self._run_probe(target, self._spec(record))
        except ProbeTlsError:
            return self._blocked(record, "tls_error", observed_at, owner)
        except ProbeAuthError:
            return self._blocked(record, "credential_rejected", observed_at, owner)
        except (ProbeError, OSError):
            return self._blocked(record, "connect_failed", observed_at, owner)

        seen_tables = set(observation.tables_seen)
        seen_catalogs = set(observation.catalogs_seen)
        missing_tables = sorted(t for t in record.allowed_tables if t not in seen_tables)
        missing_catalogs = sorted(c for c in record.allowed_catalogs if c not in seen_catalogs)
        if missing_tables or missing_catalogs:
            return self._blocked(
                record,
                "metadata_missing",
                observed_at,
                owner,
                findings={
                    "missing_tables": cast("JsonValue", missing_tables),
                    "missing_catalogs": cast("JsonValue", missing_catalogs),
                },
            )

        if not _grants_are_read_only(observation.grant_statements):
            return self._blocked(record, "read_only_unconfirmed", observed_at, owner)

        probe_id = uuid4().hex
        capabilities = ProbeCapabilities(
            dialect=record.connector_kind,
            read_only=True,
            metadata_probe=True,
            cancel_query=False,
            snapshot_read=False,
            consistent_analysis=False,
        )
        schema_digest = content_digest(sorted(observation.tables_seen))
        findings: dict[str, JsonValue] = {
            "tables_seen": cast("JsonValue", sorted(observation.tables_seen)),
            "queries_run": cast("JsonValue", list(observation.queries_run)),
        }
        self._store.insert_source_probe(
            probe_id=probe_id,
            source_id=record.source_id,
            version=record.version,
            status="ok",
            blocked_reason=None,
            observed_at=observed_at,
            engine_version=observation.engine_version,
            schema_digest=schema_digest,
            capabilities=capabilities.model_dump(),
            findings=findings,
            owner=owner,
        )
        return ProbeResult(
            probe_id=probe_id,
            source_id=record.source_id,
            version=record.version,
            status="ok",
            blocked_reason=None,
            observed_at=observed_at,
            engine_version=observation.engine_version,
            schema_digest=schema_digest,
            capabilities=capabilities,
        )

    def _run_probe(self, target: ProbeTarget, spec: SourceSpec) -> ProbeObservation:
        """调用探测接缝；未注入替身时用生产实现（探测时才导入连接器）。"""
        runner = self._probe_runner
        if runner is None:
            from agent.runtime.connectors.doris import probe_source

            runner = probe_source
        return runner(target, spec)

    def _resolve_target(self, record: SourceRevisionRecord) -> ProbeTarget:
        """凭据 + 目标校验 → 建连目标；未通过抛 `_ProbeBlocked`（不建连）。

        只用**已校验的 IP 字面量**构造 ProbeTarget：域名在连接前被解析并校验，
        连接不再触发解析（D03 防重绑定）。
        """
        credential = self._resolve_credential(record.secret_ref)
        if credential is None:
            raise _ProbeBlocked("credential_missing")
        host, port, user, password = credential
        addresses, reason = self._validated_addresses(host)
        if reason is not None:
            raise _ProbeBlocked(reason)
        if any(_forbidden_address(address) for address in addresses):
            raise _ProbeBlocked("target_forbidden")
        if any(not self._allowlist.allows(address, port) for address in addresses):
            raise _ProbeBlocked("target_not_allowlisted")
        return ProbeTarget(
            host=addresses[0],
            port=port,
            user=user,
            password=password,
            tls=record.tls_policy == "required",
        )

    def _resolve_credential(self, secret_ref: str) -> tuple[str, int, str, str] | None:
        """解析 `env:<NAME>` → (host, port, user, password)；不可用返回 None。

        环境变量值形态为 JSON 对象（host/port/user/password）；缺失、非 JSON、
        形态不合法一律视为凭据不可用（credential_missing，不猜测、不补默认）。
        """
        raw = self._environ.get(secret_ref.removeprefix("env:"))
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        host = payload.get("host")
        port = payload.get("port")
        user = payload.get("user")
        password = payload.get("password")
        if (
            not isinstance(host, str)
            or not host
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
            or not isinstance(user, str)
            or not user
            or not isinstance(password, str)
        ):
            return None
        return host, port, user, password

    def _validated_addresses(
        self, host: str
    ) -> tuple[list[str], ProbeBlockedReason | None]:
        """目标 → 可校验的 IP 字面量列表（字面量直通；域名走注入解析器）。"""
        try:
            return [str(ipaddress.ip_address(host))], None
        except ValueError:
            pass
        try:
            resolved = self._resolver(host)
        except (OSError, ProbeError):
            return [], "connect_failed"
        addresses: list[str] = []
        for item in resolved:
            try:
                addresses.append(str(ipaddress.ip_address(item)))
            except ValueError:
                return [], "target_not_allowlisted"
        if not addresses:
            return [], "target_not_allowlisted"
        return addresses, None

    def _blocked(
        self,
        record: SourceRevisionRecord,
        reason: ProbeBlockedReason,
        observed_at: str,
        owner: Owner,
        *,
        findings: dict[str, JsonValue] | None = None,
    ) -> ProbeResult:
        """证据化阻塞：落一行探测证据（失败理由 + 时间戳），能力全 False（N2）。"""
        probe_id = uuid4().hex
        capabilities = ProbeCapabilities(
            dialect=record.connector_kind,
            read_only=False,
            metadata_probe=False,
            cancel_query=False,
            snapshot_read=False,
            consistent_analysis=False,
        )
        self._store.insert_source_probe(
            probe_id=probe_id,
            source_id=record.source_id,
            version=record.version,
            status="blocked",
            blocked_reason=reason,
            observed_at=observed_at,
            engine_version=None,
            schema_digest=None,
            capabilities=capabilities.model_dump(),
            findings=findings or {},
            owner=owner,
        )
        return ProbeResult(
            probe_id=probe_id,
            source_id=record.source_id,
            version=record.version,
            status="blocked",
            blocked_reason=reason,
            observed_at=observed_at,
            engine_version=None,
            schema_digest=None,
            capabilities=capabilities,
        )

    @staticmethod
    def _spec(record: SourceRevisionRecord) -> SourceSpec:
        """修订行 → SourceSpec（不含明文秘密：secret_ref 仍是 env 引用）。"""
        return SourceSpec(
            source_id=record.source_id,
            revision=record.revision,
            connector_kind=record.connector_kind,
            secret_ref=record.secret_ref,
            allowed_catalogs=record.allowed_catalogs,
            allowed_tables=record.allowed_tables,
            timezone=record.timezone,
            tls_policy=record.tls_policy,
            query_budget=record.query_budget,
        )

    @staticmethod
    def _require(principal: Principal) -> None:
        if SOURCE_MANAGE_CAPABILITY not in principal.capabilities:
            raise ControlForbidden(f"控制能力不足：{SOURCE_MANAGE_CAPABILITY!r}（D02）")

    def _view(self, record: SourceRevisionRecord) -> SourceRevisionView:
        return SourceRevisionView(
            source_id=record.source_id,
            version=record.version,
            revision=record.revision,
            connector_kind=record.connector_kind,
            secret_ref=record.secret_ref,
            allowed_catalogs=sorted(record.allowed_catalogs),
            allowed_tables=sorted(record.allowed_tables),
            timezone=record.timezone,
            tls_policy=record.tls_policy,
            query_budget=record.query_budget,
            created_by=record.created_by,
            created_at=record.created_at,
            last_probe=self._last_probe(record),
        )

    def _last_probe(self, record: SourceRevisionRecord) -> SourceProbeSummary | None:
        """该修订最近一次探测证据摘要（按版本取——新修订未探测前不借旧证据）。"""
        return self._store.latest_source_probe(record.source_id, record.version)
