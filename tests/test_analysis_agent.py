"""DataAgent.analyze 编排与父子观测契约测试（ADR-0026 T07）。

覆盖 task-7-brief 检查单：
- 记录执行器覆盖完整四步流程与每个失败下标：最多 4 次、固定顺序、失败后零调用；
- 按 T06 开始父轮；解析/资格失败不执行 SQL；通过后顺序执行 T05 步骤并记录
  已执行事实；四步全成才调 T04 综合；最后包装父轮 TurnResult 与 AnalysisResult；
- 无分析意图 → 恰好委托一次 ask、分析字段为 null；有意图澄清计一父轮、
  零 SQL 零 LLM；
- 统一 model/snapshot/身份/预算：不得逐步构建活图；blocked/error 裁剪部分
  结果与被拒 SQL、reason_code 取稳定安全码、失败步角色保留；
- 父轮 atlas.turn 恰一次、每个已执行步骤 span 恰一次、失败步也有终态 span；
  无埋点时 no-op、埋点故障隔离；elapsed_ms 全程单调钟。

全部走 fake 执行器/快照注入，不碰网络与 Doris（与 tests/test_graph.py 同口径）。
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Span

import observability.otel as otel
from agent.analysis import (
    ANALYSIS_ROLES,
    AnalysisPlan,
    AnalysisResult,
    validate_analysis_result,
)
from agent.compiler import Plan, SemanticModel
from agent.graph import DataAgent, SessionIdentityConflict, build_graph
from agent.planner import ClarificationRequest
from agent.security.sql_guard import Budget
from agent.state import TurnResult, decode_rows
from serving.auth import claims_fingerprint

REPO = Path(__file__).resolve().parent.parent
SNAPSHOT_SHA = "7d48dcb"

# 分析问句三态（与 tests/test_analysis_planner.py 锁定的解析行为一致）：
FULL_Q = "分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献"  # → AnalysisPlan
CLARIFY_Q = "分析 2013Q4 相对上季度的佣金收入变化贡献"  # → ClarificationRequest
NO_INTENT_Q = "查一下 2013Q4 的佣金收入是多少"  # → None（委托普通问数）


def _meta() -> dict[str, Any]:
    p = REPO / f"data/snapshots/{SNAPSHOT_SHA}.meta.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _allowed() -> frozenset[str]:
    meta = _meta()
    return frozenset(
        f"atlas.{ns}.{table}" for ns, tables in meta["row_counts"].items() for table in tables
    )


def _budget(max_rows: int = 10_000) -> Budget:
    return Budget(dialect="doris", max_rows=max_rows, allowed_tables=_allowed())


# Guard 必拒预算：白名单不含佣金表（blocked 场景用）
DENY_BUDGET = Budget(
    dialect="doris", max_rows=10_000, allowed_tables=frozenset({"atlas.dws.other"})
)

ELIGIBLE: dict[str, Any] = {
    "available": True,
    "eligible": True,
    "reason": None,
    "evidence": {
        "eligible_combinations": [{"metric": "commission_revenue", "dimension": "Branch"}]
    },
}


def _claims(role: str, **user_context: object) -> dict[str, object]:
    """与 verify_token 输出同形态（tests/test_session_persistence.py 同构）。"""
    return {"sub": "contract-user", "role": role, "user_context": user_context}


HQ_CLAIMS = _claims("hq_admin")
BRANCH_CLAIMS = _claims("branch_manager", branch="A")


def _four_results() -> list[tuple[list[tuple[Any, ...]], list[str]]]:
    """按调用序的四步结果集（合成测试输入，非快照实测值；两期分组对账成立）。

    顺序即 ANALYSIS_ROLES：baseline_total / current_total /
    current_by_dimension / baseline_by_dimension。度量单元格一律 Decimal
    （综合算术的精确包络，决策⑤；float/str 会被综合门拒绝）。
    """
    return [
        ([(Decimal("300"),)], ["commission_revenue"]),
        ([(Decimal("260"),)], ["commission_revenue"]),
        ([("A", Decimal("90")), ("B", Decimal("170"))], ["Branch", "commission_revenue"]),
        ([("A", Decimal("180")), ("B", Decimal("120"))], ["Branch", "commission_revenue"]),
    ]


class AnalysisExecutor:
    """按调用序返回预置结果集的桩执行器（记录 Guard 出口 SQL）。

    fail_on：第 k 次调用抛异常（模拟 Doris 执行期故障），k 从 1 起。
    """

    def __init__(
        self,
        results: list[tuple[list[tuple[Any, ...]], list[str]]] | None = None,
        *,
        fail_on: int | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._results = results if results is not None else _four_results()
        self._fail_on = fail_on

    def __call__(self, sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
        self.calls.append(sql)
        if self._fail_on is not None and len(self.calls) == self._fail_on:
            raise RuntimeError("doris 断连")
        rows, columns = self._results[len(self.calls) - 1]
        return [tuple(r) for r in rows], list(columns)


def _agent(executor: Any, **kw: Any) -> DataAgent:
    """带资格证据的测试 agent（生产里由 factory 挂 analysis_eligibility）。"""
    kw.setdefault("snapshot_meta", _meta())
    agent = DataAgent(executor=executor, budget=kw.pop("budget", _budget()), **kw)
    agent.analysis_eligibility = ELIGIBLE  # type: ignore[attr-defined]
    return agent


def _values(agent: DataAgent, sid: str) -> dict[str, Any]:
    state = agent._graph.get_state({"configurable": {"thread_id": f"{agent.model.name}:{sid}"}})
    return dict(state.values or {})


class ForbiddenGenerator:
    """候选链不得进入：generate 被调 = 测试当场失败（零 LLM 断言用）。"""

    engine = "stub"

    def generate(
        self, question: str, k: int = 5, *, candidates: Sequence[str] | None = None
    ) -> Any:
        raise AssertionError("分析澄清轮不得进入候选链 generate")


class TestFullFlow(unittest.TestCase):
    """完整四步流程：固定顺序、父轮不冒充、综合精确、闭环契约零违规。"""

    def setUp(self) -> None:
        self.executor = AnalysisExecutor()
        self.agent = _agent(self.executor)
        self.sid = "t07-full"

    def test_full_four_steps_in_fixed_order_with_exact_synthesis(self) -> None:
        result = self.agent.analyze(FULL_Q, session_id=self.sid)
        self.assertIsInstance(result, AnalysisResult)
        self.assertEqual(result.turn.kind, "answer")
        self.assertEqual(result.turn.metric, "commission_revenue")
        self.assertEqual(len(self.executor.calls), 4, "执行器调用上限 4：不多不少")
        self.assertEqual([s.kind for s in result.steps], ["answer"] * 4)
        # 固定顺序：第 i 步的 SQL 与执行器第 i 次实收一致（顺序即角色序）
        self.assertEqual([s.sql for s in result.steps], list(self.executor.calls))
        # 父轮不冒充单 SQL 结果（决策⑥）
        self.assertIsNone(result.turn.sql)
        self.assertEqual(result.turn.rows, ())
        self.assertEqual(result.turn.columns, ())
        self.assertEqual(result.turn.row_count, 0)
        # 综合（T04）：精确 Decimal 加法归因
        attr = result.attribution
        assert attr is not None
        self.assertEqual(attr.status, "ok")
        self.assertEqual(attr.baseline, Decimal("300"))
        self.assertEqual(attr.current, Decimal("260"))
        self.assertEqual(attr.delta, Decimal("-40"))
        self.assertEqual([item.value for item in attr.items], ["A", "B"])
        self.assertIsNone(result.reason_code)
        # 绑定快照与语义层版本
        self.assertEqual(result.snapshot_sha, SNAPSHOT_SHA)
        self.assertEqual(result.semantic_sha256, self.agent.model.source_sha256)
        self.assertGreaterEqual(result.elapsed_ms, 0.0)
        # 父轮耗时 = 已执行步骤耗时之和（blocked/error 步按其 latency 计入）
        self.assertAlmostEqual(
            result.turn.latency_ms, sum(s.latency_ms for s in result.steps), places=6
        )
        # 闭环契约：T01 校验零违规
        self.assertEqual(validate_analysis_result(result), ())

    def test_parent_turn_recorded_once_with_step_evidence(self) -> None:
        result = self.agent.analyze(FULL_Q, session_id=self.sid)
        values = _values(self.agent, self.sid)
        self.assertEqual(values.get("turns"), 1)
        record = values["analysis_record"]
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["question"], FULL_Q)
        self.assertIsNone(record["identity_fingerprint"])
        steps = record["steps"]
        self.assertEqual([e["role"] for e in steps], list(ANALYSIS_ROLES))
        self.assertEqual([e["index"] for e in steps], [1, 2, 3, 4])
        self.assertEqual([e["status"] for e in steps], ["ok"] * 4)
        for entry, step in zip(steps, result.steps, strict=True):
            self.assertEqual(entry["sql"], step.sql)
            self.assertEqual(entry["columns"], list(step.columns))
            self.assertEqual(decode_rows(entry["rows"]), step.rows)
            self.assertEqual(entry["latency_ms"], step.latency_ms)
        self.assertEqual(record["attribution_status"], "ok")
        # 计划投影与两期绑定入记录（T08/T09 共用口径）
        self.assertEqual(len(record["plan_projections"]), 4)
        self.assertEqual(record["baseline"], {"granularity": "quarter", "value": "2013Q3"})
        self.assertEqual(record["current"], {"granularity": "quarter", "value": "2013Q4"})

    def test_record_turn_called_once_for_parent_only(self) -> None:
        with mock.patch("agent.graph.record_turn") as rec:
            self.agent.analyze(FULL_Q, session_id=self.sid)
        self.assertEqual(rec.call_count, 1, "父轮 atlas.turn 恰一次（子步不写用户轮指标）")

    def test_record_analysis_step_called_once_per_executed_step(self) -> None:
        with mock.patch("agent.graph.record_analysis_step") as rec:
            self.agent.analyze(FULL_Q, session_id=self.sid)
        self.assertEqual(rec.call_count, 4)
        self.assertEqual([c.kwargs["role"] for c in rec.call_args_list], list(ANALYSIS_ROLES))
        self.assertEqual([c.kwargs["turns"] for c in rec.call_args_list], [1, 1, 1, 1])
        self.assertEqual([c.kwargs["session_id"] for c in rec.call_args_list], [self.sid] * 4)


class TestFailureIndices(unittest.TestCase):
    """每个失败下标：error 终态、裁剪、失败后零调用、失败步角色保留。"""

    def _analyze_with_failure_at(
        self, k: int
    ) -> tuple[AnalysisResult, AnalysisExecutor, DataAgent]:
        executor = AnalysisExecutor(fail_on=k)
        agent = _agent(executor)
        sid = f"t07-fail{k}"
        return agent.analyze(FULL_Q, session_id=sid), executor, agent

    def test_executor_failure_at_each_index_trims_and_stops(self) -> None:
        for k in (1, 2, 3, 4):
            with self.subTest(k=k):
                result, executor, agent = self._analyze_with_failure_at(k)
                self.assertEqual(result.turn.kind, "error")
                self.assertEqual(result.reason_code, "execution_error")
                self.assertIsNone(result.attribution)
                self.assertEqual(len(result.steps), k)
                self.assertEqual(result.steps[-1].kind, "error")
                self.assertTrue(all(s.kind == "answer" for s in result.steps[:-1]))
                # 失败后零调用：执行器恰被调 k 次
                self.assertEqual(len(executor.calls), k)
                self.assertEqual(validate_analysis_result(result), ())
                record = _values(agent, f"t07-fail{k}")["analysis_record"]
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["failed_index"], k)
                self.assertEqual(record["reason_code"], "execution_error")
                failed = record["steps"][-1]
                self.assertEqual(failed["role"], ANALYSIS_ROLES[k - 1], "失败步角色保留")
                self.assertEqual(failed["status"], "error")
                self.assertEqual(failed["reason_code"], "execution_error")
                # 部分结果与被拒 SQL 裁剪：失败步证据不带 sql/rows/columns
                self.assertNotIn("sql", failed)
                self.assertNotIn("rows", failed)
                self.assertNotIn("columns", failed)

    def test_failed_step_still_gets_terminal_step_span(self) -> None:
        with mock.patch("agent.graph.record_analysis_step") as rec:
            result, _executor, _agent = self._analyze_with_failure_at(3)
        self.assertEqual(result.turn.kind, "error")
        self.assertEqual(rec.call_count, 3, "失败步也有终态 span")
        self.assertEqual([c.kwargs["role"] for c in rec.call_args_list], list(ANALYSIS_ROLES)[:3])
        self.assertEqual(
            [c.args[0].kind for c in rec.call_args_list],
            ["answer", "answer", "error"],
        )


class TestBlockedFirstStep(unittest.TestCase):
    """Guard 拒绝第一步：blocked 终态、零 SQL、被拒 SQL 不落证据。"""

    def test_guard_blocked_first_step_blocks_parent_and_zero_sql(self) -> None:
        executor = AnalysisExecutor()
        agent = DataAgent(executor=executor, budget=DENY_BUDGET, snapshot_meta=_meta())
        agent.analysis_eligibility = ELIGIBLE  # type: ignore[attr-defined]
        result = agent.analyze(FULL_Q, session_id="t07-blocked")
        self.assertEqual(result.turn.kind, "blocked")
        self.assertEqual(result.reason_code, "guard_blocked")
        self.assertEqual(result.turn.block_reason, result.steps[0].block_reason)
        self.assertEqual(executor.calls, [], "被 Guard 拒绝：执行器零调用")
        self.assertEqual(len(result.steps), 1)
        self.assertIsNone(result.steps[0].sql)
        self.assertEqual(validate_analysis_result(result), ())
        record = _values(agent, "t07-blocked")["analysis_record"]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["failed_index"], 1)
        self.assertEqual(record["reason_code"], "guard_blocked")
        self.assertEqual(record["steps"][-1]["role"], "baseline_total")
        self.assertNotIn("sql", record["steps"][-1])


class TestEligibilityPreGate(unittest.TestCase):
    """资格前置门：失败 → answer 父轮 + unavailable + steps=() 零 SQL（控制器裁决）。"""

    def _plain_agent(self, sid: str) -> tuple[DataAgent, AnalysisExecutor]:
        executor = AnalysisExecutor()
        agent = DataAgent(executor=executor, budget=_budget(), snapshot_meta=_meta())
        return agent, executor

    def test_missing_eligibility_attribute_yields_unavailable_answer_zero_steps(self) -> None:
        agent, executor = self._plain_agent("t07-gate1")
        result = agent.analyze(FULL_Q, session_id="t07-gate1")
        self.assertEqual(result.turn.kind, "answer")
        assert isinstance(result.plan, AnalysisPlan)
        self.assertEqual(result.plan.metric, "commission_revenue")
        self.assertEqual(result.steps, ())
        self.assertEqual(executor.calls, [], "前置门失败零 SQL")
        attr = result.attribution
        assert attr is not None
        self.assertEqual(attr.status, "unavailable")
        self.assertEqual(attr.reason_code, "missing_eligibility")
        self.assertIsNone(attr.baseline, "无实测不编造数字（N1）")
        self.assertIsNone(attr.current)
        self.assertIsNone(attr.delta)
        self.assertEqual(attr.items, ())
        self.assertEqual(result.reason_code, "missing_eligibility")
        self.assertEqual(validate_analysis_result(result), ())
        values = _values(agent, "t07-gate1")
        self.assertEqual(values.get("turns"), 1, "计一父轮")
        record = values["analysis_record"]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["reason_code"], "missing_eligibility")
        self.assertEqual(record["steps"], [])

    def test_ineligible_reason_maps_to_stable_safe_codes(self) -> None:
        cases = [
            ("snapshot_mismatch", "snapshot_mismatch"),
            ("semantic_hash_mismatch", "snapshot_mismatch"),
            ("missing_evidence", "missing_eligibility"),
            ("invalid_evidence", "missing_eligibility"),
            (None, "missing_eligibility"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                agent, executor = self._plain_agent(f"t07-gate-{expected}-{raw}")
                agent.analysis_eligibility = {  # type: ignore[attr-defined]
                    "available": False,
                    "eligible": False,
                    "reason": raw,
                    "evidence": None,
                }
                result = agent.analyze(FULL_Q, session_id=f"t07-gate-{expected}-{raw}")
                self.assertEqual(result.turn.kind, "answer")
                assert result.attribution is not None
                self.assertEqual(result.attribution.reason_code, expected)
                self.assertEqual(result.reason_code, expected)
                self.assertEqual(result.steps, ())
                self.assertEqual(executor.calls, [])
                self.assertEqual(validate_analysis_result(result), ())


class TestNoAnalysisIntent(unittest.TestCase):
    """无分析意图 → 恰好委托一次 ask，分析字段为 null。"""

    def test_plain_question_delegates_to_ask_exactly_once(self) -> None:
        executor = AnalysisExecutor()
        agent = _agent(executor)
        with mock.patch("agent.graph.record_turn") as rec:
            result = agent.analyze(NO_INTENT_Q, session_id="t07-plain")
        self.assertEqual(rec.call_count, 1, "ask 语义：父轮恰一次")
        self.assertIsNone(result.plan)
        self.assertEqual(result.steps, ())
        self.assertIsNone(result.attribution)
        self.assertIsNone(result.reason_code)
        self.assertEqual(result.turn.kind, "answer")
        self.assertEqual(result.turn.sql, executor.calls[0])
        self.assertEqual(len(executor.calls), 1)
        values = _values(agent, "t07-plain")
        self.assertEqual(values.get("turns"), 1)
        self.assertIsNone(values.get("analysis_record"), "委托轮不写分析记账")


class TestClarifyIntent(unittest.TestCase):
    """有意图澄清：计一父轮、零 SQL、零 LLM。"""

    def test_relative_time_question_counts_one_parent_turn_zero_sql_zero_llm(self) -> None:
        executor = AnalysisExecutor()
        agent = _agent(executor, generator=ForbiddenGenerator())
        with mock.patch("agent.graph.record_turn") as rec:
            result = agent.analyze(CLARIFY_Q, session_id="t07-clarify")
        self.assertEqual(rec.call_count, 1)
        self.assertEqual(result.turn.kind, "clarify")
        self.assertIsInstance(result.turn.clarification, ClarificationRequest)
        self.assertIsNone(result.plan)
        self.assertEqual(result.steps, ())
        self.assertIsNone(result.attribution)
        self.assertIsNone(result.reason_code)
        self.assertEqual(executor.calls, [], "澄清轮零 SQL")
        self.assertEqual(validate_analysis_result(result), ())
        values = _values(agent, "t07-clarify")
        self.assertEqual(values.get("turns"), 1, "澄清计一父轮")
        self.assertIsNone(values.get("analysis_record"), "澄清不开始分析记账")
        self.assertEqual(values.get("clarification"), result.turn.clarification)


class TestAnalysisContextLifecycle(unittest.TestCase):
    """分析上下文：逐步在位、成功后清除、异常中断不留陈旧上下文。"""

    def test_context_visible_to_every_step_and_cleared_after(self) -> None:
        agent = _agent(AnalysisExecutor())
        seen: list[dict[str, str] | None] = []
        original = agent._run_analysis_step

        def spy(plan: Plan, *, identity: dict[str, object] | None) -> TurnResult:
            seen.append(dict(agent._analysis_context) if agent._analysis_context else None)
            return original(plan, identity=identity)

        with mock.patch.object(agent, "_run_analysis_step", side_effect=spy):
            result = agent.analyze(FULL_Q, session_id="t07-ctx")
        self.assertEqual(result.turn.kind, "answer")
        self.assertEqual(seen, [{"session_id": "t07-ctx", "question": FULL_Q}] * 4)
        self.assertIsNone(agent._analysis_context, "成功路径结束必须清除上下文")

    def test_exception_leaves_no_stale_context(self) -> None:
        agent = _agent(AnalysisExecutor())
        sid = "t07-stale"
        real_write = agent._write_analysis_state
        calls = {"n": 0}

        def flaky(sid_: str, updates: dict[str, Any]) -> None:
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("disk full")
            real_write(sid_, updates)

        with (
            mock.patch.object(agent, "_write_analysis_state", side_effect=flaky),
            self.assertRaises(OSError),
        ):
            agent.analyze(FULL_Q, session_id=sid)
        self.assertEqual(calls["n"], 2, "异常发生在第一个子步证据写入时")
        self.assertIsNone(agent._analysis_context, "异常中断不得留下陈旧上下文")


class TestNoPerStepLiveAgent(unittest.TestCase):
    """统一 model/snapshot/身份/预算：不得逐步构建活图（兄弟图懒建 + 缓存）。"""

    def test_analyze_reuses_cached_sibling_graph(self) -> None:
        agent = _agent(AnalysisExecutor())
        with mock.patch("agent.graph.build_graph", wraps=build_graph) as build:
            agent.analyze(FULL_Q, session_id="t07-build1")
            self.assertEqual(build.call_count, 1, "首次 analyze 只懒构建一次兄弟图")
            agent.analyze(FULL_Q, session_id="t07-build2")
            self.assertEqual(build.call_count, 1, "第二次 analyze 不得再建图（更不得逐步建图）")


class TestBudgetDrivesGroupLimit(unittest.TestCase):
    """统一预算：group_limit = min(10000, budget.max_rows)（决策②）。"""

    def test_group_limit_is_min_of_10000_and_budget_max_rows(self) -> None:
        agent = _agent(AnalysisExecutor(), budget=_budget(max_rows=100))
        result = agent.analyze(FULL_Q, session_id="t07-limit")
        assert result.plan is not None
        self.assertEqual(result.plan.sub_plans[0].limit, 1, "总量步 limit=1")
        self.assertEqual(result.plan.sub_plans[2].limit, 100)
        self.assertEqual(result.plan.sub_plans[3].limit, 100)


class TestSameIdentityAdmission(unittest.TestCase):
    """同身份 analyze 准入正向用例（T06 评审遗留 M3）。"""

    def test_bound_session_accepts_same_identity_analyze(self) -> None:
        sid = "t07-ident"
        # 先行的普通 ask 占用执行器第 1 次调用：预置序列 = [普通问数结果] + 四步
        executor = AnalysisExecutor([([("x",)], ["commission_revenue"])] + _four_results())
        agent = _agent(executor)
        first = agent.ask(NO_INTENT_Q, session_id=sid, identity=HQ_CLAIMS)
        self.assertEqual(first.kind, "answer")
        result = agent.analyze(FULL_Q, session_id=sid, identity=HQ_CLAIMS)
        self.assertEqual(result.turn.kind, "answer")
        values = _values(agent, sid)
        record = values["analysis_record"]
        self.assertEqual(record["identity_fingerprint"], claims_fingerprint(HQ_CLAIMS))
        self.assertEqual(record["status"], "completed")
        self.assertEqual(values["turns"], 2)
        self.assertEqual(result.turn.turns_in_session, 2)


class TestIdentityConflictZeroWrite(unittest.TestCase):
    """异身份 analyze：冲突在任何写入之前抛出，轮数不变、零 SQL、零父轮埋点。"""

    def test_conflicting_identity_analysis_writes_nothing(self) -> None:
        executor = AnalysisExecutor()
        agent = _agent(executor)
        sid = "t07-conflict"
        agent.ask(NO_INTENT_Q, session_id=sid, identity=HQ_CLAIMS)
        turns_before = _values(agent, sid)["turns"]
        calls_before = len(executor.calls)
        with (
            mock.patch("agent.graph.record_turn") as rec,
            self.assertRaises(SessionIdentityConflict),
        ):
            agent.analyze(FULL_Q, session_id=sid, identity=BRANCH_CLAIMS)
        self.assertEqual(rec.call_count, 0, "冲突轮零父轮埋点")
        values = _values(agent, sid)
        self.assertEqual(values["turns"], turns_before, "冲突不推进轮数")
        self.assertEqual(len(executor.calls), calls_before, "冲突不执行 SQL")
        self.assertIsNone(values.get("analysis_record"), "冲突轮不写分析记账")


class TestAnalyzeJoinsInstanceLock(unittest.TestCase):
    """analyze 加入与 ask / run_plan 相同的实例锁：普通请求不得插入分析中间。"""

    def test_concurrent_ask_cannot_interleave_an_in_flight_analyze(self) -> None:
        gate = threading.Event()
        barrier = threading.Barrier(2)

        class GatedAnalysisExecutor(AnalysisExecutor):
            """第一次执行即与主线程会合、随后等待放行（T06 屏障模式同构）。"""

            def __init__(self, bar: threading.Barrier, rel: threading.Event) -> None:
                # 四步各占预置 1-4；分析放行后普通 ask 是第 5 次调用（尾补一条问数结果）
                super().__init__(_four_results() + [([("x",)], ["commission_revenue"])])
                self._barrier = bar
                self._gate = rel
                self._gated = False

            def __call__(self, sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
                self.calls.append(sql)
                if not self._gated:
                    self._gated = True
                    self._barrier.wait(timeout=10)
                    self._gate.wait(timeout=10)
                rows, columns = self._results[len(self.calls) - 1]
                return [tuple(r) for r in rows], list(columns)

        executor = GatedAnalysisExecutor(barrier, gate)
        agent = _agent(executor)
        sid = "t07-lock"
        errors: list[Exception] = []

        def _analysis() -> None:
            try:
                agent.analyze(FULL_Q, session_id=sid)
            except Exception as exc:  # noqa: BLE001 - 线程内失败经 errors 透出
                errors.append(exc)

        results: list[TurnResult] = []

        def _normal() -> None:
            results.append(agent.ask(NO_INTENT_Q, session_id=sid))

        worker = threading.Thread(target=_analysis)
        worker.start()
        barrier.wait(timeout=10)  # 与子步内的执行器会合：分析正持锁停在中间
        waiter = threading.Thread(target=_normal)
        waiter.start()
        time.sleep(0.25)  # 给普通线程足够的窗口：若锁失效它此刻已完成
        self.assertTrue(
            waiter.is_alive(),
            "普通请求在 analyze 持锁期间就完成了——实例锁失效",
        )
        self.assertEqual(len(executor.calls), 1, "普通请求在分析中间执行了 SQL（锁失效）")
        values = _values(agent, sid)
        self.assertEqual(values.get("turns"), 1, "普通请求在分析中间推进了轮数")
        self.assertEqual(values["analysis_record"]["status"], "running", "普通请求打断了分析记账")
        gate.set()
        worker.join(timeout=10)
        waiter.join(timeout=10)
        self.assertEqual(errors, [], f"分析线程失败：{errors}")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].kind, "answer")
        self.assertEqual(results[0].turns_in_session, 2)
        final = _values(agent, sid)
        self.assertIsNone(final.get("analysis_record"))
        self.assertEqual(len(executor.calls), 5)


class _OtelBase(unittest.TestCase):
    """每测试重建 in-memory provider + reader（与 tests/test_otel.py 同口径）。"""

    def setUp(self) -> None:
        self.exporter = InMemorySpanExporter()
        self.reader = InMemoryMetricReader()
        otel.configure_otel(
            tracer_provider=TracerProvider(
                active_span_processor=SimpleSpanProcessor(self.exporter)
            ),
            metric_reader=self.reader,
        )
        self.executor = AnalysisExecutor()
        self.agent = _agent(self.executor)

    def finished_spans(self) -> list[Span]:
        return list(self.exporter.get_finished_spans())

    def span_attrs(self, span: Span) -> dict[str, Any]:
        return dict((span.attributes or {}).items())

    def metric_names(self) -> list[str]:
        data = self.reader.get_metrics_data()
        return [
            metric.name
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        ]

    def metric_value(self, name: str) -> int | float | None:
        data = self.reader.get_metrics_data()
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name != name:
                        continue
                    total = 0
                    for point in metric.data.data_points:
                        total += point.value  # type: ignore[attr-defined]
                    return total
        return None


class TestAnalyzeObservability(_OtelBase):
    """父子观测：父轮 span 恰一次、每执行步 span 恰一次、步骤零指标。"""

    def test_full_flow_records_parent_turn_once_and_step_span_per_step(self) -> None:
        result = self.agent.analyze(FULL_Q, session_id="t07-otel")
        self.assertEqual(result.turn.kind, "answer")
        spans = self.finished_spans()
        self.assertEqual(
            [s.name for s in spans],
            ["atlas.analysis.step"] * 4 + ["atlas.turn"],
            "四步 span 在前、父轮 span 最后恰一次",
        )
        step_spans = spans[:4]
        roles = [self.span_attrs(s)["atlas.analysis.role"] for s in step_spans]
        self.assertEqual(roles, list(ANALYSIS_ROLES))
        parent_attrs = self.span_attrs(spans[-1])
        self.assertEqual(parent_attrs["atlas.question_id"], "t07-otel#t1")
        self.assertEqual(parent_attrs["atlas.turn.kind"], "answer")
        step_attrs = self.span_attrs(step_spans[0])
        self.assertEqual(step_attrs["atlas.question_id"], "t07-otel#t1")
        self.assertEqual(step_attrs["atlas.session_id"], "t07-otel")
        self.assertEqual(step_attrs["atlas.metric_id"], "commission_revenue")
        self.assertIn("SELECT", step_attrs["atlas.turn.sql"])
        self.assertEqual(step_attrs["atlas.model"], self.agent.model.name)
        # 只写步骤 span：不产出任何 atlas.analysis.* 指标（决策⑥）
        analysis_metrics = [n for n in self.metric_names() if n.startswith("atlas.analysis")]
        self.assertEqual(analysis_metrics, [], f"分析步骤不得产出指标：{analysis_metrics}")
        # 父轮计数仍只加一（子步不计用户轮）
        self.assertEqual(self.metric_value("atlas.turn.count"), 1)

    def test_failed_step_carries_error_kind_on_its_span(self) -> None:
        self.executor = AnalysisExecutor(fail_on=3)
        self.agent = _agent(self.executor)
        result = self.agent.analyze(FULL_Q, session_id="t07-otel-fail")
        self.assertEqual(result.turn.kind, "error")
        spans = self.finished_spans()
        self.assertEqual(len(spans), 4, "3 个已执行步 span（含失败步终态 span）+ 1 父轮 span")
        self.assertEqual([s.name for s in spans], ["atlas.analysis.step"] * 3 + ["atlas.turn"])
        self.assertEqual(
            [self.span_attrs(s)["atlas.turn.kind"] for s in spans],
            ["answer", "answer", "error", "error"],
        )
        self.assertEqual(self.span_attrs(spans[2])["atlas.analysis.role"], "current_by_dimension")


class TestAnalysisStepTelemetryIsolation(unittest.TestCase):
    """record_analysis_step 自身：no-op 缺省、畸形输入告警隔离不反噬。"""

    def test_broken_step_record_warns_once_and_flow_unaffected(self) -> None:
        exporter = InMemorySpanExporter()
        otel.configure_otel(
            tracer_provider=TracerProvider(active_span_processor=SimpleSpanProcessor(exporter)),
            metric_reader=InMemoryMetricReader(),
        )
        with self.assertWarns(RuntimeWarning):
            otel.record_analysis_step(
                "not-a-turn",  # type: ignore[arg-type]
                model=SemanticModel(),
                session_id="s",
                turns=1,
                role="baseline_total",
            )
        # 主链路不受影响：合法步骤 span 照常记录
        turn = TurnResult(kind="answer", session_id="s", question="q", metric="commission_revenue")
        otel.record_analysis_step(
            turn, model=SemanticModel(), session_id="s", turns=1, role="baseline_total"
        )
        self.assertEqual(len(exporter.get_finished_spans()), 1)

    def test_step_span_only_without_parent_span_and_noop_without_configure(self) -> None:
        otel.configure_otel()  # 无 endpoint / 无注入 → no-op 配置
        turn = TurnResult(
            kind="answer", session_id="s", question="q", metric="commission_revenue", latency_ms=1.0
        )
        otel.record_analysis_step(
            turn, model=SemanticModel(), session_id="s", turns=3, role="current_total"
        )  # 不抛、零 I/O

    def test_step_span_is_step_only(self) -> None:
        exporter = InMemorySpanExporter()
        otel.configure_otel(
            tracer_provider=TracerProvider(active_span_processor=SimpleSpanProcessor(exporter)),
            metric_reader=InMemoryMetricReader(),
        )
        turn = TurnResult(
            kind="answer", session_id="s", question="q", metric="commission_revenue", latency_ms=2.0
        )
        otel.record_analysis_step(
            turn, model=SemanticModel(), session_id="s", turns=7, role="baseline_total"
        )
        spans = list(exporter.get_finished_spans())
        self.assertEqual(len(spans), 1, "只写步骤 span，不得附带父轮 span 或指标")
        attrs = dict((spans[0].attributes or {}).items())
        self.assertEqual(spans[0].name, "atlas.analysis.step")
        self.assertEqual(attrs["atlas.analysis.role"], "baseline_total")
        self.assertEqual(attrs["atlas.question_id"], "s#t7")
        self.assertEqual(attrs["atlas.metric_id"], "commission_revenue")


if __name__ == "__main__":
    unittest.main()
