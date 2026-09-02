"""只读 SQL 网关（Atlas 安全红线）

设计原则
--------
1. **AST 层面校验，不用正则**。正则可被注释、嵌套、编码变形绕过。
2. **纵深防御**：应用层 Guard + 数据库只读账号，不依赖单一层。
3. **执行链顺序不可调换**：解析 → 禁 DDL/DML → 函数黑名单 → LIMIT
   → 时间范围 → 行级策略注入 → 成本估算 → 执行。

状态
----
骨架实现，用于 Day 19-20 启动。**未经过测试，不可直接用于生产。**
必须覆盖的边界（见 ADR-0003）：CTE、子查询、视图展开、动态 SQL、
函数黑名单、权限表达式注入。

参考 ADR：infra/adr/0003-readonly-sql-gateway.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

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
    """查询预算。超预算直接拒绝，而不是降级执行。"""

    max_rows: int = DEFAULT_MAX_ROWS
    max_days: int = DEFAULT_MAX_DAYS
    max_cost_units: float = 1.0
    dialect: str = "clickhouse"
    time_column: str = "order_date"
    allowed_tables: frozenset[str] = field(default_factory=frozenset)

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

    def rewrite(self, tree: exp.Expr) -> exp.Expr:
        """把谓词注入到 WHERE 子句（AND 连接，不覆盖已有条件）。"""
        predicate_sql = self.condition
        predicate = sqlglot.parse_one(predicate_sql, read="clickhouse")
        tree = tree.copy()
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
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_\-.:]+", text):
        raise UnsafeQuery(f"用户上下文字面量含非法字符：{text!r}")
    # 正则已排除单引号，此转义仅为纵深防御保留
    return text.replace("'", "''")


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


def apply_time_range(tree: exp.Expr, budget: Budget) -> exp.Expr:
    """强制时间范围。

    MVP 阶段：若查询中已包含对 time_column 的过滤，则跳过；
    否则追加默认范围。**这是简化实现，需要在 Day 20 补充更严谨的判定。**
    """
    tree = tree.copy()
    has_time_filter = any(
        isinstance(col, exp.Column) and col.name == budget.time_column
        for col in tree.find_all(exp.Column)
    )
    if has_time_filter:
        return tree
    # TODO(day-20): 用 AST 构造时间谓词，而不是字符串拼接
    return tree


def estimate_cost(tree: exp.Expr) -> float:
    """成本估算。

    MVP 阶段返回占位值。**必须替换为基于统计信息的真实估算**
    （可参考 ClickHouse system.parts 的行数与字节数）。
    """
    _ = tree
    return 0.0


def enforce(
    sql: str,
    policy: Policy | None = None,
    budget: Budget | None = None,
    user_context: dict[str, object] | None = None,
) -> tuple[str, float]:
    """执行完整校验链，返回（安全的 SQL，成本估算）。

    顺序不可调换 —— 见 ADR-0003。
    """
    budget = budget or Budget()
    tree = parse(sql, budget.dialect)

    check_readonly(tree)
    check_functions(tree)
    check_tables(tree, budget)

    tree = apply_limit(tree, budget.max_rows)
    tree = apply_time_range(tree, budget)

    if policy is not None:
        if user_context is not None:
            policy = Policy(
                name=policy.name,
                condition=policy.render(user_context),
                columns=policy.columns,
            )
        tree = policy.rewrite(tree)

    # 策略注入后**再次**校验，防止策略本身引入危险语句
    check_readonly(tree)
    check_functions(tree)

    cost = estimate_cost(tree)
    if budget.exceeded(cost):
        raise BudgetExceeded(f"预估成本 {cost:.2f} 超过预算 {budget.max_cost_units:.2f}")

    return tree.sql(dialect=budget.dialect), cost
