"""确定性图表契约测试（Day 47）：防幻觉 schema（只渲染已执行结果）。

口径（与 agent/tools/chart.py docstring 一致）：
- schema 必须来自已执行结果：无 sql 引用的裸数据拒绝（不画编造的图）；
- x/y/columns 零发明：全部来自执行结果 columns（断言 spec 不出现输入外的列名）；
- 数值范围：NaN/Inf/全 NULL 不渲染；行列不一致拒绝；空结果拒绝；
- 行数校验：超类目上限降级表格并注记（不丢数据不画误导图）；
- 类型确定性：时间列（含数值形态的 CalendarYearID）作 x → 折线；维度+数值
  → 柱状；无数值列 → 表格；多数值列只画第一个并注记。
"""

from __future__ import annotations

import hashlib
import json
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any

from agent.compiler import SemanticModel
from agent.graph import DataAgent
from agent.security.sql_guard import Budget
from agent.tools.chart import ChartError, render_chart

REPO = Path(__file__).resolve().parent.parent

_META = json.loads((REPO / "data/snapshots" / "7d48dcb.meta.json").read_text(encoding="utf-8"))
ALLOWED = frozenset(
    f"atlas.{ns}.{table}" for ns, tables in _META["row_counts"].items() for table in tables
)
BUDGET = Budget(dialect="doris", max_rows=10_000, allowed_tables=ALLOWED)
MODEL = SemanticModel()

SQL = "SELECT Branch, SUM(Commission) FROM atlas.dwd.fact_trades GROUP BY Branch LIMIT 5"


def execution(rows: list[tuple[Any, ...]], columns: list[str], sql: str = SQL):
    """构造已执行结果形状（同 execute_readonly 输出 / TurnResult 字段）。"""
    return {"sql": sql, "rows": rows, "columns": columns}


class TestChartTypeSelection(unittest.TestCase):
    def test_dimension_numeric_bar(self) -> None:
        spec = render_chart(
            execution(
                [("华中", 123.0), ("华东", 98.5)],
                ["Branch", "commission_revenue"],
            )
        )
        self.assertEqual(spec["type"], "bar")
        self.assertEqual(spec["x"], "Branch")
        self.assertEqual(spec["y"], ["commission_revenue"])
        self.assertEqual(spec["data"], [{"x": "华中", "y": 123.0}, {"x": "华东", "y": 98.5}])

    def test_time_column_line_even_numeric(self) -> None:
        """时间列是 int 形态（年份）也要作 x 轴 → 折线（不当度量画柱）。"""
        spec = render_chart(
            execution(
                [(2013, 123.0), (2014, 156.0)],
                ["CalendarYearID", "commission_revenue"],
            )
        )
        self.assertEqual(spec["type"], "line")
        self.assertEqual(spec["x"], "CalendarYearID")
        self.assertEqual(spec["data"][0], {"x": 2013, "y": 123.0})

    def test_no_numeric_column_table(self) -> None:
        spec = render_chart(
            execution(
                [("华中", "A"), ("华东", "B")],
                ["Branch", "Tier"],
            )
        )
        self.assertEqual(spec["type"], "table")
        self.assertEqual(spec["columns"], ["Branch", "Tier"])
        self.assertEqual(len(spec["rows"]), 2)
        self.assertIn("结果不含数值列", str(spec["note"]))

    def test_numeric_only_row_index_x(self) -> None:
        spec = render_chart(execution([(123.0,), (98.5,)], ["commission_revenue"]))
        self.assertEqual(spec["type"], "bar")
        self.assertEqual(spec["x"], "(行序)")
        self.assertEqual(spec["data"][0], {"x": 1, "y": 123.0})
        self.assertIn("行序", str(spec["note"]))

    def test_multi_numeric_renders_first_with_note(self) -> None:
        spec = render_chart(
            execution(
                [("华中", 1.0, 2.0)],
                ["Branch", "a", "b"],
            )
        )
        self.assertEqual(spec["y"], ["a"])
        self.assertIn("未渲染：b", str(spec["note"]))


