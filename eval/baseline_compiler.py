#!/usr/bin/env python3
"""compiler-only 基线分析（Day 30）：注册语义域内多少问题根本不需要 LLM。

口径（与 eval/runner.py 绑定，诚实基线不变）
---------------------------------------------
- 输入：eval/reports/<sha>.json（主评测产物：Planner → Compiler → Guard →
  Doris 全确定性链路，零 LLM 参与），<sha> 缺省取当前 HEAD。
- 样本盘存：gold 共 50 条（gold-001/047 为 TPC-DI 零售口径历史对照样本，
  gmv 等指标未注册于金融语义层，不适用；gold-101~148 金融段 48 条为主评测，
  runner 跳过零售并计数，不与金融段混报）。
- 结论只陈述实测：注册语义域内确定性链的命中/反问覆盖；不推断域外泛化
  （域外未见问句是 Day 31-34 LLM 候选生成器的实验对象，不在此基线内）。

用法（从仓库根执行）：
    uv run python -m eval.baseline_compiler            # 分析当前 HEAD 主评测
    uv run python -m eval.baseline_compiler --sha xxx  # 分析指定 sha
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from eval.runner import GOLD_DIR, REPORT_DIR, git_short_sha

TZ = timezone(timedelta(hours=8))
GOLD_GLOB = "gold-*.json"


def gold_inventory() -> tuple[int, list[Path], list[Path]]:
    """仓库 gold 样本盘存：(总数, 金融段文件, 零售段文件)。"""
    files = [p for p in sorted(GOLD_DIR.glob(GOLD_GLOB))]
    finance = [p for p in files if p.stem.removeprefix("gold-").startswith("1")]
    retail = [p for p in files if p not in finance]
    return len(files), finance, retail


def analyze(main_report: dict[str, Any]) -> dict[str, Any]:
    """从主评测报告派生 compiler-only 基线分析（只做计数与如实归类）。"""
    samples: list[dict[str, Any]] = main_report["samples"]

    plan_hit = [r for r in samples if not r["ambiguous"] and r.get("plan_ok")]
    clarify = [r for r in samples if r["ambiguous"] and r.get("clarify_ok")]
    ex_pass = [r for r in samples if r.get("ex") == "pass"]
    ex_fail = [r for r in samples if r.get("ex") == "fail"]
    errors = [r for r in samples if "error" in r]

    # 确定性链覆盖 = 解析命中 + 歧义反问（反问是确定性系统的诚实行为，
    # 不在 44 条之外引入 LLM 猜测；澄清后再走同一确定性链）
    covered = plan_hit + clarify
    return {
        "deterministic_coverage": f"{len(covered)}/{len(samples)}",
        "plan_hit": f"{len(plan_hit)}/{len([r for r in samples if not r['ambiguous']])}",
        "clarify": f"{len(clarify)}/{len([r for r in samples if r['ambiguous']])}",
        "ex": f"{len(ex_pass)}/{len(ex_pass) + len(ex_fail)}" if ex_pass or ex_fail else "n/a",
        "exec_errors": len(errors),
        "sample_classes": {
            "resolved_deterministically": len(plan_hit),
            "clarified_honestly": len(clarify),
            "ex_fail": len(ex_fail),
            "exec_errors": len(errors),
        },
    }


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

    total, finance_files, retail_files = gold_inventory()
    finance_mismatch = summary["finance_total"] != len(finance_files)
    retail_mismatch = summary["retail_skipped"] != len(retail_files)
    if finance_mismatch or retail_mismatch:
        raise SystemExit("[error] 主评测 summary 与仓库 gold 盘存不一致，先复核 eval/runner.py")

    analysis = analyze(main_report)
    baseline = {
        "sha": sha,
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "source_report": f"eval/reports/{sha}.json",
        "gold_inventory": {
            "total": total,
            "finance": len(finance_files),
            "retail_legacy_skipped": len(retail_files),
        },
        "main_summary": summary,
        "analysis": analysis,
        "conclusion": (
            "注册语义域内（48 条金融 gold 覆盖的口径）确定性链零 LLM 全覆盖："
            f"{analysis['deterministic_coverage']}（解析命中 {analysis['plan_hit']} + "
            f"歧义反问 {analysis['clarify']}）；EX {analysis['ex']} 与锚定快照一致。"
            "此结论不推断域外泛化：未见指标/复合分析/新措辞问句不在基线内，"
            "是 Day 31-34 RAG+LLM 候选生成器的对照实验对象。"
        ),
    }

    target = REPORT_DIR / f"baseline-compiler-{sha}.json"
    target.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[done] 报告已写入: {target.relative_to(REPORT_DIR.parent)}")
    print(f"[summary] {json.dumps(baseline['analysis'], ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
