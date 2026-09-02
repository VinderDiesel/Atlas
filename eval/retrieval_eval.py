"""指标检索评测：BM25 / Milvus 稀疏向量在 44 条可解析 gold 问句上的
Recall@1/@5（确定性召回，双路独立报告）。

口径（与 eval/runner.py 同一诚实基线）
-------------------------------------
- 语料：atlas_finance.ossie.yaml 的 15 个指标文档（名 + 同义词 + 描述，
  retrieval/metric_docs.py 确定性生成），与 gold 问句同源。
- 查询集：gold-1xx 中 ambiguous=false 的 44 条问句（expected_metric 为正确
  指标）；歧义样本由 Planner 澄清承接，不进检索评测。
- 指标：Recall@1 / Recall@5 = 正确指标出现在 top-1 / top-5 的比例。
- 引擎：--engine bm25（内存 BM25，零依赖）或 --engine milvus（Milvus
  稀疏词法向量，SPARSE_INVERTED_INDEX；需 atlas-milvus 服务在跑）。
- 报告：eval/reports/retrieval-<engine>-<sha>.json（文件名绑定 commit sha）。

已知边界（诚实声明）：查询措辞与语料同源（问句含语义层同义词），
Recall 是"确定性检索在自建口径上全命中"的验证性数字，不代表跨领域泛化。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from agent.compiler import SemanticModel
from retrieval.bm25 import Bm25Index
from retrieval.metric_docs import build_metric_docs

REPO = Path(__file__).resolve().parent.parent
GOLD_DIR = REPO / "eval" / "gold"
REPORTS_DIR = REPO / "eval" / "reports"


def git_short_sha() -> str:
    """当前 HEAD 短 sha（与 runner.py 同口径：报告绑定 commit）。"""
    import subprocess

    return (
        subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
    )


def load_queries() -> list[dict[str, str]]:
    """加载可解析 gold 样本作为查询集（id/question/expected_metric）。"""
    queries: list[dict[str, str]] = []
    for path in sorted(GOLD_DIR.glob("gold-*.json")):
        sample = json.loads(path.read_text(encoding="utf-8"))
        if str(sample["id"]).startswith("gold-1") and not sample.get("ambiguous", False):
            queries.append(
                {
                    "id": str(sample["id"]),
                    "question": str(sample["question"]),
                    "expected_metric": str(sample["expected_metric"]),
                }
            )
    return queries


def run_eval(engine: str = "bm25") -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """单路召回评测（bm25 或 milvus）。返回 (报告 dict, 逐查询明细 list)。

    engine="milvus" 需要 Milvus 服务可用（make up 后 atlas-milvus 健康），
    服务不可达时抛 MilvusUnavailable，由调用方提示降级。
    """
    model = SemanticModel()
    docs = build_metric_docs(model)
    queries = load_queries()
    if engine == "bm25":
        index = Bm25Index()
        for doc in docs:
            index.add(doc.doc_id, doc.text)

        def search(q: str, k: int = 5) -> list[str]:
            return [hit.doc_id for hit in index.search(q, k=k)]

        engine_label = "bm25"
        params: dict[str, Any] = {"k1": 1.5, "b": 0.75, "tokenizer": "cjk-bigram+en-word"}
    elif engine == "fuse":
        # RRF 融合：双路各取 top-20 → 融合 → 取 top-5（Day 23，retrieval/fusion.py）
        from retrieval.fusion import rrf
        from retrieval.milvus_client import MilvusIndex, MilvusUnavailable

        index = Bm25Index()
        for doc in docs:
            index.add(doc.doc_id, doc.text)
        try:
            mv = MilvusIndex()
            mv.rebuild(docs)
        except MilvusUnavailable as exc:
            raise RuntimeError(
                f"Milvus 不可用（{exc}）：fuse 引擎需要双路在跑，请 make up 后重试"
            ) from exc

        def search(q: str, k: int = 5) -> list[str]:
            bm25_top = [hit.doc_id for hit in index.search(q, k=20)]
            mv_top = [hit.doc_id for hit in mv.search(q, k=20)]
            return rrf([bm25_top, mv_top])[:k]

        engine_label = "rrf-bm25+milvus"
        params = {"k": 60, "depth_per_engine": 20, "method": "reciprocal-rank-fusion"}
    else:
        from retrieval.milvus_client import MilvusIndex, MilvusUnavailable

        try:
            mv = MilvusIndex()
            mv.rebuild(docs)
        except MilvusUnavailable as exc:
            raise RuntimeError(
                f"Milvus 不可用（{exc}）：请先 make up 启动 atlas-milvus，或改用 --engine bm25"
            ) from exc

        def search(q: str, k: int = 5) -> list[str]:
            return [hit.doc_id for hit in mv.search(q, k=k)]

        engine_label = "milvus-sparse-lexical"
        params = {"index": "SPARSE_INVERTED_INDEX", "metric": "IP", "weights": "tf"}

    details: list[dict[str, Any]] = []
    fails: list[dict[str, Any]] = []
    for q in queries:
        ids = search(q["question"])
        rank = ids.index(q["expected_metric"]) + 1 if q["expected_metric"] in ids else 0
        details.append({**q, "top5": ids, "rank": rank})
        if rank == 0:
            fails.append({**q, "top5": ids})
    n = len(queries)
    hit1 = sum(1 for d in details if d["rank"] == 1)
    hit5 = sum(1 for d in details if 0 < d["rank"] <= 5)
    report: dict[str, Any] = {
        "task": f"metric-retrieval-{engine_label}",
        "engine": engine_label,
        "sha": git_short_sha(),
        "corpus": {"source": "atlas_finance.ossie.yaml", "n_docs": len(docs)},
        "queries": {"source": "eval/gold gold-1xx ambiguous=false", "n": n},
        "params": params,
        "metrics": {
            "recall_at_1": f"{hit1}/{n}",
            "recall_at_5": f"{hit5}/{n}",
            "fail_cases": len(fails),
        },
        "fail_cases": fails,
    }
    return report, details


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="指标检索评测：BM25 / Milvus 稀疏向量 / RRF 融合（Recall@1/@5）"
    )
    parser.add_argument(
        "--engine",
        default="bm25",
        choices=["bm25", "milvus", "fuse"],
        help="召回引擎（默认 bm25；milvus/fuse 需 Milvus 服务在跑）",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="报告输出路径（默认 eval/reports/retrieval-<engine>-<sha>.json）",
    )
    args = parser.parse_args(argv)
    try:
        report, _details = run_eval(args.engine)
    except RuntimeError as exc:
        print(f"评测失败：{exc}")
        return 2
    suffix = "bm25" if args.engine == "bm25" else ("milvus" if args.engine == "milvus" else "fuse")
    out = Path(args.out) if args.out else REPORTS_DIR / f"retrieval-{suffix}-{report['sha']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    m = report["metrics"]
    print(f"retrieval 报告：{out}")
    print(f"  引擎 {report['engine']}：语料 {report['corpus']['n_docs']} 篇"
          f" / 查询 {report['queries']['n']} 条")
    print(f"  Recall@1 = {m['recall_at_1']}  Recall@5 = {m['recall_at_5']}  失败 {m['fail_cases']}")
    if m["fail_cases"]:
        print("  失败明细（人工确认后决定是否调整语料/分词）：")
        for case in report["fail_cases"]:
            print(f"    {case['id']} 期望 {case['expected_metric']} top5={case['top5']}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
