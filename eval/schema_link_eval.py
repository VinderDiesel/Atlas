"""schema linking 评测（Day 29）：44 条可解析 gold 问句上的指标 Recall@K + 表覆盖。

口径（与 eval/retrieval_eval.py 同一诚实基线）
---------------------------------------------
- 查询集：gold-1xx ambiguous=false 44 条（同 retrieval_eval.load_queries）。
- **指标 Recall@K**：SchemaLinker top-K 候选含 expected_metric 的比例（K=1/3/5）。
- **基线对照**：同批 44 条上 Day 24 的 BM25 单路引擎（eval/retrieval_eval bm25）
  的 Recall@1/@5——对照目的：验证"图域粗筛 + 受限打分 + 元数据重排"相对单路
  词法打分的增益/退化（Day 27 语料 15→20 后单路退化，KL#18）。
- **Table Coverage@K**：gold expected_sql 涉及表集合 ⊆ top-K 候选的可达表展开
  并集。诚实声明：库域仅 6 张 dwd 表且 dim 域全连通，覆盖为下界验证（linking
  输出不会编译失败），**无区分度**——区分度由指标 Recall@K 承担。
- 表真值提取：sqlglot 解析 expected_sql 的表节点，去 catalog/库名前缀。
- 报告：eval/reports/schema-link-<sha>.json（绑定 commit sha）。

用法：uv run python -m eval.schema_link_eval [--engine bm25|fuse]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from sqlglot import exp, parse_one

from agent.compiler import SemanticModel
from agent.tools.schema_linker import SchemaLinker
from eval.retrieval_eval import load_queries, run_eval

REPO = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO / "eval" / "reports"


def git_short_sha() -> str:
    """当前 HEAD 短 sha（报告绑定 commit，与 runner/retrieval_eval 同口径）。"""
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


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

    def link_one(q: dict[str, str]) -> dict[str, Any]:
        hot = dict(hot_all)
        hot[q["expected_metric"]] = max(0, hot.get(q["expected_metric"], 0) - 1)
        r = SchemaLinker(model, popularity=hot).link(q["question"], k=5, engine=engine)
        truth = truth_tables(str(q["truth_sql"]))
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
            "covered_top1": set(truth) <= set(r.tables_top1),
            "covered_top3": set(truth) <= set(r.tables_topk),
        }

    # gold 问句 + expected_sql 合并（truth 提取输入）
    gold_by_id: dict[str, dict[str, Any]] = {}
    for path in (REPO / "eval" / "gold").glob("gold-1*.json"):
        sample = json.loads(path.read_text(encoding="utf-8"))
        gold_by_id[str(sample["id"])] = sample
    enriched = []
    for q in queries:
        sample = gold_by_id.get(q["id"], {})
        sql = sample.get("expected_sql") or ""
        if not sql:
            continue
        enriched.append({**q, "truth_sql": sql})
    if len(enriched) != len(queries):
        raise SystemExit("[error] 部分 gold 样本缺 expected_sql，无法提取表真值")

    details = [link_one(q) for q in enriched]
    n = len(details)

    def hit_at(k: int) -> int:
        return sum(1 for d in details if 0 < d["rank"] <= k)

    base_report, _ = run_eval("bm25")  # Day 24 单路基线（同批查询）
    base_metrics = base_report["metrics"]

    fails = [d for d in details if d["rank"] == 0]
    return {
        "task": f"schema-link-day29-{engine}",
        "engine": engine,
        "sha": git_short_sha(),
        "corpus": {"source": "atlas_finance.ossie.yaml", "n_docs": len(model.metrics)},
        "queries": {"source": "eval/gold gold-1xx ambiguous=false + expected_sql", "n": n},
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
            # 下界验证（见模块 docstring：6 表全连通，无区分度）
            "table_coverage_top1": f"{sum(1 for d in details if d['covered_top1'])}/{n}",
            "table_coverage_top3": f"{sum(1 for d in details if d['covered_top3'])}/{n}",
        },
        "note": (
            "指标 Recall@K 是主指标；Table Coverage 为下界验证（库域 6 表 dim 全连通，"
            "无区分度，如实声明）。基线 = 同批 44 条上 Day 24 BM25 单路引擎。"
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
    args = parser.parse_args(argv)
    report = run_linker_eval(args.engine)
    out = REPORTS_DIR / f"schema-link-{args.engine}-{report['sha']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    m = report["metrics"]
    b = report["base_bm25_day24"]
    print(f"报告：{out.relative_to(REPO)}")
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
