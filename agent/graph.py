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

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import sqlglot
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RunnableConfig
from sqlglot import exp

from agent.compiler import CompileError, Compiler, Plan, SemanticModel
from agent.feedback import (
    DEFAULT_FEEDBACK_DIR,
    FeedbackKind,
    FeedbackSubmission,
    submit_feedback,
)
from agent.generator import GenerationResult, Generator, validate_plan_json
from agent.planner import ClarificationRequest, Planner
from agent.security.sql_guard import (
    Budget,
    BudgetExceeded,
    Policy,
    UnsafeQuery,
    enforce,
)
from agent.state import TurnResult, TurnState
from agent.tools.chart import ChartError, render_chart
from agent.tools.execution_validator import ExecutionValidator
from agent.tools.schema_linker import SchemaLinker
from data.identity import RuntimeSnapshot
from observability.otel import record_turn
from serving.auth import AuthError, claims_fingerprint, resolve_claims

# 执行器同构约定（与 eval/runner.execute_sql 一致）：只读执行 guarded SQL
Executor = Callable[[str], tuple[list[tuple[Any, ...]], list[str]]]

DEFAULT_CANDIDATE_K = 5
MAX_GENERATE_ATTEMPTS = 2  # 候选链 validate 失败重试上限（共 2 次 generate 尝试）

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

    def generate(self, question: str, k: int = 5) -> GenerationResult: ...


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

    返回
    ----
    LangGraph CompiledStateGraph：invoke({"question": q}, config={"configurable":
    {"thread_id": "<model.name>:<session_id>"}})——thread_id 的模型前缀是约定
    （ADR-0020 决策 ④），由 DataAgent.ask 负责拼装；直接用本图者须自己带上，否则
    多域共享同一 checkpoint 文件时会互相覆写。最终状态字段见 agent/state.TurnState；
    组装 TurnResult 请用 ask() / DataAgent。

    异常
    ----
    ValueError：executor 或 budget 未提供（图不执行未过 Guard 的 SQL）。
    """
    if executor is None:
        raise ValueError("executor 必填：只读 SQL 执行器（测试可注入 fake）")
    if budget is None:
        raise ValueError("budget 必填：Guard 预算（表白名单 = 锁定快照）")
    model = model or SemanticModel()
    planner = Planner(model)
    linker = linker or SchemaLinker(model)
    compiler = compiler or Compiler(model)
    gen = generator or Generator(model, engine=engine)
    validator = ExecutionValidator()

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
        linked = linker.link(str(state["question"]), k=DEFAULT_CANDIDATE_K)
        if not linked.candidates:
            # 0 候选：不猜不空转，落 handoff 终端（生成无素材、反问无候选可澄清）
            return {"reason": "schema linking 未检索到注册域候选指标"}
        return {"candidates": linked.candidates}

    # -- 节点：generate（LLM 候选 Plan，候选必须再过确定性关卡） -----------
    def node_generate(state: TurnState) -> dict[str, Any]:
        result = gen.generate(str(state["question"]), k=DEFAULT_CANDIDATE_K)
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
            retried = gen.generate(str(state["question"]), k=DEFAULT_CANDIDATE_K)
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
    # identity 经 invoke config 注入（每轮独立、不落 checkpoint，见 DataAgent.ask
    # docstring）；非 None 时先 resolve_claims 渲染行级策略（与 rls-verify/demo
    # 同机制：Policy(name, condition) → enforce 注入），策略名由当前语义模型
    # 的 default_row_policy 决定（ADR-0021：域 → 策略的唯一事实源；缺失即拒绝，
    # 不降级为无策略执行）；谓词非法/无 join 路径由 Guard 拒绝（blocked，不外泄
    # 细节）——graph 层不做二次校验实现。
    def node_execute(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
        plan = state.get("plan")
        if not isinstance(plan, Plan):
            return {"error": "内部状态缺失 Plan（不应到达 execute）"}
        sql = state.get("sql")
        if sql is None:  # deterministic 链：validate 未预检，现场编译
            try:
                sql, _ = compiler.compile(plan)
            except CompileError as exc:
                # `/plan/execute` 可注入任意 Plan（ADR-0022 决策 ③/代价⑤）：编译失败
                # 是回合级 error 而非 500——HTTP 面不得复制编译预检（N3 单通道），
                # 故捕获点只能在这里
                return {"error": f"{type(exc).__name__}: {exc}"}
        policy: Policy | None = None
        effect: str | None = None
        identity = (config or {}).get("configurable", {}).get("identity")
        if identity is not None:
            if not isinstance(identity, dict):
                return {"error": "identity 必须为已验证 claims 字典（role + user_context）"}
            policy_name = compiler.model.default_row_policy
            if policy_name is None:
                # 缺失即拒绝（ADR-0021 决策 ②，default_deny 精神）：不降级为无策略执行
                return {"error": "语义模型未声明 default_row_policy，无法注入行级策略"}
            try:
                resolved = resolve_claims(identity, policy_name=policy_name)
            except AuthError as exc:
                # 身份不可解析 → 拒绝执行（防御：不携带被拒原因之外的细节）
                return {"error": f"身份策略解析失败：{exc}"}
            policy = Policy(name=resolved.policy_name, condition=resolved.condition)
            # 生效句只含角色 + 策略名（0011 不外泄细节：条件值不出本模块）
            effect = f"行级策略已生效（角色 {resolved.role}，策略 {resolved.policy_name}）"
        try:
            if policy is None:
                guarded, _ = enforce(sql, budget=budget)
            else:
                guarded, _ = enforce(sql, policy=policy, budget=budget)
        except (UnsafeQuery, BudgetExceeded) as exc:
            # 只报拒绝类型与原因，不携带被拒 SQL（纵深防御，不外泄细节）
            return {"block_reason": f"{type(exc).__name__}: {exc}"}
        started = time.perf_counter()
        try:
            rows, columns = executor(guarded)
        except Exception as exc:  # noqa: BLE001 - 执行故障记入回合，不中断会话
            return {"error": f"{type(exc).__name__}: {exc}"}
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        rows_t = tuple(tuple(r) for r in rows)
        issues = validator.check(list(rows_t), list(columns), grouped=bool(plan.dimensions)).issues
        return {
            "sql": guarded,
            "rows": rows_t,
            "columns": tuple(columns),
            # 编译声明的实际时间轴别名（ADR-0025 决策 ①3）：与本轮 plan 同源；
            # plain/rank 与候选链 plan 为 None（渲染端走列名兜底）
            "time_column": compiler.emitted_time_column(plan),
            "row_count": len(rows_t),
            "latency_ms": latency_ms,
            "validation_issues": issues,
            "policy_effect": effect,
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
        # ②）；失败轮（blocked/error）不进 explain，last_plan 保持上轮成功值
        return {"explanation": explanation, "last_plan": plan}

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
            linked = linker.link(str(state["question"]), k=DEFAULT_CANDIDATE_K)
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

    # -- 组装 ---------------------------------------------------------------
    builder = StateGraph(TurnState)
    builder.add_node("plan", node_plan)
    builder.add_node("retrieve", node_retrieve)
    builder.add_node("generate", node_generate)
    builder.add_node("validate", node_validate)
    builder.add_node("execute", node_execute)
    builder.add_node("explain", node_explain)
    builder.add_node("clarify", node_clarify)
    builder.add_node("handoff", node_handoff)
    builder.add_edge(START, "plan")
    builder.add_conditional_edges(
        "plan",
        route_after_plan,
        {
            "execute": "execute",
            "retrieve": "retrieve",
            "clarify": "clarify",
            "end": END,
        },
    )
    builder.add_conditional_edges(
        "retrieve",
        route_after_retrieve,
        {"generate": "generate", "handoff": "handoff"},
    )
    builder.add_conditional_edges(
        "generate",
        route_after_generate,
        {"validate": "validate", "clarify": "clarify"},
    )
    builder.add_conditional_edges(
        "validate",
        route_after_validate,
        {"execute": "execute", "clarify": "clarify"},
    )
    builder.add_conditional_edges(
        "execute", route_after_execute, {"explain": "explain", "end": END}
    )
    builder.add_edge("clarify", END)
    builder.add_edge("handoff", END)
    builder.add_edge("explain", END)
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
        return TurnResult(kind="error", error=str(state["error"]), **base)
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
        self._graph = build_graph(
            self.model,
            engine=engine,
            executor=executor,
            budget=budget,
            allow_candidate=allow_candidate,
            generator=generator,
            linker=linker,
            compiler=compiler,
            snapshot_meta=snapshot.meta if snapshot is not None else snapshot_meta,
            checkpointer=checkpointer,
        )
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
        """
        configurable: dict[str, Any] = {"thread_id": f"{self.model.name}:{sid}"}
        config: RunnableConfig = {"configurable": configurable}
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

        返回
        ----
        TurnResult：kind ∈ answer / clarify / blocked / error。

        抛出
        ----
        SessionIdentityConflict：同 `session_id` 换身份（含重签 token）。
        """
        sid = session_id or f"session-{uuid4().hex[:8]}"
        return self._invoke_turn(sid, {"question": question, "session_id": sid}, identity)

    def run_plan(
        self,
        plan: Plan,
        *,
        session_id: str | None = None,
        identity: dict[str, object] | None = None,
        question: str | None = None,
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
        return self._invoke_turn(sid, payload, identity)

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
