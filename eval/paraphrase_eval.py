#!/usr/bin/env python3
"""同义改写鲁棒性评测（High1：验证 Planner 在注册口径内的泛化能力，暴露真实掉落率）。

为什么存在
----------
主评测（eval/runner.py）只覆盖逐字标注问句；但真实用户不会照抄标注措辞。
本评测用「同一业务意图的多种自然语言改写」喂给确定性 Planner，测量其在
注册同义词范围内的稳定性，以及超出口吻（OOV）时的真实掉落率——这是项目
当前最大的未知风险（AGENTS.md 决策优先级：能力天花板）。

口径（诚实）
----------
- 仅 Planner 层（确定性，不需 Doris）：问句 → Plan，与预期 metric/dimensions/
  time 比对（Plan Acc）。不执行 SQL、不写锚定。
- 每个样本带 intended_metric（人类意图）；当 Planner 输出 ≠ 预期即计为 drop。
- drop_rate = 同意图样本中未通过 Plan Acc 的比例；按 base 分节（同一 base 的
  多改写共享人类意图）。
- 不编造数字：结果全由脚本产出；缺失/未解析样本如实计入 drop。

用法（从仓库根执行，planner-only，无需 DB）：
    uv run python -m eval.paraphrase_eval
    uv run python -m eval.paraphrase_eval --domain finance   # 复用金融语义模型
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent.compiler import Plan, SemanticModel, TimeSpec
from agent.planner import ClarificationRequest, Planner

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLD_DIR = REPO_ROOT / "eval" / "gold" / "paraphrase"
REPORT_DIR = REPO_ROOT / "eval" / "reports"
TZ = timezone(timedelta(hours=8))

MODEL_PATH = REPO_ROOT / "semantic" / "ossie" / "atlas_finance.ossie.yaml"


def time_key(time: TimeSpec | None) -> str | None:
    return None if time is None else str(time.value)


def plan_acc(plan: Plan, gold: dict[str, Any]) -> bool:
    """Plan Acc：metric/dimensions/time 与人工标注一致（同 eval/runner.plan_acc）。"""
    return (
        plan.metric == gold.get("expected_metric")
        and plan.dimensions == tuple(gold.get("expected_dimensions", []))
        and time_key(plan.time) == gold.get("expected_time")
    )


def load_samples() -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(GOLD_DIR.glob("pp-*.json"))]


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """整体 + 按 base 的 Plan Acc / drop 统计（只输出实测计数）。"""
    total = len(results)
    ok = sum(1 for r in results if r["plan_ok"])
    by_base: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "ok": 0})
    for r in results:
        b = r["base"]
        by_base[b]["total"] += 1
        if r["plan_ok"]:
            by_base[b]["ok"] += 1
    base_lines = {
        b: {
            "plan_acc": f"{v['ok']}/{v['total']}",
            "drop_rate": f"{(v['total'] - v['ok']) / v['total']:.2%}",
        }
        for b, v in sorted(by_base.items())
    }
    drop_rate = f"{(total - ok) / total:.2%}" if total else "n/a"
    return {
        "total": total,
        "plan_acc": f"{ok}/{total}",
        "drop_rate": drop_rate,
        "by_base": base_lines,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", default="finance", help="语义模型域（当前仅 finance 改写集）")
    args = parser.parse_args()

    model = SemanticModel(MODEL_PATH)
    planner = Planner(model)
    samples = load_samples()
    if not samples:
        print(f"[empty] {GOLD_DIR} 下无改写样本", file=sys.stderr)
        return 2

    results: list[dict[str, Any]] = []
    for s in samples:
        question = str(s["question"])
        plan = planner.plan(question)
        entry: dict[str, Any] = {
            "id": s["id"],
            "base": s.get("base"),
            "question": question,
            "intended_metric": s.get("intended_metric"),
        }
        if isinstance(plan, ClarificationRequest):
            entry["plan_ok"] = False
            entry["clarify"] = True
            entry["reasons"] = list(plan.reasons)
        else:
            entry["plan_ok"] = plan_acc(plan, s)
            entry["parsed_metric"] = plan.metric
            entry["parsed_dimensions"] = list(plan.dimensions)
            entry["parsed_time"] = time_key(plan.time)
        results.append(entry)

    summary = summarize(results)
    report = {
        "eval": "paraphrase-robustness",
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "domain": args.domain,
        "summary": summary,
        "samples": results,
        "notes": (
            "口径：仅 Planner 层确定性解析；Plan Acc = metric/dimensions/time 与预期一致；"
            "drop_rate = 同意图改写中未通过 Plan Acc 的比例（含 OOV 口吻的真实泛化缺口）；"
            "不执行 SQL、不依赖 Doris；数字由本脚本产出。"
        ),
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = REPORT_DIR / "paraphrase-robustness.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[done] {target}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
