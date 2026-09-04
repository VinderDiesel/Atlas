"""serving/auth.py 契约测试：JWT 签发/校验与三角色 → 行级策略解析。

纯本地单测（不连 Doris / Polaris）：断言集中在签名安全、角色注册与
row_policy.yml 渲染结果。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from serving.auth import (
    ROLE_DIRECTORY,
    AuthError,
    ResolvedPolicy,
    resolve_claims,
    resolve_policy,
    sign_token,
    verify_token,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = REPO_ROOT / "semantic" / "policies" / "row_policy.yml"
SECRET = "test-secret"


class TestJwtRoundtrip(unittest.TestCase):
    def test_sign_verify_roundtrip(self) -> None:
        token = sign_token("hq_admin", {}, secret=SECRET, subject="u1")
        claims = verify_token(token, secret=SECRET)
        self.assertEqual(claims["role"], "hq_admin")
        self.assertEqual(claims["sub"], "u1")
        self.assertEqual(claims["user_context"], {})

    def test_user_context_roundtrip(self) -> None:
        token = sign_token("branch_manager", {"branch": "EAST01"}, secret=SECRET)
        claims = verify_token(token, secret=SECRET)
        self.assertEqual(claims["user_context"], {"branch": "EAST01"})

    def test_tampered_payload_rejected(self) -> None:
        token = sign_token("hq_admin", {}, secret=SECRET)
        head, _, sig = token.split(".")
        # 改 role 后原签名必然失配
        forged_body = "eyJzdWIiOiJkZW1vLXVzZXIiLCJyb2xlIjoiY29tcGxpYW5jZV9hdWRpdG9yIn0"
        with self.assertRaises(AuthError):
            verify_token(f"{head}.{forged_body}.{sig}", secret=SECRET)

    def test_wrong_secret_rejected(self) -> None:
        token = sign_token("hq_admin", {}, secret=SECRET)
        with self.assertRaises(AuthError):
            verify_token(token, secret="other-secret")

    def test_expired_token_rejected(self) -> None:
        token = sign_token("hq_admin", {}, secret=SECRET, ttl=-10)
        with self.assertRaises(AuthError):
            verify_token(token, secret=SECRET)

    def test_unregistered_role_rejected(self) -> None:
        with self.assertRaises(AuthError):
            sign_token("ceo_omniscient", {}, secret=SECRET)


class TestResolvePolicy(unittest.TestCase):
    """三角色条件渲染（对照 row_policy.yml 原文，防模板漂移）。"""

    def _resolve(self, role: str, claims: dict) -> ResolvedPolicy:
        token = sign_token(role, claims, secret=SECRET)
        return resolve_policy(token, secret=SECRET, policy_path=POLICY_PATH)

    def test_hq_admin_full_visibility(self) -> None:
        policy = self._resolve("hq_admin", {})
        self.assertEqual(policy.policy_name, "rp_branch_visible")
        self.assertEqual(policy.condition, "1=1")

    def test_branch_manager_scope(self) -> None:
        policy = self._resolve("branch_manager", {"branch": "BR_A1"})
        self.assertIn("dim_broker.branch = 'BR_A1'", policy.condition)

    def test_compliance_auditor_max_tier(self) -> None:
        policy = self._resolve("compliance_auditor", {"max_tier": 3})
        self.assertIn("dim_customer.tier <= 3", policy.condition)

    def test_missing_claim_placeholder_rejected(self) -> None:
        token = sign_token("branch_manager", {}, secret=SECRET)
        with self.assertRaises(AuthError):
            resolve_policy(token, secret=SECRET, policy_path=POLICY_PATH)

    def test_illegal_literal_rejected(self) -> None:
        token = sign_token("branch_manager", {"branch": "a'; DROP TABLE t"}, secret=SECRET)
        with self.assertRaises(AuthError):
            resolve_policy(token, secret=SECRET, policy_path=POLICY_PATH)

    def test_all_roles_registered_have_yaml_role(self) -> None:
        """ROLE_DIRECTORY 每个角色都能在 row_policy.yml 里找到同名 role。"""
        import yaml

        doc = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
        yaml_roles = {r["name"] for p in doc["policies"] for r in p["roles"]}
        for role, (policy_name, role_name, _) in ROLE_DIRECTORY.items():
            with self.subTest(role=role):
                self.assertIn(role_name, yaml_roles, f"{role} -> {policy_name}.{role_name}")
                if role in ("region_manager", "category_analyst"):
                    self.assertEqual(policy_name, "rp_dept_visible")


class TestRetailPolicyRender(unittest.TestCase):
    """零售档 rp_dept_visible 物理列对齐（P5，2026-09-04）：逻辑列 region /
    product_category → dim_store.s_state / dim_item.i_category；categories 列表
    经 sql_in 过滤器渲染（非标量值不进标量替换路径）。
    """

    def _resolve(self, role: str, claims: dict) -> ResolvedPolicy:
        token = sign_token(role, claims, secret=SECRET)
        return resolve_policy(token, secret=SECRET, policy_path=POLICY_PATH)

    def test_region_manager_physical_column(self) -> None:
        policy = self._resolve("region_manager", {"region": "TN"})
        self.assertEqual(policy.policy_name, "rp_dept_visible")
        self.assertIn("dim_store.s_state = 'TN'", policy.condition)

    def test_category_analyst_sql_in(self) -> None:
        policy = self._resolve(
            "category_analyst",
            {"region": "TN", "categories": ["Shoes", "Electronics"]},
        )
        self.assertEqual(policy.policy_name, "rp_dept_visible")
        self.assertIn("dim_store.s_state = 'TN'", policy.condition)
        self.assertIn("dim_item.i_category IN ('Shoes', 'Electronics')", policy.condition)

    def test_sql_in_empty_list_rejected(self) -> None:
        with self.assertRaises(AuthError):
            self._resolve("category_analyst", {"region": "TN", "categories": []})

    def test_sql_in_illegal_item_rejected(self) -> None:
        with self.assertRaises(AuthError):
            self._resolve(
                "category_analyst", {"region": "TN", "categories": ["A;DROP"]}
            )

    def test_sql_in_scalar_value_rejected(self) -> None:
        # categories 标量（非列表）不满足 sql_in 渲染要求 → 拒绝，防注入
        with self.assertRaises(AuthError):
            self._resolve("category_analyst", {"region": "TN", "categories": "Shoes"})

    def test_retail_roles_not_in_branch_policy(self) -> None:
        """零售角色必须绑定 rp_dept_visible（防跨策略误绑）。"""
        for role in ("region_manager", "category_analyst"):
            with self.subTest(role=role):
                policy_name = ROLE_DIRECTORY[role][0]
                self.assertEqual(policy_name, "rp_dept_visible")


class TestResolveClaims(unittest.TestCase):
    """resolve_claims 纯函数（C1 拆分回归锁）：与 resolve_policy(token) 等价。

    拆分把 resolve_policy 中 verify_token 之后的部分抽为 resolve_claims（claims
    版纯函数，供 DataAgent.ask(identity=…) 复用）；同 token 同 claims 必须产出
    同 ResolvedPolicy（frozen dataclass 全字段相等）。
    """

    def test_equivalent_to_resolve_policy(self) -> None:
        """全部注册角色：resolve_claims(verify_token(t)) == resolve_policy(t)。"""
        cases: list[tuple[str, dict]] = [
            ("hq_admin", {}),
            ("branch_manager", {"branch": "BR_A1"}),
            ("compliance_auditor", {"max_tier": 3}),
            ("region_manager", {"region": "TN"}),
            ("category_analyst", {"region": "TN", "categories": ["Shoes"]}),
        ]
        for role, user_context in cases:
            with self.subTest(role=role):
                token = sign_token(role, user_context, secret=SECRET)
                claims = verify_token(token, secret=SECRET)
                self.assertEqual(
                    resolve_claims(claims, policy_path=POLICY_PATH),
                    resolve_policy(token, secret=SECRET, policy_path=POLICY_PATH),
                )

    def test_claims_reject_unregistered_role(self) -> None:
        """claims role 未注册 → AuthError（防 KeyError，与 verify_token 同拒绝语义）。"""
        with self.assertRaises(AuthError):
            resolve_claims({"role": "ceo_omniscient", "user_context": {}})

    def test_claims_reject_missing_user_context(self) -> None:
        with self.assertRaises(AuthError):
            resolve_claims({"role": "hq_admin"})

    def test_claims_reject_illegal_literal(self) -> None:
        """直调纯函数同样拒绝非法字面量（渲染安全校验不依赖 token 路径）。"""
        with self.assertRaises(AuthError):
            resolve_claims(
                {"role": "branch_manager", "user_context": {"branch": "a'; DROP"}}
            )


if __name__ == "__main__":
    unittest.main()
