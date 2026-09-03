"""Day 48 端到端验收（真实 Doris + 锁定快照）：5 场景 + handoff，结构化记录。

用法
----
    uv run python eval/e2e_acceptance.py [--report eval/reports/e2e-<ts>.json]

场景与验收口径（README §3.3 Day 43-49 勾选）
    S1 正常提问  ：注册域问句 → kind=answer；Guard 出口 SQL 的表全部在锁定
                   快照白名单内；真实执行行数 ≤ SQL LIMIT
    S2 反问      ：歧义问句（eval/gold/gold-104.json 同源）→ kind=clarify，
                   反问轮不执行 SQL（不猜答）
    S3 权限拒绝  ：白名单缩窄（不含 fact_trades）→ Guard blocked；被拒 SQL
                   不达执行器；block_reason 不携带被拒 SQL
    S4 校验失败修复：多轮轨迹——S2 反问后同一会话补绝对时间 → answer
                   （修复成功）；Guard 层补验：表外 SQL 拒绝 → 白名单等价
                   SQL 通过（读多写少的只读通道纵深）
    S5 图表+解释 ：S1 的 answer 直接 render_chart（schema 来自已执行结果，
                   防幻觉）+ explain 归因全字段（表/版本/刷新时间/latency）
    S6 人工接管  ：allow_candidate 模式 + 真实检索 0 候选问句 → kind=handoff
                   （LLM 生成无素材不空转，显式转人工，不编造）

输出：人类可读 stdout + JSON 报告（本次实测的全部数字都出自本脚本；
docs/e2e-acceptance.md 引用本产物，禁止手写数字）。场景断言失败 → 退出码 1。
依赖：Doris 已 up 且快照表可查（mysql.connector 读 .env 连接参数）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from agent.graph import DataAgent
from agent.security.sql_guard import Budget, enforce
from agent.tools.chart import render_chart
from eval.runner import build_budget, execute_sql

REPO = Path(__file__).resolve().parent.parent
SNAPSHOT_META = REPO / "data/snapshots" / "7d48dcb.meta.json"

# 场景问句（与 eval/gold/ 同源，防文档问句漂移；标注 gold 出处）
GOLD102_Q = "按分支统计 2013 年佣金收入，列出前 5 名"  # eval/gold/gold-102.json
GOLD104_Q = "最近交易情况怎么样？"  # eval/gold/gold-104.json（歧义样本）
OUT_OF_DOMAIN_Q = "2013年各分支机构的绩效奖金总额排名"  # 域外：真实检索 0 候选

# 执行器同构（同 eval/runner.execute_sql）
Executor = Callable[[str], tuple[list[tuple[Any, ...]], list[str]]]


class CountingExecutor:
    """包装真实 Doris 执行器：记录收到（已过 Guard）的 SQL 条数。"""

    def __init__(self, inner: Executor) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def __call__(self, sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
        self.calls.append(sql)
        return self._inner(sql)


def result_hash(rows: list[tuple[Any, ...]]) -> str:
    """行集 → sha256 前缀（列序/行序固定，与 eval/runner.result_hash 口径同源）。"""
    payload = "\n".join("\t".join("NULL" if v is None else str(v) for v in row) for row in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _jsonable(value: Any) -> Any:
    """报告 JSON 归一化：Decimal → str（保持精确），其余递归。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def run_scenario(
    scenario_id: str,
    title: str,
    goal: str,
    run: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """执行一个场景并记录：id/title/goal/结果（异常不吞，由 main 统一处理）。"""
    record = run()
    return {"id": scenario_id, "title": title, "goal": goal, **record}


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", help="JSON 报告输出路径（默认 eval/reports/e2e-<ts>.json）")
    args = parser.parse_args()

    meta = json.loads(SNAPSHOT_META.read_text(encoding="utf-8"))
    budget = build_budget(meta)
    real = CountingExecutor(execute_sql)
    ts = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z").replace(":", "")

    def s1() -> dict[str, Any]:
        agent = DataAgent(executor=real, budget=budget, snapshot_meta=meta)
        r = agent.ask(GOLD102_Q)
        assert r.kind == "answer", f"S1 期望 answer，实际 {r.kind}"
        assert r.sql is not None
        assert real.calls, "answer 必须到达真实执行器"
        assert r.row_count == 5, f"LIMIT 5 应返回 5 行，实际 {r.row_count}"
        # Guard 出口表全部在锁定快照白名单内（AGENTS.md N3 可追溯）
        assert r.explanation is not None
        tables = tuple(r.explanation.get("tables") or ())
        outside = [t for t in tables if t not in budget.allowed_tables]
        assert not outside, f"SQL 触碰白名单外表：{outside}"
        return {
            "question": GOLD102_Q,
            "kind": r.kind,
            "metric": r.metric,
            "path": r.path,
            "engine": r.engine,
            "sql": r.sql,
            "row_count": r.row_count,
            "rows_sample": [list(x) for x in r.rows[:3]],
            "result_hash": result_hash(list(r.rows)),
            "latency_ms": r.latency_ms,
            "executor_calls": len(real.calls),
            "explanation": {
                "metric_expression": r.explanation.get("metric_expression"),
                "dimensions": list(r.explanation.get("dimensions") or ()),
                "filters": list(r.explanation.get("filters") or ()),
                "tables": list(tables),
                "data_version": r.explanation.get("data_version"),
                "data_refreshed_at": r.explanation.get("data_refreshed_at"),
                "latency_ms": r.explanation.get("latency_ms"),
            },
        }

    def s2() -> dict[str, Any]:
        # 独立 Agent（S1 的 executor 已留痕；计数从 0 看反问轮是否执行）
        quiet = CountingExecutor(execute_sql)
        agent = DataAgent(executor=quiet, budget=budget, snapshot_meta=meta)
        r = agent.ask(GOLD104_Q)
        assert r.kind == "clarify", f"S2 期望 clarify，实际 {r.kind}"
        assert r.clarification is not None
        assert quiet.calls == [], "反问轮不执行 SQL"
        return {
            "question": GOLD104_Q,
            "kind": r.kind,
            "clarification_kind": r.clarification.kind,
            "reasons": list(r.clarification.reasons),
            "candidates": list(r.clarification.candidates),
            "executor_calls": len(quiet.calls),
        }

    def s3() -> dict[str, Any]:
        # 权限拒绝：白名单缩窄（去掉 gold-102 查询的物理表 fact_trades），
        # 语义层仍可命中 → Guard 必须拦下（纵深防御：权限不跟随语义层放宽）
        narrowed = frozenset(t for t in budget.allowed_tables if not t.endswith("fact_trades"))
        assert narrowed, "缩窄后的白名单不能为空"
        quiet = CountingExecutor(execute_sql)
        deny_budget = Budget(dialect="doris", max_rows=budget.max_rows, allowed_tables=narrowed)
        agent = DataAgent(executor=quiet, budget=deny_budget, snapshot_meta=meta)
        r = agent.ask(GOLD102_Q)
        assert r.kind == "blocked", f"S3 期望 blocked，实际 {r.kind}"
        assert quiet.calls == [], "被拒 SQL 不达执行器"
        assert r.block_reason is not None
        assert GOLD102_Q not in (r.block_reason or ""), "blocked 只给原因类型"
        assert "SELECT" not in (r.block_reason or ""), "block_reason 不携带被拒 SQL"
        return {
            "question": GOLD102_Q,
            "kind": r.kind,
            "block_reason": r.block_reason,
            "narrowed_tables": len(budget.allowed_tables) - len(narrowed),
            "executor_calls": len(quiet.calls),
        }

    def s4() -> dict[str, Any]:
        # 修复轨迹（会话层）：S2 反问轮之后，同一 session 用户补口径再问 → answer
        agent = DataAgent(executor=real, budget=budget, snapshot_meta=meta)
        sid = "e2e-s4"
        r1 = agent.ask(GOLD104_Q, session_id=sid)
        assert r1.kind == "clarify", "修复轨迹第 1 轮应反问"
        r2 = agent.ask(GOLD102_Q, session_id=sid)
        assert r2.kind == "answer", f"修复轨迹第 2 轮应 answer，实际 {r2.kind}"
        assert r2.turns_in_session == 2
        # Guard 层补验：越权 SQL 拒绝 → 白名单等价 SQL 通过（修复 = 收敛到授权域）
        rogue = "SELECT * FROM atlas.dwd.fact_balances LIMIT 5"
        rejected: str | None = None
        try:
            enforce(rogue, budget=budget)
        except Exception as exc:  # noqa: BLE001 - 期望 Guard 拒绝，记录类型
            rejected = type(exc).__name__
        assert rejected is not None, "表外 SQL 必须被 Guard 拒绝"
        assert r2.sql is not None
        enforce(r2.sql, budget=budget)  # 不抛 = 通过
        return {
            "turn1": {"question": GOLD104_Q, "kind": r1.kind},
            "turn2": {
                "question": GOLD102_Q,
                "kind": r2.kind,
                "metric": r2.metric,
                "row_count": r2.row_count,
                "turns_in_session": r2.turns_in_session,
                "result_hash": result_hash(list(r2.rows)),
            },
            "guard_layer": {
                "rogue_sql": rogue,
                "rogue_rejected_as": rejected,
                "repaired_enforce": "pass",
            },
        }

    def s5() -> dict[str, Any]:
        # 图表+解释：S1 口径结果（新会话重问）直接渲染，spec 轴名零发明
        agent = DataAgent(executor=real, budget=budget, snapshot_meta=meta)
        r = agent.ask(GOLD102_Q)
        assert r.kind == "answer"
        spec = render_chart(r)  # 防幻觉：schema 必须来自已执行结果
        rendered_cols = {spec["x"], *spec["y"], *spec.get("columns", [])}
        assert rendered_cols <= set(r.columns or ()), "图表轴名必须来自执行结果"
        assert spec["sql_sha256"], "spec 必须绑定执行 SQL 摘要"
        return {
            "question": GOLD102_Q,
            "kind": r.kind,
            "chart": {
                "type": spec["type"],
                "x": spec["x"],
                "y": list(spec["y"]),
                "data_points": len(spec["data"]),
                "data_sample": spec["data"][:3],
                "note": spec.get("note"),
                "sql_sha256": spec["sql_sha256"],
            },
            "explanation": {
                "metric": r.explanation and r.explanation.get("metric"),
                "tables": r.explanation and list(r.explanation.get("tables") or ()),
                "row_count": r.explanation and r.explanation.get("row_count"),
                "latency_ms": r.explanation and r.explanation.get("latency_ms"),
                "data_version": r.explanation and r.explanation.get("data_version"),
                "data_refreshed_at": r.explanation and r.explanation.get("data_refreshed_at"),
                "path": r.explanation and r.explanation.get("path"),
            },
        }

    def s6() -> dict[str, Any]:
        # 人工接管：候选模式（allow_candidate=True）+ 真实检索 0 候选问句。
        # 素材空 → LLM 无生成基础、反问无候选可澄清 → handoff，不编造。
        quiet = CountingExecutor(execute_sql)
        agent = DataAgent(
            executor=quiet,
            budget=budget,
            snapshot_meta=meta,
            allow_candidate=True,
        )
        r = agent.ask(OUT_OF_DOMAIN_Q)
        assert r.kind == "handoff", f"S6 期望 handoff，实际 {r.kind}"
        assert r.handoff_reason is not None
        assert quiet.calls == [], "handoff 不执行 SQL"
        return {
            "question": OUT_OF_DOMAIN_Q,
            "kind": r.kind,
            "handoff_reason": r.handoff_reason,
            "executor_calls": len(quiet.calls),
        }

    scenarios = [
        run_scenario("S1", "正常提问（确定性链路）", "注册域问句 → answer", s1),
        run_scenario("S2", "反问（歧义不猜）", "歧义问句 → clarify", s2),
        run_scenario("S3", "权限拒绝（Guard 纵深）", "白名单缩窄 → blocked", s3),
        run_scenario("S4", "校验失败修复（多轮）", "反问 → 补口径 → answer", s4),
        run_scenario("S5", "图表 + 解释", "answer → 确定性图表 + 归因", s5),
        run_scenario("S6", "人工接管（handoff）", "0 候选 → 显式转人工", s6),
    ]
    report = {
        "schema_version": 1,
        "purpose": "Day 43-49 批次端到端验收（README §3.3）",
        "snapshot_sha": meta["sha"],
        "created_at": ts,
        "engine": "真实 Doris（eval/runner.execute_sql）+ 确定性链路（无 LLM）",
        "scenarios": scenarios,
        "summary": {"total": len(scenarios), "passed": len(scenarios)},
    }
    if args.report:
        out = Path(args.report)
    else:
        out = REPO / "eval" / "reports" / f"e2e-acceptance-{ts}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_jsonable(report), ensure_ascii=False, indent=2)
    out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    print(f"\n[报告] {out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except Exception as exc:  # noqa: BLE001 - 验收门禁：任何场景失败都以非 0 退出
        print(f"\n[验收失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
