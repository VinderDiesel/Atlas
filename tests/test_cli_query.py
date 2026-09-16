"""agent.cli query 子命令单元测试（隔离 DB：monkeypatch Guard/执行/编译）。

覆盖：确定性已知路径、未知默认澄清、Guard 拒绝、行级策略注入、json/table 输出、
退出码分流、快照绑定回显（stderr）。不触碰真实 Doris，也不读真实 data/snapshots/
（execute_sql/enforce 打桩；`_load_budget` 在主用例打桩，在 TestLoadBudgetWiring 里
走真实实现、只把 resolve_runtime_snapshot 打桩）。
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
from data.identity import RuntimeSnapshot, SnapshotUnavailable


def _dummy_budget() -> tuple[Budget, RuntimeSnapshot]:
    """桩与真实 `_load_budget()` 同形态：(预算, 快照解析结果)。

    ADR-0019 工作项 3 起返回值多了一个快照对象（cmd_query 要据它回显绑定），
    只改实现不改这个桩会让本文件 8 例以 TypeError 失败——桩跟实现必须同批改。
    """
    snapshot = RuntimeSnapshot(
        sha="abc1234",
        meta={"sha": "abc1234", "created_at": "2026-09-09T11:56:23+08:00"},
        source="head",
        bound_to_head=True,
    )
    return Budget(dialect="doris", max_rows=10000, allowed_tables=frozenset()), snapshot


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

    def _run_split_streams(self, argv: list[str]) -> tuple[int, str, str]:
        """分别捕获 stdout / stderr（回显走 stderr 是本文件要守的机读契约）。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_snapshot_binding_echoed_on_stderr(self) -> None:
        """query 必须回显快照绑定，且**只能**回显在 stderr（决策 ① + 代价 ③）。

        非 HEAD 绑定成为可达状态后，回显是唯一约束手段；而打进 stdout 会改变
        `--format json` 的键集（那是机读契约，扩字段属决策 ⑥ 的同批变更）。
        """
        rc, out, err = self._run_split_streams(["query", "2013 年总交易额", "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["exit_status"], "ok", "stdout 仍可整段解析")
        self.assertIn("[snapshot]", err)
        self.assertIn("sha=abc1234", err)
        self.assertIn("bound_to_head=true", err)

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


class TestLoadBudgetWiring(unittest.TestCase):
    """`_load_budget` 自身的接线（独立类：不套用上面的全局桩，才碰得到真实实现）。

    ADR-0019 判据 1 第 4 级的 CLI 侧：全无 meta → SnapshotUnavailable 必须转成
    exit 1 语义（SystemExit 带消息），而不是让异常逃到顶层变 traceback。
    """

    def test_unavailable_snapshot_becomes_systemexit(self) -> None:
        err = SnapshotUnavailable("data/snapshots 无锁定快照 meta")
        with (
            mock.patch.object(cli, "resolve_runtime_snapshot", side_effect=err),
            self.assertRaises(SystemExit) as ctx,
        ):
            cli._load_budget()
        self.assertIn("无锁定快照 meta", str(ctx.exception), "原始消息要原样带出")

    def test_returns_budget_built_from_resolved_meta(self) -> None:
        """预算必须由解析出的那份 meta 构造，并把快照对象一并返回（供回显）。"""
        meta = {
            "sha": "abc1234",
            "created_at": "2026-09-09T11:56:23+08:00",
            "row_counts": {"dwd": ["fact_trades"]},
        }
        snap = RuntimeSnapshot(sha="abc1234", meta=meta, source="latest", bound_to_head=False)
        with mock.patch.object(cli, "resolve_runtime_snapshot", return_value=snap):
            budget, got = cli._load_budget()
        self.assertIs(got, snap)
        self.assertEqual(budget.dialect, "doris")
        self.assertEqual(budget.allowed_tables, frozenset({"atlas.dwd.fact_trades"}))


if __name__ == "__main__":
    unittest.main()