class TestAntiHallucination(unittest.TestCase):
    """防幻觉：schema 必须来自已执行结果（Day 47 验收核心）。"""

    def test_no_sql_rejected(self) -> None:
        """裸数据（无已执行 SQL 引用）→ 拒绝（不渲染可能被编造的结果）。"""
        with self.assertRaises(ChartError) as ctx:
            render_chart(execution([("华中", 1.0)], ["Branch", "v"], sql=""))
        self.assertIn("缺少已执行 SQL", str(ctx.exception))

    def test_all_axes_from_executed_columns(self) -> None:
        spec = render_chart(
            execution([("华中", 1.0)], ["Branch", "commission_revenue"], sql="SELECT 1")
        )
        rendered = {spec["x"], *spec["y"], *spec.get("columns", [])}
        self.assertLessEqual(rendered, {"Branch", "commission_revenue"}, "轴名零发明")

    def test_sql_sha256_binds_schema(self) -> None:
        spec1 = render_chart(execution([("a", 1.0)], ["B", "v"], sql="SELECT 1"))
        spec2 = render_chart(execution([("a", 1.0)], ["B", "v"], sql="SELECT 2"))
        self.assertEqual(spec1["sql_sha256"], hashlib.sha256(b"SELECT 1").hexdigest()[:12])
        self.assertNotEqual(spec1["sql_sha256"], spec2["sql_sha256"], "同数据不同 SQL 必须可区分")

    def test_nan_inf_rejected(self) -> None:
        bad_rows = [("华中", float("nan")), ("华东", 1.0)]
        with self.assertRaises(ChartError) as ctx:
            render_chart(execution(bad_rows, ["Branch", "v"]))
        self.assertIn("非有限值", str(ctx.exception))
        with self.assertRaises(ChartError):
            render_chart(execution([("华东", float("inf"))], ["Branch", "v"]))

    def test_row_width_mismatch_rejected(self) -> None:
        with self.assertRaises(ChartError) as ctx:
            render_chart(execution([("华东", 1.0)], ["Branch", "v", "extra"]))
        self.assertIn("行列不一致", str(ctx.exception))

    def test_empty_rows_rejected(self) -> None:
        with self.assertRaises(ChartError):
            render_chart(execution([], ["Branch", "v"]))

    def test_all_null_column_not_picked(self) -> None:
        """全 NULL 度量列不参与选轴（不画空列）；另一数值列顶上。"""
        spec = render_chart(
            execution(
                [("华中", None, 1.0), ("华东", None, 2.0)],
                ["Branch", "null_col", "real_col"],
            )
        )
        self.assertEqual(spec["y"], ["real_col"])

    def test_decimal_values_are_numeric(self) -> None:
        """真实执行器（Doris）SUM(decimal) 返回 Decimal——必须作数值列渲染。

        Day 48 e2e 实测：只认 int/float 会把 Decimal 列误判为维度列 →
        无数值列 → 错误降级表格。
        """
        spec = render_chart(
            execution(
                [("华中", Decimal("372849.61")), ("华东", Decimal("98765.43"))],
                ["Branch", "commission_revenue"],
            )
        )
        self.assertEqual(spec["type"], "bar")
        self.assertEqual(spec["y"], ["commission_revenue"])
        self.assertEqual(spec["data"][0], {"x": "华中", "y": Decimal("372849.61")})


class TestRowLimitDegradation(unittest.TestCase):
    def test_dense_result_degrades_to_table(self) -> None:
        rows = [(f"b{i}", float(i)) for i in range(201)]
        spec = render_chart(execution(rows, ["Branch", "v"]))
        self.assertEqual(spec["type"], "table")
        self.assertIn("行数 201 超过图表类目上限 200", str(spec["note"]))
        self.assertEqual(spec["skipped"], 0, "201 行 ≤ 500 展示上限，不丢行")
        self.assertEqual(len(spec["rows"]), 201)

    def test_table_truncation_noted(self) -> None:
        rows = [(f"b{i}",) for i in range(501)]
        spec = render_chart(execution(rows, ["Branch"]))
        self.assertEqual(spec["type"], "table")
        self.assertEqual(len(spec["rows"]), 500)
        self.assertEqual(spec["skipped"], 1)


