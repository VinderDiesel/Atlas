"""T15 影子运行契约测试。

覆盖：
- ShadowSpec：影子策略规格
- ShadowPolicy：影子策略管理（默认关闭）
- ShadowRunner：影子执行（不改变用户结果、不额外跑 SQL）
- 影子单独 run lineage，不入用户会话
"""

from __future__ import annotations

from datetime import timedelta, timezone

import pytest

from serving.control.contracts import Owner

TZ = timezone(timedelta(hours=8))
PUBLISHER = Owner(issuer="test", subject="publisher")


# ----------  ShadowSpec  ----------


class TestShadowSpec:
    """ShadowSpec：影子策略规格。"""

    def test_shadow_spec_creation(self) -> None:
        """创建影子策略规格。"""
        from serving.control.models import ShadowSpec

        spec = ShadowSpec(
            candidate_model_id="model-001",
            sample_rate=0.1,
            budget_limit=100,
        )
        assert spec.candidate_model_id == "model-001"
        assert spec.sample_rate == 0.1
        assert spec.budget_limit == 100

    def test_shadow_spec_defaults(self) -> None:
        """影子策略默认关闭（sample_rate=0）。"""
        from serving.control.models import ShadowSpec

        spec = ShadowSpec(candidate_model_id="model-001")
        assert spec.sample_rate == 0.0
        assert spec.enabled is False


# ----------  ShadowPolicy  ----------


class TestShadowPolicy:
    """ShadowPolicy：影子策略管理。"""

    def test_shadow_disabled_by_default(self) -> None:
        """影子默认关闭。"""
        from serving.control.models import ShadowPolicy

        policy = ShadowPolicy()
        assert policy.is_enabled is False
        assert policy.get_active_spec() is None

    def test_enable_shadow(self) -> None:
        """启用影子策略（需批准的模型）。"""
        from serving.control.models import ShadowPolicy, ShadowSpec

        policy = ShadowPolicy()
        spec = ShadowSpec(candidate_model_id="model-001", sample_rate=0.1)
        policy.enable(spec, approver=PUBLISHER)
        assert policy.is_enabled is True
        assert policy.get_active_spec() is not None

    def test_enable_unapproved_model_rejected(self) -> None:
        """启用未批准模型 → 拒绝。"""
        from serving.control.models import ShadowPolicy, ShadowSpec

        policy = ShadowPolicy(approved_model_ids=set())  # 无已批准模型
        spec = ShadowSpec(candidate_model_id="model-001")
        with pytest.raises(ValueError, match="approved|批准"):
            policy.enable(spec, approver=PUBLISHER)

    def test_disable_shadow(self) -> None:
        """禁用影子策略。"""
        from serving.control.models import ShadowPolicy, ShadowSpec

        policy = ShadowPolicy()
        spec = ShadowSpec(candidate_model_id="model-001", sample_rate=0.1)
        policy.enable(spec, approver=PUBLISHER)
        policy.disable()
        assert policy.is_enabled is False


# ----------  ShadowRunner  ----------


class TestShadowRunner:
    """ShadowRunner：影子执行（不改变用户结果）。"""

    def test_shadow_does_not_modify_result(self) -> None:
        """影子运行不改变主结果。"""
        from serving.control.models import ShadowPolicy, ShadowRunner, ShadowSpec

        policy = ShadowPolicy()
        spec = ShadowSpec(candidate_model_id="model-001", sample_rate=1.0)
        policy.enable(spec, approver=PUBLISHER)

        runner = ShadowRunner(policy)
        main_result = {"sql": "SELECT 1", "metric": "total_amount"}

        # 影子运行
        shadow_result = runner.run_shadow(
            question="测试问句",
            main_result=main_result,
            parent_run_id="run-001",
        )

        # 主结果不变
        assert main_result["sql"] == "SELECT 1"
        # 影子结果独立
        assert shadow_result is not None
        assert shadow_result.parent_run_id == "run-001"
        assert shadow_result.shadow_model_id == "model-001"

    def test_shadow_does_not_execute_sql(self) -> None:
        """影子运行不执行额外 SQL。"""
        from serving.control.models import ShadowPolicy, ShadowRunner, ShadowSpec

        policy = ShadowPolicy()
        spec = ShadowSpec(candidate_model_id="model-001", sample_rate=1.0)
        policy.enable(spec, approver=PUBLISHER)

        runner = ShadowRunner(policy)
        main_result = {"sql": "SELECT 1", "metric": "total_amount"}

        shadow_result = runner.run_shadow(
            question="测试问句",
            main_result=main_result,
            parent_run_id="run-001",
        )

        # 影子不执行 SQL
        assert shadow_result.sql_executed is False

    def test_shadow_disabled_no_run(self) -> None:
        """影子关闭时不运行。"""
        from serving.control.models import ShadowPolicy, ShadowRunner

        policy = ShadowPolicy()  # 默认关闭
        runner = ShadowRunner(policy)
        main_result = {"sql": "SELECT 1"}

        shadow_result = runner.run_shadow(
            question="测试问句",
            main_result=main_result,
            parent_run_id="run-001",
        )

        # 影子关闭 → 无结果
        assert shadow_result is None

    def test_shadow_sample_rate_zero_no_run(self) -> None:
        """sample_rate=0 时不运行（即使启用）。"""
        from serving.control.models import ShadowPolicy, ShadowRunner, ShadowSpec

        policy = ShadowPolicy()
        spec = ShadowSpec(candidate_model_id="model-001", sample_rate=0.0)
        policy.enable(spec, approver=PUBLISHER)

        runner = ShadowRunner(policy)
        main_result = {"sql": "SELECT 1"}

        shadow_result = runner.run_shadow(
            question="测试问句",
            main_result=main_result,
            parent_run_id="run-001",
        )

        assert shadow_result is None

    def test_shadow_has_separate_lineage(self) -> None:
        """影子运行有独立 lineage（shadow_run_id）。"""
        from serving.control.models import ShadowPolicy, ShadowRunner, ShadowSpec

        policy = ShadowPolicy()
        spec = ShadowSpec(candidate_model_id="model-001", sample_rate=1.0)
        policy.enable(spec, approver=PUBLISHER)

        runner = ShadowRunner(policy)
        main_result = {"sql": "SELECT 1"}

        shadow_result = runner.run_shadow(
            question="测试问句",
            main_result=main_result,
            parent_run_id="run-001",
        )

        assert shadow_result is not None
        assert shadow_result.shadow_run_id != "run-001"
        assert shadow_result.parent_run_id == "run-001"

    def test_shadow_not_in_user_session(self) -> None:
        """影子运行不入用户会话。"""
        from serving.control.models import ShadowPolicy, ShadowRunner, ShadowSpec

        policy = ShadowPolicy()
        spec = ShadowSpec(candidate_model_id="model-001", sample_rate=1.0)
        policy.enable(spec, approver=PUBLISHER)

        runner = ShadowRunner(policy)
        main_result = {"sql": "SELECT 1"}

        shadow_result = runner.run_shadow(
            question="测试问句",
            main_result=main_result,
            parent_run_id="run-001",
        )

        # 影子运行标记为隔离
        assert shadow_result is not None
        assert shadow_result.isolated is True
