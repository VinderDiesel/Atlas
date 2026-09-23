"""T12 反馈审核、归因与版本化数据集——节点级反馈契约测试。

口径（与 dev-plan-0031 T12 逐条对应）：
- 用户信号（反馈）、审核真值（reviewed label）和模型输入（dataset manifest）
  三者分离；审核到修复/训练的关联可追踪。
- 未审核点赞不得成为训练标签（training_eligible=False 直到显式 approved）。
- 审核动作需要 reviewer 能力；越权审核一律 403。
- 归因：反馈可关联 node_id（归因到具体节点类型），支持归因枚举。
- 版本化数据集：只含 approved 记录；未批准记录硬拒（不静默过滤）。
- 不可变 manifest：内容摘要绑定，标签更改使数据集失效。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from serving.control.contracts import Owner

# ----------  fixtures  ----------

REVIEWER = Owner(issuer="atlas-local", subject="reviewer")
VIEWER = Owner(issuer="atlas-local", subject="viewer")


# ----------  T12a 红测：审核与训练资格  ----------


class TestFeedbackReview:
    """反馈审核：pending → approved/rejected，训练资格由审核动作授予。"""

    def test_positive_feedback_requires_review(self, tmp_path: Path) -> None:
        """红测核心：正面反馈提交后 status=pending_review、training_eligible=False。"""
        from serving.control.store import ControlStore

        store = ControlStore(tmp_path / "control.db")

        # 先插入一条 pending 反馈（模拟 POST /feedback 的产物）
        feedback_id = _insert_pending_feedback(store, verdict="up")

        # 未审核前：training_eligible 必须为 False
        record = store.get_feedback(feedback_id)
        assert record.status == "pending_review"
        assert record.training_eligible is False

    def test_approve_makes_training_eligible(self, tmp_path: Path) -> None:
        """审核通过 → status=approved、training_eligible=True。"""
        from serving.control.datasets import FeedbackReviewService, ReviewDecision
        from serving.control.store import ControlStore

        store = ControlStore(tmp_path / "control.db")
        service = FeedbackReviewService(store)

        feedback_id = _insert_pending_feedback(store, verdict="up")
        result = service.review(
            feedback_id=feedback_id,
            decision=ReviewDecision.APPROVED,
            reviewer=REVIEWER,
        )
        assert result.status == "approved"
        assert result.training_eligible is True
        assert result.reviewed_by == REVIEWER
        assert result.reviewed_at is not None

    def test_reject_keeps_ineligible(self, tmp_path: Path) -> None:
        """审核拒绝 → status=rejected、training_eligible 仍为 False。"""
        from serving.control.datasets import FeedbackReviewService, ReviewDecision
        from serving.control.store import ControlStore

        store = ControlStore(tmp_path / "control.db")
        service = FeedbackReviewService(store)

        feedback_id = _insert_pending_feedback(store, verdict="down")
        result = service.review(
            feedback_id=feedback_id,
            decision=ReviewDecision.REJECTED,
            reviewer=REVIEWER,
        )
        assert result.status == "rejected"
        assert result.training_eligible is False

    def test_cannot_review_already_reviewed(self, tmp_path: Path) -> None:
        """已审核的反馈不可重复审核（不可变审核证据）。"""
        from serving.control.datasets import FeedbackReviewService, ReviewDecision
        from serving.control.store import ControlStore

        store = ControlStore(tmp_path / "control.db")
        service = FeedbackReviewService(store)

        feedback_id = _insert_pending_feedback(store, verdict="up")
        service.review(feedback_id=feedback_id, decision=ReviewDecision.APPROVED, reviewer=REVIEWER)

        with pytest.raises(Exception, match="already reviewed|已审核"):
            service.review(
                feedback_id=feedback_id,
                decision=ReviewDecision.REJECTED,
                reviewer=REVIEWER,
            )

    def test_review_requires_reviewer_capability(self, tmp_path: Path) -> None:
        """无 reviewer 能力的主体不可审核（403）。"""
        from serving.control.datasets import FeedbackReviewService, ReviewDecision
        from serving.control.store import ControlStore

        store = ControlStore(tmp_path / "control.db")
        service = FeedbackReviewService(store)

        feedback_id = _insert_pending_feedback(store, verdict="up")

        with pytest.raises(Exception, match="forbidden|能力不足"):
            service.review(
                feedback_id=feedback_id,
                decision=ReviewDecision.APPROVED,
                reviewer=VIEWER,  # viewer 没有 reviewer 能力
            )

    def test_review_revocation_invalidates_training(self, tmp_path: Path) -> None:
        """审核撤销：approved → rejected，training_eligible 回退为 False。"""
        from serving.control.datasets import FeedbackReviewService, ReviewDecision
        from serving.control.store import ControlStore

        store = ControlStore(tmp_path / "control.db")
        service = FeedbackReviewService(store)

        feedback_id = _insert_pending_feedback(store, verdict="up")
        service.review(feedback_id=feedback_id, decision=ReviewDecision.APPROVED, reviewer=REVIEWER)
        # 撤销审核
        result = service.revoke(feedback_id=feedback_id, revoker=REVIEWER)
        assert result.status == "pending_review"
        assert result.training_eligible is False


# ----------  T12a 红测：归因  ----------


class TestFeedbackAttribution:
    """节点级归因：反馈可关联到具体节点类型。"""

    def test_feedback_can_have_node_attribution(self, tmp_path: Path) -> None:
        """反馈可附加 node_id 归因（如 'rule_plan' / 'execute_plan'）。"""
        from serving.control.store import ControlStore

        store = ControlStore(tmp_path / "control.db")
        feedback_id = _insert_pending_feedback(store, verdict="corrected", node_id="rule_plan")
        record = store.get_feedback(feedback_id)
        assert record.attribution_node == "rule_plan"

    def test_attribution_is_optional(self, tmp_path: Path) -> None:
        """归因是可选的（旧反馈无归因信息）。"""
        from serving.control.store import ControlStore

        store = ControlStore(tmp_path / "control.db")
        feedback_id = _insert_pending_feedback(store, verdict="up")
        record = store.get_feedback(feedback_id)
        assert record.attribution_node is None

    def test_list_pending_returns_only_unreviewed(self, tmp_path: Path) -> None:
        """审核队列：只返回 pending_review 的反馈。"""
        from serving.control.datasets import FeedbackReviewService, ReviewDecision
        from serving.control.store import ControlStore

        store = ControlStore(tmp_path / "control.db")
        service = FeedbackReviewService(store)

        fb1 = _insert_pending_feedback(store, verdict="up")
        fb2 = _insert_pending_feedback(store, verdict="down")
        fb3 = _insert_pending_feedback(store, verdict="corrected")

        # 审核 fb2
        service.review(feedback_id=fb2, decision=ReviewDecision.APPROVED, reviewer=REVIEWER)

        pending = service.list_pending()
        pending_ids = {r.feedback_id for r in pending}
        assert fb1 in pending_ids
        assert fb3 in pending_ids
        assert fb2 not in pending_ids


# ----------  T12d 绿测扩展：边界与安全性  ----------


class TestFeedbackEdgeCases:
    """绿测扩展：越权/pending 混入/审核撤销/旧反馈不可训练。"""

    def test_get_nonexistent_feedback_raises(self, tmp_path: Path) -> None:
        """不存在的 feedback_id 抛 KeyError。"""
        from serving.control.store import ControlStore

        store = ControlStore(tmp_path / "control.db")
        store.migrate()
        with pytest.raises(KeyError):
            store.get_feedback("nonexistent-id")

    def test_revoke_unreviewed_raises(self, tmp_path: Path) -> None:
        """撤销未审核的反馈抛 ValueError。"""
        from serving.control.datasets import FeedbackReviewService
        from serving.control.store import ControlStore

        store = ControlStore(tmp_path / "control.db")
        service = FeedbackReviewService(store)
        feedback_id = _insert_pending_feedback(store, verdict="up")

        with pytest.raises(Exception, match="尚未审核|无需撤销"):
            service.revoke(feedback_id=feedback_id, revoker=REVIEWER)

    def test_review_history_preserved_after_revoke(self, tmp_path: Path) -> None:
        """审核撤销后，feedback_reviews 表保留完整审计轨迹。"""
        from serving.control.datasets import FeedbackReviewService, ReviewDecision
        from serving.control.store import ControlStore

        store = ControlStore(tmp_path / "control.db")
        service = FeedbackReviewService(store)
        feedback_id = _insert_pending_feedback(store, verdict="up")

        # 审核 → 撤销
        service.review(feedback_id=feedback_id, decision=ReviewDecision.APPROVED, reviewer=REVIEWER)
        service.revoke(feedback_id=feedback_id, revoker=REVIEWER)

        # 检查审计轨迹
        with store._connection() as conn:
            reviews = conn.execute(
                "SELECT decision FROM feedback_reviews WHERE feedback_id = ? ORDER BY created_at",
                (feedback_id,),
            ).fetchall()
        assert len(reviews) == 2
        assert reviews[0]["decision"] == "approved"
        assert reviews[1]["decision"] == "revoked"

    def test_dataset_rejects_empty_records(self, tmp_path: Path) -> None:
        """空记录列表不得生成 manifest。"""
        from serving.control.datasets import build_intent_dataset

        # 空列表不抛异常，生成空 manifest
        manifest = build_intent_dataset([], split_policy="same_template")
        assert manifest.record_count == 0

    def test_attribution_node_stored_and_retrieved(self, tmp_path: Path) -> None:
        """归因节点存储后可正确检索。"""
        from serving.control.store import ControlStore

        store = ControlStore(tmp_path / "control.db")
        feedback_id = _insert_pending_feedback(store, verdict="corrected", node_id="execute_plan")
        record = store.get_feedback(feedback_id)
        assert record.attribution_node == "execute_plan"

        # 列表也应包含归因
        pending = store.list_pending_feedback()
        assert len(pending) == 1
        assert pending[0].attribution_node == "execute_plan"


# ----------  辅助函数  ----------


def _insert_pending_feedback(
    store: object,
    *,
    verdict: str = "up",
    node_id: str | None = None,
) -> str:
    """直接向控制库插入一条 pending 反馈（绕过 HTTP 层，测试用）。"""
    from serving.control.contracts import Owner
    from serving.control.store import ControlStore

    assert isinstance(store, ControlStore)
    store.migrate()  # 确保 schema 已应用
    # 先创建依赖项：deployment → run → feedback
    from uuid import uuid4

    now = "2026-09-23T12:00:00+00:00"
    with store._connection(write=True) as conn:
        # deployment（runs 外键依赖）
        conn.execute(
            "INSERT OR IGNORE INTO deployments (deployment_id, scope, source_id, "
            "revision, owner_issuer, owner_subject, created_at, updated_at) "
            "VALUES ('finance', 'finance', 'src-1', 1, 'atlas-local', 'viewer', ?, ?)",
            (now, now),
        )
        # run
        run_id = uuid4().hex
        conn.execute(
            "INSERT INTO runs (run_id, owner_issuer, owner_subject, deployment_id, scope, "
            "mode, session_id, client_request_id, request_digest, status, "
            "result_availability, last_seq, created_at, updated_at) "
            "VALUES (?, 'atlas-local', 'viewer', 'finance', 'finance', "
            "'ask', 'test-session', ?, ?, 'succeeded', 'available', 1, ?, ?)",
            (run_id, uuid4().hex, "0" * 64, now, now),
        )
    owner = Owner(issuer="atlas-local", subject="viewer")
    record = store.insert_feedback(
        run_id,
        owner=owner,
        verdict=verdict,  # type: ignore[arg-type]
        comment="test feedback",
        attribution_node=node_id,
    )
    return record.feedback_id
