"""lora.build_pairs 契约测试（Day 36）：SFT pair 构造的防泄漏红线。

覆盖：测试集逐字拒绝 / 模板级拒绝（同义词、数字变体）/ 去重 / 质量过滤
（plan 与 sql 双模式）/ 正常样本保留。全部不依赖 GPU 与 Doris。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.compiler import SemanticModel
from lora.build_pairs import build_pairs, gold_templates, metric_aliases, template_of

MODEL = SemanticModel()
ALIASES = metric_aliases(MODEL)
PROTECTED = gold_templates(MODEL)

ROOT = Path(__file__).resolve().parent.parent

# 合法 Plan JSON（与 agent/generator 的 validate_plan_json 同口径）：answer 形态 =
# 路由候选 Plan，SQL 由确定性 Compiler 生成（架构对齐 Day 31，任务清单已登记）
PLAN_ANSWER = json.dumps(
    {"metric": "commission_revenue", "dimensions": ["Branch"], "time": None, "top_n": None},
    ensure_ascii=False,
)


def _row(question: str, answer: str = PLAN_ANSWER) -> dict[str, object]:
    return {"question": question, "answer": answer}


def test_gold_question_literal_rejected() -> None:
    """红线 1：approved 问句与 gold 问句逐字相同 → 拒绝。"""
    gold_q = json.loads((ROOT / "eval/gold/finance/gold-102.json").read_text(encoding="utf-8"))["question"]
    stats, kept = build_pairs([_row(gold_q)], PROTECTED, ALIASES, MODEL)
    assert stats.leak_rejected == 1
    assert kept == []


def test_synonym_variant_template_rejected() -> None:
    """红线 2：同义词变体命中间模板 → 拒绝（防答案形态泄漏）。"""
    variant = "按分支统计 2013 年手续费收入，列出前 5 名"
    stats, kept = build_pairs([_row(variant)], PROTECTED, ALIASES, MODEL)
    assert stats.leak_rejected == 1
    assert kept == []


def test_numeric_variant_template_rejected() -> None:
    """红线 2：数字变体（2013 → 2014）同模板 → 拒绝。"""
    variant = "按分支统计 2014 年佣金收入，列出前 5 名"
    stats, kept = build_pairs([_row(variant)], PROTECTED, ALIASES, MODEL)
    assert stats.leak_rejected == 1
    assert kept == []


def test_duplicate_question_removed() -> None:
    """去重：同一问句两次 → 保留 1 条。"""
    q = "2012 年各地区佣金收入合计是多少"
    stats, kept = build_pairs([_row(q), _row(q)], PROTECTED, ALIASES, MODEL)
    assert stats.dup_removed == 1
    assert stats.kept == 1


def test_plan_mode_rejects_non_json_answer() -> None:
    """plan 模式：answer 非 JSON（如 DDL 文本）→ 拒绝。"""
    stats, kept = build_pairs(
        [_row("某指标查多少", "CREATE TABLE t (x INT)")], PROTECTED, ALIASES, MODEL
    )
    assert stats.quality_rejected == 1
    assert kept == []


def test_plan_mode_rejects_unregistered_metric() -> None:
    """plan 模式：answer 中 metric 未注册（编造）→ 拒绝（与 generator 同口径）。"""
    bad = json.dumps({"metric": "gmv", "dimensions": [], "time": None, "top_n": None})
    stats, kept = build_pairs([_row("2012 年业绩如何", bad)], PROTECTED, ALIASES, MODEL)
    assert stats.quality_rejected == 1
    assert kept == []


def test_plan_mode_rejects_bad_time() -> None:
    """plan 模式：answer 中 time 粒度非法 → 拒绝。"""
    bad = json.dumps(
        {"metric": "commission_revenue", "time": {"granularity": "week", "value": "1"}}
    )
    stats, kept = build_pairs([_row("上周佣金呢", bad)], PROTECTED, ALIASES, MODEL)
    assert stats.quality_rejected == 1
    assert kept == []


def test_sql_mode_still_supported() -> None:
    """sql 模式（备用形态）：SELECT answer 保留、非 SELECT 拒绝。"""
    q = "2016 年各分支开户数前 3"
    ok, kept = build_pairs([_row(q, "SELECT 1")], PROTECTED, ALIASES, MODEL, mode="sql")
    assert ok.kept == 1
    assert kept[0]["answer"] == "SELECT 1"
    bad, _ = build_pairs([_row(q, "DELETE FROM t")], PROTECTED, ALIASES, MODEL, mode="sql")
    assert bad.quality_rejected == 1
    assert bad.kept == 0


def test_quality_rejects_empty_fields() -> None:
    """质量门槛：空 question/answer → 拒绝。"""
    stats, kept = build_pairs([_row("  "), _row("合法问句", "  ")], PROTECTED, ALIASES, MODEL)
    assert stats.quality_rejected == 2
    assert kept == []


def test_clean_row_kept() -> None:
    """无泄漏新问句（时间数字不同、措辞不同）→ 保留。"""
    q = "2015 年各分公司开户数合计多少"
    stats, kept = build_pairs([_row(q)], PROTECTED, ALIASES, MODEL)
    assert stats.kept == 1
    assert kept[0]["question"] == q


def test_template_of_normalizes_aliases_and_numbers() -> None:
    """模板归一：同义词/数字 → 占位符（顺序稳定，可重复）。"""
    t1 = template_of("2013 年佣金收入", ALIASES)
    t2 = template_of("2014 年手续费收入", ALIASES)
    assert t1 == t2
    assert "<NUM>" in t1
    assert "<METRIC_" in t1
