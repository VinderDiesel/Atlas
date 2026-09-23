"""T15 模型注册与版本管理。

职责
----
- ModelVersion：模型版本（基座/adapter 摘要/状态/证据/许可）。
- ModelRegistry：register/approve/revoke/list/get。
- 合同/许可/数据集撤销检查。
- 未批准模型不能切换 active。

红线
----
- 失败/阻塞的训练报告不注册。
- 无 artifact_digest 不注册。
- 未许可基座模型不注册。
- 撤销已批准模型后不可再批准。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from serving.control.contracts import Owner

TZ = timezone(timedelta(hours=8))

# 模型许可白名单（与 intent_train.py 一致）
_LICENSED_BASE_MODELS: frozenset[str] = frozenset(
    {
        "Qwen/Qwen2.5-7B-Instruct",  # Apache-2.0
        "Qwen/Qwen2.5-3B-Instruct",  # Apache-2.0（降级路径）
    }
)


@dataclass
class ModelVersion:
    """模型版本（不可变身份，可变状态）。

    状态机：pending → approved → revoked
    - pending：注册后等待人工审核。
    - approved：人工批准后，可被影子/发布使用。
    - revoked：撤销后不可再批准。
    """

    model_id: str
    base_model: str
    adapter_digest: str
    training_job_id: str
    dataset_digest: str | None
    status: str  # 'pending' | 'approved' | 'revoked'
    evidence_ids: list[str] = field(default_factory=list)
    approved_by: Owner | None = None
    approved_at: str | None = None
    revoke_reason: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(TZ).isoformat(timespec="seconds"))


class ModelRegistry:
    """模型注册表：管理模型版本的注册、批准与撤销。

    线程安全：内存实现，单进程。生产环境应持久化到控制库。
    """

    def __init__(self) -> None:
        self._models: dict[str, ModelVersion] = {}

    def register(self, report: dict[str, Any]) -> ModelVersion:
        """从训练报告注册模型版本。

        Parameters
        ----------
        report : dict
            TrainingReport 字典（status='succeeded' + artifact_digest 非空）。

        Raises
        ------
        ValueError
            报告状态非 succeeded / 无 artifact / 基座未许可。
        """
        # 校验报告状态
        status = report.get("status", "")
        if status not in ("succeeded",):
            raise ValueError(
                f"训练报告状态 {status!r} 不可注册（只有 succeeded 可注册）"
            )

        # 校验 artifact
        artifact_digest = report.get("artifact_digest")
        if not artifact_digest:
            raise ValueError("训练报告无 artifact_digest，不可注册")

        # 校验基座许可
        base_model = report.get("base_model", "")
        if base_model not in _LICENSED_BASE_MODELS:
            raise ValueError(
                f"基座模型 {base_model!r} 不在许可白名单；"
                f"白名单：{sorted(_LICENSED_BASE_MODELS)}"
            )

        model_id = f"model-{uuid.uuid4().hex[:12]}"
        version = ModelVersion(
            model_id=model_id,
            base_model=base_model,
            adapter_digest=artifact_digest,
            training_job_id=report.get("job_id", ""),
            dataset_digest=report.get("dataset_digest"),
            status="pending",
        )
        self._models[model_id] = version
        return version

    def approve(
        self,
        *,
        model_id: str,
        evidence_ids: list[str],
        principal: Owner | None,
    ) -> ModelVersion:
        """人工批准模型（不切 active）。

        Parameters
        ----------
        model_id : str
            模型版本 ID。
        evidence_ids : list[str]
            评测证据 ID 列表（不可为空）。
        principal : Owner | None
            批准人（不可为 None）。

        Raises
        ------
        KeyError
            模型不存在。
        ValueError
            无证据 / 无批准人 / 模型状态非 pending。
        """
        if model_id not in self._models:
            raise KeyError(f"模型 {model_id!r} 不存在")

        if not evidence_ids:
            raise ValueError("批准必须提供至少一条评测证据（evidence_ids 不可为空）")

        if principal is None:
            raise ValueError("批准必须指定批准人（principal 不可为 None）")

        version = self._models[model_id]
        if version.status != "pending":
            raise ValueError(
                f"模型 {model_id!r} 状态为 {version.status!r}，只有 pending 可批准"
            )

        version.status = "approved"
        version.evidence_ids = list(evidence_ids)
        version.approved_by = principal
        version.approved_at = datetime.now(TZ).isoformat(timespec="seconds")
        return version

    def revoke(self, *, model_id: str, reason: str) -> ModelVersion:
        """撤销已批准模型。

        Parameters
        ----------
        model_id : str
            模型版本 ID。
        reason : str
            撤销原因。

        Raises
        ------
        KeyError
            模型不存在。
        ValueError
            模型状态非 approved。
        """
        if model_id not in self._models:
            raise KeyError(f"模型 {model_id!r} 不存在")

        version = self._models[model_id]
        if version.status != "approved":
            raise ValueError(
                f"模型 {model_id!r} 状态为 {version.status!r}，只有 approved 可撤销"
            )

        version.status = "revoked"
        version.revoke_reason = reason
        return version

    def get_model(self, model_id: str) -> ModelVersion:
        """按 ID 查询模型。

        Raises
        ------
        KeyError
            模型不存在。
        """
        if model_id not in self._models:
            raise KeyError(f"模型 {model_id!r} 不存在")
        return self._models[model_id]

    def list_models(self) -> list[ModelVersion]:
        """返回所有模型版本（按创建时间倒序）。"""
        return sorted(
            self._models.values(),
            key=lambda m: m.created_at,
            reverse=True,
        )

    def get_approved_ids(self) -> frozenset[str]:
        """返回所有已批准模型 ID 集合。"""
        return frozenset(m.model_id for m in self._models.values() if m.status == "approved")
