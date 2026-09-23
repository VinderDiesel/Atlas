"""T02b 流程合同与默认模板：D05 结构校验、注册表、预算上限与分支穷尽。

口径：
- `FlowDefinition` 只做声明式合同校验（Schema/唯一性/可达/无环/分支穷尽互斥/预算
  上限），不构造 LangGraph、不执行节点、不读取发布制品；端口兼容与数据依赖校验
  随图编译接线（D05「图编译层继续构造 LangGraph」）；
- 默认模板只封装当前已注册能力（`understand`/`switch`/`subflow` 未注册，声明即
  拒绝）；`chart` 的确定性渲染已注册，但当前挂在回合装配而非独立图节点，默认
  模板不声明图节点；
- 注册表是运行代码的一部分（「节点实现是注册代码，不是用户上传代码」），制品不能
  新增节点类型、版本或实现 ID；
- 本测试不表示任何发布已审核、也不表示流程已接线可执行。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

TEMPLATES = Path(__file__).resolve().parents[1] / "agent" / "flows" / "templates"
ALL_STATUSES = ("ok", "clarify", "unmatched", "blocked", "error")
VALID_DIGEST = "0" * 64


def _impl(node_type: str) -> str:
    from agent.flows.contracts import REGISTERED_NODE_TYPES

    for registration in REGISTERED_NODE_TYPES:
        if registration.node_type == node_type:
            return registration.implementations[0]
    return "atlas.unregistered.placeholder"


def _node(node_id: str, *, node_type: str = "rule_plan", **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "node_id": node_id,
        "node_type": node_type,
        "type_version": 1,
        "implementation_id": _impl(node_type),
        "config_ref": None,
        "input_contract": "QuestionContext",
        "output_contract": "PlanCandidate|Clarification|Unmatched",
    }
    doc.update(overrides)
    return doc


def _edge(source: str, target: str, *statuses: str) -> dict[str, Any]:
    return {
        "from_node": source,
        "to_node": target,
        "condition": {"op": "in" if len(statuses) > 1 else "equals", "values": list(statuses)},
    }


def _flow(**overrides: Any) -> dict[str, Any]:
    """最小合法流程：rule_plan 命中 → execute_plan；其余状态 → handoff 失败出口。"""
    doc: dict[str, Any] = {
        "schema_version": 1,
        "flow_id": "atlas.test",
        "revision": 1,
        "entry": "rule_plan",
        "budget": {},
        "nodes": [
            _node("rule_plan"),
            _node("execute_plan", node_type="execute_plan"),
            _node("handoff", node_type="handoff"),
        ],
        "edges": [
            _edge("rule_plan", "execute_plan", "ok"),
            _edge("rule_plan", "handoff", *ALL_STATUSES[1:]),
        ],
    }
    doc.update(overrides)
    return doc


def _rejects(doc: dict[str, Any]) -> None:
    from agent.flows.contracts import FlowDefinition

    with pytest.raises(ValidationError):
        FlowDefinition.model_validate(doc)


def test_default_templates_wrap_current_registered_capability() -> None:
    from agent.flows.contracts import BudgetSpec, FlowDefinition

    query = FlowDefinition.model_validate_json((TEMPLATES / "query.json").read_bytes())
    analysis = FlowDefinition.model_validate_json((TEMPLATES / "analysis.json").read_bytes())
    assert query.flow_id == "atlas.query"
    assert query.entry == "rule_plan"
    assert {node.node_type for node in query.nodes} == {
        "rule_plan",
        "retrieve",
        "bind_plan",
        "execute_plan",
        "explain",
        "clarify",
        "handoff",
    }
    assert analysis.flow_id == "atlas.analysis"
    assert {node.node_type for node in analysis.nodes} == {
        "analysis",
        "explain",
        "clarify",
        "handoff",
    }
    assert query.budget == BudgetSpec()
    # 未注册能力（D09 understand / 编辑图 switch、subflow）不得出现在默认模板
    declared = {node.node_type for node in query.nodes}
    declared |= {node.node_type for node in analysis.nodes}
    assert declared.isdisjoint({"understand", "switch", "subflow"})


def test_registration_covers_only_built_capability() -> None:
    from agent.flows.contracts import REGISTERED_NODE_TYPES

    assert {item.node_type for item in REGISTERED_NODE_TYPES} == {
        "rule_plan",
        "retrieve",
        "bind_plan",
        "execute_plan",
        "analysis",
        "explain",
        "chart",
        "clarify",
        "handoff",
    }
    assert all(item.type_version == 1 for item in REGISTERED_NODE_TYPES)


@pytest.mark.parametrize(
    "field,value",
    [("schema_version", 2), ("revision", 0), ("flow_id", "Atlas Query")],
)
def test_identity_fields_rejected_when_invalid(field: str, value: Any) -> None:
    _rejects({**_flow(), field: value})


def test_unknown_keys_rejected_at_every_level() -> None:
    _rejects({**_flow(), "extra": 1})
    doc = _flow()
    doc["nodes"][0]["extra"] = 1
    _rejects(doc)
    doc = _flow()
    doc["edges"][0]["extra"] = 1
    _rejects(doc)
    _rejects({**_flow(), "budget": {"extra": 1}})


def test_structure_checks() -> None:
    # node_id 重复
    doc = _flow()
    doc["nodes"].append(_node("rule_plan", node_type="clarify"))
    _rejects(doc)
    # entry 未声明
    _rejects({**_flow(), "entry": "missing"})
    # 边引用未声明节点
    doc = _flow()
    doc["edges"] = [
        _edge("rule_plan", "ghost", "ok"),
        _edge("rule_plan", "handoff", *ALL_STATUSES[1:]),
    ]
    _rejects(doc)
    # 自环（execute_plan 自身分支完整，排除可达性等其他失败原因）
    doc = _flow()
    doc["edges"].extend(
        [
            _edge("execute_plan", "execute_plan", "ok"),
            _edge("execute_plan", "handoff", *ALL_STATUSES[1:]),
        ]
    )
    _rejects(doc)
    # 从 entry 不可达
    doc = _flow()
    doc["nodes"].append(_node("explain", node_type="explain"))
    _rejects(doc)


def test_cycle_rejected() -> None:
    doc = _flow()
    doc["nodes"] = [
        _node("rule_plan"),
        _node("retrieve", node_type="retrieve"),
        _node("handoff", node_type="handoff"),
    ]
    doc["edges"] = [
        _edge("rule_plan", "retrieve", "ok"),
        _edge("rule_plan", "handoff", *ALL_STATUSES[1:]),
        _edge("retrieve", "rule_plan", "ok"),
        _edge("retrieve", "handoff", *ALL_STATUSES[1:]),
    ]
    _rejects(doc)


@pytest.mark.parametrize(
    "edges",
    [
        # 缺失其余状态
        [_edge("rule_plan", "execute_plan", "ok")],
        # ok 重复覆盖（非互斥）
        [
            _edge("rule_plan", "execute_plan", "ok"),
            _edge("rule_plan", "handoff", "ok", *ALL_STATUSES[1:]),
        ],
        # 非 equals/in 操作
        [
            {
                "from_node": "rule_plan",
                "to_node": "execute_plan",
                "condition": {"op": "always", "values": ["ok"]},
            }
        ],
        # 非状态枚举值
        [
            {
                "from_node": "rule_plan",
                "to_node": "execute_plan",
                "condition": {"op": "equals", "values": ["success"]},
            }
        ],
        # equals 多值
        [
            {
                "from_node": "rule_plan",
                "to_node": "execute_plan",
                "condition": {"op": "equals", "values": ["ok", "clarify"]},
            }
        ],
        # 空集
        [
            {
                "from_node": "rule_plan",
                "to_node": "execute_plan",
                "condition": {"op": "in", "values": []},
            }
        ],
        # 重复状态
        [
            {
                "from_node": "rule_plan",
                "to_node": "execute_plan",
                "condition": {"op": "in", "values": ["ok", "ok"]},
            }
        ],
    ],
)
def test_branch_partition_must_be_exhaustive_and_exclusive(edges: list[dict[str, Any]]) -> None:
    _rejects({**_flow(), "edges": edges})


@pytest.mark.parametrize(
    "overrides",
    [
        {"node_type": "understand"},  # D05 词表内但当前未注册
        {"node_type": "sql_generate"},  # 词表外
        {"type_version": 2},  # 未知版本
        {"implementation_id": "atlas.rule_plan.evil"},  # 未注册实现
    ],
)
def test_unregistered_nodes_rejected(overrides: dict[str, Any]) -> None:
    doc = _flow()
    doc["nodes"][0] = _node("rule_plan", **overrides)
    _rejects(doc)


@pytest.mark.parametrize(
    "config_ref",
    [
        "https://example.com/config.json",  # URL
        "agent/flows/configs/x.json",  # 路径
        "bash -c true",  # shell
        "0" * 63,  # 非完整摘要
    ],
)
def test_config_ref_must_be_artifact_digest(config_ref: str) -> None:
    doc = _flow()
    doc["nodes"][0] = _node("rule_plan", config_ref=config_ref)
    _rejects(doc)


def test_config_ref_accepts_artifact_digest() -> None:
    from agent.flows.contracts import FlowDefinition

    doc = _flow()
    doc["nodes"][0] = _node("rule_plan", config_ref=VALID_DIGEST)
    parsed = FlowDefinition.model_validate(doc)
    assert parsed.nodes[0].config_ref == VALID_DIGEST


def test_budget_defaults_are_first_version_caps() -> None:
    from agent.flows.contracts import BudgetSpec

    budget = BudgetSpec()
    assert (budget.model_calls, budget.node_attempts, budget.tool_calls) == (4, 2, 3)
    assert (budget.sql_calls, budget.run_seconds) == (4, 180)
    assert (budget.model_input_tokens, budget.model_output_tokens) == (2048, 512)
    assert budget.model_total_tokens == 10240


@pytest.mark.parametrize(
    "budget",
    [
        {"model_calls": 5},
        {"node_attempts": 3},
        {"tool_calls": 4},
        {"sql_calls": 5},
        {"run_seconds": 181},
        {"model_total_tokens": 10241},
    ],
)
def test_budget_cannot_exceed_first_version_caps(budget: dict[str, int]) -> None:
    _rejects({**_flow(), "budget": budget})


def test_node_attempts_capped_and_default_one() -> None:
    from agent.flows.contracts import FlowDefinition

    doc = _flow()
    doc["nodes"][0] = _node("rule_plan", max_attempts=3)
    _rejects(doc)
    parsed = FlowDefinition.model_validate(_flow())
    assert parsed.nodes[0].max_attempts == 1


def test_node_result_contract() -> None:
    from agent.flows.contracts import NodeResult

    result = NodeResult(status="clarify", reason_code="ambiguous_metric")
    assert result.output is None
    assert result.usage.model_calls == 0
    # output 的判别联合按节点合同在运行内核校验；本层只保证 JSON 对象或 null
    assert NodeResult(status="ok", output={"kind": "answer"}).output == {"kind": "answer"}
    with pytest.raises(ValidationError):
        NodeResult.model_validate({"status": "done"})
    with pytest.raises(ValidationError):
        NodeResult.model_validate({"status": "ok", "extra": 1})
    with pytest.raises(ValidationError):
        NodeResult.model_validate({"status": "ok", "output": [1]})
    with pytest.raises(ValidationError):
        NodeResult.model_validate({"status": "ok", "reason_code": "Bad Code"})
    with pytest.raises(ValidationError):
        NodeResult.model_validate({"status": "ok", "evidence_refs": [""]})
    with pytest.raises(ValidationError):
        NodeResult.model_validate({"status": "ok", "usage": {"sql_calls": -1}})
