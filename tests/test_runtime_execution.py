"""共享执行内核与连接器合同（ADR-0031 T04）。

口径
----
- 连接器（`agent/runtime/connectors/`）是仓库内**唯一**持有数据库连接的模块：
  评测（`eval.runner`）与在线执行都导入同一份共享实现，不再有两处 pymysql
  连接代码（T04：评测导入共享实现，不反向成为在线执行工厂）。
- `ConnectorCapabilities` 必须诚实：未实现的能力（元数据探测、取消查询、快照读、
  一致性分析）一律 False——不得把设计写成已完成（N2）。
- `SourceSpec`：`secret_ref` 只能引用环境变量（N9），`timezone` 必须显式声明
  （AGENTS §7.3）——两条都是 fail-closed，缺了即拒绝而不是填默认值。
- 本文件同时承载连接器合同（T04b）与共享执行内核用例（T04c）；内核用例的
  执行器替身经连接器接缝注入，与生产路径走同一协议。

设施
----
- `executor_spy`（T04c）经**连接器装配接缝**（`Connector` 协议的 `executor()`）
  注入：测试替身与真实 `DorisConnector` 走同一协议，不给内核加 test-only 旁路。
"""

from __future__ import annotations

import ast
import json
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest


def _spec(**overrides: Any) -> Any:
    """合法 Doris 源合同；缩窄场景用 overrides 覆盖单字段。"""
    from agent.runtime.connectors.base import SourceSpec

    fields: dict[str, Any] = {
        "source_id": "doris-primary",
        "revision": "rev-2026-09-22-1",
        "connector_kind": "doris",
        "secret_ref": "env:ATLAS_DORIS",
        "allowed_catalogs": frozenset({"atlas"}),
        "allowed_tables": frozenset({"atlas.dwd.fact_trades"}),
        "timezone": "+08:00",
        "tls_policy": "prefer",
        "query_budget": 10_000,
    }
    fields.update(overrides)
    return SourceSpec(**fields)


# ---------------------------------------------------------------------------
# 连接器合同（T04b）
# ---------------------------------------------------------------------------


def test_doris_connector_satisfies_protocol_and_reports_honest_capabilities() -> None:
    """DorisConnector 满足 Connector 协议；能力清单不得多报（N2）。"""
    from agent.runtime.connectors.base import Connector
    from agent.runtime.connectors.doris import DorisConnector

    connector = DorisConnector(_spec())
    assert isinstance(connector, Connector)
    caps = connector.capabilities
    assert caps.dialect == "doris"
    assert caps.read_only is True
    # 未实现的能力一律 False——设计未完成前不得声明可用
    assert caps.metadata_probe is False
    assert caps.cancel_query is False
    assert caps.snapshot_read is False
    assert caps.consistent_analysis is False


def test_doris_connector_rejects_non_doris_kind() -> None:
    """不把 MySQL 协议兼容当成数据源支持（D03）：kind 不符即拒绝。"""
    from agent.runtime.connectors.base import ConnectorError
    from agent.runtime.connectors.doris import DorisConnector

    with pytest.raises(ConnectorError, match="doris"):
        DorisConnector(_spec(connector_kind="mysql"))


def test_source_spec_requires_explicit_timezone() -> None:
    """时区必须显式 UTC 偏移（AGENTS §7.3）：简写与 IANA 名都不接受。"""
    for bad in ("+8", "Asia/Shanghai", ""):
        with pytest.raises(ValueError, match="时区"):
            _spec(timezone=bad)


def test_source_spec_rejects_inline_secret() -> None:
    """秘密只由环境变量引用解析（N9）：secret_ref 必须是 `env:` 引用。"""
    with pytest.raises(ValueError, match="环境变量"):
        _spec(secret_ref="root:plaintext-password")


def test_source_spec_requires_identity_and_tls_facts() -> None:
    """源身份与 TLS 策略必须非空——空值是配置错误，不静默接受。"""
    with pytest.raises(ValueError, match="source_id"):
        _spec(source_id="")
    with pytest.raises(ValueError, match="connector_kind"):
        _spec(connector_kind="")
    with pytest.raises(ValueError, match="tls_policy"):
        _spec(tls_policy="")


