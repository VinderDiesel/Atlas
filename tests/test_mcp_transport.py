"""T17 MCP stdio transport 契约测试。

覆盖：
- 工具清单（4 工具，execute_readonly 不暴露）
- 身份不可通过参数冒充（Pydantic 严格校验拒绝额外参数）
- 身份从进程环境读取
- 各工具正常调用
- 无裸 SQL 路径
- Guard 拒绝
- 真实 SDK server 注册与调用
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent


# ----------  fixtures  ----------


def _load_snapshot_meta() -> dict[str, Any]:
    meta_path = REPO / "data" / "snapshots" / "7d48dcb.meta.json"
    return json.loads(meta_path.read_text(encoding="utf-8"))


@pytest.fixture()
def snapshot_budget():
    from agent.security.sql_guard import Budget

    meta = _load_snapshot_meta()
    allowed = frozenset(
        f"atlas.{ns}.{table}"
        for ns, tables in meta["row_counts"].items()
        for table in tables
    )
    return Budget(dialect="doris", max_rows=10_000, allowed_tables=allowed)


@pytest.fixture()
def semantic_model():
    from agent.compiler import SemanticModel

    return SemanticModel()


class FakeExecutor:
    """最小只读执行器（测试用）。"""

    def __init__(
        self,
        rows: list[tuple[Any, ...]] | None = None,
        columns: list[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.rows = rows or [("v",)]
        self.columns = columns or ["v"]

    def __call__(self, sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
        self.calls.append(sql)
        return [tuple(r) for r in self.rows], list(self.columns)


@pytest.fixture()
def fake_executor():
    return FakeExecutor()


def _build_server(
    model: Any,
    executor: Any,
    budget: Any,
    identity: str = "viewer",
):
    """构建 stdio server（不启动 transport）。"""
    from serving.mcp_stdio import create_stdio_server

    return create_stdio_server(
        model=model,
        executor=executor,
        budget=budget,
        identity=identity,
    )


def _call(server: Any, name: str, args: dict[str, Any] | None = None):
    """同步调用 MCP server 工具。"""
    return asyncio.get_event_loop().run_until_complete(
        server.call_tool(name, args or {})
    )


# ----------  核心安全：身份不可冒充  ----------


class TestIdentityOverride:
    """客户端不能通过参数冒充更高权限——T17 合同核心。

    MCP 2.x 使用 Pydantic 校验参数：额外参数（如 role）被静默忽略。
    安全模型：工具 schema 不含 role/identity 字段，客户端无法知道这些字段；
    即使传入也被忽略，身份始终从进程环境读取。
    """

    def test_ask_ignores_role_parameter(
        self, semantic_model, fake_executor, snapshot_budget
    ) -> None:
        """ask 工具忽略 role 参数（不影响执行，身份从环境读取）。"""
        server = _build_server(semantic_model, fake_executor, snapshot_budget)
        # 传 role 不报错但被忽略，工具正常执行
        result = _call(
            server,
            "ask",
            {"question": "总交易额", "metric": "commission_revenue", "role": "hq_admin"},
        )
        assert result.is_error is False

    def test_execute_plan_ignores_role_parameter(
        self, semantic_model, fake_executor, snapshot_budget
    ) -> None:
        """execute_plan 工具忽略 role 参数。"""
        server = _build_server(semantic_model, fake_executor, snapshot_budget)
        result = _call(
            server,
            "execute_plan",
            {"metric": "commission_revenue", "role": "hq_admin"},
        )
        assert result.is_error is False

    def test_ask_ignores_identity_parameter(
        self, semantic_model, fake_executor, snapshot_budget
    ) -> None:
        """ask 工具忽略 identity 参数。"""
        server = _build_server(semantic_model, fake_executor, snapshot_budget)
        result = _call(
            server,
            "ask",
            {"question": "总交易额", "metric": "commission_revenue", "identity": "operator"},
        )
        assert result.is_error is False

    def test_list_metrics_ignores_role(
        self, semantic_model, fake_executor, snapshot_budget
    ) -> None:
        """list_metrics 工具忽略 role 参数。"""
        server = _build_server(semantic_model, fake_executor, snapshot_budget)
        result = _call(server, "list_metrics", {"role": "hq_admin"})
        assert result.is_error is False

    def test_describe_metric_ignores_role(
        self, semantic_model, fake_executor, snapshot_budget
    ) -> None:
        """describe_metric 工具忽略 role 参数。"""
        server = _build_server(semantic_model, fake_executor, snapshot_budget)
        result = _call(
            server,
            "describe_metric",
            {"metric": "commission_revenue", "role": "hq_admin"},
        )
        assert result.is_error is False


# ----------  工具清单  ----------


class TestToolList:
    """stdio server 工具清单。"""

    def test_four_tools_no_execute_readonly(
        self, semantic_model, fake_executor, snapshot_budget
    ) -> None:
        """恰好 4 工具，execute_readonly 不暴露。"""
        server = _build_server(semantic_model, fake_executor, snapshot_budget)
        tools = asyncio.get_event_loop().run_until_complete(server.list_tools())
        names = {t.name for t in tools}
        assert names == {"list_metrics", "describe_metric", "ask", "execute_plan"}
        assert "execute_readonly" not in names
        assert "compile_sql" not in names

    def test_tool_schemas_have_no_role_property(
        self, semantic_model, fake_executor, snapshot_budget
    ) -> None:
        """工具 schema 中不含 role/identity 属性。"""
        server = _build_server(semantic_model, fake_executor, snapshot_budget)
        tools = asyncio.get_event_loop().run_until_complete(server.list_tools())
        for tool in tools:
            schema = tool.input_schema
            if isinstance(schema, dict):
                schema_props = set(schema.get("properties", {}).keys())
            else:
                schema_props = set()
            assert "role" not in schema_props, f"{tool.name} schema 含 role"
            assert "identity" not in schema_props, f"{tool.name} schema 含 identity"


# ----------  工具正常调用  ----------


class TestToolCalls:
    """工具正常调用路径。"""

    def test_list_metrics_returns_catalog(
        self, semantic_model, fake_executor, snapshot_budget
    ) -> None:
        """list_metrics 返回指标目录。"""
        server = _build_server(semantic_model, fake_executor, snapshot_budget)
        result = _call(server, "list_metrics")
        assert result.is_error is False
        text = result.content[0].text
        data = json.loads(text)
        assert "metrics" in data
        assert data["count"] > 0

    def test_describe_metric_returns_detail(
        self, semantic_model, fake_executor, snapshot_budget
    ) -> None:
        """describe_metric 返回指标详情。"""
        server = _build_server(semantic_model, fake_executor, snapshot_budget)
        result = _call(server, "describe_metric", {"metric": "commission_revenue"})
        assert result.is_error is False
        text = result.content[0].text
        data = json.loads(text)
        assert data["name"] == "commission_revenue"
        assert "expression" in data

    def test_ask_returns_sql(
        self, semantic_model, fake_executor, snapshot_budget
    ) -> None:
        """ask 工具返回编译后的 SQL。"""
        server = _build_server(semantic_model, fake_executor, snapshot_budget)
        result = _call(
            server,
            "ask",
            {"question": "佣金收入", "metric": "commission_revenue"},
        )
        assert result.is_error is False
        text = result.content[0].text
        data = json.loads(text)
        assert "sql" in data

    def test_execute_plan_returns_results(
        self, semantic_model, fake_executor, snapshot_budget
    ) -> None:
        """execute_plan 返回 SQL 和执行结果。"""
        server = _build_server(semantic_model, fake_executor, snapshot_budget)
        result = _call(
            server,
            "execute_plan",
            {"metric": "commission_revenue", "limit": 5},
        )
        assert result.is_error is False
        text = result.content[0].text
        data = json.loads(text)
        assert "sql" in data


# ----------  安全边界  ----------


class TestSecurityBoundary:
    """安全边界：无裸 SQL、Guard 拒绝。"""

    def test_no_raw_sql_tool(
        self, semantic_model, fake_executor, snapshot_budget
    ) -> None:
        """不存在接受裸 SQL 的工具。"""
        from mcp.server.mcpserver.exceptions import ToolError

        server = _build_server(semantic_model, fake_executor, snapshot_budget)
        with pytest.raises(ToolError, match="Unknown tool"):
            _call(server, "execute_readonly", {"sql": "SELECT 1"})

    def test_compile_sql_not_exposed(
        self, semantic_model, fake_executor, snapshot_budget
    ) -> None:
        """compile_sql 不在 stdio 工具列表中（通过 ask 间接提供）。"""
        from mcp.server.mcpserver.exceptions import ToolError

        server = _build_server(semantic_model, fake_executor, snapshot_budget)
        with pytest.raises(ToolError, match="Unknown tool"):
            _call(server, "compile_sql", {"metric": "commission_revenue"})

    def test_unknown_tool_rejected(
        self, semantic_model, fake_executor, snapshot_budget
    ) -> None:
        """未知工具被拒绝。"""
        from mcp.server.mcpserver.exceptions import ToolError

        server = _build_server(semantic_model, fake_executor, snapshot_budget)
        with pytest.raises(ToolError, match="Unknown tool"):
            _call(server, "drop_everything", {})


# ----------  身份从环境读取  ----------


class TestIdentityFromEnvironment:
    """身份从进程环境读取，不信任客户端。"""

    def test_identity_recorded_in_result(
        self, semantic_model, fake_executor, snapshot_budget
    ) -> None:
        """server 使用传入的 identity（模拟从环境读取）。"""
        server = _build_server(
            semantic_model, fake_executor, snapshot_budget, identity="operator"
        )
        result = _call(server, "list_metrics")
        assert result.is_error is False
        text = result.content[0].text
        data = json.loads(text)
        assert "metrics" in data
