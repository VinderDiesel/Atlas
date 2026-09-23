"""数据身份联合类型（ADR-0031 D03）：snapshot | live 判别联合。

设计口径
--------
- `snapshot`：绑定 `data/snapshots/<sha>.meta.json` 的固定快照，可精确重放，
  评测只接受这一种（N6）。sha 只能来自 `data.identity.RuntimeSnapshot`（唯一
  来源），digest 由 meta 内容确定性计算（同内容同摘要，不引入第二事实源）。
- `live`：在线源观测身份（source_revision + schema_digest + observed_at），
  不可精确重放（`reproducible` 恒 False 且构造不出 True）。**结构性没有
  snapshot_sha 字段**——不能把在线源伪造成快照（D03：不得填写伪 snapshot_sha）。
- 门禁：`require_snapshot` 对 live / 未绑定一律 ValueError（评测、发布验证
  等只接受固定快照的入口调用）；`analysis_refusal` 返回四步分析的一致性
  拒绝码（live 源无一致性读 → `analysis_consistency_unavailable`，不得用
  四次变化中的读取做虚假贡献对账）。

边界（诚实声明）
----------------
- 本模块只表达身份与资格判断，不做连接探测（元数据探测是连接器职责，
  `agent/runtime/connectors/`）；`analysis_refusal` 在 T04 只提供判断，
  runs 面的真实接入归 T05/T06。
- 时间戳必须显式时区（AGENTS.md §7.3）：`verified_at` / `observed_at` 校验
  失败即拒绝，缺省由本模块生成 `+08:00` 时间——不产生裸时间戳。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from data.identity import RuntimeSnapshot

TZ = timezone(timedelta(hours=8))  # 契约要求：时间戳显式 +08:00

# 四步分析一致性拒绝码（D03 固定词表；不编造对账时的唯一合法出口）
ANALYSIS_CONSISTENCY_UNAVAILABLE = "analysis_consistency_unavailable"

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


def _now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _require_nonempty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串，收到 {value!r}")
    return value


def _require_explicit_timezone(value: str, field: str) -> str:
    """时间戳必须可解析且带显式时区——裸时间一律拒绝（AGENTS.md §7.3）。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空 ISO8601 字符串（显式时区），收到 {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} 不可解析：{value!r}（{exc}）") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} 未显式声明时区：{value!r}（AGENTS.md §7.3）")
    return value


def _require_digest(value: str, field: str) -> str:
    """64 位十六进制摘要（sha256 形态）——不校验会放过 'not-a-digest' 这类占位。"""
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{field} 必须是 64 位十六进制摘要（sha256 形态）：{value!r}")
    return value


def snapshot_digest(meta: dict[str, Any]) -> str:
    """快照 meta 的内容摘要（canonical JSON sha256，确定性）。

    同内容（含跨对象重载）→ 同摘要；内容任何键位变化 → 摘要变化。用于身份
    回显与漂移比对，不替代 `data/snapshot.py --check` 的行数级复核。
    """
    canonical = json.dumps(
        meta, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SnapshotDataIdentity:
    """固定快照身份：可精确重放（评测级）。

    字段与 D03 判别联合逐字对应；`reproducible` 是联合类型的公共接口（与
    `LiveDataIdentity` 的 False 对应），不是可构造字段。
    """

    mode: Literal["snapshot"]
    snapshot_sha: str
    snapshot_digest: str
    verified_at: str

    def __post_init__(self) -> None:
        _require_nonempty(self.snapshot_sha, "snapshot_sha")
        _require_digest(self.snapshot_digest, "snapshot_digest")
        _require_explicit_timezone(self.verified_at, "verified_at")

    @property
    def reproducible(self) -> bool:
        """固定快照可精确重放（与 live 的 False 对应）。"""
        return True


@dataclass(frozen=True)
class LiveDataIdentity:
    """在线源身份：不可精确重放（D03：不能填写伪 snapshot_sha）。

    结构性没有 `snapshot_sha` 字段——伪造尝试在属性访问层就失败，而不是靠
    调用方自觉。`source_version` 允许 None（源不报告版本时如实说不知道）。
    """

    mode: Literal["live"]
    source_id: str
    source_revision: str
    schema_digest: str
    observed_at: str
    source_version: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.source_id, "source_id")
        _require_nonempty(self.source_revision, "source_revision")
        _require_digest(self.schema_digest, "schema_digest")
        _require_explicit_timezone(self.observed_at, "observed_at")
        if self.source_version is not None:
            _require_nonempty(self.source_version, "source_version")

    @property
    def reproducible(self) -> bool:
        """在线源不可精确重放——恒 False，无构造入口。"""
        return False


