"""业务审计 JSONL（服务面硬化项 ②，ADR-0011 决策 2 serving/auth 硬化）。

口径（诚实声明）
--------------------
- **每业务/治理请求一行**（/api/v1 下各端点处理结果 + 429/422 业务拒绝），
  字段全集恒定：ts / claims(role,sub) / endpoint / session_id / kind /
  row_count / latency_ms / status / bucket——无值一律 null，契约测试锁定字段集。
- **不含 SQL**：SQL 细节由 OTel atlas.turn span 承担（职责单一不重复），
  session_id 关联；claims 只取 role/sub（不含 user_context 条件值——
  与 0011「不外泄细节」同精神，谓词值只存在于策略渲染面）。
- kind 取值按端点语义如实：/plan → plan|clarify；/compile → compiled|
  compile_error；/ask 与 /plan/execute → 回合 kind（answer|clarify|blocked|
  error|handoff）或 conflict（会话身份冲突 422）；限流命中 → rate_limited；
  治理面每请求一行 → governance_read（ADR-0022 决策 ⑥）。
- bucket ∈ business | governance：该请求所在限流桶（ADR-0022 决策 ⑥ 两桶），
  非限流相关行同样按请求来源面填写——同一份 JSONL 里可直接按面过滤。
- 本地文件非防篡改（无签名/权限加固），生产应外置（README KL #28 ③ 收窄
  同步声明）；uvicorn workers=1——本模块的进程内追加写是收窄后理由之一
  （限流桶 + 审计写 + SQLite 单写者，ADR-0020 决策 ⑧）。
- 默认开；ATLAS_AUDIT_DISABLED=1 关闭（env 开关，见 .env.example）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 审计文件默认目录：serving/audit/（gitignore；运行时 mkdir，不入库）
DEFAULT_DIR = Path(__file__).resolve().parent / "audit"
AUDIT_FILENAME = "audit.jsonl"

TZ = timezone(timedelta(hours=8))  # 契约要求：时间戳显式 +08:00（与快照同口径）

# 审计字段全集（契约测试锁定 keys；顺序即 JSONL 行内顺序，可读性优先）
FIELDS = (
    "ts",
    "claims",
    "endpoint",
    "session_id",
    "kind",
    "row_count",
    "latency_ms",
    "status",
    "bucket",
)


def audit_enabled() -> bool:
    """环境开关：ATLAS_AUDIT_DISABLED=1 关闭审计（缺省开）。"""
    return os.environ.get("ATLAS_AUDIT_DISABLED") != "1"


class AuditLog:
    """进程内 JSONL 追加写（append + 每行 flush；单 worker 无锁冲突）。"""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        enabled: bool | None = None,
    ) -> None:
        self.directory = Path(directory or DEFAULT_DIR)
        # enabled 缺省读环境开关（默认开）；显式传值供测试关闭/固定
        self.enabled = audit_enabled() if enabled is None else enabled

    def record(
        self,
        *,
        endpoint: str,
        claims: dict[str, object],
        session_id: str | None = None,
        kind: str | None = None,
        row_count: int | None = None,
        latency_ms: float | None = None,
        status: int = 200,
        bucket: str | None = None,
    ) -> None:
        """记一行业务审计事件（字段全集恒定，无值为 null）。

        claims 传完整已验证 claims（verify_token 输出），本函数只取 role/sub
        两个身份维——user_context 条件值不进审计（0011 不外泄细节）。
        bucket ∈ business | governance（ADR-0022 决策 ⑥ 两桶）：调用方按请求
        来源面填写；None 只在无桶语义的调用点出现（当前无此类调用点）。
        """
        if not self.enabled:
            return
        row: dict[str, Any] = {
            "ts": datetime.now(TZ).isoformat(timespec="seconds"),
            "claims": {
                "role": claims.get("role"),
                "sub": claims.get("sub"),
            },
            "endpoint": endpoint,
            "session_id": session_id,
            "kind": kind,
            "row_count": row_count,
            "latency_ms": latency_ms,
            "status": status,
            "bucket": bucket,
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / AUDIT_FILENAME
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
