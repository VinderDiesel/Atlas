"""只读 SQL 网关（Atlas 安全红线）

设计原则
--------
1. **AST 层面校验，不用正则**。正则可被注释、嵌套、编码变形绕过。
2. **纵深防御**：应用层 Guard + 数据库只读账号，不依赖单一层。
3. **执行链顺序不可调换**：解析 → 禁 DDL/DML → 函数黑名单 → LIMIT
   → 时间范围 → 行级策略注入 → 成本估算 → 执行。

状态
----
Day 19-20 骨架 → 契约测试覆盖（tests/test_sql_guard.py）与行级权限实测
（serving/rls_verify.py）后持续演进：AST 校验链（禁 DDL/DML → 函数黑名单
→ LIMIT → 时间范围 → 行级策略注入 → 二次只读校验 → 成本估算）已落地；跨表
策略谓词沿语义模型 join 图补 LEFT JOIN（Phase 2，ADR-0014 ⑤ 相关），无合法
路径仍拒绝。
已知边界（见 README Known Limitations）：视图展开递归校验仍待实现；成本估算
当前为基于扫描表数的启发式代理（非基于真实统计信息的行数估算，ADR-0003 代价
登记），阈值属保守护栏而非压测标定值。

参考 ADR：infra/adr/0003-readonly-sql-gateway.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

# ---------------------------------------------------------------------------
# 配置（从环境变量读取，见 .env.example）
# ---------------------------------------------------------------------------

DEFAULT_MAX_ROWS = 10_000
DEFAULT_MAX_DAYS = 730

# 函数黑名单：高风险或可能泄露信息的函数
BLOCKED_FUNCTIONS = frozenset(
    {
        "system",
        "execute",
        "eval",
        "file",
        "url",
        "remote",
        "s3",
        "mysql",
        "postgresql",
        "odbc",
        "jdbc",
        "httpget",
        "load_file",
        "outfile",
        "dumpfile",
        "sleep",
        "benchmark",
        "pg_sleep",
    }
)

# 禁止的语句节点类型（DDL / DML / 管理语句）
# 注意：sqlglot 30.17 的 ALTER 节点是 exp.Alter（exp.AlterTable 不存在，已实测）
FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Grant,
    exp.Revoke,
    exp.Copy,
    exp.Create,
    exp.Command,  # 兜底：无法解析为已知 AST 的命令
    exp.Attach,
    exp.Detach,
    exp.TruncateTable,
    exp.Merge,
    exp.Set,
    exp.Transaction,
)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Budget:
    """查询预算。超预算直接拒绝，而不是降级执行。

    max_cost_units 是成本预算阈值（启发式扫描广度护栏，非压测标定）：
    estimate_cost 返回归一化成本（扫描表数 / 8.0），> 1.0 即拒绝异常宽的
    多表扫描。阈值与 estimate_cost 量纲一致；部署方应按实际表数调参。
    default_time_window_days 是时间范围防御的默认回退窗：当查询已 join 语义
    模型声明的时间维表、却无任何时间谓词时，强制补 `time_col >= 当前 - N 天`
    的下界（纵深防御，防止无界全表扫描）。0 = 关闭该回退（仅依赖上游注入）。
    """

    max_rows: int = DEFAULT_MAX_ROWS
    max_days: int = DEFAULT_MAX_DAYS
    max_cost_units: float = 1.0
    dialect: str = "clickhouse"
    time_column: str = "order_date"
    allowed_tables: frozenset[str] = field(default_factory=frozenset)
    default_time_window_days: int = DEFAULT_MAX_DAYS
    table_stats: dict[str, float] = field(default_factory=dict)

    def exceeded(self, cost_units: float) -> bool:
        return cost_units > self.max_cost_units


@dataclass(frozen=True)
class Policy:
    """行级权限策略。

    condition 是 SQL 谓词模板，如 ``region = '{{ user.region }}'``。
    **必须能编译为可解释的谓词**，否则无法通过审计。
    """

    name: str
    condition: str
    columns: tuple[str, ...] = ("*",)

    def render(self, user_context: dict[str, object]) -> str:
        """渲染用户上下文到谓词。

        注意：这里做的是**模板渲染**，不是字符串拼接 SQL。
        渲染结果仍须经过 sqlglot 解析校验后才可注入。
        """
        rendered = self.condition
        for key, value in user_context.items():
            placeholder = f"{{{{ user.{key} }}}}"
            if placeholder in rendered:
                rendered = rendered.replace(placeholder, _escape_literal(value))
        if "{{" in rendered:
            raise UnsafeQuery(f"策略中存在未渲染的占位符：{rendered}")
        return rendered

    def rewrite(self, tree: exp.Expr, model: object | None = None) -> exp.Expr:
        """把谓词注入到 WHERE 子句（AND 连接，不覆盖已有条件）。

        注入前做两件事：
        1. **跨表补 join（Phase 2，KL #14 收窄）**：谓词按物理表名书写（如
           dim_broker.branch），其引用表不在查询中时，沿语义模型 relationships
           join 图补 LEFT JOIN（与编译器同形态：catalog.db.table AS base 名 + 关系
           列 EQ）；无模型或无合法路径 → 拒绝（安全底线不放开）。
        2. **表引用对齐（Day 25）**：谓词列改写为 SQL 实际别名（编译 SQL 可能给
           表起了别名，如 FROM atlas.dwd.dim_broker AS db，别名遮蔽后原表名不可用）。

        model 只要求鸭子接口：datasets[name].source（catalog.db.table）与
        relationships（from_ds/to_ds/from_columns/to_columns/reversed()），
        agent.compiler.SemanticModel 天然满足。
        """
        predicate_sql = self.condition
        predicate = sqlglot.parse_one(predicate_sql, read="clickhouse")
        tree = tree.copy()
        # Phase 2：谓词引用表不在查询中 → join 图补表；无模型/无路径即拒绝
        existing = {table.name for table in tree.find_all(exp.Table)}
        missing = _predicate_table_bases(predicate) - existing
        if missing:
            if model is None:
                raise UnsafeQuery(
                    f"策略引用表不在查询中且未提供语义模型（跨表谓词需 join 注入）："
                    f"{sorted(missing)}"
                )
            tree = _inject_policy_joins(tree, model, missing)
        predicate = _qualify_predicate_to_sql(predicate, tree)
        where = tree.find(exp.Where)
        if where is None:
            # 没有 WHERE 就补一个
            if isinstance(tree, exp.Select):
                tree.set("where", exp.Where(this=predicate))
        else:
            existing = where.this
            where.set("this", exp.and_(existing, predicate))
        return tree


class UnsafeQuery(Exception):
    """SQL 未通过安全检查。"""


class BudgetExceeded(Exception):
    """查询超出预算。"""


# ---------------------------------------------------------------------------
# 校验链
# ---------------------------------------------------------------------------


def _escape_literal(value: object) -> str:
    """校验并转义字面量，防注入。

    只做合法性校验与引号转义，**不包引号**——引号由策略模板负责
    （模板约定：`branch = '{{ user.branch }}'`），否则会双重引号。
    这是**兜底防御**，不是主要防线。主要防线是 AST 校验 + 参数化。
    """
    if isinstance(value, int | float):
        return str(value)
    text = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_\-.:]+", text):
        raise UnsafeQuery(f"用户上下文字面量含非法字符：{text!r}")
    # 正则已排除单引号，此转义仅为纵深防御保留
    return text.replace("'", "''")


def _predicate_table_bases(predicate: exp.Expr) -> set[str]:
    """谓词中限定列引用的表（base 名集合）。"""
    bases: set[str] = set()
    for column in predicate.find_all(exp.Column):
        if column.table:
            bases.add(str(column.table).split(".")[-1])
    return bases


def _physical_table(source: str) -> exp.Table:
    """catalog.db.table → exp.Table（AS base 名，与编译器产物同形态）。"""
    parts = source.split(".")
    if len(parts) != 3:
        raise UnsafeQuery(f"语义模型 dataset source 非 catalog.db.table：{source!r}")
    # this 必须为 Identifier：Guard 注入后仍会 find_all(exp.Table) 并访问 .name
    # （实测 this=str 时 table.name property 崩溃，sqlglot 期望 Identifier）
    table = exp.Table(this=exp.to_identifier(parts[2]), db=parts[1], catalog=parts[0])
    table.set("alias", exp.TableAlias(this=exp.to_identifier(parts[2])))
    return table


def _policy_join_ast(edge: Any, sources: dict[str, str]) -> exp.Join:
    """语义边 → LEFT JOIN AST：ON 由关系列 EQ 连接（与编译器 _join_ast 同构）。"""
    source = sources.get(edge.to_ds)
    if source is None:
        raise UnsafeQuery(f"语义模型缺少 dataset：{edge.to_ds}")
    on: exp.Expr | None = None
    for fc, tc in zip(edge.from_columns, edge.to_columns, strict=True):
        cond = exp.EQ(
            this=exp.column(fc, table=edge.from_ds),
            expression=exp.column(tc, table=edge.to_ds),
        )
        on = cond if on is None else exp.and_(on, cond)
    return exp.Join(this=_physical_table(source), on=on, kind="LEFT")


def _join_path_to(model: object, starts: set[str], goal: str) -> list[Any] | None:
    """BFS：从 starts（查询中已存在的表）沿 relationships 到 goal 的最短边链。

    返回边列表（已按行进方向排列，反向边已 reversed）；不可达返回 None。
    """
    visited = set(starts)
    queue: list[tuple[str, list[Any]]] = [(s, []) for s in sorted(starts)]
    while queue:
        current, chain = queue.pop(0)
        for rel in model.relationships:  # type: ignore[attr-defined]
            edge = (
                rel
                if rel.from_ds == current
                else (rel.reversed() if rel.to_ds == current else None)
            )
            if edge is None or edge.to_ds in visited:
                continue
            new_chain = chain + [edge]
            if edge.to_ds == goal:
                return new_chain
            visited.add(edge.to_ds)
            queue.append((edge.to_ds, new_chain))
    return None


def _inject_policy_joins(tree: exp.Expr, model: object, missing: set[str]) -> exp.Expr:
    """补 LEFT JOIN 使谓词引用表可达；无路径 → UnsafeQuery（安全底线不放开）。

    逐目标 BFS：路径上每个新表补一条 join（已在查询中的表跳过——已可达）；
    注入表与查询中同名表冲突时不重复注入。
    """
    try:
        sources = {ds.name: ds.source for ds in model.datasets.values()}  # type: ignore[attr-defined]
    except AttributeError as exc:
        raise UnsafeQuery(f"语义模型接口不符（需 datasets[name].source）：{exc}") from exc
    existing = {table.name for table in tree.find_all(exp.Table)}
    joins = list(tree.args.get("joins") or [])
    for goal in sorted(missing):
        chain = _join_path_to(model, existing, goal)
        if chain is None:
            raise UnsafeQuery(
                f"策略引用表 {goal} 与查询表 {sorted(existing)} 之间无合法 join 路径"
                "（语义模型 relationships 不可达）"
            )
        for edge in chain:
            if edge.to_ds in existing:
                continue
            joins.append(_policy_join_ast(edge, sources))
            existing.add(edge.to_ds)
    if joins:
        tree.set("joins", joins)
    return tree


def _qualify_predicate_to_sql(predicate: exp.Expr, tree: exp.Expr) -> exp.Expr:
    """谓词的表限定列引用 → SQL 实际别名（无别名则保持原名）。

    只处理形如 `dim_broker.branch = ...` 的限定列：谓词引用表不在查询中的
    情况已在 Policy.rewrite 先经语义模型 join 图补表（Phase 2）；此处未命中
    的限定表（无模型路径或模型缺表）作兜底拒绝——静默放行会产生引用不存在
    表的坏 SQL。未限定的列（如 `branch = ...`）不做处理，保持兼容。
    """
    aliases: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        # 多段名（catalog.db.table）也按 base 名对齐；别名遮蔽后原表名不可引用
        aliases[table.name] = table.alias or table.name
    predicate = predicate.copy()
    for column in predicate.find_all(exp.Column):
        table_name = column.table
        if not table_name:
            continue
        base = str(table_name).split(".")[-1]
        if base not in aliases:
            raise UnsafeQuery(
                f"策略引用表 {base} 不在查询中且语义模型无可达 join 路径"
                "（跨表谓词注入失败，拒绝放行）"
            )
        if aliases[base] != base:
            column.set("table", exp.to_identifier(aliases[base]))
    return predicate


def parse(sql: str, dialect: str) -> exp.Expr:
    """解析为 AST。解析失败一律拒绝（不尝试修复）。"""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # noqa: BLE001 - 解析失败必须拒绝
        raise UnsafeQuery(f"SQL 无法解析：{exc}") from exc
    if tree is None:
        raise UnsafeQuery("SQL 解析结果为空")
    return tree


def check_readonly(tree: exp.Expr) -> None:
    """禁 DDL / DML / 管理语句。"""
    for node_type in FORBIDDEN_NODES:
        if tree.find(node_type) is not None:
            raise UnsafeQuery(f"检测到禁止的语句类型：{node_type.__name__}")


def check_functions(tree: exp.Expr) -> None:
    """函数黑名单。

    注意：不认识的函数会被 sqlglot 解析为 exp.Anonymous（实测 sleep 即如此），
    因此 Anonymous 也必须过黑名单，否则形同虚设。
    """
    for func in tree.find_all(exp.Func, exp.Anonymous):
        name = func.sql_name().lower()
        if isinstance(func, exp.Anonymous):
            # 实测：Anonymous.sql_name() 恒为 'ANONYMOUS'，函数名在 this 中
            name = str(func.this).lower()
        if name in BLOCKED_FUNCTIONS:
            raise UnsafeQuery(f"检测到禁止的函数：{name}")


def check_tables(tree: exp.Expr, budget: Budget) -> None:
    """表白名单（如果配置了）。

    注意：视图展开可能绕过表白名单，需要递归解析视图定义（待实现）。
    """
    if not budget.allowed_tables:
        return
    for table in tree.find_all(exp.Table):
        full_name = ".".join(part for part in (table.catalog, table.db, table.name) if part)
        if full_name not in budget.allowed_tables:
            raise UnsafeQuery(f"表不在白名单内：{full_name}")


def apply_limit(tree: exp.Expr, max_rows: int) -> exp.Expr:
    """强制 LIMIT。已有更小的 LIMIT 则保留原值。"""
    tree = tree.copy()
    limit = tree.find(exp.Limit)
    if limit is None:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
    else:
        existing = limit.expression
        if existing.is_int and int(existing.this) > max_rows:
            limit.set("expression", exp.Literal.number(max_rows))
    return tree


def apply_time_range(tree: exp.Expr, budget: Budget, model: object | None = None) -> exp.Expr:
    """强制时间范围（纵深防御，AST 构造，禁用字符串拼接）。

    判定：当语义模型声明了 time_dimension、查询已 join 该时间维表、且查询中
    对该表时间列**没有任何谓词**时，追加默认下界
    ``time_col >= CURRENT_DATE - INTERVAL default_time_window_days DAY``。
    已含时间谓词（编译器对注册问句总是注入）→ 跳过；查询未 join 时间维表
    → 无法安全注入（补表会放大攻击面），跳过并依赖上游时间约束。

    仅消费 budget.default_time_window_days 与 model.time_dimension；不读 time_column
    （真实时间列名来自模型声明，而非预算里的占位列名）。
    """
    if budget.default_time_window_days <= 0 or model is None:
        return tree
    td = getattr(model, "time_dimension", None)
    if not isinstance(td, dict):
        return tree
    time_table = td.get("table")
    columns = td.get("columns")
    if not isinstance(time_table, str) or not isinstance(columns, dict) or not columns:
        return tree
    # 时间列优先取 date 粒度列，否则取声明中的任一列
    time_col = columns.get("date") or next(iter(columns.values()))
    if not isinstance(time_col, str):
        return tree

    existing = {table.name for table in tree.find_all(exp.Table)}
    aliases = {table.name: (table.alias or table.name) for table in tree.find_all(exp.Table)}
    referenced = time_table in existing or time_table in aliases.values()
    if not referenced:
        return tree
    # 已有对该时间表列的谓词 → 跳过（避免双重约束）
    if any(
        isinstance(col, exp.Column)
        and (col.table == time_table or aliases.get(str(col.table)) == time_table)
        for col in tree.find_all(exp.Column)
    ):
        return tree

    tree = tree.copy()
    days = budget.default_time_window_days
    interval = exp.Interval(this=exp.Literal.string(f"{days} DAY"))
    date_expr = exp.Sub(this=exp.CurrentDate(), expression=interval)
    predicate = exp.GTE(
        this=exp.column(time_col, table=time_table),
        expression=date_expr,
    )
    where = tree.find(exp.Where)
    if where is None:
        tree.set("where", exp.Where(this=predicate))
    else:
        where.set("this", exp.and_(where.this, predicate))
    return tree


def estimate_cost(tree: exp.Expr, stats: dict[str, float] | None = None) -> float:
    """成本估算（启发式，归一化，非压测标定）。

    返回归一化成本（与 Budget.max_cost_units 同量纲）：
    - 有表行数统计 stats（表全名 → 行数）时，成本 = Σ 引用表行数 / 1e6
      （百万行单位），反映真实扫描体量；
    - 无统计时回退为扫描表数 / 8.0（表数代理：扫描越宽成本越高）。
    成本仅用于阈值护栏（异常宽扫描拒绝），不用于执行计划选择。
    """
    tables = [t for t in tree.find_all(exp.Table)]
    if stats:
        total = 0.0
        for t in tables:
            full = ".".join(p for p in (t.catalog, t.db, t.name) if p)
            total += float(stats.get(full, stats.get(t.name, 0.0)))
        return total / 1_000_000.0
    return len(tables) / 8.0


def enforce(
    sql: str,
    policy: Policy | None = None,
    budget: Budget | None = None,
    user_context: dict[str, object] | None = None,
    model: object | None = None,
) -> tuple[str, float]:
    """执行完整校验链，返回（安全的 SQL，成本估算）。

    顺序不可调换 —— 见 ADR-0003。model 供行级策略跨表谓词补 join 注入
    （Policy.rewrite，语义模型鸭子接口）；查询域内策略无需 model。
    """
    budget = budget or Budget()
    tree = parse(sql, budget.dialect)

    check_readonly(tree)
    check_functions(tree)
    check_tables(tree, budget)

    tree = apply_limit(tree, budget.max_rows)
    tree = apply_time_range(tree, budget, model=model)

    if policy is not None:
        if user_context is not None:
            policy = Policy(
                name=policy.name,
                condition=policy.render(user_context),
                columns=policy.columns,
            )
        tree = policy.rewrite(tree, model=model)

    # 策略注入后**再次**校验，防止策略本身引入危险语句（含补表后的表白名单复核）
    check_readonly(tree)
    check_functions(tree)
    check_tables(tree, budget)

    cost = estimate_cost(tree, budget.table_stats)
    if budget.exceeded(cost):
        raise BudgetExceeded(f"预估成本 {cost:.2f} 超过预算 {budget.max_cost_units:.2f}")

    return tree.sql(dialect=budget.dialect), cost
