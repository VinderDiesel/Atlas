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
from collections.abc import Sequence
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
    """gold JSON 的 question 字段（与 runner 同源，防止测试问句漂移）。

    目录化后按 id 段路由：gold-1xx → finance/，gold-0xx → retail/。
    """
    domain = "finance" if gold_id.startswith("gold-1") else "retail"
    path = GOLD_DIR / domain / f"{gold_id}.json"
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

    def emitted_time_column(self, plan: Plan) -> str | None:
        """镜像 Compiler 的时间列访问器（node_execute 会调用，ADR-0025 决策 ①3）。"""
        return self.inner.emitted_time_column(plan)


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
        self.candidates_seen: list[tuple[str, ...] | None] = []

    def generate(
        self, question: str, k: int = 5, *, candidates: Sequence[str] | None = None
    ) -> GenerationResult:
        self.calls.append(question)
        self.candidates_seen.append(None if candidates is None else tuple(candidates))
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


class TestIdentityInjection(unittest.TestCase):
    """C1 服务面硬化：DataAgent.ask(identity=…) → Guard Policy 注入（主链）。

    - 无 identity 零变化（硬门）：SQL 与既有无身份断言逐字符一致，归因不加键
    - identity 非 None：claims → resolve_claims → Policy(name, condition) →
      enforce 注入（与 rls-verify/demo 同机制）；非法 claims → error 不执行
    - 生效可见性：explanation 挂「行级策略已生效（角色 X，策略 Y）」——
      只含角色 + 策略名，不含条件值（0011 不外泄细节）
    - identity **每轮独立**：经 invoke config 传递不落 checkpoint，同会话
      撤身份后新一轮零残留（无策略泄漏到无身份轮）
    """

    def setUp(self) -> None:
        self.executor = FakeExecutor()
        self.agent = DataAgent(executor=self.executor, budget=BUDGET)

    @staticmethod
    def _claims(role: str, **user_context: object) -> dict[str, object]:
        """与 verify_token 输出同形态（sub/role/user_context…）。"""
        return {"sub": "contract-user", "role": role, "user_context": user_context}

    def test_no_identity_zero_change(self) -> None:
        """无 identity 硬门：SQL 与既有断言逐字符一致、explanation 无策略键。"""
        r = self.agent.ask(GOLD102_Q)
        self.assertEqual(r.kind, "answer")
        sql = self.executor.calls[0]
        self.assertIn("LIMIT 5", sql)
        self.assertIn("GROUP BY", sql)
        self.assertIn("dim_broker.Branch", sql)
        self.assertNotIn("1 = 1", sql)  # 无身份 = 不注入任何谓词
        assert r.explanation is not None
        self.assertNotIn("policy_effect", r.explanation)  # 归因零变化

    def test_hq_admin_injects_full_visibility(self) -> None:
        """identity=hq_admin：谓词 1=1 注入（无行过滤语义），生效句含角色+策略名。"""
        r = self.agent.ask(GOLD102_Q, identity=self._claims("hq_admin"))
        self.assertEqual(r.kind, "answer")
        self.assertIn("1 = 1", self.executor.calls[0])  # rp_branch_visible.hq_admin
        assert r.explanation is not None
        effect = str(r.explanation["policy_effect"])
        self.assertIn("行级策略已生效", effect)
        self.assertIn("hq_admin", effect)
        self.assertIn("rp_branch_visible", effect)

    def test_branch_manager_injects_predicate_without_leaking_value(self) -> None:
        """identity=branch_manager{east}：SQL 含分支谓词；生效句不给条件值。"""
        r = self.agent.ask(GOLD102_Q, identity=self._claims("branch_manager", branch="east"))
        self.assertEqual(r.kind, "answer")
        sql = self.executor.calls[0]
        self.assertIn("= 'east'", sql)  # 行级谓词已随 Guard 注入执行 SQL
        assert r.explanation is not None
        effect = str(r.explanation["policy_effect"])
        self.assertIn("branch_manager", effect)
        self.assertIn("rp_branch_visible", effect)
        self.assertNotIn("east", effect)  # 条件值不外泄（0011 口径）

    def test_invalid_identity_rejected_no_execution(self) -> None:
        """identity 非 claims 形态（非 dict / role 未注册）→ error 且 SQL 不执行。"""
        r = self.agent.ask(GOLD102_Q, identity="hq_admin")  # type: ignore[arg-type]
        self.assertEqual(r.kind, "error")
        self.assertIn("identity 必须为已验证 claims 字典", r.error or "")
        self.assertEqual(self.executor.calls, [])
        r2 = self.agent.ask(GOLD102_Q, identity=self._claims("ceo_omniscient"))
        self.assertEqual(r2.kind, "error")
        self.assertIn("身份策略解析失败", r2.error or "")
        self.assertEqual(self.executor.calls, [])

    def test_identity_per_turn_no_checkpoint_leak(self) -> None:
        """身份每轮独立：同会话 带身份 → 撤身份，第二轮无策略残留。"""
        sid = "sess-identity-1"
        r1 = self.agent.ask(
            GOLD102_Q,
            session_id=sid,
            identity=self._claims("branch_manager", branch="east"),
        )
        self.assertEqual(r1.kind, "answer")
        self.assertIn("= 'east'", self.executor.calls[0])
        r2 = self.agent.ask(GOLD102_Q, session_id=sid)  # 同会话但不带身份
        self.assertEqual(r2.kind, "answer")
        self.assertEqual(r2.turns_in_session, 2)
        self.assertNotIn("east", self.executor.calls[1])  # 无谓词残留
        assert r2.explanation is not None
        self.assertNotIn("policy_effect", r2.explanation)

    def test_missing_policy_model_rejects_identity(self) -> None:
        """判据 1：模型缺 default_row_policy + 带 identity → error 不执行（决策 ②）。

        模型未声明策略时不降级为无策略执行（default_deny 精神）；同模型
        无身份路径零变化（缺失只在带身份时拒绝）。
        """
        import tempfile

        source = (REPO / "semantic" / "ossie" / "atlas_finance.ossie.yaml").read_text(
            encoding="utf-8"
        )
        stripped = source.replace('"default_row_policy": "rp_branch_visible",', "", 1)
        self.assertNotIn("default_row_policy", stripped, "剥离失败：请检查 YAML 原文")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "atlas_finance_no_policy.ossie.yaml"
            tmp_path.write_text(stripped, encoding="utf-8")
            model = SemanticModel(tmp_path)
        self.assertIsNone(model.default_row_policy)
        agent = DataAgent(model=model, executor=self.executor, budget=BUDGET)
        r = agent.ask(GOLD102_Q, identity=self._claims("hq_admin"))
        self.assertEqual(r.kind, "error")
        self.assertIn("default_row_policy", r.error or "")
        self.assertEqual(self.executor.calls, [])
        r2 = agent.ask(GOLD102_Q)  # 无身份路径不受影响（决策 ② 零变化）
        self.assertEqual(r2.kind, "answer")


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
        # T10d：重试沿用同一轮 retrieve 候选清单（不二次检索）
        self.assertEqual(gen.candidates_seen, [("commission_revenue",), ("commission_revenue",)])


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
        # 轮数单一事实源在 checkpoint 状态（ADR-0020 决策 ⑤）：进程内 sessions 表已删
        # （`sid` 第 2 轮的轮数已由 r2.turns_in_session 断言，此处只锁「私有记账面消失」）
        self.assertFalse(hasattr(DataAgent, "sessions"))

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