DataIdentity = SnapshotDataIdentity | LiveDataIdentity


def snapshot_identity(
    snapshot: RuntimeSnapshot, *, verified_at: str | None = None
) -> SnapshotDataIdentity:
    """`RuntimeSnapshot` → 快照身份。

    sha 直接取 `snapshot.sha`（唯一来源，不重算）；digest 由 meta 内容确定性
    计算；verified_at 缺省取当前时间（+08:00）。meta 缺失即拒绝——半成品身份
    比没有身份更坏。
    """
    sha = _require_nonempty(snapshot.sha, "snapshot.sha")
    meta = snapshot.meta
    if not isinstance(meta, dict) or not meta:
        raise ValueError("snapshot.meta 必须是非空 dict（digest 的唯一来源）")
    return SnapshotDataIdentity(
        mode="snapshot",
        snapshot_sha=sha,
        snapshot_digest=snapshot_digest(meta),
        verified_at=_require_explicit_timezone(verified_at or _now_iso(), "verified_at"),
    )


def live_identity(
    *,
    source_id: str,
    source_revision: str,
    schema_digest: str,
    observed_at: str | None = None,
    source_version: str | None = None,
) -> LiveDataIdentity:
    """在线源身份构造（观测事实）。

    observed_at 缺省取当前时间（+08:00）；必填字段与摘要形态校验在
    `LiveDataIdentity.__post_init__` 统一执行。
    """
    return LiveDataIdentity(
        mode="live",
        source_id=source_id,
        source_revision=source_revision,
        schema_digest=schema_digest,
        observed_at=observed_at or _now_iso(),
        source_version=source_version,
    )


def require_snapshot(identity: DataIdentity | None, *, purpose: str) -> SnapshotDataIdentity:
    """N6 门禁：只接受固定快照身份；live / 未绑定一律 ValueError。

    `purpose` 说明谁在拒（评测 / 分析 / 发布验证……）——消息不带 purpose 时
    调用方拿到「只接受快照」不知道是自己哪个入口配错了。不静默降级：live
    冒充快照或未绑定就放行，正是 N6 要挡的失效形态。
    """
    if identity is None:
        raise ValueError(f"{purpose}需要固定快照数据身份，但身份未绑定：拒绝执行（N6）")
    if not isinstance(identity, SnapshotDataIdentity):
        raise ValueError(
            f"{purpose}只接受固定快照数据身份，收到 live 在线源"
            f"（source_id={identity.source_id}）：拒绝执行（N6）"
        )
    return identity


def analysis_refusal(identity: DataIdentity | None) -> str | None:
    """四步分析一致性门（D03）：snapshot → None；live / 未绑定 → 拒绝码。

    在线源没有一致性读能力时返回 `analysis_consistency_unavailable`，不得用
    四次变化中的读取做虚假贡献对账。返回拒绝码而不是抛异常：分析面按
    `reason_code` 投影（既有失败闭合口径），由调用方决定如何在响应中表达。
    """
    if isinstance(identity, SnapshotDataIdentity):
        return None
    return ANALYSIS_CONSISTENCY_UNAVAILABLE
