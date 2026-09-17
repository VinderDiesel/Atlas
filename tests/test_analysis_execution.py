"""同源无状态子执行通道契约测试（ADR-0026 决策 ③，T05）。

口径（与 ADR-0026 / task-5-brief 一致）：
- **现状证明先行**：随机 `run_plan` 仍写 saver（saver spy 记录 get/put/put_writes），
  这是「为什么需要无状态通道」的证据，不是要修的缺陷；
- `persist=False` 编译**真正无 checkpointer** 的同源图（不是新建 MemorySaver）；
  子步经 `DataAgent._run_analysis_step` 全新输入 state + `plan_override` 调用，
  identity 与 `_invoke_turn` 同通道下推（`config["configurable"]["identity"]`）；
- **零持久化硬门**：子步全程 saver 的 get/put/put_writes 零调用、不写身份指纹、
  不埋点（record_turn 零新增）、不推进用户会话轮数；
- **等价**：两路（run_plan vs 子步）节点/边集合相等；同一 Plan 的 Guard 出口
  SQL、rows、拒绝结果相等；
- **失败步执行事实**：error/blocked 终态同样带 latency_ms 与已执行证据，并区分
  「未执行被拒」（Guard 拒绝 / 编译失败 / 身份解析失败 → sql=None）与
  「执行后失败」（执行器异常 → 记录已过 Guard 的 SQL + 尝试耗时）——T07 的
  耗时汇总只计实际执行的子 SQL。

全部走 fake 执行器/快照注入，不碰网络与 Doris（与 tests/test_graph.py 同口径）。
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from agent.compiler import OrderSpec, Plan, TimeSpec
from agent.graph import DataAgent, build_graph
from agent.security.sql_guard import Budget
from agent.state import TurnResult

REPO = Path(__file__).resolve().parent.parent

# 锁定快照表白名单（与 tests/test_graph.py 同口径：只能触碰已锁快照的表）
_META = json.loads((REPO / "data/snapshots" / "7d48dcb.meta.json").read_text(encoding="utf-8"))
ALLOWED = frozenset(
    f"atlas.{ns}.{table}" for ns, tables in _META["row_counts"].items() for table in tables
)
BUDGET = Budget(dialect="doris", max_rows=10_000, allowed_tables=ALLOWED)
# Guard 必拒预算：白名单不含佣金表（非法表场景用）
DENY_BUDGET = Budget(
    dialect="doris", max_rows=10_000, allowed_tables=frozenset({"atlas.dwd.other"})
)

GOLD102_Q = "按分支统计 2013 年佣金收入，列出前 5 名"

# 序列化白名单按**内容**自建（不从 agent.graph 私有常量抄身份，与
# tests/test_session_persistence.py 同口径）：spy 要真实承接 run_plan 的写入，
# 白名单漏传会让 Plan 退化为 dict，现状证明就失真了。
ALLOWLIST_PAIRS = (
    ("agent.compiler", "TimeSpec"),
    ("agent.compiler", "OrderSpec"),
    ("agent.compiler", "Filter"),
    ("agent.compiler", "ComparisonSpec"),
    ("agent.compiler", "Plan"),
    ("agent.planner", "ClarificationRequest"),
)


class SaverSpy(MemorySaver):
    """带调用计数的 MemorySaver：行为与真 saver 一致，额外记录 get/put/put_writes。

    为什么计数而不拦改：要证明的是「现状确实在写」与「子步一次都不写」，
    行为必须与未包装时完全一致——计数是唯一允许的差异。
    """

    def __init__(self) -> None:
        super().__init__(serde=JsonPlusSerializer(allowed_msgpack_modules=ALLOWLIST_PAIRS))
        self.n_get = 0
        self.n_put = 0
        self.n_put_writes = 0

    def get_tuple(self, config: dict[str, Any]) -> Any:
        self.n_get += 1
        return super().get_tuple(config)

    def put(self, config: dict[str, Any], checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        self.n_put += 1
        return super().put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config: dict[str, Any], writes: Any, task_id: str, **kw: Any) -> Any:
        self.n_put_writes += 1
        return super().put_writes(config, writes, task_id, **kw)


class FakeExecutor:
    """记录收到的 SQL（已过 Guard），返回固定结果集（与 tests/test_graph.py 同构）。"""

    def __init__(self, rows: list[tuple[Any, ...]] = (("v",),)) -> None:
        self.calls: list[str] = []
        self.rows = rows

    def __call__(self, sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
        self.calls.append(sql)
        return [tuple(r) for r in self.rows], ["v"]


class SlowRaisingExecutor:
    """先耗时再抛（模拟 Doris 执行期故障）：证明「执行后失败」能记下尝试耗时。"""

    def __init__(self, exc: Exception, seconds: float = 0.01) -> None:
        self.exc = exc
        self.seconds = seconds
        self.calls: list[str] = []

    def __call__(self, sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
        self.calls.append(sql)
        time.sleep(self.seconds)
        raise self.exc


class CountingLinker:
    """记录 link 调用的检索桩：plan_override 短路时它必须零调用。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def link(self, question: str, k: int = 5) -> Any:
        self.calls.append(question)
        return SimpleNamespace(candidates=("commission_revenue",))


