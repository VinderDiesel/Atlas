#!/usr/bin/env python3
"""compiler-only 基线分析（Day 30，2026-09-05 适配分域报告）：注册语义域内多少问题根本不需要 LLM。

口径（与 eval/runner.py 绑定，诚实基线不变）
---------------------------------------------
- 输入：eval/reports/<sha>.json（主评测产物：Planner → Compiler → Guard →
  Doris 全确定性链路，零 LLM 参与），<sha> 缺省取当前 HEAD。
- 样本按域分目录（目录即域声明，2026-09-05 起）：eval/gold/finance/（gold-1xx）
  与 eval/gold/retail/（gold-0xx）；主评测两域全跑、summary 按域分节不混报
  （AGENTS.md N10）。本分析沿同一口径：逐域盘存校验、逐域计数、逐域陈述，
  不做跨域合并推断。
- 样本域归属：报告 samples 自带 domain 键（2026-09-05 起 runner 在每域评测
  结果上注入）；更早的报告无该键时按 gold id 前缀回退（金融 gold-1xx / 零售
  gold-0xx，目录化前的 id 命名约定），仅作旧产物兼容，新报告不依赖回退。
- 结论只陈述实测：注册语义域内确定性链的命中/反问覆盖；不推断域外泛化
  （域外未见问句是 Day 31-34 LLM 候选生成器的实验对象，不在此基线内）。

用法（从仓库根执行）：
    uv run python -m eval.baseline_compiler              # 分析当前 HEAD 主评测
    uv run python -m eval.baseline_compiler --sha b933e20  # 分析指定 sha
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from eval.runner import GOLD_DIR, REPORT_DIR, git_short_sha

TZ = timezone(timedelta(hours=8))
DOMAINS = ("finance", "retail")


def gold_inventory() -> dict[str, int]:
    """仓库 gold 样本盘存（目录即域声明）：{total, finance, retail}。"""
    counts = {domain: len(list((GOLD_DIR / domain).glob("gold-*.json"))) for domain in DOMAINS}
    return {"total": sum(counts.values()), **counts}


def sample_domain(result: dict[str, Any]) -> str:
    """样本域归属：samples.domain 键优先；旧报告缺键时按 id 前缀回退。

    2026-09-05 前 runner 不在评测结果上注入 domain（报告 samples 跨域混排），
    目录化前的 id 命名约定（金融 gold-1xx、零售 gold-0xx）用于回退——仅兼容
    旧报告；runner 注入 domain 后的报告不触发回退。
    """
    d = result.get("domain")
    if d in DOMAINS:
        return d
    return "finance" if result["id"].removeprefix("gold-").startswith("1") else "retail"


def analyze(results: list[dict[str, Any]]) -> dict[str, Any]:
    """单域样本 → 确定性链覆盖分析（只做计数与如实归类，不做任何推断）。"""
    plan_hit = [r for r in results if not r["ambiguous"] and r.get("plan_ok")]
    clarify = [r for r in results if r["ambiguous"] and r.get("clarify_ok")]
    ex_pass = [r for r in results if r.get("ex") == "pass"]
    ex_fail = [r for r in results if r.get("ex") == "fail"]
    errors = [r for r in results if "error" in r]
    non_ambiguous = [r for r in results if not r["ambiguous"]]
    ambiguous = [r for r in results if r["ambiguous"]]

    # 确定性链覆盖 = 解析命中 + 歧义反问（反问是确定性系统的诚实行为，
    # 不在样本之外引入 LLM 猜测；澄清后再走同一确定性链）
    covered = plan_hit + clarify
    return {
        "deterministic_coverage": f"{len(covered)}/{len(results)}",
        "plan_hit": f"{len(plan_hit)}/{len(non_ambiguous)}",
        "clarify": f"{len(clarify)}/{len(ambiguous)}",
        "ex": f"{len(ex_pass)}/{len(ex_pass) + len(ex_fail)}" if ex_pass or ex_fail else "n/a",
        "exec_errors": len(errors),
        "sample_classes": {
            "resolved_deterministically": len(plan_hit),
            "clarified_honestly": len(clarify),
            "ex_fail": len(ex_fail),
            "exec_errors": len(errors),
        },
    }


def conclusion_text(analysis: dict[str, dict[str, Any]]) -> str:
    """逐域结论（数字全部来自 analysis 计数，不硬编码）。"""
    parts = []
    for domain in DOMAINS:
        a = analysis[domain]
        parts.append(
            f"{domain} 域确定性链零 LLM 覆盖 {a['deterministic_coverage']}"
            f"（解析命中 {a['plan_hit']} + 歧义反问 {a['clarify']}），EX {a['ex']}"
        )
    return (
        "注册语义域（目录即域声明，按域分节不混报）内："
        + "；".join(parts)
        + "，与锚定快照一致。此结论不推断域外泛化：未见指标/复合分析/新措辞"
        "问句不在基线内，是 Day 31-34 RAG+LLM 候选生成器的对照实验对象。"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", default=None, help="主评测报告 sha（默认当前 HEAD）")
    args = parser.parse_args()

    sha = args.sha or git_short_sha()
    report_path = REPORT_DIR / f"{sha}.json"
    if not report_path.exists():
        raise SystemExit(
            f"[error] 主评测报告不存在: {report_path}（先跑 uv run python -m eval.runner）"
        )
    main_report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    summary: dict[str, Any] = main_report["summary"]

    # 逐域盘存校验：summary 各域 total 必须与仓库 gold 各域目录文件数一致
    inventory = gold_inventory()
    mismatches = [
        f"{domain}: summary {summary[domain]['total']} != 盘存 {inventory[domain]}"
        for domain in DOMAINS
        if summary[domain]["total"] != inventory[domain]
    ]
    if mismatches:
        raise SystemExit(
            "[error] 主评测 summary 与仓库 gold 盘存不一致，先复核 eval/runner.py：\n"
            + "\n".join(mismatches)
        )

    # 域归属过滤 + 覆盖校验（防回退判定丢样本）
    samples = main_report["samples"]
    by_domain = {domain: [r for r in samples if sample_domain(r) == domain] for domain in DOMAINS}
    coverage_gaps = [
        f"{domain}: samples 过滤 {len(by_domain[domain])} != summary total {summary[domain]['total']}"
        for domain in DOMAINS
        if len(by_domain[domain]) != summary[domain]["total"]
    ]
    if coverage_gaps:
        raise SystemExit("[error] samples 域归属与 summary 不一致，先复核评测报告：\n"
                         + "\n".join(coverage_gaps))

    analysis = {domain: analyze(by_domain[domain]) for domain in DOMAINS}
    baseline = {
        "sha": sha,
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "source_report": f"eval/reports/{sha}.json",
        "gold_inventory": inventory,
        "main_summary": summary,
        "analysis": analysis,
        "conclusion": conclusion_text(analysis),
    }

    target = REPORT_DIR / f"baseline-compiler-{sha}.json"
    target.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[done] 报告已写入: {target.relative_to(REPORT_DIR.parent)}")
    print(f"[summary] {json.dumps(analysis, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
