"""eval.concept_eval 契约测试（ADR-0032 M0-T2 统计 + M1-T5 真版三分）。

覆盖：
- summarize（M0-T2）：混合样本计数 / 角色分布 / 概念去重 / registry 覆盖
  （not_in_registry 非零须出现在 missing 清单——M0-T2 停批条件的证据面）；
- classify_annotation（M1-T5）：三分边界——hit（命中且概念一致）/ mismatch
  （命中但概念不一致）/ miss（术语未命中、标注概念不在索引）；
- summarize_consistency：计数 + rate = hit/标注总数；同 term 多命中任一正确
  即 hit；无层（index=None）→ 全 miss（0 命中基线，本体对照）；
- build_report：stage="matcher"、layer_enabled 如实、consistency 落位、
  notes 不再声称"未实现"。

fixture：合成 ConceptIndex（直接构造 dataclass，无产物文件依赖）。
"""

from __future__ import annotations

import unittest
from typing import Any

from agent.concept_index import ConceptEntry, ConceptIndex, Term
from eval.concept_eval import (
    build_report,
    classify_annotation,
    summarize,
    summarize_consistency,
)

# 真实形态 IRI：一条取真 registry（fibo FND），一条构造为 registry 外（验证 missing 标注）
IRI_IN = "https://spec.edmcouncil.org/fibo/ontology/FND/Accounting/CurrencyAmount/MonetaryAmount"
IRI_OUT = "https://example.invalid/ontology/NotInRegistry/Placeholder"


def _ann(term: str, concept: str, role: str) -> dict[str, str]:
    return {"term": term, "concept": concept, "role": role, "note": ""}


# --- M1-T5 合成 fixture：直接构造 ConceptIndex dataclass（无产物文件依赖） ---

FEE_IRI = "https://example.org/fibo/Fee"
MA_IRI = "https://example.org/fibo/MonetaryAmount"
NOT_IN_INDEX_IRI = "https://example.org/fibo/NotInIndex"


def _term(text: str, *, lang: str = "zh", source: str = "S1") -> Term:
    return Term(text=text, lang=lang, source=source, via=None)


def _concept(iri: str, *terms: Term) -> ConceptEntry:
    return ConceptEntry(
        iri=iri,
        label=iri.rsplit("/", 1)[-1],
        definition=None,
        terms=terms,
        parents=(),
        children=(),
        objects=(),
    )


def _index(concepts: dict[str, ConceptEntry]) -> ConceptIndex:
    return ConceptIndex(
        schema_version=1,
        generated_at="2026-10-14T00:00:00+08:00",
        fibo_commit="test-fibo",
        commons_release="test-commons",
        source_semantic_sha="0" * 64,
        concepts=concepts,
    )


def _base_index() -> ConceptIndex:
    return _index(
        {
            "Fee": _concept(FEE_IRI, _term("经纪服务费", source="S3"), _term("fee", lang="en")),
            "MonetaryAmount": _concept(MA_IRI, _term("交易额"), _term("成交总金额", source="S3")),
        }
    )


def _sample(question: str, *annotations: tuple[str, str]) -> dict[str, Any]:
    return {
        "id": f"t-{question[:6]}",
        "question": question,
        "fibo_concepts": [_ann(term, concept, "measure") for term, concept in annotations],
    }


class TestSummarize(unittest.TestCase):
    def _samples(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "t-1",
                "fibo_concepts": [
                    _ann("交易额", IRI_IN, "measure"),
                    _ann("交易", IRI_OUT, "event"),
                ],
            },
            {"id": "t-2", "fibo_concepts": [_ann("股票", IRI_IN, "entity")]},
            {"id": "t-3"},  # 无标注样本：不计入 samples_annotated
        ]

    def test_counts_roles_and_registry_coverage(self) -> None:
        out = summarize(self._samples(), {IRI_IN})
        self.assertEqual(out["samples_total"], 3)
        self.assertEqual(out["samples_annotated"], 2)
        self.assertEqual(out["terms_total"], 3)
        self.assertEqual(out["terms_by_role"], {"measure": 1, "event": 1, "entity": 1})
        self.assertEqual(out["concepts_distinct"], 2)
        self.assertEqual(
            out["registry_coverage"],
            {"in_registry": 1, "not_in_registry": 1, "missing": [IRI_OUT]},
        )

    def test_no_annotations(self) -> None:
        """全无标注：计数归零、missing 为空（不得伪造一致率字段——产物层另测）。"""
        out = summarize([{"id": "t-9"}], {IRI_IN})
        self.assertEqual(out["samples_annotated"], 0)
        self.assertEqual(out["terms_total"], 0)
        self.assertEqual(out["terms_by_role"], {})
        self.assertEqual(out["concepts_distinct"], 0)
        self.assertEqual(
            out["registry_coverage"],
            {"in_registry": 0, "not_in_registry": 0, "missing": []},
        )


