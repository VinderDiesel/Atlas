"""只读 SQL 网关契约测试（安全红线，AGENTS.md N3）。

覆盖 ADR-0003 的校验链：解析 → 禁 DDL/DML → 函数黑名单 → LIMIT
→ 行级策略注入。这些用例必须全部通过才能改 sql_guard.py。
"""

from __future__ import annotations

import unittest

from sqlglot import exp

from agent.security.sql_guard import (
    Budget,
    Policy,
    UnsafeQuery,
    apply_limit,
    check_functions,
    check_readonly,
    enforce,
    parse,
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
        """谓词引用的表不在查询中直接拒绝（跨表 join 注入属 Phase 2，不静默放行）。"""
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


class TestBudget(unittest.TestCase):
    def test_exceeded(self) -> None:
        budget = Budget(max_cost_units=1.0)
        self.assertFalse(budget.exceeded(0.5))
        self.assertTrue(budget.exceeded(1.5))

    def test_default_rows(self) -> None:
        self.assertEqual(Budget().max_rows, 10_000)


if __name__ == "__main__":
    unittest.main()
