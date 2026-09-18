"""lora.proposals 契约测试（ADR-0027 T3）：反馈 → 指标变更 proposal 生成（纯函数，只读）。

覆盖：
- wrong_metric → 生成 proposal（含 affected_metric、change_type、evidence、suspected_definition）
- 同 metric 多条反馈 → 合并为一条 proposal（evidence 聚合）
- 无 wrong_metric/wrong_value → 不产 proposal
- write_proposals 只落 eval/failures/_proposals/，绝不写 semantic/ossie/*.yaml
- 判据 4：proposal 生成前后 semantic/ossie/ 无变更

设计约束（0027 决策 ③）：
- 归纳器是**离线纯函数**，不读盘、不调 LLM
- proposal 只落 _proposals/ 非权威区
- **绝不**提交、改写 semantic/ossie/*.yaml
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _pending(
    kind: str,
    question: str,
    metric: str | None = None,
    corrected_term: str = "",
    source: str = "test-feedback",
) -> dict:
    """构造统一 pending 形态记录（ADR-0027 决策 ④ sample 包裹）。"""
    sample: dict = {"question": question, "kind": kind}
    if metric is not None:
        sample["metric"] = metric
    if corrected_term:
        sample["corrected_term"] = corrected_term
    return {"status": "pending_review", "source": source, "sample": sample}


class TestInduceProposals(unittest.TestCase):
    """纯函数 induce_proposals 测试（不触碰文件系统）。"""

    def test_proposal_from_wrong_metric(self) -> None:
        """wrong_metric → 生成 proposal（含 affected_metric + evidence）。"""
        from lora.proposals import induce_proposals

        records = [_pending("wrong_metric", "收入是多少", metric="commission_revenue", source="f1")]
        proposals = induce_proposals(records)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].affected_metric, "commission_revenue")
        self.assertEqual(proposals[0].change_type, "wrong_metric")
        self.assertEqual(proposals[0].evidence, ["f1"])
        self.assertEqual(proposals[0].questions, ["收入是多少"])

    def test_proposal_with_corrected_term(self) -> None:
        """wrong_metric + corrected_term → suspected_definition 记录用户建议。"""
        from lora.proposals import induce_proposals

        records = [_pending("wrong_metric", "收入", metric="commission_revenue", corrected_term="总交易额", source="f1")]
        proposals = induce_proposals(records)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].suspected_definition, "总交易额")

    def test_multiple_feedbacks_same_metric_merged(self) -> None:
        """同 metric 多条 wrong_metric 反馈 → 合并为一条 proposal（evidence 聚合）。"""
        from lora.proposals import induce_proposals

        records = [
            _pending("wrong_metric", "收入", metric="commission_revenue", source="f1"),
            _pending("wrong_metric", "佣金", metric="commission_revenue", source="f2"),
        ]
        proposals = induce_proposals(records)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].affected_metric, "commission_revenue")
        self.assertEqual(len(proposals[0].evidence), 2)
        self.assertIn("f1", proposals[0].evidence)
        self.assertIn("f2", proposals[0].evidence)

    def test_no_proposal_for_non_metric_kinds(self) -> None:
        """misunderstood/other/wrong_time/wrong_value 不产 proposal（wrong_value 由 T2 处理值域别名）。"""
        from lora.proposals import induce_proposals

        records = [
            _pending("misunderstood", "问东答西", metric="m1"),
            _pending("other", "其他问题", metric="m2"),
            _pending("wrong_time", "时间错", metric="m3"),
            _pending("wrong_value", "值错", metric="m4"),
        ]
        proposals = induce_proposals(records)
        self.assertEqual(proposals, [])

    def test_no_proposal_without_metric(self) -> None:
        """wrong_metric 但无 metric 字段 → 不产 proposal（无法定位受影响指标）。"""
        from lora.proposals import induce_proposals

        records = [_pending("wrong_metric", "指标错", metric=None, source="f1")]
        proposals = induce_proposals(records)
        self.assertEqual(proposals, [])

    def test_empty_input(self) -> None:
        """空输入 → 空输出。"""
        from lora.proposals import induce_proposals

        proposals = induce_proposals([])
        self.assertEqual(proposals, [])

    def test_different_metrics_separate_proposals(self) -> None:
        """不同 metric 的 wrong_metric → 各自独立 proposal。"""
        from lora.proposals import induce_proposals

        records = [
            _pending("wrong_metric", "收入", metric="commission_revenue", source="f1"),
            _pending("wrong_metric", "交易额", metric="total_trade_value", source="f2"),
        ]
        proposals = induce_proposals(records)
        self.assertEqual(len(proposals), 2)
        metrics = {p.affected_metric for p in proposals}
        self.assertEqual(metrics, {"commission_revenue", "total_trade_value"})


class TestWriteProposals(unittest.TestCase):
    """write_proposals 文件落盘测试（临时目录，验证只写 _proposals/）。"""

    def test_writes_to_proposals_dir(self) -> None:
        """proposal 文件落入 eval/failures/_proposals/。"""
        from lora.proposals import Proposal, write_proposals

        with tempfile.TemporaryDirectory() as tmp:
            failures_dir = Path(tmp) / "eval" / "failures"
            proposals_dir = failures_dir / "_proposals"
            proposals_dir.mkdir(parents=True)

            proposals = [
                Proposal(
                    affected_metric="commission_revenue",
                    change_type="wrong_metric",
                    evidence=["f1", "f2"],
                    questions=["收入", "佣金"],
                    suspected_definition="总交易额",
                )
            ]
            written = write_proposals(proposals, failures_dir=failures_dir)

            self.assertEqual(len(written), 1)
            self.assertTrue(written[0].exists())
            data = json.loads(written[0].read_text(encoding="utf-8"))
            self.assertEqual(data["affected_metric"], "commission_revenue")
            self.assertEqual(data["evidence"], ["f1", "f2"])

    def test_no_write_when_empty(self) -> None:
        """空 proposal 列表 → 不写任何文件。"""
        from lora.proposals import write_proposals

        with tempfile.TemporaryDirectory() as tmp:
            failures_dir = Path(tmp) / "eval" / "failures"
            (failures_dir / "_proposals").mkdir(parents=True)

            written = write_proposals([], failures_dir=failures_dir)
            self.assertEqual(written, [])

    def test_never_writes_authoritative_sources(self) -> None:
        """铁律（0027 决策 ③）：write_proposals 绝不写 semantic/ossie/*.yaml。"""
        from lora.proposals import Proposal, write_proposals

        with tempfile.TemporaryDirectory() as tmp:
            failures_dir = Path(tmp) / "eval" / "failures"
            (failures_dir / "_proposals").mkdir(parents=True)
            semantic_ossie = Path(tmp) / "semantic" / "ossie"
            semantic_ossie.mkdir(parents=True)

            proposals = [
                Proposal(
                    affected_metric="test_metric",
                    change_type="wrong_metric",
                    evidence=["f1"],
                    questions=["q"],
                )
            ]
            write_proposals(proposals, failures_dir=failures_dir)

            # semantic/ossie/ 下无任何新文件
            ossie_files = list(semantic_ossie.glob("*.yaml"))
            self.assertEqual(len(ossie_files), 0)


class TestProposalGenerationReadOnly(unittest.TestCase):
    """判据 4（0027）：proposal 生成是只读操作，不改 semantic/ossie/。"""

    def test_semantic_ossie_unchanged_after_proposal_generation(self) -> None:
        """induce_proposals + write_proposals 运行前后，semantic/ossie/ 无变更。

        用文件快照对比：运行前后 glob *.yaml 的文件列表与内容逐字一致。
        """
        from lora.proposals import induce_proposals, write_proposals

        ossie_dir = REPO / "semantic" / "ossie"
        # 快照：运行前 semantic/ossie/ 下所有 yaml
        snapshot_before: dict[str, str] = {}
        for yaml_file in sorted(ossie_dir.glob("*.yaml")):
            snapshot_before[yaml_file.name] = yaml_file.read_text(encoding="utf-8")

        records = [
            _pending("wrong_metric", "测试问句", metric="test_metric", source="test-source"),
        ]
        proposals = induce_proposals(records)

        with tempfile.TemporaryDirectory() as tmp:
            failures_dir = Path(tmp) / "eval" / "failures"
            (failures_dir / "_proposals").mkdir(parents=True)
            write_proposals(proposals, failures_dir=failures_dir)

        # 快照：运行后 semantic/ossie/ 下所有 yaml
        snapshot_after: dict[str, str] = {}
        for yaml_file in sorted(ossie_dir.glob("*.yaml")):
            snapshot_after[yaml_file.name] = yaml_file.read_text(encoding="utf-8")

        self.assertEqual(snapshot_before, snapshot_after, "proposal 生成不应改 semantic/ossie/")


if __name__ == "__main__":
    unittest.main()
