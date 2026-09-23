"""T11 节点实验服务（ExperimentService）：单节点对照实验，不修改 active 发布。

设计口径
--------
- ExperimentService 管理单节点对照实验：给定 baseline_release 和 candidate_release，
  在固定数据集上运行指定节点，比较结果。
- 实验不修改 active 发布、不影响在线会话、不执行线上 SQL。
- 实验结果只记录不自动发布——人工审核后才可推进到发布流程。
- release ID 必须是 64 hex（SHA-256 摘要格式）；节点类型必须在注册表内。

边界（诚实声明）
----------------
- 首版实验只做「节点在两个 release 的相同数据集上运行并记录结果」的骨架；
  真实节点执行对照（需加载两个 release 的 bundle）随 T11 后续迭代补齐。
- 不执行在线 SQL——实验使用固定数据集（eval/gold 样本或冻结 fixture）。
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from agent.flows.contracts import REGISTERED_NODE_TYPES

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REGISTERED_NODE_IDS = frozenset(reg.node_type for reg in REGISTERED_NODE_TYPES)


@dataclass(frozen=True)
class ExperimentSpec:
    """实验规格（不可变）。"""

    baseline_release_id: str
    candidate_release_id: str
    node_id: str
    dataset_version: str


@dataclass(frozen=True)
class ExperimentReceipt:
    """实验结果收据（不可变）。"""

    experiment_id: str
    status: str  # "completed" | "failed"
    baseline_release_id: str
    candidate_release_id: str
    node_id: str
    dataset_version: str
    comparison: dict[str, Any] = field(default_factory=dict)


class ExperimentService:
    """节点实验管理服务。

    Parameters
    ----------
    store : 控制库实例（用于持久化实验记录）。
    executor : 可选执行器（实验**不**用于在线 SQL，仅接口兼容）。
    """

    def __init__(
        self,
        store: Any,
        *,
        executor: Any = None,
    ) -> None:
        self._store = store
        self._executor = executor
        self._ensure_table()

    def _ensure_table(self) -> None:
        """确保实验记录表存在。"""
        conn = self._get_connection()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT PRIMARY KEY,
                    baseline_release_id TEXT NOT NULL,
                    candidate_release_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    dataset_version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'completed',
                    comparison_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )"""
            )
            conn.commit()
        finally:
            conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._store.path))

    def run(self, spec: dict[str, Any] | ExperimentSpec) -> ExperimentReceipt:
        """运行单节点对照实验。

        Raises
        ------
        ValueError
            release ID 格式非法或节点类型未注册。
        """
        if isinstance(spec, dict):
            spec = ExperimentSpec(**spec)

        # 校验 release ID 格式
        if not _SHA256_RE.match(spec.baseline_release_id):
            raise ValueError(f"baseline_release_id 必须是 64 hex：{spec.baseline_release_id!r}")
        if not _SHA256_RE.match(spec.candidate_release_id):
            raise ValueError(f"candidate_release_id 必须是 64 hex：{spec.candidate_release_id!r}")

        # 校验节点类型
        if spec.node_id not in _REGISTERED_NODE_IDS:
            raise ValueError(
                f"node_id 必须是已注册节点类型：{spec.node_id!r}"
                f"（可用：{sorted(_REGISTERED_NODE_IDS)}）"
            )

        # 实验执行（首版骨架：记录实验、不真实执行节点对照）
        experiment_id = secrets.token_hex(16)
        comparison = {
            "baseline": {"release": spec.baseline_release_id, "node_output": None},
            "candidate": {"release": spec.candidate_release_id, "node_output": None},
            "diff": "pending_implementation",
        }

        receipt = ExperimentReceipt(
            experiment_id=experiment_id,
            status="completed",
            baseline_release_id=spec.baseline_release_id,
            candidate_release_id=spec.candidate_release_id,
            node_id=spec.node_id,
            dataset_version=spec.dataset_version,
            comparison=comparison,
        )

        # 持久化
        conn = self._get_connection()
        try:
            import json

            conn.execute(
                "INSERT INTO experiments "
                "(experiment_id, baseline_release_id, candidate_release_id, "
                "node_id, dataset_version, status, comparison_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt.experiment_id,
                    receipt.baseline_release_id,
                    receipt.candidate_release_id,
                    receipt.node_id,
                    receipt.dataset_version,
                    receipt.status,
                    json.dumps(receipt.comparison),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        return receipt

    def list_experiments(self) -> list[ExperimentReceipt]:
        """返回实验历史（按创建时间倒序）。"""
        import json

        conn = self._get_connection()
        try:
            rows = conn.execute(
                "SELECT experiment_id, baseline_release_id, candidate_release_id, "
                "node_id, dataset_version, status, comparison_json "
                "FROM experiments ORDER BY created_at DESC"
            ).fetchall()
        finally:
            conn.close()
        return [
            ExperimentReceipt(
                experiment_id=row[0],
                baseline_release_id=row[1],
                candidate_release_id=row[2],
                node_id=row[3],
                dataset_version=row[4],
                status=row[5],
                comparison=json.loads(row[6]),
            )
            for row in rows
        ]

    def get_active_release(self, deployment_id: str) -> str | None:
        """查询部署的 active release（只读；不修改指针）。"""
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT release_id FROM deployments WHERE deployment_id = ? AND is_active = 1",
                (deployment_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            # deployments 表可能尚未迁移
            return None
        finally:
            conn.close()
        return row[0] if row else None
