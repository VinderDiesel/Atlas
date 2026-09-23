"""T03d HTTP 装配：/api/v1/auth/* 四端点契约（ADR-0031 D02/D13 表内路径）。

受控 IdP 走 tests/oidc_support.py（httpx2.MockTransport，不触网、不付费）；
断言的是本仓路由装配、统一错误体、Cookie 严格属性与 CSRF/Origin 防护，不是桩。

覆盖：演示模式 503 配置阻塞（登录/身份探测两态）、部分配置 503 列缺失名、
IdP 端点劫持 → 503、login 302 授权 URL（PKCE/state/nonce）、callback 302+
Set-Cookie 严格属性、state 重放/伪造 422、缺参数 422 统一体、token 交换失败
401、session 200 身份/能力/CSRF（不含 IdP token）、无 Cookie 401、过期 401、
双凭据 401、logout CSRF 403 / 跨域与缺失 Origin 403 / 成功 204+清 Cookie、
撤销后 401、无前缀路径 404。

状态码映射（D13）：401 无认证、403 能力不足、422 合同不合法、503 依赖不可用。
"""

from __future__ import annotations

import re
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from serving.api import create_app
from serving.audit import AuditLog
from serving.control.auth import parse_control_grants
from serving.control.oidc import SESSION_COOKIE_NAME, SESSION_TTL_SECONDS
from tests.oidc_support import ISSUER, OIDC_ENV_NAMES, FakeClock, FakeIdp, stack

API = "/api/v1"
LOGIN = f"{API}/auth/login"
CALLBACK = f"{API}/auth/callback"
LOGOUT = f"{API}/auth/logout"
SESSION = f"{API}/auth/session"

# 私有部署走 TLS：https base_url 让 Secure Cookie 在 httpx cookie jar 中正常流转；
# expected_origin = scheme://netloc = 本常量（logout 的 Origin 校验基准）
BASE_URL = "https://testserver"
ORIGIN = BASE_URL
CSRF_HEADER = "X-Atlas-CSRF"


def _assert_error(resp, status: int, code: str) -> dict:
    """统一错误体断言（D13）：{"error":{code,message,request_id}}，不含底层异常。"""
    assert resp.status_code == status, f"期望 {status}，实得 {resp.status_code}：{resp.text}"
    body = resp.json()
    assert set(body) == {"error"}, f"错误体应只有 error 键：{body}"
    error = body["error"]
    assert set(error) == {"code", "message", "request_id"}, f"错误对象形态漂移：{error}"
    assert error["code"] == code
    assert error["message"]
    assert re.fullmatch(r"[0-9a-f]{32}", error["request_id"]), "request_id 应为 uuid4 hex"
    return error


def _private_client(
    tmp_path,
    *,
    grants_raw: str | None = "user-1|editor|finance",
    discovery_issuer: str | None = None,
) -> tuple[TestClient, FakeIdp, FakeClock]:
    """私有模式客户端：注入受控 BFF（MockTransport，不触网）。"""
    grants = parse_control_grants(grants_raw) if grants_raw else None
    clock, idp, bff = stack(grants=grants, discovery_issuer=discovery_issuer)
    app = create_app(audit=AuditLog(tmp_path), oidc_bff_factory=lambda: bff)
    return TestClient(app, base_url=BASE_URL), idp, clock


@pytest.fixture()
def private(tmp_path):
    """私有模式（已配置 OIDC）：受控 BFF + 授权授予 user-1|editor|finance。"""
    client, idp, clock = _private_client(tmp_path)
    with client:
        yield client, idp, clock


@pytest.fixture()
def demo(monkeypatch, tmp_path):
    """演示模式（ATLAS_OIDC_* 全空）：走缺省惰性 env 解析的真实路径，不注入。"""
    for name in OIDC_ENV_NAMES:
        monkeypatch.setenv(name, "")
    client = TestClient(create_app(audit=AuditLog(tmp_path)), base_url=BASE_URL)
    with client:
        yield client


def _http_login(client: TestClient, idp: FakeIdp, *, sub: str = "user-1") -> None:
    """完整 HTTP 登录：login 302 → 取 state/nonce 签 token → callback 302；Cookie 落 jar。"""
    start = client.get(LOGIN, follow_redirects=False)
    assert start.status_code == 302, start.text
    query = parse_qs(urlparse(start.headers["location"]).query)
    idp.claims_overrides = {"sub": sub, "nonce": query["nonce"][0]}
    done = client.get(f"{CALLBACK}?code=code-1&state={query['state'][0]}", follow_redirects=False)
    assert done.status_code == 302, done.text


