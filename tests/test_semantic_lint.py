"""semantic/lint.py 契约测试（权威源唯一性检查）。

口径：
- 真实仓库必须通过检查（归档后 semantic/ 下语义定义文件仅在 ossie/synonyms/policies）；
- 违规形态必须被抓出（非权威子目录 / semantic/ 根直放）；
- 归档区（`_*` 前缀，如 _legacy/）与 __pycache__ 豁免——它们是历史对照，不是运行时源。

用 tmp 目录构造 fixture（check_semantic_authority 接受 root 参数即为可测性）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from semantic.lint import check_semantic_authority

REPO = Path(__file__).resolve().parent.parent


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# fixture\n", encoding="utf-8")
    return path


class TestAuthorityRealRepo(unittest.TestCase):
    def test_real_repo_clean(self) -> None:
        """真实仓库零违规（幽灵定义已迁 semantic/_legacy/，见其 README）。"""
        self.assertEqual(check_semantic_authority(), [])


class TestAuthorityFixtures(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "semantic"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_unauthorized_subdir_flagged(self) -> None:
        """非权威子目录（如已废弃的 metrics/）里的 yml 必须报。"""
        _touch(self.root / "metrics" / "gmv.yml")
        errors = check_semantic_authority(self.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("metrics/gmv.yml", errors[0])
        self.assertIn("非权威目录", errors[0])

    def test_yaml_root_flagged(self) -> None:
        """semantic/ 根直放语义定义文件必须报。"""
        _touch(self.root / "model.yaml")
        errors = check_semantic_authority(self.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("不得直放于 semantic/ 根", errors[0])

    def test_legacy_and_pycache_exempt(self) -> None:
        """归档区（_* 前缀）与 __pycache__ 豁免；权威目录放行。"""
        _touch(self.root / "_legacy" / "models" / "orders.yml")
        _touch(self.root / "__pycache__" / "junk.yml")
        _touch(self.root / "ossie" / "atlas_x.ossie.yaml")
        _touch(self.root / "synonyms" / "en_us.yml")
        _touch(self.root / "policies" / "row_policy.yml")
        self.assertEqual(check_semantic_authority(self.root), [])

    def test_json_schema_and_md_not_restricted(self) -> None:
        """检查只针对语义定义文件（*.yaml/*.yml）：Schema/文档不受位置约束。"""
        _touch(self.root / "governance" / "x.schema.json")
        _touch(self.root / "governance" / "README.md")
        self.assertEqual(check_semantic_authority(self.root), [])


if __name__ == "__main__":
    unittest.main()
