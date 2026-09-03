#!/usr/bin/env python3
"""四策略对比实验（Day 34）：compiler-only / RAG+LLM / LoRA / LoRA+SC。

口径（诚实基线）
---------------
- 对比表六维：EX / Plan Acc / Token / Latency / Cost / 拒绝率（任务清单验收）。
- compiler-only：读主评测报告 eval/reports/<sha>.json（确定性零 token 零成本）；
  latency 现场实测（--measure-compiler 触发，44 条重跑计时）。
- RAG+LLM：读 eval/reports/rag-llm-<engine>-<sha>.json（stub 报告显式非 LLM；
  openai 报告待端点就绪后生成）。
- LoRA / LoRA+SC：登记 blocked——依赖 GPU 训练 adapter（Day 37-38）与 LLM 端点，
  不编造数字（AGENTS.md N1）。
- 所有策略必须过同一 Guard（安全红线）：LLM 路径 SQL 由确定性 Compiler 生成后
  走 enforce；此处仅汇总，不提供绕过通道。

用法（从仓库根执行）：
    uv run python -m eval.compare_4way --rag-engine stub        # 汇总现有报告
    uv run python -m eval.compare_4way --rag-engine openai      # LLM 报告就绪后
    uv run python -m eval.compare_4way --measure-compiler       # 补测 compiler latency
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent.compiler import Compiler, SemanticModel
from agent.planner import ClarificationRequest, Planner
from agent.security.sql_guard import enforce
from eval.runner import (
    SNAPSHOT_DIR,
    build_budget,
    execute_sql,
    git_short_sha,
    load_gold_finance,
)

TZ = timezone(timedelta(hours=8))
REPORT_DIR = Path(__file__).resolve().parent / "reports"

STRATEGIES = ("compiler-only", "rag-llm", "lora", "lora-sc")


def _load_report(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def _measure_compiler_timing() -> dict[str, Any]:
    """现场计时 compiler-only 全链路（与 runner 同口径，仅补 latency 测量）。"""
    from time import perf_counter

    sha = git_short_sha()
    model = SemanticModel()
    planner = Planner(model)
    compiler = Compiler(model)
    budget = build_budget(
        json.loads((SNAPSHOT_DIR / f"{sha}.meta.json").read_text(encoding="utf-8"))
    )
    latencies: list[float] = []
    for gold in load_gold_finance():
        if gold.get("ambiguous"):
            continue
        started = perf_counter()
        plan = planner.plan(str(gold["question"]))
        if isinstance(plan, ClarificationRequest):
            continue  # 防御：非歧义样本不应反问（44/44 确定性事实，不打断计时）
        sql, _ = compiler.compile(plan)
        guarded, _ = enforce(sql, budget=budget)
        execute_sql(guarded)
        latencies.append((perf_counter() - started) * 1000)
    return {
        "samples": len(latencies),
        "mean_ms": round(sum(latencies) / len(latencies), 1),
        "total_ms": round(sum(latencies), 1),
    }


def build_table(
    sha: str,
    main: dict[str, Any],
    rag: dict[str, Any] | None,
    compiler_timing: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """六维对比表（如实填充；无数据的维度写 n/a 而非编造）。"""
    m = main["summary"]
    rows: list[dict[str, Any]] = [
        {
            "strategy": "compiler-only",
            "status": "measured",
            "ex": m["ex"],
            "plan_acc": m["plan_acc"],
            "clarify": m["clarify"],
            "token_total": 0,  # 确定性链路零 token（设计事实，非估算）
            "latency_mean_ms": compiler_timing["mean_ms"] if compiler_timing else "n/a(未埋点)",
            "cost_usd_est": 0.0,
            "refuse_rate_clear": "0/44",
            "source": f"eval/reports/{sha}.json",
        }
    ]
    if rag is not None:
        s = rag["summary"]
        non_amb = int(s["non_ambiguous"])
        refused = int(s["refused_on_clear"])
        rows.append(
            {
                "strategy": f"rag-llm({rag.get('engine', '?')})",
                "status": "measured(stub)" if rag.get("engine") == "stub" else "measured",
                "ex": s["ex"],
                "plan_acc": s["plan_acc"],
                "clarify": s["clarify"],
                "token_total": int(s["total_tokens"]),
                "latency_mean_ms": s["mean_latency_ms"],
                "cost_usd_est": s["cost_usd_est"],
                "refuse_rate_clear": f"{refused}/{non_amb}",
                "source": f"eval/reports/rag-llm-{rag.get('engine')}-{sha}.json",
            }
        )
    for name in ("lora", "lora-sc"):
        rows.append(
            {
                "strategy": name,
                "status": "blocked",
                "ex": "n/a",
                "plan_acc": "n/a",
                "clarify": "n/a",
                "token_total": "n/a",
                "latency_mean_ms": "n/a",
                "cost_usd_est": "n/a",
                "refuse_rate_clear": "n/a",
                "block_reason": (
                    "需 GPU 训练 sql_v1 adapter（Day 37-38；LLM 端点已就绪）"
                    if name == "lora"
                    else "依赖 LoRA adapter（lora 行未解阻塞前不执行）"
                ),
                "source": None,
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rag-engine", default="stub", choices=["openai", "stub"])
    parser.add_argument("--measure-compiler", action="store_true")
    args = parser.parse_args()

    sha = git_short_sha()
    main_path = REPORT_DIR / f"{sha}.json"
    rag_path = REPORT_DIR / f"rag-llm-{args.rag_engine}-{sha}.json"
    if not main_path.exists():
        raise SystemExit(f"[error] 主评测报告缺失：{main_path}（先 make eval）")
    main = _load_report(main_path)
    rag = _load_report(rag_path) if rag_path.exists() else None
    if rag is None and args.rag_engine == "openai":
        raise SystemExit(
            f"[error] RAG+LLM 报告缺失：{rag_path}\n"
            "（先 make rag-eval ENGINE=openai；需 .env 配置 OPENAI_API_KEY/BASE_URL/MODEL_NAME）"
        )

    compiler_timing = None
    if args.measure_compiler:
        compiler_timing = _measure_compiler_timing()
    table = build_table(sha, main, rag, compiler_timing)

    notes = [
        "六维口径：EX/Plan Acc/歧义反问与 eval/runner.py 一致；token/latency/cost "
        "来自各策略报告；refuse_rate_clear = 非歧义样本中拒绝数（歧义反问不算拒绝）。",
        "compiler-only token=0/cost=0 是确定性链路的设计事实；latency 未埋点前为 "
        "n/a（OTel 全链路 Day 50；可用 --measure-compiler 现场补测）。",
        "LoRA/LoRA+SC 登记 blocked 不编造数字（AGENTS.md N1）；"
        "解锁条件：GPU 训练 adapter（Day 37-38；LLM 端点已就绪）。",
    ]
    if rag is not None and rag.get("engine") == "stub":
        notes.append("stub 引擎报告不代表 LLM 能力，仅供链路对照。")
    report = {
        "sha": sha,
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "strategies": STRATEGIES,
        "table": table,
        "notes": notes,
    }
    target = REPORT_DIR / f"compare-4way-{sha}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[done] 报告已写入: {target.relative_to(REPORT_DIR.parent.parent)}")
    print(json.dumps(table, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
