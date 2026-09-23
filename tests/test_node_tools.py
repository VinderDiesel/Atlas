"""T11 节点工具代理（ToolBroker）：节点类型 → 工具权限、预算追踪、敏感度传播。

设计口径
--------
- ToolBroker 是节点与底层 DeterministicTools 之间的**受保护中间层**：
  每个节点类型只能调用其被授权的工具子集（understand 不能执行 SQL）。
- 预算追踪：每次调用消耗 tool_calls 配额；超出即 blocked，不无限重试。
- 敏感度传播：输入问句的敏感度向工具输出传播（敏感问句禁止云工具）。
"""

from __future__ import annotations

import pytest

from agent.compiler import SemanticModel
from agent.security.sql_guard import Budget

# 使用真实金融语义模型（与 test_tools_registry.py 同源口径）
MODEL = SemanticModel()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_budget() -> Budget:
    return Budget(dialect="doris", allowed_tables=frozenset({"atlas.dwd.fact_trade"}))


@pytest.fixture()
def dummy_executor():
    """返回固定行的执行器。"""

    def _execute(sql: str):
        return [(100.0,)], ["amount"]

    return _execute


# ---------------------------------------------------------------------------
# T11a 红测：understand 节点不能执行 SQL
# ---------------------------------------------------------------------------


def test_understanding_cannot_execute_sql(sample_budget, dummy_executor):
    """understand 节点即使请求 execute_readonly 也被 broker 拒绝。"""
    from agent.tools.broker import ToolBroker

    broker = ToolBroker(
        model=MODEL,
        executor=dummy_executor,
        budget=sample_budget,
        node_type="understand",
    )
    result = broker.call("execute_readonly", {"sql": "SELECT 1"})
    assert result.status == "blocked"
    assert result.reason_code == "tool_not_allowed"


def test_understand_can_list_and_describe(sample_budget, dummy_executor):
    """understand 节点允许 list_metrics 和 describe_metric（只读目录查询）。"""
    from agent.tools.broker import ToolBroker

    broker = ToolBroker(
        model=MODEL,
        executor=dummy_executor,
        budget=sample_budget,
        node_type="understand",
    )
    result = broker.call("list_metrics", {})
    assert result.status == "ok"
    assert result.result is not None
    assert "metrics" in result.result

    result = broker.call("describe_metric", {"metric": list(MODEL.metrics)[0]})
    assert result.status == "ok"
    assert result.result["name"] == list(MODEL.metrics)[0]


def test_understand_can_compile_but_not_execute(sample_budget, dummy_executor):
    """understand 节点允许 compile_sql（生成 SQL 不执行），但禁止 execute_readonly。"""
    from agent.tools.broker import ToolBroker

    broker = ToolBroker(
        model=MODEL,
        executor=dummy_executor,
        budget=sample_budget,
        node_type="understand",
    )
    first_metric = list(MODEL.metrics)[0]
    result = broker.call("compile_sql", {"plan": {"metric": first_metric}})
    assert result.status == "ok"
    assert "sql" in result.result

    result = broker.call("execute_readonly", {"sql": "SELECT 1"})
    assert result.status == "blocked"
    assert result.reason_code == "tool_not_allowed"


# ---------------------------------------------------------------------------
# T11c 红测：未知工具 / 参数越权 / 预算
# ---------------------------------------------------------------------------


def test_unknown_tool_is_blocked(sample_budget, dummy_executor):
    """未注册的工具一律 blocked。"""
    from agent.tools.broker import ToolBroker

    broker = ToolBroker(
        model=MODEL,
        executor=dummy_executor,
        budget=sample_budget,
        node_type="understand",
    )
    result = broker.call("nonexistent_tool", {})
    assert result.status == "blocked"
    assert result.reason_code == "unknown_tool"


def test_execute_node_has_full_access(sample_budget, dummy_executor):
    """execute_plan 节点可以调用全部四个工具。"""
    from agent.tools.broker import ToolBroker

    broker = ToolBroker(
        model=MODEL,
        executor=dummy_executor,
        budget=sample_budget,
        node_type="execute_plan",
        max_tool_calls=4,
    )
    first_metric = list(MODEL.metrics)[0]
    for tool_name, args in [
        ("list_metrics", {}),
        ("describe_metric", {"metric": first_metric}),
        ("compile_sql", {"plan": {"metric": first_metric}}),
        ("execute_readonly", {"sql": "SELECT 1"}),
    ]:
        result = broker.call(tool_name, args)
        assert result.status == "ok", f"{tool_name} should be allowed for execute_plan"


