"""T03 OIDC 共享测试设施：受控 IdP（httpx2.MockTransport）+ 可控时钟 + 栈装配。

外部 IdP 全部走 MockTransport 受控 HTTP——不触网、不付费、不使用真实 token；
id_token 用 joserfc 真实 RS256 签名，被验证的是本仓的验签与流程逻辑而非桩。

使用方：tests/test_oidc_login.py（BFF 内核）与 tests/test_auth_endpoints.py
（HTTP 装配）。本模块只含测试设施，生产代码零依赖。
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx2
from cryptography.hazmat.primitives.asymmetric import rsa
from joserfc import jwt as jose_jwt
from joserfc.jwk import OctKey, RSAKey

ISSUER = "https://idp.example.com"
CLIENT_ID = "atlas-client"
CLIENT_SECRET = "s3cret"
REDIRECT_URI = "http://localhost:8000/api/v1/auth/callback"
AUDIENCE = "atlas-client"

FULL_ENV = {
    "ATLAS_OIDC_ISSUER": ISSUER,
    "ATLAS_OIDC_CLIENT_ID": CLIENT_ID,
    "ATLAS_OIDC_CLIENT_SECRET": CLIENT_SECRET,
    "ATLAS_OIDC_REDIRECT_URI": REDIRECT_URI,
    "ATLAS_OIDC_AUDIENCE": AUDIENCE,
}

# ATLAS_OIDC_* 全量变量名（演示模式/部分配置测试共用；与 serving/control/oidc.py 对齐）
OIDC_ENV_NAMES = tuple(FULL_ENV)

_PRIVATE_KEY: rsa.RSAPrivateKey | None = None


def _rsa_private() -> rsa.RSAPrivateKey:
    """模块级共享测试私钥：生成一次（2048 位，约 100ms），避免每测重新生成。"""
    global _PRIVATE_KEY
    if _PRIVATE_KEY is None:
        _PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _PRIVATE_KEY


class FakeClock:
    """可控时钟：会话/pending 过期与 exp 校验的确定性时间来源。"""

    def __init__(self, start: int = 1_800_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += seconds


class FakeIdp:
    """受控 IdP：discovery / token / jwks 三端点，用真实 RSA 私钥签 id_token。"""

    def __init__(
        self,
        clock: FakeClock,
        *,
        kid: str = "kid-1",
        discovery_issuer: str | None = None,
    ) -> None:
        self._clock = clock
        self.kid = kid
        self.discovery_issuer = discovery_issuer or ISSUER
        self.jwks_versions: list[dict[str, object]] = [self._jwks(kid)]
        self.claims_overrides: dict[str, object] = {}
        self.alg = "RS256"
        self.omit_id_token = False
        self.token_status = 200
        self.token_calls = 0
        self.discovery_calls = 0
        self.jwks_calls = 0

    def _jwks(self, kid: str) -> dict[str, object]:
        pub = RSAKey.import_key(_rsa_private().public_key(), {"kid": kid})
        return {"keys": [pub.as_dict()]}

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            self.discovery_calls += 1
            return httpx2.Response(
                200,
                json={
                    "issuer": self.discovery_issuer,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
                },
            )
        if path == "/token":
            self.token_calls += 1
            body: dict[str, object] = {"access_token": "at-1", "token_type": "Bearer"}
            if not self.omit_id_token:
                body["id_token"] = self.mint()
            return httpx2.Response(self.token_status, json=body)
        if path == "/.well-known/jwks.json":
            self.jwks_calls += 1
            index = min(self.jwks_calls - 1, len(self.jwks_versions) - 1)
            return httpx2.Response(200, json=self.jwks_versions[index])
        return httpx2.Response(404)

    def mint(self, **overrides: object) -> str:
        """签发 id_token：默认 claims + claims_overrides + 本次覆盖；alg/kid 可被测试篡改。"""
        now = self._clock()
        claims: dict[str, object] = {
            "sub": "user-1",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + 300,
            "nonce": None,
        }
        claims.update(self.claims_overrides)
        claims.update(overrides)
        header = {"alg": self.alg, "kid": self.kid}
        if self.alg == "RS256":
            key: object = RSAKey.import_key(_rsa_private(), {"kid": self.kid})
        else:
            key = OctKey.import_key("test-hmac-secret")
        return jose_jwt.encode(header, claims, key, algorithms=[self.alg])


def stack(*, grants=None, discovery_issuer=None, kid="kid-1"):
    """一个完整受控栈：共享时钟 + FakeIdp + OidcBff（配置来自 FULL_ENV）。"""
    from serving.control.oidc import OidcBff, OidcSettings

    clock = FakeClock()
    idp = FakeIdp(clock, kid=kid, discovery_issuer=discovery_issuer)
    bff = OidcBff(
        OidcSettings.from_env(FULL_ENV),
        transport=idp.transport(),
        now=clock,
        grants=grants,
    )
    return clock, idp, bff


def nonce_of(authorize_url: str) -> str:
    """从授权 URL 提取 nonce（签 id_token 用）。"""
    return parse_qs(urlparse(authorize_url).query)["nonce"][0]


def login(bff, idp, *, sub: str = "user-1", **claim_overrides):
    """完整登录：begin → 用授权 URL 的 nonce 签 id_token → complete → Principal。"""
    start = bff.begin_login()
    idp.claims_overrides = {"sub": sub, "nonce": nonce_of(start.authorize_url), **claim_overrides}
    return bff.complete_login(code="code-1", state=start.state)
