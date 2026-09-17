"""Day 48 端到端验收（真实 Doris + 锁定快照）：S1~S7 问数场景 + S8~S12 分析场景。

用法
----
    uv run python eval/e2e_acceptance.py [--report eval/reports/e2e-acceptance-<sha>.json]
        [--snapshot-sha <快照sha>]

场景与验收口径（README §3.3 Day 43-49 勾选 + ADR-0014 ② 多轮追问）
    S1 正常提问  ：注册域问句 → kind=answer；Guard 出口 SQL 的表全部在锁定
                   快照白名单内；真实执行行数 ≤ SQL LIMIT
    S2 反问      ：歧义问句（eval/gold/finance/gold-104.json 同源）→ kind=clarify，
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
    S7 多轮追问  ：S1 的 2013 口径后同会话追问「那 2014 年呢」→ kind=answer
                   （ADR-0014 ② 指代补全：复用上轮结构仅换时间）；追问轮 SQL
                   再过 Guard 复核（enforce 不抛 + 出口表全在白名单内）

ADR-0026 T10 分析场景（S8~S12；样本/预算/资格证据绑定同一 --snapshot-sha 快照，
该快照缺资格证据（<sha>.analysis.json）时场景记 fail 而非静默跳过）：
    S8 绝对期间贡献：分析问句 → 四步 → analysis.status=ok；totals 只对账样本
                   eval/analysis/finance/attribution-001.json 的 reference 期望
                   （不硬编码实测字面量），不断言 delta 符号（不预设涨跌）
    S9 受限角色  ：branch_manager /analyze → Guard blocked（RLS 谓词需 join 注入）；
                   零 SQL 达执行器、无综合、同会话后续 /ask 仍可用
    S10 相对时间 ：相对期间问句 → clarify(relative_time)，零 SQL（不猜口径）
    S11 中途拒绝 ：白名单缩窄（去 dim_broker）→ 步 3 被拒即裁剪（后续步不执行，
                   部分结果不作最终解释）
    S12 综合不可用：max_rows=1 → 分组步触及 LIMIT → possible_truncation
                   （四步全成才进综合；不可用综合不携带总量）

输出：人类可读 stdout + JSON 报告（本次实测的全部数字都出自本脚本；
docs/e2e-acceptance.md 引用本产物，禁止手写数字）。单场景失败被记录
（status=fail + error 摘要）继续跑完；summary.failed > 0 → 退出码 1。
依赖：Doris 已 up 且快照表可查（pymysql 读 .env 连接参数）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from agent.analysis import analysis_status
from agent.compiler import SemanticModel
from agent.graph import DataAgent
from agent.security.sql_guard import Budget, enforce
from agent.tools.chart import render_chart
from eval.analysis_eligibility import load_eligibility
from eval.runner import build_budget, execute_sql, git_short_sha
from serving.auth import sign_token, verify_token

REPO = Path(__file__).resolve().parent.parent
SNAPSHOTS_DIR = REPO / "data/snapshots"
# 缺省保持既有裸跑现状（S1~S7 锚定 7d48dcb）；--snapshot-sha 给定后全部场景绑定该快照
DEFAULT_SNAPSHOT_SHA = "7d48dcb"

# 场景问句（与 eval/gold/ 同源，防文档问句漂移；标注 gold 出处）
GOLD102_Q = "按分支统计 2013 年佣金收入，列出前 5 名"  # eval/gold/finance/gold-102.json
GOLD104_Q = "最近交易情况怎么样？"  # eval/gold/finance/gold-104.json（歧义样本）
OUT_OF_DOMAIN_Q = "2013年各分支机构的绩效奖金总额排名"  # 域外：真实检索 0 候选
# ADR-0026 T10 分析场景问句（与 eval/analysis/finance/attribution-001.json 同源）
ATTRIBUTION_Q = "分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献"
RELATIVE_TIME_Q = "分析 2013Q4 相对上季度的佣金收入变化贡献"
# S9 受限角色载体：branch 载荷值与 eval/api_acceptance.py A6 同源实测值（单源注释互证）
BRANCH = "uHtbMrIxbLVfWHFhCIeAnTu"
# S8 数字唯一来源（AGENTS.md N1）：totals 断言值只取该样本 reference，禁止硬编码实测字面量
ATTRIBUTION_SAMPLE = REPO / "eval/analysis/finance/attribution-001.json"

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
    """执行一个场景并记录：id/title/goal/status/结果（R10-6 报告契约）。

    单场景异常（含场景内断言失败）捕获记 status="fail" + error 摘要后返回，
    不中断后续场景（「通过数从实际 scenarios 生成」的前提是全部场景都跑完）；
    main 结束时按 summary.failed 决定退出码，既有「失败 → 非零退出」语义保留。
    """
    try:
        record = run()
    except Exception as exc:  # noqa: BLE001 - 单场景失败必须被记录而不是炸掉整个验收
        return {
            "id": scenario_id,
            "title": title,
            "goal": goal,
            "status": "fail",
            "error": f"{type(exc).__name__}: {exc}",
        }
    # status 放在 record 之后：场景返回体不得覆盖门禁判定的 pass 终态
    return {"id": scenario_id, "title": title, "goal": goal, **record, "status": "pass"}


def build_summary(scenarios: list[dict[str, Any]]) -> dict[str, int]:
    """summary 从实际 scenario status 生成（R10-6）：禁止 passed=len(scenarios) 硬编码。

    passed 只计 status=="pass"；failed = total - passed（任何非 pass 形态一律
    记失败，fail-closed，不给「既非 pass 也非 fail」的状态留活口）。
    """
    total = len(scenarios)
    passed = sum(1 for s in scenarios if s.get("status") == "pass")
    return {"total": total, "passed": passed, "failed": total - passed}


def build_report(
    scenarios: list[dict[str, Any]],
    *,
    snapshot_sha: str,
    code_sha: str,
    created_at: str,
) -> dict[str, Any]:
    """构建 e2e 验收报告（R10-6）：code_sha + 绑定 snapshot_sha + 实际 status summary。"""
    return {
        "schema_version": 1,
        "purpose": (
            "Day 43-49 批次端到端验收（README §3.3）"
            " + ADR-0014 ② 多轮同构追问 + ADR-0026 T10 多步贡献分析"
        ),
        "code_sha": code_sha,
        "snapshot_sha": snapshot_sha,
        "created_at": created_at,
        "engine": "真实 Doris（eval/runner.execute_sql）+ 确定性链路（无 LLM）",
        "scenarios": scenarios,
        "summary": build_summary(scenarios),
    }


def require_eligibility_evidence(
    snapshot_sha: str, snapshots_dir: Path | None = None
) -> dict[str, Any]:
    """分析场景资格证据门（R10-4）：证据缺失/不合格 → AssertionError（场景记 fail）。

    「快照缺资格证据时场景失败而非静默跳过」（任务原文）：门在场景执行前触发，
    失败形态是**场景 fail**，不是 skip/缺席。检查与 eval.analysis_eligibility
    的绑定口径同源：`<sha>.analysis.json` 存在、eligible 为 true、snapshot_sha
    自洽（semantic_sha256 绑定由 load_eligibility 在 agent 侧复核，这里只挡
    「证据根本不存在/未登记」的快照，避免把前置门失败误当真实分析结果）。

    参数
    ----
    snapshot_sha  : 锁定快照 sha（--snapshot-sha 绑定值）。
    snapshots_dir : 快照目录（缺省 data/snapshots；测试注入临时目录）。

    返回
    ----
    证据 dict（eligible=True），供场景留痕（semantic_sha256 等）。

    抛出
    ----
    AssertionError：证据文件缺失 / 不可读 / eligible 非 true / 快照 sha 不符。
    """
    directory = Path(snapshots_dir) if snapshots_dir is not None else SNAPSHOTS_DIR
    path = directory / f"{snapshot_sha}.analysis.json"
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(
            f"快照 {snapshot_sha} 缺分析资格证据（{path}）：分析场景失败而非静默跳过"
            "——请先对该快照跑 make analysis-verify（T02 资格复核）登记证据"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AssertionError(f"快照 {snapshot_sha} 的资格证据不可读（{path}）：{exc}") from exc
    assert isinstance(evidence, dict), f"资格证据形态异常（非 dict）：{path}"
    assert evidence.get("eligible") is True, (
        f"快照 {snapshot_sha} 的资格证据未登记合格（eligible != true）：{path}"
    )
    assert evidence.get("snapshot_sha") == snapshot_sha, (
        f"资格证据快照绑定不符：证据声明 {evidence.get('snapshot_sha')!r} ≠ {snapshot_sha!r}"
    )
    return evidence


_REPORT_SHA_RE = re.compile(r"^e2e-acceptance-([0-9a-f]+)\.json$")


def _sample_reference_total(results: list[Any], role: str, metric: str) -> Decimal:
    """样本 reference 总量角色块 → 指标 Decimal（S8 数字唯一来源，AGENTS.md N1）。

    只认「恰一行、列含与指标同名」的总量块（与 eval/analysis_eval._single_total
    同契约）；畸形参考块 raise，绝不回退取最后一列（fail-closed，防错位比对）。
    """
    block = next((b for b in results if isinstance(b, dict) and b.get("role") == role), None)
    assert block is not None, f"样本 reference 缺角色块 {role}"
    columns = block.get("columns")
    assert isinstance(columns, list) and metric in columns, f"样本 {role} 缺指标列 {metric}"
    rows = block.get("rows")
    assert isinstance(rows, list) and len(rows) == 1, f"样本 {role} 总量块必须恰一行"
    row = rows[0]
    assert isinstance(row, list) and len(row) > columns.index(metric), f"样本 {role} 行缺指标列"
    cell = row[columns.index(metric)]
    assert cell is not None, f"样本 {role} 指标单元格为空"
    return Decimal(str(cell))


def validate_report_path(report_path: str | Path, code_sha: str) -> Path:
    """归档 SHA 闸门（R10-6）：`e2e-acceptance-<hex>.json` 的 hex 必须 == 真实 HEAD sha。

    「归档前检查真实 SHA，不手造报告」：文件名带 sha 即必须与 git_short_sha()
    一致，否则拒绝落盘（make 归档名取 HEAD 短 sha，防 HEAD 前进后归档到旧名）。
    非 sha 形态路径（如既有 e2e-acceptance.json 或任意 --report 路径）不受闸门
    约束，行为不变。

    抛出
    ----
    ValueError：文件名形如 e2e-acceptance-<hex>.json 且 hex ≠ code_sha。
    """
    path = Path(report_path)
    matched = _REPORT_SHA_RE.match(path.name)
    if matched and matched.group(1) != code_sha:
        raise ValueError(
            f"报告文件名 sha {matched.group(1)!r} ≠ 当前 HEAD {code_sha!r}："
            "归档前必须核对真实 SHA，不手造报告"
        )
    return path


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", help="JSON 报告输出路径（默认 eval/reports/e2e-acceptance-<ts>.json）"
    )
    parser.add_argument(
        "--snapshot-sha",
        default=DEFAULT_SNAPSHOT_SHA,
        help="全部场景（含 S1~S7 的 budget）绑定的快照 sha；分析场景还要求该快照"
        "已登记资格证据（data/snapshots/<sha>.analysis.json），缺失 → 场景记 fail",
    )
    args = parser.parse_args()

    snapshot_sha = args.snapshot_sha
    meta = json.loads((SNAPSHOTS_DIR / f"{snapshot_sha}.meta.json").read_text(encoding="utf-8"))
    assert meta.get("sha") == snapshot_sha, (
        f"meta.sha 与 --snapshot-sha 不符：{SNAPSHOTS_DIR / f'{snapshot_sha}.meta.json'}"
    )
    budget = build_budget(meta)
    code_sha = git_short_sha()
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

    def s7() -> dict[str, Any]:
        # 多轮同构追问（ADR-0014 ②）：上轮 2013 佣金 Top5 分支 → 「那 2014 年呢」
        # → 指代补全为 2014 同构结果；追问轮 SQL 再过 Guard 复核（纵深不放松）
        agent = DataAgent(executor=real, budget=budget, snapshot_meta=meta)
        sid = "e2e-s7"
        r1 = agent.ask(GOLD102_Q, session_id=sid)
        assert r1.kind == "answer", f"S7 首轮期望 answer，实际 {r1.kind}"
        r2 = agent.ask("那 2014 年呢", session_id=sid)
        assert r2.kind == "answer", f"S7 追问轮期望 answer，实际 {r2.kind}"
        assert r2.turns_in_session == 2
        assert r2.metric == "commission_revenue", "追问必须继承上轮指标"
        assert r2.row_count == 5, "同构追问应保持 LIMIT 5 行级口径"
        assert r2.explanation is not None
        dims = tuple(r2.explanation.get("dimensions") or ())
        assert dims == ("Branch",), f"追问必须继承分支维度，实际 {dims}"
        assert r2.sql is not None
        # Guard 注入与行级结果一致断言：追问轮 SQL 再过 enforce 不抛；出口表全白名单
        enforce(r2.sql, budget=budget)
        tables = tuple(r2.explanation.get("tables") or ())
        outside = [t for t in tables if t not in budget.allowed_tables]
        assert not outside, f"追问轮 SQL 触碰白名单外表：{outside}"
        return {
            "turn1": {
                "question": GOLD102_Q,
                "kind": r1.kind,
                "result_hash": result_hash(list(r1.rows)),
                "row_count": r1.row_count,
            },
            "turn2": {
                "question": "那 2014 年呢",
                "kind": r2.kind,
                "metric": r2.metric,
                "turns_in_session": r2.turns_in_session,
                "dimensions": list(dims),
                "row_count": r2.row_count,
                "sql": r2.sql,
                "result_hash": result_hash(list(r2.rows)),
                "rows_sample": [list(x) for x in r2.rows[:3]],
                "latency_ms": r2.latency_ms,
            },
            "guard": {
                "enforce_again": "pass",
                "outside_tables": outside,
                "tables": list(tables),
            },
        }

    def s8() -> dict[str, Any]:
        # 绝对期间贡献（ADR-0026，不预设涨跌）：样本/预算/资格证据/结果绑定同一
        # 快照；totals 断言值只来自样本 reference（AGENTS.md N1），不断言 delta 符号
        evidence = require_eligibility_evidence(snapshot_sha)
        sample = json.loads(ATTRIBUTION_SAMPLE.read_text(encoding="utf-8"))
        assert sample.get("snapshot_sha") == snapshot_sha, (
            f"样本快照绑定 {sample.get('snapshot_sha')!r} ≠ 运行快照 {snapshot_sha!r}"
        )
        assert sample.get("semantic_sha256") == evidence.get("semantic_sha256"), (
            "样本语义 sha 与资格证据不一致（同快照必须同语义层）"
        )
        quiet = CountingExecutor(execute_sql)
        agent = DataAgent(executor=quiet, budget=budget, snapshot_meta=meta)
        # 直构 agent 无 analysis_eligibility（仅 factory 附加）：分析场景必须显式挂资格
        agent.analysis_eligibility = load_eligibility(SemanticModel(), meta)
        r = agent.analyze(ATTRIBUTION_Q, session_id="e2e-s8")
        expected_plan = sample["expected_plan"]
        assert r.turn.kind == "answer", f"S8 期望 answer，实际 {r.turn.kind}"
        assert analysis_status(r) == "ok", f"S8 期望分析 ok，实际 {analysis_status(r)}"
        assert r.plan is not None
        assert r.plan.metric == expected_plan["metric"], "S8 指标与样本期望不符"
        assert r.plan.dimension == expected_plan["dimension"], "S8 维度与样本期望不符"
        assert (r.plan.baseline.granularity, r.plan.baseline.value) == (
            expected_plan["baseline"]["granularity"],
            expected_plan["baseline"]["value"],
        ), "S8 基期与样本期望不符"
        assert (r.plan.current.granularity, r.plan.current.value) == (
            expected_plan["current"]["granularity"],
            expected_plan["current"]["value"],
        ), "S8 当期与样本期望不符"
        assert len(r.steps) == 4, f"S8 期望恰 4 步，实际 {len(r.steps)}"
        assert all(s.sql for s in r.steps), "S8 四步必须全带 SQL"
        assert all("LIMIT" in (s.sql or "").upper() for s in r.steps), "S8 每条 SQL 缺 LIMIT"
        assert all("2013" in (s.sql or "") for s in r.steps), "S8 每条 SQL 缺 2013 时间约束"
        assert len(quiet.calls) == 4, f"S8 应恰执行 4 步 SQL，实际 {len(quiet.calls)}"
        attr = r.attribution
        assert attr is not None and attr.status == "ok", "S8 必须产出 ok 综合"
        assert attr.items, "S8 贡献项不得为空"
        reference = sample["reference"]["results"]
        base_expected = _sample_reference_total(
            reference, "baseline_total", expected_plan["metric"]
        )
        cur_expected = _sample_reference_total(reference, "current_total", expected_plan["metric"])
        assert attr.baseline == base_expected, (
            f"S8 baseline 期望 {base_expected}（样本 reference），实际 {attr.baseline}"
        )
        assert attr.current == cur_expected, (
            f"S8 current 期望 {cur_expected}（样本 reference），实际 {attr.current}"
        )
        assert attr.delta == cur_expected - base_expected, "S8 delta 与两期总量复算不一致"
        assert r.snapshot_sha == snapshot_sha, "S8 结果必须绑定运行快照"
        return {
            "question": ATTRIBUTION_Q,
            "kind": r.turn.kind,
            "analysis_status": analysis_status(r),
            "metric": r.plan.metric,
            "dimension": r.plan.dimension,
            "baseline": {
                "granularity": r.plan.baseline.granularity,
                "value": r.plan.baseline.value,
            },
            "current": {
                "granularity": r.plan.current.granularity,
                "value": r.plan.current.value,
            },
            "steps": len(r.steps),
            "totals": {
                "baseline": str(attr.baseline),
                "current": str(attr.current),
                "delta": str(attr.delta),
            },
            "items_count": len(attr.items),
            "executor_calls": len(quiet.calls),
            "snapshot_sha": r.snapshot_sha,
            "semantic_sha256": r.semantic_sha256,
        }

    def s9() -> dict[str, Any]:
        # 真实受限角色（R10-2）：branch_manager 的 rp_branch_visible 谓词引用
        # dim_broker，分析步 1 的 SQL 无该表且 Guard 无法 join 注入 → blocked
        require_eligibility_evidence(snapshot_sha)
        identity = verify_token(sign_token("branch_manager", {"branch": BRANCH}))
        quiet = CountingExecutor(execute_sql)
        agent = DataAgent(executor=quiet, budget=budget, snapshot_meta=meta)
        agent.analysis_eligibility = load_eligibility(SemanticModel(), meta)
        sid = "e2e-s9"
        r1 = agent.analyze(ATTRIBUTION_Q, session_id=sid, identity=identity)
        assert r1.turn.kind == "blocked", f"S9 期望 blocked，实际 {r1.turn.kind}"
        assert r1.reason_code == "guard_blocked", f"S9 原因码不符：{r1.reason_code!r}"
        block_reason = r1.turn.block_reason or ""
        assert block_reason, "S9 blocked 轮必须带 block_reason"
        assert "SELECT" not in block_reason.upper(), "S9 block_reason 不得携带被拒 SQL 文本"
        assert len(r1.steps) == 1, f"S9 被拒后不得有后续步，实际 {len(r1.steps)} 步"
        assert r1.steps[0].kind == "blocked" and r1.steps[0].sql is None
        assert r1.attribution is None, "S9 中途被拒不得有综合结果（部分结果不作最终解释）"
        assert quiet.calls == [], "S9 被拒 SQL 不得达执行器"
        assert r1.turn.turns_in_session == 1, f"S9 轮数不多算：{r1.turn.turns_in_session}"
        # blocked 轮后同会话普通 /ask（同身份）→ 会话仍可用且轮数连续（不多算）
        r2 = agent.ask(GOLD102_Q, session_id=sid, identity=identity)
        assert r2.kind == "answer", f"S9 后续 /ask 期望 answer，实际 {r2.kind}"
        assert r2.turns_in_session == 2, f"S9 后续轮数期望 2，实际 {r2.turns_in_session}"
        return {
            "question": ATTRIBUTION_Q,
            "identity_role": "branch_manager",
            "kind": r1.turn.kind,
            "reason_code": r1.reason_code,
            "block_reason": block_reason,
            "steps": len(r1.steps),
            "totals": None,
            "executor_calls": len(quiet.calls),
            "turns_in_session": r1.turn.turns_in_session,
            "followup": {
                "question": GOLD102_Q,
                "kind": r2.kind,
                "turns_in_session": r2.turns_in_session,
            },
        }

    def s10() -> dict[str, Any]:
        # 相对时间拒答（R9 已钉文案，agent/planner.py:375）：漂移口径不进固定快照评测
        require_eligibility_evidence(snapshot_sha)
        quiet = CountingExecutor(execute_sql)
        agent = DataAgent(executor=quiet, budget=budget, snapshot_meta=meta)
        agent.analysis_eligibility = load_eligibility(SemanticModel(), meta)
        r = agent.analyze(RELATIVE_TIME_Q, session_id="e2e-s10")
        assert r.turn.kind == "clarify", f"S10 期望 clarify，实际 {r.turn.kind}"
        assert r.plan is None, "S10 澄清轮不得有计划"
        clar = r.turn.clarification
        assert clar is not None, "S10 澄清轮必须带 clarification"
        assert clar.kind == "relative_time", f"S10 澄清类型不符：{clar.kind}"
        assert list(clar.reasons) == ["不支持相对时间（固定快照评测下会漂移，请使用绝对日期）"], (
            f"S10 澄清理由不符：{list(clar.reasons)}"
        )
        assert quiet.calls == [], "S10 澄清轮不执行 SQL"
        return {
            "question": RELATIVE_TIME_Q,
            "kind": r.turn.kind,
            "clarification_kind": clar.kind,
            "reasons": list(clar.reasons),
            "executor_calls": len(quiet.calls),
        }

    def s11() -> dict[str, Any]:
        # 强制中途 Guard 拒绝（确定性、无身份）：白名单去掉 dim_broker →
        # 步 1-2（fact+dim_date）执行、步 3（分组需 dim_broker）被拒即裁剪
        require_eligibility_evidence(snapshot_sha)
        narrowed = frozenset(t for t in budget.allowed_tables if not t.endswith("dim_broker"))
        assert len(narrowed) < len(budget.allowed_tables), "S11 白名单缩窄未生效"
        quiet = CountingExecutor(execute_sql)
        agent = DataAgent(
            executor=quiet,
            budget=Budget(dialect="doris", max_rows=budget.max_rows, allowed_tables=narrowed),
            snapshot_meta=meta,
        )
        agent.analysis_eligibility = load_eligibility(SemanticModel(), meta)
        r = agent.analyze(ATTRIBUTION_Q, session_id="e2e-s11")
        assert r.turn.kind == "blocked", f"S11 期望 blocked，实际 {r.turn.kind}"
        assert r.reason_code == "guard_blocked", f"S11 原因码不符：{r.reason_code!r}"
        assert len(quiet.calls) == 2, f"S11 应恰执行 2 步 SQL，实际 {len(quiet.calls)}"
        assert [s.kind for s in r.steps] == ["answer", "answer", "blocked"], (
            f"S11 步序应为 2 成功 + 1 被拒，实际 {[s.kind for s in r.steps]}"
        )
        assert r.steps[-1].sql is None, "S11 被拒步不得携带 SQL"
        assert r.attribution is None, "S11 中途被拒不得有综合结果（部分结果不作最终解释）"
        assert r.turn.turns_in_session == 1, f"S11 轮数不多算：{r.turn.turns_in_session}"
        return {
            "question": ATTRIBUTION_Q,
            "kind": r.turn.kind,
            "reason_code": r.reason_code,
            "narrowed_tables": len(budget.allowed_tables) - len(narrowed),
            "step_kinds": [s.kind for s in r.steps],
            "totals": None,
            "executor_calls": len(quiet.calls),
            "turns_in_session": r.turn.turns_in_session,
        }

    def s12() -> dict[str, Any]:
        # 强制综合不可用（确定性）：max_rows=1 → 分组步 group_limit=1 触及 LIMIT →
        # possible_truncation（四步全成才进综合，PRE_GATE 语义见 agent/analysis.py:111-118）
        require_eligibility_evidence(snapshot_sha)
        quiet = CountingExecutor(execute_sql)
        agent = DataAgent(
            executor=quiet,
            budget=Budget(dialect="doris", max_rows=1, allowed_tables=budget.allowed_tables),
            snapshot_meta=meta,
        )
        agent.analysis_eligibility = load_eligibility(SemanticModel(), meta)
        r = agent.analyze(ATTRIBUTION_Q, session_id="e2e-s12")
        assert r.turn.kind == "answer", f"S12 期望 answer，实际 {r.turn.kind}"
        assert len(r.steps) == 4, f"S12 综合期码 ⇔ 恰 4 步，实际 {len(r.steps)}"
        assert analysis_status(r) == "unavailable", f"S12 期望综合不可用，实际 {analysis_status(r)}"
        assert r.reason_code == "possible_truncation", f"S12 原因码不符：{r.reason_code!r}"
        attr = r.attribution
        assert attr is not None and attr.status == "unavailable"
        assert attr.reason_code == "possible_truncation"
        assert attr.baseline is None and attr.current is None and attr.delta is None, (
            "S12 不可用综合不得携带总量（部分结果不作最终解释）"
        )
        assert attr.items == (), "S12 不可用综合不得有贡献项"
        assert r.turn.sql is None and r.turn.rows == (), "S12 父轮不冒充单 SQL 结果"
        assert r.turn.turns_in_session == 1, f"S12 轮数不多算：{r.turn.turns_in_session}"
        return {
            "question": ATTRIBUTION_Q,
            "kind": r.turn.kind,
            "analysis_status": analysis_status(r),
            "reason_code": r.reason_code,
            "steps": len(r.steps),
            "totals": None,
            "items_count": 0,
            "executor_calls": len(quiet.calls),
            "turns_in_session": r.turn.turns_in_session,
        }

    scenarios = [
        run_scenario("S1", "正常提问（确定性链路）", "注册域问句 → answer", s1),
        run_scenario("S2", "反问（歧义不猜）", "歧义问句 → clarify", s2),
        run_scenario("S3", "权限拒绝（Guard 纵深）", "白名单缩窄 → blocked", s3),
        run_scenario("S4", "校验失败修复（多轮）", "反问 → 补口径 → answer", s4),
        run_scenario("S5", "图表 + 解释", "answer → 确定性图表 + 归因", s5),
        run_scenario("S6", "人工接管（handoff）", "0 候选 → 显式转人工", s6),
        run_scenario(
            "S7", "多轮同构追问（ADR-0014 ②）", "2013 口径 → 那 2014 年呢 → 同构 answer", s7
        ),
        run_scenario(
            "S8", "绝对期间贡献分析（不预设涨跌）", "分析问句 → 四步 → 样本 reference 对账", s8
        ),
        run_scenario(
            "S9", "真实受限角色（Guard/RLS）", "branch_manager /analyze → blocked + 会话可用", s9
        ),
        run_scenario("S10", "相对时间拒答", "相对期间 → clarify(relative_time)", s10),
        run_scenario("S11", "强制中途 Guard 拒绝", "白名单缩窄 → 步 3 被拒即裁剪", s11),
        run_scenario("S12", "强制综合不可用", "max_rows=1 → possible_truncation", s12),
    ]
    report = build_report(scenarios, snapshot_sha=snapshot_sha, code_sha=code_sha, created_at=ts)
    if args.report:
        out = validate_report_path(args.report, code_sha)
    else:
        out = REPO / "eval" / "reports" / f"e2e-acceptance-{ts}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_jsonable(report), ensure_ascii=False, indent=2)
    out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    print(f"\n[报告] {out}")
    return 1 if report["summary"]["failed"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except Exception as exc:  # noqa: BLE001 - 验收门禁：任何场景失败都以非 0 退出
        print(f"\n[验收失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