class ForbiddenGenerator:
    """plan_override 通道不得进入候选链：generate 被调 = 测试当场失败。"""

    engine = "stub"

    def generate(self, question: str, k: int = 5) -> Any:
        raise AssertionError("plan_override 通道不得进入候选链 generate")


def _commission_plan() -> Plan:
    """gold-102 同构的固定 Plan（与 tests/test_graph.py 的 _answer_plan 同口径）。"""
    return Plan(
        metric="commission_revenue",
        dimensions=("Branch",),
        time=TimeSpec("year", 2013),
        order_by=(OrderSpec("commission_revenue", desc=True),),
        limit=5,
    )


def _compile_broken_plan() -> Plan:
    """语义层不存在的指标：node_execute 现场 CompileError（ADR-0022 代价 ⑤）。"""
    return Plan(metric="no_such_metric", dimensions=("Branch",), time=TimeSpec("year", 2013))


def _claims(role: str, **user_context: object) -> dict[str, object]:
    """与 verify_token 输出同形态（sub/role/user_context…），tests/test_graph.py 同构。"""
    return {"sub": "contract-user", "role": role, "user_context": user_context}


HQ_CLAIMS = _claims("hq_admin")


class TestPersistContract(unittest.TestCase):
    """build_graph(persist=...) 契约：默认逐字节不变，False = 真无 checkpointer。"""

    def _build(self, **kw: Any):
        kw.setdefault("executor", FakeExecutor())
        kw.setdefault("budget", BUDGET)
        return build_graph(**kw)

    def test_default_still_builds_fresh_memory_saver(self) -> None:
        """默认行为逐字节一致：None → 每图新建 MemorySaver（ADR-0020 决策 ①）。"""
        g1 = self._build()
        g2 = self._build()
        self.assertIsInstance(g1.checkpointer, MemorySaver)
        self.assertIsInstance(g2.checkpointer, MemorySaver)
        self.assertIsNot(g1.checkpointer, g2.checkpointer)

    def test_injected_saver_used_verbatim(self) -> None:
        """注入实例原样使用（不复制不重包）。"""
        spy = SaverSpy()
        self.assertIs(self._build(checkpointer=spy).checkpointer, spy)

    def test_persist_false_compiles_without_checkpointer(self) -> None:
        """persist=False：编译不带 checkpointer——是 None，不是新建 MemorySaver。"""
        self.assertIsNone(self._build(persist=False).checkpointer)

    def test_persist_false_with_checkpointer_is_config_error(self) -> None:
        """persist=False 与注入 checkpointer 同时出现 = 配置错误（ADR-0026 L149）。"""
        with self.assertRaises(ValueError):
            self._build(persist=False, checkpointer=SaverSpy())


class TestRunPlanStillPersists(unittest.TestCase):
    """现状证明（brief 检查单第 1 条前半）：随机 run_plan 仍写 saver。"""

    def test_run_plan_reads_and_writes_saver(self) -> None:
        spy = SaverSpy()
        agent = DataAgent(executor=FakeExecutor(), budget=BUDGET, checkpointer=spy)
        r = agent.run_plan(_commission_plan(), identity=HQ_CLAIMS)
        self.assertEqual(r.kind, "answer")
        # 带 identity 的 run_plan：先 get_state 比对指纹，再 invoke 写 checkpoint
        self.assertGreaterEqual(spy.n_get, 1)
        self.assertGreaterEqual(spy.n_put, 1)


class TestStatelessStepNoSaver(unittest.TestCase):
    """零持久化硬门：子步全程 saver 的 get/put/put_writes 零调用。"""

    def test_step_never_touches_saver(self) -> None:
        spy = SaverSpy()
        executor = FakeExecutor(rows=[("分支A", 100.0)])
        agent = DataAgent(executor=executor, budget=BUDGET, checkpointer=spy)
        r = agent._run_analysis_step(_commission_plan(), identity=HQ_CLAIMS)
        self.assertEqual(r.kind, "answer")
        self.assertEqual(len(executor.calls), 1)  # SQL 照常执行（只免持久化）
        self.assertEqual(spy.n_get, 0)
        self.assertEqual(spy.n_put, 0)
        self.assertEqual(spy.n_put_writes, 0)


