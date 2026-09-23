"""LangGraph 状态机（Day 43）：把 P2 评测链编排为可多轮对话的 Data Agent。

节点与路由（确定性优先，与 P2 结论对齐）
----------------------------------------
任务书七节点序列 clarify → retrieve → plan → generate → validate → execute →
explain，落地为 **plan 先行** 的条件路由图（AGENTS.md 决策优先级 4：能用
确定性解析就不用 LLM——解析必须先于一切）：

    START → plan ──(命中)──────────────→ execute → explain → END
               │
               ├─(ambiguous / relative_time) → END（clarification 直出，反问不猜）
               │
               └─(unmatched，仅 allow_candidate=True) → retrieve → generate
                    → validate ──(通过)──→ execute（同上）
                                │
                                └─(失败，validate 内重试 ≤1 次仍败) → clarify → END

    （unmatched 且 allow_candidate=False → clarify 终端，反问附确定性检索候选；
     candidate 模式 retrieve 0 候选 → handoff 终端——生成无素材、反问无候选，
     空转无意义，显式转人工接管（Day 48），不编造答案。）

- plan       ：确定性 Planner（问句 → Plan | ClarificationRequest）
- clarify    ：反问终端节点（歧义/相对时间直出；unmatched 及候选链放弃在此
  组装反问：原因 + schema linking 候选，不猜答、0 LLM token）
- retrieve   ：域外措辞的 schema linking 候选收窄（agent/tools/schema_linker.py）
- generate   ：LLM 候选 Plan（engine=stub|openai；SQL 一律由 Compiler 生成，
  见 agent/generator.py 最小攻击面原则）
- validate   ：候选的确定性复核（validate_plan_json 与编译预检双关卡，
  与 lora/build_pairs 训练语料同口径）；失败在节点内重生成一次（共 ≤2 次
  generate），仍败转 clarify——图无回环，失败轮不残留误导状态
- execute    ：Compiler → Guard(enforce) → 执行器（唯一执行通道，N3 红线；
  执行链委托共享内核 agent/runtime/execution.execute_plan〔ADR-0031 T04d〕；
  Guard 拒绝 → blocked 终端，不泄露被拒 SQL）
- explain    ：归因组装（Day 43 最小骨架：指标/口径表达式/SQL/行数/链路来源；
  Day 46 扩展表/过滤/刷新时间/版本）
- handoff    ：人工接管终端（Day 48）——候选链 retrieve 0 候选时 LLM 生成无
  素材、反问无候选可澄清，系统不空转不编造，显式转人工（handoff_reason
  携带完整原因链）。安全边界：Guard 拒绝（blocked）与执行期故障（error）
  不转人工——前者人工也不得绕过（AGENTS.md N3），后者属运维排查非对话接管。

**候选链可达性（诚实口径）**：注册域内（48 条金融 gold 覆盖口径）Planner
44/44 命中 + 歧义 4/4 反问，**generate/validate 在默认策略下不可达**——LLM
候选链仅当 allow_candidate=True（RAG 评测 / 端到端场景演示）才从 unmatched
问句进入，且与 eval/rag_eval.py 同口径（歧义问句给答案 = 评测失败）。

**多轮（ADR-0014 ②）**：checkpointer（默认 MemorySaver，可换 SqliteSaver——ADR-0020）
按 session_id 持久化每轮事实轨迹；轮数（turns）与身份指纹（session_fingerprint）同为
状态字段（决策 ⑤⑥），没有进程内记账表。plan 节点以最近成功轮采纳的 Plan（last_plan，
explain 回写）做指代预检——同构追问（"那 2014 年呢 / 换成 X 统计"）残句无
指标词时复用上轮 metric/维度/过滤结构，仅替换本轮时间/维度片段，合并 Plan
仍过编译预检；自由代词与无法归属的碎片不猜 → 反问完整重述。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

import sqlglot
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RunnableConfig
from pydantic import JsonValue
from sqlglot import exp

from agent.analysis import (
    ANALYSIS_ROLES,
    AnalysisPlan,
    AnalysisPlanner,
    AnalysisReasonCode,
    AnalysisResult,
    AnalysisStepEvent,
    Attribution,
    plan_projection,
    synthesize,
)
from agent.compiler import Compiler, Plan, SemanticModel
from agent.feedback import (
    DEFAULT_FEEDBACK_DIR,
    FeedbackKind,
    FeedbackSubmission,
    submit_feedback,
)
from agent.generator import GenerationResult, Generator, validate_plan_json
from agent.planner import ClarificationRequest, Planner
from agent.runtime.context import RunContext
from agent.runtime.execution import execute_plan
from agent.security.sql_guard import Budget
from agent.state import ANALYSIS_RECORD_VERSION, TurnResult, TurnState, encode_rows
from agent.tools.chart import ChartError, render_chart
from agent.tools.schema_linker import SchemaLinker
from data.identity import RuntimeSnapshot
from observability.otel import record_analysis_step, record_turn
from serving.auth import claims_fingerprint
from serving.control.contracts import EventType
from serving.control.events import EventSink, RunEvent

# 执行器同构约定（与 eval/runner.execute_sql 一致）：只读执行 guarded SQL
Executor = Callable[[str], tuple[list[tuple[Any, ...]], list[str]]]

DEFAULT_CANDIDATE_K = 5
MAX_GENERATE_ATTEMPTS = 2  # 候选链 validate 失败重试上限（共 2 次 generate 尝试）

# 定义图节点清单（ADR-0031 T05 同源归约的「未执行」判定基线）：与下方装配
# （builder.add_node）保持同步——归约用它把未到达节点标 not_reached 而不是 skipped。
GRAPH_NODE_IDS: tuple[str, ...] = (
    "plan",
    "retrieve",
    "generate",
    "validate",
    "execute",
    "explain",
    "clarify",
    "handoff",
)

# 事件接缝的当前运行/节点上下文（ADR-0031 T05b）：节点包装器从 invoke config
# 读出 atlas_run 后写入本线程上下文，供节点内部子调用（TOOL_*）关联；无接缝
# 调用链上恒为 None，不产生额外分配。
_ACTIVE_RUN: ContextVar[dict[str, Any] | None] = ContextVar("atlas_active_run", default=None)
_ACTIVE_NODE: ContextVar[tuple[str, str] | None] = ContextVar("atlas_active_node", default=None)

# checkpointer 序列化白名单：TurnState 里**所有**自研 dataclass，含嵌在 Plan 内的成员。
# 两条实测口径（langgraph 1.2.11 / checkpoint 4.2.0，别按直觉改回去）：
# ① 白名单非空时未注册类型是被 **blocked 并退化成 dict**，不是「permissive 放行 + 警告」
#    ——警告只在完全不传白名单（属性为字面量 True）时才有。所以「嵌在 Plan 里就不用列」
#    是错的：`Filter` / `ComparisonSpec` 少列一项，跨轮读回的 `last_plan` 就编译失败
#    （"'dict' object has no attribute 'column'"，症状伪装成反问，tests/test_session_persistence
#    的 TestCheckpointStateFidelity 锁死这条）。
# ② `with_msgpack_allowlist()` 在 permissive 模式下直接返回 self 不合并，因此只能走构造参数。
_CHECKPOINT_SERDE = JsonPlusSerializer(
    allowed_msgpack_modules=(
        ("agent.compiler", "TimeSpec"),
        ("agent.compiler", "OrderSpec"),
        ("agent.compiler", "Filter"),
        ("agent.compiler", "ComparisonSpec"),
        ("agent.compiler", "Plan"),
        ("agent.planner", "ClarificationRequest"),
    )
)


class SessionIdentityConflict(Exception):
    """同会话换身份（ADR-0020 决策 ⑥）：会话已绑定另一 claims 指纹。

    类型化异常而不是布尔返回：`serving/api.py` 要把它翻成 422 + 审计行
    （`kind=conflict`），CLI 侧则是一个说得清的失败原因——「静默拒绝」与
    「静默放行接受新身份」都是要消灭的失效形态。
    """


def _extract_tables(sql: str | None, dialect: str) -> tuple[str, ...]:
    """从执行 SQL 提取物理表清单（Guard 出口 SQL，仅供 explain 归因展示）。"""
    if not sql:
        return ()
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 - 归因展示失败不影响答案本身
        return ()
    seen: list[str] = []
    for table in tree.find_all(exp.Table):
        full = ".".join(p for p in (table.catalog, table.db, table.name) if p)
        if full and full not in seen:
            seen.append(full)
    return tuple(seen)


class PlanGenerator(Protocol):
    """generate 节点依赖的生成器协议（Generator 满足；测试可注入 fake）。"""

    engine: str

    def generate(
        self,
        question: str,
        k: int = 5,
        *,
        candidates: Sequence[str] | None = None,
    ) -> GenerationResult: ...


def _plan_to_json(plan: Plan) -> dict[str, Any]:
    """Plan → validate_plan_json 输入形态（time.value 统一 str，校验器可收）。"""
    obj: dict[str, Any] = {"metric": plan.metric, "dimensions": list(plan.dimensions)}
    if plan.time is not None:
        obj["time"] = {"granularity": plan.time.granularity, "value": str(plan.time.value)}
    return obj


def build_graph(
    model: SemanticModel | None = None,
    *,
    engine: str = "stub",
    executor: Executor | None = None,
    budget: Budget | None = None,
    allow_candidate: bool = False,
    generator: PlanGenerator | None = None,
    linker: SchemaLinker | None = None,
    compiler: Compiler | None = None,
    snapshot_meta: dict[str, Any] | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    persist: bool = True,
    event_sink: EventSink | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """组装并编译 LangGraph 状态机（checkpointer 可注入，默认 MemorySaver）。

    参数
    ----
    model           : 语义模型（默认加载 atlas_finance.ossie.yaml）。
    engine          : 候选链生成引擎 "stub" | "openai"（注册域内不消费）。
    executor        : 只读执行器（必填；SQL 已过 Guard 才到达执行器）。
    budget          : Guard 预算（必填；表白名单 = 锁定快照，见 AGENTS.md N3）。
    allow_candidate : 允许 unmatched 问句进入 LLM 候选链（默认 False = 反问，
                      确定性优先；True = RAG 评测/演示模式）。
    generator/linker/compiler : 依赖注入（测试用 fake；默认按 model 构建）。
    snapshot_meta   : 锁定快照元数据（runner.build_budget 同源；仅用于 explain
                      的数据版本归因：sha/created_at。None = 未绑定快照，
                      归因里这两个字段为空——不编造刷新时间）。
    checkpointer    : 会话轨迹存储（ADR-0020 决策 ①）。None = 每图新建一个
                      `MemorySaver(serde=_CHECKPOINT_SERDE)`，与注入前的历史行为
                      完全一致（`tests/` 30 处 + `eval/` 9 处构造点零影响、零落盘，
                      计数口径见 ADR-0020 决策 ① 的实测注）；传入实例则**原样
                      使用**——本函数不复制、不重包、不替调用方决定 serde（决策 ②：
                      白名单漏传会让 `Plan` 在严格模式下退化为 dict，而退化发生在
                      谁手里谁才修得动）。
    persist         : 是否带会话轨迹存储编译（ADR-0026 决策 ③）。True（默认）=
                      既有语义（见 checkpointer）；False = 编译真正无 checkpointer
                      的同源图——多期分析的子执行通道用：节点/边定义与本函数
                      persist=True 完全同一套，仅无独立 checkpoint、无身份指纹
                      绑定（父级身份绑定是上层职责）。False 且同时传入
                      checkpointer 是配置冲突，直接 ValueError，不允许静默二选一。
    event_sink      : 真实事件接缝（ADR-0031 D07②）。None（默认）= 旧行为逐字节
                      不变（CLI/评测/既有测试零开销）；非 None 时**仅当** invoke
                      config 同时带 `configurable.atlas_run`（run_id 等运行上下文，
                      同 identity 的每请求通道）才发出真实事件。写入失败向上传播
                      （fail closed：事件盘失败，下一次 SQL 不得启动）。

    返回
    ----
    LangGraph CompiledStateGraph：invoke({"question": q}, config={"configurable":
    {"thread_id": "<model.name>:<session_id>"}})——thread_id 的模型前缀是约定
    （ADR-0020 决策 ④），由 DataAgent.ask 负责拼装；直接用本图者须自己带上，否则
    多域共享同一 checkpoint 文件时会互相覆写。最终状态字段见 agent/state.TurnState；
    组装 TurnResult 请用 ask() / DataAgent。persist=False 的图无 checkpoint，
    invoke 不需要 thread_id。

    异常
    ----
    ValueError：executor 或 budget 未提供（图不执行未过 Guard 的 SQL）；
                persist=False 且 checkpointer 非 None（配置冲突，ADR-0026）。
    """
    if executor is None:
        raise ValueError("executor 必填：只读 SQL 执行器（测试可注入 fake）")
    if budget is None:
        raise ValueError("budget 必填：Guard 预算（表白名单 = 锁定快照）")
    if not persist and checkpointer is not None:
        raise ValueError(
            "persist=False 与注入 checkpointer 冲突：无状态子执行通道（ADR-0026 决策 ③）"
            "不得携带会话轨迹存储——要持久化用默认 persist=True，要无状态就别传 checkpointer"
        )
    model = model or SemanticModel()
    planner = Planner(model)
    linker = linker or SchemaLinker(model)
    compiler = compiler or Compiler(model)
    gen = generator or Generator(model, engine=engine)

    # -- 节点：plan（确定性解析入口 + 每轮状态冲刷） ------------------------
    def node_plan(state: TurnState) -> dict[str, Any]:
        # Plan 直执短路（ADR-0022 决策 ③）：`/plan/execute` 经 invoke 注入的
        # plan_override 优先于 Planner——不调 Planner、不进 unmatched/候选链。
        # **用后即焚**：冲刷字典把 plan_override 写回 None（本轮的 override 只对
        # 本轮有效），否则带 session 的后续 /ask 轮会被陈旧 override 劫持。
        override = state.get("plan_override")
        # 冲刷：plan 节点是新一轮逻辑起点，先清上一轮终点残留的结果键
        # （checkpointer 跨轮恢复；LangGraph 1.2.11 无删除键 API，None 覆盖 =
        # 置空——路由与组装全部 isinstance/truthy 判定，空值不参与路由）
        out: dict[str, Any] = {
            "plan": None,
            "plan_override": None,
            "clarification": None,
            "candidates": None,
            "unmatched": False,
            "reason": None,
            "attempts": 0,
            "validated": False,
            "sql": None,
            "rows": None,
            "columns": None,
            "time_column": None,
            "row_count": 0,
            "latency_ms": 0.0,
            "validation_issues": None,
            "path": None,
            "engine": "deterministic",
            "explanation": None,
            "policy_effect": None,
            "block_reason": None,
            "error": None,
            "handoff_reason": None,
            # 多期间分析记账（ADR-0026 决策 ④）：新用户轮清旧分析记录——覆盖一个
            # running 记录即该轮无终态（历史快照仍留证）；终态记录被正常清走。
            # 分析子步（_run_analysis_step / _write_analysis_state）不经过本节点。
            "analysis_record": None,
            # 轮数单一事实源（决策 ⑤）：与 last_plan 同构的跨轮状态——本节点是每轮
            # 逻辑起点，读 checkpoint 里的旧值 +1 写回。它属于「承接」而非「残留」，
            # 刻意不放进上面的冲刷集合；`or 0` 兜住首轮（键不存在）与 None
            "turns": int(state.get("turns") or 0) + 1,
        }
        if isinstance(override, Plan):
            out["plan"] = override
            return out
        question = str(state["question"])
        result = planner.plan(question)
        if isinstance(result, ClarificationRequest) and result.kind == "unmatched":
            # 残句追问挂起守卫（ADR-0026 决策 ⑥）：最近一轮是多期间分析时，
            # 残句不许偷接分析前的旧计划口径——last_plan 保留（分析不回退它），
            # 但分析期间 blocked=True，明确提示完整重述；一次明确的普通 Plan
            # 执行成功（explain 回写 blocked=False）才解除。
            if state.get("analysis_followup_blocked"):
                out["clarification"] = ClarificationRequest(
                    question,
                    (  # 1-tuple：缺尾逗号会退化成裸 str（reasons 被逐字迭代）
                        "最近一轮是多期间分析：残句追问不再承接分析前的旧计划口径，"
                        "请完整重述要问的问题",
                    ),
                )
                return out
            # 指代预检（ADR-0014 ②）：残句（无指标词）且同会话存在上轮成功 Plan →
            # 尝试结构补全；补全产物仍走既有校验链（编译预检），失败/歧义不猜
            prev_plan = state.get("last_plan")
            if isinstance(prev_plan, Plan):
                merged = planner.followup(question, prev_plan)
                if isinstance(merged, Plan):
                    try:
                        compiler.compile(merged)
                    except Exception as exc:  # noqa: BLE001 - 原因完整转述
                        merged = ClarificationRequest(
                            question,
                            (f"追问补全后编译失败：{exc}——请完整重述问句",),
                        )
                if isinstance(merged, Plan):
                    result = merged
                elif isinstance(merged, ClarificationRequest):
                    # 补全歧义/相对时间：残句检索无意义，直出反问（不附候选）
                    out["clarification"] = merged
                    return out
                # merged is None：非链接形态 → 维持原 unmatched 流程（fall through）
        if isinstance(result, ClarificationRequest):
            if result.kind == "unmatched":
                # 未命中：不落 clarification（可能继续走候选链）；原因走 reason
                # 链，终点由 clarify 终端组装（歧义/相对时间才直出 END）
                out.update(unmatched=True, reason=result.reasons[0])
            else:
                out["clarification"] = result  # ambiguous / relative_time 直出 END
        else:
            out["plan"] = result
        return out

    # -- 节点：retrieve（域外措辞候选收窄） --------------------------------
    def node_retrieve(state: TurnState) -> dict[str, Any]:
        _tool("TOOL_STARTED", "schema_linking")
        linked = linker.link(str(state["question"]), k=DEFAULT_CANDIDATE_K)
        _tool("TOOL_FINISHED", "schema_linking", candidates=len(linked.candidates))
        if not linked.candidates:
            # 0 候选：不猜不空转，落 handoff 终端（生成无素材、反问无候选可澄清）
            return {"reason": "schema linking 未检索到注册域候选指标"}
        return {"candidates": linked.candidates}

    # -- 节点：generate（LLM 候选 Plan，候选必须再过确定性关卡） -----------
    def node_generate(state: TurnState) -> dict[str, Any]:
        _tool("TOOL_STARTED", "generate")
        # 候选输入路径（D09 L265）：显式携带 retrieve 产物，Generator 内不再二次检索
        result = gen.generate(
            str(state["question"]),
            k=DEFAULT_CANDIDATE_K,
            candidates=state.get("candidates"),
        )
        _tool(
            "TOOL_FINISHED",
            "generate",
            prompt_tokens=int(result.usage.get("prompt_tokens", 0)),
            completion_tokens=int(result.usage.get("completion_tokens", 0)),
        )
        prev = state.get("usage") or {}
        usage = {
            key: int(prev.get(key, 0)) + int(result.usage.get(key, 0))
            for key in ("prompt_tokens", "completion_tokens")
        }
        out: dict[str, Any] = {"usage": usage, "attempts": int(state.get("attempts", 0)) + 1}
        if result.plan is not None:
            out["plan"] = result.plan
        else:
            reason = result.refusal.reason if result.refusal else "生成器未给出候选"
            out["reason"] = f"候选生成放弃：{reason}"
        return out

    # -- 节点：validate（确定性复核 + 编译预检，失败节点内重生成一次） ------
    def _check_candidate(plan: Plan) -> tuple[Plan | None, str]:
        """确定性双关卡：validate_plan_json 复核 + 编译预检。
        返回 (规范 plan, "") 或 (None, 失败原因)。"""
        plan2, reason = validate_plan_json(model, _plan_to_json(plan))
        if plan2 is None:
            return None, f"候选未通过确定性校验：{reason}"
        try:
            compiler.compile(plan2)  # 预检（SQL 结果不入状态，execute 现场执行）
        except Exception as exc:  # noqa: BLE001 - 编译失败原因要完整转述
            return None, f"候选编译失败：{exc}"
        return plan2, ""

    def node_validate(state: TurnState) -> dict[str, Any]:
        plan = state.get("plan")
        if not isinstance(plan, Plan):
            return {"reason": "候选 Plan 缺失", "attempts": int(state.get("attempts", 0))}
        attempts = int(state.get("attempts", 0))
        plan2, fail = _check_candidate(plan)
        if plan2 is None and attempts < MAX_GENERATE_ATTEMPTS:
            # 防御性重试：LLM 候选一次不合格 → 重生成一次（共 ≤2 次 generate，
            # 注册域内 0 触发——候选链仅域外措辞可达，见模块 docstring）
            _tool("TOOL_STARTED", "generate")
            retried = gen.generate(
                str(state["question"]),
                k=DEFAULT_CANDIDATE_K,
                candidates=state.get("candidates"),
            )
            _tool(
                "TOOL_FINISHED",
                "generate",
                prompt_tokens=int(retried.usage.get("prompt_tokens", 0)),
                completion_tokens=int(retried.usage.get("completion_tokens", 0)),
            )
            attempts += 1
            if retried.plan is not None:
                plan2, fail = _check_candidate(retried.plan)
            elif retried.refusal is not None:
                fail = f"候选重试放弃：{retried.refusal.reason}"
        if plan2 is None:
            return {"reason": fail, "attempts": attempts}
        return {
            "plan": plan2,
            "path": "candidate",
            "engine": gen.engine,
            "validated": True,
            "attempts": attempts,
        }

    # -- 节点：handoff（人工接管终端；候选链素材空，不空转不编造） ----------
    def node_handoff(state: TurnState) -> dict[str, Any]:
        reason = state.get("reason") or "问句未命中注册指标，且检索无候选口径"
        return {
            "handoff_reason": (
                f"本回合无法自动完成：{reason}；LLM 候选生成无素材、反问无候选"
                "可澄清，系统不空转不编造——已转人工接管"
            )
        }

    # -- 节点：execute（编译→Guard→执行 的唯一通道） ------------------------
    # 执行链委托共享内核 agent/runtime/execution.execute_plan（ADR-0031 T04d）：
    # 现场编译 → 身份策略 → Guard → 执行计时 → 结果校验逐段等价；identity 经
    # invoke config 注入（每轮独立、不落 checkpoint，见 DataAgent.ask docstring），
    # 非 None 时由内核 resolve_claims 渲染行级策略（与 rls-verify/demo 同机制：
    # Policy(name, condition) → enforce 注入），策略名由当前语义模型
    # 的 default_row_policy 决定（ADR-0021：域 → 策略的唯一事实源；缺失即拒绝，
    # 不降级为无策略执行）；谓词非法/无 join 路径由 Guard 拒绝（blocked，不外泄
    # 细节）——graph 层不做二次校验实现。
    def node_execute(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
        plan = state.get("plan")
        if not isinstance(plan, Plan):
            return {"error": "内部状态缺失 Plan（不应到达 execute）"}
        identity = (config or {}).get("configurable", {}).get("identity")
        if identity is not None and not isinstance(identity, dict):
            # 非 claims 形态 → 拒绝（fail-closed；绝不静默丢弃身份降级为无策略
            # 执行。内核有同款防御，此处前置保留既有的错误出口）
            return {"error": "identity 必须为已验证 claims 字典（role + user_context）"}
        _tool("TOOL_STARTED", "execute_plan")
        result = execute_plan(
            plan,
            RunContext(
                budget=budget,
                executor=executor,
                identity=identity,
                compiler=compiler,
            ),
        )
        _tool("TOOL_FINISHED", "execute_plan", kind=result.kind, executed=bool(result.executed))
        if result.kind == "blocked":
            # 只报拒绝类型与原因，不携带被拒 SQL（纵深防御，不外泄细节）
            return {"block_reason": result.block_reason}
        if result.kind == "error":
            if not result.executed:
                # 编译失败（`/plan/execute` 可注入任意 Plan，ADR-0022 决策 ③/代价⑤：
                # 回合级 error 而非 500——HTTP 面不得复制编译预检，N3 单通道）与
                # 身份解析失败等未执行出口：不携带 SQL 与耗时（冲刷值保持 None/0.0）
                return {"error": result.error}
            # 执行后失败（ADR-0026 决策 ③）：与「未执行被拒」必须可区分——SQL 已过
            # Guard 且已送达执行器，如实记下已执行证据与尝试耗时，上层据此做到
            # 「耗时汇总只计实际执行的子 SQL」
            return {
                "error": result.error,
                "sql": result.sql,
                "latency_ms": result.latency_ms,
            }
        return {
            "sql": result.sql,
            "rows": result.rows,
            "columns": result.columns,
            # 编译声明的实际时间轴别名（ADR-0025 决策 ①3）：与本轮 plan 同源；
            # plain/rank 与候选链 plan 为 None（渲染端走列名兜底）
            "time_column": result.time_column,
            "row_count": result.row_count,
            "latency_ms": result.latency_ms,
            "validation_issues": result.validation_issues,
            "policy_effect": result.policy_effect,
            # 冲刷会把 path 置 None（键存在），get 默认值不生效 → or 回退
            "path": state.get("path") or "deterministic",
            "engine": state.get("engine") or "deterministic",
        }

    # -- 节点：explain（归因组装；Day 46 全字段） --------------------------
    def node_explain(state: TurnState) -> dict[str, Any]:
        plan = state.get("plan")
        if not isinstance(plan, Plan):
            return {}
        explanation: dict[str, Any] = {
            "metric": plan.metric,
            "metric_expression": model.metrics.get(plan.metric, ""),
            # 口径版本 = 语义层 YAML（git 版本管理）；表达式即唯一版本标识
            "dimensions": tuple(plan.dimensions),
            "time": str(plan.time.value) if plan.time is not None else None,
            # filters：本轮 Plan 的过滤条件（= != < >；维度值/度量阈值，如实展示）
            "filters": [f"{f.column} {f.op} {f.value}" for f in plan.filters],
            "sql": state.get("sql"),
            "tables": _extract_tables(state.get("sql"), budget.dialect),
            "row_count": state.get("row_count", 0),
            "latency_ms": state.get("latency_ms", 0.0),
            "path": state.get("path") or "deterministic",
            "engine": state.get("engine") or "deterministic",
            # 数据版本 = 锁定快照（无快照注入时不编造刷新时间）
            "data_version": (snapshot_meta or {}).get("sha"),
            "data_refreshed_at": (snapshot_meta or {}).get("created_at"),
        }
        # identity 注入可见性（ADR-0011 硬化项）：只在策略生效轮追加生效句
        # （角色 + 策略名，条件值不外泄）；无 identity 轮零变化（不加键）
        effect = state.get("policy_effect")
        if effect:
            explanation["policy_effect"] = effect
        # 回写 last_plan：本轮成功采纳的 Plan 成为下轮追问的指代基线（ADR-0014
        # ②）；失败轮（blocked/error）不进 explain，last_plan 保持上轮成功值。
        # 同点解除分析追问挂起（ADR-0026 决策 ⑥）：一次明确的普通 Plan 执行
        # 成功到达 explain = 残句挂起解除的唯一出口（分析子步不经过本节点）。
        return {
            "explanation": explanation,
            "last_plan": plan,
            "analysis_followup_blocked": False,
        }

    # -- 节点：clarify（反问终端：unmatched/候选链放弃统一组装反问） --------
    def node_clarify(state: TurnState) -> dict[str, Any]:
        clarification = state.get("clarification")
        if isinstance(clarification, ClarificationRequest):
            # 防御：歧义/相对时间直出路径不应到达本节点；到达则原样放行
            return {"clarification": clarification}
        # 组装（kind=unmatched）：原因走 reason 覆盖链；候选 = retrieve 产物，
        # 未经过 retrieve 则现场确定性检索（0 LLM token，不编造候选）
        candidates = state.get("candidates")
        if candidates is None:
            _tool("TOOL_STARTED", "schema_linking")
            linked = linker.link(str(state["question"]), k=DEFAULT_CANDIDATE_K)
            _tool("TOOL_FINISHED", "schema_linking", candidates=len(linked.candidates))
            candidates = tuple(linked.candidates)
        reason = state.get("reason") or "问句未命中任何注册指标，需要澄清口径"
        return {
            "clarification": ClarificationRequest(
                question=str(state["question"]),
                reasons=(str(reason),),
                candidates=candidates,
                kind="unmatched",
            )
        }

    # -- 条件路由 -----------------------------------------------------------
    def route_after_plan(state: TurnState) -> str:
        # ambiguous / relative_time：planner 已直出完整反问，跳过 clarify 终端
        if isinstance(state.get("clarification"), ClarificationRequest):
            return "end"
        if state.get("unmatched"):
            return "retrieve" if allow_candidate else "clarify"
        return "execute"  # Planner 命中，确定性链

    def route_after_retrieve(state: TurnState) -> str:
        return "generate" if state.get("candidates") else "handoff"

    def route_after_generate(state: TurnState) -> str:
        # 本轮 generate 成功必写 plan（plan 节点已冲刷上轮残留）；无 plan = refusal
        return "validate" if isinstance(state.get("plan"), Plan) else "clarify"

    def route_after_validate(state: TurnState) -> str:
        # 图无回环：validate 内已消费重试（≤2 次 generate），失败直接落 clarify
        return "execute" if state.get("validated") else "clarify"

    def route_after_execute(state: TurnState) -> str:
        if state.get("block_reason") or state.get("error"):
            return "end"
        return "explain"

    # -- 事件接缝（ADR-0031 D07②③，T05b）-----------------------------------
    # sink=None 或 invoke 未带运行上下文（旧调用/CLI/评测）→ 一切按旧路径直通：
    # 零事件、零额外分配、节点/边行为逐字节不变。graph 侧不持有 run_id——运行
    # 上下文每请求经 config 传入（同 identity 通道），事件归属由服务端分配。
    def _event_run(config: RunnableConfig | None) -> dict[str, Any] | None:
        """读 invoke config 的运行上下文；缺 run_id 视为无接缝（不发事件）。"""
        if not isinstance(config, dict):
            return None
        configurable = config.get("configurable") or {}
        ctx = configurable.get("atlas_run")
        if isinstance(ctx, dict) and ctx.get("run_id"):
            return ctx
        return None

    def _emit(
        run: dict[str, Any],
        event_type: EventType,
        *,
        node_id: str | None = None,
        node_run_id: str | None = None,
        payload: dict[str, JsonValue] | None = None,
        attempt: int | None = None,
    ) -> None:
        """提交一条真实事件；payload 是脱敏摘要（问句/SQL/行/Prompt 不进事件）。"""
        assert event_sink is not None
        parent = run.get("parent_node_run_id")
        event_sink.append(
            RunEvent(
                run_id=str(run["run_id"]),
                event_type=event_type,
                payload=payload or {},
                node_id=node_id,
                node_run_id=node_run_id,
                parent_node_run_id=parent if isinstance(parent, str) else None,
                attempt=attempt,
            )
        )

    def _tool(
        event_type: Literal["TOOL_STARTED", "TOOL_FINISHED"], name: str, **facts: JsonValue
    ) -> None:
        """节点内部子调用留痕（检索/模型/执行）：无接缝 no-op，不影响返回值。"""
        if event_sink is None:
            return
        run = _ACTIVE_RUN.get()
        if run is None:
            return
        node = _ACTIVE_NODE.get()
        _emit(
            run,
            event_type,
            node_id=node[0] if node else None,
            node_run_id=node[1] if node else None,
            payload={"name": name, **facts},
        )

    def _node_traced(
        node_id: str, node: Callable[..., dict[str, Any]], *, with_config: bool = False
    ) -> Callable[..., dict[str, Any]]:
        """节点接缝：NODE_STARTED → 节点体 → NODE_FINISHED / NODE_FAILED。

        NODE_STARTED 写失败 → 节点体不执行；NODE_FINISHED 写失败 → 结果不交付
        （异常传播）。每次进入节点分配独立 node_run_id（重试/多次运行可区分）。

        config 注解必须是 `RunnableConfig`（不带 `| None`）：langgraph 按字面量白
        名单校验注解形式，`from __future__ import annotations` 下 `RunnableConfig
        | None` 是字符串且不在白名单 → 既不注入 config 又发 UserWarning。
        """

        def traced(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
            run = _event_run(config) if event_sink is not None else None
            if run is None:
                return node(state, config) if with_config else node(state)
            node_run_id = uuid4().hex
            _emit(run, "NODE_STARTED", node_id=node_id, node_run_id=node_run_id)
            run_token = _ACTIVE_RUN.set(run)
            node_token = _ACTIVE_NODE.set((node_id, node_run_id))
            try:
                out = node(state, config) if with_config else node(state)
            except BaseException:
                # 失败留痕尽力而为：二次写入失败不得掩盖原始异常（原始异常本身
                # 已保证 fail closed——执行流不会继续到 SQL）
                with suppress(Exception):
                    _emit(run, "NODE_FAILED", node_id=node_id, node_run_id=node_run_id)
                raise
            finally:
                _ACTIVE_NODE.reset(node_token)
                _ACTIVE_RUN.reset(run_token)
            _emit(run, "NODE_FINISHED", node_id=node_id, node_run_id=node_run_id)
            return out

        return traced

    def _route_traced(node_id: str, route: Callable[..., str]) -> Callable[..., str]:
        """边接缝：记录确定的分支裁决（EDGE_TAKEN）；写失败同样 fail closed。

        同 _node_traced：config 注解用 `RunnableConfig`（langgraph 白名单形式）。
        """

        def traced(state: TurnState, config: RunnableConfig) -> str:
            target = route(state)
            run = _event_run(config) if event_sink is not None else None
            if run is not None:
                _emit(run, "EDGE_TAKEN", node_id=node_id, payload={"to": target})
            return target

        return traced

    # -- 组装 ---------------------------------------------------------------
    builder = StateGraph(TurnState)
    builder.add_node("plan", _node_traced("plan", node_plan))
    builder.add_node("retrieve", _node_traced("retrieve", node_retrieve))
    builder.add_node("generate", _node_traced("generate", node_generate))
    builder.add_node("validate", _node_traced("validate", node_validate))
    builder.add_node("execute", _node_traced("execute", node_execute, with_config=True))
    builder.add_node("explain", _node_traced("explain", node_explain))
    builder.add_node("clarify", _node_traced("clarify", node_clarify))
    builder.add_node("handoff", _node_traced("handoff", node_handoff))
    builder.add_edge(START, "plan")
    builder.add_conditional_edges(
        "plan",
        _route_traced("plan", route_after_plan),
        {
            "execute": "execute",
            "retrieve": "retrieve",
            "clarify": "clarify",
            "end": END,
        },
    )
    builder.add_conditional_edges(
        "retrieve",
        _route_traced("retrieve", route_after_retrieve),
        {"generate": "generate", "handoff": "handoff"},
    )
    builder.add_conditional_edges(
        "generate",
        _route_traced("generate", route_after_generate),
        {"validate": "validate", "clarify": "clarify"},
    )
    builder.add_conditional_edges(
        "validate",
        _route_traced("validate", route_after_validate),
        {"execute": "execute", "clarify": "clarify"},
    )
    builder.add_conditional_edges(
        "execute",
        _route_traced("execute", route_after_execute),
        {"explain": "explain", "end": END},
    )
    builder.add_edge("clarify", END)
    builder.add_edge("handoff", END)
    builder.add_edge("explain", END)
    if not persist:
        # ADR-0026 决策 ③：无状态子执行图与上方默认分支共用**同一套**节点/边定义
        # （execute 实现只有一份），只在编译点分叉——不带 checkpointer 才是真无状态
        # （新建 MemorySaver 只是不落盘，仍会造成有会话轨迹可查的假象）
        return builder.compile()
    # 默认分支保持逐字不变（决策 ①：注入是加法，不改历史行为）。用 `is None` 而不是
    # `or`：checkpointer 是第三方对象，其真值语义不由我们定义（空存储若实现
    # `__len__` 就会被 `or` 误判为「没传」，于是静默换回 MemorySaver——正是要防的形态）
    return builder.compile(
        checkpointer=(
            MemorySaver(serde=_CHECKPOINT_SERDE) if checkpointer is None else checkpointer
        )
    )


def turn_from_state(state: dict[str, Any], session_id: str, turns: int = 1) -> TurnResult:
    """图最终状态 → 对外 TurnResult（kind 判定顺序：blocked → error → clarify → answer）。

    轮数的单一事实源是状态里的 `turns`（ADR-0020 决策 ⑤，plan 节点每轮 +1）；
    参数 `turns` 只在状态里没有该键时兜底（直调测试的桩状态），调用方不再记账。

    图表渲染点是本函数（ADR-0025 决策 ②）：chart 只挂 answer 轮，由本轮
    sql/rows/columns + 编译声明的 time_column 确定性渲染——CLI 与 HTTP 共用
    此单一调用点，spec 不落 checkpoint（派生数据，重算即可）。
    """
    question = str(state.get("question", ""))
    base = dict(
        session_id=session_id,
        question=question,
        turns_in_session=int(state.get("turns") or turns),
        usage=state.get("usage") or {},
        engine=state.get("engine", "deterministic"),
        path=state.get("path"),
        validation_issues=state.get("validation_issues") or (),
        explanation=state.get("explanation"),
    )
    if state.get("block_reason"):
        return TurnResult(kind="blocked", block_reason=str(state["block_reason"]), **base)
    if state.get("error"):
        # 失败终态也携带执行事实（ADR-0026 决策 ③）：node_execute 只在 SQL 真正
        # 送达执行器后才写 sql/latency_ms（执行器异常轮），其余 error 来源
        # （编译失败/身份解析失败/内部缺失）保持冲刷值 None/0.0——调用方据此
        # 区分「执行后失败」与「未执行被拒」，不靠猜。
        return TurnResult(
            kind="error",
            error=str(state["error"]),
            sql=state.get("sql"),
            latency_ms=state.get("latency_ms", 0.0),
            **base,
        )
    if state.get("handoff_reason"):
        return TurnResult(kind="handoff", handoff_reason=str(state["handoff_reason"]), **base)
    clarification = state.get("clarification")
    if isinstance(clarification, ClarificationRequest):
        return TurnResult(kind="clarify", clarification=clarification, **base)
    plan = state.get("plan")
    if not isinstance(plan, Plan):
        return TurnResult(kind="error", error="状态机未产出结果（内部错误）", **base)
    turn = TurnResult(
        kind="answer",
        metric=plan.metric,
        sql=state.get("sql"),
        columns=state.get("columns") or (),
        rows=state.get("rows") or (),
        row_count=state.get("row_count", 0),
        latency_ms=state.get("latency_ms", 0.0),
        time_column=state.get("time_column"),
        **base,
    )
    # 渲染拒绝（空结果/坏数值/行宽不齐）→ chart=None：数据仍按既有契约返回，
    # 图表失败不反噬回答，也不把「画不了」伪装成空图
    try:
        chart = render_chart(turn, time_columns=(turn.time_column,) if turn.time_column else ())
    except ChartError:
        return turn
    return replace(turn, chart=chart)


class DataAgent:
    """会话级 Agent 入口：graph 生命周期 + 会话轮数管理（多轮对话体验）。

    用法
    ----
        agent = DataAgent(executor=execute_sql, budget=build_budget(meta))
        r1 = agent.ask("按分支统计 2013 年佣金收入，列出前 5 名")
        r2 = agent.ask("2013 年成交量和交易额分别是多少？")  # kind=clarify
        r1.kind  # "answer"；r1.sql / r1.rows / r1.explanation …

    多轮边界见 agent/state.py docstring：同一 session 连续提问 + 每轮留痕，
    支持同构追问补全（"那 2014 年呢"，ADR-0014 ②），自由代词指代不猜。

    快照绑定的两个参数（ADR-0019 决策 ①⑥）
    --------------------------------------
    snapshot      : 运行时解析出的完整绑定 `data.identity.RuntimeSnapshot`——含
                    meta **加上**「这份 meta 是怎么被选中的」（source）与「是否
                    等于 HEAD」（bound_to_head），后两者是 `/ask` 回显的键。给了它
                    就不必再给 snapshot_meta；两个都给且 meta 不一致 → 构造即抛
                    ValueError（预算与回显不得来自两份 meta）。
    snapshot_meta : 只给 meta（评测与测试桩：绑哪份由外部决定，无解析来源可言）→
                    `self.snapshot` 为 None，回显为 null，而不是假称「未绑定 HEAD」。

    会话存储（ADR-0020 决策 ①）
    --------------------------
    checkpointer  : None = 进程内 `MemorySaver`（会话轨迹随重启消失）；传入
                    `SqliteSaver` 等实例则跨重启续接。是否持久化由**部署方**经
                    `agent.factory.create_live_agent()` 读 `ATLAS_CHECKPOINT_DB`
                    决定，不在图构造层隐式默认——本类的全部既有调用方（评测与
                    tests/ 的 27 处 `DataAgent(` 构造）因此零变化、零落盘。
    """

    def __init__(
        self,
        model: SemanticModel | None = None,
        *,
        engine: str = "stub",
        executor: Executor,
        budget: Budget,
        allow_candidate: bool = False,
        generator: PlanGenerator | None = None,
        linker: SchemaLinker | None = None,
        compiler: Compiler | None = None,
        snapshot_meta: dict[str, Any] | None = None,
        snapshot: RuntimeSnapshot | None = None,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        if snapshot is not None and snapshot_meta is not None and snapshot.meta != snapshot_meta:
            # 消息带出两份 sha：只说「参数不一致」会让人回去翻调用栈才知道是哪个
            # 快照被绑错（同 ADR-0019 判据 6 对消息内容的要求）
            raise ValueError(
                "DataAgent 的快照绑定有两个来源：snapshot.meta 与 snapshot_meta 不一致"
                f"（snapshot.sha={snapshot.sha}，snapshot_meta.sha="
                f"{snapshot_meta.get('sha')}）——只传其一即可：Guard 白名单与 `/ask` "
                "回显必须是同一份 meta（ADR-0019 决策 ②）"
            )
        self.model = model or SemanticModel()
        # 注入的 saver 由本实例长期持有（决策 ③ 的连接生命周期 = 进程）：
        # SqliteSaver 包着 sqlite3.Connection，若无引用持有则会被 GC 关掉连接
        self.checkpointer = checkpointer
        # 图依赖集（ADR-0026 决策 ③）：主图与无状态子执行图共用同一组构造参数，
        # 只在 persist 上分叉——依赖注入（fake linker/compiler 等）两个图拿到的是
        # **同一批实例**，保证两路行为逐字节同源
        self._graph_kwargs: dict[str, Any] = {
            "engine": engine,
            "executor": executor,
            "budget": budget,
            "allow_candidate": allow_candidate,
            "generator": generator,
            "linker": linker,
            "compiler": compiler,
            "snapshot_meta": snapshot.meta if snapshot is not None else snapshot_meta,
            # 事件接缝（ADR-0031 T05b）：主图与无状态子图拿到同一 sink；运行上下文
            # 仍由每次 invoke 的 config 传入（本参数不含 run_id，图实例可被多 run 复用）
            "event_sink": event_sink,
        }
        self._graph = build_graph(self.model, checkpointer=checkpointer, **self._graph_kwargs)
        # 实例级可重入锁（ADR-0026 决策 ④①）：ask / run_plan / analyze（T07）的
        # 读取状态 → 身份比较 → 父轮开始 → 子步 → 父轮结束整段在同一把锁内完成，
        # 保证分析四步不被并发的普通请求插入（决策 ④ 的串行化口径）。RLock：
        # analyze（T07）持锁期间会再调本实例的 _run_analysis_step / _write_analysis_state，
        # 重入不死锁。仅进程内单实例语义——不声称支持多进程 / 多实例部署。
        self._lock = threading.RLock()
        # 无状态子执行图（ADR-0026 决策 ③）：懒构建——SchemaLinker 等重依赖已在
        # 上面为主图就绪，兄弟图只是第二次编译；不拖慢既有 27 处构造点的启动。
        # None = 尚未构建（不是「构建失败」），首次子步时才补上。
        self._analysis_graph: CompiledStateGraph[Any, Any, Any, Any] | None = None
        # 多期分析父轮上下文（T07 的 analyze 在逐子步调用前写入、结束后清除；
        # 本类只读）：子步 payload 的 question/session_id 取父轮值，缺省回落
        # run_plan 同口径的随机会话与 plan:<metric> 问句。
        self._analysis_context: dict[str, str] | None = None
        self.snapshot_meta = snapshot.meta if snapshot is not None else snapshot_meta
        # 完整绑定事实（决策 ①/⑥）：`snapshot_meta` 只有内容，没有「这份 meta 是怎么
        # 被选中的」（source）与「是否等于 HEAD」（bound_to_head）——而后者正是
        # `/ask` 与 `/health` 必须回显的键。None = 未经运行时解析构造的 agent
        # （测试桩 / 评测脚本只给 meta），回显为 null，不假称「绑在 HEAD 上」。
        self.snapshot = snapshot

    def _invoke_turn(
        self,
        sid: str,
        payload: dict[str, Any],
        identity: dict[str, object] | None,
        run_context: dict[str, Any] | None = None,
    ) -> TurnResult:
        """回合装配与执行（ask / run_plan 共享）：thread_id、身份校验、invoke、埋点。

        thread_id 带模型命名空间（ADR-0020 决策 ④）：多域 Agent 共享同一个
        checkpoint 文件后，裸 sid 会让 finance / retail 两条会话互相覆写同一
        thread——用户表现为在零售里问完、换到金融用同 sid 追问，补全出来的是
        **另一个域**的 last_plan。用 model.name 而不是文件名：模型名是语义身份、
        与 ossie 文件名解耦（compiler.py 的既有注释），换绑定文件不该让历史会话
        失效。对外仍是裸 sid（`TurnResult.session_id` / HTTP 契约不变）。

        用 RunnableConfig 标注而不是裸 dict：`get_state` 与 `invoke` 的重载只认它
        （裸 dict 实测被 mypy 判为「无匹配重载」）。configurable 是同一个 dict 引用，
        下面补 identity 对两个调用都可见，不必重建 config。

        run_context 是运行上下文（ADR-0031 T05c）：`/runs` 面传
        `{"run_id": ...}`，经 configurable.atlas_run 发给图的事件接缝（T05b），
        使本轮真实事件归入该 run。None（CLI/评测/既有调用）→ 无事件接缝，行为
        与接入前逐字节一致。
        """
        configurable: dict[str, Any] = {"thread_id": f"{self.model.name}:{sid}"}
        config: RunnableConfig = {"configurable": configurable}
        if run_context is not None:
            # 拷贝：运行上下文由服务端每请求分配（run_id 等），图实例可被多 run 复用
            configurable["atlas_run"] = dict(run_context)
        if identity is not None:
            configurable["identity"] = identity
            # 身份绑定与校验（ADR-0020 决策 ⑥）：指纹随状态进 checkpoint（跨重启仍
            # 可校验），校验**在 invoke 之前**——冲突轮不写状态、不执行 SQL、不推进
            # 轮数。只写哈希不写 claims 本体（0011 不外泄身份细节）。
            fingerprint = claims_fingerprint(identity)
            bound = self._graph.get_state(config).values.get("session_fingerprint")
            if bound is not None and bound != fingerprint:
                raise SessionIdentityConflict(
                    f"会话 {sid} 已绑定另一身份：换身份必须换新 session_id"
                    "（ADR-0020 决策 ⑥；重签 token 也算换身份）"
                )
            payload["session_fingerprint"] = fingerprint
        final = self._graph.invoke(payload, config=config)
        # 轮数来自状态字段 turns（决策 ⑤），组装只是读它——不再有进程内记账
        turn = turn_from_state(dict(final), sid)
        # Day 50 全链路埋点：atlas.turn span + 指标（未 configure 时 no-op，
        # 埋点故障被隔离——观测绝不反噬主链路，见 observability/otel.py）
        record_turn(turn, snapshot_sha=(self.snapshot_meta or {}).get("sha"))
        return turn

    def ask(
        self,
        question: str,
        *,
        session_id: str | None = None,
        identity: dict[str, object] | None = None,
        run_context: dict[str, Any] | None = None,
    ) -> TurnResult:
        """问一句：同一 session_id 视为同一会话（多轮计数与事实留痕）。

        参数
        ----
        question   : 自然语言问句（每轮全量解析；同构残句追问走 last_plan 补全，
                     自由代词指代会反问完整重述，见 ADR-0014 ②）。
        session_id : 会话键（缺省生成随机会话，单轮）。checkpoint 里的 thread_id 是
                     `f"{model.name}:{sid}"`，但**返回值与 HTTP 契约仍是裸 `sid`**。
        identity   : 已验证 claims 字典（verify_token 输出形态：role + user_context
                     …，ADR-0011 硬化项）：非 None 时本轮按角色渲染行级策略并随
                     Guard 注入。**每轮独立**——claims 本体经 invoke config 传递不落
                     checkpoint；不传即无策略（多轮会话中由 API 层每轮显式下推，见
                     serving/api）。**落 checkpoint 的只有校验用指纹哈希**：首轮写入
                     `session_fingerprint`，续轮在 invoke 前比对，不一致抛
                     `SessionIdentityConflict`（ADR-0020 决策 ⑥）。
        run_context: 运行上下文（`{"run_id": ...}`，ADR-0031 `/runs` 面传入）；
                     None = 无事件接缝（CLI / 评测 / 旧调用行为不变）。

        返回
        ----
        TurnResult：kind ∈ answer / clarify / blocked / error。

        抛出
        ----
        SessionIdentityConflict：同 `session_id` 换身份（含重签 token）。
        """
        sid = session_id or f"session-{uuid4().hex[:8]}"
        # 实例级可重入锁（ADR-0026 决策 ④①）：与 run_plan / analyze（T07，须加入
        # 同一把锁）串行化，保证分析四步不被并发的普通请求插入中间状态。
        with self._lock:
            return self._invoke_turn(
                sid, {"question": question, "session_id": sid}, identity, run_context
            )

    def run_plan(
        self,
        plan: Plan,
        *,
        session_id: str | None = None,
        identity: dict[str, object] | None = None,
        question: str | None = None,
        run_context: dict[str, Any] | None = None,
    ) -> TurnResult:
        """执行一个给定的 Plan（`/api/v1/plan/execute` 的底层，ADR-0022 决策 ③）。

        与 ask() 共享回合装配（`_invoke_turn`），只差初始 state 多一个
        `plan_override`——`node_plan` 首行短路，不调 Planner、不进候选链；执行仍只
        经 `node_execute` 唯一通道（编译 → `resolve_claims` 渲染策略 → Guard
        enforce + 二次只读校验 → 执行），查询逻辑不在 HTTP 层复制（N3）。

        参数
        ----
        plan       : 结构化指标计划（`/compile` 请求体同构）。语义层里不存在的
                     指标名等非法计划**不在本方法内预检**：在前方执行链上以
                     `kind="error"` 如实返回（ADR-0022 代价 ⑤），不是异常。
        session_id : 会话键（缺省生成随机会话——一次性，不写任何可续接的会话态）。
                     给定则与 /ask 同一会话空间（thread_id 命名空间相同），同受
                     身份指纹 422 约束；`node_explain` 会把该 Plan 写进 last_plan，
                     成为下一轮残句追问的补全基线（代价 ⑥，期望行为）。
        identity   : 已验证 claims（与 ask 同语义：身份指纹校验 + 行级策略注入）。
        question   : 展示/审计用问句（**不参与解析**；缺省 `plan:<metric>` 形态）。
        run_context: 运行上下文（`{"run_id": ...}`，ADR-0031 `/runs` 面传入）；
                     None = 无事件接缝（旧调用行为不变）。

        返回
        ----
        TurnResult：kind ∈ answer / blocked / error——无自然语言解析面故
        clarify 不可达；不进候选链故 handoff 不可达（ADR-0022 决策 ③）。

        抛出
        ----
        SessionIdentityConflict：同 `session_id` 换身份（与 ask 同一校验路径）。
        """
        sid = session_id or f"session-{uuid4().hex[:8]}"
        payload: dict[str, Any] = {
            "question": question or f"plan:{plan.metric}",
            "session_id": sid,
            "plan_override": plan,
        }
        # 实例级可重入锁（ADR-0026 决策 ④①）：与 ask / analyze（T07，须加入
        # 同一把锁）串行化，保证分析四步不被并发的普通请求插入中间状态。
        with self._lock:
            return self._invoke_turn(sid, payload, identity, run_context)

    def analyze(
        self,
        question: str,
        *,
        session_id: str | None = None,
        identity: dict[str, object] | None = None,
    ) -> AnalysisResult:
        """多期间变化贡献分析编排（ADR-0026 决策③，T07）：解析 → 前置门 → 四步 → 综合。

        编排次序（task-7-brief 检查单 + 控制器裁决）：

        1. AnalysisPlanner 解析（T03，同源 `self.model`，group_limit =
           min(10000, budget.max_rows)）：None → 恰好委托**一次** ask（普通问数，
           分析字段全空，不写分析记账）；ClarificationRequest → 澄清父轮（计一
           父轮、零 SQL、零 LLM、不开始分析记账）；AnalysisPlan → 完整四步编排。
        2. T06 父轮开始（`_begin_analysis`）：严格身份准入（冲突在任何写入之前
           抛出——轮数不变、零 SQL）→ turns+1 → analysis_record=running。
           解析失败在前，不执行任何 SQL。
        3. 资格前置门（T02 证据，factory 挂 `analysis_eligibility` 属性；测试
           直接构造的 agent 无此属性 = 视同资格缺失，绝不静默放行）：不通过 →
           answer 父轮 + unavailable（missing_eligibility / snapshot_mismatch
           稳定安全码，映射见下）+ plan 必带 + steps=()（零步零 SQL；N1 不编造
           数字），终态 failed 落账（无 failed_index——没有步骤失败）。
        4. 通过 → 顺序执行 T05 四步（无状态兄弟图 `_run_analysis_step`，同一批
           注入依赖；每步一条 `record_analysis_step` 步骤 span，失败步也有终态
           span），每步已执行事实经 `encode_rows` 带类型入账；blocked/error 即
           裁剪终止（部分结果与被拒 SQL 不落证据、失败后零调用、失败步角色
           保留），四步全成才调 T04 `synthesize`。
        5. 父轮 TurnResult（决策⑥：不冒充单 SQL 结果）+ `record_turn` 恰一次，
           包装 AnalysisResult 返回（闭环契约：validate_analysis_result 零违规）。

        身份准入的三种口径：委托 ask 走 `_invoke_turn` 宽松转移；四步编排走
        `_begin_analysis` 严格准入（running 恢复 + 绑定会话拒绝匿名/异身份）；
        澄清父轮按 ask 同款宽松校验（冲突在任何写入前抛出）。

        统一性（brief 检查单 4）：整个编排持实例锁（与 ask / run_plan 同锁）；
        model / snapshot / 身份 / 预算全部来自本实例构造参数——每步复用缓存的
        persist=False 兄弟图，绝不逐步构建活图。`_analysis_context` 全程 finally
        清除（异常中断不留陈旧上下文）。elapsed_ms 用 monotonic 钟覆盖整个编排；
        latency_ms 仍只汇总实际执行的子 SQL 耗时。

        参数
        ----
        question  : 分析问句（原样解析与留痕，不改写）。
        session_id: 会话键（缺省随机会话；与 ask 同一 thread 空间）。
        identity  : 已验证 claims（None = 匿名；四步编排按 `_begin_analysis`
                    严格准入，running 恢复只接受与记录一致的身份）。

        返回
        ----
        AnalysisResult：五终态（ok / unavailable / clarify / blocked / error）
        的字段组合满足 T01 闭合矩阵。

        抛出
        ----
        SessionIdentityConflict：身份冲突（在任何状态写入之前抛出，零 SQL）。
        RuntimeError：子步出现意外终态（决策③边界，不静默）。
        """
        started = time.monotonic()
        sid = session_id or f"session-{uuid4().hex[:8]}"
        sha_raw = (self.snapshot_meta or {}).get("sha")
        snapshot_sha = sha_raw if isinstance(sha_raw, str) else None
        semantic_sha = self.model.source_sha256
        budget = self._graph_kwargs["budget"]
        group_limit = min(10_000, int(budget.max_rows))

        def _elapsed() -> float:
            return (time.monotonic() - started) * 1000

        with self._lock:
            parsed = AnalysisPlanner(self.model, group_limit=group_limit).plan(question)
            if parsed is None:
                # 无分析意图：恰好委托一次 ask（同一回合装配通道，无分析字段）。
                turn = self._invoke_turn(sid, {"question": question, "session_id": sid}, identity)
                return AnalysisResult(
                    turn=turn,
                    plan=None,
                    steps=(),
                    attribution=None,
                    reason_code=None,
                    elapsed_ms=_elapsed(),
                    snapshot_sha=snapshot_sha,
                    semantic_sha256=semantic_sha,
                )
            if isinstance(parsed, ClarificationRequest):
                turn = self._clarify_turn(sid, question, parsed, identity)
                return AnalysisResult(
                    turn=turn,
                    plan=None,
                    steps=(),
                    attribution=None,
                    reason_code=None,
                    elapsed_ms=_elapsed(),
                    snapshot_sha=snapshot_sha,
                    semantic_sha256=semantic_sha,
                )
            plan = parsed
            # T06 父轮开始：严格身份准入（冲突在任何写入之前抛出）→ turns+1 →
            # running 记录（版本/问句/身份指纹/空 steps）→ 单轮残留清理。
            self._begin_analysis(sid, question, identity)
            config: RunnableConfig = {"configurable": {"thread_id": f"{self.model.name}:{sid}"}}
            parent_no = int(dict(self._graph.get_state(config).values or {}).get("turns") or 1)
            steps: list[TurnResult] = []
            terminal: Literal["blocked", "error"] | None = None
            gate_code: AnalysisReasonCode | None = None
            attribution: Attribution | None = None
            result_code: AnalysisReasonCode | None = None
            try:
                # 资格前置门（T02 证据由 factory 挂 analysis_eligibility；缺失/
                # 不可用一律拒绝，绝不静默放行）。原因码映射为稳定安全码：
                # snapshot/semantic 版本不一致 → snapshot_mismatch，其余（含
                # 证据缺失/畸形/未知）→ missing_eligibility。
                eligibility = getattr(self, "analysis_eligibility", None)
                if not (
                    isinstance(eligibility, dict)
                    and eligibility.get("available")
                    and eligibility.get("eligible")
                ):
                    raw = eligibility.get("reason") if isinstance(eligibility, dict) else None
                    gate_code = (
                        "snapshot_mismatch"
                        if raw in ("snapshot_mismatch", "semantic_hash_mismatch")
                        else "missing_eligibility"
                    )
                if gate_code is not None:
                    # 控制器裁决：前置门失败 = answer 父轮 + unavailable +
                    # plan 必带 + steps=()（零步零 SQL）；N1：无实测不编造数字。
                    attribution = Attribution(
                        status="unavailable",
                        baseline=None,
                        current=None,
                        delta=None,
                        items=(),
                        reason_code=gate_code,
                        text="分析前置资格门未通过：当前快照下该指标/维度组合不可做变化贡献分解。",
                    )
                    result_code = gate_code
                else:
                    # 准入证据落账：计划投影 + 两期绑定 + 快照/语义层版本
                    # （T08/T09 共用口径；只存有界事实，不存贡献率/叙事）。
                    self._write_analysis_state(
                        sid,
                        {
                            "analysis_record": {
                                "plan_projections": [plan_projection(p) for p in plan.sub_plans],
                                "baseline": {
                                    "granularity": plan.baseline.granularity,
                                    "value": plan.baseline.value,
                                },
                                "current": {
                                    "granularity": plan.current.granularity,
                                    "value": plan.current.value,
                                },
                                "snapshot_sha": snapshot_sha,
                                "semantic_sha256": semantic_sha,
                            }
                        },
                    )
                    # 子步上下文（T05 契约）：session_id/question 逐子步在位，
                    # 结束（成功/失败/异常）后 finally 清除，不留陈旧上下文。
                    self._analysis_context = {"session_id": sid, "question": question}
                    # 四步循环抽进私有生成器（ADR-0028 GATE-④ 技术前置：稳定
                    # 分步接缝），此处**急切驱动到耗尽**——锁不外泄、副作用与
                    # 旧内联逐字等价（决策③）。
                    events = list(
                        self._iter_analysis_step_events(
                            plan, identity=identity, session_id=sid, parent_no=parent_no
                        )
                    )  # noqa: E501
                    steps = [ev.step for ev in events]
                    if events and events[-1].step.kind in ("blocked", "error"):
                        terminal_step = events[-1].step
                        terminal = terminal_step.kind  # type: ignore[assignment]
                        assert terminal is not None  # 收窄为 Literal[blocked|error]
                        result_code = events[-1].reason_code
                    if terminal is None:
                        # 四步全成才调 T04 综合（纯函数，精确 Decimal）。
                        attribution = synthesize(plan, tuple(steps))
                        result_code = attribution.reason_code
            finally:
                # 异常中断不留陈旧上下文（brief 必办 2）：成功/失败/异常统一清除。
                self._analysis_context = None

            if gate_code is not None:
                terminal_record: dict[str, Any] = {"status": "failed", "reason_code": gate_code}
            elif terminal is None:
                assert attribution is not None  # 不变量：无 terminal 必已综合
                terminal_record = {
                    "status": "completed",
                    "attribution_status": attribution.status,
                    "reason_code": attribution.reason_code,
                }
            else:
                terminal_record = {
                    "status": "failed",
                    "failed_index": len(steps),
                    "reason_code": result_code,
                }
            self._write_analysis_state(sid, {"analysis_record": terminal_record})

            # 父轮 TurnResult（决策⑥：不冒充单 SQL 结果；latency 只汇总执行步）。
            parent = TurnResult(
                kind="answer" if terminal is None else terminal,
                session_id=sid,
                question=question,
                turns_in_session=parent_no,
                metric=plan.metric,
                latency_ms=float(sum(s.latency_ms for s in steps)),
            )
            if terminal == "blocked" and steps:
                parent = replace(parent, block_reason=steps[-1].block_reason)
            elif terminal == "error" and steps:
                parent = replace(parent, error=steps[-1].error)
            # 父轮 atlas.turn 恰一次（子步只写步骤 span，不进用户轮指标）。
            record_turn(parent, snapshot_sha=snapshot_sha)
            return AnalysisResult(
                turn=parent,
                plan=plan,
                steps=tuple(steps),
                attribution=attribution,
                reason_code=result_code,
                elapsed_ms=_elapsed(),
                snapshot_sha=snapshot_sha,
                semantic_sha256=semantic_sha,
            )

    def _iter_analysis_step_events(
        self,
        plan: AnalysisPlan,
        *,
        identity: dict[str, object] | None,
        session_id: str,
        parent_no: int,
    ) -> Iterator[AnalysisStepEvent]:
        """四步分析循环的分步接缝（ADR-0028 GATE-④ 技术前置）：每执行一步产出一个事件。

        把 `analyze()` 内部 T05 四步循环抽成生成器：按 `ANALYSIS_ROLES` 顺序逐步
        执行、每步埋一条步骤 span、落一份证据，并 `yield` 一个 `AnalysisStepEvent`；
        遇 blocked/error 落裁剪证据后 `break`（被拒 SQL 不外泄，N3），意外终态抛
        `RuntimeError`（决策③边界，不静默）。行为与被抽出的旧内联循环**逐字等价**。

        刻意**不是**公开流：`analyze()` 在同一把锁内急切驱动本生成器到耗尽，锁的
        生命周期不外泄给消费者（否则消费者停在 yield = 持锁 / finally 推迟清上下文）。
        将来若 GATE-④ 时间前置满足、SSE 解禁，是另写消费者急切驱动本接缝——接缝
        存在且被契约测试锁定，但不因此新增任何执行面或对外端点（裁定 C / N2 / N3）。

        前置调用契约（由 `analyze()` 负责、本生成器不重复）：已通过资格前置门、
        已落准入证据记录（`_begin_analysis` + 计划投影 `analysis_record`）、已置
        `self._analysis_context`——子步 `_run_analysis_step` 依赖该上下文。

        参数
        ----
        plan      : 解析出的四步 AnalysisPlan（sub_plans 顺序即 ANALYSIS_ROLES）。
        identity  : 已验证 claims（透传子步，None = 无行级策略）。
        session_id: 会话键（供步骤 span 记账，与 analyze 父轮同 thread）。
        parent_no : 父轮序号（步骤 span 的 turns 标签）。

        产出
        ----
        AnalysisStepEvent：每执行一步一个；成功步 reason_code=None，终态步携带
        稳定安全码且其 step.sql 为 None（被拒 SQL 不入事件）。
        """
        evidence: list[dict[str, Any]] = []
        for i, role in enumerate(ANALYSIS_ROLES):
            step = self._run_analysis_step(plan.sub_plans[i], identity=identity)
            # 每个已执行步骤恰一条步骤 span（失败步也有终态 span）；
            # 埋点故障在 otel 层隔离，不反噬编排。
            record_analysis_step(
                step, model=self.model, session_id=session_id, turns=parent_no, role=role
            )
            if step.kind == "answer":
                entry: dict[str, Any] = {
                    "index": i + 1,
                    "role": role,
                    "status": "ok",
                    "sql": step.sql,
                    "columns": list(step.columns),
                    "rows": encode_rows(step.rows),
                    "latency_ms": step.latency_ms,
                }
                evidence.append(entry)
                self._write_analysis_state(
                    session_id, {"analysis_record": {"steps": list(evidence)}}
                )
                yield AnalysisStepEvent(
                    role=role, step=step, evidence_entry=entry, reason_code=None
                )
                continue
            if step.kind in ("blocked", "error"):
                # 裁剪终止：部分结果与被拒 SQL 不落证据（只留 role/status/原因码），
                # 失败后零调用，角色保留。
                failed_code: AnalysisReasonCode = (
                    "guard_blocked" if step.kind == "blocked" else "execution_error"
                )
                entry = {
                    "index": i + 1,
                    "role": role,
                    "status": step.kind,
                    "reason_code": failed_code,
                }
                evidence.append(entry)
                self._write_analysis_state(
                    session_id, {"analysis_record": {"steps": list(evidence)}}
                )
                yield AnalysisStepEvent(
                    role=role, step=step, evidence_entry=entry, reason_code=failed_code
                )
                break
            raise RuntimeError(
                f"分析子步出现意外终态 {step.kind!r}（决策③：子步只允许 answer/blocked/error）"
            )

    def _clarify_turn(
        self,
        sid: str,
        question: str,
        parsed: ClarificationRequest,
        identity: dict[str, object] | None,
    ) -> TurnResult:
        """分析澄清父轮：计一父轮、零 SQL、零 LLM、冲刷残留分析记录。

        与普通 clarify 轮同款状态语义（node_plan 冲刷集的同构落账，直接
        `_apply_state` 写入——不 invoke 父查询图，杜绝候选链与 Planner 介入）：
        turns+1、原问句、澄清对象入 clarification，单轮结果残留全清理；
        analysis_record 同步冲刷为 None（澄清轮是**新的用户轮**——此前被
        Guard 拒绝的分析终态会留下 status=failed 的残留记录，不冲刷会让
        下轮读到陈旧分析态，与 node_plan 的新用户轮清旧记录同构）。
        身份按 ask 宽松转移校验：已绑定会话收到异身份澄清在**任何写入之前**
        抛 SessionIdentityConflict；带身份的新会话按首轮绑定语义写指纹
        （0011：只落哈希不落 claims 本体）。
        """
        config: RunnableConfig = {"configurable": {"thread_id": f"{self.model.name}:{sid}"}}
        values = dict(self._graph.get_state(config).values or {})
        old_turns = int(values.get("turns") or 0)
        payload: dict[str, Any] = {
            "question": question,
            "session_id": sid,
            "turns": old_turns + 1,
            "clarification": parsed,
            "plan": None,
            "plan_override": None,
            "candidates": None,
            "unmatched": False,
            "reason": None,
            "attempts": 0,
            "validated": False,
            "sql": None,
            "rows": None,
            "columns": None,
            "time_column": None,
            "row_count": 0,
            "latency_ms": 0.0,
            "validation_issues": None,
            "path": None,
            "engine": "deterministic",
            "explanation": None,
            "policy_effect": None,
            "block_reason": None,
            "error": None,
            "handoff_reason": None,
            # R2（ADR-0026 控制器裁定）：与 node_plan 冲刷集同构——澄清轮是新的
            # 用户轮，冲掉上一分析终态（如 blocked 留下的 status=failed）的残留
            "analysis_record": None,
        }
        if identity is not None:
            fingerprint = claims_fingerprint(identity)
            bound = values.get("session_fingerprint")
            if bound is not None and bound != fingerprint:
                raise SessionIdentityConflict(
                    f"会话 {sid} 已绑定另一身份：换身份必须换新 session_id"
                    "（ADR-0020 决策 ⑥；重签 token 也算换身份）"
                )
            payload["session_fingerprint"] = fingerprint
        self._apply_state(sid, payload)
        # 父轮澄清对象从 checkpoint 读回后再组装（与 _invoke_turn → turn_from_state
        # 同构）：serde msgpack 往返会把 dataclass 的 tuple 字段退化为 list，父轮
        # 返回值必须与 checkpoint 持有的形态一致，不得漏出未往返的原始对象。
        stored = dict(self._graph.get_state(config).values or {})
        roundtripped = stored.get("clarification")
        turn = TurnResult(
            kind="clarify",
            session_id=sid,
            question=question,
            turns_in_session=int(stored.get("turns") or old_turns + 1),
            clarification=roundtripped
            if isinstance(roundtripped, ClarificationRequest)
            else parsed,
        )
        record_turn(turn, snapshot_sha=(self.snapshot_meta or {}).get("sha"))
        return turn

    # -- 多期间分析父轮记账（ADR-0026 决策 ④⑤⑥；analyze 编排在 T07） -------
    # 调用契约：_begin_analysis 与 _write_analysis_state 只能在持有 self._lock
    # 的分析流程里调用（analyze（T07）必须在 ask / run_plan 的同一把锁内编排
    # 开始 → 子步 → 结束四步，否则并发普通请求可插入父轮中间状态）。

    def _begin_analysis(
        self,
        sid: str,
        question: str,
        identity: dict[str, object] | None,
    ) -> dict[str, Any]:
        """开始一轮多期间分析父轮：身份校验 → running 标记 → 父轮状态写入。

        只认持久化状态（不新增进程内轮数副本）。动作次序（ADR-0026 决策 ④③⑤）：

        1. 读父 thread 的 checkpoint values；
        2. 身份转移判定（冲突在**任何状态写入之前**抛出——轮数不变、零写入）：
           - 旧记录 running（中断恢复）：仅接受与记录一致的 identity——先前匿名
             只可匿名接续，先前实名只可同指纹接续；不一致抛
             SessionIdentityConflict。running→interrupted 的标记在本次进入时
             发生（同身份），**不增旧轮计数**。
           - 无 running 记录：已绑定 session_fingerprint 的会话收到匿名 analyze
             按冲突拒绝；异身份同样拒绝；未绑定的匿名会话沿用首次绑定语义
             （带身份调用则写入指纹）。普通 ask / run_plan 的宽松转移规则
             （`_invoke_turn`）不受本套更严规则影响。
        3. 写父轮状态：turns=旧值+1、原问句、analysis_record（status=running，
           版本/问句/身份指纹/空 steps）、analysis_followup_blocked=True，并清理
           plan/sql/rows 等全部单轮结果残留；**不 invoke 父查询图**——本方法不
           执行任何 SQL（决策 ④③「不 invoke 父查询图」）；
        4. 每次写入后断言图不再待继续（`next == ()`，见 `_apply_state`）。

        参数
        ----
        sid      : 会话键（thread_id = `f"{model.name}:{sid}"`，与 ask 同空间）。
        question : 分析原问句（原样入记录与状态，不改写）。
        identity : 分析请求身份（None = 匿名）。指纹经 claims_fingerprint 计算，
                   claims 本体不落 checkpoint。

        返回
        ----
        本轮新 analysis_record 的浅拷贝（status=running，含 schema_version）。
        T07 的 analyze 以它为基准逐子步补 evidence，结束后写终态。

        抛出
        ----
        SessionIdentityConflict：身份冲突 / running 恢复身份不一致（零写入）。
        RuntimeError：状态写入后图仍待继续（checkpointer 行为异常，不静默）。
        """
        with self._lock:
            config: RunnableConfig = {"configurable": {"thread_id": f"{self.model.name}:{sid}"}}
            values = dict(self._graph.get_state(config).values or {})
            fingerprint = claims_fingerprint(identity) if identity is not None else None
            old_record = values.get("analysis_record")
            resuming = False
            if isinstance(old_record, dict) and old_record.get("status") == "running":
                resuming = True
                # 中断恢复（决策 ④⑤）：running 记录只接受与记录一致的身份——
                # 先前匿名仅可匿名接续，先前实名仅可同指纹接续。标记发生在
                # 这里（下一次分析进入），不增旧轮计数。
                prev_fingerprint = old_record.get("identity_fingerprint")
                if prev_fingerprint != fingerprint:
                    raise SessionIdentityConflict(
                        f"会话 {sid} 存在 running 分析记录，仅接受与记录一致的身份"
                        "接续（先前匿名仅可匿名接续；ADR-0026 决策 ④⑤）"
                    )
            else:
                # 常规进入：已绑定会话收到匿名 / 异身份分析按冲突拒绝——
                # 在任何状态写入之前抛出，轮数不变、零 SQL。
                bound = values.get("session_fingerprint")
                if bound is not None and (fingerprint is None or bound != fingerprint):
                    raise SessionIdentityConflict(
                        f"会话 {sid} 已绑定身份，不接受匿名或异身份的多期间分析"
                        "（换身份必须换新 session_id；ADR-0026 决策 ④②）"
                    )
            if resuming:
                # 同身份再进入：旧 running 先标 interrupted（只补一个键，steps
                # 等其余证据原样保留），再接受新一轮——历史快照序列留证。
                self._write_analysis_state(sid, {"analysis_record": {"status": "interrupted"}})
            record: dict[str, Any] = {
                "schema_version": ANALYSIS_RECORD_VERSION,
                "status": "running",
                "question": question,
                "identity_fingerprint": fingerprint,
                "steps": [],
            }
            old_turns = int(values.get("turns") or 0)
            # 单轮结果残留全清理（决策 ④③）：last_plan 刻意**不在**清理集——
            # 分析不回退它，靠 analysis_followup_blocked 挂起残句追问（决策 ⑥）。
            payload: dict[str, Any] = {
                "question": question,
                "session_id": sid,
                "turns": old_turns + 1,
                "analysis_record": record,
                "analysis_followup_blocked": True,
                "plan": None,
                "plan_override": None,
                "clarification": None,
                "candidates": None,
                "unmatched": False,
                "reason": None,
                "attempts": 0,
                "validated": False,
                "sql": None,
                "rows": None,
                "columns": None,
                "time_column": None,
                "row_count": 0,
                "latency_ms": 0.0,
                "validation_issues": None,
                "path": None,
                "engine": "deterministic",
                "explanation": None,
                "policy_effect": None,
                "block_reason": None,
                "error": None,
                "handoff_reason": None,
            }
            if fingerprint is not None:
                payload["session_fingerprint"] = fingerprint
            self._apply_state(sid, payload)
            return dict(record)

    def _write_analysis_state(self, sid: str, updates: dict[str, Any]) -> None:
        """写分析父轮状态（子步证据 / 终态），**不推进用户轮数**。

        只 update_state 父 thread（as_node="explain"，决策 ④④）；turns 是轮数
        单一事实源（ADR-0020 决策 ⑤），只有 `_begin_analysis` 与普通图执行
        （node_plan）可以写它——updates 里带 turns 直接拒绝。analysis_record
        的写入语义：updates 里的 dict 与 checkpoint 里现存记录做 **键级合并**
        （子步补 steps、终态补 status 不抹掉既有键）；`_begin_analysis` 开新
        记录走自己的整体替换 payload，不经本方法合并。

        参数
        ----
        sid     : 会话键（与 ask / _begin_analysis 同一 thread 空间）。
        updates : 要写入的状态键值（analysis_record 传部分 dict 即可）。

        抛出
        ----
        ValueError：updates 试图写 turns。
        RuntimeError：写入后图仍待继续（checkpointer 行为异常，不静默）。
        """
        if "turns" in updates:
            raise ValueError(
                "_write_analysis_state 不推进用户轮数：turns 是轮数单一事实源"
                "（ADR-0020 决策 ⑤），只有 _begin_analysis / 普通图执行可以写它"
            )
        with self._lock:
            config: RunnableConfig = {"configurable": {"thread_id": f"{self.model.name}:{sid}"}}
            values = self._graph.get_state(config).values or {}
            payload = dict(updates)
            incoming = updates.get("analysis_record")
            if isinstance(incoming, dict):
                current = values.get("analysis_record")
                payload["analysis_record"] = (
                    {**current, **incoming} if isinstance(current, dict) else dict(incoming)
                )
            self._apply_state(sid, payload)

    def _apply_state(self, sid: str, payload: dict[str, Any]) -> None:
        """把 payload 写进父 thread 并断言图不再待继续（决策 ④④）。

        as_node="explain" 显式写入（不冒充节点执行、不触发路由）；写入后读回
        `next`，非空说明 checkpointer 落了等待继续的状态——分析记账绝不能把
        父图挂进待续队列，此时抛 RuntimeError（持久化层异常不返回伪成功）。

        参数
        ----
        sid     : 会话键（thread_id = `f"{model.name}:{sid}"`）。
        payload : 本次要落 checkpoint 的状态键值（键必须在 TurnState schema 内，
                  不在 schema 的键会被 langgraph 过滤）。
        """
        config: RunnableConfig = {"configurable": {"thread_id": f"{self.model.name}:{sid}"}}
        self._graph.update_state(config, payload, as_node="explain")
        if self._graph.get_state(config).next != ():
            raise RuntimeError(
                f"会话 {sid} 的分析状态写入后图仍待继续（next != ()）："
                "分析记账不允许把父图挂进待续队列（ADR-0026 决策 ④④）"
            )

    def _stateless_graph(self) -> CompiledStateGraph[Any, Any, Any, Any]:
        """同源无状态子执行图（ADR-0026 决策 ③）：首次访问时构建并缓存。

        与主图 `self._graph` 的关系：同一个 `build_graph`、同一套节点/边定义、
        同一组构造参数（`self._graph_kwargs`，注入的依赖是**同一批实例**），
        仅 persist=False——编译不带 checkpointer。因此子步无独立 checkpoint、
        无身份指纹绑定（父级身份绑定是上层职责，本类只透传 identity），也
        不经 `_invoke_turn`（无 thread_id 可言）。

        返回
        ----
        编译后的无状态 LangGraph（`checkpointer is None`）；每 agent 只构建一次。
        """
        if self._analysis_graph is None:
            self._analysis_graph = build_graph(self.model, persist=False, **self._graph_kwargs)
        return self._analysis_graph

    def _run_analysis_step(
        self,
        plan: Plan,
        *,
        identity: dict[str, object] | None,
    ) -> TurnResult:
        """执行多期分析的一个子步骤（ADR-0026 决策 ③）：无状态图 + plan_override 直执。

        每步以**全新输入 state** 调 persist=False 兄弟图：`node_plan` 首行短路
        （plan_override 用后即焚），不调 Planner、不进候选链；执行仍只经
        `node_execute` 唯一通道（编译 → resolve_claims 渲染策略 → Guard enforce
        → 执行器）。与 `_invoke_turn` 的差异是刻意的：无 checkpointer 故无指纹
        绑定/get_state 校验（父级身份绑定是上层职责，本方法只透传 identity）；
        不调 `record_turn`（子步不写用户轮指标，ADR-0026：无用户轮计数、无
        atlas.turn.count 增量）。

        失败步的执行事实由图状态如实带回：error/blocked 终态同样有 latency_ms
        与已执行证据，并区分「未执行被拒」（Guard 拒绝/编译失败/身份解析失败 →
        sql=None、latency=0）与「执行后失败」（执行器异常 → 记录已过 Guard 的
        SQL + 尝试耗时）——上层的耗时汇总只计实际执行的子 SQL。

        参数
        ----
        plan     : 本步骤的固定 Plan（AnalysisPlan.sub_plans 的成员）。非 Plan
                   一律 TypeError：静默回落 Planner 解析会让子步重新生成子问句，
                   违反「子步不猜」边界。
        identity : 已验证 claims（与 ask/run_plan 同语义、同通道：
                   `config["configurable"]["identity"]`）。None = 无行级策略。

        返回
        ----
        TurnResult：kind ∈ answer / blocked / error——无自然语言解析面故
        clarify 不可达；不进候选链故 handoff 不可达。turns_in_session 是子步
        局部产物（每步从 1 起），不推进任何用户会话的轮数。
        """
        if not isinstance(plan, Plan):
            raise TypeError(f"plan 必须是 Plan 实例，收到 {type(plan).__name__}")
        ctx = self._analysis_context or {}
        sid = str(ctx.get("session_id") or f"session-{uuid4().hex[:8]}")
        question = str(ctx.get("question") or f"plan:{plan.metric}")
        payload: dict[str, Any] = {"question": question, "session_id": sid, "plan_override": plan}
        configurable: dict[str, Any] = {}
        if identity is not None:
            configurable["identity"] = identity
        config: RunnableConfig = {"configurable": configurable}
        final = self._stateless_graph().invoke(payload, config=config)
        return turn_from_state(dict(final), sid)

    def submit_feedback(
        self,
        result: TurnResult,
        kind: FeedbackKind,
        *,
        comment: str = "",
        feedback_dir: Path | None = None,
    ) -> Path:
        """一键纠错 → 修正日志（status=pending_review，见 agent/feedback.py）。

        参数
        ----
        result      : 被纠错的回合（自动带出 question/sql/metric 等快照）。
        kind        : 纠错类型（wrong_value / wrong_metric / wrong_time /
                      misunderstood / other）。
        comment     : 用户原话（可空串 = 纯一键纠错）。
        feedback_dir: 修正日志目录（默认 eval/failures/user_feedback/）。

        返回
        ----
        Path：写入的 JSON 文件路径。
        """
        return submit_feedback(
            FeedbackSubmission(
                question=result.question,
                kind=kind,
                comment=comment,
                session_id=result.session_id,
                metric=result.metric,
                sql=result.sql,
                path=result.path,
                engine=result.engine,
                turns_in_session=result.turns_in_session,
            ),
            feedback_dir=feedback_dir or DEFAULT_FEEDBACK_DIR,
        )
