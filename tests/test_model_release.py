"""T15 模型注册、批准与回退契约测试。

覆盖：
- ModelVersion：模型版本数据结构
- ModelRegistry：register/approve/revoke/list
- 合同/许可/数据集撤销检查
- 未批准不能切换 active
- 可回退旧模型
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from serving.control.contracts import Owner

TZ = timezone(timedelta(hours=8))
PUBLISHER = Owner(issuer="test", subject="publisher")
REVIEWER = Owner(issuer="test", subject="reviewer")


def _make_training_report(
    *,
    job_id: str = "job-001",
    status: str = "succeeded",
    artifact_digest: str | None = "a" * 64,
    base_model: str = "Qwen/Qwen2.5-7B-Instruct",
) -> dict:
    """构建最小 TrainingReport 字典。"""
    return {
        "job_id": job_id,
        "status": status,
        "artifact_digest": artifact_digest,
        "base_model": base_model,
        "resource_report": {"gpu": "none", "elapsed_s": 0},
        "code_version": "abc123",
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
    }


# ----------  模型注册  ----------


class TestModelRegistryRegister:
    """ModelRegistry.register：训练报告 → 模型版本。"""

    def test_register_succeeded_report(self) -> None:
        """succeeded 训练报告 → ModelVersion（pending 状态）。"""
        from lora.registry import ModelRegistry

        registry = ModelRegistry()
        report = _make_training_report()
        version = registry.register(report)
        assert version.model_id.startswith("model-")
        assert version.status == "pending"
        assert version.base_model == "Qwen/Qwen2.5-7B-Instruct"

    def test_register_blocked_report_rejected(self) -> None:
        """blocked 训练报告 → 拒绝注册（不产生模型版本）。"""
        from lora.registry import ModelRegistry

        registry = ModelRegistry()
        report = _make_training_report(status="blocked", artifact_digest=None)
        with pytest.raises(ValueError, match="blocked|failed|artifact"):
            registry.register(report)

    def test_register_no_artifact_rejected(self) -> None:
        """无 artifact_digest → 拒绝注册。"""
        from lora.registry import ModelRegistry

        registry = ModelRegistry()
        report = _make_training_report(artifact_digest=None)
        with pytest.raises(ValueError, match="artifact"):
            registry.register(report)

    def test_register_unlicensed_model_rejected(self) -> None:
        """未许可基座模型 → 拒绝注册。"""
        from lora.registry import ModelRegistry

        registry = ModelRegistry()
        report = _make_training_report(base_model="SomeCompany/ProprietaryModel")
        with pytest.raises(ValueError, match="白名单|licen"):
            registry.register(report)


# ----------  模型批准  ----------


class TestModelRegistryApprove:
    """ModelRegistry.approve：人工批准模型（不切 active）。"""

    def test_approve_pending_model(self) -> None:
        """pending 模型 → approved（需证据 + 批准人）。"""
        from lora.registry import ModelRegistry

        registry = ModelRegistry()
        report = _make_training_report()
        version = registry.register(report)

        approved = registry.approve(
            model_id=version.model_id,
            evidence_ids=["eval-001"],
            principal=PUBLISHER,
        )
        assert approved.status == "approved"

    def test_approve_without_evidence_rejected(self) -> None:
        """无证据 → 拒绝批准。"""
        from lora.registry import ModelRegistry

        registry = ModelRegistry()
        report = _make_training_report()
        version = registry.register(report)

        with pytest.raises(ValueError, match="批准|approved|evidence"):
            registry.approve(
                model_id=version.model_id,
                evidence_ids=[],
                principal=PUBLISHER,
            )

    def test_approve_without_principal_rejected(self) -> None:
        """无批准人 → 拒绝批准。"""
        from lora.registry import ModelRegistry

        registry = ModelRegistry()
        report = _make_training_report()
        version = registry.register(report)

        with pytest.raises((ValueError, PermissionError), match="批准|principal|approved"):
            registry.approve(
                model_id=version.model_id,
                evidence_ids=["eval-001"],
                principal=None,
            )

    def test_approve_nonexistent_model_raises(self) -> None:
        """批准不存在的模型 → KeyError。"""
        from lora.registry import ModelRegistry

        registry = ModelRegistry()
        with pytest.raises(KeyError):
            registry.approve(
                model_id="nonexistent",
                evidence_ids=["eval-001"],
                principal=PUBLISHER,
            )


# ----------  模型撤销  ----------


class TestModelRegistryRevoke:
    """ModelRegistry.revoke：撤销已批准模型。"""

    def test_revoke_approved_model(self) -> None:
        """approved 模型可撤销 → revoked。"""
        from lora.registry import ModelRegistry

        registry = ModelRegistry()
        report = _make_training_report()
        version = registry.register(report)
        registry.approve(
            model_id=version.model_id,
            evidence_ids=["eval-001"],
            principal=PUBLISHER,
        )

        revoked = registry.revoke(model_id=version.model_id, reason="数据问题")
        assert revoked.status == "revoked"

    def test_revoke_pending_model_rejected(self) -> None:
        """pending 模型不可撤销（必须先批准再撤销）。"""
        from lora.registry import ModelRegistry

        registry = ModelRegistry()
        report = _make_training_report()
        version = registry.register(report)

        with pytest.raises(ValueError, match="approved|批准"):
            registry.revoke(model_id=version.model_id, reason="测试")


# ----------  模型查询  ----------


class TestModelRegistryList:
    """ModelRegistry.list_models：查询模型版本。"""

    def test_list_empty(self) -> None:
        """空注册表 → 空列表。"""
        from lora.registry import ModelRegistry

        registry = ModelRegistry()
        assert registry.list_models() == []

    def test_list_after_register(self) -> None:
        """注册后可查询。"""
        from lora.registry import ModelRegistry

        registry = ModelRegistry()
        report = _make_training_report()
        registry.register(report)
        models = registry.list_models()
        assert len(models) == 1
        assert models[0].status == "pending"

    def test_get_model(self) -> None:
        """按 ID 查询模型。"""
        from lora.registry import ModelRegistry

        registry = ModelRegistry()
        report = _make_training_report()
        version = registry.register(report)
        fetched = registry.get_model(version.model_id)
        assert fetched.model_id == version.model_id

    def test_get_nonexistent_raises(self) -> None:
        """查询不存在模型 → KeyError。"""
        from lora.registry import ModelRegistry

        registry = ModelRegistry()
        with pytest.raises(KeyError):
            registry.get_model("nonexistent")