class TestStatelessTopology(unittest.TestCase):
    """同源证明：persist 两态共享同一套节点定义（无复制）。"""

    def _build(self, **kw: Any):
        kw.setdefault("executor", FakeExecutor())
        kw.setdefault("budget", BUDGET)
        return build_graph(**kw)

    @staticmethod
    def _shape(graph: Any) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
        """拓扑形状：节点名集合 + (source, target) 边集合（与实例无关的可比口径）。"""
        wrapped = graph.get_graph()
        nodes = frozenset(wrapped.nodes.keys())
        edges = frozenset((e.source, e.target) for e in wrapped.edges)
        return nodes, edges

    def test_persist_modes_share_nodes_and_edges(self) -> None:
        stateful = self._build()
        stateless = self._build(persist=False)
        self.assertEqual(self._shape(stateless), self._shape(stateful))

    def test_agent_sibling_graph_same_topology_and_cached(self) -> None:
        executor = FakeExecutor()
        agent = DataAgent(executor=executor, budget=BUDGET)
        sibling = agent._stateless_graph()
        self.assertEqual(self._shape(sibling), self._shape(agent._graph))
        self.assertIsNone(sibling.checkpointer)
        self.assertIs(sibling, agent._stateless_graph())  # 每 agent 只建一次


class TestSamePlanEquivalence(unittest.TestCase):
    """等价断言：同一 Plan 两路的 Guard 出口 SQL、rows、拒绝结果相等。"""

    def test_same_plan_same_guard_exit_sql_and_rows(self) -> None:
        executor = FakeExecutor(rows=[("分支A", 100.0)])
        agent = DataAgent(executor=executor, budget=BUDGET)
        r_plan = agent.run_plan(_commission_plan())
        r_step = agent._run_analysis_step(_commission_plan(), identity=None)
        self.assertEqual(r_plan.kind, "answer")
        self.assertEqual(r_step.kind, "answer")
        # Guard 出口 SQL 逐字符一致 + 执行器收到的就是它
        self.assertEqual(r_plan.sql, r_step.sql)
        self.assertEqual(r_step.sql, executor.calls[1])
        self.assertEqual(r_plan.rows, r_step.rows)
        self.assertEqual(r_step.rows, (("分支A", 100.0),))

    def test_same_plan_same_identity_policy(self) -> None:
        """同一 identity 下推：两路谓词注入一致（与 _invoke_turn 同通道）。"""
        executor = FakeExecutor()
        agent = DataAgent(executor=executor, budget=BUDGET)
        r_plan = agent.run_plan(_commission_plan(), identity=HQ_CLAIMS)
        r_step = agent._run_analysis_step(_commission_plan(), identity=HQ_CLAIMS)
        self.assertEqual(r_plan.sql, r_step.sql)
        self.assertIn("1 = 1", r_step.sql or "")

    def test_same_plan_same_guard_rejection(self) -> None:
        """同一拒绝预算：两路 kind=blocked 且拒绝原因一致；被拒 SQL 不达执行器。"""
        executor = FakeExecutor()
        agent = DataAgent(executor=executor, budget=DENY_BUDGET)
        r_plan = agent.run_plan(_commission_plan())
        r_step = agent._run_analysis_step(_commission_plan(), identity=None)
        self.assertEqual(r_plan.kind, "blocked")
        self.assertEqual(r_step.kind, "blocked")
        self.assertEqual(r_plan.block_reason, r_step.block_reason)
        self.assertEqual(executor.calls, [])


