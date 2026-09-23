"""共享安全执行内核（ADR-0031 T04c）：编译 → 策略 → Guard → 执行 → 校验。

设计口径
--------
- **唯一执行通道**：所有执行入口（graph 节点、工具注册表、直执通道）都委托本
  模块；连接层（`eval.runner.execute_sql` 等）已是连接器与上下文的委托层
  （T04b），本模块是"从 Plan 或 SQL 到结果"的唯一内核（N3 单通道）。
- **等价提取**：`execute_plan` 与 `agent.graph.node_execute` 的执行链逐段对应
  （现场编译 → 身份策略 → Guard → 执行计时 → ExecutionValidator），不新增
  行为；T04d 把 graph 改为委托本内核后，行为等价由既有 graph 测试锁定。
- **失败语义**（与 ADR-0026 决策 ③ 一致，供上层区分）：
  - `blocked`：Guard 拒绝（UnsafeQuery / BudgetExceeded）——不携带被拒 SQL；
  - `error`：编译失败 / 身份不可解析 / 执行期故障——**执行后失败**携带已执行
    SQL 与尝试耗时（`executed=True`），编译与身份失败不携带（`executed=False`）；
  - `ok`：执行成功 + 校验注记（`validation_issues` 如实透传，不隐藏空结果）。
- **fail-closed**：`RunContext.compiler is None` 或 `executor is None` 一律
  `error` 拒绝——不回退到任何默认编译器或全局连接（RunContext 的合同语义）。

边界（诚实声明）
----------------
- 不向 Guard 传 `model=`：与 node_execute 现状等价（时间窗回退与跨表谓词补
  join 的 model 通道尚未启用，保持现状，不借重构悄悄扩大行为面）。
- 行级策略只来自 `RunContext.identity`（已验证 claims）；本模块**不校验**
  token（那是 serving/auth 的职责），只做 claims → 策略解析与注入。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

from agent.compiler import CompileError, Plan
from agent.runtime.context import RunContext
from agent.security.sql_guard import BudgetExceeded, Policy, UnsafeQuery, enforce
from agent.tools.execution_validator import ExecutionValidator
from serving.auth import AuthError, resolve_claims

ExecutionKind = Literal["ok", "blocked", "error"]

_VALIDATOR = ExecutionValidator()  # 无状态，模块级单例


@dataclass(frozen=True)
class ExecutionResult:
    """一次执行的结论（安全序列化：被拒 SQL 不外泄；rows 沿用 Decimal 口径）。

    消费方可用 `executed` 区分「未执行被拒」（Guard 拒绝 / 编译失败）与
    「执行后失败」——耗时汇总只应计入实际执行的子 SQL（ADR-0026 决策 ③）。

    Attributes
    ----------
    kind : ok | blocked | error。
    sql : 已执行（或已送达执行器）的 Guard 后 SQL；blocked 与编译失败为 None。
    columns / rows / row_count : 结果集（rows 为 tuple of tuples，与 TurnState 同形）。
    latency_ms : 仅实际执行的耗时；未执行恒 None。
    validation_issues : ExecutionValidator 注记（如 empty_result）；不隐藏，不上抛。
    time_column : 编译声明的时间轴列别名（ADR-0025 决策 ①3）；无则 None。
    policy_effect : 行级策略生效句（只含角色 + 策略名，不含条件值——0011 口径）。
    block_reason : Guard 拒绝类型与原因（不携带被拒 SQL）。
    error : 编译 / 身份 / 执行故障描述。
    executed : True 表示 SQL 已送达执行器（执行后失败时仍为 True）。
    """

    kind: ExecutionKind
    sql: str | None = None
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    row_count: int = 0
    latency_ms: float | None = None
    validation_issues: tuple[str, ...] = ()
    time_column: str | None = None
    policy_effect: str | None = None
    block_reason: str | None = None
    error: str | None = None
    executed: bool = False


def execute_plan(plan: Plan, context: RunContext) -> ExecutionResult:
    """Plan → 安全执行（编译 → 身份策略 → Guard → 执行 → 校验）。

    Parameters
    ----------
    plan : 结构化指标计划。调用方可能来自 HTTP 直执通道（ADR-0022 决策 ③），
           本函数不信任调用方——全量走编译与 Guard，编译失败是回合级 error。
    context : 运行上下文（预算 / 执行器 / 身份 / 编译器 / 数据身份）。

    Returns
    -------
    ExecutionResult：kind ∈ {ok, blocked, error}；语义见类 docstring。
    """
    compiler = context.compiler
    if compiler is None:
        return ExecutionResult(
            kind="error", error="RunContext.compiler 缺失：无法编译 Plan（fail-closed）"
        )
    try:
        sql, _ = compiler.compile(plan)
    except CompileError as exc:
        # 直执通道可注入任意 Plan：编译失败必须可诊断，且不外泄未被执行的 SQL
        return ExecutionResult(kind="error", error=f"{type(exc).__name__}: {exc}")

    policy: Policy | None = None
    effect: str | None = None
    identity: object = context.identity
    if identity is not None:
        if not isinstance(identity, dict):
            return ExecutionResult(
                kind="error", error="identity 必须为已验证 claims 字典（role + user_context）"
            )
        policy_name = compiler.model.default_row_policy
        if policy_name is None:
            # 缺失即拒绝（ADR-0021 决策 ②，default_deny 精神）：不降级为无策略执行
            return ExecutionResult(
                kind="error", error="语义模型未声明 default_row_policy，无法注入行级策略"
            )
        try:
            resolved = resolve_claims(identity, policy_name=policy_name)
        except AuthError as exc:
            # 身份不可解析 → 拒绝执行；SQL 未经过 Guard，不携带
            return ExecutionResult(kind="error", error=f"身份策略解析失败：{exc}")
        policy = Policy(name=resolved.policy_name, condition=resolved.condition)
        # 生效句只含角色 + 策略名（0011 不外泄细节：条件值不出本模块）
        effect = f"行级策略已生效（角色 {resolved.role}，策略 {resolved.policy_name}）"

    try:
        if policy is None:
            guarded, _ = enforce(sql, budget=context.budget)
        else:
            guarded, _ = enforce(sql, policy=policy, budget=context.budget)
    except (UnsafeQuery, BudgetExceeded) as exc:
        # 只报拒绝类型与原因，不携带被拒 SQL（纵深防御，不外泄细节）
        return ExecutionResult(kind="blocked", block_reason=f"{type(exc).__name__}: {exc}")

    return _run(
        guarded,
        context,
        grouped=bool(plan.dimensions),
        time_column=compiler.emitted_time_column(plan),
        policy_effect=effect,
    )


def execute_guarded_sql(sql: str, context: RunContext) -> ExecutionResult:
    """SQL 级入口：Guard + 执行（供已持有 SQL 的调用方委托，如工具注册表）。

    SQL 不经过编译（调用方给出的任意 SQL），因此**不做身份策略注入**——
    与 `registry.execute_readonly` 现状等价；Plan 通道请用 `execute_plan`。

    Parameters
    ----------
    sql : 任意 SQL（调用方不可信）；只读 / 黑名单 / 表白名单 / LIMIT 由 Guard 强制。
    context : 运行上下文（预算与执行器的最小装配；compiler 不参与）。

    Returns
    -------
    ExecutionResult：语义同 execute_plan；无编译与策略段。
    """
    if not isinstance(sql, str) or not sql.strip():
        return ExecutionResult(kind="error", error="参数校验失败：sql 必须是非空字符串")
    try:
        guarded, _ = enforce(sql, budget=context.budget)
    except (UnsafeQuery, BudgetExceeded) as exc:
        return ExecutionResult(kind="blocked", block_reason=f"{type(exc).__name__}: {exc}")
    return _run(guarded, context)


def _run(
    guarded: str,
    context: RunContext,
    *,
    grouped: bool = False,
    time_column: str | None = None,
    policy_effect: str | None = None,
) -> ExecutionResult:
    """执行已过 Guard 的 SQL 并校验结果（模块内唯一触库点）。"""
    executor = context.executor
    if executor is None:
        # fail-closed：没有装配数据源就拒绝，绝不回退到某个全局连接
        return ExecutionResult(
            kind="error", error="RunContext.executor 缺失：无可用数据源（fail-closed）"
        )
    started = time.perf_counter()
    try:
        rows, columns = executor(guarded)
    except Exception as exc:  # noqa: BLE001 - 执行故障要完整转述，不中断会话
        # 执行后失败（ADR-0026 决策 ③）：SQL 已过 Guard 且已送达执行器，如实记下
        # 已执行证据与尝试耗时；上层耗时汇总只计实际执行的子 SQL
        return ExecutionResult(
            kind="error",
            sql=guarded,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            error=f"{type(exc).__name__}: {exc}",
            executed=True,
        )
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    rows_t = tuple(tuple(r) for r in rows)
    issues = _VALIDATOR.check(list(rows_t), list(columns), grouped=grouped).issues
    return ExecutionResult(
        kind="ok",
        sql=guarded,
        columns=tuple(columns),
        rows=rows_t,
        row_count=len(rows_t),
        latency_ms=latency_ms,
        validation_issues=issues,
        time_column=time_column,
        policy_effect=policy_effect,
        executed=True,
    )
