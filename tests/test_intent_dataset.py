"""T12 版本化数据集契约测试——DatasetManifest 与 build_intent_dataset。

口径（与 dev-plan-0031 T12 逐条对应）：
- build_intent_dataset 只接受 approved 记录；未批准记录硬拒（不静默过滤）。
- DatasetManifest 不可变：内容摘要绑定，标签更改使数据集失效。
- 来源族拆分：user（用户反馈）vs eval（评测失败）vs synthetic（蒸馏）。
- 用途授权：manifest 声明 purpose，消费方必须匹配。
- 旧反馈导入无来源信息则明确不可训练。
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ----------  T12b 红测：版本化数据集  ----------


class TestBuildIntentDataset:
    """build_intent_dataset：approved 记录 → 不可变 manifest。"""

    def test_unapproved_records_hard_rejected(self, tmp_path: Path) -> None:
        """红测核心：未批准记录硬拒（ValueError），不静默过滤后标满量导出。"""
        from serving.control.contracts import Owner
        from serving.control.datasets import build_intent_dataset

        owner = Owner(issuer="atlas-local", subject="reviewer")
        # 构造一条 pending 状态的 ReviewedLabel
        pending_label = _make_label(status="pending_review", owner=owner)

        with pytest.raises(ValueError, match="unapproved|未批准"):
            build_intent_dataset([pending_label], split_policy="same_template")

    def test_approved_only_builds_manifest(self, tmp_path: Path) -> None:
        """全部 approved → 成功构建 manifest，含内容摘要。"""
        from serving.control.contracts import Owner
        from serving.control.datasets import build_intent_dataset

        owner = Owner(issuer="atlas-local", subject="reviewer")
        labels = [
            _make_label(feedback_id="fb-1", status="approved", owner=owner, question="问题 A"),
            _make_label(feedback_id="fb-2", status="approved", owner=owner, question="问题 B"),
        ]
        manifest = build_intent_dataset(labels, split_policy="same_template")
        assert manifest.record_count == 2
        assert manifest.split_policy == "same_template"
        assert manifest.content_digest  # 非空
        assert manifest.schema_version == 1

    def test_mixed_approved_and_rejected_raises(self, tmp_path: Path) -> None:
        """混合 approved + rejected → 硬拒（rejected 不是 approved）。"""
        from serving.control.contracts import Owner
        from serving.control.datasets import build_intent_dataset

        owner = Owner(issuer="atlas-local", subject="reviewer")
        labels = [
            _make_label(feedback_id="fb-1", status="approved", owner=owner),
            _make_label(feedback_id="fb-2", status="rejected", owner=owner),
        ]
        with pytest.raises(ValueError, match="unapproved|未批准"):
            build_intent_dataset(labels, split_policy="same_template")

    def test_manifest_is_immutable(self, tmp_path: Path) -> None:
        """manifest 不可变：frozen dataclass，字段不可修改。"""
        from serving.control.contracts import Owner
        from serving.control.datasets import build_intent_dataset

        owner = Owner(issuer="atlas-local", subject="reviewer")
        labels = [_make_label(status="approved", owner=owner)]
        manifest = build_intent_dataset(labels, split_policy="same_template")

        with pytest.raises(AttributeError, match="cannot assign|cannot set|frozen"):
            manifest.record_count = 999  # type: ignore[misc]

    def test_manifest_content_digest_changes_on_label_change(self, tmp_path: Path) -> None:
        """标签更改使数据集摘要失效（内容摘要不同）。"""
        from serving.control.contracts import Owner
        from serving.control.datasets import build_intent_dataset

        owner = Owner(issuer="atlas-local", subject="reviewer")
        labels_v1 = [_make_label(feedback_id="fb-1", status="approved", owner=owner, question="A")]
        labels_v2 = [_make_label(feedback_id="fb-1", status="approved", owner=owner, question="B")]
        m1 = build_intent_dataset(labels_v1, split_policy="same_template")
        m2 = build_intent_dataset(labels_v2, split_policy="same_template")
        assert m1.content_digest != m2.content_digest

    def test_source_family_split(self, tmp_path: Path) -> None:
        """来源族拆分：manifest 记录每条记录的来源族。"""
        from serving.control.contracts import Owner
        from serving.control.datasets import build_intent_dataset

        owner = Owner(issuer="atlas-local", subject="reviewer")
        labels = [
            _make_label(feedback_id="fb-1", status="approved", owner=owner, source_family="user"),
            _make_label(
                feedback_id="fb-2", status="approved", owner=owner, source_family="eval"
            ),
        ]
        manifest = build_intent_dataset(labels, split_policy="same_template")
        assert manifest.source_families == {"user", "eval"}

    def test_old_feedback_without_source_not_trainable(self, tmp_path: Path) -> None:
        """旧反馈导入无来源信息 → 明确标记不可训练（source_family='unknown'）。"""
        from serving.control.contracts import Owner
        from serving.control.datasets import ReviewedLabel, build_intent_dataset

        owner = Owner(issuer="atlas-local", subject="reviewer")
        # 无来源信息的旧反馈
        old_label = ReviewedLabel(
            feedback_id="old-fb",
            run_id="run-1",
            owner=owner,
            verdict="up",
            status="approved",
            reviewed_by=owner,
            reviewed_at="2026-09-23T12:00:00+00:00",
            question="旧问题",
            source_family="unknown",
            attribution_node=None,
            correction=None,
            comment=None,
        )
        # unknown 来源的 approved 记录仍然可以构建数据集，但 manifest 标记
        manifest = build_intent_dataset([old_label], split_policy="same_template")
        assert "unknown" in manifest.source_families
        assert manifest.has_unknown_source is True


class TestDatasetManifestPersistence:
    """manifest 持久化与版本追踪。"""

    def test_manifest_can_be_written_and_read(self, tmp_path: Path) -> None:
        """manifest 可序列化为 JSON 并反序列化。"""
        import json

        from serving.control.contracts import Owner
        from serving.control.datasets import build_intent_dataset

        owner = Owner(issuer="atlas-local", subject="reviewer")
        labels = [_make_label(status="approved", owner=owner)]
        manifest = build_intent_dataset(labels, split_policy="same_template")

        # 序列化
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False), encoding="utf-8")

        # 反序列化
        from serving.control.datasets import DatasetManifest

        restored = DatasetManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))
        assert restored.content_digest == manifest.content_digest
        assert restored.record_count == manifest.record_count


# ----------  辅助函数  ----------


def _make_label(
    *,
    feedback_id: str = "fb-test",
    status: str = "approved",
    owner: object = None,
    question: str = "测试问题",
    source_family: str = "user",
) -> object:
    """构造 ReviewedLabel 测试夹具。"""
    from serving.control.contracts import Owner
    from serving.control.datasets import ReviewedLabel

    if owner is None:
        owner = Owner(issuer="atlas-local", subject="reviewer")
    return ReviewedLabel(
        feedback_id=feedback_id,
        run_id="run-test",
        owner=owner,
        verdict="up",
        status=status,
        reviewed_by=owner,
        reviewed_at="2026-09-23T12:00:00+00:00",
        question=question,
        source_family=source_family,
        attribution_node=None,
        correction=None,
        comment=None,
    )
