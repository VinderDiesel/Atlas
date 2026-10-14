"""T2 评测：同义词/值域别名候选归纳器（ADR-0027 决策 ②）。

覆盖：
- wrong_metric + corrected_term → 同义词候选
- misunderstood + corrected_term → 同义词候选
- wrong_value + corrected_term → 值域别名候选
- pending_review 状态不产出候选（只处理 approved）
- 无 corrected_term 的 approved 样本跳过
- 候选文件不写权威源（synonyms/*.yml、values/*.json 不变）
"""

from __future__ import annotations

import json
import unittest
from contextlib import suppress
from pathlib import Path
from typing import Any

from lora.candidate_induce import induce_candidates


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
    sha: str = "t2test",
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


class TestCandidateInduce(unittest.TestCase):
    """候选归纳器：合成 pending → 期望候选（G2 解除评测）。"""

    def setUp(self) -> None:
        self.tmp = Path(__file__).resolve().parent.parent / "eval" / "failures" / "_test_tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        import shutil

        with suppress(Exception):
            shutil.rmtree(self.tmp)

    def test_wrong_metric_produces_synonym_candidate(self) -> None:
        """wrong_metric + corrected_term → 同义词候选。"""
        _write_pending(
            self.tmp / "user_feedback" / "t2-wm1.json",
            kind="wrong_metric",
            corrected_term="佣金收入",
            metric="commission_income",
            question="佣金是多少",
            comment="指标选错了",
            sample_id="wm1",
        )
        stats = induce_candidates(self.tmp, dry=True)
        self.assertEqual(stats["synonym_candidates"], 1)
        self.assertEqual(stats["value_alias_candidates"], 0)

    def test_misunderstood_produces_synonym_candidate(self) -> None:
        """misunderstood + corrected_term → 同义词候选。"""
        _write_pending(
            self.tmp / "user_feedback" / "t2-mu1.json",
            kind="misunderstood",
            corrected_term="净利润",
            metric="net_profit",
            question="赚了多少",
            sample_id="mu1",
        )
        stats = induce_candidates(self.tmp, dry=True)
        self.assertEqual(stats["synonym_candidates"], 1)

    def test_wrong_value_produces_value_alias_candidate(self) -> None:
        """wrong_value + corrected_term → 值域别名候选。"""
        _write_pending(
            self.tmp / "user_feedback" / "t2-wv1.json",
            kind="wrong_value",
            corrected_term="金牌",
            metric="Tier",
            question="金牌客户有多少",
            sample_id="wv1",
        )
        stats = induce_candidates(self.tmp, dry=True)
        self.assertEqual(stats["synonym_candidates"], 0)
        self.assertEqual(stats["value_alias_candidates"], 1)

    def test_pending_review_not_processed(self) -> None:
        """pending_review 状态不产出候选。"""
        _write_pending(
            self.tmp / "user_feedback" / "t2-pr1.json",
            status="pending_review",
            kind="wrong_metric",
            corrected_term="佣金收入",
            metric="commission_income",
            sample_id="pr1",
        )
        stats = induce_candidates(self.tmp, dry=True)
        self.assertEqual(stats["synonym_candidates"], 0)
        self.assertEqual(stats["skipped_not_approved"], 1)

    def test_no_corrected_term_skipped(self) -> None:
        """approved 但无 corrected_term → 跳过。"""
        _write_pending(
            self.tmp / "user_feedback" / "t2-nc1.json",
            status="approved",
            kind="wrong_metric",
            corrected_term="",
            metric="commission_income",
            sample_id="nc1",
        )
        stats = induce_candidates(self.tmp, dry=True)
        self.assertEqual(stats["synonym_candidates"], 0)
        self.assertEqual(stats["skipped_no_correction"], 1)

    def test_dry_does_not_write_files(self) -> None:
        """dry=True 不落盘候选文件。"""
        _write_pending(
            self.tmp / "user_feedback" / "t2-dry1.json",
            kind="wrong_metric",
            corrected_term="测试词",
            metric="test_metric",
            sample_id="dry1",
        )
        induce_candidates(self.tmp, dry=True)
        syn_dir = self.tmp.parent.parent / "semantic" / "synonyms" / "_candidates"
        # dry 模式不应在 _candidates/ 下创建新文件
        new_files = list(syn_dir.glob("syn_t2test-dry1.json")) if syn_dir.exists() else []
        self.assertEqual(len(new_files), 0)

    def test_non_dry_writes_candidate_file(self) -> None:
        """dry=False 落盘候选 JSON，内容含 term/source_feedback。"""
        _write_pending(
            self.tmp / "user_feedback" / "t2-nd1.json",
            kind="wrong_metric",
            corrected_term="营收",
            metric="total_revenue",
            question="营收多少",
            comment="应该看总营收",
            sha="abc123",
            sample_id="nd1",
        )
        syn_out = self.tmp / "_syn_out"
        val_out = self.tmp / "_val_out"
        stats = induce_candidates(self.tmp, dry=False, syn_dir=syn_out, val_dir=val_out)
        self.assertEqual(stats["synonym_candidates"], 1)
        # 验证候选文件内容
        candidate_file = syn_out / "syn_abc123-nd1.json"
        try:
            self.assertTrue(candidate_file.exists(), "候选文件应已写入")
            content = json.loads(candidate_file.read_text(encoding="utf-8"))
            self.assertEqual(content["term"], "营收")
            self.assertEqual(content["suggest_for"], "total_revenue")
            self.assertEqual(content["suggest_kind"], "metric")
            self.assertIn("user_feedback", content["source_feedback"])
        finally:
            _cleanup(candidate_file)


if __name__ == "__main__":
    unittest.main()
