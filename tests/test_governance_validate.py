"""治理扩展 supersedes 链语义契约测试（Day 27 指标版本机制）。

覆盖 check_supersedes_chains 的全部规则：取代目标存在、非自身、
版本号严格递增（天然防环）、被取代者不得仍为 active、治理记录不得重名。

口径说明：指标演进一律采用**新名 + supersedes 指向旧名**（同名指标在
ossie_validate 已被全局唯一约束拦截，不存在同名两代共存的合法形态）。
纯函数级测试，不依赖语义层文件与文件系统。

这些用例必须全部通过才能改 governance_validate.py。
"""

from __future__ import annotations

import unittest

from semantic.governance_validate import (
    MetricGovernance,
    check_supersedes_chains,
)


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


if __name__ == "__main__":
    unittest.main()
