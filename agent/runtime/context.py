"""运行上下文（ADR-0031 T04）：执行契约与快照预算构造。

设计口径
--------
- `RunContext` 是**唯一**的执行装配合同：预算、执行器、已验证身份、数据身份、
  编译器等全部显式注入，测试与生产走同一字段（账本 T04：不设 test_executor
  之类测试专用属性——测试专用属性会让生产路径只有一个「测过的近似体」）。
- **fail-closed 默认值**：`executor=None` 表示没有可用数据源，内核必须拒绝而
  不是回退到某个全局连接；`identity=None` 表示没有已验证 claims，行级策略
  不得注入（node_execute 的既有语义：identity 缺省 → 无策略执行）。
- `budget_from_snapshot` 是旧 `eval.runner.build_budget` 的共享实现（白名单 =
  锁定快照内全部表，N6）；评测与在线执行共用同一份推导，避免两处口径各自漂移。
  构造失败一律 ValueError 且消息带 `row_counts`——静默给空白名单会让一切查询
  被 Guard 以「表不在白名单内」误拒，比说不了更坏。

边界（诚实声明）
----------------
- 本模块不执行查询、不做策略解析（那是执行内核 `execution.py` 的职责），只承载
  上下文与预算构造。内核在 T04c 交付；`compiler` 字段供后续直执通道使用。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from agent.runtime.identity import DataIdentity
from agent.security.sql_guard import Budget

if TYPE_CHECKING:  # agent.compiler 会拉入语义层；本模块只做注解，不制造运行时依赖
    from agent.compiler import Compiler

# 执行器形态：与 eval/runner.execute_sql、graph.py 闭包同构——(SQL) → (行, 列)。
# 内核面向该最小接口，连接实现（connectors/doris.py）与测试替身都是它的实例。
Executor = Callable[[str], tuple[list[tuple[Any, ...]], list[str]]]

DEFAULT_MAX_ROWS = 10_000  # 与 sql_guard.DEFAULT_MAX_ROWS 同值；快照预算的显式上限
DEFAULT_TIMEZONE = "+08:00"  # AGENTS.md §7.3：时区显式声明


@dataclass(frozen=True)
class RunContext:
    """一次执行的完整上下文（不可变；内核只读消费）。

    Attributes
    ----------
    budget : Guard 预算（必填）——表白名单与行数上限的唯一来源。
    executor : 查询执行器；None = 不可执行（fail-closed，内核拒绝而非回退）。
    identity : 已验证 claims 字典（role + user_context）；None = 无行级策略。
    data_identity : 数据身份联合（snapshot | live）；None = 未绑定（N6 门禁拒）。
    compiler : Plan 编译器的显式注入位（直执 / 调试通道用）。
    reference_time : 相对时间的参考时刻；None = 由调用方按当前时间处理。
    timezone : 运行时区（默认 +08:00，与全部时间戳契约一致）。
    """

    budget: Budget
    executor: Executor | None = None
    identity: dict[str, object] | None = None
    data_identity: DataIdentity | None = None
    compiler: Compiler | None = None
    reference_time: datetime | None = None
    timezone: str = DEFAULT_TIMEZONE


def budget_from_snapshot(
    snapshot_meta: dict[str, Any], *, max_rows: int = DEFAULT_MAX_ROWS
) -> Budget:
    """快照 meta → Guard 预算（旧 `eval.runner.build_budget` 的共享实现）。

    白名单 = `atlas.{namespace}.{table}` 全展开：评测 SQL 只允许触碰已锁快照
    的表——未来新增表未入快照前不允许被执行，防止对象漂移（data/snapshots/README.md）。
    权威形态是「命名空间 → 表 → 行数」映射；同时接受非字符串的可迭代表名集合
    ——ADR-0019 调用点判据用合成 meta（如 `{"dwd": {"probe_only"}}`）验证
    「预算必须来自解析出的那份 meta」，旧 `build_budget` 只要求可迭代表名，
    委托后行为必须等价。

    Parameters
    ----------
    snapshot_meta : 锁定快照 meta；必须含 row_counts（「命名空间 → 表 → 行数」）。
    max_rows : 强制行数上限；默认与 sql_guard 同值。

    Returns
    -------
    Budget
        `dialect="doris"`，`allowed_tables` = 快照内全部表。

    Raises
    ------
    ValueError
        `row_counts` 缺失或某命名空间的值不可迭代表名——静默给空白名单会让一切
        查询被误拒为「表不在白名单内」；字符串值会被逐字符迭代成垃圾白名单，
        必须显式拒绝（即把配置错误伪装成安全拒绝，比说不了更坏）。
    """
    counts = snapshot_meta.get("row_counts")
    if not isinstance(counts, dict):
        raise ValueError(
            f"快照 meta 缺 row_counts（或形态不是对象）：{snapshot_meta.get('sha')!r}"
            "——无法构造 Guard 白名单（data/snapshots/README.md 口径）"
        )
    allowed: set[str] = set()
    for ns, tables in counts.items():
        if isinstance(tables, (str, bytes)) or not isinstance(
            tables, (dict, list, tuple, set, frozenset)
        ):
            raise ValueError(
                f"快照 meta 的 row_counts 不可展开为白名单：{ns!r} → {tables!r}"
                "（应为「命名空间 → 表 → 行数」，见 data/snapshots/README.md）"
            )
        allowed.update(f"atlas.{ns}.{table}" for table in tables)
    return Budget(dialect="doris", max_rows=max_rows, allowed_tables=frozenset(allowed))
