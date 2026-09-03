#!/usr/bin/env python3
"""失败驱动的数据飞轮（Day 41）：评测失败 → 归类 → 人工确认 → 合规语料 → 重训。

状态机（每阶段输出计数到 flywheel-state.json，绑定 git sha）
----------------------------------------------------------
  scan    读评测报告（eval/reports/<sha>.json 或 --report），复用
          eval.failure_collect.classify 归类写 eval/failures/<category>/，
          status=pending_review
  review  （人工闸口，红线：AGENTS.md「人工确认后才可进 SFT」）
          人工把 JSON status 改为 approved 并补 sample.answer_plan
          （合法 Plan JSON——形态与 generator 推理同口径，ADR-0008）
  export  收集 approved 样本 → lora/data/approved_pairs.jsonl
          （answer_plan 必须过 agent.generator.validate_plan_json，否则拦下计数）
  build   子进程调 lora.build_pairs（防泄漏过滤 → pairs.jsonl）
  train   子进程调 lora.train（前置检查：语料 min_samples + 依赖 + GPU；
          不满足 → blocked 状态如实记录，不假跑）

用法（从仓库根执行）：
    uv run python -m lora.flywheel                    # 最新评测报告
    uv run python -m lora.flywheel --report eval/reports/<sha>.json
    uv run python -m lora.flywheel --dry              # 只输出状态机，不写文件不跑训练
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from eval.failure_collect import FAILURES_DIR, classify
from eval.runner import git_short_sha

LORA_DIR = Path(__file__).resolve().parent
DATA_DIR = LORA_DIR / "data"
REPORTS_DIR = Path(__file__).resolve().parent.parent / "eval" / "reports"
TZ = timezone(timedelta(hours=8))
STATE_FILE = DATA_DIR / "flywheel-state.json"


def _load_report(report_path: Path) -> dict[str, Any]:
    """读评测报告（runner/rag_eval 同构：samples[] 含 ex/error/refused/plan_ok）。"""
    if not report_path.exists():
        raise SystemExit(f"[error] 报告不存在：{report_path}")
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    return report


def scan(report_path: Path, dry: bool = False) -> dict[str, int]:
    """scan 阶段：失败样本归类 → failures/<category>/（复用 Day 35 工具）。"""
    report = _load_report(report_path)
    sha = str(report.get("sha", report_path.stem))
    samples: list[dict[str, Any]] = report.get("samples", [])
    counts: dict[str, int] = {}
    picked = 0
    for sample in samples:
        # 失败定义与 failure_collect 一致：歧义反问 pass 不算；其余未过 = 失败
        if sample.get("clarify_ok") is True:
            continue
        if sample.get("plan_ok") is True and sample.get("ex") == "pass":
            continue
        category = classify(sample)
        counts[category] = counts.get(category, 0) + 1
        picked += 1
        payload = {
            "sha": sha,
            "source": str(report_path),
            "collected_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "category": category,
            "status": "pending_review",
            "sample": sample,
        }
        if dry:
            continue
        target_dir = FAILURES_DIR / category
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{sha}-{sample.get('id')}.json"
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"[scan] 失败样本 {picked} 条归类（报告 {report_path.name}）：{counts}")
    return counts


def export_approved(dry: bool = False) -> dict[str, int]:
    """export 阶段：人工 approved + 带合法 answer_plan 的样本 → approved_pairs.jsonl。

    红线自动执行：answer_plan 必须过 validate_plan_json（与推理同口径），
    即使人工误标 approved 也会在此闸口拦下（guard_rejected 计数）。
    """
    from agent.compiler import SemanticModel
    from agent.generator import validate_plan_json

    model = SemanticModel()
    exported: list[dict[str, Any]] = []
    skipped_no_answer = 0
    skipped_invalid_plan = 0
    for payload_path in sorted(FAILURES_DIR.glob("*/*.json")):
        payload: dict[str, Any] = json.loads(payload_path.read_text(encoding="utf-8"))
        if payload.get("status") != "approved":
            continue
        sample = payload.get("sample", {})
        q = str(sample.get("question", "")).strip()
        plan_obj = sample.get("answer_plan")
        if not isinstance(plan_obj, dict):
            skipped_no_answer += 1
            print(f"[export] 拦截（approved 但缺 answer_plan）：{payload_path.name}")
            continue
        plan, reason = validate_plan_json(model, plan_obj)
        if plan is None:
            skipped_invalid_plan += 1
            print(f"[export] 拦截（answer_plan 未过校验 {reason}）：{payload_path.name}")
            continue
        exported.append(
            {
                "question": q,
                "answer": json.dumps(plan_obj, ensure_ascii=False),
                "source_sha": str(payload.get("sha", "")),
                "reviewed_by": str(payload.get("reviewed_by", "")),
            }
        )
    if exported and not dry:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        target = DATA_DIR / "approved_pairs.jsonl"
        lines = [json.dumps(r, ensure_ascii=False) for r in exported]
        if target.exists():
            lines = target.read_text(encoding="utf-8").splitlines() + lines
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[export] 导出 {len(exported)} 条 → {target}")
    else:
        print(
            f"[export] 无可导出样本（approved 含合法 answer_plan = {len(exported)}；"
            f"缺 answer_plan={skipped_no_answer}；校验不过={skipped_invalid_plan}）"
        )
    return {
        "exported": len(exported),
        "skipped_no_answer": skipped_no_answer,
        "skipped_invalid_plan": skipped_invalid_plan,
    }


def _run(target: list[str], dry: bool) -> int:
    """子进程执行（保证与 CLI 同版本同路径；dry 模式跳过）。"""
    if dry:
        print(f"[dry] 将执行：{' '.join(target)}")
        return 0
    proc = subprocess.run(target, cwd=LORA_DIR.parent, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="评测报告（默认最新 <sha>.json）")
    parser.add_argument("--dry", action="store_true", help="只输出状态机不落盘/不训练")
    args = parser.parse_args()

    report_path = args.report
    if report_path is None:
        sha = git_short_sha()
        report_path = REPORTS_DIR / f"{sha}.json"

    state: dict[str, Any] = {
        "sha": git_short_sha(),
        "ran_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "report": str(report_path),
        "stages": {},
    }
    # 1) scan（复用 failure_collect 归类逻辑，幂等：同报告重扫覆盖同 id 文件）
    state["stages"]["scan"] = scan(report_path, dry=args.dry)
    # 2) review 闸口（人工）：列出待确认文件路径，人工补 answer_plan + approved
    pending = sorted(
        p.name
        for p in FAILURES_DIR.glob("*/*.json")
        if "pending_review" in p.read_text(encoding="utf-8")
    )
    if pending and not args.dry:
        print("[review] 待人工确认（编辑 status=approved 并补 sample.answer_plan 后重跑）：")
        for name in pending:
            print(f"  - eval/failures/**/{name}")
    # 3) export → 4) build → 5) train（各自前置检查，blocked 不假跑）
    state["stages"]["export"] = export_approved(dry=args.dry)
    state["stages"]["build"] = {
        "exit": _run(["uv", "run", "python", "-m", "lora.build_pairs"], args.dry)
    }
    state["stages"]["train"] = {"exit": _run(["make", "-s", "train"], args.dry)}
    if state["stages"]["train"]["exit"] not in (0, 2):
        print("[train] 异常退出（exit≠0/2），检查训练前置与日志")
    if not args.dry:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"[state] → {STATE_FILE if not args.dry else '(dry，未写盘)'}")
    print(json.dumps(state["stages"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
