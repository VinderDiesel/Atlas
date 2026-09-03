"""确定性图表（Day 47）：从**已执行结果**渲染柱状/折线/表格，不发明 schema。

设计（任务书：先校验行数与数值范围再渲染；schema 必须来自已执行结果）
------------------------------------------------------------------------
- **只吃执行产物**：render_chart 接受带 sql/rows/columns 的对象（TurnResult
  或 execute_readonly 输出）。sql 是 Guard 出口 SQL（已执行）——没有已执行
  SQL 引用的裸数据一律拒绝（防"手工编造结果集"的幻觉图表）。
- **列名零发明**：x/y/columns 只能来自执行结果 columns；数值列/维度列按
  值类型确定性分类（全部数值 → 数值列；含非数值 → 维度列；全 NULL → 弃用）。
- **数值范围校验**：y 值出现 NaN/Inf 直接拒绝渲染（坏数不画）；全 NULL 列
  不参与选轴。
- **行数校验**：空结果拒绝渲染；超过 MAX_CATEGORIES 的密集结果降级为表格
  并如实注记（不丢数据，只是不画误导性密集图）。
- **图表类型确定性规则**：有数值列 → 柱状；x 为时间列（dim_date 物理列或
  列名含 date/year/quarter/month 等时间词）→ 折线；无数值列 → 表格。
- **可审计**：输出带 sql_sha256（执行 SQL 摘要），schema 可溯源到具体 SQL。

已知边界（MVP，诚实声明）
------------------------
- 多数值列只渲染第一个（note 注明），不做多轴/堆叠组合。
- 折线不做排序/插值，按行序连线；含 NULL 的点保留为空（不补 0 不插值）。
- 返回结构化 spec（数据即已执行 rows 的子集），不做像素渲染；前端渲染是
  serving 层职责。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from numbers import Number
from typing import Any, Protocol

# 图表类目上限：超出后密集结果降级表格（行数校验的一部分）
MAX_CATEGORIES = 200
# 表格降级时的最大展示行数（截断并注记，不静默丢数据）
MAX_TABLE_ROWS = 500

# 时间列识别（dim_date 物理列约定，见 agent/compiler.py TIME_COLUMNS）
_TIME_COLUMN_NAMES = frozenset(
    {"CalendarYearID", "CalendarQtrID", "CalendarMonthID", "DateValue", "date", "time"}
)


class ChartError(Exception):
    """图表渲染被拒绝（空结果 / 无执行 SQL / 坏数值 / 行列不一致）。"""


class ExecutionLike(Protocol):
    """已执行结果的只读形状（TurnResult 与 execute_readonly 输出都满足）。

    成员声明为只读 property：frozen dataclass（TurnResult）字段不可写，
    非只读的 Protocol 注解会与实现方冲突（mypy strict 实测）。
    """

    @property
    def sql(self) -> str | None: ...

    @property
    def rows(self) -> tuple[tuple[Any, ...], ...] | list[list[Any]]: ...

    @property
    def columns(self) -> tuple[str, ...] | list[str]: ...


@dataclass(frozen=True)
class _ColumnStats:
    """一列的确定性分类结果（渲染决策只依据这些事实）。"""

    name: str
    numeric: bool  # 非 NULL 值全部是有限数值
    all_null: bool  # 整列 NULL（不可选作轴）
    time_like: bool  # 列名命中时间列约定（折线候选）


def _is_numeric(value: Any) -> bool:
    """数值判定（含 Decimal 等 numbers.Number；bool 不是数值）。

    真实执行器（Doris via mysql.connector）SUM(decimal) 返回 Decimal，
    只认 int/float 会把真数值列误判为维度（Day 48 e2e 实测 S5 退化）。
    """
    return isinstance(value, Number) and not isinstance(value, bool)


def _classify_columns(rows: list[list[Any]], columns: list[str]) -> list[_ColumnStats]:
    """按值类型确定性分类（数值/维度/全 NULL），不做任何推断。"""
    stats: list[_ColumnStats] = []
    for idx, name in enumerate(columns):
        values = [row[idx] for row in rows if idx < len(row)]
        non_null = [v for v in values if v is not None]
        if not non_null:
            stats.append(_ColumnStats(name, numeric=False, all_null=True, time_like=False))
            continue
        numeric = all(_is_numeric(v) for v in non_null)
        stats.append(
            _ColumnStats(
                name=name,
                numeric=numeric,
                all_null=False,
                time_like=name in _TIME_COLUMN_NAMES,
            )
        )
    return stats


def _is_finite(value: Any) -> bool:
    """数值是否有限（NaN/Inf/超大 Decimal 转换失败都视为非有限）。"""
    try:
        return math.isfinite(float(value))
    except (ValueError, OverflowError):
        return False


def _validate_rows(rows: list[list[Any]], columns: list[str]) -> None:
    """行/列一致性校验：列宽不齐或行为空 → 拒绝（不猜不补）。"""
    if not rows:
        raise ChartError("空结果不渲染（0 行）——不画无数据的图")
    for idx, row in enumerate(rows):
        if len(row) < len(columns):
            raise ChartError(
                f"行列不一致：第 {idx + 1} 行宽 {len(row)} < 列数 {len(columns)}"
                "（schema 必须来自已执行结果，不补齐）"
            )


def render_chart(execution: ExecutionLike) -> dict[str, Any]:
    """从已执行结果渲染确定性图表 spec。

    参数
    ----
    execution : 带 sql/rows/columns 的执行产物，支持两种形状——
                TurnResult 对象（属性访问）与 execute_readonly 输出
                （dict 键访问）；sql 必须非空，rows/columns 与执行器
                返回同构。

    返回
    ----
    图表 spec：
    - bar/line：{"type", "x", "y", "data": [{"x", "y"}...], "sql_sha256",
      "note", "skipped"}
    - table   ：{"type", "columns", "rows", "sql_sha256", "note", "skipped"}

    异常
    ----
    ChartError：无执行 SQL / 空结果 / 行宽不足 / y 含 NaN·Inf / 全 NULL。
    """
    if isinstance(execution, dict):
        # execute_readonly 输出形状（dict 键访问）
        sql = execution.get("sql")
        rows = [list(r) for r in execution.get("rows", [])]
        columns = list(execution.get("columns", []))
    else:
        # TurnResult 形状（属性访问）
        sql = execution.sql
        rows = [list(r) for r in execution.rows]
        columns = list(execution.columns)
    if not sql:
        raise ChartError("缺少已执行 SQL 引用：schema 必须来自已执行结果（不渲染裸数据）")
    _validate_rows(rows, columns)

    stats = _classify_columns(rows, columns)
    # 时间列（如 CalendarYearID 数值形态）是轴不是度量：排除出 y 候选
    numeric_cols = [s for s in stats if s.numeric and not s.all_null and not s.time_like]
    time_cols = [s for s in stats if s.time_like and not s.all_null]
    cat_cols = [s for s in stats if not s.numeric and not s.all_null and not s.time_like]
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()[:12]

    # 行数校验：超出图表类目上限 → 降级表格（截断并注记，不画密集误导图）
    if len(rows) > MAX_CATEGORIES:
        return {
            "type": "table",
            "columns": columns,
            "rows": rows[:MAX_TABLE_ROWS],
            "sql_sha256": digest,
            "note": f"行数 {len(rows)} 超过图表类目上限 {MAX_CATEGORIES}，"
            f"降级表格（展示前 {min(MAX_TABLE_ROWS, len(rows))} 行）",
            "skipped": max(len(rows) - MAX_TABLE_ROWS, 0),
        }

    # 无数值列 → 表格（原样展示，如实）
    if not numeric_cols:
        return {
            "type": "table",
            "columns": columns,
            "rows": rows[:MAX_TABLE_ROWS],
            "sql_sha256": digest,
            "note": "结果不含数值列，以表格展示",
            "skipped": max(len(rows) - MAX_TABLE_ROWS, 0),
        }

    # 选轴：x 优先时间列（折线），否则维度列（柱状）；y = 首个数值列
    y_col = numeric_cols[0]
    note_parts: list[str] = []
    if len(numeric_cols) > 1:
        others = ", ".join(s.name for s in numeric_cols[1:])
        note_parts.append(f"多数值列仅渲染 {y_col.name}（未渲染：{others}）")
    if time_cols:
        x_col = time_cols[0]
        chart_type = "line"
        if len(time_cols) > 1:
            note_parts.append(f"多个时间列，取首个 {x_col.name} 作 x 轴")
    elif cat_cols:
        x_col = cat_cols[0]
        chart_type = "bar"
        if len(cat_cols) > 1:
            note_parts.append(f"多个候选维度列，取首个 {x_col.name} 作 x 轴")
    else:
        # 只有数值列：无维度轴 → 单序列展示（x 用行序，如实注记）
        x_col = None
        chart_type = "bar"
        note_parts.append("无维度列，x 轴按行序 1..N")
    x_idx = columns.index(x_col.name) if x_col else None
    y_idx = columns.index(y_col.name)

    # 数值范围校验（画图前最后一关）：被选中数值轴的值必须有限（NaN/Inf 拒绝）。
    # 只检查数值类型值：维度/时间列里的字符串不是数值范围，不参与本校验。
    for col_idx, col_name in ((x_idx, x_col.name if x_col else ""), (y_idx, y_col.name)):
        if col_idx is None:
            continue
        for row in rows:
            value = row[col_idx]
            if value is not None and _is_numeric(value) and not _is_finite(value):
                raise ChartError(f"数值范围校验失败：列 {col_name} 含非有限值 {value!r}，拒绝渲染")

    data = [
        {
            "x": (row[x_idx] if x_idx is not None else idx + 1),
            "y": row[y_idx],
        }
        for idx, row in enumerate(rows)
    ]
    spec: dict[str, Any] = {
        "type": chart_type,
        "x": x_col.name if x_col else "(行序)",
        "y": [y_col.name],
        "data": data,
        "sql_sha256": digest,
        "skipped": 0,
    }
    if note_parts:
        spec["note"] = "；".join(note_parts)
    return spec
