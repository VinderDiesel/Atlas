"""ADR-0031 D02/D13 OIDC BFF：Authorization Code + PKCE（服务的私有登录后端）。

纪律（与 D02 身份方案逐条对应）：

1. **固定模式**：issuer/audience/redirect URI 全部来自部署配置；端点只从
   固定 issuer 的 discovery 文档读取，且校验文档 issuer 与配置一致
   （端点劫持防御）——不拼猜路径、不信任任意重定向。
2. **验签固定 RS256**：显式检查 header.alg + joserfc `algorithms=["RS256"]`
   双保险；绝不按客户端 header.alg 自动选择信任模式（D02 ④）。未知 kid
   触发一次 JWKS 轮换刷新，仍未知即拒绝。
3. **token 只在服务端内存**：id_token/access_token 不返回浏览器；浏览器只收
   不透明 `HttpOnly; Secure; SameSite=Lax` 会话 Cookie（D02 ③）。pending 与
   session 都是进程内字典——服务重启 = 全部失效，要求重新登录。
4. **写请求防护**：Cookie 与 Bearer 不得同时出现；Cookie 写请求要求 CSRF 与会话
   绑定、Origin 与部署源同源（缺一律拒绝，fail-closed）。
5. **配置阻塞不伪装**：OIDC 变量部分配置 → OidcConfigError（服务层投影 503），
   绝不半启用；凭据/token 不入日志与响应。

外部 HTTP 全部经注入 transport（测试用 httpx2.MockTransport 受控响应，不触网）。
"""

from __future__ import annotations

import base64
import hmac
import json
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast

import httpx2
from authlib.integrations.httpx_client import OAuth2Client
from joserfc import jwt as joserfc_jwt
from joserfc.errors import InvalidKeyIdError, JoseError
from joserfc.jwk import KeyFlexible, KeySet, KeySetSerialization
from joserfc.jwt import JWTClaimsRegistry
from starlette.responses import Response

from serving.control.auth import ControlGrant, Principal, principal_for_oidc

SESSION_COOKIE_NAME = "atlas_session"
SESSION_TTL_SECONDS = 8 * 3600  # 一个工作日的服务端会话；重启即失效（内存态）
PENDING_TTL_SECONDS = 600  # 登录中途（授权跳转 → 回调）允许的最长间隔
_JOSE_ALGORITHMS = ("RS256",)  # 固定；绝不按 header.alg 选信任模式

_ENV_ISSUER = "ATLAS_OIDC_ISSUER"
_ENV_CLIENT_ID = "ATLAS_OIDC_CLIENT_ID"
_ENV_CLIENT_SECRET = "ATLAS_OIDC_CLIENT_SECRET"
_ENV_REDIRECT_URI = "ATLAS_OIDC_REDIRECT_URI"
_ENV_AUDIENCE = "ATLAS_OIDC_AUDIENCE"
_ENV_NAMES = (_ENV_ISSUER, _ENV_CLIENT_ID, _ENV_CLIENT_SECRET, _ENV_REDIRECT_URI, _ENV_AUDIENCE)


class OidcError(Exception):
    """OIDC BFF 基类错误（服务层按子类投影状态码，不返回底层异常细节）。"""


class OidcConfigError(OidcError):
    """配置不完整/非法：登录不可用（D13 503 依赖不可用），不伪装成功。"""


class OidcStateError(OidcError):
    """state 未知/重放/过期：登录请求无效，需重新发起。"""


class OidcTokenError(OidcError):
    """token 交换或 id_token 验签/claims 校验失败。"""


class OidcSessionError(OidcError):
    """会话级拒绝：CSRF/Origin/双凭据/会话缺失或过期。"""


