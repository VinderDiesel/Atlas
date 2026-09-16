"""治理扩展 supersedes 链语义契约测试（Day 27 指标版本机制）。

覆盖 check_supersedes_chains 的全部规则：取代目标存在、非自身、
版本号严格递增（天然防环）、被取代者不得仍为 active、治理记录不得重名。
另覆盖 check_snapshot_anchor（expected_value_snapshot_sha 数值背书快照
锚定校验，KL #16 收口）：已回填值必须指向已锁定快照，占位是机制未启用
状态的如实声明。
另覆盖 check_policy_consistency（ADR-0021 决策 ⑥：域 ↔ 策略 ↔ 角色 ↔
claims 契约双向一致性）：正例吃真实 row_policy.yml + ROLE_DIRECTORY +
两个 ossie 模型声明（make lint 同口径的单元版），四条检查各有单点反例。

口径说明：指标演进一律采用**新名 + supersedes 指向旧名**（同名指标在
ossie_validate 已被全局唯一约束拦截，不存在同名两代共存的合法形态）。
supersedes / snapshot 部分为纯函数级测试，不依赖语义层文件与文件系统；
policy-consistency 正例直接读真实语义层文件以锁全绿。

这些用例必须全部通过才能改 governance_validate.py。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from semantic.governance_validate import (
    PLACEHOLDER_SNAPSHOT_SHA,
    MetricGovernance,
    check_policy_consistency,
    check_snapshot_anchor,
    check_supersedes_chains,
    collect_referenced_policies,
    load_policies_by_name,
)
from serving.auth import ROLE_DIRECTORY, RoleSpec

REPO = Path(__file__).resolve().parent.parent
POLICY_PATH = REPO / "semantic" / "policies" / "row_policy.yml"
MODEL_YAMLS = [
    REPO / "semantic" / "ossie" / "atlas_finance.ossie.yaml",
    REPO / "semantic" / "ossie" / "atlas_retail.ossie.yaml",
]


def _rec(
    name: str,
    version: int = 1,
    status: str = "active",
    supersedes: str | None = None,
    file_tag: str = "x.yaml",
) -> MetricGovernance:
    return MetricGovernance(
        metric_name=name,
        version=version,
        status=status,
        supersedes=supersedes,
        prefix=f"{file_tag}.<model>.metric.{name}",
    )


def _run(records: list[MetricGovernance]) -> list[str]:
    errors: list[str] = []
    check_supersedes_chains(records, errors)
    return errors


class TestSupersedesChainValid(unittest.TestCase):
    """合法场景：无取代记录、两代取代、多级链。"""

    def test_plain_metrics_without_supersedes_pass(self) -> None:
        # 现状：20 个 v1 active 指标，互不取代（supersedes=null）
        records = [_rec(f"metric_{i}") for i in range(3)]
        self.assertEqual(_run(records), [])

    def test_new_name_supersedes_old_name_passes(self) -> None:
        records = [
            _rec("gmv_daily", version=2, status="active", supersedes="gmv_legacy"),
            _rec("gmv_legacy", version=1, status="deprecated"),
        ]
        self.assertEqual(_run(records), [])

    def test_multi_level_chain_passes(self) -> None:
        # 逐代取代：v3 → v2 → v1，每代 version 递增、被取代者已 deprecated
        records = [
            _rec("gmv_v3", version=3, status="active", supersedes="gmv_v2"),
            _rec("gmv_v2", version=2, status="deprecated", supersedes="gmv_v1"),
            _rec("gmv_v1", version=1, status="deprecated"),
        ]
        self.assertEqual(_run(records), [])


class TestSupersedesChainInvalid(unittest.TestCase):
    """非法场景：每条规则对应一个被拒绝的用例。"""

    def test_supersedes_unknown_target_rejected(self) -> None:
        errors = _run([_rec("gmv", version=2, supersedes="ghost_metric")])
        self.assertEqual(len(errors), 1)
        self.assertIn("指向不存在的指标 ghost_metric", errors[0])

    def test_supersedes_itself_rejected(self) -> None:
        # 指向自身时短路 continue，只报一条（版本检查对自身无意义）
        errors = _run([_rec("gmv", version=2, supersedes="gmv")])
        self.assertEqual(len(errors), 1)
        self.assertIn("不能指向自身", errors[0])

    def test_version_must_increase_strictly(self) -> None:
        records = [
            _rec("gmv_new", version=1, status="active", supersedes="gmv_old"),
            _rec("gmv_old", version=2, status="deprecated"),
        ]
        errors = _run(records)
        self.assertEqual(len(errors), 1)
        self.assertIn("必须严格大于被取代版本", errors[0])

    def test_superseded_metric_must_not_be_active(self) -> None:
        records = [
            _rec("gmv_new", version=2, status="active", supersedes="gmv_old"),
            _rec("gmv_old", version=1, status="active"),
        ]
        errors = _run(records)
        self.assertEqual(len(errors), 1)
        self.assertIn("仍为 active", errors[0])
        self.assertIn("deprecated", errors[0])

    def test_duplicate_governance_record_rejected(self) -> None:
        # 同名治理记录 = 跨文件同名指标定义重复（ossie_validate 同口径防呆）
        errors = _run(
            [
                _rec("gmv", file_tag="a.yaml"),
                _rec("gmv", file_tag="b.yaml"),
            ]
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("治理记录重名", errors[0])

    def test_cycle_blocked_by_version_rule(self) -> None:
        # v1 试图回指 v3：1 <= 3 不满足严格递增 → 报错（防环的显式证明）；
        # 目标 c_v3 仍为 active，同时命中"被取代者不得 active"
        records = [
            _rec("c_v3", version=3, status="active", supersedes="b_v2"),
            _rec("b_v2", version=2, status="deprecated", supersedes="a_v1"),
            _rec("a_v1", version=1, status="deprecated", supersedes="c_v3"),
        ]
        errors = _run(records)
        self.assertEqual(len(errors), 2)
        self.assertTrue(
            any("必须严格大于" in e for e in errors),
            msg=f"预期版本不递增错误，实际：{errors}",
        )
        self.assertTrue(
            any("仍为 active" in e for e in errors),
            msg=f"预期被取代者 active 错误，实际：{errors}",
        )

    def test_multi_error_reported_together(self) -> None:
        # 跨记录多错误聚合：目标不存在 + 版本不递增各报一条
        # （同一条记录上两者互斥：目标不存在时短路 continue，不查版本）
        records = [
            _rec("a_new", version=2, status="active", supersedes="ghost"),
            _rec("b_new", version=1, status="active", supersedes="b_old"),
            _rec("b_old", version=2, status="deprecated"),
        ]
        errors = _run(records)
        self.assertEqual(len(errors), 2)
        self.assertTrue(
            any("指向不存在的指标 ghost" in e for e in errors),
            msg=f"预期目标不存在错误，实际：{errors}",
        )
        self.assertTrue(
            any("必须严格大于" in e for e in errors),
            msg=f"预期版本不递增错误，实际：{errors}",
        )


class TestSnapshotAnchor(unittest.TestCase):
    """expected_value_snapshot_sha 锚定校验（KL #16 收口，2026-09-04）。"""

    def test_locked_sha_with_cases_passes(self) -> None:
        # 金融段现状：有用例 + 已锁定快照（如 30b8344）→ 通过
        errors = check_snapshot_anchor({"gold-156"}, "30b8344", {"30b8344", "a207284"}, "m")
        self.assertEqual(errors, [])

    def test_unlocked_sha_rejected(self) -> None:
        # 回填值指向未锁定快照（占位漂移 / 手滑 / 快照被删后遗留）→ 拒绝
        errors = check_snapshot_anchor({"gold-156"}, "deadbeef", {"30b8344"}, "m")
        self.assertEqual(len(errors), 1)
        self.assertIn("不是已锁定快照", errors[0])
        self.assertIn("deadbeef", errors[0])

    def test_placeholder_allowed_when_mechanism_not_enabled(self) -> None:
        # 零售模型现状：有用例但数据未装载、无可锚快照 → 占位是如实声明，允许
        for sha in (None, "", PLACEHOLDER_SNAPSHOT_SHA):
            errors = check_snapshot_anchor({"gold-001"}, sha, {"30b8344"}, "m")
            self.assertEqual(errors, [])

    def test_no_cases_no_constraint(self) -> None:
        # 无用例指标不强制锚定（无论占位还是已锁值都不报）
        self.assertEqual(check_snapshot_anchor(set(), None, {"30b8344"}, "m"), [])
        self.assertEqual(check_snapshot_anchor(set(), "ghost", {"30b8344"}, "m"), [])


