"""T09a: 受约束图校验——FlowIssue 结构化校验器红测。

ADR-0031 D05/D06：节点类型/版本/实现必须注册；分支只允许 equals/in，穷尽互斥
覆盖 5 种 NodeStatus；DAG 无环；execute_plan 不可绕过。

每个测试验证 validate_flow() 返回的 FlowIssue.code 集合包含预期错误码。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from agent.flows.registry import node_catalog
from agent.flows.validate import FlowIssue, validate_flow

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "agent" / "flows" / "templates"


def _query_dict() -> dict:
    """加载默认 query 模板为 dict（每次深拷贝，测试可安全修改）。"""
    return json.loads((TEMPLATES_DIR / "query.json").read_text())


def _set_node_type(d: dict, node_id: str, new_type: str) -> None:
    """修改 dict 中指定节点的 node_type（绕过 Pydantic 注册校验）。"""
    for node in d["nodes"]:
        if node["node_id"] == node_id:
            node["node_type"] = new_type
            return


def _codes(issues: list[FlowIssue]) -> set[str]:
    return {issue.code for issue in issues}


# ---------------------------------------------------------------------------
# 节点类型与实现注册
# ---------------------------------------------------------------------------


def test_unknown_node_type_rejected():
    """任务卡示例：合法模板把 execute_plan 的 node_type 改为未注册 raw_execute。"""
    d = _query_dict()
    _set_node_type(d, "execute_plan", "raw_execute")
    issues = validate_flow(d, node_catalog())
    assert "unknown_node_type" in _codes(issues)


def test_unregistered_implementation():
    """注册类型但实现 ID 不在白名单。"""
    d = _query_dict()
    for node in d["nodes"]:
        if node["node_id"] == "execute_plan":
            node["implementation_id"] = "atlas.execution.unregistered"
            break
    issues = validate_flow(d, node_catalog())
    assert "unregistered_implementation" in _codes(issues)


# ---------------------------------------------------------------------------
# 结构校验
# ---------------------------------------------------------------------------


def test_duplicate_node_id():
    d = _query_dict()
    d["nodes"].append(copy.deepcopy(d["nodes"][0]))
    issues = validate_flow(d, node_catalog())
    assert "duplicate_node_id" in _codes(issues)


def test_entry_not_found():
    d = _query_dict()
    d["entry"] = "nonexistent"
    issues = validate_flow(d, node_catalog())
    assert "entry_not_found" in _codes(issues)


def test_edge_references_unknown_node():
    d = _query_dict()
    d["edges"].append(
        {
            "from_node": "explain", "to_node": "ghost",
            "condition": {"op": "equals", "values": ["ok"]},
        }
    )
    issues = validate_flow(d, node_catalog())
    assert "edge_unknown_node" in _codes(issues)


def test_self_loop_rejected():
    d = _query_dict()
    d["edges"].append(
        {
            "from_node": "explain", "to_node": "explain",
            "condition": {"op": "equals", "values": ["ok"]},
        }
    )
    issues = validate_flow(d, node_catalog())
    assert "self_loop" in _codes(issues)


def test_unreachable_node():
    d = _query_dict()
    d["nodes"].append(
        {
            "node_id": "orphan",
            "node_type": "explain",
            "type_version": 1,
            "implementation_id": "atlas.explain.template",
            "config_ref": None,
            "input_contract": "ExecutionResult",
            "output_contract": "Explanation",
        }
    )
    issues = validate_flow(d, node_catalog())
    assert "unreachable" in _codes(issues)


# ---------------------------------------------------------------------------
# 分支穷尽与失败出口
# ---------------------------------------------------------------------------


def test_branch_not_exhaustive():
    """rule_plan 只覆盖 ok/clarify/unmatched，缺 blocked/error 但有其他失败出口→穷尽告警。"""
    d = _query_dict()
    d["edges"] = [
        e
        for e in d["edges"]
        if e["from_node"] != "rule_plan" or e["to_node"] != "handoff"
    ]
    d["edges"].append(
        {
            "from_node": "rule_plan", "to_node": "handoff",
            "condition": {"op": "equals", "values": ["blocked"]},
        }
    )
    issues = validate_flow(d, node_catalog())
    codes = _codes(issues)
    assert "branch_not_exhaustive" in codes
    assert "no_failure_exit" not in codes


def test_branch_overlapping():
    """两条边覆盖同一状态→分支重叠。"""
    d = _query_dict()
    d["edges"].append(
        {
            "from_node": "rule_plan", "to_node": "execute_plan",
            "condition": {"op": "equals", "values": ["ok"]},
        }
    )
    issues = validate_flow(d, node_catalog())
    assert "branch_overlapping" in _codes(issues)


def test_no_failure_exit():
    """execute_plan 只有 ok 出口，blocked/error 无去向。"""
    d = _query_dict()
    d["edges"] = [
        e for e in d["edges"] if e["from_node"] != "execute_plan"
    ]
    d["edges"].append(
        {
            "from_node": "execute_plan", "to_node": "explain",
            "condition": {"op": "equals", "values": ["ok"]},
        }
    )
    issues = validate_flow(d, node_catalog())
    codes = _codes(issues)
    assert "no_failure_exit" in codes
    assert "branch_not_exhaustive" not in codes


# ---------------------------------------------------------------------------
# DAG 校验
# ---------------------------------------------------------------------------


def test_cycle_detected():
    """A→B→A 形成环（非自环）。"""
    d = {
        "schema_version": 1,
        "flow_id": "cycle.test",
        "revision": 1,
        "entry": "a",
        "budget": {},
        "nodes": [
            {
                "node_id": "a",
                "node_type": "rule_plan",
                "type_version": 1,
                "implementation_id": "atlas.planner.rules",
                "config_ref": None,
                "input_contract": "QuestionContext",
                "output_contract": "PlanCandidate|Clarification|Unmatched",
            },
            {
                "node_id": "b",
                "node_type": "rule_plan",
                "type_version": 1,
                "implementation_id": "atlas.planner.rules",
                "config_ref": None,
                "input_contract": "QuestionContext",
                "output_contract": "PlanCandidate|Clarification|Unmatched",
            },
        ],
        "edges": [
            {"from_node": "a", "to_node": "b", "condition": {"op": "equals", "values": ["ok"]}},
            {
                "from_node": "a",
                "to_node": "b",
                "condition": {"op": "in", "values": ["clarify", "unmatched", "blocked", "error"]},
            },
            {"from_node": "b", "to_node": "a", "condition": {"op": "equals", "values": ["ok"]}},
            {
                "from_node": "b",
                "to_node": "a",
                "condition": {"op": "in", "values": ["clarify", "unmatched", "blocked", "error"]},
            },
        ],
    }
    issues = validate_flow(d, node_catalog())
    assert "cycle" in _codes(issues)


# ---------------------------------------------------------------------------
# 预算
# ---------------------------------------------------------------------------


def test_budget_exceeded():
    d = _query_dict()
    d["budget"] = {"model_calls": 99}
    issues = validate_flow(d, node_catalog())
    assert "budget_exceeded" in _codes(issues)


# ---------------------------------------------------------------------------
# 端口兼容
# ---------------------------------------------------------------------------


def test_port_mismatch_reserved():
    """端口兼容校验为预留维度：默认模板不触发（非 ok 边携带转换数据，静态不可判定）。"""
    issues = validate_flow(_query_dict(), node_catalog())
    assert "port_mismatch" not in _codes(issues)


# ---------------------------------------------------------------------------
# 安全必经路径
# ---------------------------------------------------------------------------


def test_execute_plan_cannot_be_bypassed():
    """保留 execute_plan 节点但重路由边绕过它→explain 不经 execute_plan 可达。"""
    d = _query_dict()
    # 移除 execute_plan 的所有边，添加 rule_plan→explain 直连
    d["edges"] = [
        e
        for e in d["edges"]
        if e["from_node"] != "execute_plan" and e["to_node"] != "execute_plan"
    ]
    d["edges"].append(
        {
            "from_node": "rule_plan", "to_node": "explain",
            "condition": {"op": "equals", "values": ["ok"]},
        }
    )
    issues = validate_flow(d, node_catalog())
    assert "execute_plan_bypassed" in _codes(issues)


# ---------------------------------------------------------------------------
# 合法模板通过
# ---------------------------------------------------------------------------


def test_valid_default_query_template_passes():
    issues = validate_flow(_query_dict(), node_catalog())
    assert len(issues) == 0, f"unexpected issues: {issues}"


def test_valid_default_analysis_template_passes():
    d = json.loads((TEMPLATES_DIR / "analysis.json").read_text())
    issues = validate_flow(d, node_catalog())
    assert len(issues) == 0, f"unexpected issues: {issues}"