def _csrf(client: TestClient) -> str:
    return str(client.get(SESSION).json()["csrf_token"])


# ---------------------------------------------------------------------------
# 演示模式与配置阻塞（D02 ②：未配置显示阻塞，不伪装登录成功）
# ---------------------------------------------------------------------------


def test_demo_mode_login_blocked_503(demo) -> None:
    resp = demo.get(LOGIN, follow_redirects=False)
    _assert_error(resp, 503, "oidc_not_configured")
    assert "location" not in resp.headers, "演示模式不得重定向到任何 IdP"
    assert "set-cookie" not in resp.headers


def test_demo_mode_session_reports_config_block(demo) -> None:
    """前端模式探测：演示模式 503 + oidc_not_configured（区别于私有模式未登录 401）。"""
    _assert_error(demo.get(SESSION), 503, "oidc_not_configured")


def test_demo_mode_logout_blocked_503(demo) -> None:
    _assert_error(demo.post(LOGOUT, headers={"Origin": ORIGIN}), 503, "oidc_not_configured")


def test_partial_config_blocks_with_missing_names(monkeypatch, tmp_path) -> None:
    """部分配置 = 部署阻塞：503 + 列出缺失变量名（不半启用、不伪装演示模式）。"""
    for name in OIDC_ENV_NAMES:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("ATLAS_OIDC_ISSUER", ISSUER)
    client = TestClient(create_app(audit=AuditLog(tmp_path)), base_url=BASE_URL)
    with client:
        error = _assert_error(
            client.get(LOGIN, follow_redirects=False), 503, "oidc_config_incomplete"
        )
    assert "ATLAS_OIDC_CLIENT_ID" in error["message"]


def test_login_idp_endpoint_mismatch_503(tmp_path) -> None:
    """discovery issuer 与配置不符（端点劫持防御）→ 503 依赖不可用，不重定向。"""
    client, _, _ = _private_client(tmp_path, discovery_issuer="https://evil.example.com")
    with client:
        resp = client.get(LOGIN, follow_redirects=False)
        _assert_error(resp, 503, "oidc_idp_unavailable")
        assert "location" not in resp.headers


# ---------------------------------------------------------------------------
# 登录流：login 302 与 callback 全链
# ---------------------------------------------------------------------------