class TestPolicyConsistency(unittest.TestCase):
    """ADR-0021 决策 ⑥ 四检查（判据 8）：真实数据正例 + 四条单点反例。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.roles = dict(ROLE_DIRECTORY)
        cls.policies = load_policies_by_name(POLICY_PATH)

    def _run(
        self,
        referenced: dict[str, set[str]] | None = None,
        policies: dict[str, dict] | None = None,
        roles: dict[str, RoleSpec] | None = None,
    ) -> list[str]:
        errors: list[str] = []
        check_policy_consistency(
            collect_referenced_policies(MODEL_YAMLS) if referenced is None else referenced,
            self.policies if policies is None else policies,
            self.roles if roles is None else roles,
            errors,
        )
        return errors

    def test_real_data_passes(self) -> None:
        """正例：真实语义层全绿（落地后 make lint 全绿的单元版）。"""
        self.assertEqual(
            collect_referenced_policies(MODEL_YAMLS),
            {
                "rp_branch_visible": {"atlas_finance.ossie.yaml"},
                "rp_dept_visible": {"atlas_retail.ossie.yaml"},
            },
        )
        self.assertEqual(self._run(), [])

    def test_check1_unregistered_role_rejected(self) -> None:
        """反例 1：删掉 broker 注册 → 检查 1 报「未在 ROLE_DIRECTORY 注册」。"""
        roles = {k: v for k, v in self.roles.items() if k != "broker"}
        errors = self._run(roles=roles)
        self.assertEqual(len(errors), 1)
        self.assertIn("broker 未在 ROLE_DIRECTORY 注册", errors[0])

    def test_check2_orphan_registration_rejected(self) -> None:
        """反例 2：注册一个 YAML 里不存在的角色 → 检查 2 报「注册孤儿」。"""
        roles = dict(self.roles)
        roles["ghost_role"] = RoleSpec(("x",), frozenset(), "ghost")
        errors = self._run(roles=roles)
        self.assertEqual(len(errors), 1)
        self.assertIn("ghost_role", errors[0])
        self.assertIn("注册孤儿", errors[0])

    def test_check3_required_claims_drift_rejected(self) -> None:
        """反例 3a：required_claims 少键 → 检查 3 报「≠ 模板占位符」。"""
        roles = dict(self.roles)
        roles["category_analyst"] = RoleSpec(("region",), frozenset({"categories"}))
        errors = self._run(roles=roles)
        self.assertEqual(len(errors), 1)
        self.assertIn("required_claims", errors[0])

    def test_check3_list_claims_drift_rejected(self) -> None:
        """反例 3b：list_claims 缺 sql_in 键 → 检查 3 报「≠ 模板 sql_in 占位符」。"""
        roles = dict(self.roles)
        roles["category_analyst"] = RoleSpec(("region", "categories"), frozenset())
        errors = self._run(roles=roles)
        self.assertEqual(len(errors), 1)
        self.assertIn("list_claims", errors[0])

    def test_check4_orphan_policy_rejected(self) -> None:
        """反例 4：策略无模型引用 → 检查 4 报「策略孤儿」。"""
        policies = dict(self.policies)
        policies["rp_ghost"] = {
            "name": "rp_ghost",
            "roles": [{"name": "hq_admin", "condition": "1=1"}],
        }
        errors = self._run(policies=policies)
        self.assertEqual(len(errors), 1)
        self.assertIn("rp_ghost", errors[0])
        self.assertIn("策略孤儿", errors[0])


if __name__ == "__main__":
    unittest.main()
