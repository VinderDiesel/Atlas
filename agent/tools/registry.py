"""确定性工具注册表（Day 44）：list_metrics / describe_metric / compile_sql /
execute_readonly 四件套（Day 45 以 MCP 风格暴露，本层是参数校验后的真身）。

定位与口径
----------
- **确定性**：全部工具不经过 LLM；同一入参 → 同一输出（数据来自语义层 YAML
  与 Compiler/Guard，均确定性组件）。
- **执行必须过 Guard（N3 红线）**：execute_readonly 收到的是不可信入参（未来
  由 LLM/MCP 调用方提供），内部先 enforce（只读 + 函数黑名单 + 表白名单 +
  LIMIT 注入）再执行；被拒信息不含被拒 SQL（与 agent/graph.py 口径一致）。
- **参数校验在真身处**：Day 45 强调"工具描述不可信，需参数校验"——描述由
  暴露层生成（可能被诱导/伪造），本层以白名单键 + 类型 + 取值域校验兜底，
  未知键一律拒绝（不静默忽略，防拼写漂移）。
- **输出 JSON 可序列化**（rows 转 list）：供 MCP 层/调用方直接消费。
- **filters 与 Planner 同能力（ADR-0014 ①）**：形态为
  `[{"column", "op", "value"}, ...]`，op 限 = != < <= > >=；column 必须是已注册
  字段或本 Plan 的 metric 名（后者为度量阈值，编译为 HAVING）。结构非法即拒，
  **不接受任何嵌套对象/裸 SQL 片段**（参数化进 AST，不拼接字符串）。

已知边界（MVP，诚实声明）
------------------------
- describe_metric 返回**表达式定义层**信息（口径表达式/同义词/owner），不含
  物理表清单——表由 Compile/执行 SQL 展开（Day 46 explain 归因展示）。
- compile_sql 输出**纯净查询**（Compile 语义，同 agent/compiler.py）：
  LIMIT 等 Guard 注入发生在执行环节（enforce），不在编译层。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent.compiler import Compiler, Filter, OrderSpec, Plan, SemanticModel, TimeSpec
from agent.security.sql_guard import Budget, BudgetExceeded, UnsafeQuery, enforce

# 执行器同构约定（与 eval/runner.execute_sql / agent/graph.py 一致）
Executor = Callable[[str], tuple[list[tuple[Any, ...]], list[str]]]

# compile_sql 支持的时间粒度（TimeSpec 固定集合；列映射由模型 time_dimension
# 声明驱动，见 agent/compiler.py；拒绝未知粒度）
_GRANULARITIES = ("year", "quarter", "month", "date")
# compile_sql 白名单键（filters 已开放，形态校验见 _filters）
_PLAN_KEYS = frozenset({"metric", "dimensions", "time", "filters", "order_by", "limit"})
# 允许的过滤操作符（与 agent/compiler.py._compare 的 ops 表逐字一致；
# 不在此集内 compiler 会拒，工具层提前拒是为了给出可读参数错误）
_FILTER_OPS = ("=", "!=", "<", "<=", ">", ">=")
_MAX_LIMIT = 1000  # 上限与 Guard max_rows 数量级一致（10_000 的 1/10）


class ToolError(Exception):
    """工具调用失败（参数校验拒绝 / Guard 拒绝 / 执行故障），message 即原因。

    与 SQL 安全相关的错误**不携带被拒 SQL**（纵深防御，见模块 docstring）。
    """


@dataclass
class _CallLog:
    """工具调用留痕（审计：谁在什么时点调了什么，参数摘要）。"""

    tool: str
    args: dict[str, Any]
    ok: bool
    ms: float


@dataclass
class DeterministicTools:
    """确定性工具注册表：语义目录查询 + 编译 + 只读执行（依赖注入）。

    参数
    ----
    model    : 语义模型（目录与编译的事实源）。
    executor : 只读执行器（必填；仅 execute_readonly 消费，SQL 已过 Guard）。
    budget   : Guard 预算（必填；表白名单 = 锁定快照，见 AGENTS.md N3）。

    异常
    ----
    ValueError：executor 或 budget 未提供（本注册表不执行未过 Guard 的 SQL）。
    """

    model: SemanticModel
    executor: Executor
    budget: Budget
    calls: list[_CallLog] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.executor is None or self.budget is None:
            raise ValueError("executor 与 budget 必填：SQL 不过 Guard 不执行")

    # -- 目录查询 ----------------------------------------------------------

    def list_metrics(self) -> dict[str, Any]:
        """已注册指标目录（按名称排序，确定性输出）。

        返回
        ----
        {"metrics": [{"name", "description", "owner", "synonym_count"}, ...],
         "count": N}
        """
        rows = [
            {
                "name": name,
                "description": self.model.metric_descriptions.get(name, ""),
                "owner": self.model.metric_owners.get(name, ""),
                "synonym_count": len(self.model.metric_synonyms.get(name, ())),
            }
            for name in sorted(self.model.metrics)
        ]
        return self._logged("list_metrics", {}, {"metrics": rows, "count": len(rows)})

    def describe_metric(self, metric: str) -> dict[str, Any]:
        """单个指标口径详情（表达式定义层；表清单见 compile/执行 SQL）。

        参数
        ----
        metric : 已注册指标名（大小写敏感，见 list_metrics）。

        返回
        ----
        {"name", "description", "expression", "owner", "synonyms": [...]}

        异常
        ----
        ToolError：参数非 str 或指标不存在。
        """
        if not isinstance(metric, str):
            raise ToolError(f"参数校验失败：metric 必须是字符串，收到 {type(metric).__name__}")
        expression = self.model.metrics.get(metric)
        if expression is None:
            raise ToolError(
                f"指标不存在：{metric}（可用：{', '.join(sorted(self.model.metrics))}）"
            )
        return self._logged(
            "describe_metric",
            {"metric": metric},
            {
                "name": metric,
                "description": self.model.metric_descriptions.get(metric, ""),
                "expression": expression,
                "owner": self.model.metric_owners.get(metric, ""),
                "synonyms": list(self.model.metric_synonyms.get(metric, ())),
            },
        )

    # -- 编译 --------------------------------------------------------------

    def compile_sql(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Plan(dict) → 只读 SQL（确定性编译，不经过 LLM/Guard 注入）。

        参数
        ----
        plan : {"metric": str,                       # 必填，已注册指标
                "dimensions": [str, ...],            # 可缺省；维度字段名
                "time": {"granularity": str,         # 可缺省；year/quarter/
                        "value": int | str},         #   month/date（见 Compiler）
                "filters": [{"column": str,          # 可缺省；维度过滤→WHERE，
                             "op": str,              #   column==metric 时→HAVING；
                             "value": int|float|str}],  #   op 见 _FILTER_OPS
                "order_by": [{"column": str,         # 可缺省；metric 或维度名
                              "desc": bool}],        #   desc 缺省 False
                "limit": int}                        # 可缺省；1..1000，默认 100

        返回
        ----
        {"sql": str, "join_chain": [str, ...]}（join_chain 为 join 说明）

        异常
        ----
        ToolError：白名单外键 / 类型 / 取值域不符，或编译失败。
        """
        unknown = set(plan) - _PLAN_KEYS
        if unknown:
            raise ToolError(
                f"参数校验失败：不支持的键 {sorted(unknown)}（允许：{sorted(_PLAN_KEYS)}）"
            )
        metric = self._require_metric(plan)
        try:
            compiled = Plan(
                metric=metric,
                dimensions=self._dimensions(plan),
                time=self._time(plan),
                filters=self._filters(plan, metric),
                order_by=self._order_by(plan),
                limit=self._limit(plan),
            )
            sql, notes = Compiler(self.model).compile(compiled)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - 编译失败原因要完整转述
            raise ToolError(f"编译失败：{exc}") from exc
        return self._logged(
            "compile_sql",
            {
                "metric": compiled.metric,
                "limit": compiled.limit,
                # 审计可见：带几条过滤（不带值，避免客户维度值落进调用留痕）
                "filters": len(compiled.filters),
            },
            {"sql": sql, "join_chain": list(notes)},
        )

    def _require_metric(self, plan: dict[str, Any]) -> str:
        metric = plan.get("metric")
        if not isinstance(metric, str) or metric not in self.model.metrics:
            raise ToolError(
                f"参数校验失败：metric 必须是已注册指标"
                f"（可用：{', '.join(sorted(self.model.metrics))}）"
            )
        return metric

    def _dimensions(self, plan: dict[str, Any]) -> tuple[str, ...]:
        dims = plan.get("dimensions", ())
        if dims is None:
            return ()
        if not isinstance(dims, (list, tuple)) or not all(isinstance(d, str) for d in dims):
            raise ToolError("参数校验失败：dimensions 必须是字符串数组")
        for dim in dims:
            if self.model.find_field(dim) is None:
                raise ToolError(f"参数校验失败：维度字段不存在：{dim}")
        return tuple(dims)

    def _time(self, plan: dict[str, Any]) -> TimeSpec | None:
        spec = plan.get("time")
        if spec is None:
            return None
        if not isinstance(spec, dict) or set(spec) != {"granularity", "value"}:
            raise ToolError('参数校验失败：time 必须是 {"granularity": str, "value": int|str}')
        granularity = spec["granularity"]
        value = spec["value"]
        if granularity not in _GRANULARITIES:
            raise ToolError(f"参数校验失败：time.granularity 必须是 {_GRANULARITIES}")
        if not isinstance(value, (int, str)) or (isinstance(value, str) and not value.strip()):
            raise ToolError("参数校验失败：time.value 必须是 int 或非空 str")
        return TimeSpec(granularity, value)

    def _filters(self, plan: dict[str, Any], metric: str) -> tuple[Filter, ...]:
        """filters 形态校验（结构白名单，不接受裸 SQL 片段）。

        column 允许两类：语义层已注册字段（→ WHERE 维度过滤），或等于本 Plan 的
        metric 名（→ HAVING 度量阈值，compiler 按 column==metric 判定，见
        agent/compiler.py 拆分规则）。value 仅标量（int/float/非空 str）：嵌套对象
        会被 compiler 转成字面量字符串，属静默错口径，此处直接拒。
        """
        items = plan.get("filters", ())
        if items is None:
            return ()
        if not isinstance(items, (list, tuple)):
            raise ToolError("参数校验失败：filters 必须是对象数组")
        out: list[Filter] = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {"column", "op", "value"}:
                raise ToolError(
                    "参数校验失败：filters 元素必须恰为 "
                    '{"column": str, "op": str, "value": 标量}'
                )
            column, op, value = item["column"], item["op"], item["value"]
            if not isinstance(column, str) or not column.strip():
                raise ToolError("参数校验失败：filters.column 必须是非空字符串")
            if column != metric and self.model.find_field(column) is None:
                raise ToolError(
                    f"参数校验失败：filters.column 必须是已注册字段或本 Plan 的指标名：{column}"
                )
            if op not in _FILTER_OPS:
                raise ToolError(f"参数校验失败：filters.op 必须是 {_FILTER_OPS}，收到 {op!r}")
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ToolError("参数校验失败：filters.value 必须是数字或非空字符串")
            if isinstance(value, str) and not value.strip():
                raise ToolError("参数校验失败：filters.value 不能是空字符串")
            out.append(Filter(column, op, value))
        return tuple(out)

    def _order_by(self, plan: dict[str, Any]) -> tuple[OrderSpec, ...]:
        specs = plan.get("order_by", ())
        if specs is None:
            return ()
        if not isinstance(specs, (list, tuple)):
            raise ToolError("参数校验失败：order_by 必须是对象数组")
        out: list[OrderSpec] = []
        for item in specs:
            if not isinstance(item, dict) or "column" not in item:
                raise ToolError('参数校验失败：order_by 元素必须是 {"column": str, ...}')
            if not isinstance(item["column"], str) or not item["column"].strip():
                raise ToolError("参数校验失败：order_by.column 必须是非空字符串")
            desc = item.get("desc", False)
            if not isinstance(desc, bool):
                raise ToolError("参数校验失败：order_by.desc 必须是布尔值")
            out.append(OrderSpec(item["column"], desc=desc))
        return tuple(out)

    def _limit(self, plan: dict[str, Any]) -> int:
        limit = plan.get("limit", 100)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_LIMIT:
            raise ToolError(f"参数校验失败：limit 必须是 1..{_MAX_LIMIT} 的整数")
        return limit

    # -- 执行（N3：先 Guard 后执行，被拒信息不携带 SQL） -------------------

    def execute_readonly(self, sql: str) -> dict[str, Any]:
        """只读执行：Guard(enforce) 通过后调用注入的执行器。

        参数
        ----
        sql : 任意 SQL（调用方不可信）；只读/黑名单/表白名单/LIMIT 由 Guard
              强制，通过才执行。

        返回
        ----
        {"sql": guarded_sql, "rows": [[...], ...], "columns": [str, ...],
         "row_count": int, "latency_ms": float}

        异常
        ----
        ToolError：Guard 拒绝（类型+原因，无被拒 SQL）或执行器故障。
        """
        if not isinstance(sql, str) or not sql.strip():
            raise ToolError("参数校验失败：sql 必须是非空字符串")
        try:
            guarded, _ = enforce(sql, budget=self.budget)
        except (UnsafeQuery, BudgetExceeded) as exc:
            raise ToolError(f"只读校验拒绝（{type(exc).__name__}）：{exc}") from exc
        started = time.perf_counter()
        try:
            rows, columns = self.executor(guarded)
        except Exception as exc:  # noqa: BLE001 - 执行故障要完整转述
            raise ToolError(f"执行失败（{type(exc).__name__}）：{exc}") from exc
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        return self._logged(
            "execute_readonly",
            {"sql": guarded},
            {
                "sql": guarded,
                "rows": [list(r) for r in rows],
                "columns": list(columns),
                "row_count": len(rows),
                "latency_ms": latency_ms,
            },
        )

    # -- 留痕 --------------------------------------------------------------

    def _logged(self, tool: str, args: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(_CallLog(tool=tool, args=dict(args), ok=True, ms=0.0))
        return result