def test_budget_exceeded_blocks_further_calls(sample_budget, dummy_executor):
    """超出 tool_calls 预算后后续调用全部 blocked。"""
    from agent.tools.broker import ToolBroker

    broker = ToolBroker(
        model=MODEL,
        executor=dummy_executor,
        budget=sample_budget,
        node_type="understand",
        max_tool_calls=2,
    )
    # 前两次调用成功
    r1 = broker.call("list_metrics", {})
    assert r1.status == "ok"
    r2 = broker.call("list_metrics", {})
    assert r2.status == "ok"
    # 第三次被预算拒绝
    r3 = broker.call("list_metrics", {})
    assert r3.status == "blocked"
    assert r3.reason_code == "budget_exceeded"


def test_sensitive_question_blocks_cloud_tools(sample_budget, dummy_executor):
    """敏感问句标记后，所有工具调用被拒绝（防止数据泄漏到云）。"""
    from agent.tools.broker import ToolBroker

    broker = ToolBroker(
        model=MODEL,
        executor=dummy_executor,
        budget=sample_budget,
        node_type="understand",
        sensitivity="high",
    )
    result = broker.call("list_metrics", {})
    assert result.status == "blocked"
    assert result.reason_code == "sensitivity_denied"


def test_tool_call_count_tracked(sample_budget, dummy_executor):
    """broker 追踪调用次数。"""
    from agent.tools.broker import ToolBroker

    broker = ToolBroker(
        model=MODEL,
        executor=dummy_executor,
        budget=sample_budget,
        node_type="understand",
        max_tool_calls=5,
    )
    broker.call("list_metrics", {})
    broker.call("list_metrics", {})
    assert broker.tool_calls_used == 2


def test_invalid_arguments_blocked(sample_budget, dummy_executor):
    """工具参数校验失败返回 blocked（不是 ok）。"""
    from agent.tools.broker import ToolBroker

    broker = ToolBroker(
        model=MODEL,
        executor=dummy_executor,
        budget=sample_budget,
        node_type="execute_plan",
    )
    # describe_metric 需要 metric 参数
    result = broker.call("describe_metric", {})
    assert result.status == "error"
    assert result.reason_code == "tool_error"


# ---------------------------------------------------------------------------
# T11c 绿测扩展：token 预算、失败回退、LLM 节点
# ---------------------------------------------------------------------------


def test_token_budget_tracked(sample_budget, dummy_executor):
    """token 预算追踪（D05 BudgetSpec：输入 2048/输出 512/累计 10240）。"""
    from agent.tools.broker import ToolBroker

    broker = ToolBroker(
        model=MODEL,
        executor=dummy_executor,
        budget=sample_budget,
        node_type="understand",
        max_input_tokens=2048,
        max_output_tokens=512,
        max_total_tokens=10240,
    )
    # 模拟 token 消耗（通过 record_token_usage）
    broker.record_token_usage(input_tokens=1000, output_tokens=200)
    assert broker.input_tokens_used == 1000
    assert broker.output_tokens_used == 200

    # 再消耗一次
    broker.record_token_usage(input_tokens=500, output_tokens=100)
    assert broker.input_tokens_used == 1500
    assert broker.output_tokens_used == 300


def test_token_budget_exceeded_blocks(sample_budget, dummy_executor):
    """超出 token 预算后调用被拒绝。"""
    from agent.tools.broker import ToolBroker

    broker = ToolBroker(
        model=MODEL,
        executor=dummy_executor,
        budget=sample_budget,
        node_type="understand",
        max_input_tokens=100,
        max_output_tokens=50,
        max_total_tokens=200,
    )
    broker.record_token_usage(input_tokens=100, output_tokens=50)
    # 超出输入预算
    result = broker.call("list_metrics", {})
    assert result.status == "blocked"
    assert result.reason_code == "token_budget_exceeded"


def test_handoff_node_has_no_tools(sample_budget, dummy_executor):
    """handoff 节点无任何工具权限。"""
    from agent.tools.broker import ToolBroker

    broker = ToolBroker(
        model=MODEL,
        executor=dummy_executor,
        budget=sample_budget,
        node_type="handoff",
    )
    result = broker.call("list_metrics", {})
    assert result.status == "blocked"
    assert result.reason_code == "tool_not_allowed"


def test_unknown_node_type_has_no_tools(sample_budget, dummy_executor):
    """未注册的节点类型无任何工具权限。"""
    from agent.tools.broker import ToolBroker

    broker = ToolBroker(
        model=MODEL,
        executor=dummy_executor,
        budget=sample_budget,
        node_type="raw_execute",  # 未注册
    )
    result = broker.call("execute_readonly", {"sql": "SELECT 1"})
    assert result.status == "blocked"
    assert result.reason_code == "tool_not_allowed"
