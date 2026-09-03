#!/usr/bin/env python3
"""RAG+LLM 路由策略评测（Day 31）：与 compiler-only 基线同口径的对照实验。

链路（与 eval/runner.py 口径一致，唯一差异是 Planner → Generator）：
    gold 问句 → [SchemaLinker 检索上下文（确定性 RAG）] → Generator（LLM）
              → Plan 候选（确定性校验兜底，不过即 refuse）
              → Compiler → Guard → Doris 执行 → sha256 比对（EX）
              → eval/reports/rag-llm-<engine>-<sha>.json

评测口径（诚实基线）：
- Plan Acc / 歧义反问 / EX 定义与 eval/runner.py 完全一致（直接复用其函数），
  保证与 compiler-only 基线（48/48）可比——LLM 策略是"替代确定性路由"的对照。
- 歧义样本：Generator 返回 refuse = 反问 pass（与 runner 的 ClarificationRequest
  口径对齐）；给出 Plan = 猜测 = fail。
- 非歧义样本：refuse = fail（确定性路由能答的问题 LLM 拒答 = 退化）。
- token / latency 实测自 usage 与墙钟；cost 为估算（单价假设 CLI 可覆盖，
  报告显式声明，非账单）。
- engine=stub：确定性假引擎（内部走 Planner），仅用于验证评测链路契约——
  数字不代表任何 LLM 能力，报告须显式标注。

用法（从仓库根执行）：
    uv run python -m eval.rag_eval --engine stub    # 链路自检（不耗 token）
    uv run python -m eval.rag_eval --engine openai  # 真实 LLM（读 .env 密钥）
    uv run python -m eval.rag_eval --limit 5        # 冒烟（先小样本验证）
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent.compiler import Compiler, SemanticModel
from agent.generator import Generator
from agent.security.sql_guard import enforce
from eval.runner import (
    SNAPSHOT_DIR,
    build_budget,
    execute_sql,
    git_short_sha,
    load_gold_finance,
    plan_acc,
    result_hash,
)

TZ = timezone(timedelta(hours=8))
REPORT_DIR = Path(__file__).resolve().parent / "reports"
# cost 估算单价假设（USD / 1M tokens，gpt-4o-mini 官价；--price-* 可覆盖）
DEFAULT_PRICE_IN = 0.15
DEFAULT_PRICE_OUT = 0.60
PLACEHOLDER_HASH = "<待执行后填写>"


def estimate_cost(usage: dict[str, int], price_in: float, price_out: float) -> float:
    """token 用量 → 估算成本（USD）。显式声明为估算，非账单。"""
    return (
        usage.get("prompt_tokens", 0) * price_in + usage.get("completion_tokens", 0) * price_out
    ) / 1_000_000


def evaluate_sample(
    generator: Generator,
    compiler: Compiler,
    gold: dict[str, Any],
    budget: Any,
    price_in: float,
    price_out: float,
) -> dict[str, Any]:
    """评测单个样本（口径与 eval.runner.evaluate 对齐）。"""
    question = str(gold["question"])
    out: dict[str, Any] = {
        "id": gold["id"],
        "question": question,
        "ambiguous": bool(gold.get("ambiguous", False)),
    }
    gen = generator.generate(question)
    out["refused"] = gen.refused
    out["latency_ms"] = gen.latency_ms
    out["usage"] = gen.usage
    out["cost_usd_est"] = estimate_cost(gen.usage, price_in, price_out)

    if gen.refused:
        out["refusal_reason"] = gen.refusal.reason if gen.refusal else ""
        # 歧义样本 refuse = 反问 pass；非歧义样本 refuse = 退化 fail
        out["clarify_ok"] = bool(gold.get("ambiguous", False))
        return out

    plan = gen.plan
    assert plan is not None
    out["plan_ok"] = plan_acc(plan, gold)
    if gold.get("ambiguous", False):
        # 歧义样本 LLM 给了 Plan（未反问）= 猜测 = fail
        out["clarify_ok"] = False
        return out

    # 与 runner 相同的执行链：编译 → Guard → Doris → sha256
    try:
        sql, _ = compiler.compile(plan)
        guarded, _ = enforce(sql, budget=budget)
        rows, columns = execute_sql(guarded)
    except Exception as exc:  # noqa: BLE001 - 执行错误记录到报告，不中断整轮
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    out["sql"] = guarded
    out["columns"] = columns
    out["row_count"] = len(rows)
    digest = result_hash(rows)
    stored = gold.get("result_hash")
    if stored in (None, "", PLACEHOLDER_HASH):
        out["ex"] = "no_anchor"  # 未锚定样本（不应出现在已锁评测集）
    elif stored == digest:
        out["ex"] = "pass"
    else:
        out["ex"] = "fail"
        out["expected_hash"] = stored
    return out


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总（只输出实测计数）。"""
    non_amb = [r for r in results if not r["ambiguous"]]
    amb = [r for r in results if r["ambiguous"]]
    plan_ok = sum(1 for r in non_amb if r.get("plan_ok"))
    refused_clear = sum(1 for r in non_amb if r.get("refused"))
    ex_pass = sum(1 for r in non_amb if r.get("ex") == "pass")
    ex_fail = sum(1 for r in non_amb if r.get("ex") == "fail")
    ex_err = sum(1 for r in results if "error" in r)
    clarify_ok = sum(1 for r in amb if r.get("clarify_ok"))
    total_tokens = sum(
        r["usage"].get("prompt_tokens", 0) + r["usage"].get("completion_tokens", 0) for r in results
    )
    latency = [r["latency_ms"] for r in results if r["latency_ms"] > 0]
    cost = sum(float(r.get("cost_usd_est", 0.0)) for r in results)
    return {
        "non_ambiguous": len(non_amb),
        "ambiguous": len(amb),
        "plan_acc": f"{plan_ok}/{len(non_amb)}",
        "refused_on_clear": refused_clear,
        "clarify": f"{clarify_ok}/{len(amb)}",
        "ex": f"{ex_pass}/{ex_pass + ex_fail}" if ex_pass + ex_fail else "n/a",
        "exec_errors": ex_err,
        "total_tokens": total_tokens,
        "mean_latency_ms": round(sum(latency) / len(latency), 1) if latency else 0.0,
        "cost_usd_est": round(cost, 6),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="openai", choices=["openai", "stub"])
    # 模型选择链：--model 构造参数 > OPENAI_MODEL_NAME > 内置默认（agent/generator.py 消费）
    parser.add_argument("--model", default=None, help="模型名（OPENAI_MODEL_NAME / gpt-4o-mini）")
    parser.add_argument("--limit", type=int, default=None, help="只评测前 N 条（冒烟）")
    parser.add_argument("--price-in", type=float, default=DEFAULT_PRICE_IN)
    parser.add_argument("--price-out", type=float, default=DEFAULT_PRICE_OUT)
    args = parser.parse_args()

    sha = git_short_sha()
    model = SemanticModel()
    generator = Generator(model, engine=args.engine, model_name=args.model)
    compiler = Compiler(model)
    snapshot_meta: dict[str, Any] = json.loads(
        (SNAPSHOT_DIR / f"{sha}.meta.json").read_text(encoding="utf-8")
    )
    budget = build_budget(snapshot_meta)

    samples = load_gold_finance()
    if args.limit:
        samples = samples[: args.limit]
    results = [
        evaluate_sample(generator, compiler, g, budget, args.price_in, args.price_out)
        for g in samples
    ]

    report = {
        "sha": sha,
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "engine": args.engine,
        "model": generator.model_name,
        "summary": summarize(results),
        "samples": results,
        "notes": [
            "口径：Plan Acc/EX 定义与 eval/runner.py 一致（compiler-only 基线 48/48 可比）。",
            "cost_usd_est 为估算：tokens × 单价假设"
            f"（in=${args.price_in}/M, out=${args.price_out}/M），非账单。",
        ],
    }
    if args.engine == "stub":
        report["notes"].append(
            "engine=stub：确定性假引擎（内部走 Planner），仅验证评测链路契约，"
            "数字不代表任何 LLM 能力。"
        )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = REPORT_DIR / f"rag-llm-{args.engine}-{sha}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[done] 报告已写入: {target.relative_to(REPORT_DIR.parent.parent)}")
    print(f"[summary] {json.dumps(report['summary'], ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