def test_connector_executor_routes_through_shared_implementation(monkeypatch: Any) -> None:
    """executor() 产出共享实现：env 参数解析、行列提取、游标与连接必关。

    传输层（pymysql.connect）以假连接替换——这是边界 mock：被测的是本仓库的
    参数解析与游标生命周期，不是驱动本身。
    """
    from agent.runtime.connectors import doris as doris_mod
    from agent.runtime.connectors.doris import DorisConnector

    captured: dict[str, Any] = {}
    closed = {"cursor": False, "conn": False}

    class _Cursor:
        # 真实驱动 description 是 7 元组；实现只取 desc[0]，最小形状即可
        description = (("trade_count",), ("total",))

        def __init__(self) -> None:
            self.executed: list[str] = []

        def execute(self, sql: str) -> None:
            self.executed.append(sql)

        def fetchall(self) -> list[tuple[Any, ...]]:
            return [(3, Decimal("1.5"))]

        def close(self) -> None:
            closed["cursor"] = True

    class _Conn:
        def __init__(self) -> None:
            self.cursor_obj = _Cursor()

        def cursor(self) -> _Cursor:
            return self.cursor_obj

        def close(self) -> None:
            closed["conn"] = True

    fake = _Conn()

    def _fake_connect(**kwargs: Any) -> _Conn:
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(doris_mod.pymysql, "connect", _fake_connect)
    monkeypatch.setenv("DORIS_HOST", "doris.internal.example")
    monkeypatch.setenv("DORIS_PORT", "9031")
    monkeypatch.setenv("DORIS_USER", "atlas_readonly")
    monkeypatch.setenv("DORIS_PASSWORD", "test-only-password")

    executor = DorisConnector(_spec()).executor()
    rows, columns = executor("SELECT 1 FROM atlas.dwd.fact_trades")

    assert captured == {
        "host": "doris.internal.example",
        "port": 9031,
        "user": "atlas_readonly",
        "password": "test-only-password",
        "connect_timeout": 15,
    }
    assert rows == [(3, Decimal("1.5"))]
    assert columns == ["trade_count", "total"]
    assert fake.cursor_obj.executed == ["SELECT 1 FROM atlas.dwd.fact_trades"]
    assert closed == {"cursor": True, "conn": True}


# ---------------------------------------------------------------------------
# 旧入口 = 共享实现（兼容层是委托，不是第二份实现）
# ---------------------------------------------------------------------------


def test_runner_execute_sql_is_shared_implementation() -> None:
    """评测导入共享实现：`eval.runner.execute_sql` 与连接器是同一函数对象。"""
    from agent.runtime.connectors.doris import execute_sql as shared
    from eval import runner

    assert runner.execute_sql is shared


def test_runner_build_budget_is_shared_implementation() -> None:
    """旧 build_budget 委托共享实现：同一函数对象，白名单口径只此一份。"""
    from agent.runtime.context import budget_from_snapshot
    from eval import runner

    assert runner.build_budget is budget_from_snapshot


