"""Ossie 语义模型 → dbt MetricFlow YAML 导出（Day 53，证明语义层非封闭）。

映射口径（诚实注记，AGENTS.md 第 9 节）
--------------------------------------
Ossie 是 expression-carrying：metric 表达式原子承载（如 SUM(Quantity*TradePrice)）。
dbt MetricFlow 是 measure-centric：measure = 单列 + 聚合，metric = measure 组合。
两者**不是无损互转**，本导出如实分三态：

1. agg（14/20）：单列单聚合（SUM/AVG/COUNT/COUNT(DISTINCT) 单列）→
   measure + agg metric，MetricFlow 原生表达；
2. ratio（3/20）：聚合后除法（SUM(a)/COUNT(b) 或 /COUNT(DISTINCT c)）→
   MetricFlow ratio metric（分子/分母均为导出 measure，聚合后相除语义一致）；
3. unmapped（3/20）：含先乘后加 SUM(a*b)（total_trade_value /
   average_trade_value / commission_rate）→ MetricFlow measure 无法表达
   （需 dbt 模型层物化乘积列），只登记原因不伪造等价物。

产物
----
- exports/dbt_semantic_models.yml：dbt MetricFlow YAML（semantic_models +
  metrics + 逐条映射状态注释）
- exports/metric-export-report.json：机器可读映射报告（逐 metric 状态，
  数字可溯源——本模块不做任何手工数字声明）

验证：tests/test_export_dbt.py（20/20 分类断言 + YAML round-trip）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

from data.identity import git_short_sha

REPO = Path(__file__).resolve().parent.parent
DEFAULT_IN = REPO / "semantic" / "ossie" / "atlas_finance.ossie.yaml"
DEFAULT_OUT = REPO / "exports" / "dbt_semantic_models.yml"
DEFAULT_REPORT = REPO / "exports" / "metric-export-report.json"

_AGG_TO_DBT = {
    "SUM": "sum",
    "AVG": "average",
    "MIN": "min",
    "MAX": "max",
    "COUNT": "count",
    "COUNT_DISTINCT": "count_distinct",
}


@dataclass(frozen=True)
class AggSpec:
    """从 metric 表达式提取出的原子聚合（单列 + 聚合函数）。"""

    dataset: str
    column: str
    agg: str  # SUM / AVG / COUNT / COUNT_DISTINCT …

    @property
    def measure_name(self) -> str:
        return f"{self.dataset}_{self.column}_{_AGG_TO_DBT[self.agg]}"

    def as_measure(self) -> dict[str, Any]:
        return {
            "name": self.measure_name,
            "agg": _AGG_TO_DBT[self.agg],
            "expr": self.column,
        }


@dataclass
class MetricClassification:
    """单条 metric 的导出分类结果。"""

    name: str
    sql: str
    kind: str  # agg / ratio / unmapped
    measure: AggSpec | None = None
    numerator: AggSpec | None = None
    denominator: AggSpec | None = None
    reason: str = ""


@dataclass
class ExportResult:
    """整文件分类结果（供测试断言与报告输出）。"""

    model_name: str
    classifications: list[MetricClassification] = field(default_factory=list)

    def of(self, kind: str) -> list[MetricClassification]:
        return [c for c in self.classifications if c.kind == kind]


def _agg_inner(node: exp.Expression) -> exp.Expression | None:
    """聚合节点的单一内层表达式（COUNT(DISTINCT x) 的 x 在 .expressions[0]）。"""
    inner = node.this
    if isinstance(inner, exp.Distinct):
        exprs = inner.expressions
        if len(exprs) != 1:
            return None  # 多列 DISTINCT 无法表达为单列 measure
        inner = exprs[0]
    # 收窄返回类型：node.this 为 Any，非表达式子类时无法作为单列 measure
    return inner if isinstance(inner, exp.Expression) else None


def _aggregates(node: exp.Expr) -> list[tuple[str, exp.Column | None]]:
    """提取 AST 里所有聚合（保序）：(函数名, 内层单列 | None)。

    注：parse_one / Div.this 的静态类型是基类 Expr，故入参取 Expr。
    """
    out: list[tuple[str, exp.Column | None]] = []
    for a in node.find_all(exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Count):
        if isinstance(a, exp.Sum):
            func = "SUM"
        elif isinstance(a, exp.Avg):
            func = "AVG"
        elif isinstance(a, exp.Min):
            func = "MIN"
        elif isinstance(a, exp.Max):
            func = "MAX"
        elif isinstance(a, exp.Count):
            inner = a.this
            func = "COUNT_DISTINCT" if isinstance(inner, exp.Distinct) else "COUNT"
        else:  # pragma: no cover - find_all 限定以上五类
            continue
        inner = _agg_inner(a)
        out.append((func, inner if isinstance(inner, exp.Column) else None))
    return out


def classify_metric(name: str, sql: str) -> MetricClassification:
    """把一条 Ossie metric 表达式分类为 agg / ratio / unmapped（见模块 docstring）。"""
    ast = sqlglot.parse_one(sql)
    aggs = _aggregates(ast)

    def to_spec(col: exp.Column, func: str) -> AggSpec:
        return AggSpec(dataset=col.table, column=col.name, agg=func)

    # 单层聚合函数包裹（表达式=一个聚合整体）→ agg 或 unmapped（聚合内运算）
    wrapper = ast
    if isinstance(ast, exp.Alias):
        wrapper = ast.this
    if len(aggs) == 1 and not isinstance(wrapper, (exp.Div, exp.Mul, exp.Add, exp.Sub)):
        func, col = aggs[0]
        if col is not None:
            return MetricClassification(name, sql, "agg", measure=to_spec(col, func))
        return MetricClassification(
            name, sql, "unmapped", reason="聚合内为运算/表达式，非单列（需 dbt 模型层物化列）"
        )

    # 聚合后除法（ratio）：分子分母均为单列聚合
    if isinstance(wrapper, exp.Div):
        num_aggs = _aggregates(wrapper.this)
        den_aggs = _aggregates(wrapper.expression)
        if len(num_aggs) == 1 and len(den_aggs) == 1:
            num_col = num_aggs[0][1]
            den_col = den_aggs[0][1]
            if num_col is not None and den_col is not None:
                return MetricClassification(
                    name,
                    sql,
                    "ratio",
                    numerator=to_spec(num_col, num_aggs[0][0]),
                    denominator=to_spec(den_col, den_aggs[0][0]),
                )

    return MetricClassification(
        name,
        sql,
        "unmapped",
        reason="含非单列聚合的组合表达式（先乘后加等），"
        "MetricFlow measure 无法表达，需 dbt 模型层物化",
    )


def _time_dimension_fields(ds: dict[str, Any]) -> list[dict[str, Any]]:
    """时间维度字段（is_time=true，dim 表内）。"""
    out = []
    for f in ds.get("fields", []):
        if f.get("dimension", {}).get("is_time"):
            out.append(f)
    return out


def _render_semantic_model(ds: dict[str, Any]) -> dict[str, Any]:
    """Ossie dataset → dbt semantic_models 条目（categorical dims；时间维度
    粒度语义 Ossie 未声明，不伪造——见导出报告注记）。"""
    name = ds["name"]
    dims = [
        {"name": f["name"], "type": "categorical"}
        for f in ds.get("fields", [])
        if not f.get("dimension", {}).get("is_time") and name.startswith("dim_")
    ]
    block: dict[str, Any] = {
        "name": name,
        "description": str(ds.get("description", "")).strip(),
        "model": f"ref('{name}')",
        "dimensions": dims,
        # measures 由 export_main 依据 AggSpec 填充（只在被 metric 引用的表上生成）
        "measures": [],
    }
    return block


def export_document(doc: dict[str, Any], report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """组装 dbt MetricFlow YAML 文档 + 填充报告（供测试无副作用调用）。"""
    model = doc["semantic_model"][0]
    metrics_raw = model["metrics"]
    result = ExportResult(model_name=model["name"])
    for m in metrics_raw:
        sql = next(
            d["expression"] for d in m["expression"]["dialects"] if d["dialect"] == "ANSI_SQL"
        )
        result.classifications.append(classify_metric(str(m["name"]), sql))

    measures: dict[tuple[str, str, str], AggSpec] = {}
    for c in result.classifications:
        for spec in (c.measure, c.numerator, c.denominator):
            if spec is not None:
                measures[(spec.dataset, spec.column, spec.agg)] = spec

    semantic_blocks = [_render_semantic_model(ds) for ds in model["datasets"]]
    by_name = {b["name"]: b for b in semantic_blocks}
    for (ds_name, _col, _agg), spec in measures.items():
        by_name[ds_name]["measures"].append(spec.as_measure())

    metrics_out: list[dict[str, Any]] = []
    for c in result.classifications:
        if c.kind == "agg":
            assert c.measure is not None
            metrics_out.append(
                {
                    "name": c.name,
                    "description": _metric_description(metrics_raw, c.name),
                    "type": "agg",
                    "type_params": {"measure": c.measure.measure_name},
                }
            )
        elif c.kind == "ratio":
            assert c.numerator is not None and c.denominator is not None
            metrics_out.append(
                {
                    "name": c.name,
                    "description": _metric_description(metrics_raw, c.name),
                    "type": "ratio",
                    "type_params": {
                        "numerator": c.numerator.measure_name,
                        "denominator": c.denominator.measure_name,
                    },
                }
            )
        # unmapped：不生成 metric 块，理由进报告（不伪造等价物）

    report["model_name"] = model["name"]
    report["metric_total"] = len(result.classifications)
    report["mapped"] = {
        "agg": [c.name for c in result.of("agg")],
        "ratio": [c.name for c in result.of("ratio")],
    }
    report["unmapped"] = [
        {"name": c.name, "sql": c.sql, "reason": c.reason} for c in result.of("unmapped")
    ]
    return {"semantic_models": semantic_blocks, "metrics": metrics_out}


def _metric_description(metrics_raw: list[dict[str, Any]], name: str) -> str:
    for m in metrics_raw:
        if m["name"] == name:
            desc = m.get("description")
            return str(desc) if isinstance(desc, str) else ""
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ossie → dbt MetricFlow YAML 导出")
    parser.add_argument("--in", dest="in_path", type=Path, default=DEFAULT_IN)
    parser.add_argument("--out", dest="out_path", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)

    # CLI 可能传相对路径，报告里 relative_to(REPO) 要求绝对路径
    args.in_path = args.in_path.resolve()
    if not args.in_path.is_file():
        print(f"[export] 输入模型不存在：{args.in_path}", file=sys.stderr)
        return 2

    doc = yaml.safe_load(args.in_path.read_text(encoding="utf-8"))
    if not args.out_path.parent.is_dir():
        args.out_path.parent.mkdir(parents=True)
    if not args.report.parent.is_dir():
        args.report.parent.mkdir(parents=True)

    # HEAD 解析的容错留在调用点（ADR-0019 判据 5(d)）：data.identity.git_short_sha
    # 刻意不吞异常——「无 git 环境也要完成导出」是本导出工具的特有需求，若下沉进
    # 共享函数，其余消费方（含评测报告命名）会静默拿到假身份。
    try:
        sha = git_short_sha()
    except Exception:  # 非 git 工作树且未注入 ATLAS_GIT_SHA（如容器内无 .git）
        sha = "unknown"

    report: dict[str, Any] = {
        "ossie_file": str(args.in_path.relative_to(REPO)),
        "sha": sha,
    }
    rendered = export_document(doc, report)

    header = (
        "# 由 semantic/export_dbt.py 生成的 dbt MetricFlow YAML（自动产物，勿手改）\n"
        f"# 源：{report['ossie_file']}（git sha {report['sha']}）\n"
        "# 映射口径（诚实注记）：Ossie 为 expression-carrying，MetricFlow 为\n"
        "# measure-centric——agg/ratio 态为原生表达；unmapped 态（含 SUM(a*b)\n"
        "# 先乘后加）MetricFlow measure 无法表达，需 dbt 模型层物化乘积列，\n"
        "# 逐条理由见 exports/metric-export-report.json。\n"
        "# 时间维度粒度：Ossie 未声明 granularity，本导出不伪造时间维度；\n"
        "# dbt 侧需按目标模型声明（见导出报告注记）。\n"
        f"# 源指标合计 {report['metric_total']}：mapped "
        f"{len(report['mapped']['agg']) + len(report['mapped']['ratio'])}"
        f"（agg {len(report['mapped']['agg'])} + ratio {len(report['mapped']['ratio'])}），"
        f"unmapped {len(report['unmapped'])}。\n"
    )
    args.out_path.write_text(
        header + yaml.safe_dump(rendered, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "[export] "
        f"{len(report['unmapped'])} unmapped / {report['metric_total']} metrics"
        f" -> 报告 {args.report}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