@dataclass(frozen=True)
class OidcSettings:
    """私有模式 OIDC 配置（全部来自部署环境；固定值，不做运行时改写）。"""

    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    audience: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OidcSettings | None:
        """读取部署环境：全空 → None（演示模式）；部分配置 → OidcConfigError。

        任一非空即视为声明了私有模式：此时缺任何一项都阻塞登录并列出
        缺失变量名（D02 ②：未配置显示配置阻塞，不伪装登录成功）。
        """
        import os

        env = os.environ if env is None else env
        values = {name: (env.get(name) or "").strip() for name in _ENV_NAMES}
        if not any(values.values()):
            return None
        missing = [name for name in _ENV_NAMES if not values[name]]
        if missing:
            raise OidcConfigError(f"OIDC 配置不完整，缺少：{', '.join(missing)}")
        return cls(
            issuer=values[_ENV_ISSUER],
            client_id=values[_ENV_CLIENT_ID],
            client_secret=values[_ENV_CLIENT_SECRET],
            redirect_uri=values[_ENV_REDIRECT_URI],
            audience=values[_ENV_AUDIENCE],
        )


@dataclass(frozen=True)
class LoginStart:
    """登录开始结果：浏览器应跳转的授权 URL 与本次 state。"""

    authorize_url: str
    state: str


@dataclass(frozen=True)
class Session:
    """服务端内存会话：浏览器仅持有不透明的 session_id（Cookie 值）。"""

    session_id: str
    principal: Principal
    csrf_token: str
    created_at: int
    expires_at: int


@dataclass(frozen=True)
class _PendingLogin:
    """登录中途状态（state → nonce/PKCE verifier），一次性且短期。"""

    nonce: str
    code_verifier: str
    expires_at: int


def _peek_header(id_token: str) -> dict[str, object]:
    """解析 id_token 的 header（仅用于取 kid；alg 由固定白名单显式拒绝）。"""
    parts = id_token.split(".")
    if len(parts) != 3:
        raise OidcTokenError("id_token 格式错误：应为三段 base64url")
    padded = parts[0] + "=" * (-len(parts[0]) % 4)
    try:
        header = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError) as exc:
        raise OidcTokenError("id_token header 无法解析") from exc
    if not isinstance(header, dict):
        raise OidcTokenError("id_token header 形态错误")
    return header


