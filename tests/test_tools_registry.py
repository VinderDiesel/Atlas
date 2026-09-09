"""确定性工具四件套契约测试（Day 44）。

口径（与 agent/tools/registry.py docstring 一致）：
- 全确定性组件（语义层 YAML + Compiler + Guard），不碰网络；
- 执行器与预算 fake/快照注入（同 test_graph.py 口径：只能触碰锁定快照的表）；
- N3 红线断言：Guard 拒绝的 SQL 不达执行器，且错误信息不含被拒 SQL；
- 参数校验断言：未知键/坏类型/超域一律拒绝（Day 45 MCP 暴露的兜底层）。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from agent.compiler import SemanticModel
from agent.security.sql_guard import Budget
from agent.tools.registry import DeterministicTools, ToolError

REPO = Path(__file__).resolve().parent.parent

# 锁定快照表白名单（与评测同口径：只能触碰已锁快照的表）
_META = json.loads((REPO / "data/snapshots" / "7d48dcb.meta.json").read_text(encoding="utf-8"))
ALLOWED = frozenset(
    f"atlas.{ns}.{table}" for ns, tables in _META["row_counts"].items() for table in tables
)
BUDGET = Budget(dialect="doris", max_rows=10_000, allowed_tables=ALLOWED)
MODEL = SemanticModel()

GOLD102_PLAN = {
    "metric": "commission_revenue",
    "dimensions": ["Branch"],
    "time": {"granularity": "year", "value": 2013},
    "order_by": [{"column": "commission_revenue", "desc": True}],
    "limit": 5,
}


class FakeExecutor:
    """记录收到的 SQL（已过 Guard），返回固定结果集。"""

    def __init__(
        self,
        rows: list[tuple[Any, ...]] = (("v",),),
        columns: list[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.rows = rows
        self.columns = list(columns or ["v"])

    def __call__(self, sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
        self.calls.append(sql)
        return [tuple(r) for r in self.rows], list(self.columns)


class _Raising(FakeExecutor):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    def __call__(self, sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
        self.calls.append(sql)
        raise self.exc


class TestNoGuardNoSql(unittest.TestCase):
    def test_constructor_requires_executor_budget(self) -> None:
        """未注入执行器/预算 → fail fast（不执行未过 Guard 的 SQL）。"""
        with self.assertRaises(ValueError):
            DeterministicTools(model=MODEL, executor=None, budget=BUDGET)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            DeterministicTools(model=MODEL, executor=FakeExecutor(), budget=None)  # type: ignore[arg-type]


class TestCatalog(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = DeterministicTools(model=MODEL, executor=FakeExecutor(), budget=BUDGET)

    def test_list_metrics_20_sorted(self) -> None:
        r = self.tools.list_metrics()
        self.assertEqual(r["count"], 20)
        names = [m["name"] for m in r["metrics"]]
        self.assertEqual(names, sorted(names))
        row = next(m for m in r["metrics"] if m["name"] == "commission_revenue")
        self.assertEqual(row["owner"], "finance@atlas.local")
        self.assertGreater(row["synonym_count"], 0)

    def test_describe_metric_detail(self) -> None:
        d = self.tools.describe_metric("commission_revenue")
        self.assertEqual(d["name"], "commission_revenue")
        self.assertIn("Commission", d["expression"])
        self.assertIn("佣金", d["description"])
        self.assertIn("佣金收入", d["synonyms"])
        self.assertEqual(d["owner"], "finance@atlas.local")

    def test_describe_metric_unknown_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.tools.describe_metric("gmv_total")
        self.assertIn("指标不存在", str(ctx.exception))
        self.assertIn("commission_revenue", str(ctx.exception))
        with self.assertRaises(ToolError):
            self.tools.describe_metric(123)  # type: ignore[arg-type]


class TestCompileSql(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = DeterministicTools(model=MODEL, executor=FakeExecutor(), budget=BUDGET)

    def test_compile_gold102_shape(self) -> None:
        r = self.tools.compile_sql(GOLD102_PLAN)
        self.assertIn("LIMIT 5", r["sql"])
        self.assertIn("GROUP BY", r["sql"])
        self.assertIsInstance(r["join_chain"], list)

    def test_compile_defaults(self) -> None:
        r = self.tools.compile_sql({"metric": "cash_balance"})
        self.assertIn("LIMIT 100", r["sql"])

    def test_compile_rejects_unknown_key(self) -> None:
        """未知键拒绝：不静默忽略（防拼写漂移）。filters 已于 B1 开放，
        改用仍未开放的 having 做样本（防止“错键被当作合法”静默吞掉）。"""
        with self.assertRaises(ToolError) as ctx:
            self.tools.compile_sql({**GOLD102_PLAN, "having": []})
        self.assertIn("不支持的键", str(ctx.exception))
        self.assertIn("having", str(ctx.exception))

    def test_compile_rejects_bad_metric(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.tools.compile_sql({"metric": "revenue_unknown"})
        self.assertIn("metric", str(ctx.exception))

    def test_compile_rejects_bad_dimensions(self) -> None:
        with self.assertRaises(ToolError):
            self.tools.compile_sql({**GOLD102_PLAN, "dimensions": "Branch"})
        with self.assertRaises(ToolError) as ctx:
            self.tools.compile_sql({**GOLD102_PLAN, "dimensions": ["NoSuchDim"]})
        self.assertIn("NoSuchDim", str(ctx.exception))

    def test_compile_rejects_bad_time(self) -> None:
        with self.assertRaises(ToolError):
            self.tools.compile_sql({**GOLD102_PLAN, "time": {"granularity": "century", "value": 1}})
        with self.assertRaises(ToolError):
            self.tools.compile_sql({**GOLD102_PLAN, "time": {"granularity": "year"}})
        with self.assertRaises(ToolError):
            self.tools.compile_sql({**GOLD102_PLAN, "time": {"granularity": "year", "value": ""}})

    def test_compile_rejects_bad_order_by_and_limit(self) -> None:
        with self.assertRaises(ToolError):
            self.tools.compile_sql({**GOLD102_PLAN, "order_by": [{"desc": True}]})
        with self.assertRaises(ToolError):
            self.tools.compile_sql({**GOLD102_PLAN, "order_by": [{"column": "x", "desc": 1}]})
        for bad in (0, 1001, True, "5"):
            with self.assertRaises(ToolError):
                self.tools.compile_sql({**GOLD102_PLAN, "limit": bad})

    def test_validation_happens_before_compile(self) -> None:
        """字段层校验先于编译：坏 metric 报参数错误而非编译错误。

        说明：MVP 语义层 6 张 dwd 表图全连通（见 schema_linker docstring），
        字段层通过后编译失败路径在当前图结构下不可达——registry 保留防御
        except 转 ToolError，此处只锁定校验前置的契约。
        """
        with self.assertRaises(ToolError) as ctx:
            self.tools.compile_sql({"metric": "no_such_metric"})
        self.assertIn("参数校验失败", str(ctx.exception))


class TestCompileSqlFilters(unittest.TestCase):
    """filters 已开放（B1）：工具层形态与 Planner/Compiler 能力对齐。

    样本形态直接取黄金集已锚定样本（gold-151 度量阈值、gold-153 维度等值），
    不造新口径；这些 Plan 过去只能由 Planner 产出，现在工具/MCP 消费方也能传入。
    """

    def setUp(self) -> None:
        self.tools = DeterministicTools(model=MODEL, executor=FakeExecutor(), budget=BUDGET)

    def test_dimension_filter_compiles_to_where(self) -> None:
        """gold-153 形态：维度等值 → WHERE 谓词（值以字面量入 AST，非拼接）。"""
        r = self.tools.compile_sql(
            {
                "metric": "commission_revenue",
                "dimensions": ["Branch"],
                "time": {"granularity": "year", "value": 2014},
                "filters": [{"column": "Tier", "op": "=", "value": 3}],
                "limit": 5,
            }
        )
        self.assertIn("WHERE", r["sql"])
        self.assertIn("Tier", r["sql"])

    def test_metric_threshold_compiles_to_having(self) -> None:
        """gold-151 形态：column == metric 名 → 度量阈值走 HAVING（不是 WHERE）。"""
        r = self.tools.compile_sql(
            {
                "metric": "commission_revenue",
                "dimensions": ["Branch"],
                "time": {"granularity": "year", "value": 2013},
                "filters": [{"column": "commission_revenue", "op": ">", "value": 10000000}],
                "limit": 5,
            }
        )
        self.assertIn("HAVING", r["sql"])
        self.assertIn("10000000", r["sql"])

    def test_exclusion_filter_supported(self) -> None:
        """gold-150 形态：!= 排除（字符串值）。"""
        r = self.tools.compile_sql(
            {
                "metric": "total_trade_quantity",
                "time": {"granularity": "year", "value": 2013},
                "filters": [
                    {"column": "Branch", "op": "!=", "value": "IEMJHuQgCPDHCwwJkgQQeaqGvzMcVD"}
                ],
            }
        )
        self.assertIn("Branch", r["sql"])

    def test_rejects_malformed_filters(self) -> None:
        """结构拒绝面：非数组 / 缺键 / 多余键 / 非法 op / 伪列 / 嵌套值 / bool / 空串。"""
        base = {"metric": "commission_revenue"}
        bad: list[tuple[Any, str]] = [
            ("not-a-list", "对象数组"),
            ([{"column": "Tier", "op": "="}], "恰为"),  # 缺 value
            ([{"column": "Tier", "op": "=", "value": 3, "sql": "1=1"}], "恰为"),  # 多余键
            ([{"column": "Tier", "op": "LIKE", "value": "3"}], "filters.op"),
            ([{"column": "NoSuchCol", "op": "=", "value": 1}], "已注册字段"),
            ([{"column": "Tier", "op": "=", "value": {"nested": 1}}], "数字或非空字符串"),
            ([{"column": "Tier", "op": "=", "value": True}], "数字或非空字符串"),
            ([{"column": "Tier", "op": "=", "value": "  "}], "空字符串"),
        ]
        for value, expect in bad:
            with self.subTest(filters=value):
                with self.assertRaises(ToolError) as ctx:
                    self.tools.compile_sql({**base, "filters": value})
                self.assertIn(expect, str(ctx.exception))

    def test_none_filters_treated_as_empty(self) -> None:
        """filters=None 等价于缺省（与 dimensions/time 同形态容错）。"""
        r = self.tools.compile_sql({"metric": "cash_balance", "filters": None})
        self.assertNotIn("WHERE", r["sql"])


class TestExecuteReadonly(unittest.TestCase):
    def setUp(self) -> None:
        self.executor = FakeExecutor(rows=(("a", 1), ("b", 2)), columns=("Branch", "Revenue"))
        self.tools = DeterministicTools(model=MODEL, executor=self.executor, budget=BUDGET)

    def test_execute_guarded_sql_reaches_executor(self) -> None:
        r = self.tools.execute_readonly(
            "SELECT SK_BrokerID, SUM(Commission) AS Revenue"
            " FROM atlas.dwd.fact_trades GROUP BY SK_BrokerID"
        )
        self.assertEqual(r["row_count"], 2)
        self.assertEqual(r["columns"], ["Branch", "Revenue"])
        self.assertEqual(r["rows"], [["a", 1], ["b", 2]])
        self.assertIn("LIMIT", r["sql"], "Guard 注入 LIMIT 后才执行")
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.executor.calls[0], r["sql"], "执行器收到的是 Guard 出口 SQL")

    def test_blocked_sql_never_reaches_executor(self) -> None:
        """N3：白名单外表 → Guard 拒绝 → 不执行，错误不含被拒 SQL。"""
        executor = FakeExecutor()
        deny_budget = Budget(
            dialect="doris", max_rows=10_000, allowed_tables=frozenset({"atlas.dwd.other"})
        )
        tools = DeterministicTools(model=MODEL, executor=executor, budget=deny_budget)
        sql = "SELECT * FROM atlas.dwd.fact_trades"
        with self.assertRaises(ToolError) as ctx:
            tools.execute_readonly(sql)
        self.assertIn("只读校验拒绝", str(ctx.exception))
        self.assertIn("UnsafeQuery", str(ctx.exception))
        self.assertNotIn(sql, str(ctx.exception), "被拒 SQL 不得回显")
        self.assertEqual(executor.calls, [])

    def test_dml_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.tools.execute_readonly("DROP TABLE atlas.dwd.fact_trades")
        self.assertIn("只读校验拒绝", str(ctx.exception))
        self.assertEqual(self.executor.calls, [])

    def test_executor_failure_reported(self) -> None:
        tools = DeterministicTools(
            model=MODEL, executor=_Raising(RuntimeError("db down")), budget=BUDGET
        )
        with self.assertRaises(ToolError) as ctx:
            tools.execute_readonly("SELECT 1")
        self.assertIn("执行失败", str(ctx.exception))
        self.assertIn("db down", str(ctx.exception))

    def test_non_string_sql_rejected(self) -> None:
        with self.assertRaises(ToolError):
            self.tools.execute_readonly("  ")  # type: ignore[arg-type]
        with self.assertRaises(ToolError):
            self.tools.execute_readonly(42)  # type: ignore[arg-type]

    def test_call_log_audit(self) -> None:
        """每次成功调用留痕（工具名 + 参数摘要），供审计/作用域控制。"""
        self.tools.execute_readonly("SELECT 1")
        self.tools.describe_metric("cash_balance")
        self.assertEqual(
            [c.tool for c in self.tools.calls],
            ["execute_readonly", "describe_metric"],
        )
        self.assertIn("sql", self.tools.calls[0].args)


if __name__ == "__main__":
    unittest.main()