class TestTurnResultIntegration(unittest.TestCase):
    """真实链路：DataAgent answer 结果直接可渲染（schema 来自 Guard 出口 SQL）。"""

    class _FakeExecutor:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def __call__(self, sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
            self.calls.append(sql)
            return [("华中", 123.0), ("华东", 98.5)], ["Branch", "commission_revenue"]

    def test_answer_turn_renders_bar(self) -> None:
        agent = DataAgent(executor=self._FakeExecutor(), budget=BUDGET)
        r = agent.ask("按分支统计 2013 年佣金收入，列出前 5 名")
        self.assertEqual(r.kind, "answer")
        spec = render_chart(r)
        self.assertEqual(spec["type"], "bar")
        self.assertEqual(spec["x"], "Branch")
        self.assertEqual(len(spec["data"]), 2)
        self.assertEqual(
            spec["sql_sha256"], hashlib.sha256(str(r.sql).encode("utf-8")).hexdigest()[:12]
        )


class TestDeclaredTimeColumns(unittest.TestCase):
    """ADR-0025 判据 2：编译路径的时间轴只由 `time_columns` 入参声明（决策 ①4）。

    列名取背景 3 表的实测形态：编译别名已小写化（`CalendarYearID` →
    `calendaryearid`），零售列名（`d_year` 等）本就不在兜底集合内。这条直接
    锁住背景 3(c) 的静默误导——时间列未识别时 y 轴会画成年份编号本身。
    """

    def test_declared_d_year_line_y_is_measure(self) -> None:
        """判据 2 核心：time_columns={'d_year'} → line，且 y 是度量不是年份。"""
        spec = render_chart(
            execution(
                [(2000, 100.0, None), (2001, 150.0, 100.0)],
                ["d_year", "total_sales_price", "prev_period_value"],
            ),
            time_columns={"d_year"},
        )
        self.assertEqual(spec["type"], "line")
        self.assertEqual(spec["x"], "d_year")
        self.assertEqual(spec["y"], ["total_sales_price"])
        self.assertEqual(spec["data"], [{"x": 2000, "y": 100.0}, {"x": 2001, "y": 150.0}])

    def test_declared_aliases_line_for_both_domains(self) -> None:
        """背景 3 实测别名（金融 yoy/cumulative + 零售 yoy/cumulative）逐条作 x 轴。"""
        for alias, third in (
            ("calendaryearid", "prev_period_value"),
            ("calendarmonthid", "cumulative_value"),
            ("d_year", "prev_period_value"),
            ("d_moy", "cumulative_value"),
        ):
            with self.subTest(alias=alias):
                spec = render_chart(
                    execution([(1, 10.0, None), (2, 20.0, 10.0)], [alias, "m", third]),
                    time_columns={alias},
                )
                self.assertEqual(spec["type"], "line")
                self.assertEqual(spec["x"], alias)
                self.assertEqual(spec["y"], ["m"])

    def test_empty_declared_with_frozenset_miss_is_bar(self) -> None:
        """判据 2 第二句：入参为空且列名不在兜底集合 → bar（不猜时间轴）。"""
        spec = render_chart(
            execution([(2001, 150.0)], ["d_year", "total_sales_price"]), time_columns=()
        )
        self.assertEqual(spec["type"], "bar")

    def test_declared_set_is_authoritative_over_frozenset(self) -> None:
        """入参非空时不再查兜底集合（决策 ①4「轴选择只依据入参」）。"""
        spec = render_chart(
            execution([(2001, 150.0)], ["CalendarYearID", "total_sales_price"]),
            time_columns={"d_year"},  # 声明列不在结果里 → 不得退回 frozenset 命中
        )
        self.assertEqual(spec["type"], "bar")

    def test_fallback_hit_notes_authority_level(self) -> None:
        """决策 ③3：走兜底且命中时 note 追加权威等级标注（判据 2 第三句）。"""
        spec = render_chart(
            execution([(2013, 123.0), (2014, 156.0)], ["CalendarYearID", "v"]),
            time_columns=(),
        )
        self.assertEqual(spec["type"], "line")
        self.assertIn("兜底识别", str(spec["note"]))
        self.assertIn("非编译器声明", str(spec["note"]))

    def test_declared_path_has_no_fallback_note(self) -> None:
        """编译声明路径不带兜底标注（权威等级不同，note 不得混同）。"""
        spec = render_chart(
            execution([(2013, 123.0), (2014, 156.0)], ["CalendarYearID", "v"]),
            time_columns={"CalendarYearID"},
        )
        self.assertEqual(spec["type"], "line")
        self.assertNotIn("兜底识别", str(spec.get("note", "")))


if __name__ == "__main__":
    unittest.main()
