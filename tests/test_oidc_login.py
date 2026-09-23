"""T03 OIDC BFF：Authorization Code + PKCE、固定验签、会话 Cookie 与 CSRF（ADR-0031 D02/D13）。

外部 IdP 全部走 httpx2.MockTransport 受控 HTTP——不触网、不付费、不使用真实 token；
id_token 用 joserfc 真实 RS256 签名，被验证的是本仓的验签与流程逻辑而非桩。
覆盖：state 一次性/过期、PKCE、nonce、issuer/audience/exp 校验、未知 kid 与轮换刷新、
算法拒绝（HS256 混淆）、HS256 明文拒绝、Cookie 属性、CSRF、Origin、双凭据冲突、
会话过期/撤销/跨用户隔离、控制授予（fail-closed）与配置阻塞。
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from starlette.responses import Response

from tests.oidc_support import (
    AUDIENCE,
    CLIENT_ID,
    FULL_ENV,
    ISSUER,
    REDIRECT_URI,
)
from tests.oidc_support import login as _login
from tests.oidc_support import nonce_of as _nonce_of
from tests.oidc_support import stack as _stack

# ---------------------------------------------------------------------------
# 配置与部署模式
# ---------------------------------------------------------------------------


def test_settings_all_blank_means_demo_mode() -> None:
    """全部留空 = 演示模式（None），不得报错也不得伪装私有配置。"""
    from serving.control.oidc import OidcSettings

    assert OidcSettings.from_env({}) is None
    assert OidcSettings.from_env(dict.fromkeys(FULL_ENV, "")) is None


def test_settings_partial_config_blocked_with_missing_names() -> None:
    """部分配置 = 配置阻塞：报出缺失变量名，不静默半启用。"""
    from serving.control.oidc import OidcConfigError, OidcSettings

    with pytest.raises(OidcConfigError) as excinfo:
        OidcSettings.from_env({"ATLAS_OIDC_ISSUER": ISSUER})
    message = str(excinfo.value)
    assert "ATLAS_OIDC_CLIENT_ID" in message
    assert "ATLAS_OIDC_CLIENT_SECRET" in message


def test_settings_complete_enters_private_mode() -> None:
    """五个变量齐备 = 私有模式：固定 issuer/audience/redirect URI 原样入配置。"""
    from serving.control.oidc import OidcSettings

    settings = OidcSettings.from_env(FULL_ENV)
    assert settings is not None
    assert settings.issuer == ISSUER
    assert settings.client_id == CLIENT_ID
    assert settings.audience == AUDIENCE
    assert settings.redirect_uri == REDIRECT_URI


# ---------------------------------------------------------------------------
# 登录开始与 state 一次性
# ---------------------------------------------------------------------------


def test_begin_login_builds_pkce_authorize_url() -> None:
    """授权 URL：code + PKCE(S256) + state + nonce + 固定 redirect_uri。"""
    _, _, bff = _stack()
    start = bff.begin_login()
    query = parse_qs(urlparse(start.authorize_url).query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["state"] == [start.state]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    assert query["nonce"][0]


def test_complete_login_rejects_unknown_state_without_touching_idp() -> None:
    """state 未知 → 拒绝，且不向 IdP 发任何请求（校验顺序在交换之前）。"""
    from serving.control.oidc import OidcStateError

    _, idp, bff = _stack()
    with pytest.raises(OidcStateError):
        bff.complete_login(code="code-1", state="forged-state")
    assert idp.token_calls == 0
    assert idp.discovery_calls == 0


def test_state_is_single_use() -> None:
    """同一 state 第二次提交（重放）→ 拒绝。"""
    from serving.control.oidc import OidcStateError

    _, idp, bff = _stack()
    _login(bff, idp)
    start = bff.begin_login()
    idp.claims_overrides = {"sub": "user-1", "nonce": _nonce_of(start.authorize_url)}
    bff.complete_login(code="code-1", state=start.state)
    with pytest.raises(OidcStateError):
        bff.complete_login(code="code-1", state=start.state)


def test_pending_login_expires() -> None:
    """pending 超时（默认 10 分钟）后 state 不再可用，且不触达 IdP。"""
    from serving.control.oidc import PENDING_TTL_SECONDS, OidcStateError

    clock, idp, bff = _stack()
    start = bff.begin_login()
    clock.advance(PENDING_TTL_SECONDS + 1)
    with pytest.raises(OidcStateError):
        bff.complete_login(code="code-1", state=start.state)
    assert idp.token_calls == 0


# ---------------------------------------------------------------------------
# id_token 验签与 claims 校验
# ---------------------------------------------------------------------------


def test_complete_login_happy_path_returns_principal() -> None:
    """完整链路：state/nonce/PKCE/验签全过 → Principal（issuer/subject/指纹来自 claims）。"""
    _, idp, bff = _stack()
    principal = _login(bff, idp)
    assert principal.issuer == ISSUER
    assert principal.subject == "user-1"
    assert principal.auth_fingerprint
    assert idp.token_calls == 1


def test_complete_login_rejects_hs256_confusion() -> None:
    """算法拒绝：HS256 混淆 token（非固定算法）一律拒绝，绝不按 header.alg 选信任模式。"""
    from serving.control.oidc import OidcTokenError

    _, idp, bff = _stack()
    idp.alg = "HS256"
    with pytest.raises(OidcTokenError):
        _login(bff, idp)


def test_complete_login_rejects_wrong_issuer() -> None:
    """issuer 不符（token 由其它主体签发）→ 拒绝。"""
    from serving.control.oidc import OidcTokenError

    _, idp, bff = _stack()
    with pytest.raises(OidcTokenError):
        _login(bff, idp, iss="https://evil.example.com")


def test_complete_login_rejects_wrong_audience() -> None:
    """audience 不符（token 签给别的客户端）→ 拒绝。"""
    from serving.control.oidc import OidcTokenError

    _, idp, bff = _stack()
    with pytest.raises(OidcTokenError):
        _login(bff, idp, aud="other-client")


def test_complete_login_rejects_expired_id_token() -> None:
    """exp 已过 → 拒绝。"""
    from serving.control.oidc import OidcTokenError

    clock, idp, bff = _stack()
    with pytest.raises(OidcTokenError):
        _login(bff, idp, exp=clock() - 10)


def test_complete_login_rejects_nonce_mismatch() -> None:
    """nonce 与发起登录时的 pending 不符 → 拒绝（重放/串会话防御）。"""
    from serving.control.oidc import OidcTokenError

    _, idp, bff = _stack()
    with pytest.raises(OidcTokenError):
        _login(bff, idp, nonce="another-nonce")


def test_unknown_kid_after_refresh_rejected() -> None:
    """未知 kid：JWKS 初次缓存查不到 → 刷新一次再查 → 仍无 → 拒绝。"""
    from serving.control.oidc import OidcTokenError

    _, idp, bff = _stack()
    idp.kid = "kid-9"  # JWKS 里永远没有
    with pytest.raises(OidcTokenError):
        _login(bff, idp)
    assert idp.jwks_calls == 2  # 初次 + 轮换刷新各一次


def test_rotated_kid_accepted_after_refresh() -> None:
    """JWKS 轮换：新 kid 不在旧缓存，刷新后拿到 → 登录成功。"""
    _, idp, bff = _stack(kid="kid-2")
    idp.jwks_versions = [idp._jwks("kid-1"), idp._jwks("kid-2")]
    principal = _login(bff, idp)
    assert principal.subject == "user-1"
    assert idp.jwks_calls == 2


def test_complete_login_rejects_missing_id_token() -> None:
    """token 响应缺少 id_token → 拒绝（不接受裸 access_token 冒充登录）。"""
    from serving.control.oidc import OidcTokenError

    _, idp, bff = _stack()
    idp.omit_id_token = True
    with pytest.raises(OidcTokenError):
        _login(bff, idp)


def test_discovery_issuer_mismatch_rejected() -> None:
    """discovery 文档 issuer 与配置不符（端点劫持）→ 拒绝。"""
    from serving.control.oidc import OidcTokenError

    _, _, bff = _stack(discovery_issuer="https://evil.example.com")
    with pytest.raises(OidcTokenError):
        bff.begin_login()


def test_discovery_is_fetched_once() -> None:
    """discovery 文档缓存：多次登录只拉取一次。"""
    _, idp, bff = _stack()
    _login(bff, idp, sub="user-1")
    _login(bff, idp, sub="user-2")
    assert idp.discovery_calls == 1


# ---------------------------------------------------------------------------
# 会话：创建 / 过期 / 撤销 / 隔离 / Cookie 属性
# ---------------------------------------------------------------------------


def _session_for(bff, idp, sub: str):
    principal = _login(bff, idp, sub=sub)
    return bff.create_session(principal)


def test_sessions_are_user_isolated() -> None:
    """跨用户隔离：两个会话各自绑定创建者身份，会话与 CSRF 值互不相同。"""
    _, idp, bff = _stack()
    session1 = _session_for(bff, idp, "user-1")
    session2 = _session_for(bff, idp, "user-2")
    assert session1.session_id != session2.session_id
    assert session1.csrf_token != session2.csrf_token
    restored1 = bff.get_session(session1.session_id)
    restored2 = bff.get_session(session2.session_id)
    assert restored1 is not None and restored1.principal.subject == "user-1"
    assert restored2 is not None and restored2.principal.subject == "user-2"


def test_session_expires_and_is_evicted() -> None:
    """会话过期：超过 TTL 读取返回 None（重新登录），不是无限期会话。"""
    from serving.control.oidc import SESSION_TTL_SECONDS

    clock, idp, bff = _stack()
    session = _session_for(bff, idp, "user-1")
    clock.advance(SESSION_TTL_SECONDS + 1)
    assert bff.get_session(session.session_id) is None


def test_logout_revokes_session() -> None:
    """退出失效：撤销后旧 cookie 值不再可用；重复撤销返回 False。"""
    _, idp, bff = _stack()
    session = _session_for(bff, idp, "user-1")
    assert bff.revoke_session(session.session_id) is True
    assert bff.get_session(session.session_id) is None
    assert bff.revoke_session(session.session_id) is False


def test_session_cookie_attributes_are_strict() -> None:
    """会话 Cookie：HttpOnly + Secure + SameSite=Lax + 服务端 max-age；清理发 Max-Age=0。"""
    from serving.control.oidc import SESSION_COOKIE_NAME, clear_session_cookie, set_session_cookie

    _, idp, bff = _stack()
    session = _session_for(bff, idp, "user-1")
    response = Response()
    set_session_cookie(response, session)
    header = response.headers["set-cookie"]
    lowered = header.lower()
    assert header.startswith(f"{SESSION_COOKIE_NAME}={session.session_id}")
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=lax" in lowered

    cleared = Response()
    clear_session_cookie(cleared)
    assert cleared.headers["set-cookie"].lower().startswith(f'{SESSION_COOKIE_NAME}="";')
    assert "max-age=0" in cleared.headers["set-cookie"].lower()


# ---------------------------------------------------------------------------
# 请求级校验：双凭据 / CSRF / Origin
# ---------------------------------------------------------------------------


def test_dual_credentials_rejected() -> None:
    """身份不得同时来自 Cookie 与 Bearer（D02 ④）；单凭据各自放行。"""
    from serving.control.oidc import OidcSessionError, reject_dual_credentials

    reject_dual_credentials(cookie_session_id="s-1", bearer_token=None)
    reject_dual_credentials(cookie_session_id=None, bearer_token="t-1")
    with pytest.raises(OidcSessionError):
        reject_dual_credentials(cookie_session_id="s-1", bearer_token="t-1")


def test_csrf_verification() -> None:
    """CSRF：仅接受与会话绑定的 token（常量时间比较）；缺失/错误拒绝。"""
    from serving.control.oidc import OidcSessionError, verify_csrf

    _, idp, bff = _stack()
    session = _session_for(bff, idp, "user-1")
    verify_csrf(session, session.csrf_token)
    for bad in (None, "", "forged-token"):
        with pytest.raises(OidcSessionError):
            verify_csrf(session, bad)


def test_origin_verification() -> None:
    """Cookie 写请求同源校验：缺失 Origin 或跨域 Origin 一律拒绝（fail-closed）。"""
    from serving.control.oidc import OidcSessionError, verify_origin

    expected = "http://localhost:8000"
    verify_origin(expected, expected_origin=expected)
    for bad in (None, "", "https://evil.example.com", "http://localhost:8001"):
        with pytest.raises(OidcSessionError):
            verify_origin(bad, expected_origin=expected)


# ---------------------------------------------------------------------------
# 控制授予：解析、合并与 fail-closed
# ---------------------------------------------------------------------------


def test_parse_control_grants_merges_entries() -> None:
    """授予条目解析：同一 subject 多条合并（角色与 scope 取并集）。"""
    from serving.control.auth import parse_control_grants

    grants = parse_control_grants(
        "alice|editor,reviewer|finance;bob|viewer|finance,retail;alice|publisher|retail"
    )
    assert grants["alice"].roles == ("editor", "reviewer", "publisher")
    assert grants["alice"].scopes == frozenset({"finance", "retail"})
    assert grants["bob"].roles == ("viewer",)
    assert grants["bob"].scopes == frozenset({"finance", "retail"})


def test_parse_control_grants_rejects_bad_config() -> None:
    """非法授予配置 fail-fast：未知角色 / 缺段 / 空 subject 均拒绝，不静默忽略。"""
    from serving.control.auth import ControlGrantError, parse_control_grants

    for bad in ("x|admin|finance", "alice|editor", "|editor|finance"):
        with pytest.raises(ControlGrantError):
            parse_control_grants(bad)


def test_login_without_grant_is_fail_closed() -> None:
    """未列出的 subject：可登录但零能力零 scope，任何动作被拒（fail-closed）。"""
    from serving.control.auth import ControlForbidden, authorize

    _, idp, bff = _stack()
    principal = _login(bff, idp)
    assert principal.capabilities == frozenset()
    assert principal.scopes == frozenset()
    with pytest.raises(ControlForbidden):
        authorize(principal, "run.create", "finance")


def test_login_with_grant_derives_capabilities_and_scopes() -> None:
    """授予 alice|editor|finance：能力来自模板、scope 来自配置，角色外与 scope 外均拒绝。"""
    from serving.control.auth import ControlForbidden, authorize, parse_control_grants

    grants = parse_control_grants("user-1|editor|finance")
    _, idp, bff = _stack(grants=grants)
    principal = _login(bff, idp)
    assert principal.role == "editor"
    assert principal.scopes == frozenset({"finance"})
    authorize(principal, "draft.edit", "finance")
    with pytest.raises(ControlForbidden):
        authorize(principal, "release.publish", "finance")
    with pytest.raises(ControlForbidden):
        authorize(principal, "draft.edit", "retail")


def test_multi_role_grant_union_capabilities() -> None:
    """兼任多角色（D02）：能力为模板并集，仍各自显式动作、逐条审核。"""
    from serving.control.auth import authorize, parse_control_grants

    grants = parse_control_grants("user-1|editor,reviewer|finance")
    _, idp, bff = _stack(grants=grants)
    principal = _login(bff, idp)
    assert principal.role == "editor,reviewer"
    authorize(principal, "draft.edit", "finance")
    authorize(principal, "draft.review", "finance")
    assert "release.publish" not in principal.capabilities
