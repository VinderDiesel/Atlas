"""serving/auth.py 契约测试：JWT 签发/校验与 6 角色 × 2 域行级策略解析（ADR-0021）。

纯本地单测（不连 Doris / Polaris）：断言集中在签名安全、角色 claims 契约、
row_policy.yml 渲染结果与「域 → 策略」二维事实源（决策 ①~⑤ 的契约锁）。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from agent.compiler import SemanticModel
from serving.auth import (
    ROLE_DIRECTORY,
    AuthError,
    ResolvedPolicy,
    RoleSpec,
    resolve_claims,
    resolve_policy,
    roles_for_policy,
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
    """金融域条件渲染（对照 row_policy.yml 原文，防模板漂移）。

    policy_name 由调用方按域传入（ADR-0021 决策 ③ 破坏性签名；默认金融域）。
    """

    def _resolve(
        self, role: str, claims: dict, policy_name: str = "rp_branch_visible"
    ) -> ResolvedPolicy:
        token = sign_token(role, claims, secret=SECRET)
        return resolve_policy(
            token, secret=SECRET, policy_path=POLICY_PATH, policy_name=policy_name
        )

    def test_hq_admin_full_visibility(self) -> None:
        policy = self._resolve("hq_admin", {})
        self.assertEqual(policy.policy_name, "rp_branch_visible")
        self.assertEqual(policy.condition, "1=1")

    def test_hq_admin_policy_name_follows_domain(self) -> None:
        """判据 3：hq_admin 策略名按域正确（锁死后果 1 回归）——condition 均 1=1。"""
        finance = self._resolve("hq_admin", {}, policy_name="rp_branch_visible")
        retail = self._resolve("hq_admin", {}, policy_name="rp_dept_visible")
        self.assertEqual(finance.policy_name, "rp_branch_visible")
        self.assertEqual(retail.policy_name, "rp_dept_visible")
        self.assertEqual(finance.condition, "1=1")
        self.assertEqual(retail.condition, "1=1")

    def test_branch_manager_scope(self) -> None:
        policy = self._resolve("branch_manager", {"branch": "BR_A1"})
        self.assertIn("dim_broker.branch = 'BR_A1'", policy.condition)

    def test_broker_brokerid_int_rendering(self) -> None:
        """判据 4：broker 条件 int 渲染无引号（模板不带引号，与 max_tier 同形）。"""
        policy = self._resolve("broker", {"brokerid": 7285})
        self.assertEqual(policy.policy_name, "rp_branch_visible")
        self.assertEqual(policy.condition, "dim_broker.brokerid = 7285")

    def test_compliance_auditor_max_tier(self) -> None:
        policy = self._resolve("compliance_auditor", {"max_tier": 3})
        self.assertIn("dim_customer.tier <= 3", policy.condition)

    def test_missing_claim_placeholder_rejected(self) -> None:
        """解析侧兜底：claims 缺 branch 时渲染拒绝（签发期已拒，见契约类）。

        此处直调 resolve_claims 锁「解析层独立兜底」——签发期校验（决策 ④）
        之外，解析层仍须拒绝未渲染占位符（纵深防御）。
        """
        with self.assertRaises(AuthError):
            resolve_claims(
                {"role": "branch_manager", "user_context": {}},
                policy_name="rp_branch_visible",
                policy_path=POLICY_PATH,
            )

    def test_illegal_literal_rejected(self) -> None:
        with self.assertRaises(AuthError):
            self._resolve("branch_manager", {"branch": "a'; DROP TABLE t"})

    def test_roles_bidirectional_consistency(self) -> None:
        """双向一致性（ADR-0021 决策 ⑥，与 make lint 同口径）。

        方向 1：ROLE_DIRECTORY 每角色 ∈ 被引用策略的 roles；
        方向 2：被引用策略的每角色 ∈ ROLE_DIRECTORY（防 broker 类潜伏复发）。
        """
        import yaml

        doc = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
        referenced = {
            SemanticModel(
                REPO_ROOT / "semantic" / "ossie" / "atlas_finance.ossie.yaml"
            ).default_row_policy,
            SemanticModel(
                REPO_ROOT / "semantic" / "ossie" / "atlas_retail.ossie.yaml"
            ).default_row_policy,
        }
        yaml_roles = {
            r["name"] for p in doc["policies"] if p["name"] in referenced for r in p["roles"]
        }
        for role in ROLE_DIRECTORY:
            with self.subTest(role=role, direction="registered→yaml"):
                self.assertIn(role, yaml_roles)
        for role in sorted(yaml_roles):
            with self.subTest(role=role, direction="yaml→registered"):
                self.assertIn(role, ROLE_DIRECTORY)


class TestRetailPolicyRender(unittest.TestCase):
    """零售档 rp_dept_visible 物理列对齐（P5，2026-09-04）：逻辑列 region /
    product_category → dim_store.s_state / dim_item.i_category；categories 列表
    经 sql_in 过滤器渲染（非标量值不进标量替换路径）。
    """

    def _resolve(self, role: str, claims: dict) -> ResolvedPolicy:
        token = sign_token(role, claims, secret=SECRET)
        return resolve_policy(
            token,
            secret=SECRET,
            policy_path=POLICY_PATH,
            policy_name="rp_dept_visible",
        )

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
            self._resolve("category_analyst", {"region": "TN", "categories": ["A;DROP"]})

    def test_sql_in_scalar_value_rejected(self) -> None:
        # categories 标量（非列表）不满足 sql_in 渲染要求 → 拒绝，防注入
        with self.assertRaises(AuthError):
            self._resolve("category_analyst", {"region": "TN", "categories": "Shoes"})

    def test_retail_roles_rejected_in_branch_policy(self) -> None:
        """跨域拒绝（ADR-0021 决策 ③，判据 2）：零售角色配金融策略 → AuthError。"""
        for role, claims in (
            ("region_manager", {"region": "TN"}),
            ("category_analyst", {"region": "TN", "categories": ["Shoes"]}),
        ):
            with self.subTest(role=role):
                token = sign_token(role, claims, secret=SECRET)
                with self.assertRaises(AuthError) as ctx:
                    resolve_policy(
                        token,
                        secret=SECRET,
                        policy_path=POLICY_PATH,
                        policy_name="rp_branch_visible",
                    )
                self.assertIn("域不匹配", str(ctx.exception))


class TestResolveClaims(unittest.TestCase):
    """resolve_claims 纯函数（C1 拆分回归锁）：与 resolve_policy(token) 等价。

    拆分把 resolve_policy 中 verify_token 之后的部分抽为 resolve_claims（claims
    版纯函数，供 DataAgent.ask(identity=…) 复用）；同 token 同 claims 必须产出
    同 ResolvedPolicy（frozen dataclass 全字段相等）。
    """

    def test_equivalent_to_resolve_policy(self) -> None:
        """全部 6 角色 × 所属域：resolve_claims(verify_token(t)) == resolve_policy(t)。"""
        cases: list[tuple[str, dict, str]] = [
            ("hq_admin", {}, "rp_branch_visible"),
            ("hq_admin", {}, "rp_dept_visible"),
            ("branch_manager", {"branch": "BR_A1"}, "rp_branch_visible"),
            ("broker", {"brokerid": 7285}, "rp_branch_visible"),
            ("compliance_auditor", {"max_tier": 3}, "rp_branch_visible"),
            ("region_manager", {"region": "TN"}, "rp_dept_visible"),
            (
                "category_analyst",
                {"region": "TN", "categories": ["Shoes"]},
                "rp_dept_visible",
            ),
        ]
        for role, user_context, policy_name in cases:
            with self.subTest(role=role, policy=policy_name):
                token = sign_token(role, user_context, secret=SECRET)
                claims = verify_token(token, secret=SECRET)
                self.assertEqual(
                    resolve_claims(claims, policy_name=policy_name, policy_path=POLICY_PATH),
                    resolve_policy(
                        token,
                        secret=SECRET,
                        policy_path=POLICY_PATH,
                        policy_name=policy_name,
                    ),
                )

    def test_claims_reject_unregistered_role(self) -> None:
        """claims role 未注册 → AuthError（防 KeyError，与 verify_token 同拒绝语义）。"""
        with self.assertRaises(AuthError):
            resolve_claims(
                {"role": "ceo_omniscient", "user_context": {}},
                policy_name="rp_branch_visible",
            )

    def test_claims_reject_missing_user_context(self) -> None:
        with self.assertRaises(AuthError):
            resolve_claims({"role": "hq_admin"}, policy_name="rp_branch_visible")

    def test_claims_reject_illegal_literal(self) -> None:
        """直调纯函数同样拒绝非法字面量（渲染安全校验不依赖 token 路径）。"""
        with self.assertRaises(AuthError):
            resolve_claims(
                {"role": "branch_manager", "user_context": {"branch": "a'; DROP"}},
                policy_name="rp_branch_visible",
            )

    def test_claims_reject_domain_mismatch(self) -> None:
        """域不匹配显式拒绝（ADR-0021 决策 ③，判据 2 的 claims 版）。"""
        with self.assertRaises(AuthError) as ctx:
            resolve_claims(
                {"role": "region_manager", "user_context": {"region": "TN"}},
                policy_name="rp_branch_visible",
                policy_path=POLICY_PATH,
            )
        self.assertIn("域不匹配", str(ctx.exception))

    def test_claims_reject_unknown_policy(self) -> None:
        """未声明/拼错的策略名拒绝（策略不存在，ADR-0021 决策 ③）。"""
        with self.assertRaises(AuthError):
            resolve_claims(
                {"role": "hq_admin", "user_context": {}},
                policy_name="rp_nope",
                policy_path=POLICY_PATH,
            )


class TestRoleSpecContract(unittest.TestCase):
    """ROLE_DIRECTORY claims 契约（ADR-0021 决策 ④⑤）与签发期校验（判据 4/5）。"""

    def test_directory_six_roles_registered(self) -> None:
        """收口判据：6 键注册表（broker 在列）；值类型为 RoleSpec。"""
        self.assertEqual(
            set(ROLE_DIRECTORY),
            {
                "hq_admin",
                "branch_manager",
                "broker",
                "compliance_auditor",
                "region_manager",
                "category_analyst",
            },
        )
        for role, spec in ROLE_DIRECTORY.items():
            with self.subTest(role=role):
                self.assertIsInstance(spec, RoleSpec)

    def test_sign_rejects_missing_required_claim(self) -> None:
        """判据 5：缺必需键在**签发期**拒绝并列出缺失键（改造前签发成功）。"""
        with self.assertRaises(AuthError) as ctx:
            sign_token("branch_manager", {}, secret=SECRET)
        self.assertIn("缺少必需 claims 键", str(ctx.exception))
        self.assertIn("branch", str(ctx.exception))

    def test_sign_rejects_empty_list_claim(self) -> None:
        """判据 5 续：空列表在签发期拒绝（sql_in 空集语义陷阱前置拦截）。"""
        with self.assertRaises(AuthError):
            sign_token("category_analyst", {"region": "TN", "categories": []}, secret=SECRET)

    def test_sign_rejects_scalar_list_claim(self) -> None:
        with self.assertRaises(AuthError):
            sign_token(
                "category_analyst",
                {"region": "TN", "categories": "Shoes"},
                secret=SECRET,
            )

    def test_sign_accepts_category_analyst_with_list(self) -> None:
        token = sign_token(
            "category_analyst", {"region": "TN", "categories": ["Shoes"]}, secret=SECRET
        )
        self.assertEqual(verify_token(token, secret=SECRET)["role"], "category_analyst")


class TestRolesForPolicy(unittest.TestCase):
    """按域暴露已注册角色（ADR-0021 决策 ⑤，判据 6；YAML 声明顺序）。"""

    def test_dept_policy_three_roles(self) -> None:
        self.assertEqual(
            roles_for_policy("rp_dept_visible", policy_path=POLICY_PATH),
            ("hq_admin", "region_manager", "category_analyst"),
        )

    def test_branch_policy_four_roles_with_broker(self) -> None:
        self.assertEqual(
            roles_for_policy("rp_branch_visible", policy_path=POLICY_PATH),
            ("hq_admin", "branch_manager", "broker", "compliance_auditor"),
        )

    def test_unknown_policy_rejected(self) -> None:
        with self.assertRaises(AuthError):
            roles_for_policy("rp_nope", policy_path=POLICY_PATH)


if __name__ == "__main__":
    unittest.main()
