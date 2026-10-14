"""semantic/lint.py 第 7 节（概念索引 + 别名校验）契约测试（ADR-0032 M1-T4）。

覆盖：
1. 别名文件（concepts_zh_cn.yml）：非 registry 短名 → 错；空术语 → 错；同概念内
   重复 → 错；同一术语映射两个概念（同 locale 歧义 = curation bug）→ 错；YAML
   畸形 → 错
2. 索引：结构非法（垃圾 JSON / 缺键 / schema_version 未知）→ 错；概念 IRI ∉
   registry → 错；objects[].object 不在语义模型（指标/字段/关系定位失败）→ 错；
   与 ossie fibo_alignment 双向一致（孤儿 / 漏配）→ 错；
   source_semantic_sha ≠ 当前 ossie 文件 sha256 → 错；
   索引内 S3 术语 ≠ 当前别名文件合并结果 → 错（改源不重建 = 红）
3. 真实仓库 → 零错（同时锁 lint 与构建器口径同源）；目录 / 文件不存在 → 跳过不报错

fixture 策略（镜像 tests/test_semantic_lint.py 的 tmp 目录模式）：
- 别名用例：纯 tmp 合成（键必须取自真实 iri_registry——registry / ossie 是不可
  注入的仓库对照，不做 mock）；
- 索引用例：真实产物深拷贝 → 局部 mutate → 写 tmp（除被 mutate 点外全部合法，
  错误精确可控）；相对锚点含义见下方常量注释。
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from semantic.lint import check_concept_layer

REPO = Path(__file__).resolve().parent.parent
REAL_INDEX_PATH = REPO / "semantic" / "ontology" / "atlas_finance.concept_index.json"
REAL_ALIAS_PATH = REPO / "semantic" / "synonyms" / "concepts_zh_cn.yml"
REGISTRY_PATH = REPO / "data" / "fibo" / "iri_registry.json"

FEE = "fibo-fbc-pas-ca:Fee"  # 真实产物锚点：4 条 objects、2 条 S3 术语
EQUITY = "fibo-fbc-fi-fi:EquityInstrument"  # 真实产物锚点：1 条 S3 术语（个股）
MAPPED_OBJECT = "metric:commission_revenue"  # 在 ossie fibo_alignment 内（Fee 名下）
UNMAPPED_METRIC = "metric:total_trade_tax"  # 在语义模型内、不在 fibo_alignment（孤儿锚点）


def _registry_names() -> list[str]:
    doc = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return sorted(doc["concepts"])


def _real_index_doc() -> dict[str, Any]:
    return json.loads(REAL_INDEX_PATH.read_text(encoding="utf-8"))


def _real_alias_concepts() -> dict[str, list[str]]:
    doc = yaml.safe_load(REAL_ALIAS_PATH.read_text(encoding="utf-8"))
    return {str(k): [str(t) for t in v] for k, v in (doc.get("concepts") or {}).items()}


class FixtureMixin(unittest.TestCase):
    """tmp 目录 fixture：ontology/ 与 synonyms/ 各自独立注入。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ontology = self.root / "ontology"
        self.ontology.mkdir()
        self.synonyms = self.root / "synonyms"
        self.synonyms.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_alias(self, concepts: object, *, name: str = "concepts_zh_cn.yml") -> Path:
        path = self.synonyms / name
        if isinstance(concepts, str):
            path.write_text(concepts, encoding="utf-8")
        else:
            path.write_text(
                yaml.safe_dump({"concepts": concepts}, allow_unicode=True), encoding="utf-8"
            )
        return path

    def _write_index(self, doc: object) -> Path:
        path = self.ontology / "atlas_finance.concept_index.json"
        if isinstance(doc, str):
            path.write_text(doc, encoding="utf-8")
        else:
            path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return path

    def _check(self) -> list[str]:
        return check_concept_layer(self.ontology, self.synonyms)

    def _joined(self) -> str:
        return "\n".join(self._check())


class TestAliasFixtures(FixtureMixin):
    """别名文件校验（ontology 目录空 → 索引段跳过，错误仅来自别名）。"""

    def test_unknown_concept_name(self) -> None:
        self._write_alias({"no-such:Concept": ["术语"]})
        joined = self._joined()
        self.assertIn("no-such:Concept", joined)
        self.assertIn("iri_registry.json", joined)

    def test_empty_term(self) -> None:
        name = _registry_names()[0]
        self._write_alias({name: ["", "   "]})
        joined = self._joined()
        self.assertIn("空术语", joined)
        self.assertIn(name, joined)

    def test_duplicate_term_same_concept(self) -> None:
        name = _registry_names()[0]
        self._write_alias({name: ["重复词", "重复词"]})
        joined = self._joined()
        self.assertIn("重复", joined)
        self.assertIn("重复词", joined)

    def test_same_term_two_concepts(self) -> None:
        first, second = _registry_names()[:2]
        self._write_alias({first: ["歧义词"], second: ["歧义词"]})
        joined = self._joined()
        self.assertIn("歧义词", joined)
        self.assertIn(first, joined)
        self.assertIn(second, joined)

    def test_malformed_yaml(self) -> None:
        self._write_alias("{ not: [valid")
        self.assertIn("YAML", self._joined())

    def test_valid_alias_zero_errors(self) -> None:
        name = _registry_names()[0]
        self._write_alias({name: ["合法术语"]})
        self.assertEqual(self._check(), [])


