"""最小反馈采集（ADR-0031 D13）：提交与本人列表；跨用户审核队列属 T12。

授权口径（与运行视图同源的「统一不可见投影」）
--------------------------------------------
- 能力先判：无 `feedback.submit` 一律 403（对象是否存在不作为响应差异）；
- 对象级：仅本人且 `run.scope ∈ principal.scopes` 可见，否则与「不存在」同为
  404——拒绝越权关联（D13），不泄露他人运行的存在性；
- 未审核固定 pending_review / training_eligible=False（T12 前不开放训练），
  写入由 ControlStore.insert_feedback 单一实现，服务层不复制状态逻辑。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from serving.control.auth import ControlForbidden, Principal
from serving.control.contracts import FeedbackRecord, FeedbackRequest, Owner
from serving.control.runs import RunNotFound

if TYPE_CHECKING:
    from serving.control.contracts import RunRecord
    from serving.control.store import ControlStore

_ACTION = "feedback.submit"


class FeedbackService:
    """反馈提交/本人列表服务：能力、归属与作用域在此收敛（HTTP 投影在 router）。"""

    def __init__(self, store: ControlStore) -> None:
        """装配反馈服务；store 为控制库（run 事实与反馈行的唯一持久层）。"""
        self._store = store

    def submit(self, *, principal: Principal, request: FeedbackRequest) -> FeedbackRecord:
        """采集最小反馈；越权关联与未知 run 同为 RunNotFound（404 统一投影）。

        Raises
        ------
        ControlForbidden : 无 feedback.submit 能力
        RunNotFound : run 不存在 / 非本人 / 作用域外（不透露差异）
        """
        self._require_capability(principal)
        record = self._visible_run(principal, request.run_id)
        return self._store.insert_feedback(
            record.run_id,
            owner=Owner(issuer=principal.issuer, subject=principal.subject),
            verdict=request.verdict,
            comment=request.comment,
            correction=request.correction,
        )

    def list_own(self, *, principal: Principal) -> list[FeedbackRecord]:
        """本人反馈列表（新→旧）；不暴露他人的行（审核队列属 T12）。

        Raises
        ------
        ControlForbidden : 无 feedback.submit 能力
        """
        self._require_capability(principal)
        return self._store.list_feedback(
            owner=Owner(issuer=principal.issuer, subject=principal.subject)
        )

    @staticmethod
    def _require_capability(principal: Principal) -> None:
        """能力先于对象判定：无能力一律 403，不让「对象存在性」进入响应差异。"""
        if _ACTION not in principal.capabilities:
            raise ControlForbidden(f"控制能力不足：{_ACTION!r}")

    def _visible_run(self, principal: Principal, run_id: str) -> RunRecord:
        """取本人且作用域内的 run；其余（含不存在）一律 RunNotFound。"""
        try:
            record = self._store.get_run(run_id)
        except KeyError as exc:
            raise RunNotFound(run_id) from exc
        is_owner = (
            record.owner.issuer == principal.issuer and record.owner.subject == principal.subject
        )
        if not is_owner or record.scope not in principal.scopes:
            raise RunNotFound(run_id)
        return record
