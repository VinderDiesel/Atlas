"""T1：节点 I/O 提取模块测试（agent/node_io.py）。

验证 extract_node_io 按 NODE_IO_SPEC 声明的键从 TurnState 提取输入/输出快照，
dataclass 值序列化为 dict，tuple 值转 list，未知节点返回空 dict。
"""

from __future__ import annotations

from agent.compiler import ComparisonSpec, Filter, OrderSpec, Plan, TimeSpec
from agent.node_io import NODE_IO_SPEC, extract_node_io
from agent.planner import ClarificationRequest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sample_plan() -> Plan:
    return Plan(
        metric="total_revenue",
        dimensions=("region",),
        time=TimeSpec(granularity="year", value=2013),
        filters=(Filter(column="status", op="=", value="completed"),),
        order_by=(OrderSpec(column="total_revenue", desc=True),),
        limit=50,
        comparison=ComparisonSpec(kind="yoy"),
    )


def _full_state(**overrides) -> dict:
    """构造包含所有 NODE_IO_SPEC 键的完整 state（模拟 TurnState）。"""
    base: dict = {
        "question": "2013 年各地区总交易额",
        "session_id": "sess-001",
        "turns": 1,
        "session_fingerprint": "fp-abc",
        "plan_override": None,
        "plan": _sample_plan(),
        "last_plan": None,
        "clarification": None,
        "candidates": ("metric_a", "metric_b", "metric_c"),
        "unmatched": False,
        "reason": "",
        "attempts": 0,
        "validated": False,
        "sql": "",
        "rows": (("East", 1000), ("West", 2000)),
        "columns": ("region", "total_revenue"),
        "time_column": "CalendarYear",
        "row_count": 2,
        "latency_ms": 42.5,
        "explanation": "交易额按地区汇总",
        "handoff_request": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# T1-1：plan 节点 input 提取
# ---------------------------------------------------------------------------

class TestPlanNodeInput:
    def test_extracts_declared_input_keys(self):
        state = _full_state()
        result = extract_node_io("plan", state, kind="input")
        # spec 声明：plan_override, last_plan, unmatched, turns
        assert "plan_override" in result
        assert "last_plan" in result
        assert "unmatched" in result
        assert "turns" in result

    def test_excludes_non_declared_keys(self):
        state = _full_state()
        result = extract_node_io("plan", state, kind="input")
        # question/sql/rows/candidates 等不在 plan input spec 中
        assert "question" not in result
        assert "sql" not in result
        assert "rows" not in result
        assert "candidates" not in result


# ---------------------------------------------------------------------------
# T1-2：plan 节点 output 提取
# ---------------------------------------------------------------------------

class TestPlanNodeOutput:
    def test_plan_dataclass_serialized_to_dict(self):
        state = _full_state()
        result = extract_node_io("plan", state, kind="output")
        assert "plan" in result
        plan_dict = result["plan"]
        assert isinstance(plan_dict, dict)
        assert plan_dict["metric"] == "total_revenue"
        assert plan_dict["dimensions"] == ["region"]  # tuple → list
        assert plan_dict["limit"] == 50

    def test_time_spec_serialized(self):
        state = _full_state()
        result = extract_node_io("plan", state, kind="output")
        time_dict = result["plan"]["time"]
        assert time_dict["granularity"] == "year"
        assert time_dict["value"] == 2013

    def test_filter_serialized(self):
        state = _full_state()
        result = extract_node_io("plan", state, kind="output")
        filters = result["plan"]["filters"]
        assert len(filters) == 1
        assert filters[0]["column"] == "status"
        assert filters[0]["op"] == "="


# ---------------------------------------------------------------------------
# T1-3：retrieve 节点
# ---------------------------------------------------------------------------

class TestRetrieveNode:
    def test_output_candidates_tuple_to_list(self):
        state = _full_state()
        result = extract_node_io("retrieve", state, kind="output")
        assert result["candidates"] == ["metric_a", "metric_b", "metric_c"]

    def test_input_includes_plan_and_candidates(self):
        state = _full_state()
        result = extract_node_io("retrieve", state, kind="input")
        assert "plan" in result
        assert "candidates" in result


# ---------------------------------------------------------------------------
# T1-4：validate 节点
# ---------------------------------------------------------------------------

class TestValidateNode:
    def test_output_includes_validated_and_sql(self):
        state = _full_state(validated=True, sql="SELECT 1")
        result = extract_node_io("validate", state, kind="output")
        assert result["validated"] is True
        assert result["sql"] == "SELECT 1"

    def test_input_includes_plan_and_attempts(self):
        state = _full_state(attempts=2)
        result = extract_node_io("validate", state, kind="input")
        assert "plan" in result
        assert result["attempts"] == 2


# ---------------------------------------------------------------------------
# T1-5：execute 节点
# ---------------------------------------------------------------------------

class TestExecuteNode:
    def test_output_rows_excluded_but_row_count_included(self):
        """D07 约束：rows 不在 execute output spec 中，row_count 在。"""
        state = _full_state(sql="SELECT region, SUM(amount) FROM sales GROUP BY region")
        result = extract_node_io("execute", state, kind="output")
        assert "rows" not in result
        assert result["row_count"] == 2
        assert "SELECT" in result["sql"]
        assert result["latency_ms"] == 42.5

    def test_output_columns_as_list(self):
        state = _full_state()
        result = extract_node_io("execute", state, kind="output")
        assert result["columns"] == ["region", "total_revenue"]


# ---------------------------------------------------------------------------
# T1-6：clarify / handoff 终端节点
# ---------------------------------------------------------------------------

class TestTerminalNodes:
    def test_clarify_output_has_clarification_request(self):
        req = ClarificationRequest(
            question="您指的是哪个指标？",
            reasons=("歧义：命中多个口径",),
            candidates=("total_revenue", "net_revenue"),
            kind="ambiguous",
        )
        state = _full_state(clarification=req)
        result = extract_node_io("clarify", state, kind="output")
        clar = result["clarification"]
        assert isinstance(clar, dict)
        assert clar["kind"] == "ambiguous"
        assert clar["reasons"] == ["歧义：命中多个口径"]
        assert clar["candidates"] == ["total_revenue", "net_revenue"]

    def test_handoff_input_has_reason(self):
        state = _full_state(reason="无法解析问句")
        result = extract_node_io("handoff", state, kind="input")
        assert result["reason"] == "无法解析问句"


# ---------------------------------------------------------------------------
# T1-7：边界情况
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_unknown_node_id_returns_empty(self):
        state = _full_state()
        result = extract_node_io("nonexistent_node", state, kind="input")
        assert result == {}

    def test_missing_key_omitted_not_error(self):
        """state 中缺少 spec 声明的键时，结果中省略该键（不报错）。"""
        state = _full_state()
        state.pop("plan_override", None)
        result = extract_node_io("plan", state, kind="input")
        assert "plan_override" not in result

    def test_none_value_preserved(self):
        """值为 None 时保留（与缺失区分）。"""
        state = _full_state(plan_override=None)
        result = extract_node_io("plan", state, kind="input")
        assert "plan_override" in result
        assert result["plan_override"] is None

    def test_spec_has_all_eight_nodes(self):
        """NODE_IO_SPEC 覆盖全部 8 个图节点。"""
        expected = {
            "plan", "retrieve", "generate", "validate",
            "execute", "explain", "clarify", "handoff",
        }
        assert set(NODE_IO_SPEC.keys()) == expected
