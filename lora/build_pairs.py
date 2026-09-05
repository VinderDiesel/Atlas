#!/usr/bin/env python3
"""SFT pair 构造器（Day 36）：approved 失败样本 → 去重/质量过滤/防泄漏 → pairs.jsonl。

红线（任务清单 Day 36，AGENTS.md 可复现与诚实优先）
---------------------------------------------------
1. **测试集样本不得进训练集**：gold 问句（eval/gold/）是评测集，任何 pair 的
   question 与 gold 问句逐字相同 → 拒绝。
2. **按问句模板切分，防泄漏**：pair 问句模板（指标同义词 → <METRIC>、数字 →
   <NUM> 归一）与任一 gold 问句模板相同 → 拒绝并记录（同模板衍生问句会在
   评测时泄漏答案形态）。
3. 去重（question 精确去重）+ 质量过滤（question/answer 非空、长度上限、
   answer 形态按 --mode 校验）。

answer 形态（架构对齐 Day 31 Generator：LLM/LoRA 只做 Plan 候选，SQL 由确定性
Compiler 生成）
----------------------------------------------------------------------
- mode=plan（默认）：answer = 合法 Plan JSON，校验复用 agent/generator.py 的
  validate_plan_json——训练目标与推理输出口径一致（metric 注册/维度存在/时间
  格式/top_n 边界），拒绝「训练语料合法但推理永远拒」的错位。
- mode=sql：answer 必须能被 sqlglot 解析为 SELECT（备用形态，当前无消费方；
  任务清单 Day 36 的 question → SQL 表述为早期遗留，已由本架构对齐取代并登记）。

数据源（诚实声明）
------------------
- 当前唯一合规来源 = eval/failures/ 下人工确认 approved 的样本（status 由人工
  从 pending_review 改为 approved 后导出 approved_pairs.jsonl）。
- gold 48 条与 ETL SQL 均不可作训练源（前者是评测集、后者非问答对）——
  当前语料为空是设计结论，不是缺陷；待 LLM 实测产生失败样本后由飞轮驱动。

用法（从仓库根执行）：
    uv run python -m lora.build_pairs --approved lora/data/approved_pairs.jsonl
    输出 lora/data/pairs.jsonl + 报告（去重/质量过滤/泄漏拒绝计数）
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot

from agent.compiler import Plan, SemanticModel
from agent.generator import validate_plan_json
from agent.planner import Planner
from eval.runner import GOLD_DIR, git_short_sha

LORA_DIR = Path(__file__).resolve().parent
DATA_DIR = LORA_DIR / "data"

_NUM_RE = re.compile(r"\d+")


def metric_aliases(model: SemanticModel) -> dict[str, str]:
    """指标同义词 → <METRIC_1> 等统一占位（模板归一用，顺序稳定）。"""
    aliases: dict[str, str] = {}
    for idx, syns in enumerate(model.metric_synonyms.values(), start=1):
        for syn in syns:
            aliases[syn] = f"<METRIC_{idx}>"
    return aliases


def template_of(question: str, aliases: dict[str, str]) -> str:
    """问句 → 模板：同义词/数字归一，供模板级泄漏检测。"""
    text = question
    for syn, placeholder in aliases.items():
        text = text.replace(syn, placeholder)
    text = _NUM_RE.sub("<NUM>", text)
    return text


def gold_templates(model: SemanticModel) -> set[str]:
    """gold 评测问句的模板集合（含逐字原文，双层防泄漏）。"""
    aliases = metric_aliases(model)
    texts = set()
    # 目录化后跨 finance/ retail/ 域子目录（测试集逐字/模板双层防泄漏）
    for path in GOLD_DIR.glob("*/gold-*.json"):
        sample: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        q = str(sample.get("question", ""))
        texts.add(q)
        texts.add(template_of(q, aliases))
    return texts


@dataclass
class PairStats:
    total: int = 0
    dup_removed: int = 0
    leak_rejected: int = 0
    quality_rejected: int = 0
    kept: int = 0
    leak_examples: list[str] = field(default_factory=list)


def quality_ok(question: str, answer: str, model: SemanticModel, mode: str) -> bool:
    """质量门槛：非空、长度上限、answer 形态按 mode 校验。

    mode=plan：validate_plan_json 通过（与 Generator 推理入口同口径）；
    mode=sql：answer 可被 sqlglot 解析为只读 SELECT。
    """
    if not question.strip() or not answer.strip():
        return False
    if len(question) > 300 or len(answer) > 3000:
        return False
    if mode == "plan":
        try:
            obj = json.loads(answer)
        except json.JSONDecodeError:
            return False
        if not isinstance(obj, dict):
            return False
        plan, _reason = validate_plan_json(model, obj)
        return plan is not None
    try:
        ast = sqlglot.parse_one(answer)
    except Exception:  # noqa: BLE001 - 解析失败 = 质量不过
        return False
    return ast is not None and isinstance(ast, sqlglot.exp.Select)


def build_pairs(
    approved_rows: list[dict[str, Any]],
    protected: set[str],
    aliases: dict[str, str],
    model: SemanticModel,
    mode: str = "plan",
) -> tuple[PairStats, list[dict[str, Any]]]:
    """过滤并去重 approved 行 → 统计（不直接写文件，由 main 落盘）。"""
    stats = PairStats()
    seen: set[str] = set()
    kept_rows: list[dict[str, Any]] = []
    for row in approved_rows:
        stats.total += 1
        q = str(row.get("question", "")).strip()
        a = str(row.get("answer", "")).strip()
        if q in seen:
            stats.dup_removed += 1
            continue
        seen.add(q)
        if q in protected or template_of(q, aliases) in protected:
            stats.leak_rejected += 1
            if len(stats.leak_examples) < 5:
                stats.leak_examples.append(q)
            continue
        if not quality_ok(q, a, model, mode):
            stats.quality_rejected += 1
            continue
        kept_rows.append({"question": q, "answer": a})
    stats.kept = len(kept_rows)
    return stats, kept_rows


def plan_to_generator_json(plan: Plan) -> dict[str, Any]:
    """确定性 Plan → Generator 训练目标 JSON（与 agent/generator.validate_plan_json 同口径）。

    只保留 metric / dimensions / time / top_n（Generator 不消费 filters/order_by；
    top_n 由 limit<100 还原，与 Planner._parse_top_n 口径一致）。
    """
    obj: dict[str, Any] = {
        "metric": plan.metric,
        "dimensions": list(plan.dimensions),
    }
    if plan.time is not None:
        obj["time"] = {"granularity": plan.time.granularity, "value": plan.time.value}
    # 前 N 名 → limit（Planner 仅在此情形设 order_by 且 limit = N）
    if plan.order_by and plan.limit < 100:
        obj["top_n"] = plan.limit
    return obj


def build_distillation_pairs(
    model: SemanticModel,
    protected: set[str],
    aliases: dict[str, str],
) -> tuple[PairStats, list[dict[str, Any]]]:
    """蒸馏语料构造（High2：用确定性编译器作 teacher，去空语料，不碰 gold 标签）。

    对每条注册指标同义词，套用若干问句模板（年/季度/维度分组）生成自然语言问句，
    经 Planner 解析为 Plan，再规约为 Generator 训练目标 JSON（answer），并用
    validate_plan_json 校验（与推理同口径）。问句若命中 gold 模板 → 拒绝（双层防泄漏）。

    为什么合法：训练目标是编译器在**注册口径**内的确定性产物，gold 评测问句与答案
    均不参与；这是「教师=确定性编译器」的自蒸馏，而非用 gold 当标签（KL #18/#19 红线）。
    """
    planner = Planner(model)
    stats = PairStats()
    seen: set[str] = set()
    kept_rows: list[dict[str, Any]] = []
    # 取一个维度同义词作分组变体（存在时）
    dim_syns = model.dimension_synonyms
    dim_variant = next(iter(dim_syns.values()))[0] if dim_syns else None

    templates: list[str] = []
    for _name, syns in model.metric_synonyms.items():
        for syn in syns:
            templates.append(f"{syn} 2013 年")
            templates.append(f"2013 年{syn}是多少")
            templates.append(f"2013 年第二季度{syn}")
            if dim_variant:
                templates.append(f"按{dim_variant}统计 2013 年{syn}")

    for q in templates:
        stats.total += 1
        if q in seen:
            stats.dup_removed += 1
            continue
        seen.add(q)
        if q in protected or template_of(q, aliases) in protected:
            stats.leak_rejected += 1
            if len(stats.leak_examples) < 5:
                stats.leak_examples.append(q)
            continue
        plan = planner.plan(q)
        if not isinstance(plan, Plan):
            stats.quality_rejected += 1  # 解析为澄清/未命中 → 不入训练
            continue
        obj = plan_to_generator_json(plan)
        validated, reason = validate_plan_json(model, obj)
        if validated is None:
            stats.quality_rejected += 1
            continue
        answer = json.dumps(obj, ensure_ascii=False)
        if not quality_ok(q, answer, model, mode="plan"):
            stats.quality_rejected += 1
            continue
        kept_rows.append({"question": q, "answer": answer})
    stats.kept = len(kept_rows)
    return stats, kept_rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved", type=Path, default=DATA_DIR / "approved_pairs.jsonl")
    parser.add_argument(
        "--mode",
        choices=("plan", "sql"),
        default="plan",
        help="answer 形态校验：plan（默认，与 generator 同口径）/ sql",
    )
    parser.add_argument(
        "--distill",
        action="store_true",
        help="蒸馏模式：用确定性编译器（Planner→Plan）生成 SFT 语料，不依赖 approved 样本",
    )
    args = parser.parse_args()

    model = SemanticModel()
    protected = gold_templates(model)
    aliases = metric_aliases(model)

    if args.distill:
        stats, kept = build_distillation_pairs(model, protected, aliases)
    else:
        approved_rows: list[dict[str, Any]] = []
        if args.approved.exists():
            for line in args.approved.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    approved_rows.append(json.loads(line))
        stats, kept = build_pairs(approved_rows, protected, aliases, model, mode=args.mode)

    sha = git_short_sha()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if kept:
        target = DATA_DIR / "pairs.jsonl"
        target.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept),
            encoding="utf-8",
        )
    report = {
        "sha": sha,
        "source": "distill(teacher=compiler)" if args.distill else str(args.approved),
        "gold_protected": len(protected),
        "stats": {
            "total": stats.total,
            "dup_removed": stats.dup_removed,
            "leak_rejected": stats.leak_rejected,
            "quality_rejected": stats.quality_rejected,
            "kept": stats.kept,
        },
        "leak_examples": stats.leak_examples,
        "notes": (
            "蒸馏模式：训练目标 = 确定性编译器在注册口径内的 Plan 产物，gold 不参与"
            "（KL #18/#19 红线不触碰）；非 approved 飞轮样本，作为 LoRA 预训练冷启动语料。"
            if args.distill
            else "数据源策略：approved 样本人工确认后才可进训练（AGENTS.md）；"
            "gold 评测问句与同模板衍生问句一律拒绝（红线）；"
            f"mode={args.mode} 形态校验（plan 与 agent/generator 推理同口径）；"
            "当前无 approved 样本 → 空语料是设计结论，待失败样本飞轮驱动（Day 41）。"
        ),
    }
    if kept:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        out = DATA_DIR / f"pairs-{sha}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[done] 语料 {stats.kept} 条 → {DATA_DIR / 'pairs.jsonl'}；报告 {out}")
    else:
        print(f"[empty] 合规语料为空（输入 {stats.total} 条，0 通过过滤）")
        print(f"[stats] {json.dumps(report['stats'], ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