def test_runner_reexports_are_explicit_for_type_checkers() -> None:
    """兼容名的再导出形式对 mypy 可见（结构断言；`make test` 不跑 mypy）。

    重命名导入（`import budget_from_snapshot as build_budget`）在运行时与显式
    再导出等价，但 mypy strict（--no-implicit-reexport）只认「同名 as 同名」，
    下游 7 处消费方会报 attr-defined（cli / factory / analysis_eligibility /
    api_acceptance / compare_4way / e2e_acceptance / rag_eval）——运行时断言
    测不出这类破功（同 0019 判据 5(c)、0021 判据 2 的既有踩坑口径）。
    """
    src = (Path(__file__).resolve().parent.parent / "eval" / "runner.py").read_text(
        encoding="utf-8"
    )
    # 只看代码行：注释里的反例示例文本不应触发判定
    code_lines = [
        line.strip()
        for line in src.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert "from agent.runtime.connectors.doris import execute_sql as execute_sql" in code_lines
    assert "build_budget = budget_from_snapshot" in code_lines, (
        "build_budget 兼容名必须是模块属性别名（重命名导入对 mypy 不可见）"
    )
    renamed = "from agent.runtime.context import budget_from_snapshot as build_budget"
    assert renamed not in code_lines, "禁重命名导入：mypy strict 下不构成显式再导出"


# ---------------------------------------------------------------------------
# T04c 设施：正式合同（RunContext）与连接器装配接缝
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parent.parent
_SNAPSHOT_META = json.loads(
    (_REPO / "data" / "snapshots" / "7d48dcb.meta.json").read_text(encoding="utf-8")
)
# 锁定快照白名单（N6：评测与在线执行同源；data/snapshots/README.md 口径）
_SNAPSHOT_ALLOWED = frozenset(
    f"atlas.{ns}.{table}"
    for ns, tables in _SNAPSHOT_META["row_counts"].items()
    for table in tables
)


class _SpyConnector:
    """执行器替身：经真实 `Connector.executor()` 装配接缝注入（无 test-only 旁路）。"""

    def __init__(
        self,
        *,
        rows: list[tuple[Any, ...]] | None = None,
        columns: list[str] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._rows = rows if rows is not None else [(Decimal("1500.00"),)]
        self._columns = columns if columns is not None else ["commission_revenue"]
        self._exc = exc

    @property
    def spec(self) -> Any:
        return _spec()

    @property
    def capabilities(self) -> Any:
        from agent.runtime.connectors.base import ConnectorCapabilities

        return ConnectorCapabilities(
            dialect="doris",
            read_only=True,
            metadata_probe=False,
            cancel_query=False,
            snapshot_read=False,
            consistent_analysis=False,
        )

    def executor(self) -> Any:
        """装配接缝：返回记录调用的执行器（与 DorisConnector.executor 同形）。"""

        def _execute(sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
            self.calls.append(sql)
            if self._exc is not None:
                raise self._exc
            return [tuple(r) for r in self._rows], list(self._columns)

        return _execute


@pytest.fixture
def executor_spy() -> _SpyConnector:
    """连接器接缝替身（默认返回单行结果集）。"""
    return _SpyConnector()


def _budget(*, exclude: set[str] | frozenset[str] = frozenset()) -> Any:
    """锁定快照白名单预算；exclude 模拟源合同未覆盖（schema 漂移）的表。"""
    from agent.security.sql_guard import Budget

    return Budget(dialect="doris", max_rows=10_000, allowed_tables=_SNAPSHOT_ALLOWED - exclude)


def _context(
    executor: Any,
    *,
    budget: Any = None,
    identity: Any = None,
    compiler: Any = None,
) -> Any:
    """按正式 RunContext 合同装配（缺省 = 完整快照白名单 + 金融语义编译器）。"""
    from agent.compiler import Compiler, SemanticModel
    from agent.runtime.context import RunContext

    return RunContext(
        budget=budget if budget is not None else _budget(),
        executor=executor,
        identity=identity,
        compiler=compiler if compiler is not None else Compiler(SemanticModel()),
    )


def _finance_plan() -> Any:
    """gold-102 口径的注册计划：Branch 维度 → 编译 SQL join dim_broker。"""
    from agent.compiler import Plan, TimeSpec

    return Plan(
        metric="commission_revenue",
        dimensions=("Branch",),
        time=TimeSpec("year", 2013),
        limit=5,
    )


@pytest.fixture
def run_context(executor_spy: _SpyConnector) -> Any:
    """正式 RunContext 合同（账本 T04 口径；白名单模拟 schema 漂移）。

    漂移集：`dim_broker` 尚未进入源快照白名单（语义模型已引用）——账本红测
    「非法表 → 数据库完全不被调用」即此场景；完整白名单的基准装配见
    `_context(...)` 缺省值。
    """
    return _context(executor_spy.executor(), budget=_budget(exclude={"atlas.dwd.dim_broker"}))


@pytest.fixture
def unsafe_plan() -> Any:
    """可编译、但查询表不被当前源合同覆盖的计划（红测的「非法表」）。"""
    return _finance_plan()


# ---------------------------------------------------------------------------
# T04c：执行内核（compile → 策略 → Guard → 执行 → 校验）
# ---------------------------------------------------------------------------


def test_executor_spy_satisfies_connector_protocol(executor_spy: _SpyConnector) -> None:
    """接缝可信：替身与 DorisConnector 走同一连接器协议（executor() 唯一入口）。"""
    from agent.runtime.connectors.base import Connector

    assert isinstance(executor_spy, Connector)
    assert callable(executor_spy.executor())


def test_invalid_plan_never_reaches_source(
    unsafe_plan: Any, run_context: Any, executor_spy: _SpyConnector
) -> None:
    """账本红测：非法表（源合同未覆盖）→ blocked，数据库完全不被调用。"""
    from agent.runtime.execution import execute_plan

    result = execute_plan(unsafe_plan, run_context)

    assert result.kind == "blocked"
    assert executor_spy.calls == []
    assert result.sql is None
    assert result.executed is False
    assert "表不在白名单内" in (result.block_reason or "")


def test_blocked_result_carries_no_rejected_sql(run_context: Any) -> None:
    """失败 SQL 裁剪：blocked 只给拒绝类型与原因，不回显被拒 SQL（N3）。"""
    from agent.runtime.execution import execute_plan

    result = execute_plan(_finance_plan(), run_context)

    assert (result.block_reason or "").startswith("UnsafeQuery")
    assert "SELECT" not in (result.block_reason or "")
    assert result.row_count == 0
    assert result.validation_issues == ()


def test_uncompilable_plan_never_reaches_source(executor_spy: _SpyConnector) -> None:
    """直接 Plan 校验：未注册 metric → kind=error、零触库、不携带 SQL。"""
    from agent.compiler import Plan
    from agent.runtime.execution import execute_plan

    result = execute_plan(Plan(metric="not_a_registered_metric"), _context(executor_spy.executor()))

    assert result.kind == "error"
    assert "CompileError" in (result.error or "")
    assert result.sql is None
    assert result.executed is False
    assert executor_spy.calls == []


def test_cross_domain_identity_rejected_without_touching_source(
    executor_spy: _SpyConnector,
) -> None:
    """跨域拒绝：零售角色（region_manager）解析金融策略 → error，零触库。"""
    from agent.runtime.execution import execute_plan

    ctx = _context(
        executor_spy.executor(),
        identity={"sub": "u-region", "role": "region_manager", "user_context": {"region": "TN"}},
    )
    result = execute_plan(_finance_plan(), ctx)

    assert result.kind == "error"
    assert "身份策略解析失败" in (result.error or "")
    assert result.sql is None
    assert executor_spy.calls == []


def test_registered_identity_injects_policy_and_effect(executor_spy: _SpyConnector) -> None:
    """hq_admin：谓词注入执行 SQL；生效句只含角色 + 策略名（不外泄条件值）。"""
    from agent.runtime.execution import execute_plan

    ctx = _context(
        executor_spy.executor(),
        identity={"sub": "u-hq", "role": "hq_admin", "user_context": {}},
    )
    result = execute_plan(_finance_plan(), ctx)

    assert result.kind == "ok"
    assert "1 = 1" in executor_spy.calls[0]
    assert result.policy_effect == "行级策略已生效（角色 hq_admin，策略 rp_branch_visible）"


def test_branch_manager_predicate_reaches_sql_without_value_leak(
    executor_spy: _SpyConnector,
) -> None:
    """branch_manager{east}：SQL 含分支谓词；生效句不含条件值（0011 口径）。"""
    from agent.runtime.execution import execute_plan

    ctx = _context(
        executor_spy.executor(),
        identity={"sub": "u-bm", "role": "branch_manager", "user_context": {"branch": "east"}},
    )
    result = execute_plan(_finance_plan(), ctx)

    assert result.kind == "ok"
    assert "= 'east'" in executor_spy.calls[0]
    assert "branch_manager" in (result.policy_effect or "")
    assert "east" not in (result.policy_effect or "")


def test_executor_failure_keeps_sql_as_execution_evidence() -> None:
    """执行后失败（ADR-0026 决策 ③）：sql/executed/latency 作为已执行证据。"""
    from agent.runtime.execution import execute_plan

    spy = _SpyConnector(exc=RuntimeError("doris connection reset"))
    result = execute_plan(_finance_plan(), _context(spy.executor()))

    assert result.kind == "error"
    assert "doris connection reset" in (result.error or "")
    assert result.sql is not None and "atlas.dwd.fact_trades" in result.sql
    assert result.executed is True
    assert result.latency_ms is not None
    assert len(spy.calls) == 1


def test_missing_executor_fails_closed(executor_spy: _SpyConnector) -> None:
    """executor=None（无可用数据源）→ error；不回退到任何全局连接（fail-closed）。"""
    from agent.runtime.execution import execute_plan

    result = execute_plan(_finance_plan(), _context(None))

    assert result.kind == "error"
    assert "executor" in (result.error or "")
    assert result.sql is None
    assert executor_spy.calls == []


def test_missing_compiler_fails_closed(executor_spy: _SpyConnector) -> None:
    """compiler=None → error；绝不猜测编译（fail-closed）。"""
    from dataclasses import replace

    from agent.runtime.execution import execute_plan

    ctx = replace(_context(executor_spy.executor()), compiler=None)
    result = execute_plan(_finance_plan(), ctx)

    assert result.kind == "error"
    assert "compiler" in (result.error or "")
    assert executor_spy.calls == []


def test_success_returns_validated_result(executor_spy: _SpyConnector) -> None:
    """成功路径：ok + 结果集 + 校验注记 + 耗时；执行器恰好被调用一次。"""
    from agent.runtime.execution import execute_plan

    result = execute_plan(_finance_plan(), _context(executor_spy.executor()))

    assert result.kind == "ok"
    assert result.executed is True
    assert result.sql is not None and "LIMIT 5" in result.sql
    assert result.columns == ("commission_revenue",)
    assert result.rows == ((Decimal("1500.00"),),)
    assert result.row_count == 1
    assert result.validation_issues == ()
    assert result.time_column is None  # plain 计划无时间轴列（ADR-0025 决策 ①3）
    assert result.latency_ms is not None
    assert len(executor_spy.calls) == 1


def test_empty_result_is_reported_not_hidden() -> None:
    """校验注记如实透传（execution_validator 口径）：空结果 → empty_result。"""
    from agent.runtime.execution import execute_plan

    spy = _SpyConnector(rows=[], columns=["commission_revenue"])
    result = execute_plan(_finance_plan(), _context(spy.executor()))

    assert result.kind == "ok"
    assert result.validation_issues == ("empty_result",)
    assert result.row_count == 0


def test_guarded_sql_entry_shares_the_same_kernel(executor_spy: _SpyConnector) -> None:
    """SQL 级入口（工具注册表委托）：Guard 通过 → 执行；拒绝 → blocked 零触库。"""
    from agent.runtime.execution import execute_guarded_sql

    ctx = _context(executor_spy.executor())
    ok = execute_guarded_sql("SELECT COUNT(*) AS n FROM atlas.dwd.fact_trades LIMIT 5", ctx)
    assert ok.kind == "ok"
    assert ok.sql is not None
    assert len(executor_spy.calls) == 1

    blocked = execute_guarded_sql("SELECT COUNT(*) AS n FROM atlas.dwd.unlisted_table LIMIT 5", ctx)
    assert blocked.kind == "blocked"
    assert blocked.sql is None
    assert len(executor_spy.calls) == 1  # 被拒 SQL 未触库


# ---------------------------------------------------------------------------
# T04d：在线执行入口委托共享内核（结构锁定；行为面无法区分「各写一份」与
# 「委托同一内核」——两种实现的对外行为逐字相同，只有源码结构能锁死这条
# 架构约束，同 test_identity 判据 5(c) 的补偿口径：make test 不跑 mypy/架构检查）
# ---------------------------------------------------------------------------


def _imported_names(rel: str, module: str) -> set[str]:
    """模块 `rel` 从 `module` 导入的名字集合（AST 解析，多行/函数内 import 准确）。"""
    tree = ast.parse((_REPO / rel).read_text(encoding="utf-8"))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == module
        for alias in node.names
    }


def test_online_entrypoints_do_not_import_guard_enforce() -> None:
    """graph/registry 不得自行调用 Guard——执行只能经共享内核（N3 单通道）。"""
    for rel in ("agent/graph.py", "agent/tools/registry.py"):
        assert "enforce" not in _imported_names(rel, "agent.security.sql_guard"), (
            f"{rel} 不得导入 Guard.enforce：执行链必须委托 agent/runtime/execution"
        )


def test_online_entrypoints_delegate_to_shared_kernel() -> None:
    """graph 经 execute_plan、registry 经 execute_guarded_sql 进入同一内核。"""
    assert "execute_plan" in _imported_names("agent/graph.py", "agent.runtime.execution")
    assert "execute_guarded_sql" in _imported_names(
        "agent/tools/registry.py", "agent.runtime.execution"
    )


def test_graph_delegates_result_validation_to_kernel() -> None:
    """node_execute 不再自持 ExecutionValidator（校验随执行链一起进内核）。"""
    assert "ExecutionValidator" not in (_REPO / "agent" / "graph.py").read_text(
        encoding="utf-8"
    )


def test_online_factories_do_not_reverse_depend_on_eval_runner() -> None:
    """factory/cli 不得从评测模块导入执行/预算（T04：评测不再是执行工厂）。"""
    for rel in ("agent/factory.py", "agent/cli.py"):
        assert not _imported_names(rel, "eval.runner"), (
            f"{rel} 不得反向依赖 eval.runner：执行与预算导入自 agent.runtime"
        )


if __name__ == "__main__":
    unittest.main()
