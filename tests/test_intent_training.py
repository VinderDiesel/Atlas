"""T14 intent_v1 SFT 训练预检与任务管理契约测试。

覆盖：
- validate_training_job：预算/CUDA/数据/依赖/许可预检
- train_intent：端到端训练（CPU 路径只跑预检）
- TrainingJobManager：任务状态/取消/幂等/预算不重复扣
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from serving.control.contracts import Owner

TZ = timezone(timedelta(hours=8))
REVIEWER = Owner(issuer="test", subject="reviewer")


# ----------  辅助工厂  ----------


def _make_manifest(
    *,
    record_count: int = 5,
    content_digest: str = "a" * 64,
    path: Path | None = None,
) -> dict[str, Any]:
    """构建最小 DatasetManifest 字典。"""
    return {
        "schema_version": 1,
        "record_count": record_count,
        "split_policy": "same_template",
        "content_digest": content_digest,
        "source_families": ["user"],
        "has_unknown_source": False,
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "records": [],
        "path": str(path) if path else None,
    }


def _make_reviewed_label_dict(feedback_id: str = "fb-001") -> dict[str, Any]:
    """构建最小 ReviewedLabel 字典（用于 manifest records）。"""
    return {
        "feedback_id": feedback_id,
        "run_id": "run-001",
        "owner": {"issuer": "test", "subject": "user-1"},
        "verdict": "corrected",
        "status": "approved",
        "reviewed_by": {"issuer": "test", "subject": "reviewer"},
        "reviewed_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "question": "2013年第二季度总交易额",
        "source_family": "user",
        "attribution_node": None,
        "correction": {"metric": "total_amount", "dimensions": []},
        "comment": None,
    }


def _write_manifest_file(
    tmp_path: Path, record_count: int = 5, *, with_records: bool = True
) -> Path:
    """写 manifest JSON 文件并返回路径。

    with_records=True 时填充真实 ReviewedLabel 字典（防 empty_dataset 短路）。
    """
    records = (
        [_make_reviewed_label_dict(f"fb-{i}") for i in range(record_count)]
        if with_records
        else []
    )
    manifest = {
        "schema_version": 1,
        "record_count": record_count,
        "split_policy": "same_template",
        "content_digest": "a" * 64,
        "source_families": ["user"],
        "has_unknown_source": False,
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "records": records,
        "path": None,
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def _make_spec(**overrides: Any) -> Any:
    """构建 TrainingSpec，默认 budget_approval=None（未批准）。"""
    from lora.intent_train import TrainingSpec

    defaults = {
        "job_id": "job-001",
        "dataset_manifest_path": None,
        "base_model": "Qwen/Qwen2.5-7B-Instruct",
        "base_model_revision": "abc123",
        "recipe_path": None,
        "budget_approval_id": None,
        "budget_approved": False,
        "output_dir": None,
    }
    defaults.update(overrides)
    return TrainingSpec(**defaults)


# ----------  红测：预算批准  ----------


class TestBudgetPreflight:
    """无预算批准不启动训练，即使 CUDA 可见。"""

    def test_training_needs_explicit_budget(self, tmp_path: Path) -> None:
        """预算未批准 → blocked + budget_not_approved。"""
        from lora.intent_train import validate_training_job

        manifest_path = _write_manifest_file(tmp_path)
        spec = _make_spec(dataset_manifest_path=manifest_path)
        assert spec.budget_approved is False
        result = validate_training_job(spec)
        assert result.status == "blocked"
        assert "budget_not_approved" in result.blocked_reasons

    def test_budget_approved_cuda_still_blocks(self, tmp_path: Path) -> None:
        """预算批准但无 CUDA → blocked + no_cuda/no_torch（不因预算通过而跳过后续检查）。"""
        from lora.intent_train import validate_training_job

        manifest_path = _write_manifest_file(tmp_path)
        spec = _make_spec(
            dataset_manifest_path=manifest_path,
            budget_approved=True,
            budget_approval_id="approval-001",
        )
        result = validate_training_job(spec)
        # CPU 环境必然无 CUDA 或无 torch
        assert result.status == "blocked"
        assert "no_cuda" in result.blocked_reasons or "no_torch" in result.blocked_reasons


# ----------  红测：数据校验  ----------


class TestDataPreflight:
    """数据缺失/为空/不可达 → blocked。"""

    def test_missing_dataset_blocked(self) -> None:
        """manifest 路径不存在 → blocked + dataset_not_found。"""
        from lora.intent_train import validate_training_job

        spec = _make_spec(
            dataset_manifest_path=Path("/nonexistent/manifest.json"),
            budget_approved=True,
            budget_approval_id="a",
        )
        result = validate_training_job(spec)
        assert result.status == "blocked"
        assert "dataset_not_found" in result.blocked_reasons

    def test_empty_dataset_blocked(self, tmp_path: Path) -> None:
        """manifest record_count=0 → blocked + empty_dataset。"""
        from lora.intent_train import validate_training_job

        manifest_path = _write_manifest_file(tmp_path, record_count=0)
        spec = _make_spec(
            dataset_manifest_path=manifest_path,
            budget_approved=True,
            budget_approval_id="a",
        )
        result = validate_training_job(spec)
        assert result.status == "blocked"
        assert "empty_dataset" in result.blocked_reasons


# ----------  红测：模型许可  ----------


class TestLicensePreflight:
    """模型许可校验。"""

    def test_unlicensed_model_blocked(self, tmp_path: Path) -> None:
        """模型不在许可白名单 → blocked + model_not_licensed。"""
        from lora.intent_train import validate_training_job

        manifest_path = _write_manifest_file(tmp_path)
        spec = _make_spec(
            dataset_manifest_path=manifest_path,
            budget_approved=True,
            budget_approval_id="a",
            base_model="SomeCompany/ProprietaryModel",
        )
        result = validate_training_job(spec)
        assert result.status == "blocked"
        assert "model_not_licensed" in result.blocked_reasons


# ----------  红测：失败任务不注册模型  ----------


class TestFailedJobNoModel:
    """失败/阻塞的任务不得产生模型 artifact。"""

    def test_blocked_job_no_artifact(self, tmp_path: Path) -> None:
        """blocked 训练 → status=blocked + artifact_digest=None。"""
        from lora.intent_train import train_intent

        spec = _make_spec(
            dataset_manifest_path=Path("/nonexistent/manifest.json"),
            output_dir=tmp_path / "output",
        )
        report = train_intent(spec)
        assert report.status == "blocked"
        assert report.artifact_digest is None

    def test_failed_job_no_artifact(self, tmp_path: Path) -> None:
        """训练失败 → artifact_digest=None。"""
        from lora.intent_train import train_intent

        spec = _make_spec(
            dataset_manifest_path=Path("/nonexistent/manifest.json"),
            output_dir=tmp_path / "output",
        )
        report = train_intent(spec)
        assert report.artifact_digest is None
        assert report.status in ("blocked", "failed")


# ----------  红测：任务管理  ----------


class TestTrainingJobManager:
    """TrainingJobManager：状态机/取消/幂等/预算。"""

    def test_submit_creates_queued_job(self) -> None:
        """提交任务 → queued 状态。"""
        from lora.intent_train import TrainingJobManager

        mgr = TrainingJobManager()
        spec = _make_spec()
        job_id = mgr.submit(spec)
        state = mgr.get_state(job_id)
        assert state.status == "queued"
        assert state.job_id == job_id

    def test_cancel_transitions_to_cancelled(self) -> None:
        """queued 任务可取消。"""
        from lora.intent_train import TrainingJobManager

        mgr = TrainingJobManager()
        spec = _make_spec()
        job_id = mgr.submit(spec)
        mgr.cancel(job_id)
        state = mgr.get_state(job_id)
        assert state.status == "cancelled"

    def test_cancel_terminal_job_raises(self) -> None:
        """终态任务不可取消。"""
        from lora.intent_train import TrainingJobManager

        mgr = TrainingJobManager()
        spec = _make_spec()
        job_id = mgr.submit(spec)
        mgr.cancel(job_id)
        with pytest.raises((ValueError, RuntimeError)):
            mgr.cancel(job_id)

    def test_cancel_nonexistent_raises(self) -> None:
        """取消不存在的任务 → KeyError。"""
        from lora.intent_train import TrainingJobManager

        mgr = TrainingJobManager()
        with pytest.raises(KeyError):
            mgr.cancel("nonexistent-job")

    def test_duplicate_submit_same_id_rejected(self) -> None:
        """同 job_id 重复提交 → 拒绝（不重复扣预算）。"""
        from lora.intent_train import TrainingJobManager

        mgr = TrainingJobManager()
        spec = _make_spec()
        mgr.submit(spec)
        with pytest.raises((ValueError, RuntimeError)):
            mgr.submit(spec)

    def test_budget_charged_once(self) -> None:
        """预算批准 ID 只能使用一次，重复使用 → 拒绝。"""
        from lora.intent_train import TrainingJobManager

        mgr = TrainingJobManager(budget_approvals={"approval-001"})
        spec1 = _make_spec(budget_approved=True, budget_approval_id="approval-001")
        mgr.submit(spec1)
        spec2 = _make_spec(
            job_id="job-002",
            budget_approved=True,
            budget_approval_id="approval-001",
        )
        with pytest.raises((ValueError, RuntimeError)):
            mgr.submit(spec2)

    def test_job_idempotent_status(self) -> None:
        """多次读取同一任务状态返回相同结果。"""
        from lora.intent_train import TrainingJobManager

        mgr = TrainingJobManager()
        spec = _make_spec()
        job_id = mgr.submit(spec)
        s1 = mgr.get_state(job_id)
        s2 = mgr.get_state(job_id)
        assert s1.status == s2.status
        assert s1.job_id == s2.job_id
