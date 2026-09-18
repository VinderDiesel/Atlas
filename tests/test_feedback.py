"""用户纠错反馈入口契约测试（Day 46）。

口径（与 agent/feedback.py docstring 一致）：
- 写盘形态：status=pending_review + origin=user + 回合快照（人工确认前不
  进入任何训练/演示数据，AGENTS.md 数据飞轮规则）；
- 只写不覆盖（时间戳+uuid 文件名）；时间 UTC ISO 8601；
- kind/question 在写盘前校验；测试目录全部注入 tmp_path，不碰真实日志。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.feedback import DEFAULT_FEEDBACK_DIR, FeedbackSubmission, submit_feedback
from agent.graph import DataAgent
from agent.security.sql_guard import Budget

REPO = Path(__file__).resolve().parent.parent

_META = json.loads((REPO / "data/snapshots" / "7d48dcb.meta.json").read_text(encoding="utf-8"))
ALLOWED = frozenset(
    f"atlas.{ns}.{table}" for ns, tables in _META["row_counts"].items() for table in tables
)
BUDGET = Budget(dialect="doris", max_rows=10_000, allowed_tables=ALLOWED)

GOLD102_Q = "按分支统计 2013 年佣金收入，列出前 5 名"


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, sql: str) -> tuple[list[tuple[Any, ...]], list[str]]:
        self.calls.append(sql)
        return [("华中", 123.0), ("华东", 98.5)], ["Branch", "commission_revenue"]


class TestSubmitFeedback(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-feedback-"))

    def _read(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_submit_writes_pending_review_record(self) -> None:
        path = submit_feedback(
            FeedbackSubmission(
                question=GOLD102_Q,
                kind="wrong_metric",
                comment="我说的是交易额不是佣金",
                session_id="sess-1",
                metric="commission_revenue",
                sql="SELECT 1",
                submitted_at="2026-09-03T10:00:00+00:00",
            ),
            feedback_dir=self.tmp,
        )
        self.assertTrue(path.is_file())
        record = self._read(path)
        self.assertEqual(record["schema_version"], 2)
        self.assertEqual(record["status"], "pending_review")
        self.assertEqual(record["origin"], "user")
        # 统一 pending 形态（ADR-0027 决策 ④）：submission 字段在 sample 键下
        sample = record["sample"]
        self.assertEqual(sample["kind"], "wrong_metric")
        self.assertEqual(sample["question"], GOLD102_Q)
        self.assertEqual(sample["comment"], "我说的是交易额不是佣金")
        self.assertEqual(sample["metric"], "commission_revenue")
        self.assertEqual(sample["submitted_at"], "2026-09-03T10:00:00+00:00")

    def test_submitted_at_defaults_to_utc_iso(self) -> None:
        before = datetime.now(UTC).isoformat(timespec="seconds")
        path = submit_feedback(
            FeedbackSubmission(question="x", kind="other"),
            feedback_dir=self.tmp,
        )
        after = datetime.now(UTC).isoformat(timespec="seconds")
        stamp = str(self._read(path)["sample"]["submitted_at"])
        self.assertTrue(stamp.endswith("+00:00"), "时间必须是显式 UTC 时区")
        self.assertGreaterEqual(stamp, before)
        self.assertLessEqual(stamp, after)

    def test_writes_do_not_overwrite(self) -> None:
        p1 = submit_feedback(FeedbackSubmission(question="q", kind="other"), feedback_dir=self.tmp)
        p2 = submit_feedback(FeedbackSubmission(question="q", kind="other"), feedback_dir=self.tmp)
        self.assertNotEqual(p1.name, p2.name)
        self.assertEqual(len(list(self.tmp.glob("*.json"))), 2)

    def test_bad_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            submit_feedback(
                FeedbackSubmission(question="q", kind="drop_all"),  # type: ignore[arg-type]
                feedback_dir=self.tmp,
            )
        self.assertEqual(list(self.tmp.glob("*.json")), [])

    def test_empty_question_rejected(self) -> None:
        with self.assertRaises(ValueError):
            submit_feedback(FeedbackSubmission(question="  ", kind="other"), feedback_dir=self.tmp)


class TestDataAgentIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-feedback-agent-"))
        self.agent = DataAgent(executor=FakeExecutor(), budget=BUDGET)

    def test_feedback_from_answer_turn(self) -> None:
        r = self.agent.ask(GOLD102_Q)
        self.assertEqual(r.kind, "answer")
        path = self.agent.submit_feedback(
            r, "wrong_value", comment="数值和报表对不上", feedback_dir=self.tmp
        )
        record = json.loads(path.read_text(encoding="utf-8"))
        sample = record["sample"]
        self.assertEqual(sample["metric"], "commission_revenue")
        self.assertIn("SELECT", str(sample["sql"]))
        self.assertIn("LIMIT 5", str(sample["sql"]))
        self.assertEqual(sample["question"], GOLD102_Q)
        self.assertEqual(sample["engine"], "deterministic")
        self.assertEqual(sample["turns_in_session"], 1)

    def test_feedback_from_clarify_turn_allowed(self) -> None:
        """反问轮纠错（问句本身歧义/误判）也应可上报；metric/sql 为空如实记录。"""
        r = self.agent.ask("最近交易情况怎么样？")
        self.assertEqual(r.kind, "clarify")
        path = self.agent.submit_feedback(
            r, "misunderstood", comment="这个不该反问", feedback_dir=self.tmp
        )
        record = json.loads(path.read_text(encoding="utf-8"))
        sample = record["sample"]
        self.assertIsNone(sample["metric"])
        self.assertIsNone(sample["sql"])

    def test_default_dir_is_failures_user_feedback(self) -> None:
        self.assertEqual(
            DEFAULT_FEEDBACK_DIR,
            REPO / "eval" / "failures" / "user_feedback",
            "默认落盘必须与评测失败归集目录平级（数据飞轮单一入口）",
        )


if __name__ == "__main__":
    unittest.main()
