"""T14 intent_v1 数据准备契约测试。

覆盖：
- validate_training_pair：单条训练样本格式校验
- build_training_pairs：ReviewedLabel → 训练样本转换
- 超长/无标签/泄漏拒绝
- loss mask 标记
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from serving.control.contracts import Owner

TZ = timezone(timedelta(hours=8))
REVIEWER = Owner(issuer="test", subject="reviewer")
USER = Owner(issuer="test", subject="user-1")


def _make_reviewed_label(
    *,
    feedback_id: str = "fb-001",
    question: str = "2013年第二季度总交易额",
    verdict: str = "corrected",
    status: str = "approved",
    correction: dict | None = None,
    source_family: str = "user",
    attribution_node: str | None = None,
) -> object:
    """构建最小 ReviewedLabel。"""
    from serving.control.datasets import ReviewedLabel

    if correction is None and verdict == "corrected":
        correction = {"metric": "total_amount", "dimensions": [], "time": None}

    return ReviewedLabel(
        feedback_id=feedback_id,
        run_id="run-001",
        owner=USER,
        verdict=verdict,
        status=status,
        reviewed_by=REVIEWER,
        reviewed_at=datetime.now(TZ).isoformat(timespec="seconds"),
        question=question,
        source_family=source_family,
        attribution_node=attribution_node,
        correction=correction,
        comment=None,
    )


# ----------  validate_training_pair  ----------


class TestValidateTrainingPair:
    """单条训练样本格式校验。"""

    def test_valid_pair_passes(self) -> None:
        """合法样本通过校验。"""
        from lora.intent_data import validate_training_pair

        pair = {
            "question": "2013年第二季度总交易额",
            "answer": '{"metric": "total_amount"}',
            "loss_mask_prompt": True,
        }
        assert validate_training_pair(pair) is True

    def test_empty_question_rejected(self) -> None:
        """空问句拒绝。"""
        from lora.intent_data import validate_training_pair

        pair = {"question": "", "answer": '{"metric": "total_amount"}'}
        assert validate_training_pair(pair) is False

    def test_empty_answer_rejected(self) -> None:
        """空答案拒绝。"""
        from lora.intent_data import validate_training_pair

        pair = {"question": "交易额", "answer": ""}
        assert validate_training_pair(pair) is False

    def test_non_json_answer_rejected(self) -> None:
        """非 JSON 答案拒绝。"""
        from lora.intent_data import validate_training_pair

        pair = {"question": "交易额", "answer": "not json"}
        assert validate_training_pair(pair) is False

    def test_answer_without_metric_rejected(self) -> None:
        """答案缺少 metric 字段拒绝。"""
        from lora.intent_data import validate_training_pair

        pair = {"question": "交易额", "answer": '{"dimensions": []}'}
        assert validate_training_pair(pair) is False

    def test_oversized_question_rejected(self) -> None:
        """超长问句拒绝（max_length 由配方指定，此处用默认 2048）。"""
        from lora.intent_data import validate_training_pair

        pair = {"question": "x" * 3000, "answer": '{"metric": "total_amount"}'}
        assert validate_training_pair(pair) is False


# ----------  build_training_pairs  ----------


class TestBuildTrainingPairs:
    """ReviewedLabel → 训练样本转换。"""

    def test_corrected_verdict_produces_pair(self) -> None:
        """corrected 反馈 → 训练样本（correction 作为 answer）。"""
        from lora.intent_data import build_training_pairs

        label = _make_reviewed_label(verdict="corrected")
        pairs = build_training_pairs([label])
        assert len(pairs) == 1
        assert pairs[0]["question"] == "2013年第二季度总交易额"
        answer = json.loads(pairs[0]["answer"])
        assert answer["metric"] == "total_amount"

    def test_up_verdict_with_correction_produces_pair(self) -> None:
        """up 反馈 + correction → 训练样本（correction 作为 answer）。"""
        from lora.intent_data import build_training_pairs

        correction = {"metric": "total_amount", "dimensions": []}
        label = _make_reviewed_label(verdict="up", correction=correction)
        pairs = build_training_pairs([label])
        assert len(pairs) == 1

    def test_down_verdict_without_correction_skipped(self) -> None:
        """down 反馈无 correction → 跳过（无标签不训练）。"""
        from lora.intent_data import build_training_pairs

        label = _make_reviewed_label(verdict="down", correction=None)
        pairs = build_training_pairs([label])
        assert len(pairs) == 0

    def test_no_correction_skipped(self) -> None:
        """无 correction 的反馈 → 跳过。"""
        from lora.intent_data import build_training_pairs

        label = _make_reviewed_label(verdict="up", correction=None)
        pairs = build_training_pairs([label])
        assert len(pairs) == 0

    def test_loss_mask_present(self) -> None:
        """训练样本包含 loss_mask_prompt 标记。"""
        from lora.intent_data import build_training_pairs

        label = _make_reviewed_label(verdict="corrected")
        pairs = build_training_pairs([label])
        assert len(pairs) == 1
        assert pairs[0]["loss_mask_prompt"] is True

    def test_source_family_preserved(self) -> None:
        """来源族信息保留在训练样本中。"""
        from lora.intent_data import build_training_pairs

        label = _make_reviewed_label(verdict="corrected", source_family="eval")
        pairs = build_training_pairs([label])
        assert len(pairs) == 1
        assert pairs[0]["source_family"] == "eval"

    def test_multiple_labels(self) -> None:
        """多条标签 → 多条训练样本。"""
        from lora.intent_data import build_training_pairs

        labels = [
            _make_reviewed_label(feedback_id=f"fb-{i}", verdict="corrected")
            for i in range(3)
        ]
        pairs = build_training_pairs(labels)
        assert len(pairs) == 3


# ----------  load_intent_dataset  ----------


class TestLoadIntentDataset:
    """从 DatasetManifest 加载训练数据。"""

    def test_load_from_manifest_path(self, tmp_path) -> None:
        """从 manifest JSON 文件加载训练数据。"""
        from lora.intent_data import load_intent_dataset
        from serving.control.datasets import build_intent_dataset

        # 构建 manifest
        labels = [
            _make_reviewed_label(feedback_id=f"fb-{i}", verdict="corrected")
            for i in range(3)
        ]
        manifest = build_intent_dataset(labels, split_policy="same_template")

        # 写 manifest 文件
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False), encoding="utf-8"
        )

        # 加载训练数据
        pairs = load_intent_dataset(manifest_path)
        assert len(pairs) == 3
        for pair in pairs:
            assert "question" in pair
            assert "answer" in pair
            assert "loss_mask_prompt" in pair

    def test_load_empty_manifest(self, tmp_path) -> None:
        """空 manifest → 空列表。"""
        from lora.intent_data import load_intent_dataset

        manifest = {
            "schema_version": 1,
            "record_count": 0,
            "split_policy": "same_template",
            "content_digest": "a" * 64,
            "source_families": [],
            "has_unknown_source": False,
            "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "records": [],
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        pairs = load_intent_dataset(manifest_path)
        assert pairs == []