class OidcBff:
    """OIDC Authorization Code + PKCE 的 BFF 内核（纯逻辑，无 HTTP 框架依赖）。

    transport：注入 httpx2 transport（测试 MockTransport；生产 None = 默认网络）。
    now：注入时钟（callable → epoch 秒），会话/pending 过期与 exp 校验共用。
    grants：subject → ControlGrant（ATLAS_CONTROL_GRANTS 解析结果）；未授予者
    登录成功但零能力（fail-closed）。
    """

    def __init__(
        self,
        settings: OidcSettings,
        *,
        transport: httpx2.BaseTransport | None = None,
        now: Callable[[], int] | None = None,
        grants: Mapping[str, ControlGrant] | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._now: Callable[[], int] = now if now is not None else (lambda: int(time.time()))
        self._grants: Mapping[str, ControlGrant] = grants if grants is not None else {}
        self._metadata_cache: dict[str, object] | None = None
        self._key_set: KeySet | None = None
        self._pending: dict[str, _PendingLogin] = {}
        self._sessions: dict[str, Session] = {}

    # ------------------------------------------------------------------
    # IdP 访问（discovery / JWKS / token）
    # ------------------------------------------------------------------

    def _get_json(self, url: str) -> dict[str, object]:
        with httpx2.Client(transport=self._transport, timeout=10.0) as client:
            response = client.get(url)
        if response.status_code != 200:
            raise OidcTokenError(f"IdP 端点响应异常：HTTP {response.status_code}")
        try:
            doc = response.json()
        except ValueError as exc:
            raise OidcTokenError("IdP 端点返回非 JSON") from exc
        if not isinstance(doc, dict):
            raise OidcTokenError("IdP 端点返回形态错误")
        return doc

    def _metadata(self) -> dict[str, object]:
        if self._metadata_cache is None:
            url = f"{self._settings.issuer}/.well-known/openid-configuration"
            doc = self._get_json(url)
            if doc.get("issuer") != self._settings.issuer:
                raise OidcTokenError("discovery issuer 与配置不符（端点劫持防御）")
            self._metadata_cache = doc
        return self._metadata_cache

    def _endpoint(self, name: str) -> str:
        value = self._metadata().get(name)
        if not isinstance(value, str) or not value:
            raise OidcTokenError(f"discovery 缺少端点：{name}")
        return value

    def _fetch_jwks(self) -> KeySet:
        doc = self._get_json(self._endpoint("jwks_uri"))
        try:
            return KeySet.import_key_set(cast(KeySetSerialization, doc))
        except (ValueError, KeyError) as exc:
            raise OidcTokenError("JWKS 文档无法解析") from exc

    def _key_for_kid(self, kid: str) -> KeyFlexible:
        """按 kid 取验签公钥；未知 kid → 强制刷新 JWKS 一次（轮换），仍无即拒绝。"""
        if self._key_set is None:
            self._key_set = self._fetch_jwks()
        try:
            return self._key_set.get_by_kid(kid)
        except InvalidKeyIdError:
            self._key_set = self._fetch_jwks()
            try:
                return self._key_set.get_by_kid(kid)
            except InvalidKeyIdError as exc:
                raise OidcTokenError(f"JWKS 未知 kid={kid!r}（已尝试轮换刷新）") from exc

    def _oauth_client(self) -> OAuth2Client:
        kwargs: dict[str, object] = {}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return OAuth2Client(
            self._settings.client_id,
            self._settings.client_secret,
            redirect_uri=self._settings.redirect_uri,
            scope="openid",
            code_challenge_method="S256",
            **kwargs,
        )

    # ------------------------------------------------------------------
    # 登录流程
    # ------------------------------------------------------------------

    def _sweep_pending(self) -> None:
        now = self._now()
        for state in [s for s, p in self._pending.items() if p.expires_at <= now]:
            del self._pending[state]

    def begin_login(self) -> LoginStart:
        """发起登录：生成 state/nonce/PKCE 并返回授权 URL（pending 存服务端内存）。"""
        self._sweep_pending()
        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        nonce = secrets.token_urlsafe(32)
        self._pending[state] = _PendingLogin(
            nonce=nonce,
            code_verifier=code_verifier,
            expires_at=self._now() + PENDING_TTL_SECONDS,
        )
        client = self._oauth_client()
        authorize_url, _ = client.create_authorization_url(
            self._endpoint("authorization_endpoint"),
            state=state,
            code_verifier=code_verifier,
            nonce=nonce,
        )
        return LoginStart(authorize_url=authorize_url, state=state)

    def complete_login(self, *, code: str, state: str) -> Principal:
        """回调完成：state 一次性校验 → 换 token → 验签 id_token → Principal。

        Raises
        ------
        OidcStateError : state 未知/已使用/过期
        OidcTokenError : token 交换失败或 id_token 任一校验失败
        """
        pending = self._pending.pop(state, None)
        if pending is None:
            raise OidcStateError("state 未知或已被使用")
        if pending.expires_at <= self._now():
            raise OidcStateError("登录请求已过期，请重新发起")
        client = self._oauth_client()
        try:
            token = client.fetch_token(
                self._endpoint("token_endpoint"),
                grant_type="authorization_code",
                code=code,
                code_verifier=pending.code_verifier,
            )
        except Exception as exc:  # authlib/httpx2 异常族：统一转协议层错误
            raise OidcTokenError("token 交换失败") from exc
        id_token = token.get("id_token") if isinstance(token, dict) else None
        if not isinstance(id_token, str) or not id_token:
            raise OidcTokenError("token 响应缺少 id_token")
        claims = self._verify_id_token(id_token, nonce=pending.nonce)
        return principal_for_oidc(
            claims,
            issuer=self._settings.issuer,
            grants=self._grants,
        )

    def _verify_id_token(self, id_token: str, *, nonce: str) -> dict[str, object]:
        """固定 RS256 验签 + claims 校验（issuer/audience/exp/sub/nonce）。"""
        header = _peek_header(id_token)
        if header.get("alg") not in _JOSE_ALGORITHMS:
            raise OidcTokenError(f"id_token 算法不受信任：{header.get('alg')!r}（仅固定 RS256）")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise OidcTokenError("id_token 缺少 kid，无法定位验签公钥")
        key = self._key_for_kid(kid)
        try:
            token = joserfc_jwt.decode(id_token, key, algorithms=list(_JOSE_ALGORITHMS))
        except JoseError as exc:
            raise OidcTokenError(f"id_token 验签失败：{type(exc).__name__}") from exc
        claims = token.claims
        registry = JWTClaimsRegistry(
            now=self._now(),
            exp={"essential": True},
            iss={"essential": True, "value": self._settings.issuer},
            aud={"essential": True, "value": self._settings.audience},
            sub={"essential": True},
        )
        try:
            registry.validate(claims)
        except JoseError as exc:
            raise OidcTokenError(f"id_token claims 校验失败：{type(exc).__name__}") from exc
        token_nonce = claims.get("nonce")
        if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, nonce):
            raise OidcTokenError("id_token nonce 与登录请求不符")
        return claims

    # ------------------------------------------------------------------
    # 会话（服务端内存；重启即全部失效）
    # ------------------------------------------------------------------

    def create_session(self, principal: Principal) -> Session:
        now = self._now()
        session = Session(
            session_id=secrets.token_urlsafe(32),
            principal=principal,
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            expires_at=now + SESSION_TTL_SECONDS,
        )
        self._sessions[session.session_id] = session
        return session

    def get_session(self, session_id: str) -> Session | None:
        """按 cookie 值取会话；过期即驱逐并返回 None（要求重新登录）。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.expires_at <= self._now():
            del self._sessions[session.session_id]
            return None
        return session

    def revoke_session(self, session_id: str) -> bool:
        """退出：撤销会话（返回是否确有会话被撤销，幂等）。"""
        return self._sessions.pop(session_id, None) is not None


# ---------------------------------------------------------------------------
# 请求级防护（服务层装配调用；纯函数便于单测）
# ---------------------------------------------------------------------------


def reject_dual_credentials(*, cookie_session_id: str | None, bearer_token: str | None) -> None:
    """Cookie 与 Bearer 不得同时出现（D02 ④）；不混用身份来源。"""
    if cookie_session_id and bearer_token:
        raise OidcSessionError("身份不得同时来自 Cookie 与 Bearer")


def verify_csrf(session: Session, csrf_token: str | None) -> None:
    """Cookie 写请求的 CSRF 校验：与会话绑定、常量时间比较；缺失/错误拒绝。"""
    if not csrf_token or not hmac.compare_digest(csrf_token, session.csrf_token):
        raise OidcSessionError("CSRF token 缺失或不匹配")


def verify_origin(origin: str | None, *, expected_origin: str) -> None:
    """Cookie 写请求的同源校验；缺失 Origin 或跨域一律拒绝（fail-closed）。"""
    if not origin:
        raise OidcSessionError("缺少 Origin：Cookie 写请求要求同源")
    if not hmac.compare_digest(origin, expected_origin):
        raise OidcSessionError("Origin 与部署源不符")


def set_session_cookie(response: Response, session: Session) -> None:
    """把会话写入严格属性 Cookie：HttpOnly + Secure + SameSite=Lax，JS 不可读。"""
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session.session_id,
        max_age=session.expires_at - session.created_at,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """清理会话 Cookie（退出/失效时调用）。"""
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
