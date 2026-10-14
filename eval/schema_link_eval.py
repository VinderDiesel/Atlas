"""schema linking 评测（Day 29）：finance 域可解析 gold 问句上的指标 Recall@K + 表覆盖。

口径（与 eval/retrieval_eval.py 同一诚实基线）
---------------------------------------------
- 查询集：finance 域 gold-1xx 且 ambiguous=false（同 retrieval_eval.load_queries；
  条数随标注演进，以产物 queries.n 为准）。
- **指标 Recall@K**：SchemaLinker top-K 候选含 expected_metric 的比例（K=1/3/5）。
- **基线对照**：同批查询上 Day 24 口径的 BM25 单路引擎（eval/retrieval_eval bm25）
  的 Recall@1/@5——对照目的：验证"图域粗筛 + 受限打分 + 元数据重排"相对单路
  词法打分的增益/退化（Day 27 语料 15→20 后单路退化，KL#18）。
- **Table Coverage@K**：gold expected_sql 涉及表集合 ⊆ top-K 候选的可达表展开
  并集。诚实声明：库域仅 6 张 dwd 表且 dim 域全连通，覆盖为下界验证（linking
  输出不会编译失败），**无区分度**——区分度由指标 Recall@K 承担。
- 表真值提取：sqlglot 解析 expected_sql 的表节点，去 catalog/库名前缀；仅在有
  expected_sql 的子集上计算（比较类样本 expected_sql=null，真链以
  expected_comparison + result_hash 锚定，见 eval/runner.py——该类样本保留在
  主指标全量查询集中，缺真样本 id 显式登记在产物）。
- 报告：eval/reports/schema-link-<engine>-<sha>[-<tag>].json（绑定 commit sha）。
- **概念层 A/B（ADR-0032 ⑥）**：注入 `concept_index`（检索缝术语扩展，M2-T1）——
  报告 `concept_layer` 字段记 "on"/"off"（env `ATLAS_CONCEPT_LAYER` 尊重：off 族 /
  产物缺失 → None 旁路，逐字回退 M0 行为）。`--tag T` 追加文件名后缀（A/B 产物
  隔离；默认不变 → eval/report.py §5 按当前 sha 默认名取数不受影响）。

用法：uv run python -m eval.schema_link_eval [--engine bm25|fuse] [--tag concept]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from sqlglot import exp, parse_one

from agent.compiler import SemanticModel
from agent.concept_index import load_concept_index
from agent.tools.schema_linker import SchemaLinker
from data.identity import git_short_sha
from eval.retrieval_eval import load_queries, run_eval

REPO = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO / "eval" / "reports"


def truth_tables(expected_sql: str) -> list[str]:
    """expected_sql → 涉及表集合（去 catalog/库前缀，如 atlas.dwd.fact_trades → fact_trades）。"""
    tree = parse_one(expected_sql)
    names: list[str] = []
    for table in tree.find_all(exp.Table):
        parts = [p for p in (table.catalog, table.db, table.name) if p]
        name = parts[-1]
        if name not in names:
            names.append(name)
    return names


def run_linker_eval(engine: str = "bm25") -> dict[str, Any]:
    """schema linking 评测（含 BM25 基线对照）。返回报告 dict。"""
    model = SemanticModel()
    queries = load_queries()
    hot_all = Counter(q["expected_metric"] for q in queries)
    # 概念层注入（ADR-0032 ③/⑤）：env 尊重——ATLAS_CONCEPT_LAYER off 族 → None（旁路）。
    # 产物固定名 = 域短名 atlas_finance.concept_index.json（ADR-0032 ④；model.name 为
    # "atlas_finance_analytics"、不匹配产物名）——本评测 finance 域专用，取 loader 默认。
    concept_index = load_concept_index()

    def link_one(q: dict[str, Any]) -> dict[str, Any]:
        hot = dict(hot_all)
        hot[q["expected_metric"]] = max(0, hot.get(q["expected_metric"], 0) - 1)
        r = SchemaLinker(model, popularity=hot, concept_index=concept_index).link(
            q["question"], k=5, engine=engine
        )
        # truth_sql=null（比较类样本）→ truth=None：covered 记 None，与"有真值但
        # 未覆盖"的 False 区分，覆盖率汇总只统计有真值子集（见下方 truth_details）。
        truth = truth_tables(str(q["truth_sql"])) if q["truth_sql"] else None
        return {
            "id": q["id"],
            "question": q["question"],
            "expected_metric": q["expected_metric"],
            "top5": list(r.candidates),
            "dims": list(r.dims),
            "rank": r.candidates.index(q["expected_metric"]) + 1
            if q["expected_metric"] in r.candidates
            else 0,
            "kept": len(r.kept),
            "truth_tables": truth,
            "tables_top1": list(r.tables_top1),
            "covered_top1": None if truth is None else set(truth) <= set(r.tables_top1),
            "covered_top3": None if truth is None else set(truth) <= set(r.tables_topk),
        }

    # gold 问句 + expected_sql 合并（truth 提取输入）；gold 按域子目录
    # （修复记录见 retrieval_eval.load_queries：平铺 glob 曾静默 0 命中）
    gold_by_id: dict[str, dict[str, Any]] = {}
    for path in (REPO / "eval" / "gold" / "finance").glob("gold-1*.json"):
        sample = json.loads(path.read_text(encoding="utf-8"))
        gold_by_id[str(sample["id"])] = sample
    # expected_sql=null 的样本（比较类真链对齐）保留全量参与主指标 Recall@K，
    # 仅从表真值统计中剔除并显式登记。原实现遇缺真即 SystemExit 阻断——与
    # eval/runner.py 对同类样本以 expected_comparison + result_hash 评估的口径
    # 冲突（7 条 gold-172~178 使入口无法出报告），2026-10-14 基线冻结时修复。
    enriched: list[dict[str, Any]] = []
    skipped_truth: list[str] = []
    for q in queries:
        sample = gold_by_id.get(q["id"], {})
        sql = sample.get("expected_sql")
        if not sql:
            skipped_truth.append(q["id"])
        enriched.append({**q, "truth_sql": sql or None})

    details = [link_one(q) for q in enriched]
    n = len(details)
    truth_details = [d for d in details if d["truth_tables"] is not None]
    truth_n = len(truth_details)

    def hit_at(k: int) -> int:
        return sum(1 for d in details if 0 < d["rank"] <= k)

    base_report, _ = run_eval("bm25")  # Day 24 单路基线（同批查询）
    base_metrics = base_report["metrics"]

    fails = [d for d in details if d["rank"] == 0]
    return {
        "task": f"schema-link-day29-{engine}",
        "engine": engine,
        "sha": git_short_sha(),
        "concept_layer": "on" if concept_index is not None else "off",
        "corpus": {"source": "atlas_finance.ossie.yaml", "n_docs": len(model.metrics)},
        "queries": {
            "source": "eval/gold finance gold-1xx ambiguous=false",
            "n": n,
            "truth_tables_n": truth_n,
            "skipped_without_expected_sql": {"n": len(skipped_truth), "ids": skipped_truth},
        },
        "base_bm25_day24": {
            "engine": "bm25 (full-corpus, no rerank, retrieval_eval)",
            "recall_at_1": base_metrics["recall_at_1"],
            "recall_at_5": base_metrics["recall_at_5"],
            "report": f"retrieval-bm25-{base_report['sha']}.json",
        },
        "metrics": {
            "metric_recall_at_1": f"{hit_at(1)}/{n}",
            "metric_recall_at_3": f"{hit_at(3)}/{n}",
            "metric_recall_at_5": f"{hit_at(5)}/{n}",
            "fail_cases": len(fails),
            # 下界验证（见模块 docstring：6 表全连通，无区分度）；仅表真值子集
            "table_coverage_top1": (
                f"{sum(1 for d in truth_details if d['covered_top1'])}/{truth_n}"
            ),
            "table_coverage_top3": (
                f"{sum(1 for d in truth_details if d['covered_top3'])}/{truth_n}"
            ),
        },
        "note": (
            "指标 Recall@K 是主指标（全量查询集）；Table Coverage 为下界验证"
            "（库域 6 表 dim 全连通，无区分度，如实声明）且仅在有 expected_sql "
            "的子集上计算（缺真样本见 queries.skipped_without_expected_sql）。"
            "基线 = 同批全量查询上 Day 24 BM25 单路引擎。"
        ),
        "fail_cases": fails,
        "details": details,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="schema linking 评测（Day 29）")
    parser.add_argument(
        "--engine",
        default="bm25",
        choices=["bm25", "fuse"],
        help="召回引擎（fuse 需 atlas-milvus 在跑）",
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="产物文件名后缀标签（A/B 对照隔离，如 --tag concept；默认无 → 原名不变）",
    )
    args = parser.parse_args(argv)
    report = run_linker_eval(args.engine)
    suffix = f"-{args.tag}" if args.tag else ""
    out = REPORTS_DIR / f"schema-link-{args.engine}-{report['sha']}{suffix}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    m = report["metrics"]
    b = report["base_bm25_day24"]
    print(f"报告：{out.relative_to(REPO)}")
    print(f"  概念层：{report['concept_layer']}（off=旁路基线，逐字回退 M0 行为）")
    print(f"  基线 BM25(全量域无重排)：Recall@1={b['recall_at_1']} @5={b['recall_at_5']}")
    print(
        f"  SchemaLinker({report['engine']})：Recall@1={m['metric_recall_at_1']} "
        f"@3={m['metric_recall_at_3']} @5={m['metric_recall_at_5']} "
        f"表覆盖@1={m['table_coverage_top1']} @3={m['table_coverage_top3']} 失败 {m['fail_cases']}"
    )
    if report["fail_cases"]:
        print("  失败明细（人工分析）:")
        for case in report["fail_cases"]:
            print(
                f"    {case['id']} 期望 {case['expected_metric']} top5={case['top5']} "
                f"dims={case['dims']}"
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
