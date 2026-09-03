#!/usr/bin/env python3
"""评测失败样本自动归类（Day 35，数据飞轮第一步）。

从评测报告（eval/runner.py / eval/rag_eval.py 同构：samples[] 含
ex/error/refused/plan_ok）提取失败样本 → eval/failures/<category>/ 下
status=pending_review 的 JSON，供人工复核（AGENTS.md：人工确认后才可进 SFT）。

初步归类规则（确定性，精确归因留人工）：
- refused 且非歧义标注 → understanding（应答却拒 = 路由失败）
- plan_ok=false → understanding（Plan 与标注不符）
- error 含 CompileError → generation；含 UnsafeQuery/Guard → guard_rejected；
  其余 error → execution_error
- ex=fail（hash 不一致）→ invalid_result（待人工确认口径/数据问题）
- 歧义样本反问 pass 不算失败；歧义样本给 Plan（猜答）→ understanding

用法（从仓库根执行）：
    uv run python -m eval.failure_collect --report eval/reports/<sha>.json
    uv run python -m eval.failure_collect --report ... --dry   # 只打印不写文件
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TZ = timezone(timedelta(hours=8))
FAILURES_DIR = Path(__file__).resolve().parent / "failures"


def classify(sample: dict[str, Any]) -> str:
    """按报告字段确定性归类（category 键见 failures/categories.json）。"""
    if sample.get("ambiguous") and not sample.get("clarify_ok", True):
        return "understanding"  # 歧义样本给了 Plan（猜答）
    if sample.get("refused"):
        return "understanding"  # 应解析样本拒答 = 路由失败
    if sample.get("plan_ok") is False:
        return "understanding"
    error = str(sample.get("error", ""))
    if error:
        if "CompileError" in error:
            return "generation"
        if "UnsafeQuery" in error or "Guard" in error:
            return "guard_rejected"
        return "execution_error"
    if sample.get("ex") == "fail":
        return "invalid_result"
    return "understanding"  # 兜底（不应发生，防静默丢弃）


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, help="评测报告 JSON 路径")
    parser.add_argument("--dry", action="store_true")
    args = parser.parse_args()

    report_path = Path(args.report)
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    sha = str(report.get("sha", report_path.stem))
    samples: list[dict[str, Any]] = report.get("samples", [])

    picked = 0
    for sample in samples:
        # 失败定义：歧义反问 pass 不算；其余未过 = 失败
        if sample.get("clarify_ok") is True:
            continue
        if sample.get("plan_ok") is True and sample.get("ex") == "pass":
            continue
        category = classify(sample)
        picked += 1
        payload = {
            "sha": sha,
            "source": str(report_path),
            "collected_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "category": category,
            "status": "pending_review",
            "sample": sample,
        }
        if args.dry:
            print(f"[dry] {sample.get('id')} → {category}")
            continue
        target_dir = FAILURES_DIR / category
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{sha}-{sample.get('id')}.json"
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        target.write_text(content, encoding="utf-8")
        print(f"[done] {sample.get('id')} → {target.relative_to(FAILURES_DIR.parent.parent)}")
    print(f"[summary] 失败样本 {picked} 条已归类（报告 {report_path.name}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
