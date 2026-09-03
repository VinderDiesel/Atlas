"""graph 状态机契约测试（Day 43-44）：路由口径 + 歧义反问 + Guard + 多轮。

口径（与 agent/graph.py docstring 一致）：
- 执行器与预算全部 fake/快照注入，不碰网络与 Doris（候选链 LLM 不在契约内）；
- 歧义反问验收 = 金融域 4 条歧义 gold（gold-104/121/122/148，与 eval/runner
  clarify 4/4 同源）在状态机内全部 kind=clarify 且不执行 SQL（不猜答）；
- Guard 拦截断言：被拒 SQL 不达执行器（N3 纵深防御：blocked 只给原因）。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent.compiler import CompileError, Compiler, OrderSpec, Plan, SemanticModel, TimeSpec
from agent.generator import GenerationRefusal, GenerationResult
from agent.graph import DataAgent, build_graph, turn_from_state
from agent.security.sql_guard import Budget, enforce

REPO = Path(__file__).resolve().parent.parent
GOLD_DIR = REPO / "eval" / "gold"

# 锁定快照表白名单（测试与评测同口径：只能触碰已锁快照的表）
_META = json.loads((REPO / "data/snapshots" / "7d48dcb.meta.json").read_text(encoding="utf-8"))
ALLOWED = frozenset(
    f"atlas.{ns}.{table}" for ns, tables in _META["row_counts"].items() for table in tables
)
BUDGET = Budget(dialect="doris", max_rows=10_000, allowed_tables=ALLOWED)
MODEL = SemanticModel()

GOLD102_Q = "按分支统计 2013 年佣金收入，列出前 5 名"
# 4 条金融歧义 gold 问句（Day 44 验收：全部反问不猜）
AMBIGUOUS_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("gold-104", "最近交易情况怎么样？"),
    ("gold-121", "上个月总成交量是多少？"),
    ("gold-122", "2013 年成交量和交易额分别是多少？"),
    ("gold-148", "2014 年成交笔数和成交证券数分别是多少？"),
)
OUT_OF_DOMAIN_Q = "2013年各分支机构的绩效奖金总额排名"  # 域外措辞：无同义词命中


def load_gold_question(gold_id: str) -> str:
    """gold JSON 的 question 字段（与 runner 同源，防止测试问句漂移）。"""
    path = GOLD_DIR / f"{gold_id}.json"
    return str(json.loads(path.read_text(encoding="utf-8"))["question"])


class FakeExecutor:
    """记录收到的 SQL（已过 Guard），返回固定结果集。"""

    def __init__(
        self,
        rows: list[tuple[Any, ...]] = (("v",),),
        columns: list[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.rows = rows
        self.columns = list(columns or ["v"])

    def __call__(self, sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
        self.calls.append(sql)
        return [tuple(r) for r in self.rows], list(self.columns)


class RaisingExecutor:
    """模拟 Doris 执行期故障（断连/语法）。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls: list[str] = []

    def __call__(self, sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
        self.calls.append(sql)
        raise self.exc


class FlakyCompiler:
    """首调抛 CompileError（模拟候选不可编译），此后委托真实编译器。"""

    def __init__(self) -> None:
        self.inner = Compiler(MODEL)
        self.fail_first = True

    def compile(self, plan: Plan) -> tuple[str, list[str]]:
        if self.fail_first:
            self.fail_first = False
            raise CompileError("flaky 模拟的编译失败（仅测试）")
        return self.inner.compile(plan)


class FakeLinker:
    """注入候选的 schema linker（测试 retrieve/generate 链路由，不碰真实检索）。"""

    def __init__(self, candidates: tuple[str, ...] = ("commission_revenue",)) -> None:
        self.candidates = candidates
        self.calls: list[str] = []

    def link(self, question: str, k: int = 5):  # noqa: ANN001 - 图只用 .candidates
        self.calls.append(question)
        return SimpleNamespace(candidates=self.candidates)


class FakeGenerator:
    """可编程生成器：按序返回 plan 或 refusal（测试候选链，不碰网络）。"""

    engine = "stub"

    def __init__(self, plans: list[Plan] | None = None, refusals: int = 0) -> None:
        self._plans = list(plans or [])
        self._refusals = refusals
        self.calls: list[str] = []

    def generate(self, question: str, k: int = 5) -> GenerationResult:
        self.calls.append(question)
        if self._refusals > 0:
            self._refusals -= 1
            return GenerationResult(
                question=question,
                refusal=GenerationRefusal(question, "无法确定指标口径"),
            )
        if self._plans:
            return GenerationResult(question=question, plan=self._plans.pop(0))
        return GenerationResult(
            question=question,
            refusal=GenerationRefusal(question, "无法确定指标口径"),
        )


def _answer_plan() -> Plan:
    """与 gold-102 人工标注一致的 Plan（候选链 fake 输出）。"""
    return Plan(
        metric="commission_revenue",
        dimensions=("Branch",),
        time=TimeSpec("year", 2013),
        order_by=(OrderSpec("commission_revenue", desc=True),),
        limit=5,
    )


class TestTopology(unittest.TestCase):
    def test_all_required_nodes_present(self) -> None:
        """任务书七节点全部落地 + Day 48 handoff 人工接管节点。"""
        g = build_graph(executor=FakeExecutor(), budget=BUDGET)
        nodes = set(g.get_graph().nodes)
        for name in (
            "plan",
            "clarify",
            "retrieve",
            "generate",
            "validate",
            "execute",
            "explain",
            "handoff",
        ):
            self.assertIn(name, nodes, f"缺少节点 {name}")

    def test_no_guard_no_sql(self) -> None:
        """构造时未给预算/执行器 → 直接报错（fail fast，SQL 不过 Guard 不执行）。"""
        with self.assertRaises(ValueError):
            build_graph(executor=FakeExecutor(), budget=None)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            build_graph(executor=None, budget=BUDGET)  # type: ignore[arg-type]


class TestDeterministicPath(unittest.TestCase):
    def setUp(self) -> None:
        self.executor = FakeExecutor()

    def _ask(self, question: str = GOLD102_Q):
        agent = DataAgent(executor=self.executor, budget=BUDGET)
        return agent.ask(question)

    def test_gold102_answer(self) -> None:
        """确定性链：Planner 命中 → execute → explain，SQL 过 Guard 达执行器。"""
        r = self._ask()
        self.assertEqual(r.kind, "answer")
        self.assertEqual(r.metric, "commission_revenue")
        self.assertEqual(r.path, "deterministic")
        self.assertEqual(r.engine, "deterministic")
        self.assertEqual(r.row_count, 1)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertIn("LIMIT 5", self.executor.calls[0])
        self.assertIn("GROUP BY", self.executor.calls[0])
        # Guard 出口（含 LIMIT 注入），与评测口径同构
        guarded, _ = enforce(self.executor.calls[0], budget=BUDGET)
        self.assertIn("LIMIT 5", guarded)

    def test_explanation_present(self) -> None:
        """explain 节点：归因骨架（指标/口径表达式/SQL/行数/链路来源）。"""
        r = self._ask()
        self.assertIsNotNone(r.explanation)
        assert r.explanation is not None
        self.assertEqual(r.explanation["metric"], "commission_revenue")
        expr = str(r.explanation["metric_expression"]).lower()
        self.assertIn("commission", expr)
        self.assertEqual(r.explanation["path"], "deterministic")
        self.assertEqual(r.explanation["row_count"], 1)
        # Day 46：过滤/MVP 恒空如实展示；未绑定快照时数据版本为空（不编造）
        self.assertEqual(r.explanation["filters"], [])
        self.assertEqual(r.explanation["dimensions"], ("Branch",))
        self.assertIsNone(r.explanation["data_version"])
        self.assertIsNone(r.explanation["data_refreshed_at"])

    def test_explanation_tables_and_data_version(self) -> None:
        """Day 46 归因全字段：物理表清单来自执行 SQL AST；快照版本来自注入 meta。"""
        agent = DataAgent(executor=self.executor, budget=BUDGET, snapshot_meta=_META)
        r = agent.ask(GOLD102_Q)
        assert r.explanation is not None
        tables = r.explanation["tables"]
        self.assertIsInstance(tables, (list, tuple))
        self.assertIn("atlas.dwd.fact_trades", tables)
        self.assertEqual(r.explanation["data_version"], _META["sha"])
        self.assertEqual(r.explanation["data_refreshed_at"], _META["created_at"])
        self.assertIsInstance(r.explanation["latency_ms"], float)


class TestClarifyAcceptance(unittest.TestCase):
    """Day 44 验收：4 条歧义 gold 全部触发反问，且不执行 SQL（不猜答）。"""

    def _graph_ask(self, question: str):
        executor = FakeExecutor()
        agent = DataAgent(executor=executor, budget=BUDGET)
        result = agent.ask(question)
        return result, executor

    def test_all_ambiguous_gold_clarify(self) -> None:
        for gold_id, _question in AMBIGUOUS_QUESTIONS:
            with self.subTest(gold=gold_id):
                question = load_gold_question(gold_id)
                r, executor = self._graph_ask(question)
                self.assertEqual(r.kind, "clarify", f"{gold_id} 应反问而非回答")
                self.assertIsNotNone(r.clarification)
                assert r.clarification is not None
                self.assertTrue(r.clarification.reasons)
                self.assertEqual(executor.calls, [], f"{gold_id} 反问时不应执行 SQL")

    def test_out_of_domain_clarify_no_fake_candidates(self) -> None:
        """域外无词重叠问句：真实检索 0 候选 → 反问且不编造候选（诚实边界）。"""
        r, executor = self._graph_ask(OUT_OF_DOMAIN_Q)
        self.assertEqual(r.kind, "clarify")
        assert r.clarification is not None
        self.assertEqual(executor.calls, [])
        # 检索无候选时 candidates 保持空——反问不附任何编造的指标
        self.assertEqual(r.clarification.candidates, ())

    def test_out_of_domain_clarify_with_candidates(self) -> None:
        """域外措辞默认（allow_candidate=False）：反问 + 附检索候选（确定性，0 token）。"""
        executor = FakeExecutor()
        linker = FakeLinker(candidates=("commission_revenue", "total_trade_value"))
        agent = DataAgent(executor=executor, budget=BUDGET, linker=linker)  # type: ignore[arg-type]
        r = agent.ask(OUT_OF_DOMAIN_Q)
        self.assertEqual(r.kind, "clarify")
        assert r.clarification is not None
        self.assertEqual(executor.calls, [])
        self.assertEqual(r.clarification.candidates, ("commission_revenue", "total_trade_value"))

    def test_unmatched_relative_time_kinds(self) -> None:
        """gold-104 unmatched 与 gold-121 relative_time 的 kind 语义正确。"""
        r104, _ = self._graph_ask(load_gold_question("gold-104"))
        assert r104.clarification is not None
        self.assertEqual(r104.clarification.kind, "unmatched")
        r121, _ = self._graph_ask(load_gold_question("gold-121"))
        assert r121.clarification is not None
        self.assertEqual(r121.clarification.kind, "relative_time")


class TestCandidatePath(unittest.TestCase):
    """allow_candidate=True：域外措辞 → retrieve → generate → validate → execute。"""

    def _agent(self, executor: FakeExecutor, gen: FakeGenerator):
        return DataAgent(
            executor=executor,
            budget=BUDGET,
            allow_candidate=True,
            generator=gen,  # type: ignore[arg-type]
            linker=FakeLinker(),  # type: ignore[arg-type]
        )

    def test_candidate_answer_with_fake_generator(self) -> None:
        executor = FakeExecutor()
        gen = FakeGenerator(plans=[_answer_plan()])
        r = self._agent(executor, gen).ask(OUT_OF_DOMAIN_Q)
        self.assertEqual(r.kind, "answer")
        self.assertEqual(r.metric, "commission_revenue")
        self.assertEqual(r.path, "candidate")
        self.assertEqual(len(gen.calls), 1)
        self.assertEqual(len(executor.calls), 1)

    def test_stub_generator_refusal_clarify(self) -> None:
        """候选链生成器 refuse → 安全落回反问（确定性兜底，不猜不执行）。"""
        executor = FakeExecutor()
        gen = FakeGenerator()
        r = self._agent(executor, gen).ask(OUT_OF_DOMAIN_Q)
        self.assertEqual(r.kind, "clarify")
        assert r.clarification is not None
        self.assertIn("候选生成放弃", r.clarification.reasons[0])
        self.assertEqual(len(gen.calls), 1)
        self.assertEqual(executor.calls, [])

    def test_validate_retry_once(self) -> None:
        """候选编译失败 → 自动重试一次 → 成功执行（attempts 上限内）。"""
        executor = FakeExecutor()
        compiler = FlakyCompiler()
        gen = FakeGenerator(plans=[_answer_plan(), _answer_plan()])
        agent = DataAgent(
            executor=executor,
            budget=BUDGET,
            allow_candidate=True,
            generator=gen,  # type: ignore[arg-type]
            linker=FakeLinker(),  # type: ignore[arg-type]
            compiler=compiler,  # type: ignore[arg-type]
        )
        r = agent.ask(OUT_OF_DOMAIN_Q)
        self.assertEqual(r.kind, "answer")
        self.assertEqual(len(gen.calls), 2, "编译失败应触发一次重生成")
        self.assertEqual(len(executor.calls), 1)


class TestHandoff(unittest.TestCase):
    """Day 48 人工接管：候选链 retrieve 0 候选 → 显式 handoff（不空转不编造）。"""

    def _agent(self, executor: FakeExecutor):
        return DataAgent(
            executor=executor,
            budget=BUDGET,
            allow_candidate=True,  # 候选模式才可能进入 retrieve（见路由口径）
            linker=FakeLinker(candidates=()),  # 0 候选：素材空
        )

    def test_zero_candidates_handoff(self) -> None:
        """检索 0 候选：LLM 生成无素材 → 转人工，不落空反问、不执行 SQL。"""
        executor = FakeExecutor()
        r = self._agent(executor).ask(OUT_OF_DOMAIN_Q)
        self.assertEqual(r.kind, "handoff")
        self.assertIsNotNone(r.handoff_reason)
        assert r.handoff_reason is not None
        self.assertIn("转人工", r.handoff_reason)
        self.assertIn("未检索到注册域", r.handoff_reason)
        self.assertEqual(executor.calls, [], "handoff 不执行 SQL")
        self.assertIsNone(r.clarification, "handoff 不是反问")
        self.assertEqual(r.row_count, 0)
        self.assertIsNone(r.explanation, "handoff 无归因（无结果可解释）")

    def test_handoff_reason_carries_cause_chain(self) -> None:
        """handoff_reason 带完整原因链（问题 → 未命中 → 素材空 → 转人工）。"""
        r = self._agent(FakeExecutor()).ask(OUT_OF_DOMAIN_Q)
        assert r.handoff_reason is not None
        for fragment in ("无法自动完成", "不编造", "人工接管"):
            self.assertIn(fragment, r.handoff_reason)

    def test_handoff_does_not_leak_into_next_turn(self) -> None:
        """多轮冲刷：handoff 后同 session 换注册域问句 → 正常 answer（无残留）。"""
        executor = FakeExecutor()
        agent = self._agent(executor)
        r1 = agent.ask(OUT_OF_DOMAIN_Q, session_id="sess-h")
        self.assertEqual(r1.kind, "handoff")
        r2 = agent.ask(GOLD102_Q, session_id="sess-h")
        self.assertEqual(r2.kind, "answer")
        self.assertEqual(r2.turns_in_session, 2)
        self.assertIsNotNone(r2.explanation, "answer 轮有归因")


class TestBlockedAndError(unittest.TestCase):
    def test_guard_blocked_no_execution(self) -> None:
        """查询表不在白名单 → Guard 拒绝 → kind=blocked，SQL 不达执行器。

        Guard 语义（sql_guard.check_tables）：allowed_tables 为空 = 不启用表
        检查（配置化默认），因此用「非空但不含查询表」的预算构造拒绝。
        """
        executor = FakeExecutor()
        deny_budget = Budget(
            dialect="doris", max_rows=10_000, allowed_tables=frozenset({"atlas.dwd.other"})
        )
        agent = DataAgent(executor=executor, budget=deny_budget)
        r = agent.ask(GOLD102_Q)
        self.assertEqual(r.kind, "blocked")
        self.assertIsNotNone(r.block_reason)
        self.assertIn("UnsafeQuery", r.block_reason or "")
        self.assertEqual(executor.calls, [], "被 Guard 拒绝的 SQL 不得执行")

    def test_executor_failure_kind_error(self) -> None:
        executor = RaisingExecutor(RuntimeError("db down"))
        agent = DataAgent(executor=executor, budget=BUDGET)
        r = agent.ask(GOLD102_Q)
        self.assertEqual(r.kind, "error")
        self.assertIn("db down", r.error or "")
        # SQL 已过 Guard 才达执行器（执行器故障 ≠ Guard 拒绝）
        self.assertEqual(len(executor.calls), 1)


class TestSessionTurns(unittest.TestCase):
    def test_multi_turn_same_session(self) -> None:
        """checkpointer 多轮：同 session 轮数递增；不同 session 互不干扰。"""
        executor = FakeExecutor()
        agent = DataAgent(executor=executor, budget=BUDGET)
        sid = "sess-test-1"
        r1 = agent.ask(GOLD102_Q, session_id=sid)
        self.assertEqual(r1.turns_in_session, 1)
        r2 = agent.ask("2013 年成交量和交易额分别是多少？", session_id=sid)
        self.assertEqual(r2.turns_in_session, 2)
        self.assertEqual(r2.kind, "clarify")
        r3 = agent.ask(GOLD102_Q, session_id="sess-test-2")
        self.assertEqual(r3.turns_in_session, 1)
        self.assertEqual(agent.sessions[sid], 2)
        self.assertEqual(agent.sessions["sess-test-2"], 1)

    def test_resume_answer_after_clarify_same_session(self) -> None:
        """同 session 交叉轮（反问↔命中）：plan 节点每轮冲刷，残留不误判。

        回归：LangGraph 1.2.11 checkpointer 跨轮恢复上一轮终点状态，若不清
        冲刷，clarify 轮残留的 clarification/explanation/block_reason 会让
        下一轮命中问句被误路由或携带上一轮归因（曾实测复现）。
        """
        executor = FakeExecutor()
        agent = DataAgent(executor=executor, budget=BUDGET)
        sid = "sess-resume-1"
        r1 = agent.ask(load_gold_question("gold-121"), session_id=sid)  # 相对时间反问
        self.assertEqual(r1.kind, "clarify")
        self.assertEqual(r1.clarification and r1.clarification.kind, "relative_time")
        r2 = agent.ask(GOLD102_Q, session_id=sid)  # 命中轮：必须走执行而非残留直出
        self.assertEqual(r2.kind, "answer")
        self.assertEqual(r2.metric, "commission_revenue")
        self.assertEqual(len(executor.calls), 1)
        r3 = agent.ask(load_gold_question("gold-122"), session_id=sid)  # 再反问
        self.assertEqual(r3.kind, "clarify")
        self.assertIsNone(r3.explanation, "反问轮不得携带上轮 answer 的归因")
        r4 = agent.ask(GOLD102_Q, session_id=sid)  # 命中轮 2：answer 残留也不误判
        self.assertEqual(r4.kind, "answer")
        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(r4.turns_in_session, 4)

    def test_turn_from_state_empty_is_error(self) -> None:
        """空状态组装 = 内部错误（状态机未产出结果时不伪装答案）。"""
        r = turn_from_state({"question": "x"}, "s")
        self.assertEqual(r.kind, "error")


class TestFollowupResolution(unittest.TestCase):
    """ADR-0014 ② 指代消解 MVP：多轮同构追问的图级链路契约。

    - 同 session 连续提问：残句（无指标词）命中链接词 → 复用上轮 Plan 补全并执行
    - 自由代词 / 相对时间 / 新会话首问残句 → 反问不猜、0 SQL 执行
    """

    def _agent(self, executor: FakeExecutor | None = None):
        self.executor = executor or FakeExecutor()
        return DataAgent(executor=self.executor, budget=BUDGET)

    def test_change_time_second_turn_executes(self) -> None:
        """上轮 2013 佣金 Top5 分支 → 「那 2014 年呢」→ 2014 同构 SQL。"""
        agent = self._agent()
        sid = "s-fu-time"
        r1 = agent.ask(GOLD102_Q, session_id=sid)
        self.assertEqual(r1.kind, "answer")
        r2 = agent.ask("那 2014 年呢", session_id=sid)
        self.assertEqual(r2.kind, "answer")
        self.assertEqual(r2.metric, "commission_revenue")
        self.assertEqual(r2.turns_in_session, 2)
        self.assertEqual(len(self.executor.calls), 2)
        sql2 = self.executor.calls[1]
        self.assertNotEqual(sql2, self.executor.calls[0], "追问必须产出新 SQL（换年）")
        self.assertIn("CalendarYearID = 2014", sql2)
        self.assertIn("dim_broker.Branch", sql2)  # 维度结构同构继承
        self.assertIn("Commission", sql2)
        self.assertIn("LIMIT 5", sql2)

    def test_change_dimension_second_turn_executes(self) -> None:
        """「那按客户等级统计呢」→ 分组维度替换为 Tier（时间保持 2013）。"""
        agent = self._agent()
        sid = "s-fu-dim"
        r1 = agent.ask(GOLD102_Q, session_id=sid)
        self.assertEqual(r1.kind, "answer")
        r2 = agent.ask("那按客户等级统计呢", session_id=sid)
        self.assertEqual(r2.kind, "answer")
        assert r2.explanation is not None
        self.assertEqual(r2.explanation["dimensions"], ("Tier",))
        sql2 = self.executor.calls[1]
        self.assertIn("dim_customer.Tier", sql2)
        self.assertIn("CalendarYearID = 2013", sql2)
        self.assertIn("LIMIT 5", sql2)

    def test_free_pronoun_clarifies_without_sql(self) -> None:
        """自由代词「那它呢」→ 反问完整重述；不执行 SQL（不猜）。"""
        agent = self._agent()
        sid = "s-fu-pron"
        r1 = agent.ask(GOLD102_Q, session_id=sid)
        self.assertEqual(r1.kind, "answer")
        r2 = agent.ask("那它呢", session_id=sid)
        self.assertEqual(r2.kind, "clarify")
        self.assertEqual(len(self.executor.calls), 1, "反问轮不得执行 SQL")
        self.assertEqual(r2.clarification and r2.clarification.kind, "ambiguous")

    def test_relative_time_followup_clarifies(self) -> None:
        """「那去年呢」→ relative_time 反问（快照评测口径不漂移）。"""
        agent = self._agent()
        sid = "s-fu-rel"
        agent.ask(GOLD102_Q, session_id=sid)
        r2 = agent.ask("那去年呢", session_id=sid)
        self.assertEqual(r2.kind, "clarify")
        self.assertEqual(r2.clarification and r2.clarification.kind, "relative_time")
        self.assertEqual(len(self.executor.calls), 1)

    def test_fresh_session_followup_does_not_leak(self) -> None:
        """跨会话隔离：新会话首问残句无 last_plan → 不误触发补全，正常反问。"""
        agent = self._agent()
        r = agent.ask("那 2014 年呢", session_id="s-fu-fresh")
        self.assertEqual(r.kind, "clarify")
        self.assertEqual(r.clarification and r.clarification.kind, "unmatched")
        self.assertEqual(self.executor.calls, [], "无补全基线时残句不得执行 SQL")

    def test_clarify_turn_does_not_update_last_plan(self) -> None:
        """反问轮不改写 last_plan：其后的同构追问仍补全为最近成功口径。"""
        agent = self._agent()
        sid = "s-fu-mid"
        r1 = agent.ask(GOLD102_Q, session_id=sid)  # 2013 Top5 分支
        self.assertEqual(r1.kind, "answer")
        r2 = agent.ask("那它呢", session_id=sid)  # 反问（不成功轮）
        self.assertEqual(r2.kind, "clarify")
        r3 = agent.ask("那 2014 年呢", session_id=sid)  # 仍应补全 2013 结构换 2014
        self.assertEqual(r3.kind, "answer")
        self.assertIn("CalendarYearID = 2014", self.executor.calls[1])
        self.assertIn("dim_broker.Branch", self.executor.calls[1])


if __name__ == "__main__":
    unittest.main()
