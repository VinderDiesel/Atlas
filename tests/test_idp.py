"""外部 IdP（RS256 / JWKS）验签钩子测试（serving/auth.py）。

自包含：用 cryptography 生成 RSA 密钥对，手工构造 RS256 JWT（不依赖 PyJWT），
再用 verify_token_external 验签。覆盖：签名有效 / 签名被篡改 / alg 非 RS256 /
过期 / audience·issuer 校验。JWKS 解析以内存 dict 模拟（不触网）。
"""

from __future__ import annotations

import base64
import json
import time
import unittest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from serving.auth import AuthError, verify_token_external


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _int_to_b64url(n: int) -> str:
    length = (n.bit_length() + 7) // 8
    return _b64url(n.to_bytes(length, "big"))


def make_rs256_token(private_key: rsa.RSAPrivateKey, payload: dict, kid: str = "k1") -> str:
    header = {"alg": "RS256", "kid": kid, "typ": "JWT"}
    body = json.dumps(payload, sort_keys=True).encode()
    signing_input = (
        f"{_b64url(json.dumps(header, sort_keys=True).encode())}.{_b64url(body)}".encode()
    )
    sig = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode()}.{_b64url(sig)}"


def jwk_of(public_key: rsa.RSAPublicKey, kid: str = "k1") -> dict:
    nums = public_key.public_numbers()
    return {"kty": "RSA", "kid": kid, "n": _int_to_b64url(nums.n), "e": _int_to_b64url(nums.e)}


class TestExternalIdP(unittest.TestCase):
    def setUp(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.jwk = jwk_of(self.key.public_key())
        now = int(time.time())
        self.payload = {
            "sub": "u1",
            "role": "hq_admin",
            "iat": now - 10,
            "exp": now + 600,
            "aud": "atlas",
            "iss": "https://idp.example.com",
            "user_context": {"branch": "east"},
        }

    def test_valid_token_verifies(self) -> None:
        token = make_rs256_token(self.key, self.payload)
        claims = verify_token_external(
            token, self.jwk, audience="atlas", issuer="https://idp.example.com"
        )
        self.assertEqual(claims["role"], "hq_admin")

    def test_tampered_signature_rejected(self) -> None:
        token = make_rs256_token(self.key, self.payload)
        bad = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        with self.assertRaises(AuthError):
            verify_token_external(bad, self.jwk)

    def test_wrong_key_rejected(self) -> None:
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = make_rs256_token(other, self.payload)
        with self.assertRaises(AuthError):
            verify_token_external(token, self.jwk)

    def test_non_rs256_rejected(self) -> None:
        token = make_rs256_token(self.key, self.payload)
        # 把 header 的 alg 改成 HS256（签名不变）→ 验签应在算法检查处拒绝
        parts = token.split(".")
        fake_header = _b64url(json.dumps({"alg": "HS256", "kid": "k1", "typ": "JWT"}).encode())
        bad = f"{fake_header}.{parts[1]}.{parts[2]}"
        with self.assertRaises(AuthError):
            verify_token_external(bad, self.jwk)

    def test_expired_rejected(self) -> None:
        expired = {**self.payload, "exp": int(time.time()) - 10}
        token = make_rs256_token(self.key, expired)
        with self.assertRaises(AuthError):
            verify_token_external(token, self.jwk)

    def test_audience_mismatch_rejected(self) -> None:
        token = make_rs256_token(self.key, self.payload)
        with self.assertRaises(AuthError):
            verify_token_external(
                token, self.jwk, audience="other", issuer="https://idp.example.com"
            )

    def test_unregistered_role_rejected(self) -> None:
        bad = {**self.payload, "role": "superuser"}
        token = make_rs256_token(self.key, bad)
        with self.assertRaises(AuthError):
            verify_token_external(
                token, self.jwk, audience="atlas", issuer="https://idp.example.com"
            )


if __name__ == "__main__":
    unittest.main()
