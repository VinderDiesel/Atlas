#!/usr/bin/env python3
"""EVAL_REPORT.md 自动生成器（Day 39）：机械转述 eval/reports/*.json，无手写数字。

设计
----
- stdout 输出 markdown，Makefile `report:` 重定向到 EVAL_REPORT.md。
- **每个数字都有 source**：表格的 source 列 = 报告文件名；值 = 报告内字段的
  机械转述（不做任何算术/推断）。
- 报告缺失 → 输出占位行「<缺失：文件名（先运行对应 make target）>」，不填数字。
- 当前 sha = git HEAD；只聚合当前 sha 的报告（历史 sha 报告不入汇总，
  防止新旧混报；历史对照在任务清单/README 留痕）。

用法（从仓库根执行）：
    make report        # 等价于 uv run python -m eval.report > EVAL_REPORT.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from eval.runner import git_short_sha
from eval.workbench_baseline import BaselineReport

REPORTS_DIR = Path(__file__).resolve().parent / "reports"
FAILURES_DIR = Path(__file__).resolve().parent / "failures"
TZ = timezone(timedelta(hours=8))

MISSING = "<缺失：报告文件不存在，先运行对应 make target>"

# 主评测报告文件名 = <git sha>.json（纯 sha，无类型前缀）；用其鉴别「主评测」报告，
# 与 baseline-compiler-<sha>.json / rag-llm-*-<sha>.json 等类型化报告区分。
_SHA_RE = re.compile(r"^[0-9a-f]{7,}$")


def _load(name: str) -> dict[str, Any] | None:
    """读报告文件；缺失/损坏返回 None（不静默填数）。"""
    path = REPORTS_DIR / name
    if not path.exists():
        return None
    try:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload


def _field(data: dict[str, Any] | None, name: str, *path: str, default: str = MISSING) -> str:
    """机械取值：data None 或路径缺失 → default（占位，不推断）。"""
    if data is None:
        return default
    cur: Any = data
    for key in (name, *path):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return str(cur)


def _row(label: str, value: str, source: str) -> str:
    """markdown 表格行：label | value | source。"""
    return f"| {label} | {value} | `{source}` |"


def _discover_main_reports() -> list[tuple[Path, str]]:
    """所有主评测报告（文件名 = <sha>.json），按 mtime 倒序（最新在前）。"""
    found: list[tuple[Path, str]] = []
    for p in REPORTS_DIR.glob("*.json"):
        if _SHA_RE.fullmatch(p.stem):
            found.append((p, p.stem))
    found.sort(key=lambda x: x[0].stat().st_mtime, reverse=True)
    return found


def _section_main(sha: str, latest: bool = False) -> list[str]:
    """§1 主评测（compiler-only 基线，make eval；per-domain 分节，P1/P7 目录化后结构）。

    runner 报告 summary 为 {domain: {total/plan_acc/clarify/ex/ex_anchored/
    exec_errors/by_lang}}（金融/零售各自成节不混报）；本表对每域机械转述六键 +
    by_lang zh/en 总数（双语细目见 eval/gold/README.md）。

    聚合（诚实）：当前 sha 无报告时，回退到**最新一次**主评测报告（按 mtime），
    并在表格后标注来源 sha——避免「headline 报告主体为空」的退化（High4）。
    `--latest` 则强制使用最新报告聚合。
    """
    name = f"{sha}.json"
    d = _load(name)
    used_sha = sha
    if d is None:
        recent = _discover_main_reports()
        if recent:
            used_sha = recent[0][1]
            name = f"{used_sha}.json"
            d = _load(name)
    lines = [
        "## 1. 主评测（compiler-only 基线 · `make eval`）",
        "",
        "| 指标 | 值 | source |",
        "|---|---|---|",
    ]
    if d is None:
        lines.append(_row("summary", MISSING, name))
        return lines
    if used_sha != sha:
        lines.append(
            f"> 当前 HEAD `{sha}` 尚无主评测报告，已回退聚合最新一次 `{used_sha}`"
            "（按 mtime 选择；数字不可跨快照混报，见报告 source）。"
        )
        lines.append("")
    summary = d.get("summary", {})
    for domain in ("finance", "retail"):
        lines.append(_row(f"{domain}_total", _field(summary, domain, "total"), name))
        for key in ("plan_acc", "clarify", "ex", "ex_anchored", "exec_errors"):
            lines.append(_row(f"{domain}_{key}", _field(summary, domain, key), name))
        for lang in ("zh", "en"):
            lines.append(
                _row(
                    f"{domain}_{lang}_total",
                    _field(summary, domain, "by_lang", lang, "total"),
                    name,
                )
            )
    lines.append("")
    lines.append(f"**评测脚本**：`eval/runner.py`（{name} `created_at`={_field(d, 'created_at')}）")
    return lines


def _section_trend(top_n: int = 5) -> list[str]:
    """§0 趋势：最近 N 次主评测的 Plan Acc / EX 走势（按 mtime 倒序，不混报）。

    仅转述各历史主报告 summary 的实测字段；无历史则占位。数字均来自
    eval/reports/<sha>.json，不推断。
    """
    reports = _discover_main_reports()[:top_n]
    lines = [
        "## 0. 主评测趋势（最近评测轮次，按 mtime）",
        "",
        "| sha | finance Plan Acc | retail Plan Acc | finance EX | retail EX |",
        "|---|---|---|---|---|",
    ]
    if not reports:
        lines.append(f"| {MISSING} | | | | |")
        return lines
    for _path, sha in reports:
        d = _load(f"{sha}.json")
        if d is None:
            continue
        s = d.get("summary", {})
        lines.append(
            f"| `{sha}` | {_field(s, 'finance', 'plan_acc')} | {_field(s, 'retail', 'plan_acc')} "
            f"| {_field(s, 'finance', 'ex')} | {_field(s, 'retail', 'ex')} |"
        )
    return lines


def _section_baseline(sha: str) -> list[str]:
    """§2 确定性链覆盖分析（make baseline；per-domain 分节，2026-09-05 起）。

    baseline 报告 analysis 为 {domain: {deterministic_coverage/plan_hit/clarify/ex/
    exec_errors}}（与主评测 summary 同口径分域，不混报）；旧平铺结构报告缺域键
    时逐域整组占位，不推断。
    """
    name = f"baseline-compiler-{sha}.json"
    d = _load(name)
    lines = ["## 2. 确定性链覆盖分析（`make baseline`）", ""]
    if d is None:
        lines.append(_row("analysis", MISSING, name))
        return lines
    analysis = d.get("analysis", {})
    for domain in ("finance", "retail"):
        values = [
            _row(f"{domain}_{label}", _field(analysis, domain, label), name)
            for label in ("deterministic_coverage", "plan_hit", "clarify", "ex", "exec_errors")
        ]
        if all(MISSING in v for v in values):
            continue
        lines.append(f"### {domain} 域")
        lines.append("")
        lines.append("| 指标 | 值 | source |")
        lines.append("|---|---|---|")
        lines.extend(values)
        lines.append("")
    lines.append(f"> 结论（转述 `conclusion` 字段）：{_field(d, 'conclusion')}")
    return lines


def _section_compare(sha: str) -> list[str]:
    """§3 四策略对比（make compare，六维口径）。"""
    name = f"compare-4way-{sha}.json"
    d = _load(name)
    lines = ["## 3. 四策略对比（`make compare`）", ""]
    if d is None:
        lines.append(_row("table", MISSING, name))
        return lines
    rows = d.get("table", [])
    lines.append(
        "| 策略 | status | EX | Plan Acc | clarify | token_total | latency_mean_ms | "
        "cost_usd_est | refuse(clear) | source |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    if isinstance(rows, list) and rows:
        for r in rows:
            if not isinstance(r, dict):
                continue
            src = r.get("source")
            src_text = str(src) if isinstance(src, str) and src else name
            lines.append(
                f"| {r.get('strategy', MISSING)} | {r.get('status', MISSING)} | "
                f"{r.get('ex', MISSING)} | {r.get('plan_acc', MISSING)} | "
                f"{r.get('clarify', MISSING)} | {r.get('token_total', MISSING)} | "
                f"{r.get('latency_mean_ms', MISSING)} | {r.get('cost_usd_est', MISSING)} | "
                f"{r.get('refuse_rate_clear', MISSING)} | `{src_text}` |"
            )
    else:
        lines.append(_row("table", MISSING, name))
    notes = d.get("notes", [])
    if isinstance(notes, list) and notes:
        lines.append("")
        lines.append("**口径声明（转述 notes）**：")
        for note in notes:
            lines.append(f"- {note}")
    return lines


def _section_rag(sha: str) -> list[str]:
    """§4 RAG+LLM 生成链路报告（rag-llm-<engine>-<sha>.json，可多个引擎）。"""
    lines = ["## 4. RAG+LLM 生成链路（`make rag-eval ENGINE=<engine>`）", ""]
    found = False
    for path in sorted(REPORTS_DIR.glob(f"rag-llm-*-{sha}.json")):
        name = path.name
        d = _load(name)
        found = True
        lines.append(f"### engine={name.removeprefix('rag-llm-').removesuffix(f'-{sha}.json')}")
        lines.append("")
        lines.append("| 指标 | 值 | source |")
        lines.append("|---|---|---|")
        if d is None:
            lines.append(_row("summary", MISSING, name))
            continue
        s = d.get("summary", {})
        summary: dict[str, Any] = s if isinstance(s, dict) else {}
        for label in (
            "non_ambiguous",
            "ambiguous",
            "plan_acc",
            "clarify",
            "ex",
            "refused_on_clear",
            "exec_errors",
            "total_tokens",
            "mean_latency_ms",
            "cost_usd_est",
        ):
            lines.append(_row(label, str(summary.get(label, MISSING)), name))
        eng = d.get("engine", "?")
        if eng == "stub":
            has_real = bool(REPORTS_DIR.glob(f"rag-llm-*-{sha}.json")) and any(
                p.name != name for p in REPORTS_DIR.glob(f"rag-llm-*-{sha}.json")
            )
            lines.append("")
            lines.append("> stub 引擎 = 确定性假引擎（走 Planner），仅验证评测链路口径，")
            if has_real:
                lines.append("> 不具任何 LLM 能力（真实引擎实测见本段其他 engine 小节）。")
            else:
                lines.append("> 不具任何 LLM 能力；真实引擎实测待端点就绪（.env 配置后重跑）。")
        lines.append("")
    if not found:
        lines.append(_row("summary", MISSING, f"rag-llm-*-{sha}.json"))
    return lines


def _retrieval_cell(metrics: dict[str, Any], key: str, fallback: str = MISSING) -> str:
    """取检索指标字段：schema-link 报告用 metric_* 键，retrieval 用 recall_* 键。"""
    for k in (key, f"metric_{key}"):
        if k in metrics:
            return str(metrics[k])
    return fallback


def _section_retrieval(sha: str) -> list[str]:
    """§5 检索与 schema linking（retrieval-*/schema-link-*）。"""
    lines = ["## 5. 检索与 Schema Linking（`make schema-link` 等）", ""]
    lines.append("| 报告 | 指标 Recall@1 | 指标 Recall@5 | fail_cases | 说明 |")
    lines.append("|---|---|---|---|---|")
    specs: list[tuple[str, str]] = [
        (f"schema-link-bm25-{sha}.json", "schema linking 图域粗筛（Day 29）"),
        (f"retrieval-rerank-{sha}.json", "rerank 主链路（retrieval，Day 24）"),
        (f"retrieval-bm25-{sha}.json", "BM25 全量域单路（对照）"),
        (f"retrieval-fuse-{sha}.json", "双路 fuse（对照）"),
    ]
    for name, desc in specs:
        d = _load(name)
        if d is None:
            lines.append(f"| `{name}` | {MISSING} | | | {desc} |")
            continue
        m = d.get("metrics", {})
        metrics: dict[str, Any] = m if isinstance(m, dict) else {}
        lines.append(
            f"| `{name}` | {_retrieval_cell(metrics, 'recall_at_1')} | "
            f"{_retrieval_cell(metrics, 'recall_at_5')} | "
            f"{metrics.get('fail_cases', MISSING)} | {desc} |"
        )
    return lines


def _section_security(sha: str) -> list[str]:
    """§6 安全与权限验证（p1-chain / rls-verify / polaris-rbac / metrics-verify）。"""
    lines = ["## 6. 安全与权限验证", ""]
    lines.append("| 验证 | 结果 | source |")
    lines.append("|---|---|---|")

    p1 = f"p1-chain-{sha}.json"
    d = _load(p1)
    mg = "?"
    if d is not None:
        g = d.get("malicious_gate", {})
        mg = (
            f"{g.get('blocked', '?')}/{g.get('total', '?')}（all_blocked={g.get('all_blocked')}）"
            if isinstance(g, dict)
            else "?"
        )
    lines.append(_row(f"恶意 SQL 拦截（Guard）{mg}", _field(d, "question"), p1))
    lines.append(_row("P1 五道 gates", str(d.get("gates", MISSING)) if d else MISSING, p1))

    rls = f"rls-verify-{sha}.json"
    d = _load(rls)
    roles_info = MISSING
    if d is not None:
        roles = d.get("roles", {})
        if isinstance(roles, dict) and roles:
            parts = [
                f"{name}:{roles.get('row_count', '?')}" if isinstance(roles, dict) else "?"
                for name, roles in roles.items()
                if isinstance(roles, dict)
            ]
            roles_info = "；".join(parts) if parts else MISSING
    lines.append(_row(f"行级权限（三角色 row_count）{roles_info}", _field(d, "question"), rls))

    pr = f"polaris-rbac-{sha}.json"
    d = _load(pr)
    lines.append(_row("Polaris RBAC（atlas_analyst）", _field(d, "analyst_principal"), pr))

    mv = f"metrics-verify-{sha}.json"
    d = _load(mv)
    lines.append(_row("派生指标全链路 verify", _field(d, "status"), mv))
    return lines


def _section_failures(sha: str) -> list[str]:
    """§7 失败样本归集（程序化计数 eval/failures/）。"""
    lines = ["## 7. 失败样本归集（`eval/failure_collect.py`）", ""]
    lines.append("| category | pending_review | approved | source |")
    lines.append("|---|---|---|---|")
    cats = [p for p in FAILURES_DIR.glob("*") if p.is_dir() and not p.name.startswith("_")]
    if not cats:
        lines.append(
            "| （无失败样本目录——归集机制就绪，样本待 LLM 实测产生） | | | `eval/failures/` |"
        )
        return lines
    for cat in sorted(cats):
        pending = sum(
            1 for f in cat.glob("*.json") if "pending_review" in f.read_text(encoding="utf-8")
        )
        approved = sum(
            1 for f in cat.glob("*.json") if '"approved"' in f.read_text(encoding="utf-8")
        )
        lines.append(f"| {cat.name} | {pending} | {approved} | `eval/failures/{cat.name}/` |")
    return lines


def _section_meta(sha: str) -> list[str]:
    """§8 报告来源完整性：当前 sha 下全部报告清单（文件级可审计）。"""
    lines = ["## 8. 报告来源清单（eval/reports/，绑定 sha）", ""]
    names = sorted(p.name for p in REPORTS_DIR.glob(f"*-{sha}.json")) + [f"{sha}.json"]
    lines.append("```")
    lines.extend(names)
    lines.append("```")
    lines.append("")
    lines.append(
        "> 规则：本文件无手写数字；上表每个值均可在对应 source 文件中机械核对。"
        "缺失报告显示占位符而非推断值（AGENTS.md 9.3）。"
    )
    return lines


def render_workbench_baseline(report: BaselineReport) -> str:
    """将 T01 清单渲染为独立章节；不混入旧 EX 汇总或推断当前质量。

    参数为已校验报告，返回 Markdown；不读取其他报告或访问业务源。
    """
    lines = [
        "# 工作台证据基线（清单，不是效果评测）",
        "",
        f"代码：`{report.code_sha}`；工作树摘要：`{report.worktree_digest}`。",
        "本次未执行业务评测；历史证据仅供追溯，数据指纹未现场复核。",
        "",
        "| 策略 | 状态 |",
        "|---|---|",
        *(f"| {item.strategy} | {item.status} |" for item in report.strategies),
        "",
        "## 历史证据身份",
        "",
        "| source | code_sha | digest |",
        "|---|---|---|",
        *(
            f"| `{item.path}` | `{item.code_sha}` | `{item.digest}` |"
            for item in report.historical_evidence
        ),
        "",
        "## 清单问题",
        "",
        *(f"- `{item.path}`：{item.reason}" for item in report.issues),
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """生成 EVAL_REPORT.md（写 stdout）。

    argv：参数列表（缺省 None = 读 sys.argv[1:]）；测试传入显式空列表以隔离
    跑测器自身的 argv（unittest discover 的 `-s tests` 会被 argparse 拒收）。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--latest",
        action="store_true",
        help="强制聚合最新一次/最近 N 次评测报告（跨 sha，避免 headline 报告为空）",
    )
    parser.add_argument("--workbench-report", type=Path, help="单独渲染 T01 清单，不混入效果汇总")
    args = parser.parse_args(argv)
    if args.workbench_report is not None:
        if args.latest:
            parser.error("--workbench-report 不能与 --latest 混用")
        baseline = BaselineReport.model_validate_json(
            args.workbench_report.read_text(encoding="utf-8")
        )
        sys.stdout.write(render_workbench_baseline(baseline))
        return 0

    sha = git_short_sha()
    lines: list[str] = [
        "# EVAL_REPORT — Atlas 评测汇总（自动生成）",
        "",
        "> 生成器：`eval/report.py`（Day 39）；生成时间："
        f"{datetime.now(TZ).isoformat(timespec='seconds')}（仅时间戳不可复现）",
        f"> 绑定 git sha：`{sha}`；数据快照：`data/snapshots/{sha}.meta.json`",
        "> 规则：**无手写数字**；每格数字 source 列可追溯，缺失显示占位不推断。",
        f"> 聚合模式：{'--latest（跨 sha 取最新报告）' if args.latest else '当前 sha 单轮'}。",
        "",
        "---",
        "",
    ]
    lines.extend(_section_trend())
    lines.append("")
    lines.extend(_section_main(sha, latest=args.latest))
    lines.append("")
    lines.extend(_section_baseline(sha))
    lines.append("")
    lines.extend(_section_compare(sha))
    lines.append("")
    lines.extend(_section_rag(sha))
    lines.append("")
    lines.extend(_section_retrieval(sha))
    lines.append("")
    lines.extend(_section_security(sha))
    lines.append("")
    lines.extend(_section_failures(sha))
    lines.append("")
    lines.extend(_section_meta(sha))
    lines.append("")
    sys.stdout.write("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
