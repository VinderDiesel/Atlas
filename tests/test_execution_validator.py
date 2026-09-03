"""执行校验器契约测试（Day 33）：空 / 全 NULL / 非分组多行 / 正常。"""

from __future__ import annotations

import unittest

from agent.tools.execution_validator import ExecutionValidator

V = ExecutionValidator()


class TestExecutionValidator(unittest.TestCase):
    def test_ok_single_row(self) -> None:
        r = V.check([("2013Q2", 344129059.35)], ["CalendarQtrID", "total_trade_value"])
        self.assertTrue(r.ok)
        self.assertEqual(r.issues, ())

    def test_empty_result(self) -> None:
        r = V.check([], ["total_trade_value"])
        self.assertFalse(r.ok)
        self.assertIn("empty_result", r.issues)

    def test_all_null_column(self) -> None:
        r = V.check([(None,), (None,)], ["commission_revenue"], grouped=True)
        self.assertFalse(r.ok)
        self.assertIn("all_null_column:commission_revenue", r.issues)

    def test_unexpected_multi_row_non_grouped(self) -> None:
        """非分组（单行聚合语义）返回多行 → 口径异常。"""
        r = V.check([(1,), (2,)], ["total_trade_value"])
        self.assertFalse(r.ok)
        self.assertIn("unexpected_multi_row", r.issues)

    def test_grouped_multi_row_ok(self) -> None:
        """分组查询多行合法。"""
        r = V.check([("A", 1.0), ("B", 2.0)], ["Branch", "commission_revenue"], grouped=True)
        self.assertTrue(r.ok)

    def test_partial_null_ok(self) -> None:
        """部分行 NULL（如无佣金的分支）不判异常——NULL 是数据特性。"""
        rows = [("A", None), ("B", 2.0)]
        r = V.check(rows, ["Branch", "commission_revenue"], grouped=True)
        self.assertTrue(r.ok)


if __name__ == "__main__":
    unittest.main()
