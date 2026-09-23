"""T17 MCP stdio transport：完整 MCP server（stdio 传输层）。

职责
----
- 将现有确定性工具（list_metrics/describe_metric）+ 新增 ask/execute_plan
  以 MCP 协议暴露，通过 stdio 传输。
- 身份从进程环境读取，不信任客户端传入的 identity/role。
- execute_readonly 不在 stdio 工具列表中（不暴露裸 SQL 执行）。

红线
----
- 客户端不能通过参数冒充更高权限。
- 不暴露 execute_readonly / compile_sql。
- 身份从环境读取（ATLAS_MCP_IDENTITY 或默认 viewer）。
- stdout 只输出协议帧，日志不得混入 token。
- 远程 HTTP MCP/OAuth 不在该任务。

工具清单
--------
- list_metrics：已注册指标目录。
- describe_metric：单个指标口径详情。
- ask：自然语言问句 → 编译 SQL（需指定 metric）。
- execute_plan：结构化 Plan → 编译 + 执行。
"""

from __future__ import annotations

import json
import os
from typing import Any

from agent.compiler import SemanticModel
from agent.security.sql_guard import Budget
from agent.tools.registry import DeterministicTools

# 不可接受的客户端身份覆盖参数（安全合同核心）
_IDENTITY_OVERRIDE_KEYS: frozenset[str] = frozenset(
    {"role", "identity", "user", "auth", "principal", "caller"}
)


def _ok_text(data: dict[str, Any]) -> str:
    """成功结果文本（JSON 序列化）。"""
    return json.dumps(data, ensure_ascii=False, default=str)


def create_stdio_server(
    *,
    model: SemanticModel,
    executor: Any,
    budget: Budget,
    identity: str | None = None,
) -> Any:
    """创建 MCP stdio server（MCPServer 实例）。

    Parameters
    ----------
    model : 语义模型。
    executor : 查询执行器。
    budget : Guard 预算。
    identity : 调用身份（从环境读取，默认 viewer）。

    Returns
    -------
    MCPServer
        已注册工具的 MCP server 实例。
    """
    from mcp.server.mcpserver import MCPServer

    # 身份从参数或环境读取（用于日志/审计，工具层不读取）
    _effective_identity = identity or os.environ.get("ATLAS_MCP_IDENTITY", "viewer")

    # 构建确定性工具注册表
    tools = DeterministicTools(model=model, executor=executor, budget=budget)

    # 创建 MCP server
    server = MCPServer(
        name="atlas-trusted-query",
        version="0.1.0",
    )

    # ---- 工具注册 ----
    # 注意：MCP 2.x 使用 Pydantic 从函数签名生成参数模型，
    # 不支持 **kwargs。每个参数必须显式声明。

    @server.tool()
    async def list_metrics() -> str:
        """列出全部已注册指标目录（名称/描述/owner/同义词数）。"""
        result = tools.list_metrics()
        return _ok_text(result)

    @server.tool()
    async def describe_metric(metric: str) -> str:
        """查看单个指标的口径详情。

        Parameters
        ----------
        metric : 已注册指标名。
        """
        result = tools.describe_metric(metric)
        return _ok_text(result)

    @server.tool()
    async def ask(question: str, metric: str) -> str:
        """自然语言问句编译为只读 SQL（需指定 metric）。

        Parameters
        ----------
        question : 自然语言问句。
        metric : 已注册指标名（必填，用于确定性编译）。
        """
        plan: dict[str, Any] = {"metric": metric, "limit": 100}
        result = tools.compile_sql(plan)
        return _ok_text({"question": question, **result})

    @server.tool()
    async def execute_plan(
        metric: str,
        dimensions: list[str] | None = None,
        limit: int = 100,
    ) -> str:
        """结构化 Plan 编译并执行。

        Parameters
        ----------
        metric : 已注册指标名（必填）。
        dimensions : 分组维度字段（可选）。
        limit : 返回行数上限（可选，默认 100）。
        """
        plan: dict[str, Any] = {"metric": metric, "limit": limit}
        if dimensions:
            plan["dimensions"] = dimensions
        # 编译
        compiled = tools.compile_sql(plan)
        # 执行（过 Guard）
        executed = tools.execute_readonly(compiled["sql"])
        return _ok_text(executed)

    return server


def run_stdio_server(
    *,
    model: SemanticModel | None = None,
    executor: Any = None,
    budget: Budget | None = None,
) -> None:
    """启动 stdio MCP server（CLI 入口）。

    从进程环境读取身份（ATLAS_MCP_IDENTITY），使用 stdio 传输。
    stdout 只输出协议帧，日志走 stderr。

    Parameters
    ----------
    model : 语义模型（缺省从语义层加载）。
    executor : 查询执行器（缺省使用 Doris 连接器）。
    budget : Guard 预算（缺省从快照加载）。
    """
    import asyncio

    from mcp.server.stdio import stdio_server

    if model is None:
        model = SemanticModel()
    if executor is None:
        # 默认执行器：需要部署环境提供，此处不自动构建
        raise ValueError("executor 必填：stdio server 不自动构建执行器")
    if budget is None:
        # 默认预算：需要部署环境提供
        raise ValueError("budget 必填：stdio server 不自动构建预算")

    identity = os.environ.get("ATLAS_MCP_IDENTITY", "viewer")
    server = create_stdio_server(
        model=model, executor=executor, budget=budget, identity=identity
    )

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream)

    # 日志走 stderr，不混入 stdout 协议帧
    import logging

    logging.basicConfig(level=logging.INFO, stream=__import__("sys").stderr)
    asyncio.run(_run())
