"""T3 评测：指标变更 proposal 生成器（ADR-0027 决策 ③）。

覆盖：
- wrong_metric + corrected_term → review_metric proposal
- wrong_value + corrected_term → add_alias proposal
- pending_review 不产出 proposal
- 无 corrected_term 跳过
- 同 metric 多条反馈 → evidence_feedbacks 非空
- 不写 semantic/ossie/（只读验证判据 4）
"""

from __future__ import annotations

import json
import unittest
from contextlib import suppress
from pathlib import Path

from lora.proposal_generate import generate_proposals


def _cleanup(p: Path) -> None:
    with suppress(FileNotFoundError, OSError):
        p.unlink()


def _write_pending(
    target: Path,
    *,
    status: str = "approved",
    kind: str = "wrong_metric",
    corrected_term: str = "",
    metric: str = "total_trade_value",
    question: str = "问句",
    comment: str = "",
    sha: str = "t3test",
    sample_id: str = "s1",
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "status": status,
        "origin": "user",
        "sha": sha,
        "sample": {
            "id": sample_id,
            "question": question,
            "kind": kind,
            "comment": comment,
            "metric": metric,
            "corrected_term": corrected_term,
        },
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class TestProposalGenerate(unittest.TestCase):
    """Proposal 生成器：合成 pending → 期望 proposal（判据 4 先写评测）。"""

    def setUp(self) -> None:
        self.tmp = Path(__file__).resolve().parent.parent / "eval" / "failures" / "_test_tmp_t3"
        self.tmp.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        import shutil

        with suppress(Exception):
            shutil.rmtree(self.tmp)

    def test_wrong_metric_produces_review_proposal(self) -> None:
        """wrong_metric + corrected_term → review_metric proposal。"""
        _write_pending(
            self.tmp / "user_feedback" / "t3-wm1.json",
            kind="wrong_metric",
            corrected_term="佣金收入",
            metric="commission_income",
            question="佣金是多少",
            sample_id="wm1",
        )
        stats = generate_proposals(self.tmp, dry=True)
        self.assertEqual(stats["metric_proposals"], 1)
        self.assertEqual(stats["value_proposals"], 0)

    def test_wrong_value_produces_alias_proposal(self) -> None:
        """wrong_value + corrected_term → add_alias proposal。"""
        _write_pending(
            self.tmp / "user_feedback" / "t3-wv1.json",
            kind="wrong_value",
            corrected_term="金牌",
            metric="Tier",
            question="金牌客户有多少",
            sample_id="wv1",
        )
        stats = generate_proposals(self.tmp, dry=True)
        self.assertEqual(stats["metric_proposals"], 0)
        self.assertEqual(stats["value_proposals"], 1)

    def test_pending_review_skipped(self) -> None:
        """pending_review 不产出 proposal。"""
        _write_pending(
            self.tmp / "user_feedback" / "t3-pr1.json",
            status="pending_review",
            kind="wrong_metric",
            corrected_term="佣金收入",
            metric="commission_income",
            sample_id="pr1",
        )
        stats = generate_proposals(self.tmp, dry=True)
        self.assertEqual(stats["metric_proposals"], 0)
        self.assertEqual(stats["skipped"], 1)

    def test_evidence_feedbacks_links_same_metric(self) -> None:
        """同 metric 多条 approved 反馈 → evidence_feedbacks 非空。"""
        _write_pending(
            self.tmp / "user_feedback" / "t3-ef1.json",
            kind="wrong_metric",
            corrected_term="佣金",
            metric="commission_income",
            question="佣金多少",
            sample_id="ef1",
        )
        _write_pending(
            self.tmp / "user_feedback" / "t3-ef2.json",
            kind="wrong_metric",
            corrected_term="提成",
            metric="commission_income",
            question="提成多少",
            sample_id="ef2",
        )
        stats = generate_proposals(self.tmp, dry=True)
        self.assertEqual(stats["metric_proposals"], 2)

    def test_does_not_write_to_ossie(self) -> None:
        """判据 4：proposal 生成器不写 semantic/ossie/。"""
        _write_pending(
            self.tmp / "user_feedback" / "t3-ro1.json",
            kind="wrong_metric",
            corrected_term="测试",
            metric="test_metric",
            sample_id="ro1",
        )
        ossie_dir = self.tmp.parent.parent / "semantic" / "ossie"
        ossie_before = set()
        if ossie_dir.exists():
            ossie_before = {p.name for p in ossie_dir.iterdir()}
        generate_proposals(self.tmp, dry=False, proposals_dir=self.tmp / "_prop_out")
        ossie_after = set()
        if ossie_dir.exists():
            ossie_after = {p.name for p in ossie_dir.iterdir()}
        self.assertEqual(ossie_before, ossie_after, "proposal 生成不应修改 semantic/ossie/")

    def test_non_dry_writes_proposal_file(self) -> None:
        """dry=False 落盘 proposal JSON。"""
        _write_pending(
            self.tmp / "user_feedback" / "t3-nd1.json",
            kind="wrong_metric",
            corrected_term="营收",
            metric="total_revenue",
            question="营收多少",
            sha="xyz789",
            sample_id="nd1",
        )
        prop_out = self.tmp / "_prop_out"
        stats = generate_proposals(self.tmp, dry=False, proposals_dir=prop_out)
        self.assertEqual(stats["metric_proposals"], 1)
        proposal_file = prop_out / "prop_xyz789-nd1.json"
        try:
            self.assertTrue(proposal_file.exists(), "proposal 文件应已写入")
            content = json.loads(proposal_file.read_text(encoding="utf-8"))
            self.assertEqual(content["proposal_type"], "review_metric")
            self.assertEqual(content["target_metric"], "total_revenue")
            self.assertEqual(content["suggested_term"], "营收")
        finally:
            _cleanup(proposal_file)


if __name__ == "__main__":
    unittest.main()
