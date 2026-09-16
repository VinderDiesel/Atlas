"""eval.runner 评测逻辑契约测试（ADR-0017 判据 3：Guard 拒绝不中断整轮）。

覆盖：单样本 Guard 拒绝 → `guard: blocked` 态（不冒泡中断整轮）；执行失败仍走
`error` 键——两态分开计数，防 `exec_errors` 语义被安全拒绝污染（blocked 是安全
网关拦下、SQL 未落库；exec_errors 是环境/方言失败）。
不触碰真实 Doris（enforce / execute_sql 打桩）、不读真实 data/snapshots/、
不写 gold 文件（桩证据链在 enforce 处即止，evaluate 不会走到锚定回填分支）。
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

import eval.runner as runner
from agent.compiler import Plan
from agent.security.sql_guard import Budget, UnsafeQuery


class _StubPlanner:
    """最小 Planner 桩：固定返回非澄清 Plan（不触发 clarify 分支）。"""

    def plan(self, question: str, locale: str = "zh") -> Plan:
        return Plan(metric="total_trade_value")


class _StubCompiler:
    """最小 Compiler 桩：SQL 内容无关紧要（enforce 被 mock 替换）。"""

    def compile(self, plan: Plan) -> tuple[str, list[str]]:
        return "SELECT 1 AS x", []


def _gold_stub() -> dict[str, Any]:
    return {
        "id": "gold-000",
        "question": "契约测试桩问句",
        "ambiguous": False,
        "result_hash": None,
        "snapshot_sha": None,
        "_file": "/nonexistent/contract-stub.json",
    }


class TestGuardRejectionBlocked(unittest.TestCase):
    """判据 3：Guard 拒绝记为 blocked 态且不中断整轮（防未来样本回归）。"""

    def test_blocked_recorded_without_raise(self) -> None:
        with mock.patch.object(
            runner, "enforce", side_effect=UnsafeQuery("表不在白名单内：atlas.dwd.secret")
        ):
            result = runner.evaluate(
                _StubPlanner(),
                _StubCompiler(),
                _gold_stub(),
                Budget(dialect="doris"),
                "testsha",
                dry=False,
            )
        self.assertEqual(result.get("guard"), "blocked")
        self.assertIn("表不在白名单内", str(result.get("guard_reason")))
        self.assertNotIn("error", result)

    def test_execute_failure_still_recorded(self) -> None:
        """回归锁：执行失败仍走 error 键（本判据只捕 UnsafeQuery，不放宽其他异常）。"""
        with (
            mock.patch.object(runner, "enforce", return_value=("SELECT 1 AS x", 0.0)),
            mock.patch.object(runner, "execute_sql", side_effect=RuntimeError("boom")),
        ):
            result = runner.evaluate(
                _StubPlanner(),
                _StubCompiler(),
                _gold_stub(),
                Budget(dialect="doris"),
                "testsha",
                dry=False,
            )
        self.assertIn("RuntimeError", str(result.get("error")))
        self.assertNotIn("guard", result)


class TestSummarizeBlockedCount(unittest.TestCase):
    """blocked 与 exec_errors 分开计数（判据 3 的「态」在汇总层可见）。"""

    def test_separate_counts(self) -> None:
        results: list[dict[str, Any]] = [
            {"ambiguous": False, "plan_ok": True, "guard": "blocked", "guard_reason": "x"},
            {"ambiguous": False, "plan_ok": True, "error": "RuntimeError: boom"},
            {"ambiguous": False, "plan_ok": True, "ex": "pass"},
        ]
        summary = runner.summarize(results)
        self.assertEqual(summary["guard_blocked"], 1)
        self.assertEqual(summary["exec_errors"], 1)


if __name__ == "__main__":
    unittest.main()
