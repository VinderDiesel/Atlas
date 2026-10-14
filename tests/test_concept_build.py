"""概念索引构建器纯函数层契约测试（ADR-0032 M1-T1）。

覆盖（均为合成输入，无 rdflib）：
- `induce_hierarchy`：registry 内归纳子图——未注册中间类不物化、跨级边
  `transitive=True`、直接边 False、多路径 False 优先；
- `assemble_index`：术语三源合并与 provenance（S1 本体注解 / S2 对齐对象
  synonyms 带 via / S3 人工别名）、反向索引 objects、registry 外 IRI
  响亮失败（ValueError 含 IRI 清单）、label/definition 落位、重建确定性。

接口补全说明（依计划 §4.2 M1-T1 步骤：术语三源合并测试 + 产物 schema 为准）：
- `MappingEntry` 增 `synonyms` 字段（S2 数据入口：提取层采自对齐对象的
  `ai_context.synonyms`；计划接口清单未列，纯函数层 4 参数下无其他承载通道）；
- `assemble_index` 第 5 参数 `fibo_facts`（S1 数据入口：label/definition/术语，
  提取层从 rdflib 图采集后以纯数据传入）。过程已登记账本与提交说明。
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from data.fibo.build_concept_index import assemble_index, induce_hierarchy

IRI_A = "https://example.invalid/ontology/A"
IRI_B = "https://example.invalid/ontology/B"
IRI_C = "https://example.invalid/ontology/C"
IRI_X = "https://example.invalid/ontology/X"
IRI_M = "https://example.invalid/ontology/M"


def _mapping(
    obj: str,
    iri: str,
    *,
    match_type: str = "broad_match",
    confidence: str = "high",
    note: str = "",
    synonyms: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "object": obj,
        "concept_iri": iri,
        "match_type": match_type,
        "confidence": confidence,
        "note": note,
        "synonyms": synonyms or [],
    }


def _facts(
    label: str, definition: str | None = None, terms: list[str] | None = None
) -> dict[str, Any]:
    return {"label": label, "definition": definition, "terms": terms or []}


class TestInduceHierarchy(unittest.TestCase):
    def test_skips_unregistered_middle(self) -> None:
        """A→B→C（B 未注册）⇒ C.parents=[(A, True)]，B 不出现。"""
        edges = {IRI_C: [IRI_B], IRI_B: [IRI_A]}
        out = induce_hierarchy(edges, {"A": IRI_A, "C": IRI_C})
        self.assertEqual(set(out), {"A", "C"})
        self.assertEqual(out["C"]["parents"], [("A", True)])
        self.assertEqual(out["A"]["children"], [("C", True)])
        self.assertEqual(out["A"]["parents"], [])
        self.assertEqual(out["C"]["children"], [])

    def test_direct_registry_edges_and_order(self) -> None:
        """直接父均在 registry ⇒ transitive=False；多父按短名排序。"""
        edges = {IRI_C: [IRI_B, IRI_A]}
        out = induce_hierarchy(edges, {"A": IRI_A, "B": IRI_B, "C": IRI_C})
        self.assertEqual(out["C"]["parents"], [("A", False), ("B", False)])
        self.assertEqual(out["A"]["children"], [("C", False)])

    def test_direct_edge_wins_over_transitive_path(self) -> None:
        """C 直连 P（False）且经未注册 X 也到 P ⇒ 归并为 (P, False) 一条。"""
        edges = {IRI_C: [IRI_A, IRI_X], IRI_X: [IRI_A]}
        out = induce_hierarchy(edges, {"A": IRI_A, "C": IRI_C})
        self.assertEqual(out["C"]["parents"], [("A", False)])


class TestAssembleIndex(unittest.TestCase):
    def _assemble(self, **overrides: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "registry": {"M": IRI_M},
            "mapped": [
                _mapping("metric:x", IRI_M, note="note-x", synonyms=["交易额", "成交额"]),
                _mapping("dim_m", IRI_M, match_type="exact_match", synonyms=["交易额"]),
            ],
            "hierarchy": {"M": {"parents": [], "children": []}},
            "aliases": {"M": ["成交金额"]},
            "fibo_facts": {"M": _facts("MonetaryAmount", "money amount", ["MonetaryAmount"])},
        }
        kwargs.update(overrides)
        return assemble_index(**kwargs)

    def test_terms_three_sources_with_provenance(self) -> None:
        concepts = self._assemble()["concepts"]
        terms = concepts["M"]["terms"]
        self.assertIn({"text": "MonetaryAmount", "lang": "en", "source": "S1", "via": None}, terms)
        # S2：同词多对象 ⇒ 各 via 一条（并集保 provenance）；lang 由 CJK 判定
        self.assertIn({"text": "交易额", "lang": "zh", "source": "S2", "via": "metric:x"}, terms)
        self.assertIn({"text": "交易额", "lang": "zh", "source": "S2", "via": "dim_m"}, terms)
        self.assertIn({"text": "成交金额", "lang": "zh", "source": "S3", "via": None}, terms)
        # 排序 (source, text, via)：S1 在前，S2 内按 text 排序
        sources = [t["source"] for t in terms]
        self.assertEqual(sources, ["S1", "S2", "S2", "S2", "S3"])
        self.assertEqual(
            [t["text"] for t in terms if t["source"] == "S2"], ["交易额", "交易额", "成交额"]
        )

    def test_objects_reverse_index(self) -> None:
        concepts = self._assemble()["concepts"]
        objects = concepts["M"]["objects"]
        self.assertEqual(
            objects,
            [
                {
                    "object": "dim_m",
                    "match_type": "exact_match",
                    "confidence": "high",
                    "note": "",
                },
                {
                    "object": "metric:x",
                    "match_type": "broad_match",
                    "confidence": "high",
                    "note": "note-x",
                },
            ],
        )

    def test_label_definition_from_facts(self) -> None:
        concepts = self._assemble(fibo_facts={"M": _facts("Fee")})["concepts"]
        self.assertEqual(concepts["M"]["label"], "Fee")
        self.assertIsNone(concepts["M"]["definition"])
        self.assertEqual(concepts["M"]["iri"], IRI_M)

    def test_registry_foreign_iri_raises_with_list(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._assemble(mapped=[_mapping("metric:x", IRI_C)])
        self.assertIn(IRI_C, str(ctx.exception))

    def test_deterministic_rebuild(self) -> None:
        first = json.dumps(self._assemble(), ensure_ascii=False, sort_keys=True)
        second = json.dumps(self._assemble(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