class TestChartWiring(unittest.TestCase):
    """ADR-0025 决策 ①②：time_column 随状态携带、chart 在 turn_from_state 渲染。

    - answer 轮：time_column = 编译声明列（yoy 实测别名 calendaryearid），
      chart 由 render_chart 一次渲染（CLI 与 HTTP 共用该单点）；
    - 非 answer 轮与空结果：chart=None（不伪装空图，也不反噬回答）；
    - 跨轮冲刷：反问轮的状态里不得残留上轮 time_column（checkpoint 累积面）。
    """

    YOY_ROWS = [(2014, 100.0, None), (2015, 150.0, 100.0)]
    YOY_COLUMNS = ["calendaryearid", "total_trade_value", "prev_period_value"]

    def test_yoy_answer_carries_time_column_and_line_chart(self) -> None:
        executor = FakeExecutor(rows=self.YOY_ROWS, columns=self.YOY_COLUMNS)
        r = DataAgent(executor=executor, budget=BUDGET).ask(load_gold_question("gold-172"))
        self.assertEqual(r.kind, "answer")
        self.assertEqual(r.time_column, "calendaryearid")
        self.assertIsNotNone(r.chart)
        assert r.chart is not None
        self.assertEqual(r.chart["type"], "line")
        self.assertEqual(r.chart["x"], "calendaryearid")
        self.assertEqual(r.chart["y"], ["total_trade_value"])

    def test_plain_answer_time_column_none_chart_bar(self) -> None:
        executor = FakeExecutor(rows=[("华中", 123.0)], columns=["Branch", "commission_revenue"])
        r = DataAgent(executor=executor, budget=BUDGET).ask(GOLD102_Q)
        self.assertEqual(r.kind, "answer")
        self.assertIsNone(r.time_column)
        self.assertIsNotNone(r.chart)
        assert r.chart is not None
        self.assertEqual(r.chart["type"], "bar")

    def test_empty_result_answer_chart_null_not_error(self) -> None:
        """空结果：ChartError 降为 chart=None，回答本身仍按表格契约返回。"""
        executor = FakeExecutor(rows=[], columns=["Branch", "commission_revenue"])
        r = DataAgent(executor=executor, budget=BUDGET).ask(GOLD102_Q)
        self.assertEqual(r.kind, "answer")
        self.assertEqual(r.row_count, 0)
        self.assertIsNone(r.chart)

    def test_non_answer_turn_chart_and_time_column_null(self) -> None:
        r = DataAgent(executor=FakeExecutor(), budget=BUDGET).ask(load_gold_question("gold-104"))
        self.assertEqual(r.kind, "clarify")
        self.assertIsNone(r.chart)
        self.assertIsNone(r.time_column)

    def test_time_column_flushed_between_turns(self) -> None:
        """跨轮冲刷：plan 节点把上轮 time_column 置 None，反问轮状态不残留。"""
        executor = FakeExecutor(rows=self.YOY_ROWS, columns=self.YOY_COLUMNS)
        agent = DataAgent(executor=executor, budget=BUDGET)
        sid = "s-chart-flush"
        r1 = agent.ask(load_gold_question("gold-172"), session_id=sid)
        self.assertEqual(r1.time_column, "calendaryearid")
        r2 = agent.ask(load_gold_question("gold-104"), session_id=sid)
        self.assertEqual(r2.kind, "clarify")
        state = agent._graph.get_state(
            {"configurable": {"thread_id": f"{MODEL.name}:{sid}"}}
        ).values
        self.assertIsNone(state.get("time_column"))


if __name__ == "__main__":
    unittest.main()
