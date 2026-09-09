"""MCP 风格工具服务契约测试（Day 45）。

口径（与 agent/tools/mcp_server.py docstring 一致）：
- 调用结果不裸抛：成功 isError=False；失败 isError=True + error.type 分类
  （unknown_tool / scope_denied / validation_error / tool_error）；
- 作用域：list_tools 只暴露允许工具（被禁工具不可见也不可调）；
- execute_readonly 默认禁用（scope_denied），显式 allow_execute_sql 才放行；
- 参数校验（JSON Schema additionalProperties=false）在 registry 真身前拦截；
- N3：Guard 拒绝的 SQL 不达执行器（fake 注入同快照口径）。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from agent.compiler import SemanticModel
from agent.security.sql_guard import Budget
from agent.tools.mcp_server import McpToolServer, Scope
from agent.tools.registry import DeterministicTools

REPO = Path(__file__).resolve().parent.parent

_META = json.loads((REPO / "data/snapshots" / "7d48dcb.meta.json").read_text(encoding="utf-8"))
ALLOWED = frozenset(
    f"atlas.{ns}.{table}" for ns, tables in _META["row_counts"].items() for table in tables
)
BUDGET = Budget(dialect="doris", max_rows=10_000, allowed_tables=ALLOWED)
MODEL = SemanticModel()


class FakeExecutor:
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


def make_server(scope: Scope | None = None, executor: FakeExecutor | None = None):
    tools = DeterministicTools(model=MODEL, executor=executor or FakeExecutor(), budget=BUDGET)
    return McpToolServer(tools, scope)


def text_of(result: dict[str, Any]) -> dict[str, Any]:
    """MCP 结果 content[0].text 反序列化（断言用）。"""
    return json.loads(str(result["content"][0]["text"]))


class TestListTools(unittest.TestCase):
    def test_four_tools_with_schema(self) -> None:
        r = make_server(Scope(allow_execute_sql=True)).list_tools()
        names = [t["name"] for t in r["tools"]]
        self.assertEqual(
            names,
            ["list_metrics", "describe_metric", "compile_sql", "execute_readonly"],
        )
        compile_spec = next(t for t in r["tools"] if t["name"] == "compile_sql")
        self.assertTrue(compile_spec["inputSchema"]["additionalProperties"] is False)
        describe = next(t for t in r["tools"] if t["name"] == "describe_metric")
        self.assertEqual(describe["inputSchema"]["required"], ["metric"])

    def test_default_scope_hides_execute_readonly(self) -> None:
        """默认作用域：execute_readonly 不可见（最高风险工具不暴露）。"""
        r = make_server().list_tools()
        names = [t["name"] for t in r["tools"]]
        self.assertNotIn("execute_readonly", names)
        self.assertIn("compile_sql", names)

    def test_scope_subset_only_visible(self) -> None:
        r = make_server(Scope(allowed_tools=frozenset({"describe_metric"}))).list_tools()
        self.assertEqual([t["name"] for t in r["tools"]], ["describe_metric"])

    def test_scope_execute_visible_when_allowed(self) -> None:
        r = make_server(Scope(allow_execute_sql=True)).list_tools()
        names = [t["name"] for t in r["tools"]]
        self.assertIn("execute_readonly", names)


class TestHappyPath(unittest.TestCase):
    def setUp(self) -> None:
        self.executor = FakeExecutor()
        self.server = make_server(Scope(allow_execute_sql=True), self.executor)

    def test_call_describe_metric(self) -> None:
        r = self.server.call_tool("describe_metric", {"metric": "commission_revenue"})
        self.assertFalse(r["isError"])
        data = text_of(r)
        self.assertIn("Commission", data["expression"])
        self.assertIn("佣金收入", data["synonyms"])

    def test_call_compile_sql(self) -> None:
        args = {
            "metric": "commission_revenue",
            "dimensions": ["Branch"],
            "time": {"granularity": "year", "value": 2013},
            "limit": 5,
        }
        r = self.server.call_tool("compile_sql", args)
        self.assertFalse(r["isError"])
        self.assertIn("LIMIT 5", text_of(r)["sql"])

    def test_call_compile_sql_with_filters(self) -> None:
        """filters 已经 MCP 层开放（schema 放行 + registry 编译出谓词）。"""
        args = {
            "metric": "commission_revenue",
            "dimensions": ["Branch"],
            "time": {"granularity": "year", "value": 2014},
            "filters": [{"column": "Tier", "op": "=", "value": 3}],
            "limit": 5,
        }
        r = self.server.call_tool("compile_sql", args)
        self.assertFalse(r["isError"], str(r))
        sql = text_of(r)["sql"]
        self.assertIn("WHERE", sql)
        self.assertIn("Tier", sql)

    def test_call_execute_readonly_guarded(self) -> None:
        sql = "SELECT SK_BrokerID FROM atlas.dwd.fact_trades LIMIT 3"
        r = self.server.call_tool("execute_readonly", {"sql": sql})
        self.assertFalse(r["isError"])
        data = text_of(r)
        self.assertEqual(data["row_count"], 1)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertIn("LIMIT 3", data["sql"])

    def test_call_list_metrics_no_args(self) -> None:
        r = self.server.call_tool("list_metrics")
        self.assertFalse(r["isError"])
        self.assertEqual(text_of(r)["count"], 20)
        # arguments=None 与 {} 等价
        self.assertFalse(self.server.call_tool("list_metrics", None)["isError"])


class TestValidationAndScope(unittest.TestCase):
    def setUp(self) -> None:
        self.executor = FakeExecutor()
        self.server = make_server(Scope(allow_execute_sql=True), self.executor)

    def _error_type(self, name: str, args: dict[str, Any] | None) -> str:
        r = self.server.call_tool(name, args)
        self.assertTrue(r["isError"], f"{name} 应失败：{r}")
        return str(r["error"]["type"])

    def test_unknown_tool(self) -> None:
        self.assertEqual(self._error_type("drop_everything", {}), "unknown_tool")

    def test_scope_denied_default_execute(self) -> None:
        """默认作用域 call execute_readonly → scope_denied（不达 registry/执行器）。"""
        executor = FakeExecutor()
        server = make_server(None, executor)
        r = server.call_tool("execute_readonly", {"sql": "SELECT 1"})
        self.assertEqual(str(r["error"]["type"]), "scope_denied")
        self.assertEqual(executor.calls, [])

    def test_scope_denied_subset(self) -> None:
        server = make_server(Scope(allowed_tools=frozenset({"describe_metric"})))
        r = server.call_tool("compile_sql", {"metric": "cash_balance"})
        self.assertEqual(str(r["error"]["type"]), "scope_denied")

    def test_validation_error_missing_required(self) -> None:
        self.assertEqual(self._error_type("describe_metric", {}), "validation_error")

    def test_validation_error_unknown_key(self) -> None:
        """additionalProperties=false：未知键在 schema 层拒绝（不达 registry）。"""
        self.assertEqual(
            self._error_type("describe_metric", {"metric": "cash_balance", "extra": 1}),
            "validation_error",
        )

    def test_validation_error_filter_shape(self) -> None:
        """filters 形态在 schema 层就被拦（不达 registry）：非法 op / 非对象元素
        / 缺键 / 嵌套值 / 未知键（裸 SQL 片段入口）。"""
        bad: list[Any] = [
            [{"column": "Tier", "op": "LIKE", "value": "3"}],  # 非法 op
            ["Tier=3"],  # 元素非对象
            [{"column": "Tier", "op": "="}],  # 缺 value
            [{"column": "Tier", "op": "=", "value": {"nested": 1}}],  # 嵌套值
            [{"column": "Tier", "op": "=", "value": "", "sql": "1=1"}],  # 空串+未知键
        ]
        for value in bad:
            with self.subTest(filters=value):
                self.assertEqual(
                    self._error_type("compile_sql", {"metric": "cash_balance", "filters": value}),
                    "validation_error",
                )

    def test_validation_error_bad_types(self) -> None:
        self.assertEqual(self._error_type("compile_sql", {"metric": 123}), "validation_error")
        self.assertEqual(
            self._error_type(
                "compile_sql",
                {"metric": "cash_balance", "time": {"granularity": "century", "value": 1}},
            ),
            "validation_error",
        )
        self.assertEqual(
            self._error_type("compile_sql", {"metric": "cash_balance", "limit": 0}),
            "validation_error",
        )
        self.assertEqual(self._error_type("execute_readonly", {"sql": 42}), "validation_error")

    def test_tool_error_guard_rejected(self) -> None:
        """schema 通过但 Guard 拒绝（非白名单表）→ tool_error，SQL 不达执行器。"""
        sql = "SELECT * FROM atlas.dwd.no_such_table"
        r = self.server.call_tool("execute_readonly", {"sql": sql})
        self.assertEqual(str(r["error"]["type"]), "tool_error")
        self.assertIn("只读校验拒绝", str(r["error"]["message"]))
        self.assertEqual(self.executor.calls, [])
        self.assertNotIn(sql, str(r["error"]["message"]))

    def test_tool_error_unknown_metric(self) -> None:
        r = self.server.call_tool("describe_metric", {"metric": "no_such"})
        self.assertEqual(str(r["error"]["type"]), "tool_error")
        self.assertIn("指标不存在", str(r["error"]["message"]))


if __name__ == "__main__":
    unittest.main()
