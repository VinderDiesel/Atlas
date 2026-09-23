"""ADR-0031 D02/D13 认证路由：OIDC BFF 的 HTTP 装配（/api/v1/auth/*）。

四端点（D13 表内路径；「所有表内路径加 /api/v1 前缀」）：
- GET  /auth/login    → 302 授权 URL（PKCE + state + nonce）；未配置/配置不全 503
- GET  /auth/callback → 严格验证后 302 + 严格属性会话 Cookie；state 无效 422、token 失败 401
- POST /auth/logout   → CSRF + 同源 Origin 校验 → 撤销会话 + 清 Cookie（204）
- GET  /auth/session  → 当前身份、能力与 CSRF token；不返回 IdP token

错误统一体（D13）：`{"error":{"code","message","request_id"}}`，不含底层异常。
状态码语义（D13）：401 无认证、403 能力不足、422 合同不合法、503 依赖不可用。

装配契约：`build_auth_router(bff_factory, prefix=...)` 由 serving/api.py 挂载
（路径前缀单一事实源在 api.py；**必须在 SPA catch-all 之前 include**）。
CSRF 头名 `X-Atlas-CSRF`（token 由 GET /auth/session 下发；前端只用内存，不入存储）。
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from serving.control.auth import ControlGrantError
from serving.control.oidc import (
    SESSION_COOKIE_NAME,
    OidcBff,
    OidcConfigError,
    OidcSessionError,
    OidcStateError,
    OidcTokenError,
    Session,
    clear_session_cookie,
    reject_dual_credentials,
    set_session_cookie,
    verify_csrf,
    verify_origin,
)

CSRF_HEADER = "X-Atlas-CSRF"

# 惰性 BFF 工厂：返回 BFF（私有模式）或 None（演示模式）；配置阻塞抛
# OidcConfigError / ControlGrantError（由 _bff_or_error 投影为 503）。
BffFactory = Callable[[], "OidcBff | None"]


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    """统一错误体（D13）；request_id 每响生成，不暴露底层异常与凭据。"""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "request_id": uuid4().hex}},
    )


def _bff_or_error(factory: BffFactory) -> tuple[OidcBff | None, JSONResponse | None]:
    """解析 BFF；配置阻塞三态投影（D02 ②：不伪装登录成功，不半启用）：

    - 全空（演示模式）→ 503 `oidc_not_configured`（前端据此显示演示模式角色切换）
    - 部分配置 → 503 `oidc_config_incomplete`（message 列缺失变量名，部署者自诊）
    - 授予配置非法 → 503 `control_grants_invalid`（fail-closed，不默认放行）
    """
    try:
        bff = factory()
    except OidcConfigError as exc:
        return None, _error(503, "oidc_config_incomplete", str(exc))
    except ControlGrantError as exc:
        return None, _error(503, "control_grants_invalid", str(exc))
    if bff is None:
        return None, _error(
            503, "oidc_not_configured", "私有登录未配置（演示模式；ATLAS_OIDC_* 全为空）"
        )
    return bff, None


def _cookie_session(request: Request, bff: OidcBff) -> tuple[Session | None, JSONResponse | None]:
    """Cookie 会话前置（logout / session 共用）：双凭据 → 无 Cookie → 会话无效，均 401。"""
    try:
        reject_dual_credentials(
            cookie_session_id=request.cookies.get(SESSION_COOKIE_NAME),
            bearer_token=request.headers.get("Authorization"),
        )
    except OidcSessionError as exc:
        return None, _error(401, "dual_credentials", str(exc))
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return None, _error(401, "not_authenticated", "缺少会话 Cookie，请先登录")
    session = bff.get_session(cookie)
    if session is None:
        return None, _error(401, "session_expired", "会话已过期或已失效，请重新登录")
    return session, None


def build_auth_router(bff_factory: BffFactory, *, prefix: str) -> APIRouter:
    """构造认证面 router；prefix 由装配方传入（前缀单一事实源在 serving/api.py）。"""
    router = APIRouter(prefix=prefix, tags=["auth"])

    @router.get(
        "/login",
        response_model=None,
        summary="发起 OIDC 登录（302 到 IdP 授权端点；PKCE + state + nonce）",
    )
    def login() -> JSONResponse | RedirectResponse:
        bff, error = _bff_or_error(bff_factory)
        if error is not None:
            return error
        assert bff is not None
        try:
            start = bff.begin_login()
        except OidcTokenError as exc:
            # discovery/JWKS 不可达或 issuer 不符（端点劫持防御）→ 依赖不可用，不重定向
            return _error(503, "oidc_idp_unavailable", str(exc))
        return RedirectResponse(start.authorize_url, status_code=302)

    @router.get(
        "/callback",
        response_model=None,
        summary="OIDC 回调（state 一次性 + 固定 RS256 验签）",
    )
    def callback(request: Request) -> JSONResponse | RedirectResponse:
        bff, error = _bff_or_error(bff_factory)
        if error is not None:
            return error
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        if not code:
            return _error(422, "missing_code", "回调缺少 code 参数")
        if not state:
            return _error(422, "missing_state", "回调缺少 state 参数")
        assert bff is not None
        try:
            principal = bff.complete_login(code=code, state=state)
        except OidcStateError as exc:
            return _error(422, "oidc_state_invalid", str(exc))
        except OidcTokenError as exc:
            return _error(401, "oidc_token_invalid", str(exc))
        session = bff.create_session(principal)
        response = RedirectResponse("/", status_code=302)
        set_session_cookie(response, session)
        return response

    @router.post(
        "/logout",
        status_code=204,
        response_model=None,
        summary="退出（CSRF + 同源校验；撤销会话并清 Cookie）",
    )
    def logout(request: Request) -> JSONResponse | Response:
        bff, error = _bff_or_error(bff_factory)
        if error is not None:
            return error
        assert bff is not None
        session, error = _cookie_session(request, bff)
        if error is not None:
            return error
        assert session is not None
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        try:
            verify_origin(request.headers.get("Origin"), expected_origin=expected_origin)
        except OidcSessionError as exc:
            return _error(403, "origin_rejected", str(exc))
        try:
            verify_csrf(session, request.headers.get(CSRF_HEADER))
        except OidcSessionError as exc:
            return _error(403, "csrf_failed", str(exc))
        bff.revoke_session(session.session_id)
        response = Response(status_code=204)
        clear_session_cookie(response)
        return response

    @router.get(
        "/session",
        response_model=None,
        summary="当前会话身份、能力与 CSRF token（不返回 IdP token）",
    )
    def session_info(request: Request) -> JSONResponse:
        bff, error = _bff_or_error(bff_factory)
        if error is not None:
            return error
        assert bff is not None
        session, error = _cookie_session(request, bff)
        if error is not None:
            return error
        assert session is not None
        principal = session.principal
        return JSONResponse(
            {
                "subject": principal.subject,
                "issuer": principal.issuer,
                "role": principal.role,
                "capabilities": sorted(principal.capabilities),
                "scopes": sorted(principal.scopes),
                "csrf_token": session.csrf_token,
                "expires_at": session.expires_at,
            }
        )

    return router
