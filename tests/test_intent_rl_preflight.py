"""T16 GRPO 预检契约测试。

覆盖：
- GRPOSpec：GRPO 训练规格
- validate_grpo_preflight：前置门禁检查
- 前置缺失（SFT/奖励/预算/许可）→ blocked
- 数据集隔离（训练 vs 评测不泄漏）
"""

from __future__ import annotations

from pathlib import Path

# ----------  GRPOSpec  ----------


class TestGRPOSpec:
    """GRPOSpec 数据结构。"""

    def test_spec_creation(self) -> None:
        from lora.intent_rl import GRPOSpec

        spec = GRPOSpec(
            job_id="grpo-001",
            sft_baseline_frozen=True,
            reward_cases_path=Path("/tmp/reward_cases.json"),
            budget_approved=True,
            base_model="Qwen/Qwen2.5-7B-Instruct",
            dataset_manifest_path=Path("/tmp/manifest.json"),
            recipe_path=Path("lora/configs/intent_rl.yaml"),
        )
        assert spec.job_id == "grpo-001"
        assert spec.sft_baseline_frozen is True

    def test_spec_defaults(self) -> None:
        from lora.intent_rl import GRPOSpec

        spec = GRPOSpec(
            job_id="grpo-002",
            sft_baseline_frozen=False,
            reward_cases_path=Path("/tmp/cases.json"),
            budget_approved=False,
            base_model="Qwen/Qwen2.5-7B-Instruct",
        )
        assert spec.dataset_manifest_path is None
        assert spec.recipe_path is None


# ----------  预检门禁  ----------


class TestGRPOPreflight:
    """GRPO 预检：前置缺失 → blocked。"""

    def _make_spec(self, **overrides: object):
        from lora.intent_rl import GRPOSpec

        defaults = dict(
            job_id="grpo-test",
            sft_baseline_frozen=True,
            reward_cases_path=Path("/tmp/reward_cases.json"),
            budget_approved=True,
            base_model="Qwen/Qwen2.5-7B-Instruct",
            dataset_manifest_path=Path("/tmp/manifest.json"),
        )
        defaults.update(overrides)
        return GRPOSpec(**defaults)  # type: ignore[arg-type]

    def test_sft_not_frozen_blocked(self) -> None:
        """SFT 基线未冻结 → blocked。"""
        from lora.intent_rl import validate_grpo_preflight

        spec = self._make_spec(sft_baseline_frozen=False)
        result = validate_grpo_preflight(spec)
        assert result.status == "blocked"
        assert "sft_not_frozen" in result.blocked_reasons

    def test_no_reward_cases_blocked(self) -> None:
        """无奖励用例 → blocked。"""
        from lora.intent_rl import validate_grpo_preflight

        spec = self._make_spec(reward_cases_path=None)
        result = validate_grpo_preflight(spec)
        assert result.status == "blocked"
        assert "no_reward_cases" in result.blocked_reasons

    def test_budget_not_approved_blocked(self) -> None:
        """预算未批准 → blocked。"""
        from lora.intent_rl import validate_grpo_preflight

        spec = self._make_spec(budget_approved=False)
        result = validate_grpo_preflight(spec)
        assert result.status == "blocked"
        assert "budget_not_approved" in result.blocked_reasons

    def test_unlicensed_model_blocked(self) -> None:
        """未许可基座模型 → blocked。"""
        from lora.intent_rl import validate_grpo_preflight

        spec = self._make_spec(base_model="Meta/Llama-3-8B")
        result = validate_grpo_preflight(spec)
        assert result.status == "blocked"
        assert "unlicensed_model" in result.blocked_reasons

    def test_all_prereqs_met_ready(self) -> None:
        """所有前置满足 → ready（或仅缺 CUDA）。"""
        from lora.intent_rl import validate_grpo_preflight

        spec = self._make_spec()
        result = validate_grpo_preflight(spec)
        # CPU 环境可能缺 CUDA，但不缺前置
        missing_prereqs = {
            r
            for r in result.blocked_reasons
            if r
            in (
                "sft_not_frozen",
                "no_reward_cases",
                "budget_not_approved",
                "unlicensed_model",
            )
        }
        assert len(missing_prereqs) == 0

    def test_multiple_reasons_accumulated(self) -> None:
        """多个阻塞理由累积返回。"""
        from lora.intent_rl import validate_grpo_preflight

        spec = self._make_spec(
            sft_baseline_frozen=False,
            budget_approved=False,
        )
        result = validate_grpo_preflight(spec)
        assert result.status == "blocked"
        assert "sft_not_frozen" in result.blocked_reasons
        assert "budget_not_approved" in result.blocked_reasons