class TestClassifyAnnotation(unittest.TestCase):
    """三分边界（合成索引；匹配语义 = M1-T3 match_terms：子串 + 不去重多命中）。"""

    def setUp(self) -> None:
        self.index = _base_index()

    def test_hit(self) -> None:
        self.assertEqual(
            classify_annotation("2013 年经纪服务费是多少？", "经纪服务费", FEE_IRI, self.index),
            "hit",
        )

    def test_mismatch(self) -> None:
        """术语被命中但桥到别的概念（命中 Fee ≠ 标注 MonetaryAmount）。"""
        self.assertEqual(
            classify_annotation("经纪服务费环比", "经纪服务费", MA_IRI, self.index),
            "mismatch",
        )

    def test_miss_term_not_matched(self) -> None:
        """标注术语未被任何索引术语命中（问句里无对应表达）。"""
        self.assertEqual(classify_annotation("总资产是多少", "总资产", MA_IRI, self.index), "miss")

    def test_miss_concept_not_in_index(self) -> None:
        """术语被命中但标注概念反查失败 → miss（概念层未覆盖，与无层语义一致）。"""
        self.assertEqual(
            classify_annotation("经纪服务费是多少", "经纪服务费", NOT_IN_INDEX_IRI, self.index),
            "miss",
        )

    def test_multi_hit_any_correct_counts_hit(self) -> None:
        """同 term 桥接多个概念（不去重多命中）：任一命中标注概念即 hit。"""
        index = _index(
            {
                "Fee": _concept(FEE_IRI, _term("费用")),
                "Expense": _concept("https://example.org/fibo/Expense", _term("费用")),
            }
        )
        self.assertEqual(classify_annotation("费用明细", "费用", FEE_IRI, index), "hit")


class TestSummarizeConsistency(unittest.TestCase):
    def test_counts_and_rate(self) -> None:
        samples = [
            _sample("2013 年经纪服务费", ("经纪服务费", FEE_IRI)),  # hit
            _sample("交易额与经纪服务费", ("经纪服务费", FEE_IRI)),  # hit（多命中含标注项）
            _sample("交易额", ("交易额", FEE_IRI)),  # mismatch（交易额 → MonetaryAmount）
            _sample("总资产", ("总资产", MA_IRI)),  # miss
        ]
        self.assertEqual(
            summarize_consistency(samples, _base_index()),
            {"hit": 2, "mismatch": 1, "miss": 1, "rate": "2/4"},
        )

    def test_no_layer_all_miss(self) -> None:
        """无层（index=None）= 0 命中基线（本体对照），分母不变。"""
        samples = [_sample("经纪服务费", ("经纪服务费", FEE_IRI))]
        self.assertEqual(
            summarize_consistency(samples, None),
            {"hit": 0, "mismatch": 0, "miss": 1, "rate": "0/1"},
        )

    def test_no_annotations(self) -> None:
        """零标注：全零 + rate = "0/0"（不臆造分母）。"""
        self.assertEqual(
            summarize_consistency([{"id": "t-x", "question": "无标注"}], _base_index()),
            {"hit": 0, "mismatch": 0, "miss": 0, "rate": "0/0"},
        )


class TestBuildReport(unittest.TestCase):
    def test_stage_and_consistency_landed(self) -> None:
        samples = [_sample("经纪服务费", ("经纪服务费", FEE_IRI))]
        report = build_report(samples, _base_index(), "abc1234", registry_iris={FEE_IRI})
        self.assertEqual(report["eval"], "concept-alignment")
        self.assertEqual(report["stage"], "matcher")
        self.assertEqual(report["sha"], "abc1234")
        self.assertTrue(report["layer_enabled"])
        self.assertEqual(report["consistency"], {"hit": 1, "mismatch": 0, "miss": 0, "rate": "1/1"})
        self.assertEqual(report["stats"]["terms_total"], 1)  # M0 统计面保留
        self.assertNotIn("未实现", report["notes"])  # 旧「matcher 未实现」文案必须消失

    def test_no_layer_report(self) -> None:
        samples = [_sample("经纪服务费", ("经纪服务费", FEE_IRI))]
        report = build_report(samples, None, "abc1234", registry_iris={FEE_IRI})
        self.assertFalse(report["layer_enabled"])
        self.assertEqual(report["consistency"]["rate"], "0/1")
        self.assertIn("无层", report["notes"])  # 无层语义在口径声明中显式


if __name__ == "__main__":
    unittest.main()
