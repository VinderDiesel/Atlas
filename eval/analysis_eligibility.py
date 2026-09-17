"""ADR-0026 T02：可加组合资格 —— 连接质量证明与快照绑定证据。

职责（ADR-0026「登记可加组合并建立快照资格证据」）：
1. **组合分类**（evaluate_combo）：指标 × 维度组合必须是封闭注册表
   （SemanticModel.attribution_dimensions）内登记的条目，且指标表达式是
   期间内可加的事件型 SUM（可含行内乘法）或非 DISTINCT COUNT——比率、
   均值、DISTINCT、窗口结果、跨事实表表达式一律拒绝；期末余额类指标
   因不在注册表而天然拒绝（放开须先裁定分解数学，见 ADR-0026 末段）。
2. **连接质量证明**（verify_eligibility）：用 sqlglot AST 构造检查 SQL
   （重复目标键 / 孤儿引用 / 行不变量），带覆盖锁定快照全期的显式绝对
   时间谓词（不依赖 Guard 默认时间窗回退），逐条 enforce 后执行；
   覆盖不全（join 不可达、策略表不可达、时间范围非法）一律不签发
   eligible。权限追加 JOIN 的策略条件表必须进入检查范围。
3. **证据绑定**（record_eligibility / load_eligibility）：合格报告落盘
   data/snapshots/<snapshot_sha>.analysis.json（机器生成，永不手写）；
   读取按 (snapshot_sha, semantic_sha256) 双重绑定校验，缺失 / 过期 /
   哈希漂移返回不可用事实，不阻断普通 Agent 构造。
4. **资格 CLI**（main）：显式 --snapshot-sha，先复核锁定数据（meta 文件
   名与内容 sha 一致），复核失败非零退出且不写入任何合格状态。

参考 ADR：infra/adr/0026-multi-step-task-planning.md（T02）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

from agent.compiler import CompileError, Compiler, Relationship, SemanticModel
from agent.security import sql_guard
from agent.security.sql_guard import Budget
from data import identity
from semantic.governance_validate import POLICY_PATH, load_policies_by_name

# 假执行器与 eval.runner.execute_sql 同契约：SQL → (rows, columns)
Executor = Callable[[str], tuple[list[tuple[Any, ...]], list[str]]]

# 策略条件里的表限定 token（dim_customer.tier）；排除 Jinja user 上下文
_TABLE_TOKEN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*[A-Za-z_][A-Za-z0-9_]*")

# 可加闭包的内层节点白名单：行内乘法与括号/取负/字面量/列（含列内标识符）
# （ADR-0026 119-123）
_COMBO_INNER_NODES = (
    exp.Column,
    exp.Identifier,
    exp.Literal,
    exp.Mul,
    exp.Paren,
    exp.Neg,
    exp.Cast,
    exp.Star,
)


# ---------------------------------------------------------------------------
# 组合分类（封闭注册表 + 表达式结构裁定）
# ---------------------------------------------------------------------------


def evaluate_combo(model: SemanticModel, metric: str, dimension: str) -> dict[str, Any]:
    """裁定单个 指标 × 维度 组合是否为可加组合（不做任何 SQL 执行）。

    返回 {"metric", "dimension", "eligible", "reason"}；reason=None 表示
    结构上是可加组合（是否最终 eligible 由 verify_eligibility 的连接质量
    证明决定）。裁定顺序即语义：注册表 → 维度存在性 → 时间字段 → 解析
    → 窗口 → 均值 → DISTINCT → 比率 → 引用 → 跨事实 → 可加闭包。
    """
    pair: dict[str, Any] = {
        "metric": metric,
        "dimension": dimension,
        "eligible": False,
        "reason": None,
    }

    def reject(reason: str) -> dict[str, Any]:
        pair["reason"] = reason
        return pair

    registered = model.attribution_dimensions.get(metric)
    if registered is None or dimension not in registered:
        # 维度本身不存在于模型 → unknown_dimension（先于未登记判定，给出根因）；
        # 否则（含余额类指标不在注册表）= 不可做归因分析（封闭注册表）
        if model.find_field(dimension) is None:
            return reject("unknown_dimension")
        return reject("undeclared_combination")
    found = model.find_field(dimension)
    if found is None:
        return reject("unknown_dimension")
    if found[1].is_time:
        return reject("time_dimension_field")
    expression = model.metrics.get(metric)
    if expression is None:
        return reject("unknown_metric")
    try:
        ast = sqlglot.parse_one(expression)
    except sqlglot.errors.ParseError as exc:
        pair["reason"] = f"unparseable_expression: {exc}"
        return pair
    if ast.find(exp.Window) is not None:
        return reject("window_function")
    if ast.find(exp.Avg) is not None:
        return reject("average_aggregate")
    if ast.find(exp.Distinct) is not None:
        return reject("distinct_aggregate")
    if ast.find(exp.Div) is not None:
        return reject("ratio_expression")
    compiler = Compiler(model)
    try:
        compiler._resolve_refs(ast)
    except CompileError:
        pair["reason"] = "unknown_field"
        return pair
    tables = {c.table for c in ast.find_all(exp.Column) if c.table}
    fact_tables = {t for t in tables if t in model.datasets and not t.startswith("dim_")}
    if len(fact_tables) >= 2:
        return reject("cross_fact_expression")
    top = ast
    while isinstance(top, exp.Paren):
        top = top.this
    if not isinstance(top, (exp.Sum, exp.Count)):
        return reject("unsupported_expression")
    inner = top.this
    if inner is not None:
        for node in inner.walk():
            if not isinstance(node, _COMBO_INNER_NODES):
                return reject("unsupported_expression")
    pair["eligible"] = True
    return pair


# ---------------------------------------------------------------------------
# 连接质量证明（AST 构造 → Guard enforce → 执行）
# ---------------------------------------------------------------------------


def _fact_dataset(model: SemanticModel, metric: str) -> str | None:
    """指标表达式引用的事实表（第一个非 dim_ 限定表）；不可判定 → None。"""
    expression = model.metrics.get(metric)
    if expression is None:
        return None
    try:
        ast = sqlglot.parse_one(expression)
    except sqlglot.errors.ParseError:
        return None
    for col in ast.find_all(exp.Column):
        if col.table and col.table in model.datasets and not col.table.startswith("dim_"):
            return str(col.table)
    return None


def _bfs_edges(
    model: SemanticModel, main_ds: str, targets: set[str]
) -> tuple[list[Relationship], set[str]]:
    """与 Compiler._join_chain 同口径的 BFS（返回边对象，供逐边检查复用）。

    返回 (按发现顺序的边列表, 不可达目标集)。多跳路径的中间边全部包含。
    """
    remaining = {t for t in targets if t != main_ds}
    edges: list[Relationship] = []
    added: set[tuple[str, str]] = set()
    visited = {main_ds}
    queue: list[tuple[str, list[Relationship]]] = [(main_ds, [])]
    while queue:
        current, chain = queue.pop(0)
        for rel in model.relationships:
            edge = (
                rel
                if rel.from_ds == current
                else (rel.reversed() if rel.to_ds == current else None)
            )
            if edge is None or edge.to_ds in visited:
                continue
            new_chain = chain + [edge]
            if edge.to_ds in remaining:
                for e in new_chain:
                    key = (e.from_ds, e.to_ds)
                    if key not in added:
                        edges.append(e)
                        added.add(key)
                remaining.discard(edge.to_ds)
            visited.add(edge.to_ds)
            queue.append((edge.to_ds, new_chain))
    return edges, remaining


def _edge_join(compiler: Compiler, edge: Relationship, kind: str) -> exp.Join:
    """边 → JOIN AST（复用 Compiler._join_ast 的表/列/ON 口径，可改连接类型）。"""
    join = compiler._join_ast(edge)
    if kind != "INNER":
        join.set("kind", kind)
    return join


def _time_between(
    compiler: Compiler, time_table: str, date_col: str, lo: str, hi: str
) -> exp.Between:
    """锁定快照全期的显式绝对时间谓词（不依赖 Guard 默认时间窗回退）。"""
    return exp.Between(
        this=compiler._column_ast(time_table, date_col),
        low=exp.cast(exp.Literal.string(lo), to="DATE"),
        high=exp.cast(exp.Literal.string(hi), to="DATE"),
    )


def _dup_check_sql(
    compiler: Compiler,
    edge: Relationship,
    main_ds: str,
    time_table: str,
    time_pred: exp.Between,
) -> exp.Select:
    """重复目标键检查：引用键子查询限定时间范围后，目标表键必须唯一。

    单列键：``COUNT(*) - COUNT(DISTINCT k)``（引用键经 IN (SELECT …) 圈定）；
    多列键：目标表与去重引用键子查询按全键 INNER JOIN 后同式相减。
    """
    to_ds = edge.to_ds
    key_cols = list(edge.to_columns)
    # 引用键子查询：事实侧经 INNER 链到 edge.from_ds 与时间表，再按时间范围过滤
    sub = exp.Select(
        expressions=[compiler._column_ast(edge.from_ds, fc) for fc in edge.from_columns],
        from_=exp.From(this=compiler._table_ast(main_ds)),
    )
    sub_joins, _ = compiler._join_chain(main_ds, {edge.from_ds, time_table})
    if sub_joins:
        sub.set("joins", sub_joins)
    sub.set("where", exp.Where(this=time_pred.copy()))
    distinct_keys = exp.Distinct(expressions=[compiler._column_ast(to_ds, k) for k in key_cols])
    scalar = exp.Sub(this=exp.Count(this=exp.Star()), expression=exp.Count(this=distinct_keys))
    select = exp.Select(
        expressions=[scalar.as_("dup_cnt")],
        from_=exp.From(this=compiler._table_ast(to_ds)),
    )
    if len(key_cols) == 1:
        # 单列键：引用键经 IN (SELECT …) 圈定目标行，键唯一性同式相减
        select.set(
            "where",
            exp.Where(
                this=exp.In(
                    this=compiler._column_ast(to_ds, key_cols[0]),
                    query=exp.Subquery(this=sub),
                )
            ),
        )
    else:
        # 多列键：目标表按全键 INNER JOIN 去重引用键子查询后同式相减
        ref = sub.copy()
        ref.set("distinct", exp.Distinct())
        on: exp.Condition | None = None
        for fc, kc in zip(edge.from_columns, key_cols, strict=True):
            cond = exp.EQ(
                this=compiler._column_ast(to_ds, kc),
                expression=exp.column(fc, table="ref_keys"),
            )
            on = cond if on is None else exp.and_(on, cond)
        select.set(
            "joins",
            [
                exp.Join(
                    this=exp.Subquery(
                        this=ref,
                        alias=exp.TableAlias(this=exp.to_identifier("ref_keys")),
                    ),
                    on=on,
                    kind="INNER",
                )
            ],
        )
    select.set("limit", exp.Limit(expression=exp.Literal.number(1)))
    return select


def _orphan_check_sql(
    compiler: Compiler,
    edge: Relationship,
    main_ds: str,
    time_table: str,
    time_pred: exp.Between,
) -> exp.Select:
    """孤儿引用检查：时间范围内的事实行引用键必须在目标表存在（LEFT JOIN 判空）。

    目标表即时间维表时，时间谓词放入 LEFT JOIN ON（否则 IS NULL 行会被
    WHERE 时间过滤吞掉，检查退化为恒过）。
    """
    to_ds = edge.to_ds
    null_pred: exp.Condition | None = None
    for kc in edge.to_columns:
        cond = exp.Is(this=compiler._column_ast(to_ds, kc), expression=exp.Null())
        null_pred = cond if null_pred is None else exp.and_(null_pred, cond)
    if to_ds == time_table:
        join = _edge_join(compiler, edge, "LEFT")
        # Join.on 是 builder 方法不是属性：ON 条件经 args 读写
        on = join.args.get("on")
        join.set("on", exp.and_(on, time_pred.copy()) if on is not None else time_pred.copy())
        joins: list[exp.Join] = [join]
        where = null_pred
    else:
        inner_joins, _ = compiler._join_chain(main_ds, {edge.from_ds, time_table})
        joins = [*inner_joins, _edge_join(compiler, edge, "LEFT")]
        where = exp.and_(time_pred.copy(), null_pred) if null_pred is not None else time_pred.copy()
    select = exp.Select(
        expressions=[exp.Count(this=exp.Star()).as_("orphan_cnt")],
        from_=exp.From(this=compiler._table_ast(main_ds)),
    )
    select.set("joins", joins)
    select.set("where", exp.Where(this=where))
    select.set("limit", exp.Limit(expression=exp.Literal.number(1)))
    return select


def _invariant_check_sql(
    compiler: Compiler,
    main_ds: str,
    time_table: str,
    time_pred: exp.Between,
    base_targets: set[str],
    joined_targets: set[str],
) -> exp.Select:
    """行不变量检查：追加全部分组/权限 INNER JOIN 不得放大或丢失事实行。

    ``base - joined``：放大（重复键）为负、漏行（过滤性连接）为正，仅 0 通过。
    INNER 证明严格强于 LEFT 注入（INNER 干净 ⟹ LEFT 干净）。
    """
    subqueries: list[exp.Subquery] = []
    for targets in (base_targets, joined_targets):
        joins, _ = compiler._join_chain(main_ds, targets)
        sub = exp.Select(
            expressions=[exp.Count(this=exp.Star())],
            from_=exp.From(this=compiler._table_ast(main_ds)),
        )
        if joins:
            sub.set("joins", joins)
        sub.set("where", exp.Where(this=time_pred.copy()))
        subqueries.append(exp.Subquery(this=sub))
    scalar = exp.Sub(this=subqueries[0], expression=subqueries[1])
    select = exp.Select(expressions=[scalar.as_("row_delta")])
    select.set("limit", exp.Limit(expression=exp.Literal.number(1)))
    return select


def _scope_tables(sql: str) -> list[str]:
    """检查 SQL 实际触碰的表（catalog.db.table 全名，按 AST 提取）。"""
    names = set()
    for table in sql_guard.parse(sql, "doris").find_all(exp.Table):
        full = ".".join(p for p in (table.catalog, table.db, table.name) if p)
        names.add(full)
    return sorted(names)


def _run_check(sql_text: str, executor: Executor, budget: Budget) -> tuple[str, Any]:
    """逐条 enforce 后执行检查 SQL，返回 (result, observed)。失败一律 fail closed。"""
    try:
        # 资格检查 SQL 自带锁定快照全期的显式绝对时间谓词，不依赖 Guard 默认
        # 时间窗回退（Guard 回退只扫外层 WHERE，会把默认窗口误注入子查询承载
        # 时间过滤的检查 SQL），故 enforce 不传 model。
        enforced, _cost = sql_guard.enforce(sql_text, budget=budget)
    except (sql_guard.UnsafeQuery, sql_guard.BudgetExceeded) as exc:
        return "fail", str(exc)
    try:
        rows, _columns = executor(enforced)
        value = int(rows[0][0])
    except Exception as exc:  # noqa: BLE001 — 执行层任何异常都按检查失败处理
        return "fail", str(exc)
    return ("pass" if value == 0 else "fail"), value


def _policy_tables(model: SemanticModel, policy_path: Path | None) -> tuple[set[str], str | None]:
    """default_row_policy → 策略条件引用的表集（权限追加 JOIN 的检查范围）。

    条件里的 ``表.列`` token 即 Guard 行级策略将注入的 JOIN 目标；user.* 是
    Jinja 上下文不是表。文件缺失 / 策略未登记 / 引用未知表 → fail closed。
    """
    path = policy_path or POLICY_PATH
    try:
        policies = load_policies_by_name(path)
    except (OSError, yaml.YAMLError, KeyError) as exc:
        return set(), f"policy_unresolvable: 策略文件不可读 {path}：{exc}"
    policy = policies.get(str(model.default_row_policy))
    if policy is None:
        return set(), f"policy_unresolvable: 策略 {model.default_row_policy} 未登记于 {path}"
    tables: set[str] = set()
    for role in policy.get("roles", []):
        condition = str(role.get("condition", ""))
        for match in _TABLE_TOKEN_RE.finditer(condition):
            name = match.group(1)
            if name != "user":
                tables.add(name)
    unknown = sorted(t for t in tables if t not in model.datasets)
    if unknown:
        return set(), f"policy_unresolvable: 策略条件引用模型外的表 {unknown}"
    return tables, None


def _parse_data_range(data_range: object) -> tuple[str | None, str | None]:
    """锁定快照 data_range（ISO 起止）→ (起始日, 结束日)；非法 → (None, None)。"""
    if not isinstance(data_range, str) or "~" not in data_range:
        return None, None
    try:
        lo = datetime.fromisoformat(data_range.split("~", 1)[0])
        hi = datetime.fromisoformat(data_range.split("~", 1)[1])
    except ValueError:
        return None, None
    if lo > hi:
        return None, None
    return lo.strftime("%Y-%m-%d"), hi.strftime("%Y-%m-%d")


def verify_eligibility(
    model: SemanticModel,
    snapshot_meta: dict[str, Any],
    executor: Executor,
    budget: Budget,
    *,
    policy_path: Path | None = None,
) -> dict[str, Any]:
    """对锁定快照复核全部已登记组合的连接质量，产出资格报告（含全部检查 SQL）。

    eligible 仅当：存在已登记组合且全部结构可加、join 链与权限策略表全部
    可达、每条检查 SQL 经 Guard enforce 后执行且计数为 0。覆盖不全（不可
    达 / 非法时间范围 / 无登记组合）一律不签发，且原因写入 reasons。
    """
    report: dict[str, Any] = {
        "model": model.name,
        "semantic_sha256": model.source_sha256,
        "snapshot_sha": snapshot_meta.get("sha"),
        "time_range": None,
        "pairs": [],
        "join_paths": [],
        "checks": [],
        "reasons": [],
        "eligible": False,
    }
    lo, hi = _parse_data_range(snapshot_meta.get("data_range"))
    if lo is None or hi is None:
        report["reasons"].append("invalid_data_range")
        return report
    report["time_range"] = {"min": lo, "max": hi}

    combos = [
        (metric, dim)
        for metric in sorted(model.attribution_dimensions)
        for dim in model.attribution_dimensions[metric]
    ]
    if not combos:
        report["reasons"].append("no_declared_combination")
        return report
    pairs = [evaluate_combo(model, metric, dim) for metric, dim in combos]
    report["pairs"] = pairs
    executable = [p for p in pairs if p["eligible"]]
    if not executable:
        return report

    td = model.time_dimension
    if not isinstance(td, dict) or not td.get("table") or not td.get("columns", {}).get("date"):
        report["reasons"].append("time_scope_unsupported")
        return report
    time_table = str(td["table"])
    date_col = str(td["columns"]["date"])

    main_ds = _fact_dataset(model, executable[0]["metric"])
    if main_ds is None:
        report["reasons"].append(f"fact_dataset_unresolvable: {executable[0]['metric']}")
        return report

    dim_targets = {time_table}
    for pair in executable:
        found = model.find_field(pair["dimension"])
        if found is not None:
            dim_targets.add(found[0])
    compiler = Compiler(model)
    time_pred = _time_between(compiler, time_table, date_col, lo, hi)

    # join 链解析：维度 + 时间表先行；权限策略表随后——任一不可达 fail closed
    core_edges, core_missing = _bfs_edges(model, main_ds, dim_targets)
    if core_missing:
        report["reasons"].append(
            f"join_path_unresolvable: 无法从 {main_ds} 经 relationships 到达 {sorted(core_missing)}"
        )
        return report
    policy_tables: set[str] = set()
    if model.default_row_policy:
        policy_tables, policy_error = _policy_tables(model, policy_path)
        if policy_error is not None:
            report["reasons"].append(policy_error)
            return report
        edges, missing = _bfs_edges(model, main_ds, dim_targets | policy_tables)
        if missing:
            report["reasons"].append(
                "policy_unresolvable: 权限追加表无法从 "
                f"{main_ds} 经 relationships 到达 {sorted(missing)}"
            )
            return report
    else:
        edges = core_edges
    report["join_paths"] = [f"{e.from_ds} → {e.to_ds}（{e.name}）" for e in edges]

    # 逐边：重复目标键 + 孤儿引用；再加行不变量——覆盖不全不签发
    seen: set[str] = set()
    for edge in edges:
        for name, sql_ast in (
            (
                f"duplicate_target_key:{edge.to_ds}",
                _dup_check_sql(compiler, edge, main_ds, time_table, time_pred),
            ),
            (
                f"orphan_reference:{edge.from_ds}->{edge.to_ds}",
                _orphan_check_sql(compiler, edge, main_ds, time_table, time_pred),
            ),
        ):
            if name in seen:
                continue
            seen.add(name)
            report["checks"].append({"name": name, "sql_ast": sql_ast, "scope_tables": None})
    base_targets = {time_table}
    joined_targets = dim_targets | policy_tables
    report["checks"].append(
        {
            "name": "row_invariant",
            "sql_ast": _invariant_check_sql(
                compiler, main_ds, time_table, time_pred, base_targets, joined_targets
            ),
            "scope_tables": None,
        }
    )

    # 逐条 enforce → 执行 → 记录（SQL、实际检查范围、结果）
    executed: list[dict[str, Any]] = []
    for check in report["checks"]:
        sql_ast = check.pop("sql_ast")
        sql_text = sql_ast.sql()
        result, observed = _run_check(sql_text, executor, budget)
        executed.append(
            {
                "name": check["name"],
                "sql": sql_text,
                "scope": {
                    "tables": _scope_tables(sql_text),
                    "time_range": {"min": lo, "max": hi},
                    "sharding": None,
                },
                "result": result,
                "observed": observed,
            }
        )
    report["checks"] = executed

    report["eligible"] = (
        bool(report["pairs"])
        and all(p["eligible"] for p in report["pairs"])
        and bool(report["checks"])
        and all(c["result"] == "pass" for c in report["checks"])
        and not report["reasons"]
    )
    return report


# ---------------------------------------------------------------------------
# 证据落盘与只读绑定
# ---------------------------------------------------------------------------


def record_eligibility(report: dict[str, Any], snapshot_dir: Path | None = None) -> Path | None:
    """合格报告落盘 data/snapshots/<snapshot_sha>.analysis.json（机器生成）。

    不合格（eligible=False）一律不写任何产物，返回 None——失败状态只存在
    于复核会话，不进快照目录冒充证据。
    """
    if not report.get("eligible"):
        return None
    directory = Path(snapshot_dir) if snapshot_dir is not None else Path(identity.SNAPSHOT_DIR)
    path = directory / f"{report['snapshot_sha']}.analysis.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_eligibility(model: SemanticModel, snapshot_meta: dict[str, Any]) -> dict[str, Any]:
    """只读绑定资格证据：缺失 / 过期 / 哈希漂移返回不可用事实，不抛异常。

    绑定校验双重执行：snapshot_sha 必须与当前锁定快照一致（过期证据拒绝），
    semantic_sha256 必须等于当前模型源内容哈希（模型任何字节变化使旧证据
    失效）。返回 {"available", "eligible", "reason", "evidence"}；available
    = True 当且仅当证据存在、合格且双重绑定全部匹配。普通 Agent 构造不因
    本函数阻断（证据缺失只是 available=False 的事实）。
    """
    sha = str(snapshot_meta.get("sha", ""))
    result: dict[str, Any] = {
        "available": False,
        "eligible": False,
        "reason": None,
        "evidence": None,
    }
    path = Path(identity.SNAPSHOT_DIR) / f"{sha}.analysis.json"
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        result["reason"] = "missing_evidence"
        return result
    except (OSError, json.JSONDecodeError):
        result["reason"] = "invalid_evidence"
        return result
    if not isinstance(evidence, dict):
        result["reason"] = "invalid_evidence"
        return result
    result["evidence"] = evidence
    if evidence.get("snapshot_sha") != sha or evidence.get("eligible") is not True:
        result["reason"] = "snapshot_mismatch"
        return result
    if evidence.get("semantic_sha256") != model.source_sha256:
        # 绑定断言（brief 原文）：semantic hash 不一致时绝不返回 eligible=True
        result["reason"] = "semantic_hash_mismatch"
        return result
    result["available"] = True
    result["eligible"] = True
    return result


# ---------------------------------------------------------------------------
# 资格 CLI（显式 --snapshot-sha，先复核锁定数据）
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None, *, executor: Executor | None = None) -> int:
    """复核锁定快照的分析资格；合格写证据产物，失败非零且不写任何状态。"""
    parser = argparse.ArgumentParser(
        prog="analysis-eligibility",
        description="ADR-0026 T02：复核锁定快照上已登记可加组合的连接质量并登记资格证据",
    )
    parser.add_argument("--snapshot-sha", required=True, help="锁定快照 sha（必填，防误跑全目录）")
    parser.add_argument("--model", type=Path, default=None, help="语义模型路径（缺省用金融模型）")
    parser.add_argument(
        "--snapshots-dir", type=Path, default=None, help="快照目录（缺省 data/snapshots）"
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=None,
        help="行级策略文件（缺省 semantic/policies/row_policy.yml）",
    )
    args = parser.parse_args(argv)

    snap_dir = Path(args.snapshots_dir) if args.snapshots_dir else Path(identity.SNAPSHOT_DIR)
    meta_path = snap_dir / f"{args.snapshot_sha}.meta.json"
    if not meta_path.exists():
        print(f"[analysis-eligibility] 锁定快照 meta 不存在：{meta_path}", file=sys.stderr)
        return 1
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[analysis-eligibility] meta 不可读：{exc}", file=sys.stderr)
        return 1
    # 复核锁定数据：meta 文件名与内容 sha 必须一致（与 data.identity._load_meta 同口径）
    if meta.get("sha") != args.snapshot_sha:
        print(
            f"[analysis-eligibility] meta 内容 sha {meta.get('sha')!r} 与文件名"
            f" {args.snapshot_sha!r} 不一致，拒绝复核",
            file=sys.stderr,
        )
        return 1

    model = SemanticModel(args.model) if args.model else SemanticModel()
    if executor is None:
        from eval.runner import execute_sql

        executor = execute_sql
    from eval.runner import build_budget

    budget = build_budget(meta)
    report = verify_eligibility(model, meta, executor, budget, policy_path=args.policy)
    if not report["eligible"]:
        print("[analysis-eligibility] 复核未通过，不登记资格证据：", file=sys.stderr)
        for reason in report["reasons"]:
            print(f"  原因：{reason}", file=sys.stderr)
        for pair in report["pairs"]:
            if not pair["eligible"]:
                print(
                    f"  组合 {pair['metric']} × {pair['dimension']}：{pair['reason']}",
                    file=sys.stderr,
                )
        for check in report["checks"]:
            if check["result"] != "pass":
                print(f"  检查 {check['name']}：observed={check['observed']}", file=sys.stderr)
                print(f"    SQL：{check['sql']}", file=sys.stderr)
        return 1
    path = record_eligibility(report, snap_dir)
    print(f"[analysis-eligibility] 资格证据已登记：{path}")
    return 0


if __name__ == "__main__":  # 无此入口时 python -m 会静默 import 后 exit 0（空跑绿灯）
    raise SystemExit(main())
