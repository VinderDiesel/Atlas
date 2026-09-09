"""agent.cli query 子命令单元测试（隔离 DB：monkeypatch Guard/执行/编译）。

覆盖：确定性已知路径、未知默认澄清、Guard 拒绝、行级策略注入、json/table 输出、
退出码分流。不触碰真实 Doris（execute_sql/enforce/_load_budget 全部打桩）。
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from typing import Any
from unittest import mock

import agent.cli as cli
from agent.compiler import Plan
from agent.planner import ClarificationRequest
from agent.security.sql_guard import Budget, UnsafeQuery


def _dummy_budget() -> Budget:
    return Budget(dialect="doris", max_rows=10000, allowed_tables=frozenset())


def _fake_plan() -> Plan:
    return Plan(
        metric="commission_revenue", dimensions=(), time=None,
        filters=(), order_by=(), limit=100,
    )


class TestCliQuery(unittest.TestCase):
    def setUp(self) -> None:
        self.patches = [
            mock.patch.object(cli.Compiler, "compile", return_value=("SELECT 1 AS x", {})),
            mock.patch.object(cli, "enforce", return_value=("SELECT 1 AS x", 1.0)),
            mock.patch.object(cli, "execute_sql", return_value=([(1,), (2,)], ["x"])),
            mock.patch.object(cli, "_load_budget", return_value=_dummy_budget()),
            mock.patch.object(cli.Planner, "plan", return_value=_fake_plan()),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def _run(self, argv: list[str]) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(argv)
        return rc, buf.getvalue()

    def test_deterministic_ok_table(self) -> None:
        rc, _ = self._run(["query", "2013 年总交易额"])
        self.assertEqual(rc, 0)

    def test_deterministic_ok_json(self) -> None:
        rc, out = self._run(["query", "2013 年总交易额", "--format", "json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["exit_status"], "ok")
        self.assertEqual(payload["metric"], "commission_revenue")
        self.assertEqual(payload["rows"], [[1], [2]])
        self.assertEqual(payload["row_count"], 2)
        self.assertIsNone(payload["row_policy"])

    def test_clarify_unmatched_default_exit2(self) -> None:
        with mock.patch.object(
            cli.Planner,
            "plan",
            return_value=ClarificationRequest(
                question="q", reasons=("未命中",), candidates=("m1",), kind="unmatched"
            ),
        ):
            rc, _ = self._run(["query", "新措辞问句"])
        self.assertEqual(rc, 2)

    def test_clarify_json_shape(self) -> None:
        with mock.patch.object(
            cli.Planner,
            "plan",
            return_value=ClarificationRequest(
                question="q", reasons=("未命中",), candidates=("m1",), kind="unmatched"
            ),
        ):
            rc, out = self._run(["query", "新措辞问句", "--format", "json"])
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        self.assertEqual(payload["exit_status"], "clarify")
        self.assertEqual(payload["candidates"], ["m1"])

    def test_blocked_guard_exit3(self) -> None:
        with mock.patch.object(cli, "enforce", side_effect=UnsafeQuery("含写操作")):
            rc, _ = self._run(["query", "2013 年总交易额"])
        self.assertEqual(rc, 3)

    def test_role_injection_reaches_guard(self) -> None:
        captured: dict[str, Any] = {}

        def fake_enforce(sql, policy=None, budget=None, user_context=None, model=None):
            captured["policy"] = policy
            return ("SELECT 1 AS x", 1.0)

        resolved = type("R", (), {"policy_name": "rp_branch_visible", "condition": "1=1"})()
        with mock.patch.object(cli, "enforce", side_effect=fake_enforce), mock.patch.object(
            cli, "resolve_claims", return_value=resolved
        ):
            rc, out = self._run(
                ["query", "2013 年总交易额", "--format", "json",
                 "--role", "branch_manager", "--role-ctx", "branch=BR_A1"]
            )
        self.assertEqual(rc, 0)
        self.assertIsNotNone(captured["policy"])
        self.assertEqual(captured["policy"].name, "rp_branch_visible")
        self.assertEqual(json.loads(out)["row_policy"], "rp_branch_visible")

    def test_role_bad_context_exit3(self) -> None:
        from serving.auth import AuthError

        with mock.patch.object(cli, "resolve_claims", side_effect=AuthError("缺 user_context")):
            rc, _ = self._run(["query", "2013 年总交易额", "--role", "branch_manager"])
        self.assertEqual(rc, 3)

    def test_unmatched_with_llm_uses_candidate_path(self) -> None:
        gen_res = type(
            "G", (), {"plan": _fake_plan(), "refusal": None, "usage": {"total_tokens": 7}}
        )()
        with mock.patch.object(
            cli.Planner,
            "plan",
            return_value=ClarificationRequest(question="q", reasons=("未命中",), kind="unmatched"),
        ), mock.patch.object(cli.Generator, "generate", return_value=gen_res):
            rc, out = self._run(["query", "新措辞", "--llm", "--format", "json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["path"], "candidate")
        self.assertEqual(payload["usage"], {"total_tokens": 7})


if __name__ == "__main__":
    unittest.main()
