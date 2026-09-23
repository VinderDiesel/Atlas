"""T03 控制面授权：能力与数据角色正交，作用域未授权即拒绝（ADR-0031 D02）。

控制能力只来自部署配置授予的 capabilities；数据角色（hq_admin/broker 等）
不自动升为发布者。scope 校验 fail-closed：未列出领域一律拒绝。
"""

from __future__ import annotations

import pytest

FINANCE = "finance"
RETAIL = "retail"


def _principal(
    *,
    role: str = "viewer",
    data_role: str = "hq_admin",
    capabilities: frozenset[str] | None = None,
    scopes: frozenset[str] = frozenset({FINANCE}),
):
    """构造 Principal；capabilities=None 时按角色模板取默认能力集。"""
    from serving.control.auth import ROLE_CAPABILITIES, Principal, claims_fingerprint

    claims = {
        "sub": "ctrl-user",
        "role": data_role,
        "iat": 1_700_000_000,
        "exp": 1_700_003_600,
        "user_context": {},
    }
    return Principal(
        issuer="https://idp.example.com",
        subject="ctrl-user",
        role=role,
        user_context=claims,
        capabilities=ROLE_CAPABILITIES[role] if capabilities is None else capabilities,
        auth_fingerprint=claims_fingerprint(claims),
        scopes=scopes,
    )


def test_data_admin_is_not_control_publisher() -> None:
    """红测（工作卡原文）：viewer 无发布权，即使数据角色是 hq_admin。"""
    from serving.control.auth import ControlForbidden, authorize

    viewer_principal = _principal(role="viewer", data_role="hq_admin")
    with pytest.raises(ControlForbidden):
        authorize(viewer_principal, "release.publish", FINANCE)


def test_viewer_can_create_run_in_authorized_scope() -> None:
    """viewer 的合法动作在已授权领域内放行。"""
    from serving.control.auth import authorize

    authorize(_principal(role="viewer"), "run.create", FINANCE)
    authorize(_principal(role="viewer"), "run.read_own", FINANCE)
    authorize(_principal(role="viewer"), "feedback.submit", FINANCE)


def test_role_templates_are_mutually_exclusive_and_complete() -> None:
    """五模板两两不交（不隐式继承）；并集 = 全部已注册能力，无未归属能力。"""
    from serving.control.auth import ALL_ACTIONS, ROLE_CAPABILITIES

    assert set(ROLE_CAPABILITIES) == {"viewer", "operator", "editor", "reviewer", "publisher"}
    names = sorted(ROLE_CAPABILITIES)
    for i, left in enumerate(names):
        for right in names[i + 1 :]:
            assert not ROLE_CAPABILITIES[left] & ROLE_CAPABILITIES[right], (left, right)
    union = frozenset().union(*ROLE_CAPABILITIES.values())
    assert union == ALL_ACTIONS
    assert len(ALL_ACTIONS) == 22


def test_editor_cannot_review_or_publish() -> None:
    """编辑不能自审自发布（D02：编辑、审核、发布是不同显式动作）。"""
    from serving.control.auth import ControlForbidden, authorize

    editor = _principal(role="editor")
    authorize(editor, "draft.edit", FINANCE)
    for action in ("draft.review", "release.publish", "release.rollback"):
        with pytest.raises(ControlForbidden):
            authorize(editor, action, FINANCE)


def test_publisher_cannot_review() -> None:
    """发布模板不含审核能力；审核由 reviewer 模板单独授予。"""
    from serving.control.auth import ControlForbidden, authorize

    publisher = _principal(role="publisher")
    authorize(publisher, "release.publish", FINANCE)
    with pytest.raises(ControlForbidden):
        authorize(publisher, "draft.review", FINANCE)


def test_action_outside_authorized_scope_rejected() -> None:
    """有发布能力但目标领域不在授权 scope：仍拒绝（按领域 scope 裁剪）。"""
    from serving.control.auth import ControlForbidden, authorize

    publisher = _principal(role="publisher", scopes=frozenset({RETAIL}))
    authorize(publisher, "release.publish", RETAIL)
    with pytest.raises(ControlForbidden):
        authorize(publisher, "release.publish", FINANCE)


def test_empty_scopes_reject_everything() -> None:
    """scopes 为空 = 未授权任何领域，fail-closed 全拒。"""
    from serving.control.auth import ControlForbidden, authorize

    orphan = _principal(role="publisher", scopes=frozenset())
    with pytest.raises(ControlForbidden):
        authorize(orphan, "release.publish", FINANCE)


def test_unknown_action_rejected() -> None:
    """未注册动作（拼写错/未来动作）不得因模板"看上去够大"而放行。"""
    from serving.control.auth import ControlForbidden, authorize

    publisher = _principal(role="publisher")
    with pytest.raises(ControlForbidden):
        authorize(publisher, "release.approve_everything", FINANCE)


def test_data_roles_are_not_control_roles() -> None:
    """数据角色名（hq_admin 等）不是控制模板，不隐式赋予任何能力。"""
    from serving.control.auth import ROLE_CAPABILITIES

    assert not {"hq_admin", "branch_manager", "broker", "category_analyst"} & set(ROLE_CAPABILITIES)


def test_fingerprint_reuses_claims_contract() -> None:
    """Principal 指纹与旧会话合同同源（serving.auth.claims_fingerprint，不重实现）。"""
    from serving.auth import claims_fingerprint
    from serving.control.auth import Principal

    claims = {"sub": "ctrl-user", "role": "hq_admin", "iat": 1, "exp": 2, "user_context": {}}
    principal = Principal(
        issuer="https://idp.example.com",
        subject="ctrl-user",
        role="viewer",
        user_context=claims,
        capabilities=frozenset(),
        auth_fingerprint=claims_fingerprint(claims),
        scopes=frozenset({FINANCE}),
    )
    assert principal.auth_fingerprint == claims_fingerprint(claims)
    resigned = {**claims, "iat": 99, "exp": 100}
    assert claims_fingerprint(resigned) != principal.auth_fingerprint


def test_principal_from_claims_binds_subject_and_fingerprint() -> None:
    """已验证 claims → Principal：subject 与指纹来自 claims，不手填；缺 sub 拒绝。"""
    from serving.auth import claims_fingerprint
    from serving.control.auth import principal_from_claims

    claims = {"sub": "u-1", "role": "hq_admin", "iat": 1, "exp": 2, "user_context": {}}
    principal = principal_from_claims(
        claims,
        issuer="https://idp.example.com",
        role="viewer",
        capabilities=frozenset({"run.create"}),
        scopes=frozenset({FINANCE}),
    )
    assert principal.subject == "u-1"
    assert principal.auth_fingerprint == claims_fingerprint(claims)
    with pytest.raises(ValueError):
        principal_from_claims(
            {"role": "hq_admin"},
            issuer="https://idp.example.com",
            role="viewer",
            capabilities=frozenset(),
            scopes=frozenset(),
        )
