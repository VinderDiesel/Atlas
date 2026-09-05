"""只读 SQL 网关契约测试（安全红线，AGENTS.md N3）。

覆盖 ADR-0003 的校验链：解析 → 禁 DDL/DML → 函数黑名单 → LIMIT
→ 行级策略注入（含 Phase 2 跨表谓词沿语义模型 join 图补 LEFT JOIN）。
这些用例必须全部通过才能改 sql_guard.py。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from sqlglot import exp

from agent.compiler import Dataset, Relationship, SemanticModel
from agent.security.sql_guard import (
    Budget,
    Policy,
    UnsafeQuery,
    apply_limit,
    apply_time_range,
    check_functions,
    check_readonly,
    enforce,
    estimate_cost,
    parse,
)

# 真实语义模型（atlas_finance.ossie.yaml）：Guard 跨表 join 注入的鸭子接口输入。
# 若 semantic YAML 的表/关系改名，以下用例会失败——这是有意的回归锁。
MODEL = SemanticModel()

# 迷你 join 图：仅 fact_trades → dim_account 一条边，其余表不可达（无路径拒绝用例）。
FAKE_MODEL = SimpleNamespace(
    datasets={
        "fact_trades": Dataset(name="fact_trades", source="atlas.dwd.fact_trades", fields={}),
        "dim_account": Dataset(name="dim_account", source="atlas.dwd.dim_account", fields={}),
    },
    relationships=[
        Relationship(
            name="trades_to_account",
            from_ds="fact_trades",
            to_ds="dim_account",
            from_columns=("SK_AccountID",),
            to_columns=("SK_AccountID",),
        )
    ],
)


class TestReadonlyGate(unittest.TestCase):
    """禁 DDL / DML / 管理语句（N3 的核心）。"""

    FORBIDDEN_SQL = [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN x INT",
        "GRANT SELECT ON t TO u",
        "REVOKE SELECT ON t FROM u",
        "COPY t TO 's3://x'",
        "CREATE TABLE t (a INT)",
        "MERGE INTO t USING s ON t.a = s.a WHEN MATCHED THEN UPDATE SET a = s.a",
        "TRUNCATE TABLE t",
        "SET x = 1",
        "BEGIN TRANSACTION",
    ]

    def test_allow_simple_select(self) -> None:
        tree = parse("SELECT a FROM t", "clickhouse")
        check_readonly(tree)
        check_functions(tree)

    def test_reject_forbidden_statements(self) -> None:
        for sql in self.FORBIDDEN_SQL:
            with self.subTest(sql=sql):
                tree = parse(sql, "clickhouse")
                with self.assertRaises(UnsafeQuery):
                    check_readonly(tree)


class TestFunctionBlacklist(unittest.TestCase):
    def test_reject_sleep(self) -> None:
        tree = parse("SELECT sleep(5)", "clickhouse")
        with self.assertRaises(UnsafeQuery):
            check_functions(tree)

    def test_reject_pg_sleep(self) -> None:
        tree = parse("SELECT pg_sleep(5)", "clickhouse")
        with self.assertRaises(UnsafeQuery):
            check_functions(tree)

    def test_allow_aggregate(self) -> None:
        tree = parse("SELECT sum(a) FROM t", "clickhouse")
        check_functions(tree)


class TestLimitEnforcement(unittest.TestCase):
    def test_force_limit_when_missing(self) -> None:
        tree = apply_limit(parse("SELECT a FROM t", "clickhouse"), max_rows=10_000)
        self.assertIsNotNone(tree.find(exp.Limit))
        self.assertIn("LIMIT 10000", tree.sql())

    def test_keep_smaller_existing_limit(self) -> None:
        tree = apply_limit(parse("SELECT a FROM t LIMIT 5", "clickhouse"), max_rows=10_000)
        self.assertIn("LIMIT 5", tree.sql())

    def test_shrink_larger_existing_limit(self) -> None:
        tree = apply_limit(parse("SELECT a FROM t LIMIT 999999", "clickhouse"), max_rows=10_000)
        self.assertIn("LIMIT 10000", tree.sql())


class TestRowPolicy(unittest.TestCase):
    def test_policy_injected_as_and(self) -> None:
        policy = Policy(name="rp_branch", condition="branch = '{{ user.branch }}'", columns=("*",))
        sql, _ = enforce(
            "SELECT * FROM trades",
            policy=policy,
            user_context={"branch": "east"},
        )
        self.assertIn("branch = 'east'", sql)

    def test_unrendered_placeholder_rejected(self) -> None:
        policy = Policy(name="rp_branch", condition="branch = '{{ user.branch }}'", columns=("*",))
        with self.assertRaises(UnsafeQuery):
            enforce("SELECT * FROM trades", policy=policy, user_context={})

    def test_illegal_literal_chars_rejected(self) -> None:
        policy = Policy(name="rp_x", condition="x = '{{ user.x }}'", columns=("*",))
        with self.assertRaises(UnsafeQuery):
            enforce("SELECT * FROM trades", policy=policy, user_context={"x": "a; DROP TABLE t"})

    def test_clean_policy_still_readonly(self) -> None:
        """策略注入后再校验（防策略本身引入危险语句）。"""
        policy = Policy(name="rp_x", condition="x = '{{ user.x }}'", columns=("*",))
        sql, _ = enforce(
            "SELECT * FROM trades WHERE y = 1",
            policy=policy,
            user_context={"x": "safe"},
        )
        self.assertIn("x = 'safe'", sql)
        self.assertIn("y = 1", sql)

    def test_qualified_column_rewritten_to_alias(self) -> None:
        """Day 25：谓词按物理表名书写，注入时改写为编译 SQL 的实际别名。

        别名遮蔽后原表名不可用（实测：FROM atlas.dwd.dim_broker AS db 下
        写 WHERE dim_broker.branch=... 报 Unknown table），Guard 必须对齐。
        """
        policy = Policy(
            name="rp_branch",
            condition="dim_broker.branch = '{{ user.branch }}'",
            columns=("*",),
        )
        sql, _ = enforce(
            "SELECT * FROM atlas.dwd.dim_broker AS db WHERE region = 1",
            policy=policy,
            user_context={"branch": "east"},
        )
        self.assertIn("db.branch = 'east'", sql)
        self.assertNotIn("dim_broker.branch", sql)
        self.assertIn("region = 1", sql)  # 原有 WHERE 不丢

    def test_policy_table_not_in_query_rejected(self) -> None:
        """无模型时谓词跨表引用仍拒绝（Phase 2 后：无模型无法补 join 图，安全底线不放开）。

        跨表注入需要语义模型（model）提供 join 图——缺模型时即使谓词表真实存在
        也拒绝，不静默放行；提供 model 的形态见 TestCrossTableJoinInjection。
        """
        policy = Policy(
            name="rp_branch",
            condition="dim_broker.branch = '{{ user.branch }}'",
            columns=("*",),
        )
        with self.assertRaises(UnsafeQuery):
            enforce(
                "SELECT * FROM trades",
                policy=policy,
                user_context={"branch": "east"},
            )

    def test_alias_equal_to_table_name_keeps_original(self) -> None:
        """编译器产物别名=原名（AS dim_broker）时无需改写。"""
        policy = Policy(
            name="rp_tier",
            condition="dim_customer.tier <= {{ user.max_tier }}",
            columns=("*",),
        )
        sql, _ = enforce(
            "SELECT * FROM atlas.dwd.dim_customer AS dim_customer",
            policy=policy,
            user_context={"max_tier": 3},
        )
        self.assertIn("dim_customer.tier <= 3", sql)


class TestCrossTableJoinInjection(unittest.TestCase):
    """Phase 2（KL #14 收窄）：策略谓词引用表不在查询中时沿语义模型 join 图补表。

    查询形态与编译器产物一致（FROM atlas.dwd.<表> AS <表名>）；行级策略经
    serving/rls_verify.py 三角色链路调 enforce(model=...) 获得跨表能力。
    """

    def test_one_hop_broker_injected(self) -> None:
        """查询只含 fact_trades，策略引用 dim_broker.branch → 补单跳 LEFT JOIN。"""
        policy = Policy(
            name="rp_broker",
            condition="dim_broker.branch = '{{ user.branch }}'",
            columns=("*",),
        )
        sql, _ = enforce(
            "SELECT * FROM atlas.dwd.fact_trades AS fact_trades",
            policy=policy,
            user_context={"branch": "east"},
            model=MODEL,
        )
        self.assertIn("LEFT JOIN atlas.dwd.dim_broker AS dim_broker", sql)
        self.assertIn("ON fact_trades.SK_BrokerID = dim_broker.SK_BrokerID", sql)
        self.assertIn("dim_broker.branch = 'east'", sql)

    def test_two_hop_customer_injected(self) -> None:
        """查询只含 fact_cash_balances，策略引用 dim_customer.tier → 补双跳 LEFT JOIN。

        真实图最短路径：fact_cash_balances → dim_account → dim_customer
        （cash 无直达 customer 的边）。
        """
        policy = Policy(
            name="rp_tier",
            condition="dim_customer.tier <= {{ user.max_tier }}",
            columns=("*",),
        )
        sql, _ = enforce(
            "SELECT * FROM atlas.dwd.fact_cash_balances AS fact_cash_balances",
            policy=policy,
            user_context={"max_tier": 3},
            model=MODEL,
        )
        self.assertIn("LEFT JOIN atlas.dwd.dim_account AS dim_account", sql)
        self.assertIn("ON fact_cash_balances.SK_AccountID = dim_account.SK_AccountID", sql)
        self.assertIn("LEFT JOIN atlas.dwd.dim_customer AS dim_customer", sql)
        self.assertIn("ON dim_account.SK_CustomerID = dim_customer.SK_CustomerID", sql)
        self.assertIn("dim_customer.tier <= 3", sql)

    def test_combined_two_policy_tables_injected(self) -> None:
        """策略同时引用两维表（AND）→ 各补一条 LEFT JOIN，两条件并入 WHERE。"""
        policy = Policy(
            name="rp_combined",
            condition=(
                "dim_broker.branch = '{{ user.branch }}' "
                "AND dim_customer.tier <= {{ user.max_tier }}"
            ),
            columns=("*",),
        )
        sql, _ = enforce(
            "SELECT * FROM atlas.dwd.fact_trades AS fact_trades",
            policy=policy,
            user_context={"branch": "east", "max_tier": 3},
            model=MODEL,
        )
        self.assertIn("LEFT JOIN atlas.dwd.dim_broker AS dim_broker", sql)
        self.assertIn("LEFT JOIN atlas.dwd.dim_customer AS dim_customer", sql)
        self.assertIn("dim_broker.branch = 'east'", sql)
        self.assertIn("dim_customer.tier <= 3", sql)
        # join 注入不改动原有 SELECT/FROM 目标表
        self.assertIn("FROM atlas.dwd.fact_trades AS fact_trades", sql)

    def test_original_where_kept_and_anded(self) -> None:
        """查询原有 WHERE 保留，策略谓词以 AND 并入（不覆盖）。"""
        policy = Policy(name="rp_broker", condition="dim_broker.branch = 'east'", columns=("*",))
        sql, _ = enforce(
            "SELECT * FROM atlas.dwd.fact_trades AS fact_trades" " WHERE fact_trades.Quantity > 0",
            policy=policy,
            user_context={},
            model=MODEL,
        )
        self.assertIn("fact_trades.Quantity > 0", sql)
        self.assertIn("dim_broker.branch = 'east'", sql)

    def test_unreachable_policy_table_rejected(self) -> None:
        """迷你 join 图（无 customer 关系）中引用 dim_customer → 无路径拒绝。

        真实模型所有维表经 dim_account 枢纽互达，不可达场景只能以受控图构造；
        这正是 Guard 鸭子接口（datasets/relationships）的设计用途。
        """
        policy = Policy(name="rp_tier", condition="dim_customer.tier <= 3", columns=("*",))
        with self.assertRaises(UnsafeQuery) as ctx:
            enforce(
                "SELECT * FROM atlas.dwd.fact_trades AS fact_trades",
                policy=policy,
                user_context={},
                model=FAKE_MODEL,
            )
        self.assertIn("无合法 join 路径", str(ctx.exception))

    def test_malicious_policy_rejected_after_join(self) -> None:
        """注入后二次校验：跨表策略自身含黑名单函数（sleep）→ 拒绝放行。"""
        policy = Policy(
            name="rp_malicious",
            condition="dim_broker.branch = 'x' OR sleep(1) = 0",
            columns=("*",),
        )
        with self.assertRaises(UnsafeQuery) as ctx:
            enforce(
                "SELECT * FROM atlas.dwd.fact_trades AS fact_trades",
                policy=policy,
                user_context={},
                model=MODEL,
            )
        self.assertIn("禁止的函数：sleep", str(ctx.exception))

    def test_injected_join_table_rechecked_against_whitelist(self) -> None:
        """补表后的表白名单复核：注入表不在白名单 → 拒绝。

        白名单只放行 fact_trades 时，LEFT JOIN 补入的 dim_broker 必须被
        注入后 check_tables 拦下（防策略借注入绕过表白名单）。
        """
        budget = Budget(allowed_tables=frozenset({"atlas.dwd.fact_trades"}))
        policy = Policy(name="rp_broker", condition="dim_broker.branch = 'x'", columns=("*",))
        with self.assertRaises(UnsafeQuery) as ctx:
            enforce(
                "SELECT * FROM atlas.dwd.fact_trades AS fact_trades",
                policy=policy,
                user_context={},
                model=MODEL,
                budget=budget,
            )
        self.assertIn("表不在白名单内：atlas.dwd.dim_broker", str(ctx.exception))


class TestBudget(unittest.TestCase):
    def test_exceeded(self) -> None:
        budget = Budget(max_cost_units=1.0)
        self.assertFalse(budget.exceeded(0.5))
        self.assertTrue(budget.exceeded(1.5))

    def test_default_rows(self) -> None:
        self.assertEqual(Budget().max_rows, 10_000)


class TestTimeRangeEnforcement(unittest.TestCase):
    """apply_time_range 真实注入默认时间窗（纵深防御，非空操作）。"""

    # 迷你模型：声明时间维表 dim_date，物理时间列 CalendarDate
    FAKE_MODEL = SimpleNamespace(
        time_dimension={"table": "dim_date", "columns": {"date": "CalendarDate"}}
    )

    def test_injects_default_window_when_time_table_joined(self) -> None:
        """查询 join 时间维表且无时间谓词 → 强制补下界。"""
        sql, cost = enforce(
            "SELECT f.x FROM atlas.dwd.fact_trades AS fact_trades "
            "LEFT JOIN atlas.dwd.dim_date AS dim_date ON fact_trades.d = dim_date.d",
            model=self.FAKE_MODEL,
        )
        self.assertIn("dim_date.CalendarDate", sql)
        self.assertIn("INTERVAL", sql)
        self.assertIn("CURRENT_DATE", sql)

    def test_skips_when_time_predicate_present(self) -> None:
        """已有时间谓词 → 不重复注入。"""
        sql, _ = enforce(
            "SELECT f.x FROM atlas.dwd.fact_trades AS fact_trades "
            "LEFT JOIN atlas.dwd.dim_date AS dim_date ON fact_trades.d = dim_date.d "
            "WHERE dim_date.CalendarDate = DATE '2013-01-01'",
            model=self.FAKE_MODEL,
        )
        self.assertNotIn("INTERVAL", sql)

    def test_no_injection_without_time_table(self) -> None:
        """查询未 join 时间维表 → 不强行补表（防放大攻击面）。"""
        sql, _ = enforce(
            "SELECT * FROM atlas.dwd.fact_trades AS fact_trades", model=self.FAKE_MODEL
        )
        self.assertNotIn("INTERVAL", sql)

    def test_window_disabled_when_zero(self) -> None:
        budget = Budget(default_time_window_days=0)
        out = apply_time_range(
            parse(
                "SELECT f.x FROM atlas.dwd.fact_trades AS fact_trades "
                "LEFT JOIN atlas.dwd.dim_date AS dim_date ON fact_trades.d = dim_date.d",
                "clickhouse",
            ),
            budget,
            model=self.FAKE_MODEL,
        )
        self.assertNotIn("INTERVAL", out.sql())


class TestCostEstimate(unittest.TestCase):
    """estimate_cost 返回真实启发式（非恒 0 占位）。"""

    def test_returns_real_number_without_stats(self) -> None:
        tree = parse("SELECT a FROM t1 JOIN t2 ON t1.x = t2.x", "clickhouse")
        self.assertAlmostEqual(estimate_cost(tree), 2.0 / 8.0)

    def test_uses_table_stats_when_provided(self) -> None:
        tree = parse("SELECT a FROM atlas.dwd.fact_trades AS ft", "clickhouse")
        stats = {"atlas.dwd.fact_trades": 2_940_000.0}
        # 294 万行 → 2.94 百万行单位
        self.assertAlmostEqual(estimate_cost(tree, stats), 2.94)

    def test_budget_rejects_wide_scan(self) -> None:
        budget = Budget(max_cost_units=1.0, table_stats={"atlas.dwd.a": 9_000_000.0})
        tree = parse("SELECT a FROM atlas.dwd.a AS a", "clickhouse")
        self.assertTrue(budget.exceeded(estimate_cost(tree, budget.table_stats)))


if __name__ == "__main__":
    unittest.main()
