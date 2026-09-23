"""T07 部署绑定服务（ADR-0031 D04/D13 管理面）。

设计口径
--------
- **创建即 draft**：`POST /manage/deployments` 只登记指针
  `(deployment_id, scope, source_id)`，`active_release_id` 保持 NULL——首次
  发布是 T08 的显式 CAS 动作（D04），创建不是隐式发布。
- **源必须先存在**：绑定的 source 必须已有至少一条修订（T07a 追加式合同），
  否则拒绝——不创建指向未配置源的悬空绑定（配置即证据）。
- **读取门与写入门分开**：写入要求 `deployment.manage`（operator）；读取允许
  `deployment.manage` 或任一发布动作能力（`release.publish`/`release.rollback`）
  ——发布/回退需要读取当前指针做 CAS（D04），但读取不因此获得管理写入（D02）。
- **作用域 fail-closed**：scope 必须 ∈ principal.scopes；列表只返回已授权领域
  的部署，详情对未授权领域 403，未知一律 404（管理面区分，不泄露他域存在性）。

边界（诚实声明）
----------------
- 本服务不激活发布、不校验制品兼容性、不检查源探测证据（T08/T13）。
- `revision` 是 CAS 指针版本（发布/回退递增），不是制品内容版本。
"""

from __future__ import annotations

import sqlite3

from serving.control.auth import ControlForbidden, Principal, authorize
from serving.control.contracts import DeploymentRecord, DeploymentRequest, Owner
from serving.control.store import ControlStore

DEPLOYMENT_MANAGE_CAPABILITY = "deployment.manage"
# 读取门：部署管理或发布动作（发布/回退以当前指针做 CAS，需先读）。
_DEPLOYMENT_READ_CAPABILITIES = frozenset(
    {"deployment.manage", "release.publish", "release.rollback"}
)


class DeploymentConflict(RuntimeError):
    """部署 ID 已存在（HTTP 层投影为 409；不覆盖既有指针）。"""


class DeploymentService:
    """部署绑定服务：能力/作用域门 + 源存在性 + 无活动发布创建。"""

    def __init__(self, store: ControlStore) -> None:
        self._store = store

    def create_draft(self, principal: Principal, request: DeploymentRequest) -> DeploymentRecord:
        """登记无活动发布的部署（draft）；返回固定 8 键部署行。

        Raises
        ------
        ControlForbidden
            缺 `deployment.manage` 或 scope ∉ principal.scopes（D02）。
        KeyError
            绑定的 source 不存在任何修订（路由层投影为 404）。
        DeploymentConflict
            同 ID 部署已存在（不覆盖既有指针）。
        """
        authorize(principal, DEPLOYMENT_MANAGE_CAPABILITY, request.scope)
        if self._store.latest_source_revision(request.source_id) is None:
            raise KeyError(request.source_id)
        try:
            self._store.create_deployment(
                request.deployment_id,
                owner=Owner(issuer=principal.issuer, subject=principal.subject),
                scope=request.scope,
                source_id=request.source_id,
            )
        except sqlite3.IntegrityError as exc:
            raise DeploymentConflict(f"部署已存在：{request.deployment_id!r}") from exc
        record = self._store.get_deployment(request.deployment_id)
        if record is None:
            raise KeyError(request.deployment_id)
        return record

    def view(self, principal: Principal, deployment_id: str) -> DeploymentRecord:
        """部署详情；未知抛 KeyError，未授权领域抛 ControlForbidden（不裁剪）。"""
        self._require_read(principal)
        record = self._store.get_deployment(deployment_id)
        if record is None:
            raise KeyError(deployment_id)
        if record.scope not in principal.scopes:
            raise ControlForbidden(f"作用域未授权：{record.scope!r}")
        return record

    def list_deployments(self, principal: Principal) -> list[DeploymentRecord]:
        """已授权领域的部署列表（服务端裁剪；零授权 = 空表）。"""
        self._require_read(principal)
        return [
            record for record in self._store.list_deployments() if record.scope in principal.scopes
        ]

    @staticmethod
    def _require_read(principal: Principal) -> None:
        if not _DEPLOYMENT_READ_CAPABILITIES & principal.capabilities:
            raise ControlForbidden(
                "控制能力不足：读取部署需要 deployment.manage 或发布动作能力（D02）"
            )