class TestStepFailureFacts(unittest.TestCase):
    """失败步执行事实：区分「未执行被拒」与「执行后失败」（brief 检查单第 4 条）。"""

    def test_illegal_table_blocked_never_executes(self) -> None:
        """非法表 → Guard 拒绝：未执行被拒（sql=None、latency=0、执行器零调用）。"""
        executor = FakeExecutor()
        agent = DataAgent(executor=executor, budget=DENY_BUDGET)
        r = agent._run_analysis_step(_commission_plan(), identity=None)
        self.assertEqual(r.kind, "blocked")
        self.assertNotEqual(r.block_reason, "")
        self.assertEqual(executor.calls, [])
        self.assertIsNone(r.sql)
        self.assertEqual(r.latency_ms, 0.0)

    def test_unresolvable_role_errors_before_execution(self) -> None:
        """权限解析失败（未知角色）→ error：未执行（sql=None、执行器零调用）。"""
        executor = FakeExecutor()
        agent = DataAgent(executor=executor, budget=BUDGET)
        r = agent._run_analysis_step(_commission_plan(), identity=_claims("ceo_omniscient"))
        self.assertEqual(r.kind, "error")
        self.assertIn("身份策略解析失败", r.error or "")
        self.assertEqual(executor.calls, [])
        self.assertIsNone(r.sql)
        self.assertEqual(r.latency_ms, 0.0)

    def test_compile_failure_errors_before_execution(self) -> None:
        """编译失败 → error：未执行（sql=None、执行器零调用）。"""
        executor = FakeExecutor()
        agent = DataAgent(executor=executor, budget=BUDGET)
        r = agent._run_analysis_step(_compile_broken_plan(), identity=None)
        self.assertEqual(r.kind, "error")
        self.assertIn("CompileError", r.error or "")
        self.assertEqual(executor.calls, [])
        self.assertIsNone(r.sql)
        self.assertEqual(r.latency_ms, 0.0)

    def test_executor_failure_records_executed_evidence_and_latency(self) -> None:
        """执行器异常 → error：执行后失败——已过 Guard 的 SQL 与尝试耗时如实记录。"""
        executor = SlowRaisingExecutor(RuntimeError("doris 断连"))
        agent = DataAgent(executor=executor, budget=BUDGET)
        r = agent._run_analysis_step(_commission_plan(), identity=None)
        self.assertIsInstance(r, TurnResult)
        self.assertEqual(r.kind, "error")
        self.assertIn("RuntimeError", r.error or "")
        self.assertEqual(len(executor.calls), 1)
        # 已执行证据：error 终态也携带本轮已过 Guard 的 SQL（与执行器实收一致）
        self.assertEqual(r.sql, executor.calls[0])
        self.assertGreaterEqual(r.latency_ms, 8.0)


class TestStepIsolation(unittest.TestCase):
    """子步隔离：不走候选链、不埋点、不推进用户轮数、父上下文按约定透传。"""

    def test_step_never_enters_candidate_chain(self) -> None:
        """allow_candidate=True 下 plan_override 仍短路：linker/generator 零调用。"""
        linker = CountingLinker()
        agent = DataAgent(
            executor=FakeExecutor(),
            budget=BUDGET,
            allow_candidate=True,
            linker=linker,
            generator=ForbiddenGenerator(),
        )
        r = agent._run_analysis_step(_commission_plan(), identity=None)
        self.assertEqual(r.kind, "answer")
        self.assertEqual(linker.calls, [])

    def test_step_does_not_record_turn(self) -> None:
        """不写用户轮指标：record_turn 在 ask 时调用、子步零新增。"""
        agent = DataAgent(executor=FakeExecutor(), budget=BUDGET)
        with mock.patch("agent.graph.record_turn") as rec:
            agent.ask(GOLD102_Q)
            n_after_ask = rec.call_count
            agent._run_analysis_step(_commission_plan(), identity=None)
            self.assertEqual(rec.call_count, n_after_ask)

    def test_step_does_not_advance_session_turns(self) -> None:
        """父会话轮数不被子步推进；子步自身 turns 是局部产物（每步从 1 起）。"""
        agent = DataAgent(executor=FakeExecutor(), budget=BUDGET)
        r1 = agent.ask(GOLD102_Q, session_id="s-iso")
        self.assertEqual(r1.turns_in_session, 1)
        r_step = agent._run_analysis_step(_commission_plan(), identity=None)
        self.assertEqual(r_step.turns_in_session, 1)
        r2 = agent.ask(GOLD102_Q, session_id="s-iso")
        self.assertEqual(r2.turns_in_session, 2)

    def test_step_carries_parent_context_when_provided(self) -> None:
        """T07 缝隙：分析上下文在位时，子步问句/会话键取父轮值。"""
        agent = DataAgent(executor=FakeExecutor(), budget=BUDGET)
        agent._analysis_context = {"session_id": "s-parent", "question": "各期间佣金对比"}
        r = agent._run_analysis_step(_commission_plan(), identity=None)
        self.assertEqual(r.session_id, "s-parent")
        self.assertEqual(r.question, "各期间佣金对比")

    def test_step_falls_back_without_context(self) -> None:
        """无上下文（直调）：随机会话键 + plan:<metric> 缺省问句（run_plan 同口径）。"""
        agent = DataAgent(executor=FakeExecutor(), budget=BUDGET)
        r = agent._run_analysis_step(_commission_plan(), identity=None)
        self.assertTrue(r.session_id.startswith("session-"))
        self.assertEqual(r.question, "plan:commission_revenue")


if __name__ == "__main__":
    unittest.main()
