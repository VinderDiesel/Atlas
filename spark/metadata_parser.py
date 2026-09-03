"""spark/metadata_parser.py：从 SQL/DDL 注释中抽取语义对象候选（Day 26）。

口径与边界（诚实声明）
------------------------
- 本工具产出的是**候选**（candidate），不是已发布定义：候选不得当作
  semantic/ 下的权威指标，人工审核与发布是 Day 27 的独立流程（清单原文）。
- 抽取规则全部确定性实现（无 LLM），候选一律带 evidence（引用注释/语句原文），
  可复现、可绑定 git sha。宁多勿漏：启发式噪声（如把维度数值列 tier 也列为
  measure 候选）由人工审核过滤，抽取器不做强判断。
- 注释经 sqlglot Tokenizer 提取（前置注释挂在首个 token 的 comments 属性，规避
  字符串内 '--' 误判）；结构经 sqlglot AST 提取（表/列/聚合投影）。
- metric（聚合）候选只在 SELECT 出现聚合投影（SUM/COUNT/AVG/MIN/MAX + AS 别名）
  且非窗口函数时生成；明细加工层聚合罕见（25 脚本实测仅 1 处真聚合），零命中不
  代表能力缺失（聚合规则由 tests/test_metadata_parser.py 覆盖）。
- 项目主场景已切金融（ADR-0006），清单原文的"TPC-DS 脚本"在本仓库不存在；
  试跑语料 = sql/dwd 8 张 DWD SQL + data/loader.py 生成的 tpcdi ODS DDL 17 张，
  共 25 个 SQL 脚本（≥10 达标），口径调整记录在 docs/逐日任务清单.md Day 26。

用法：
    uv run python spark/metadata_parser.py                        # 默认语料
    uv run python spark/metadata_parser.py --paths a.sql b.sql    # 指定文件
    uv run python spark/metadata_parser.py --no-loader-ddl        # 不用 loader DDL
    uv run python spark/metadata_parser.py --no-report            # 只打印摘要

产出：eval/reports/metadata-extract-<git sha>.json（每次运行绑定 HEAD）
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlglot import Tokenizer, exp  # noqa: E402
from sqlglot import parse as sqlglot_parse  # noqa: E402

_DIALECT_ORDER = ("doris", "spark", "mysql")
_NUMERIC_TYPES = {
    "INT",
    "INTEGER",
    "BIGINT",
    "LONG",
    "SMALLINT",
    "TINYINT",
    "DECIMAL",
    "NUMERIC",
    "NUMBER",
    "DOUBLE",
    "FLOAT",
    "REAL",
}
# 数值列的业务语义提示词素（列名 lower 后子串命中即标注，tier 等维度数值列
# 会误报——宁多勿漏，由 Day 27 人工审核过滤，见模块 docstring）
_MEASURE_HINTS = (
    "amount",
    "balance",
    "close",
    "commission",
    "count",
    "fee",
    "high",
    "holding",
    "income",
    "low",
    "price",
    "qty",
    "quantity",
    "rate",
    "revenue",
    "tax",
    "tier",
    "value",
    "volume",
)
_HEADER_KEYS = ("功能", "用途", "说明", "重跑", "作者", "维护")


def git_short_sha() -> str:
    """当前 HEAD 短 sha（报告绑定，保证数字可追溯）。"""
    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _clean_identifier(name: str) -> str:
    """去掉 sqlglot 可能保留的引号。"""
    return name.strip().strip('`"[]')


def _sql_name(table: exp.Table) -> str:
    """表 AST → 去引号的 catalog.schema.table 全名。"""
    parts = [
        _clean_identifier(str(part))
        for part in (table.catalog, table.db, table.name)
        if part is not None and str(part) != ""
    ]
    return ".".join(parts)


def _parse_first(sql_text: str, preferred: str | None) -> tuple[list[exp.Expression], str]:
    """多方言依次尝试解析，返回 (语句列表, 命中的方言)；全部失败返回 ([], "unknown")。

    sqlglot 对部分方言语法（如 Spark `USING iceberg`）需对应 dialect 才能还原，
    按 hint → doris → spark → mysql 顺序回退。
    """
    candidates = dict.fromkeys([d for d in (preferred, *_DIALECT_ORDER) if d])
    for dialect in candidates:
        try:
            trees = sqlglot_parse(sql_text, read=dialect)
        except Exception:  # noqa: BLE001 - 方言探测失败属预期，继续试下一个
            continue
        statements = [t for t in trees if t is not None]
        if statements:
            return statements, dialect
    return [], "unknown"


def _leading_comment_lines(sql_text: str, dialect: str) -> list[str]:
    """文件头注释行：sqlglot 把前置注释挂在首个 token 的 comments 属性上。

    Token.comments 已按行序保留原文（无 -- 前缀），且 tokenizer 内部处理了
    字符串/引号边界，不会把字符串里的 '--' 误判为注释。
    """
    try:
        tokens = Tokenizer(dialect=dialect).tokenize(sql_text)
    except Exception:  # noqa: BLE001
        return []
    comments = tokens[0].comments if tokens else None
    return [str(line) for line in comments] if comments else []


def _header_block(comment_lines: list[str]) -> list[dict[str, Any]]:
    """头部注释行 → [{line, text}]（line 为 1 起序号，用于报告定位）。"""
    return [{"line": index + 1, "text": line.strip()} for index, line in enumerate(comment_lines)]


def _header_meta(block: list[dict[str, Any]]) -> dict[str, str]:
    """头部注释块的白名单键提取（功能/重跑/…首行命中）；口径等整块内容原样保留。"""
    meta: dict[str, str] = {}
    for entry in block:
        text = entry["text"]
        match = re.match(r"^([^：:]{1,12})[：:]\s*(.+)$", text)
        if match and match.group(1) in _HEADER_KEYS:
            meta.setdefault(match.group(1), match.group(2).strip())
    return meta


def _measures_from_create(create: exp.Create) -> list[dict[str, Any]]:
    """CREATE TABLE 数值列 → measure 候选（排除 *_id/ID、布尔与字符串列）。"""
    measures: list[dict[str, Any]] = []
    table = create.this
    if isinstance(table, exp.Schema):
        schema, table_node = table, table.this
    else:
        # 防御：非 Schema 形态的 CREATE（如 AS SELECT）无列可抽
        return []
    table_name = _sql_name(table_node)
    for column_def in schema.expressions:
        if not isinstance(column_def, exp.ColumnDef):
            continue
        column = _clean_identifier(str(column_def.name))
        lowered = column.lower()
        # PascalCase（SK_AccountID）与 snake（sk_account_id）两种风格都以 id 结尾；
        # 仅挡 id/flag 结尾，其他数值列（Cash/NetWorth）保留为候选
        if lowered.endswith("id") or lowered.endswith("_flag"):
            continue
        kind = column_def.args.get("kind")
        type_name = ""
        if kind is not None and isinstance(kind.this, exp.DataType.Type):
            type_name = kind.this.name
        if type_name not in _NUMERIC_TYPES:
            continue
        hint = next((h for h in _MEASURE_HINTS if h in lowered), "")
        comments = list(column_def.comments or [])
        measures.append(
            {
                "table": table_name,
                "column": column,
                "type": type_name,
                "hint": hint,
                "evidence": comments[:1],
            }
        )
    return measures


def _aggregate_metrics(tree: exp.Expression, dialect: str) -> list[dict[str, Any]]:
    """SELECT 投影中带别名的聚合函数 → metric 候选（窗口函数剔除）。"""
    metrics: list[dict[str, Any]] = []
    for select in tree.find_all(exp.Select):
        for projection in select.expressions:
            if not isinstance(projection, exp.Alias):
                continue
            func = projection.this
            if not isinstance(func, (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max)):
                continue
            if func.args.get("window") is not None:
                continue  # 窗口函数是分析计算，不是可下推的聚合指标
            alias = _clean_identifier(str(projection.alias))
            metrics.append(
                {
                    "name": alias,
                    "expression": func.sql(dialect),
                    "evidence": f"{func.sql(dialect)} AS {alias}",
                }
            )
    return metrics


def analyze_text(sql_text: str, source: str) -> dict[str, Any]:
    """单个 SQL 文件 → 候选报告（结构抽取 + 头注释元信息）。"""
    dialect_hint = "spark" if "loader" in source else "doris"
    trees, dialect = _parse_first(sql_text, dialect_hint)
    header = _header_block(_leading_comment_lines(sql_text, dialect)) if trees else []
    file_report: dict[str, Any] = {
        "path": source,
        "dialect": dialect,
        "header_meta": _header_meta(header),
        "header_lines": len(header),
        "datasets": [],
        "measures": [],
        "metrics": [],
    }

    metric_names: set[str] = set()
    creates: list[exp.Create] = []
    inserts: list[exp.Insert] = []
    for tree in trees:
        file_report["metrics"].extend(_aggregate_metrics(tree, dialect))
        metric_names.update(m["name"].lower() for m in file_report["metrics"])
        # 只收建表（CREATE DATABASE 等无列可抽，且不是数据集候选）
        creates.extend(c for c in tree.find_all(exp.Create) if c.args.get("kind") == "TABLE")
        inserts.extend(tree.find_all(exp.Insert))
    for create in creates:
        table_ref = create.this.this if isinstance(create.this, exp.Schema) else create.this
        file_report["datasets"].append(
            {
                "name": _sql_name(table_ref),
                "source_tables": [],
                "header_purpose": file_report["header_meta"].get("功能", ""),
            }
        )
        for measure in _measures_from_create(create):
            if measure["column"].lower() in metric_names:
                continue  # 聚合别名列不是基础度量，避免重复入 measure 候选
            file_report["measures"].append(measure)
    # lineage：INSERT 的目标表与 CREATE 数据集同名则关联源表（跨语句，create 树上
    # 找不到 insert，须在整份 SQL 的语句集合上配对）
    for dataset in file_report["datasets"]:
        dataset["source_tables"] = sorted(
            {
                _sql_name(source_table)
                for insert in inserts
                for source_table in insert.expression.find_all(exp.Table)
                if _sql_name(insert.this) == dataset["name"]
            }
        )
    return file_report


def analyze_file(path: Path) -> dict[str, Any]:
    """读取文件后委托 analyze_text（source = 仓库相对路径）。"""
    return analyze_text(path.read_text(encoding="utf-8"), str(path.relative_to(REPO_ROOT)))


def loader_ddl_files() -> list[tuple[str, str]]:
    """data/loader.py --emit-ddl 生成的 17 张 tpcdi ODS DDL → 逐表虚拟文件。

    不落盘（避免污染 sql/ 与生成物目录）；表名/文本来自同一 loader 版本，
    报告可复现。
    """
    from data.loader import emit_ddl

    text = emit_ddl()
    segments = re.split(r"(?m)^(?=-- )", text)
    files: list[tuple[str, str]] = []
    for segment in segments:
        if "CREATE TABLE" not in segment:
            continue
        match = re.search(r"CREATE TABLE\s+(\S+)\s*\(", segment)
        if not match:
            continue
        name = _clean_identifier(match.group(1))
        files.append((f"data/loader.py --emit-ddl #{name}", segment))
    return files


def known_registry() -> dict[str, Any]:
    """语义层已注册对象（atlas_finance.ossie.yaml）→ 候选去重对照集。

    用于标注 known（已注册）与 new_candidate（审核候选池，Day 27 输入），
    防止与既有同名 active 指标冲突（N8）。
    """
    import yaml

    registry: dict[str, Any] = {"datasets": set(), "fields": {}, "metrics": set()}
    path = REPO_ROOT / "semantic" / "ossie" / "atlas_finance.ossie.yaml"
    if not path.exists():
        return registry
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for model in doc.get("semantic_model", []):
        for dataset in model.get("datasets", []):
            name = _clean_identifier(str(dataset.get("name", "")))
            if not name:
                continue
            registry["datasets"].add(name)
            registry["fields"].setdefault(name, set())
            for field in dataset.get("fields", []):
                registry["fields"][name].add(_clean_identifier(str(field.get("name", ""))))
        for metric in model.get("metrics", []):
            registry["metrics"].add(_clean_identifier(str(metric.get("name", ""))))
    return registry


def run(files: list[tuple[str, str]]) -> dict[str, Any]:
    """批量抽取 + known 对照 + 统计，返回报告 dict（可直接 JSON 化）。"""
    registry = known_registry()
    reports = [analyze_text(text, source) for source, text in files]

    def _tag(entry: dict[str, Any]) -> dict[str, Any]:
        """known 标注：dataset 按短名、measure 按 table.column、metric 按短名。

        语义层注册表用短表名（fact_trades），抽取侧带 catalog/schema 前缀
        （atlas.dwd.fact_trades），对照前去掉前缀。
        """
        tagged = dict(entry)
        if "column" in entry:
            table = entry["table"].split(".")[-1]
            tagged["known"] = entry["column"] in registry["fields"].get(table, set())
        else:
            name = entry["name"].split(".")[-1]
            pool = registry["datasets"] if "source_tables" in entry else registry["metrics"]
            tagged["known"] = name in pool
        return tagged

    for report in reports:
        report["datasets"] = [_tag(d) for d in report["datasets"]]
        report["measures"] = [_tag(m) for m in report["measures"]]
        report["metrics"] = [_tag(m) for m in report["metrics"]]

    def _summarize(items: list[dict[str, Any]]) -> dict[str, int]:
        return {"total": len(items), "known": sum(1 for i in items if i["known"])}

    counts = {
        "files": len(reports),
        "header_comments": sum(1 for r in reports if r["header_lines"]),
        "datasets": _summarize([d for r in reports for d in r["datasets"]]),
        "measures": _summarize([m for r in reports for m in r["measures"]]),
        "metrics": _summarize([m for r in reports for m in r["metrics"]]),
    }
    return {
        "sha": git_short_sha(),
        "tool": "spark/metadata_parser.py",
        "corpus": {
            "dwd_sql": 8,
            "tpcdi_ods_ddl": 17,
            "note": "明细加工层聚合罕见：25 脚本仅 1 处真聚合（fact_cash_balances 内层"
            " daily_net），窗口/无别名聚合已剔除（规则由 tests/test_metadata_parser.py 覆盖）",
        },
        "counts": counts,
        "files": reports,
    }


def _print_report(report: dict[str, Any]) -> None:
    c = report["counts"]
    print(f"== 元数据抽取报告（sha {report['sha']}）")
    print(f"文件 {c['files']} 个；含头部注释块 {c['header_comments']} 个")
    print(f"dataset 候选 {c['datasets']['total']}（known {c['datasets']['known']}）")
    print(f"measure 候选 {c['measures']['total']}（known {c['measures']['known']}）")
    print(f"metric 候选 {c['metrics']['total']}（known {c['metrics']['known']}）")
    for file_report in report["files"]:
        meta = file_report["header_meta"]
        purpose = meta.get("功能") or meta.get("用途") or meta.get("说明") or ""
        print(
            f"  {file_report['path']} [{file_report['dialect']}]"
            f" dataset={len(file_report['datasets'])}"
            f" measure={len(file_report['measures'])}"
            f" metric={len(file_report['metrics'])}" + (f" 功能：{purpose}" if purpose else "")
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths", nargs="*", help="指定 SQL 文件（默认 = sql/dwd + loader DDL）")
    parser.add_argument("--no-loader-ddl", action="store_true", help="不并入 loader 生成的 DDL")
    parser.add_argument("--no-report", action="store_true", help="只打印摘要，不写 JSON")
    args = parser.parse_args()

    if args.paths:
        files: list[tuple[str, str]] = []
        for raw in args.paths:
            path = Path(raw)
            files.append((str(path.relative_to(REPO_ROOT)), path.read_text(encoding="utf-8")))
    else:
        files = [
            (str(p.relative_to(REPO_ROOT)), p.read_text(encoding="utf-8"))
            for p in sorted((REPO_ROOT / "sql" / "dwd").glob("*.sql"))
        ]
        if not args.no_loader_ddl:
            files.extend(loader_ddl_files())
    if not files:
        print("[error] 没有可解析的 SQL 文件", file=sys.stderr)
        return 2

    report = run(files)
    _print_report(report)

    if not args.no_report:
        report_dir = REPO_ROOT / "eval" / "reports"
        report_dir.mkdir(exist_ok=True)
        out = report_dir / f"metadata-extract-{report['sha']}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"报告：{out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
