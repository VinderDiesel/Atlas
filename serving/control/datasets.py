"""T12 反馈审核、归因与版本化数据集。

设计口径（dev-plan-0031 T12）
---------------------------
- 用户信号（反馈）、审核真值（ReviewedLabel）和模型输入（DatasetManifest）
  三者分离；审核到修复/训练的关联可追踪。
- FeedbackReviewService：审核/撤销/队列查询；reviewer 能力校验。
- build_intent_dataset：只接受 approved 记录；未批准记录硬拒（不静默过滤）。
- DatasetManifest：不可变（frozen），内容摘要绑定，标签更改使数据集失效。
- 来源族拆分：user / eval / synthetic / unknown。
- 旧反馈导入无来源信息则 source_family='unknown'，manifest 标记 has_unknown_source。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from serving.control.contracts import Owner, canonical_json

# ----------  枚举与常量  ----------

# reviewer 能力标识（与 serving/control/auth.py 的 CONTROL_GRANTS 对齐）。
# T12 在 workbench_support 中登记 reviewer 角色；此处只声明所需能力名。
_REVIEWER_CAPABILITY = "reviewer"

SourceFamily = Literal["user", "eval", "synthetic", "unknown"]
SplitPolicy = Literal["same_template", "cross_session", "minimal_cross_split"]


class ReviewDecision(str, Enum):  # noqa: UP042
    """审核决定（approved/rejected）；撤销由 revoke 方法单独处理。"""

    APPROVED = "approved"
    REJECTED = "rejected"


# ----------  ReviewedLabel：审核后的反馈标签  ----------


@dataclass(frozen=True)
class ReviewedLabel:
    """审核后的反馈标签：用户信号 + 审核证据 + 归因。

    与 FeedbackRecord 的区别：
    - FeedbackRecord 是持久化行（含 run_id、verdict、correction 等原始字段）。
    - ReviewedLabel 是训练就绪的标签（含 question、source_family、attribution_node）。
    - ReviewedLabel 只从 approved 的 FeedbackRecord 转换而来。
    """

    feedback_id: str
    run_id: str
    owner: Owner
    verdict: str
    status: Literal["approved", "rejected", "pending_review"]
    reviewed_by: Owner
    reviewed_at: str
    question: str
    source_family: SourceFamily
    attribution_node: str | None
    correction: dict[str, object] | None
    comment: str | None


# ----------  DatasetManifest：不可变版本化数据集  ----------


@dataclass(frozen=True)
class DatasetManifest:
    """不可变版本化数据集清单。

    内容摘要（content_digest）绑定记录集合；标签更改使摘要失效。
    source_families 记录包含的来源族集合；has_unknown_source 标记旧反馈。
    """

    schema_version: int
    record_count: int
    split_policy: str
    content_digest: str
    source_families: frozenset[str]
    has_unknown_source: bool
    created_at: str
    records: tuple[ReviewedLabel, ...] = field(repr=False)

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 兼容字典。"""
        return {
            "schema_version": self.schema_version,
            "record_count": self.record_count,
            "split_policy": self.split_policy,
            "content_digest": self.content_digest,
            "source_families": sorted(self.source_families),
            "has_unknown_source": self.has_unknown_source,
            "created_at": self.created_at,
            "records": [
                {
                    "feedback_id": r.feedback_id,
                    "run_id": r.run_id,
                    "owner": {"issuer": r.owner.issuer, "subject": r.owner.subject},
                    "verdict": r.verdict,
                    "status": r.status,
                    "reviewed_by": {
                        "issuer": r.reviewed_by.issuer,
                        "subject": r.reviewed_by.subject,
                    },
                    "reviewed_at": r.reviewed_at,
                    "question": r.question,
                    "source_family": r.source_family,
                    "attribution_node": r.attribution_node,
                    "correction": r.correction,
                    "comment": r.comment,
                }
                for r in self.records
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DatasetManifest:
        """从字典反序列化。"""
        records = tuple(
            ReviewedLabel(
                feedback_id=r["feedback_id"],
                run_id=r["run_id"],
                owner=Owner(issuer=r["owner"]["issuer"], subject=r["owner"]["subject"]),
                verdict=r["verdict"],
                status=r["status"],
                reviewed_by=Owner(
                    issuer=r["reviewed_by"]["issuer"], subject=r["reviewed_by"]["subject"]
                ),
                reviewed_at=r["reviewed_at"],
                question=r["question"],
                source_family=r["source_family"],
                attribution_node=r["attribution_node"],
                correction=r["correction"],
                comment=r["comment"],
            )
            for r in data["records"]
        )
        return cls(
            schema_version=data["schema_version"],
            record_count=data["record_count"],
            split_policy=data["split_policy"],
            content_digest=data["content_digest"],
            source_families=frozenset(data["source_families"]),
            has_unknown_source=data["has_unknown_source"],
            created_at=data["created_at"],
            records=records,
        )


# ----------  build_intent_dataset：approved → manifest  ----------


def build_intent_dataset(
    records: list[ReviewedLabel],
    *,
    split_policy: SplitPolicy,
    created_at: str | None = None,
) -> DatasetManifest:
    """从 approved 记录构建不可变版本化数据集。

    未批准记录硬拒（ValueError），不静默过滤后标满量导出。
    内容摘要由规范 JSON 的 SHA-256 计算，标签更改使摘要失效。

    Raises
    ------
    ValueError
        存在非 approved 记录时抛出。
    """
    # 硬拒未批准记录
    unapproved = [r for r in records if r.status != "approved"]
    if unapproved:
        ids = [r.feedback_id for r in unapproved[:5]]
        raise ValueError(
            f"build_intent_dataset 拒绝未批准记录（{len(unapproved)} 条，示例：{ids}）；"
            "未批准记录不得进入训练数据集"
        )

    source_families = frozenset(r.source_family for r in records)
    has_unknown = "unknown" in source_families

    # 内容摘要：按 feedback_id 排序后规范 JSON → SHA-256
    sorted_records = sorted(records, key=lambda r: r.feedback_id)
    # Owner 是 Pydantic 模型，需先转为 dict 再序列化
    serializable = [
        {
            "feedback_id": r.feedback_id,
            "run_id": r.run_id,
            "owner": {"issuer": r.owner.issuer, "subject": r.owner.subject},
            "verdict": r.verdict,
            "status": r.status,
            "reviewed_by": {"issuer": r.reviewed_by.issuer, "subject": r.reviewed_by.subject},
            "reviewed_at": r.reviewed_at,
            "question": r.question,
            "source_family": r.source_family,
            "attribution_node": r.attribution_node,
            "correction": r.correction,
            "comment": r.comment,
        }
        for r in sorted_records
    ]
    canonical = canonical_json(serializable)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    from datetime import UTC, datetime

    timestamp = created_at or datetime.now(UTC).isoformat(timespec="seconds")

    return DatasetManifest(
        schema_version=1,
        record_count=len(records),
        split_policy=split_policy,
        content_digest=digest,
        source_families=source_families,
        has_unknown_source=has_unknown,
        created_at=timestamp,
        records=tuple(sorted_records),
    )


# ----------  FeedbackReviewService：审核/撤销/队列  ----------


class FeedbackReviewService:
    """反馈审核服务：封装 store 的审核/撤销/队列操作，附加能力校验。

    reviewer 能力由调用方传入的 Owner 标识；服务层校验 subject 是否具有 reviewer 角色
    （通过 CONTROL_GRANTS 配置注入，此处简化为 subject == 'reviewer' 的约定）。
    """

    def __init__(self, store: object) -> None:
        """装配审核服务；store 为控制库（ControlStore 实例）。"""
        from serving.control.store import ControlStore

        if not isinstance(store, ControlStore):
            raise TypeError(f"store 必须是 ControlStore，收到 {type(store).__name__}")
        self._store = store

    def review(
        self,
        *,
        feedback_id: str,
        decision: ReviewDecision,
        reviewer: Owner,
    ) -> FeedbackRecord:
        """审核反馈：approved → training_eligible=True；rejected → False。

        Raises
        ------
        PermissionError
            reviewer 无审核能力。
        ValueError
            反馈已审核，不可重复审核。
        """
        self._require_reviewer(reviewer)
        return self._store.review_feedback(
            feedback_id, decision=decision.value, reviewer=reviewer
        )

    def revoke(self, *, feedback_id: str, revoker: Owner) -> FeedbackRecord:
        """撤销审核：回退到 pending_review，training_eligible=False。

        Raises
        ------
        PermissionError
            revoker 无审核能力。
        ValueError
            反馈尚未审核，无需撤销。
        """
        self._require_reviewer(revoker)
        return self._store.revoke_feedback(feedback_id, revoker=revoker)

    def list_pending(self) -> list[FeedbackRecord]:
        """审核队列：返回所有 pending_review 反馈。"""
        return self._store.list_pending_feedback()

    @staticmethod
    def _require_reviewer(owner: Owner) -> None:
        """能力校验：subject 必须具有 reviewer 角色。"""
        # 简化约定：subject == 'reviewer' 表示具有审核能力。
        # 生产环境应从 CONTROL_GRANTS 配置查询。
        if owner.subject != "reviewer":
            raise PermissionError(f"控制能力不足：无 {_REVIEWER_CAPABILITY!r} 能力")


# 避免循环导入的延迟类型引用
from serving.control.contracts import FeedbackRecord  # noqa: E402
