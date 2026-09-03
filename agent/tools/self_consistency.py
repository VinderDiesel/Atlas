"""自洽一致与候选回退路由（Day 33）：多候选执行 → 聚类取众数 / 首有效。

设计（确定性优先，诚实边界）
------------------------------
- self-consistency 原理（任务清单 Day 33）：多次采样 → 执行结果聚类 → 取众数。
  在 Atlas 中"多次采样"来源有二：
  1. 确定性多候选路由（本文件已实现）：SchemaLinker top-k 指标候选 → k 个 Plan
     → 各自编译/Guard/执行 → 相同 result_hash 聚类 → 众数组胜出（平局取
     候选序首位，确定性破例）。
  2. LLM 多次采样（预留）：同一问句多次生成 Plan 候选列表后复用同一聚类逻辑
     （接口已按 Plan 列表抽象，不依赖生成来源）。
- 诚实边界：注册语义域内确定性路由 top1 恒正确（gold 48/48），聚类结果与 top1
  恒等——本组件的实际增量价值在：top1 执行失败/无效时的**候选回退鲁棒性**，
  与未来 LLM 采样的自洽机制（现成的确定性实现，防 LLM 单次采样错误）。
- 本文件不做 SQL 执行（执行由评测侧注入的 compile_guard_execute 闭包完成），
  不 import eval（agent 层不依赖评测层）。

规则
----
- cluster_by_result：按执行结果哈希聚类取众数；平局取候选序最靠前者（确定性）。
- fallback_route：按候选序逐个执行，执行校验（ExecutionValidator）通过即采用；
  全部失败/无效 → chosen_index=None + 汇总 issues/errors（上层转为澄清/重生成）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent.compiler import Plan
from agent.tools.execution_validator import ExecutionValidator, ValidationResult

# 评测侧注入的执行闭包：Plan → 已过 Guard 的执行结果 (rows, columns)
CompileGuardExecute = Callable[[Plan], tuple[list[tuple[Any, ...]], list[str]]]


def cluster_by_result(results: list[tuple[int, str]]) -> int | None:
    """执行结果聚类取众数（返回胜出候选的 plan_index）。

    参数
    ----
    results : [(plan_index, result_hash), ...]；hash 相同 = 执行结果一致。

    返回
    ----
    众数组的 plan_index；空输入返回 None；平局取组内最靠前（即较早采样的
    候选，确定性破例，文档化约定）。

    说明
    ----
    只对"可执行成功"的结果聚类；失败候选由调用方先行剔除。
    """
    if not results:
        return None
    # 按 hash 分组，记录每组出现次数与最靠前的 plan_index
    groups: dict[str, list[int]] = {}
    for idx, digest in results:
        groups.setdefault(digest, []).append(idx)
    best_hash = max(groups, key=lambda h: (len(groups[h]), -min(groups[h])))
    return min(groups[best_hash])


@dataclass
class CandidateOutcome:
    """单个候选的执行结果：ok=执行成功且过校验。"""

    index: int
    ok: bool
    result_hash: str | None = None
    issues: tuple[str, ...] = ()
    error: str | None = None


@dataclass
class RouteResult:
    """回退路由结论：chosen=None 表示全部候选无效（上层触发澄清/重生成）。"""

    chosen: int | None = None
    outcomes: list[CandidateOutcome] = field(default_factory=list)

    @property
    def chosen_outcome(self) -> CandidateOutcome | None:
        if self.chosen is None:
            return None
        return self.outcomes[self.chosen]


def fallback_route(
    plans: list[Plan],
    compile_guard_execute: CompileGuardExecute,
    validator: ExecutionValidator,
    max_attempts: int = 3,
) -> RouteResult:
    """按候选序执行并回退：首个通过执行校验的候选即采用。

    参数
    ----
    plans : 有序候选 Plan 列表（SchemaLinker top-k 或 LLM 采样结果）
    compile_guard_execute : Plan → (rows, columns)（编译 + Guard + 执行的闭包）
    validator : 执行结果校验器（空/全 NULL/非分组多行视为无效）
    max_attempts : 最多尝试前几个候选（防候选爆炸）

    返回
    ----
    RouteResult：chosen=首个有效候选 index；全部失败 → None。
    """
    attempts = min(len(plans), max_attempts)
    outcomes: list[CandidateOutcome] = []
    for idx in range(attempts):
        plan = plans[idx]
        grouped = bool(plan.dimensions)
        try:
            rows, columns = compile_guard_execute(plan)
        except Exception as exc:  # noqa: BLE001 - 编译/Guard/执行失败记入 outcome
            outcomes.append(
                CandidateOutcome(index=idx, ok=False, error=f"{type(exc).__name__}: {exc}")
            )
            continue
        check = validator.check(rows, columns, grouped=grouped)
        if check.ok:
            # 有效结果：取结果哈希供上层做聚类/EX 比对（行序敏感时以 rows 为准）
            outcomes.append(CandidateOutcome(index=idx, ok=True, result_hash=_digest(rows)))
            return RouteResult(chosen=idx, outcomes=outcomes)
        outcomes.append(CandidateOutcome(index=idx, ok=False, issues=check.issues, error=None))
    return RouteResult(chosen=None, outcomes=outcomes)


def _digest(rows: list[tuple[Any, ...]]) -> str | None:
    """行级 sha256（与 eval/runner.result_hash 同规约，轻量本地实现防循环依赖）。

    NULL 固定表示、标量 str 化、tab 分隔、换行连接——口径同 runner；
    若 runner 规约演进，此处为评测层注入优先，本实现仅作默认值。
    """
    import hashlib

    def scalar(v: Any) -> str:
        if isinstance(v, bytes):
            return v.hex()
        if isinstance(v, float):
            return repr(v)
        return str(v)

    if not rows:
        return None
    payload = "\n".join("\t".join("NULL" if v is None else scalar(v) for v in row) for row in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# 重导出供上层类型标注使用（避免直接依赖 Counter 等实现细节）
__all__ = [
    "CandidateOutcome",
    "RouteResult",
    "cluster_by_result",
    "fallback_route",
    "CompileGuardExecute",
    "ValidationResult",
]
