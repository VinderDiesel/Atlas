"""T09b: FlowDefinition → LangGraph 编译与等价性验证。

compile_flow() 从声明式 FlowDefinition 构造 LangGraph StateGraph 拓扑。
等价性测试验证编译产物与现有 build_graph() 的路由结构一致。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.flows.compile import FlowTopology, compile_flow
from agent.flows.contracts import FlowDefinition

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "agent" / "flows" / "templates"


def _query_flow() -> FlowDefinition:
    return FlowDefinition.model_validate(
        json.loads((TEMPLATES_DIR / "query.json").read_text())
    )


# ---------------------------------------------------------------------------
# 编译基本功能
# ---------------------------------------------------------------------------


def test_compile_default_query_flow():
    """query.json 编译为 FlowTopology 成功。"""
    topo = compile_flow(_query_flow())
    assert isinstance(topo, FlowTopology)
    assert len(topo.node_ids) == 7
    assert "rule_plan" in topo.node_ids
    assert "execute_plan" in topo.node_ids


def test_compile_analysis_flow():
    """analysis.json 编译成功。"""
    flow = FlowDefinition.model_validate(
        json.loads((TEMPLATES_DIR / "analysis.json").read_text())
    )
    topo = compile_flow(flow)
    assert len(topo.node_ids) == 4
    assert topo.entry == "analysis"


# ---------------------------------------------------------------------------
# 拓扑等价性
# ---------------------------------------------------------------------------


def test_deterministic_path_topology():
    """确定性链：rule_plan →(ok)→ execute_plan →(ok)→ explain → END。"""
    topo = compile_flow(_query_flow())
    # rule_plan ok → execute_plan
    assert topo.edge_target("rule_plan", "ok") == "execute_plan"
    # execute_plan ok → explain
    assert topo.edge_target("execute_plan", "ok") == "explain"
    # explain 是终端节点
    assert "explain" in topo.terminal_nodes


def test_candidate_path_topology():
    """候选链：rule_plan →(unmatched)→ retrieve →(ok)→ bind_plan →(ok)→ execute_plan。"""
    topo = compile_flow(_query_flow())
    assert topo.edge_target("rule_plan", "unmatched") == "retrieve"
    assert topo.edge_target("retrieve", "ok") == "bind_plan"
    assert topo.edge_target("bind_plan", "ok") == "execute_plan"


def test_clarify_terminal_topology():
    """clarify 状态路由到 clarify 终端。"""
    topo = compile_flow(_query_flow())
    assert topo.edge_target("rule_plan", "clarify") == "clarify"
    assert "clarify" in topo.terminal_nodes


def test_handoff_terminal_topology():
    """blocked/error 路由到 handoff 终端。"""
    topo = compile_flow(_query_flow())
    assert topo.edge_target("rule_plan", "blocked") == "handoff"
    assert topo.edge_target("rule_plan", "error") == "handoff"
    assert "handoff" in topo.terminal_nodes


# ---------------------------------------------------------------------------
# 运行时安全
# ---------------------------------------------------------------------------


def test_budget_spec_carried():
    """预算规格从 FlowDefinition 透传到拓扑。"""
    topo = compile_flow(_query_flow())
    assert topo.budget.model_calls == 4
    assert topo.budget.sql_calls == 4
    assert topo.budget.run_seconds == 180


def test_unknown_node_type_rejected_at_compile():
    """编译期不执行节点，但 validate_flow 已在编译前拒绝未知类型。"""
    from agent.flows.registry import node_catalog
    from agent.flows.validate import validate_flow

    data = json.loads((TEMPLATES_DIR / "query.json").read_text())
    for node in data["nodes"]:
        if node["node_id"] == "execute_plan":
            node["node_type"] = "raw_execute"
            break
    issues = validate_flow(data, node_catalog())
    assert any(i.code == "unknown_node_type" for i in issues)


def test_execute_plan_protected_in_topology():
    """execute_plan 在拓扑中是所有 ok 路径的必经节点。"""
    topo = compile_flow(_query_flow())
    # 从 entry 到 explain（成功终端）的所有路径必须经过 execute_plan
    for path in topo.paths_to_terminal("explain"):
        assert "execute_plan" in path, f"路径 {path} 绕过 execute_plan"
