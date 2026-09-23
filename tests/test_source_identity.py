"""数据身份与运行上下文合同（ADR-0031 D03/T04）。

口径
----
- 数据身份是带判别字段的联合类型：`snapshot`（绑定锁定快照、可精确重放）与
  `live`（在线源观测、不可精确重放）。live **结构性**没有 snapshot_sha 字段
  ——不能把在线源伪造成快照（D03）；`reproducible` 恒 False 且构造不出 True。
- 评测只接受固定快照身份（N6）：`require_snapshot` 对 live 与未绑定一律拒绝，
  消息带 purpose 说明是谁在拒。
- live 的四步分析资格返回 `analysis_consistency_unavailable`（无一致性读不得
  做贡献对账，D03）；快照源资格为 None。接口在 T04 就位，接入归 T05/T06 runs 面。
- 时间戳必须显式时区（AGENTS §7.3）；digest 由 meta 内容确定性计算（同 meta 同
  digest）——不引入第二事实源。
- `RunContext` 是正式执行合同（账本 T04：不使用 test_executor 之类测试属性），
  executor 是正式注入接缝；`budget_from_snapshot` 与旧 `eval.runner.build_budget`
  同口径（白名单 = 快照内全部表）。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
_META: dict[str, Any] = json.loads(
    (REPO / "data/snapshots" / "7d48dcb.meta.json").read_text(encoding="utf-8")
)
TZ = timezone(timedelta(hours=8))
VERIFIED_AT = "2026-09-22T10:00:00+08:00"
OBSERVED_AT = "2026-09-22T10:05:00+08:00"
SCHEMA_DIGEST = "ab" * 32  # 64 hex，形态与 sha256 一致


def _runtime_snapshot() -> Any:
    from data.identity import RuntimeSnapshot

    return RuntimeSnapshot(
        sha="7d48dcb", meta=_META, source="head", bound_to_head=True, head="7d48dcb"
    )


def _live() -> Any:
    from agent.runtime.identity import live_identity

    return live_identity(
        source_id="doris-primary",
        source_revision="rev-2026-09-22-1",
        schema_digest=SCHEMA_DIGEST,
        observed_at=OBSERVED_AT,
        source_version="Doris 4.1",
    )


# ---------------------------------------------------------------------------
# snapshot 身份
# ---------------------------------------------------------------------------


def test_snapshot_identity_binds_sha_and_digest() -> None:
    """sha 来自 RuntimeSnapshot（唯一来源）；digest = meta 内容确定性摘要。"""
    from agent.runtime.identity import snapshot_digest, snapshot_identity

    ident = snapshot_identity(_runtime_snapshot(), verified_at=VERIFIED_AT)
    assert ident.mode == "snapshot"
    assert ident.snapshot_sha == "7d48dcb"
    assert ident.snapshot_digest == snapshot_digest(_META)
    assert len(ident.snapshot_digest) == 64
    assert ident.verified_at == VERIFIED_AT


def test_snapshot_digest_is_deterministic_and_content_sensitive() -> None:
    """同内容 → 同 digest（跨对象重载稳定）；内容变 → digest 变。"""
    from agent.runtime.identity import snapshot_digest

    same_content = json.loads(json.dumps(_META))
    assert snapshot_digest(_META) == snapshot_digest(same_content)
    mutated = dict(_META)
    mutated["created_at"] = "2026-01-01T00:00:00+08:00"
    assert snapshot_digest(mutated) != snapshot_digest(_META)


def test_snapshot_identity_requires_explicit_timezone() -> None:
    """无时区的复核时间必须拒绝（AGENTS §7.3：时间戳显式声明时区）。"""
    from agent.runtime.identity import snapshot_identity

    with pytest.raises(ValueError, match="时区"):
        snapshot_identity(_runtime_snapshot(), verified_at="2026-09-22T10:00:00")


def test_snapshot_identity_defaults_verified_at_with_timezone() -> None:
    """缺省 verified_at 自动生成带时区的时间戳（不产生裸时间）。"""
    from agent.runtime.identity import snapshot_identity

    ident = snapshot_identity(_runtime_snapshot())
    parsed = datetime.fromisoformat(ident.verified_at)
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# live 身份
# ---------------------------------------------------------------------------


def test_live_identity_has_no_snapshot_sha() -> None:
    """live 不能伪造为快照：结构性没有 snapshot_sha 字段（D03）。"""
    live = _live()
    assert live.mode == "live"
    assert not hasattr(live, "snapshot_sha")
    assert live.reproducible is False
    assert live.source_id == "doris-primary"
    assert live.source_revision == "rev-2026-09-22-1"
    assert live.schema_digest == SCHEMA_DIGEST
    assert live.source_version == "Doris 4.1"


def test_live_identity_cannot_claim_reproducible() -> None:
    """reproducible 是只读属性：无法构造或赋值为 True（frozen + property）。"""
    live = _live()
    with pytest.raises((AttributeError, TypeError)):
        live.reproducible = True  # type: ignore[misc]


def test_live_identity_requires_observation_fields() -> None:
    """观测必填字段非空、observed_at 显式时区——缺失即拒绝，不静默填默认值。"""
    from agent.runtime.identity import live_identity

    with pytest.raises(ValueError, match="source_id"):
        live_identity(
            source_id="",
            source_revision="rev-1",
            schema_digest=SCHEMA_DIGEST,
            observed_at=OBSERVED_AT,
        )
    with pytest.raises(ValueError, match="schema_digest"):
        live_identity(
            source_id="doris-primary",
            source_revision="rev-1",
            schema_digest="not-a-digest",
            observed_at=OBSERVED_AT,
        )
    with pytest.raises(ValueError, match="时区"):
        live_identity(
            source_id="doris-primary",
            source_revision="rev-1",
            schema_digest=SCHEMA_DIGEST,
            observed_at="2026-09-22T10:05:00",
        )


# ---------------------------------------------------------------------------
# 评测 / 分析门禁
# ---------------------------------------------------------------------------


def test_eval_refuses_live_identity() -> None:
    """评测只接受固定快照（N6）：live 身份拒绝，消息带 purpose。"""
    from agent.runtime.identity import require_snapshot

    with pytest.raises(ValueError, match="评测"):
        require_snapshot(_live(), purpose="评测")


def test_eval_refuses_unbound_identity() -> None:
    """未绑定数据身份同样拒绝：评测不能在没有身份的情况下出数字。"""
    from agent.runtime.identity import require_snapshot

    with pytest.raises(ValueError, match="未绑定"):
        require_snapshot(None, purpose="评测")


def test_snapshot_passes_eval_gate() -> None:
    """快照身份通过门禁并原对象返回（不重建，不换身份）。"""
    from agent.runtime.identity import require_snapshot, snapshot_identity

    ident = snapshot_identity(_runtime_snapshot())
    assert require_snapshot(ident, purpose="评测") is ident


def test_live_analysis_consistency_unavailable() -> None:
    """live 源无一致性读 → 四步分析拒绝（固定 reason code，不编造对账）。"""
    from agent.runtime.identity import analysis_refusal

    assert analysis_refusal(_live()) == "analysis_consistency_unavailable"


def test_snapshot_analysis_available() -> None:
    """快照源是不变读 → 分析资格为 None（无拒绝理由）。"""
    from agent.runtime.identity import analysis_refusal, snapshot_identity

    assert analysis_refusal(snapshot_identity(_runtime_snapshot())) is None


# ---------------------------------------------------------------------------
# RunContext 合同
# ---------------------------------------------------------------------------


def test_run_context_formal_executor_seam() -> None:
    """executor 是 RunContext 的正式注入接缝（账本 T04：无测试专用属性）。"""
    from agent.runtime.context import RunContext
    from agent.security.sql_guard import Budget

    calls: list[str] = []

    def executor(sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
        calls.append(sql)
        return [], []

    ctx = RunContext(
        budget=Budget(dialect="doris"),
        executor=executor,
        data_identity=_live(),
    )
    assert ctx.executor is executor
    assert ctx.data_identity is not None and ctx.data_identity.mode == "live"
    assert ctx.identity is None
    assert ctx.compiler is None
    ctx.executor("SELECT 1")  # 接缝可调用（内核持有，测试只验证正式字段可用）
    assert calls == ["SELECT 1"]


def test_run_context_defaults_are_fail_closed() -> None:
    """缺省上下文不可执行：executor=None（无源）、identity=None（无行级策略）。

    这不是可选项堆砌——fail-closed 的默认值让「忘了装配」表现为明确的拒绝
    而不是静默用某个兜底对象执行查询。
    """
    from agent.runtime.context import RunContext
    from agent.security.sql_guard import Budget

    ctx = RunContext(budget=Budget(dialect="doris"))
    assert ctx.executor is None
    assert ctx.identity is None
    assert ctx.data_identity is None
    assert ctx.reference_time is None
    assert ctx.timezone == "+08:00"


# ---------------------------------------------------------------------------
# 快照预算构造（旧 build_budget 的共享实现）
# ---------------------------------------------------------------------------


def test_budget_from_snapshot_expands_allowlist() -> None:
    """快照 meta → Guard 预算：白名单 = 快照内全部表（与旧 build_budget 同口径）。"""
    from agent.runtime.context import budget_from_snapshot

    budget = budget_from_snapshot(_META)
    expected = {
        f"atlas.{ns}.{table}"
        for ns, tables in _META["row_counts"].items()
        for table in tables
    }
    assert budget.allowed_tables == frozenset(expected)
    assert budget.dialect == "doris"
    assert budget.max_rows == 10_000


def test_budget_from_snapshot_rejects_malformed_meta() -> None:
    """缺 row_counts 或值不可迭代的 meta 拒绝构造——静默给空白名单会让一切查询
    被误拒；字符串值会被逐字符迭代成垃圾白名单，必须显式拒绝。"""
    from agent.runtime.context import budget_from_snapshot

    with pytest.raises(ValueError, match="row_counts"):
        budget_from_snapshot({"sha": "deadbee", "created_at": "2026-01-01T00:00:00+08:00"})
    with pytest.raises(ValueError, match="row_counts"):
        budget_from_snapshot({"sha": "deadbee", "row_counts": {"dwd": "fact_trades"}})
    with pytest.raises(ValueError, match="row_counts"):
        budget_from_snapshot({"sha": "deadbee", "row_counts": {"dwd": 5}})


def test_budget_from_snapshot_accepts_table_name_iterables() -> None:
    """合成 meta 的表名集合形态继续可展开——旧 build_budget 只要「可迭代表名」，
    ADR-0019 调用点判据（budget 必须来自解析出的那份 meta）依赖该行为，委托后
    必须保持等价。"""
    from agent.runtime.context import budget_from_snapshot

    budget = budget_from_snapshot({"sha": "synthe1", "row_counts": {"dwd": {"probe_only"}}})
    assert budget.allowed_tables == frozenset({"atlas.dwd.probe_only"})


if __name__ == "__main__":
    unittest.main()
