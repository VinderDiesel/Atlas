"""自洽聚类与候选回退契约测试（Day 33）。

口径：聚类/回退的输入输出纯逻辑（执行由注入闭包提供，本文件用 stub 闭包），
不依赖 Doris/网络；与 eval/rag_eval.py 组合时由评测层注入真实执行链。
"""

from __future__ import annotations

import unittest

from agent.compiler import Compiler, Plan, SemanticModel
from agent.tools.execution_validator import ExecutionValidator
from agent.tools.self_consistency import cluster_by_result, fallback_route

MODEL = SemanticModel()
COMPILER = Compiler(MODEL)


def plan_for(metric: str) -> Plan:
    """按 gold-102 口径构造候选 Plan（不同指标 → 不同 SQL/结果）。"""
    return Plan(metric=metric, dimensions=("Branch",), time=None, limit=5)


class TestClusterByResult(unittest.TestCase):
    def test_majority_wins(self) -> None:
        """3 次采样 2 次同结果 → 该结果胜出。"""
        r = cluster_by_result([(0, "h1"), (1, "h2"), (2, "h2")])
        self.assertEqual(r, 1)

    def test_tie_breaks_by_earliest(self) -> None:
        """平局 → 取候选序最靠前者（确定性破例）。"""
        r = cluster_by_result([(0, "h1"), (1, "h2"), (2, "h1"), (3, "h2")])
        self.assertIn(r, (0, 2))  # h1 组（min index 0 < h2 组 1）

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(cluster_by_result([]))


class TestFallbackRoute(unittest.TestCase):
    def _route(
        self, sql_outcomes: dict[str, tuple[list[tuple], list[str]]], errors: set[str] | None = None
    ):
        """stub 执行闭包：按编译出的 SQL 特征返回预置结果。"""
        errors = errors or set()

        def run(plan: Plan) -> tuple[list[tuple], list[str]]:
            sql, _ = COMPILER.compile(plan)
            if sql in errors:
                raise RuntimeError(f"stub error for {plan.metric}")
            return sql_outcomes[sql]

        return run

    def test_top1_valid_wins_without_fallback(self) -> None:
        """top1 有效即采用，不触发回退。"""
        sql_ok, _ = COMPILER.compile(plan_for("commission_revenue"))
        outcome: dict[str, tuple[list[tuple], list[str]]] = {
            sql_ok: ([("A", 1.0)], ["Branch", "commission_revenue"])
        }
        plans = [plan_for("commission_revenue"), plan_for("total_trade_value")]
        r = fallback_route(plans, self._route(outcome), ExecutionValidator())
        self.assertEqual(r.chosen, 0)
        self.assertEqual(len(r.outcomes), 1)

    def test_top1_empty_falls_back_to_next(self) -> None:
        """top1 执行 0 行（空）→ 校验失败 → 回退 top2。"""
        sql_top1, _ = COMPILER.compile(plan_for("commission_revenue"))
        sql_top2, _ = COMPILER.compile(plan_for("total_trade_value"))
        outcome: dict[str, tuple[list[tuple], list[str]]] = {
            sql_top1: ([], ["Branch", "commission_revenue"]),
            sql_top2: ([("B", 2.0)], ["Branch", "total_trade_value"]),
        }
        plans = [plan_for("commission_revenue"), plan_for("total_trade_value")]
        r = fallback_route(plans, self._route(outcome), ExecutionValidator())
        self.assertEqual(r.chosen, 1)
        self.assertFalse(r.outcomes[0].ok)
        self.assertIn("empty_result", r.outcomes[0].issues)
        self.assertTrue(r.outcomes[1].ok)

    def test_top1_error_falls_back(self) -> None:
        """top1 编译/Guard/执行抛错 → 记 error 并回退。"""
        sql_top1, _ = COMPILER.compile(plan_for("commission_revenue"))
        sql_top2, _ = COMPILER.compile(plan_for("total_trade_value"))
        outcome: dict[str, tuple[list[tuple], list[str]]] = {
            sql_top2: ([("B", 2.0)], ["Branch", "total_trade_value"])
        }
        plans = [plan_for("commission_revenue"), plan_for("total_trade_value")]
        r = fallback_route(plans, self._route(outcome, errors={sql_top1}), ExecutionValidator())
        self.assertEqual(r.chosen, 1)
        self.assertIn("RuntimeError", r.outcomes[0].error or "")

    def test_all_invalid_returns_none(self) -> None:
        """全部候选无效 → chosen=None（上层触发澄清/重生成）。"""
        sql_top1, _ = COMPILER.compile(plan_for("commission_revenue"))
        sql_top2, _ = COMPILER.compile(plan_for("total_trade_value"))
        outcome: dict[str, tuple[list[tuple], list[str]]] = {
            sql_top1: ([], ["Branch", "commission_revenue"]),
            sql_top2: ([], ["Branch", "total_trade_value"]),
        }
        plans = [plan_for("commission_revenue"), plan_for("total_trade_value")]
        r = fallback_route(plans, self._route(outcome), ExecutionValidator())
        self.assertIsNone(r.chosen)
        self.assertEqual(len(r.outcomes), 2)


if __name__ == "__main__":
    unittest.main()
