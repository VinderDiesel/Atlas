"""ADR-0031 控制面身份与能力授权（D02）；控制能力与数据角色正交。

三条纪律：

1. **能力只来自部署配置授予**（受保护配置 → capabilities），角色模板
   （viewer/operator/editor/reviewer/publisher）是默认集；模板之间**不隐式
   继承**（两两不交），编辑、审核、发布永远是不同显式动作（D02）。
2. **数据角色不升格**：hq_admin / broker 等是数据面角色（行级策略），
   不自动获得任何控制能力——本模块不 import 数据角色表，两个命名空间隔离。
3. **作用域 fail-closed**：resource_scope 必须 ∈ principal.scopes，
   空 scopes = 未授权任何领域（宁可全拒，不默认放行）。

会话指纹复用 `serving.auth.claims_fingerprint`（全量 claims sha256，
旧会话合同不放松——ADR-0031 退出门）。控制能力配置**不放业务语义 YAML**
（D02；semantic/ 是语义权威源，控制配置属部署配置）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from serving.auth import claims_fingerprint

ControlAction = Literal[
    # viewer
    "run.create",
    "run.read_own",
    "feedback.submit",
    # operator
    "source.manage",
    "deployment.manage",
    "ops.read",
    "run.read_summary",
    # editor
    "draft.edit",
    "draft.validate",
    "draft.export",
    "experiment.run",
    "dataset.create",
    # reviewer
    "draft.review",
    "feedback.review",
    "dataset.approve",
    "model.approve",
    # publisher
    "release.import",
    "release.publish",
    "release.rollback",
    "shadow.manage",
    "train.start",
    "train.cancel",
]

VIEWER_CAPABILITIES: frozenset[str] = frozenset({"run.create", "run.read_own", "feedback.submit"})
OPERATOR_CAPABILITIES: frozenset[str] = frozenset(
    {"source.manage", "deployment.manage", "ops.read", "run.read_summary"}
)
EDITOR_CAPABILITIES: frozenset[str] = frozenset(
    {"draft.edit", "draft.validate", "draft.export", "experiment.run", "dataset.create"}
)
REVIEWER_CAPABILITIES: frozenset[str] = frozenset(
    {"draft.review", "feedback.review", "dataset.approve", "model.approve"}
)
PUBLISHER_CAPABILITIES: frozenset[str] = frozenset(
    {
        "release.import",
        "release.publish",
        "release.rollback",
        "shadow.manage",
        "train.start",
        "train.cancel",
    }
)

ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "viewer": VIEWER_CAPABILITIES,
    "operator": OPERATOR_CAPABILITIES,
    "editor": EDITOR_CAPABILITIES,
    "reviewer": REVIEWER_CAPABILITIES,
    "publisher": PUBLISHER_CAPABILITIES,
}

ALL_ACTIONS: frozenset[str] = frozenset().union(*ROLE_CAPABILITIES.values())


class ControlForbidden(Exception):
    """控制能力不足或作用域未授权（HTTP 403；D13 统一错误体在服务层投影）。"""


@dataclass(frozen=True)
class Principal:
    """控制面身份；capabilities 与 scopes 来自部署配置，role 仅审计/展示。

    role 不参与授权推断（模板不隐式继承）——调用方从部署配置装配
    capabilities；同一人兼任多角色 = 能力集并集，但编辑/审核/发布
    必须分别审计（D02）。user_context 是已验证的数据面 claims 副本。
    """

    issuer: str
    subject: str
    role: str
    user_context: dict[str, object]
    capabilities: frozenset[str]
    auth_fingerprint: str
    scopes: frozenset[str] = field(default_factory=frozenset)


def authorize(principal: Principal, action: str, resource_scope: str) -> None:
    """校验控制能力与领域作用域；任一项不满足抛 ControlForbidden。

    Parameters
    ----------
    principal : 当前控制身份（能力与 scopes 已由部署配置装配）
    action : 控制动作名（注册集合见 ALL_ACTIONS；未注册动作一律拒绝）
    resource_scope : 操作目标领域（如 finance），须 ∈ principal.scopes

    Raises
    ------
    ControlForbidden : 能力不足或作用域未授权
    """
    if action not in principal.capabilities:
        raise ControlForbidden(f"控制能力不足：{action!r}")
    if resource_scope not in principal.scopes:
        raise ControlForbidden(f"作用域未授权：{resource_scope!r}")


def principal_from_claims(
    claims: dict[str, object],
    *,
    issuer: str,
    role: str,
    capabilities: frozenset[str],
    scopes: frozenset[str],
) -> Principal:
    """已验证 claims → Principal；subject 与指纹取自 claims，不手填。

    Raises
    ------
    ValueError : claims 缺少非空 sub
    """
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise ValueError("claims 缺少 sub：无法建立稳定所有者 (issuer, subject)")
    return Principal(
        issuer=issuer,
        subject=subject,
        role=role,
        user_context=dict(claims),
        capabilities=capabilities,
        auth_fingerprint=claims_fingerprint(claims),
        scopes=scopes,
    )


# ---------------------------------------------------------------------------
# 部署配置授予（ATLAS_CONTROL_GRANTS）：OIDC subject → 控制角色与领域 scope
# ---------------------------------------------------------------------------
#
# 格式（.env.example 同源）：分号分隔条目，每条 = <subject>|<角色[,角色...]>|<scope[,scope...]>。
# 例：alice|editor,reviewer|finance;bob|viewer|finance,retail
# 同一 subject 可出现在多条：角色与 scope 取并集（兼任多角色 = 能力并集，
# 但编辑/审核/发布仍是不同显式动作，分别审计——D02）。
# 未列出的 subject = 无授予（空能力、空 scope，fail-closed 全拒）。


class ControlGrantError(Exception):
    """授予配置非法（未知角色/缺段/空 subject）：fail-fast，不静默忽略。"""


@dataclass(frozen=True)
class ControlGrant:
    """一个 subject 的控制授予：模板角色元组 + 领域 scope 集。"""

    roles: tuple[str, ...]
    scopes: frozenset[str]


_CONTROL_GRANTS_ENV = "ATLAS_CONTROL_GRANTS"


def _split_list(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def parse_control_grants(raw: str) -> dict[str, ControlGrant]:
    """解析授予配置文本 → {subject: ControlGrant}；非法条目抛 ControlGrantError。

    解析期即校验角色已注册（ROLE_CAPABILITIES 查表）——拼错角色名不会被
    静默降级成“无权限”，而是阻断装配（部署配置错误应尽早暴露）。
    """
    grants: dict[str, ControlGrant] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("|")
        if len(parts) != 3:
            raise ControlGrantError(f"授予条目应为 <subject>|<角色>|<scope>：{entry!r}")
        subject, roles_raw, scopes_raw = (part.strip() for part in parts)
        if not subject:
            raise ControlGrantError(f"授予条目缺少 subject：{entry!r}")
        roles = _split_list(roles_raw)
        if not roles:
            raise ControlGrantError(f"授予条目缺少角色：{entry!r}")
        unknown = [role for role in roles if role not in ROLE_CAPABILITIES]
        if unknown:
            options = sorted(ROLE_CAPABILITIES)
            raise ControlGrantError(f"未注册控制角色：{unknown}（可选：{options}）")
        scopes = frozenset(_split_list(scopes_raw))
        existing = grants.get(subject)
        if existing is None:
            grants[subject] = ControlGrant(roles=roles, scopes=scopes)
        else:
            merged_roles = existing.roles + tuple(r for r in roles if r not in existing.roles)
            grants[subject] = ControlGrant(roles=merged_roles, scopes=existing.scopes | scopes)
    return grants


def load_control_grants(env: Mapping[str, str] | None = None) -> dict[str, ControlGrant]:
    """从部署环境读取 ATLAS_CONTROL_GRANTS（未设置 = 空授予表，fail-closed）。"""
    env = os.environ if env is None else env
    return parse_control_grants(env.get(_CONTROL_GRANTS_ENV) or "")


def principal_for_oidc(
    claims: dict[str, object],
    *,
    issuer: str,
    grants: Mapping[str, ControlGrant],
) -> Principal:
    """已验签 OIDC claims + 部署授予 → Principal（未授予者零能力零 scope）。

    控制能力取所选模板的并集；role 是展示/审计标签（模板名逗号连接，
    未授予为 "none"），不参与授权推断（authorize 只看 capabilities/scopes）。
    """
    subject = claims.get("sub")
    grant = grants.get(subject) if isinstance(subject, str) else None
    roles = grant.roles if grant is not None else ()
    capabilities = (
        frozenset().union(*(ROLE_CAPABILITIES[role] for role in roles)) if roles else frozenset()
    )
    scopes = grant.scopes if grant is not None else frozenset()
    return principal_from_claims(
        claims,
        issuer=issuer,
        role=",".join(roles) if roles else "none",
        capabilities=capabilities,
        scopes=scopes,
    )


# ---------------------------------------------------------------------------
# 本地 Bearer 通道（T05c）：/runs 走既有 require_bearer（serving.auth）验证签名，
# 控制 Principal 由本函数解析——同一 claims、同一授予表，与 OIDC 会话零分叉。
# ---------------------------------------------------------------------------

BEARER_LOCAL_ISSUER = "atlas-local"
"""本地签发 token 的稳定 issuer 命名空间（sign_token 不写 iss）。"""


def principal_for_bearer(
    claims: dict[str, object],
    *,
    grants: Mapping[str, ControlGrant],
) -> Principal:
    """已验证 Bearer claims + 部署授予 → Principal。

    本地 HS256 token（`serving.auth.sign_token`）payload 不含 `iss`——此处按
    `BEARER_LOCAL_ISSUER` 常量归入本地命名空间，与 OIDC subject 天然隔离
    （同一 subject 字符串不会跨通道撞成同一所有者）。外部 IdP token 带 `iss`
    时原样采用。
    """
    issuer = claims.get("iss")
    return principal_for_oidc(
        claims,
        issuer=issuer if isinstance(issuer, str) and issuer else BEARER_LOCAL_ISSUER,
        grants=grants,
    )
