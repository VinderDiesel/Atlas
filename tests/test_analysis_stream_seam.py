"""分析编排「分步接缝」契约测试（ADR-0028 GATE-④ 技术前置就位）。

被测对象是 `DataAgent._iter_analysis_step_events`——把 `analyze()` 内部四步循环
抽成的**私有生成器**：每执行一步就产出一个 `AnalysisStepEvent`（role + 该步
TurnResult + 该步证据 + 终态原因码）。

边界（务必读准，避免把"接缝就绪"误读为"流式已实现"）：
- 这是 ADR-0028 GATE-④「编排器需先有稳定分步产物可流」的**技术前置**，不是
  流式端点本身。**没有**任何 SSE/HTTP 端点消费它，公开 API 面 / 契约 17 条
  不变，对外**不宣称** AG-UI 兼容（N2 / 裁定 C）。
- 生成器只在 `analyze()` 同一把锁内被**急切驱动**；本测试直接驱动生成器只为
  锁定它的分步契约，不代表存在可慢读的公开流。

全部走 fake 执行器 / 快照注入（与 tests/test_analysis_agent.py 同口径），不碰
网络与 Doris。数字均为合成测试输入，非快照实测值（N1）。
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from agent.analysis import (
    ANALYSIS_REASON_CODES,
    ANALYSIS_ROLES,
    AnalysisPlan,
    AnalysisPlanner,
    AnalysisStepEvent,
)
from agent.graph import DataAgent
from agent.state import TurnResult, decode_rows

# 复用 test_analysis_agent.py 已验证的 fake 夹具（不重造桩，避免伪造测试）。
from tests.test_analysis_agent import (
    ELIGIBLE,
    FULL_Q,
    AnalysisExecutor,
    _agent,
    _meta,
    _values,
)

DENY_BUDGET_ROLE_ORDER = list(ANALYSIS_ROLES)


def _plan(agent: DataAgent) -> AnalysisPlan:
    """用与 analyze() 完全相同的 group_limit 口径解析出四步计划（决策②）。"""
    budget = agent._graph_kwargs["budget"]  # noqa: SLF001 - 镜像 analyze 内部口径
    group_limit = min(10_000, int(budget.max_rows))
    parsed = AnalysisPlanner(agent.model, group_limit=group_limit).plan(FULL_Q)
    assert isinstance(parsed, AnalysisPlan), "FULL_Q 必须解析为四步 AnalysisPlan"
    return parsed


def _drive(agent: DataAgent, plan: AnalysisPlan, sid: str) -> list[AnalysisStepEvent]:
    """镜像 analyze() 进入四步前的前置副作用，再**急切**耗尽生成器。

    与 analyze() 的调用契约一致：先落准入证据（父轮 running 记录 + 计划投影 /
    两期绑定 / 版本），再置子步上下文，最后驱动生成器。生成器本身不负责这些
    前置（那是 analyze 的职责），故此处先做，才等价于真实路径。
    """
    agent._begin_analysis(sid, FULL_Q, None)  # noqa: SLF001
    agent._write_analysis_state(  # noqa: SLF001
        sid,
        {
            "analysis_record": {
                "plan_projections": [],
                "baseline": {"granularity": "quarter", "value": "2013Q3"},
                "current": {"granularity": "quarter", "value": "2013Q4"},
                "snapshot_sha": None,
                "semantic_sha256": None,
            }
        },
    )
    agent._analysis_context = {"session_id": sid, "question": FULL_Q}  # noqa: SLF001
    try:
        return list(
            agent._iter_analysis_step_events(plan, identity=None, session_id=sid, parent_no=1)
        )
    finally:
        agent._analysis_context = None  # noqa: SLF001


class SeamFullSuccess(unittest.TestCase):
    """四步全成：逐 role 产出、每事件证据与旧 analyze 落账形状逐字一致。"""

    def test_yields_four_events_in_role_order(self) -> None:
        executor = AnalysisExecutor()
        agent = _agent(executor)
        plan = _plan(agent)
        events = _drive(agent, plan, "seam-full")

        self.assertEqual([e.role for e in events], DENY_BUDGET_ROLE_ORDER, "yield 顺序即角色序")
        self.assertEqual(len(events), 4)
        self.assertEqual(executor.calls, [e.step.sql for e in events], "每步恰执行一条 SQL")
        for ev, role in zip(events, ANALYSIS_ROLES, strict=True):
            self.assertIsInstance(ev, AnalysisStepEvent)
            self.assertIsInstance(ev.step, TurnResult)
            self.assertEqual(ev.step.kind, "answer")
            self.assertIsNone(ev.reason_code)
            # 证据条目：成功步形状与 analyze 旧内联一致（index/role/status/sql/
            # columns/rows/latency_ms），供父轮记账与旧路逐字等价。
            entry = ev.evidence_entry
            self.assertEqual(entry["role"], role)
            self.assertEqual(entry["status"], "ok")
            self.assertEqual(entry["sql"], ev.step.sql)
            self.assertEqual(entry["columns"], list(ev.step.columns))
            self.assertEqual(decode_rows(entry["rows"]), ev.step.rows)
            self.assertEqual(entry["latency_ms"], ev.step.latency_ms)

    def test_records_one_step_span_per_event_in_role_order(self) -> None:
        executor = AnalysisExecutor()
        agent = _agent(executor)
        plan = _plan(agent)
        with mock.patch("agent.graph.record_analysis_step") as rec:
            _drive(agent, plan, "seam-span")
        self.assertEqual(rec.call_count, 4, "每执行一步恰一条步骤 span")
        self.assertEqual([c.kwargs["role"] for c in rec.call_args_list], DENY_BUDGET_ROLE_ORDER)


class SeamBlockedTrims(unittest.TestCase):
    """Guard 拒绝首步：单事件 blocked、被拒 SQL 不泄露（N3）、失败步角色保留。"""

    def test_first_step_blocked_yields_single_event_without_sql(self) -> None:
        from agent.security.sql_guard import Budget

        deny = Budget(
            dialect="doris", max_rows=10_000, allowed_tables=frozenset({"atlas.dws.other"})
        )
        executor = AnalysisExecutor()
        agent = DataAgent(executor=executor, budget=deny, snapshot_meta=_meta())
        agent.analysis_eligibility = ELIGIBLE  # type: ignore[attr-defined]
        plan = _plan(agent)
        events = _drive(agent, plan, "seam-blocked")

        self.assertEqual(len(events), 1, "blocked 即裁剪终止：仅一步事件")
        ev = events[0]
        self.assertEqual(ev.role, "baseline_total")
        self.assertEqual(ev.step.kind, "blocked")
        self.assertEqual(ev.reason_code, "guard_blocked")
        self.assertIn(ev.reason_code, ANALYSIS_REASON_CODES)
        # N3：被拒 SQL 不得出现在事件里（step.sql 为 None，证据无 sql 键）
        self.assertIsNone(ev.step.sql)
        self.assertNotIn("sql", ev.evidence_entry)
        self.assertEqual(executor.calls, [], "被 Guard 拒绝：执行器零调用")


class SeamFailureTruncates(unittest.TestCase):
    """执行期故障在第 k 步：产出 k 个事件、末事件 error、失败后零调用。"""

    def test_failure_at_each_index_trims_events(self) -> None:
        for k in (1, 2, 3, 4):
            with self.subTest(k=k):
                executor = AnalysisExecutor(fail_on=k)
                agent = _agent(executor)
                plan = _plan(agent)
                events = _drive(agent, plan, f"seam-fail{k}")

                self.assertEqual(len(events), k, "失败步含自身，共 k 个事件")
                self.assertEqual([e.role for e in events], DENY_BUDGET_ROLE_ORDER[:k])
                self.assertEqual(events[-1].step.kind, "error")
                self.assertEqual(events[-1].reason_code, "execution_error")
                self.assertTrue(all(e.step.kind == "answer" for e in events[:-1]))
                self.assertEqual(len(executor.calls), k, "失败后零调用")


class SeamEvidencePersistence(unittest.TestCase):
    """生成器保留逐步落账副作用：驱动后 checkpoint 里 steps 与事件证据一致。"""

    def test_state_steps_match_emitted_evidence(self) -> None:
        executor = AnalysisExecutor()
        agent = _agent(executor)
        plan = _plan(agent)
        events = _drive(agent, plan, "seam-state")
        record: dict[str, Any] = _values(agent, "seam-state")["analysis_record"]
        persisted = record["steps"]
        self.assertEqual([e["role"] for e in persisted], [ev.role for ev in events])
        self.assertEqual([e["index"] for e in persisted], list(range(1, len(events) + 1)))
        for stored, ev in zip(persisted, events, strict=True):
            self.assertEqual(stored["sql"], ev.step.sql)


if __name__ == "__main__":
    unittest.main()
