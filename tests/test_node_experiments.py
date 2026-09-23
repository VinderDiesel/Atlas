"""T11 节点实验服务（ExperimentService）：单节点对照实验，不修改 active 发布。

设计口径
--------
- ExperimentService 管理单节点对照实验：给定 baseline_release 和 candidate_draft，
  在固定数据集上运行指定节点，比较结果。
- 实验不修改 active 发布、不影响在线会话、不执行线上 SQL。
- 实验结果只记录不自动发布——人工审核后才可推进到发布流程。
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# T11a 红测：实验隔离
# ---------------------------------------------------------------------------


def test_experiment_does_not_modify_active(tmp_path):
    """运行实验不改变 active 发布指针。"""
    from serving.control.experiments import ExperimentService
    from serving.control.store import ControlStore

    store = ControlStore(tmp_path / "control.sqlite")
    store.migrate()
    service = ExperimentService(store)

    # 实验前记录 active release
    # （尚无部署时 active 为 None，实验后仍应为 None）
    spec = {
        "baseline_release_id": "a" * 64,
        "candidate_release_id": "b" * 64,
        "node_id": "rule_plan",
        "dataset_version": "test-v1",
    }
    receipt = service.run(spec)
    assert receipt.status == "completed"
    # active 未被改变（无部署 → 仍无部署）
    assert service.get_active_release("finance") is None


def test_experiment_does_not_execute_online_sql(tmp_path):
    """实验不调用在线 SQL 执行器——只用固定数据集。"""
    from serving.control.experiments import ExperimentService
    from serving.control.store import ControlStore

    store = ControlStore(tmp_path / "control.sqlite")
    store.migrate()

    executor_calls: list[str] = []

    def _spy_executor(sql: str):
        executor_calls.append(sql)
        return [], []

    service = ExperimentService(store, executor=_spy_executor)
    spec = {
        "baseline_release_id": "a" * 64,
        "candidate_release_id": "b" * 64,
        "node_id": "rule_plan",
        "dataset_version": "test-v1",
    }
    service.run(spec)
    assert executor_calls == [], "实验不应执行任何在线 SQL"


def test_experiment_rejects_invalid_release_ids(tmp_path):
    """非法 release ID（非 64 hex）被拒绝。"""
    from serving.control.experiments import ExperimentService
    from serving.control.store import ControlStore

    store = ControlStore(tmp_path / "control.sqlite")
    store.migrate()
    service = ExperimentService(store)

    spec = {
        "baseline_release_id": "not-a-valid-sha",
        "candidate_release_id": "b" * 64,
        "node_id": "rule_plan",
        "dataset_version": "test-v1",
    }
    with pytest.raises(ValueError, match="baseline_release_id"):
        service.run(spec)


def test_experiment_rejects_unregistered_node(tmp_path):
    """未注册的节点类型被拒绝。"""
    from serving.control.experiments import ExperimentService
    from serving.control.store import ControlStore

    store = ControlStore(tmp_path / "control.sqlite")
    store.migrate()
    service = ExperimentService(store)

    spec = {
        "baseline_release_id": "a" * 64,
        "candidate_release_id": "b" * 64,
        "node_id": "raw_execute",  # 未注册
        "dataset_version": "test-v1",
    }
    with pytest.raises(ValueError, match="node_id"):
        service.run(spec)


def test_experiment_result_contains_comparison(tmp_path):
    """实验结果包含 baseline/candidate 对照摘要。"""
    from serving.control.experiments import ExperimentService
    from serving.control.store import ControlStore

    store = ControlStore(tmp_path / "control.sqlite")
    store.migrate()
    service = ExperimentService(store)

    spec = {
        "baseline_release_id": "a" * 64,
        "candidate_release_id": "b" * 64,
        "node_id": "rule_plan",
        "dataset_version": "test-v1",
    }
    receipt = service.run(spec)
    assert receipt.status == "completed"
    assert receipt.experiment_id is not None
    assert receipt.baseline_release_id == "a" * 64
    assert receipt.candidate_release_id == "b" * 64
    assert receipt.node_id == "rule_plan"


def test_experiment_list_returns_history(tmp_path):
    """实验列表返回历史记录。"""
    from serving.control.experiments import ExperimentService
    from serving.control.store import ControlStore

    store = ControlStore(tmp_path / "control.sqlite")
    store.migrate()
    service = ExperimentService(store)

    spec = {
        "baseline_release_id": "a" * 64,
        "candidate_release_id": "b" * 64,
        "node_id": "rule_plan",
        "dataset_version": "test-v1",
    }
    service.run(spec)
    history = service.list_experiments()
    assert len(history) >= 1
    assert history[0].node_id == "rule_plan"