def test_login_redirects_to_authorize_url_with_pkce(private) -> None:
    client, _, _ = private
    resp = client.get(LOGIN, follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(f"{ISSUER}/authorize")
    query = parse_qs(urlparse(location).query)
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    assert query["state"][0]
    assert query["nonce"][0]
    assert "set-cookie" not in resp.headers, "会话只在 callback 成功后建立"


def test_callback_sets_strict_session_cookie(private) -> None:
    client, idp, _ = private
    start = client.get(LOGIN, follow_redirects=False)
    query = parse_qs(urlparse(start.headers["location"]).query)
    idp.claims_overrides = {"sub": "user-1", "nonce": query["nonce"][0]}
    resp = client.get(f"{CALLBACK}?code=code-1&state={query['state'][0]}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"
    cookie = resp.headers["set-cookie"]
    lowered = cookie.lower()
    assert cookie.startswith(f"{SESSION_COOKIE_NAME}=")
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=lax" in lowered


def test_session_returns_identity_and_csrf_without_idp_tokens(private) -> None:
    client, idp, _ = private
    _http_login(client, idp)
    resp = client.get(SESSION)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "subject",
        "issuer",
        "role",
        "capabilities",
        "scopes",
        "csrf_token",
        "expires_at",
    }
    assert body["subject"] == "user-1"
    assert body["issuer"] == ISSUER
    assert body["role"] == "editor"
    assert body["capabilities"] == sorted(body["capabilities"]), "能力列表应排序（稳定输出）"
    assert "draft.edit" in body["capabilities"]
    assert body["scopes"] == ["finance"]
    assert body["csrf_token"]
    assert isinstance(body["expires_at"], int)
    assert "id_token" not in resp.text and "access_token" not in resp.text, (
        "session 不得返回 IdP token（D13 原文）"
    )


def test_callback_state_is_single_use_422(private) -> None:
    client, idp, _ = private
    start = client.get(LOGIN, follow_redirects=False)
    query = parse_qs(urlparse(start.headers["location"]).query)
    idp.claims_overrides = {"sub": "user-1", "nonce": query["nonce"][0]}
    url = f"{CALLBACK}?code=code-1&state={query['state'][0]}"
    assert client.get(url, follow_redirects=False).status_code == 302
    _assert_error(client.get(url, follow_redirects=False), 422, "oidc_state_invalid")


def test_callback_forged_state_422(private) -> None:
    client, _, _ = private
    _assert_error(client.get(f"{CALLBACK}?code=code-1&state=forged"), 422, "oidc_state_invalid")


def test_callback_missing_params_422_uniform_body(private) -> None:
    """缺参不落 FastAPI 默认 detail 数组——auth 面全部走统一错误体。"""
    client, _, _ = private
    _assert_error(client.get(CALLBACK), 422, "missing_code")
    _assert_error(client.get(f"{CALLBACK}?code=code-1"), 422, "missing_state")


def test_callback_token_failure_401(private) -> None:
    client, idp, _ = private
    start = client.get(LOGIN, follow_redirects=False)
    query = parse_qs(urlparse(start.headers["location"]).query)
    idp.omit_id_token = True
    resp = client.get(f"{CALLBACK}?code=code-1&state={query['state'][0]}", follow_redirects=False)
    _assert_error(resp, 401, "oidc_token_invalid")
    assert "set-cookie" not in resp.headers, "失败回调不得建立会话"


# ---------------------------------------------------------------------------
# 会话读取：401 形态（D13：401 无认证）
# ---------------------------------------------------------------------------


def test_session_without_cookie_401(private) -> None:
    client, _, _ = private
    _assert_error(client.get(SESSION), 401, "not_authenticated")


def test_session_expired_401(private) -> None:
    client, idp, clock = private
    _http_login(client, idp)
    clock.advance(SESSION_TTL_SECONDS + 1)
    _assert_error(client.get(SESSION), 401, "session_expired")


def test_session_dual_credentials_401(private) -> None:
    """Cookie 与 Bearer 同现 → 拒绝，不混用身份来源（D02 ④）。"""
    client, idp, _ = private
    _http_login(client, idp)
    resp = client.get(SESSION, headers={"Authorization": "Bearer forged-token"})
    _assert_error(resp, 401, "dual_credentials")


# ---------------------------------------------------------------------------
# 退出：CSRF + Origin 防护与撤销
# ---------------------------------------------------------------------------


def test_logout_without_csrf_403_and_session_survives(private) -> None:
    client, idp, _ = private
    _http_login(client, idp)
    _assert_error(client.post(LOGOUT, headers={"Origin": ORIGIN}), 403, "csrf_failed")
    assert client.get(SESSION).status_code == 200, "CSRF 失败不得撤销会话"


def test_logout_cross_origin_rejected(private) -> None:
    client, idp, _ = private
    _http_login(client, idp)
    csrf = _csrf(client)
    resp = client.post(LOGOUT, headers={"Origin": "https://evil.example.com", CSRF_HEADER: csrf})
    _assert_error(resp, 403, "origin_rejected")
    assert client.get(SESSION).status_code == 200


def test_logout_missing_origin_rejected(private) -> None:
    """缺失 Origin 一律拒绝（fail-closed）：Cookie 写请求只服务同源浏览器。"""
    client, idp, _ = private
    _http_login(client, idp)
    _assert_error(client.post(LOGOUT, headers={CSRF_HEADER: _csrf(client)}), 403, "origin_rejected")


def test_logout_without_session_401(private) -> None:
    client, _, _ = private
    _assert_error(client.post(LOGOUT, headers={"Origin": ORIGIN}), 401, "not_authenticated")


def test_logout_revokes_session_and_clears_cookie(private) -> None:
    client, idp, _ = private
    _http_login(client, idp)
    resp = client.post(LOGOUT, headers={"Origin": ORIGIN, CSRF_HEADER: _csrf(client)})
    assert resp.status_code == 204
    cookie = resp.headers.get("set-cookie", "").lower()
    assert cookie.startswith(f'{SESSION_COOKIE_NAME}="";'), f"应清空会话 Cookie：{cookie}"
    assert "max-age=0" in cookie
    _assert_error(client.get(SESSION), 401, "not_authenticated")


# ---------------------------------------------------------------------------
# 前缀硬切（D13：所有表内路径加 /api/v1 前缀；无前缀不留别名）
# ---------------------------------------------------------------------------


def test_unprefixed_auth_paths_404(tmp_path) -> None:
    no_dist = tmp_path / "no-dist"
    with mock.patch("serving.api.FRONTEND_DIST", no_dist):
        app = create_app(audit=AuditLog(tmp_path))
    with TestClient(app, base_url=BASE_URL) as client:
        for path in ("/auth/login", "/auth/callback", "/auth/session"):
            assert client.get(path, follow_redirects=False).status_code == 404, (
                f"{path} 无前缀仍可达——双份契约并存"
            )
        assert client.post("/auth/logout", headers={"Origin": ORIGIN}).status_code == 404
