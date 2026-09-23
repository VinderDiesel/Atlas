"""连接器合同（ADR-0031 D03）：源规格、能力声明与执行器装配接缝。

设计口径
--------
- **唯一持连接处**：数据库连接实现只存在于本包（首版 `doris.py`）；评测
  （`eval.runner`）与在线执行都导入同一份共享实现——评测不再反向成为在线
  执行工厂（T04 账本）。
- `SourceSpec` 是 D03 的连接器配置面：非秘密配置落受保护部署配置，秘密只由
  环境变量引用解析（`secret_ref` 必须是 `env:` 引用，N9）——把明文密码写进
  配置对象在构造层就失败，而不是靠评审自觉。
- `ConnectorCapabilities` 是**声明**，不是承诺：未实现的能力一律 False
  （N2 不得把设计写成已完成）；带时间戳的探测证据化归后续源接入任务。
- `Connector` 协议是执行器装配接缝：内核与工厂只经 `executor()` 取执行器，
  测试替身（spy）与真实 `DorisConnector` 走同一协议（不给内核 test-only 旁路）。

边界（诚实声明）
----------------
- 运行执行面只注册 Doris；`ConnectorCapabilities` 的
  `metadata_probe/cancel_query/snapshot_read/consistent_analysis` 均未实现
  （False，`test_runtime_execution` 冻结该声明）——受限元数据探测走**独立
  接缝**（T07b：`ProbeTarget`/`ProbeObservation`/`ProbeError`，实现见
  `connectors/doris.probe_source`），不经 `Connector` 协议。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from agent.runtime.context import Executor

_OFFSET_RE = re.compile(r"[+-]\d{2}:\d{2}")
_SECRET_REF_PREFIX = "env:"


class ConnectorError(ValueError):
    """连接器配置或能力不满足执行要求。"""


@dataclass(frozen=True)
class SourceSpec:
    """接入源合同（D03 字段逐字对应）。

    Attributes
    ----------
    source_id : 源标识（如 `doris-primary`）。
    revision : 源修订标识（接入方维护，用于身份回显与漂移比对）。
    connector_kind : 连接器种类；首版只接受 `doris`（由具体连接器构造时校验）。
    secret_ref : 秘密引用，必须是 `env:<NAME>` 形式（N9：秘密只由环境变量解析）。
    allowed_catalogs : 允许访问的目录（受限元数据探测与查询的边界）。
    allowed_tables : 允许访问的表（与发布源合同对齐，不借用评测快照）。
    timezone : 源时区，显式 UTC 偏移（如 `+08:00`，AGENTS §7.3）。
    tls_policy : TLS 策略声明（部署方接线时消费；M0 执行路径不读取）。
    query_budget : 源级查询预算（行数上限；消费接线归发布源合同任务）。

    Raises
    ------
    ValueError
        必填字符串为空、`secret_ref` 不是环境变量引用、`timezone` 不是显式
        UTC 偏移——一律拒绝构造，不静默填默认值。
    """

    source_id: str
    revision: str
    connector_kind: str
    secret_ref: str
    allowed_catalogs: frozenset[str]
    allowed_tables: frozenset[str]
    timezone: str
    tls_policy: str
    query_budget: int

    def __post_init__(self) -> None:
        for name in ("source_id", "revision", "connector_kind", "tls_policy"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"SourceSpec.{name} 必须是非空字符串，收到 {value!r}")
        if not isinstance(self.secret_ref, str) or not self.secret_ref.startswith(
            _SECRET_REF_PREFIX
        ):
            raise ValueError(
                f"SourceSpec.secret_ref 必须是环境变量引用（{_SECRET_REF_PREFIX}<NAME>）："
                f"秘密只由环境变量解析（AGENTS.md N9），收到 {self.secret_ref!r}"
            )
        if not isinstance(self.timezone, str) or not _OFFSET_RE.fullmatch(self.timezone):
            raise ValueError(
                f"SourceSpec.timezone 必须是显式时区（UTC 偏移，如 +08:00），"
                f"收到 {self.timezone!r}（AGENTS.md §7.3 要求显式声明时区）"
            )


@dataclass(frozen=True)
class ConnectorCapabilities:
    """连接器能力声明（D03 字段逐字对应）。

    未实现的能力一律 False（N2）；这里是静态声明——「探测结果是有时间戳的
    证据，不是永久保证」的证据化归后续源接入任务。
    """

    dialect: str
    read_only: bool
    metadata_probe: bool
    cancel_query: bool
    snapshot_read: bool
    consistent_analysis: bool


@runtime_checkable
class Connector(Protocol):
    """执行器装配接缝：内核/工厂只经 `executor()` 取执行器（D06）。

    测试替身与真实连接器实现同一协议——内核不知道执行器来自真库还是替身，
    因此「假执行器」永远从正式接缝进入，不给内核加 test-only 旁路。
    """

    @property
    def spec(self) -> SourceSpec:
        """接入源合同。"""
        ...

    @property
    def capabilities(self) -> ConnectorCapabilities:
        """能力声明（未实现 = False）。"""
        ...

    def executor(self) -> Executor:
        """产出执行器：只接受已过 Guard 的只读 SQL，返回 (rows, columns)。"""
        ...


# ---------- 受限探测合同（T07b：源接入向导的固定目录查询） ----------
#
# 探测是独立接缝而非 `Connector` 协议的一部分：目标**已由服务层校验**
# （硬禁网络 + 管理员允许列表），建连只得用校验过的 IP 字面量，且查询集合
# 固定——没有任意「测试 SQL」入口（D03）。


@dataclass(frozen=True)
class ProbeTarget:
    """受限探测的连接目标（服务层在校验通过后构造）。

    Attributes
    ----------
    host : 已校验的 IP 字面量（允许列表命中目标，不是原始域名——杜绝
        「校验用域名、连接再解析」的 DNS 重绑定窗口）。
    port : 端口（已在允许列表内）。
    user : 数据侧账号（由 `env:` 引用解析而来，非明文配置）。
    password : 数据侧口令（repr 隐藏：连接对象不把口令交给日志/断言输出）。
    tls : 是否强制 TLS 握手（tls_policy=required；建连实现负责握手与校验证书链）。
    """

    host: str
    port: int
    user: str
    password: str = field(repr=False)
    tls: bool = False


@dataclass(frozen=True)
class ProbeObservation:
    """受限探测观察结果（固定目录查询产物）：只含脱敏事实，不含凭据/连接串。

    Attributes
    ----------
    engine_version : 引擎版本（固定查询 SELECT VERSION() 的产物）。
    grant_statements : 当前账号权限声明原文（SHOW GRANTS 的 GRANT 行）。
    catalogs_seen : 白名单目录中实际可达的目录。
    tables_seen : 白名单表实际存在的 catalog.db.table 全名清单。
    queries_run : 执行过的固定查询名（证据审计；不含参数）。
    """

    engine_version: str
    grant_statements: tuple[str, ...]
    catalogs_seen: tuple[str, ...]
    tables_seen: tuple[str, ...]
    queries_run: tuple[str, ...]


class ProbeError(RuntimeError):
    """受限探测失败基类；服务层映射为 blocked_reason（不吞错、不重试）。"""


class ProbeConnectionError(ProbeError):
    """目标不可达或固定查询失败（异常消息不含凭据/连接串）。"""


class ProbeAuthError(ProbeError):
    """数据侧拒绝账号或命令（凭据无效、授权被撤销）。"""


class ProbeTlsError(ProbeError):
    """TLS 握手/证书校验失败（tls_policy=required 时不降级为明文）。"""