class TestIndexStructure(FixtureMixin):
    """索引结构非法 → 错（复用 parse_concept_index 强校验，消息含路径）。"""

    def test_malformed_json(self) -> None:
        self._write_index("{ not json")
        self.assertIn("atlas_finance.concept_index.json", self._joined())

    def test_missing_required_key(self) -> None:
        doc = _real_index_doc()
        del doc["source_semantic_sha"]
        self._write_index(doc)
        self.assertIn("source_semantic_sha", self._joined())

    def test_unknown_schema_version(self) -> None:
        doc = _real_index_doc()
        doc["schema_version"] = 2
        self._write_index(doc)
        self.assertIn("schema_version", self._joined())


class TestIndexCrossChecks(FixtureMixin):
    """索引交叉校验：真实产物深拷贝 + 局部 mutate；默认配套真实别名（S3 段干净）。"""

    def setUp(self) -> None:
        super().setUp()
        self._write_alias(_real_alias_concepts())

    def test_concept_iri_not_in_registry(self) -> None:
        doc = _real_index_doc()
        doc["concepts"][FEE]["iri"] = "https://example.org/NotRegistered"
        self._write_index(doc)
        joined = self._joined()
        self.assertIn("iri_registry.json", joined)
        self.assertIn(FEE, joined)

    def test_object_not_in_semantic_model(self) -> None:
        doc = _real_index_doc()
        doc["concepts"][FEE]["objects"].append(
            {
                "object": "metric:no_such_metric",
                "match_type": "exact_match",
                "confidence": "high",
                "note": "fixture",
            }
        )
        self._write_index(doc)
        joined = self._joined()
        self.assertIn("metric:no_such_metric", joined)
        self.assertIn("不在语义模型", joined)

    def test_orphan_object(self) -> None:
        """在语义模型但不在 fibo_alignment 的对象入索引 → 孤儿。"""
        doc = _real_index_doc()
        doc["concepts"][FEE]["objects"].append(
            {
                "object": UNMAPPED_METRIC,
                "match_type": "exact_match",
                "confidence": "high",
                "note": "fixture",
            }
        )
        self._write_index(doc)
        joined = self._joined()
        self.assertIn(UNMAPPED_METRIC, joined)
        self.assertIn("孤儿", joined)

    def test_missing_mapping(self) -> None:
        """ossie fibo_alignment 有、索引无 → 漏配。"""
        doc = _real_index_doc()
        fee = doc["concepts"][FEE]
        fee["objects"] = [o for o in fee["objects"] if o["object"] != MAPPED_OBJECT]
        self._write_index(doc)
        joined = self._joined()
        self.assertIn(MAPPED_OBJECT, joined)
        self.assertIn("漏配", joined)

    def test_stale_source_semantic_sha(self) -> None:
        doc = _real_index_doc()
        doc["source_semantic_sha"] = "0" * 64
        self._write_index(doc)
        joined = self._joined()
        self.assertIn("source_semantic_sha", joined)
        self.assertIn("ontology-build", joined)

    def test_s3_terms_drift(self) -> None:
        """别名文件加术语而索引未重建 → S3 不一致。"""
        self._write_index(_real_index_doc())
        concepts = _real_alias_concepts()
        concepts[EQUITY].append("股票")
        self._write_alias(concepts)
        joined = self._joined()
        self.assertIn("S3", joined)
        self.assertIn("股票", joined)

    def test_s3_drift_when_alias_file_missing(self) -> None:
        """别名文件缺失 → 索引内 S3 术语全部视为漂移（文件是事实源，防误删静默）。"""
        self._write_index(_real_index_doc())
        (self.synonyms / "concepts_zh_cn.yml").unlink()
        self.assertIn("S3", self._joined())

    def test_valid_index_zero_errors(self) -> None:
        """合法 fixture（真实产物 + 真实别名副本）→ 零错。"""
        self._write_index(copy.deepcopy(_real_index_doc()))
        self._write_alias(_real_alias_concepts())
        self.assertEqual(self._check(), [])


class TestSkip(unittest.TestCase):
    """目录 / 文件不存在 → 跳过不报错（概念层未启用的合法形态）。"""

    def test_missing_dirs_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(check_concept_layer(root / "no-ontology", root / "no-synonyms"), [])

    def test_empty_dirs_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ontology").mkdir()
            (root / "synonyms").mkdir()
            self.assertEqual(check_concept_layer(root / "ontology", root / "synonyms"), [])


class TestRealRepo(unittest.TestCase):
    def test_real_repo_clean(self) -> None:
        """真实仓库零错：锁 lint 第 7 节与构建器口径同源（改源不重建会红）。"""
        self.assertEqual(check_concept_layer(), [])


if __name__ == "__main__":
    unittest.main()
