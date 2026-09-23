"""Doris 连接器（ADR-0031 D03 首版唯一注册连接器）。

设计口径
--------
- 复用 PyMySQL（MySQL 协议）；**不把协议兼容当成数据源支持**：只接受
  `connector_kind == "doris"` 的 `SourceSpec`，构造时拒绝其他 kind。
- 共享实现 `execute_sql` 是仓库内唯一的 Doris 连接点：`eval.runner.execute_sql`
  与在线执行器都指向本函数（评测导入共享实现，见 T04 账本）。
- 每次调用新建连接并必关——与原 `eval.runner.execute_sql` 行为逐字一致，
  旧入口等价验证依赖这一点。
- 连接参数走环境变量（AGENTS.md 第 13 节）：DORIS_HOST/DORIS_PORT/DORIS_USER/
  DORIS_PASSWORD，本地开发默认 root 空密码。

边界（诚实声明）
----------------
- 受限探测（T07b）实现 `probe_source`：只跑固定目录查询（版本/权限/白名单
  表清单），`tls_policy` 在探测路径被消费（required=强制 TLS 且校验证书链；
  disabled=显式明文）；业务执行路径（`execute_sql`）仍不消费
  `tls_policy`/`query_budget`（消费接线归发布源合同任务）。
- 主机名身份校验不启用：探测目标是服务层校验过的 IP 字面量（防重绑定），
  身份由允许列表钉住；自定义 CA 配置面归后续任务（M1 用系统 CA）。
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from typing import Any

import pymysql

from agent.runtime.connectors.base import (
    ConnectorCapabilities,
    ConnectorError,
    ProbeAuthError,
    ProbeConnectionError,
    ProbeError,
    ProbeObservation,
    ProbeTarget,
    ProbeTlsError,
    SourceSpec,
)
from agent.runtime.context import Executor

KIND = "doris"


def execute_sql(sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
    """在 Doris 执行只读 SQL，返回 (rows, columns)。

    共享实现（原 `eval.runner.execute_sql` 逐字迁移，调用方零变化）：连接参数
    走环境变量，每次调用新建连接并在 finally 中关闭。

    Raises
    ------
    pymysql.MySQLError
        连接失败或 SQL 执行失败原样上抛（不吞、不重试）——由内核或评测决定
        如何记录该失败。
    """
    conn = pymysql.connect(
        host=os.environ.get("DORIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("DORIS_PORT", "9030")),
        user=os.environ.get("DORIS_USER", "root"),
        password=os.environ.get("DORIS_PASSWORD", ""),
        connect_timeout=15,
    )
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            columns = [desc[0] for desc in (cursor.description or [])]
            rows = [tuple(row) for row in cursor.fetchall()]
        finally:
            cursor.close()
    finally:
        conn.close()
    return rows, columns


class DorisConnector:
    """Doris 连接器：产出共享执行器；能力声明不许多报（N2）。"""

    def __init__(self, spec: SourceSpec) -> None:
        """校验 kind 后固定源合同。

        Raises
        ------
        ConnectorError
            `spec.connector_kind` 不是 `doris`——只注册 Doris，不开放 generic
            SQL URL（D03）。
        """
        if spec.connector_kind != KIND:
            raise ConnectorError(
                f"DorisConnector 只接受 connector_kind={KIND!r} 的源合同，收到 "
                f"{spec.connector_kind!r}——不把 MySQL 协议兼容当成数据源支持（D03）"
            )
        self._spec = spec

    @property
    def spec(self) -> SourceSpec:
        """接入源合同（构造时已校验）。"""
        return self._spec

    @property
    def capabilities(self) -> ConnectorCapabilities:
        """M0 诚实声明：只读执行已实现；其余能力未实现一律 False。"""
        return ConnectorCapabilities(
            dialect="doris",
            read_only=True,
            metadata_probe=False,
            cancel_query=False,
            snapshot_read=False,
            consistent_analysis=False,
        )

    def executor(self) -> Executor:
        """产出执行器：共享实现 `execute_sql`（同一函数对象，单一连接口径）。"""
        return execute_sql


# ---------- 受限元数据探测（T07b：源接入向导的固定目录查询） ----------

_IDENTIFIER_RE = re.compile(r"[0-9A-Za-z_-]+")
_AUTH_ERROR_CODES = frozenset({1044, 1045, 1142, 1227})
_TLS_ERROR_CODE = 2026


def probe_source(target: ProbeTarget, spec: SourceSpec) -> ProbeObservation:
    """受限元数据探测（D03）：只跑固定目录查询，不发任意 SQL、不做写入测试。

    查询集合固定（目录/库标识符先过白名单字符校验再反引号引用）：
    `SELECT VERSION()`、`SHOW GRANTS`、白名单内每个 catalog 的
    `SHOW DATABASES FROM `catalog``、白名单内每个 `catalog.db` 的
    `SHOW TABLES FROM `catalog`.`db``——不存在「测试连接」SQL 输入面。

    TLS：`target.tls` 为真时强制握手并校验证书链（系统 CA / SSL_CERT_FILE，
    主机名身份校验不启用——目标是校验过的 IP 字面量，身份由允许列表钉住）；
    为假时**显式**关闭 TLS（否则驱动默认 PREFERRED：先试 TLS 再静默回退）。

    Raises
    ------
    ProbeTlsError
        TLS 握手或证书校验失败（不降级明文）。
    ProbeAuthError
        数据侧拒绝账号或命令（凭据无效、授权撤销、命令被拒）。
    ProbeConnectionError
        目标不可达或固定查询失败；异常消息不含凭据/连接串。
    """
    connect_kwargs: dict[str, Any] = {
        "host": target.host,
        "port": target.port,
        "user": target.user,
        "password": target.password,
        "connect_timeout": 15,
        "read_timeout": 30,
        "write_timeout": 30,
    }
    if target.tls:
        connect_kwargs["ssl"] = {"check_hostname": False, "verify_mode": True}
    else:
        connect_kwargs["ssl_disabled"] = True
    try:
        conn = pymysql.connect(**connect_kwargs)
    except pymysql.err.MySQLError as exc:
        raise _probe_error(exc) from exc
    try:
        cursor = conn.cursor()
        try:
            rows = _fetch(cursor, "SELECT VERSION()")
            engine_version = str(rows[0][0]) if rows else ""
            grant_statements = _grant_statements(cursor)
            catalogs_seen: list[str] = []
            for catalog in sorted(spec.allowed_catalogs):
                _fetch(cursor, f"SHOW DATABASES FROM {_quoted(catalog)}")
                catalogs_seen.append(catalog)
            tables_seen: list[str] = []
            for catalog, database in sorted(_catalog_pairs(spec)):
                for row in _fetch(
                    cursor, f"SHOW TABLES FROM {_quoted(catalog)}.{_quoted(database)}"
                ):
                    tables_seen.append(f"{catalog}.{database}.{row[0]}")
        finally:
            cursor.close()
    except pymysql.err.MySQLError as exc:
        raise _probe_error(exc) from exc
    finally:
        conn.close()
    return ProbeObservation(
        engine_version=engine_version,
        grant_statements=grant_statements,
        catalogs_seen=tuple(catalogs_seen),
        tables_seen=tuple(tables_seen),
        queries_run=(
            "select_version",
            "show_grants",
            "catalog_databases",
            "catalog_tables",
        ),
    )


def _quoted(identifier: str) -> str:
    """白名单标识符 → 反引号引用；含不允许字符一律拒绝（不拼任意输入）。"""
    if not _IDENTIFIER_RE.fullmatch(identifier):
        raise ProbeConnectionError(f"标识符含不允许字符，拒绝拼接固定查询：{identifier!r}")
    return f"`{identifier}`"


def _fetch(cursor: Any, sql: str) -> list[tuple[Any, ...]]:
    cursor.execute(sql)
    return [tuple(row) for row in cursor.fetchall()]


def _catalog_pairs(spec: SourceSpec) -> set[tuple[str, str]]:
    """白名单表三段名 → (catalog, db) 去重集合（非三段名不参与表清单探测）。"""
    pairs: set[tuple[str, str]] = set()
    for table in spec.allowed_tables:
        parts = table.split(".")
        if len(parts) == 3:
            pairs.add((parts[0], parts[1]))
    return pairs


def _grant_statements(cursor: Any) -> tuple[str, ...]:
    """SHOW GRANTS 结果 → 权限声明条目（语句原文或表格形态规范式）。

    两种输出形态（真实观察，2026-09-22 Doris 4.1）：
    - 语句形态（MySQL / 旧版 Doris）：单列 `GRANT ... TO ...` 文本行；
    - 表格形态（Doris 4.x）：16 列（身份/Roles + 各 *Privs 列），单元格值为
      `<scope>: <Priv>; ...`。

    数据权限列（见 `_DATA_PRIVILEGE_COLUMNS`）决定「数据面只读」判定；资源/
    平台权限列不授予数据表读写，不参与判定。Roles 与未识别的 *Privs 列
    **不猜测语义**：条目原文保留，由服务层 fail-closed 拒绝确认只读
    （role 的权限不在此表格展开——不能因权限信息不可见而凭空声称 read_only）。
    无法解析的数据权限条目同样原文保留。不构造完整 SQL 解析。
    """
    cursor.execute("SHOW GRANTS")
    columns = [str(desc[0]) for desc in (cursor.description or [])]
    rows = [tuple(row) for row in cursor.fetchall()]
    if any(name.endswith("Privs") for name in columns):
        return _table_shape_statements(columns, rows)
    return _statement_shape_statements(rows)


# 数据权限列（Doris 4.x 表格形态）：其 *Privs 决定该账号对数据对象的能力。
_DATA_PRIVILEGE_COLUMNS = frozenset(
    {"GlobalPrivs", "CatalogPrivs", "DatabasePrivs", "TablePrivs", "ColPrivs"}
)

# 资源/平台权限列：不授予数据表读写（`normal: Usage_priv` 是普通 Doris 用户的
# 默认自带项）；不参与「数据面只读」判定。
_NON_DATA_PRIVILEGE_COLUMNS = frozenset(
    {
        "ResourcePrivs",
        "CloudClusterPrivs",
        "CloudStagePrivs",
        "StorageVaultPrivs",
        "WorkloadGroupPrivs",
        "ComputeGroupPrivs",
    }
)


def _table_shape_statements(
    columns: Sequence[str], rows: Sequence[tuple[object, ...]]
) -> tuple[str, ...]:
    """Doris 4.x 表格形态：数据权限列规范化；Roles/未知 *Privs 列原文保留。"""
    statements: list[str] = []
    for row in rows:
        for index, name in enumerate(columns):
            cell = row[index] if index < len(row) else None
            if name in _DATA_PRIVILEGE_COLUMNS:
                statements.extend(_normalized_privilege_cell(cell))
            elif name in _NON_DATA_PRIVILEGE_COLUMNS:
                continue
            elif name.endswith("Privs") or name == "Roles":
                statements.extend(_raw_cell_entry(cell))
    return tuple(statements)


def _statement_shape_statements(rows: Sequence[tuple[object, ...]]) -> tuple[str, ...]:
    """语句形态：`GRANT ` 开头的文本行原样提取（MySQL / 旧版 Doris）。"""
    statements: list[str] = []
    for row in rows:
        for cell in row:
            if not isinstance(cell, str):
                continue
            for line in cell.splitlines():
                stripped = line.strip()
                if stripped.upper().startswith("GRANT "):
                    statements.append(stripped)
    return tuple(statements)


def _normalized_privilege_cell(cell: object) -> list[str]:
    """`<scope>: <Priv>; ...` 单元格 → 规范式条目；无法解析时原文保留（fail-closed）。"""
    if not isinstance(cell, str) or not cell.strip():
        return []
    normalized: list[str] = []
    for chunk in cell.split(";"):
        entry = chunk.strip()
        if not entry:
            continue
        scope, sep, privilege = entry.rpartition(":")
        scope = scope.strip()
        privilege = privilege.strip()
        if not sep or not scope or not privilege:
            normalized.append(entry)
            continue
        normalized.append(f"GRANT {privilege.upper()} ON {scope}")
    return normalized


def _raw_cell_entry(cell: object) -> list[str]:
    """不可解释的权限单元格（Roles / 未知 *Privs 列）：非空原文保留 → 服务层 fail-closed。"""
    if not isinstance(cell, str) or not cell.strip():
        return []
    return [cell.strip()]


def _probe_error(exc: pymysql.err.MySQLError) -> ProbeError:
    """驱动异常 → 探测错误族（不回显底层文本：避免携带主机/账号信息）。"""
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    if code in _AUTH_ERROR_CODES:
        return ProbeAuthError(f"数据源拒绝账号或命令（错误码 {code}）")
    if code == _TLS_ERROR_CODE or "ssl" in str(exc).lower():
        return ProbeTlsError(f"TLS 连接失败（错误码 {code}）")
    return ProbeConnectionError(f"连接或固定查询失败（错误码 {code}）")
