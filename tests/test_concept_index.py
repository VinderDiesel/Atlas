"""agent/concept_index.py 的契约测试（ADR-0032 M1-T3）

覆盖：
1. `load_concept_index`：文件缺失 → None；`ATLAS_CONCEPT_LAYER` off 族（大小写空格
   容忍）→ None；畸形 JSON / 缺 `schema_version` → ValueError（消息含路径）；
   进程缓存一次加载
2. `parse_concept_index`：强校验——缺键 / 类型错 / 白名单（lang、source）/ 悬空边 /
   S2 缺 via provenance（镜像 value_domain.parse_profile 的"失败响亮"）
3. `match_terms`：en 大小写不敏感；zh 子串；按问句 locale 取语种（ADR-0032 ③）；
   最长命中优先、同长按索引序；不去重多命中（同文本不同 provenance 各保留）
4. `expand`：自身 + 后代全量；ancestors=1 并入最近一层祖先（不含更高层）；
   transitive 边照常连通；输出确定序（索引文件序）；未注册短名 → ValueError
5. `concept_terms`：按概念序 + 术语文件序、文本精确去重；locale 过滤
6. `detect_locale`：与 `Planner._resolve_locale` 的启发式分支（None 参数）逐例一致
"""

from __future__ import annotations

import copy
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agent import concept_index as ci
from agent.planner import Planner


def _term(text: str, lang: str, source: str, via: str | None = None) -> dict[str, object]:
    return {"text": text, "lang": lang, "source": source, "via": via}


def _edge(concept: str, transitive: bool = False) -> dict[str, object]:
    return {"concept": concept, "transitive": transitive}


def _object(object_: str) -> dict[str, object]:
    return {
        "object": object_,
        "match_type": "exact_match",
        "confidence": "high",
        "note": "fixture",
    }


def _entry(
    iri: str,
    label: str,
    terms: list[dict[str, object]],
    parents: list[dict[str, object]] | None = None,
    children: list[dict[str, object]] | None = None,
    objects: list[dict[str, object]] | None = None,
    definition: str | None = None,
) -> dict[str, object]:
    return {
        "iri": iri,
        "label": label,
        "definition": definition,
        "terms": terms,
        "parents": parents or [],
        "children": children or [],
        "objects": objects or [],
    }


def _fixture_doc() -> dict[str, object]:
    """8 概念合成索引（结构 = 产物 schema v1；键序即锁定的索引文件序）。

    层级图：m:Top ─(mid)─▶ m:Mid ─▶ m:Leaf；m:Top ─(transitive)─▶ m:Far；
    m:Amount ─▶ m:Fee（m:Fee.parents 回边自洽）。
    """
    return {
        "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00+08:00",
        "fibo_commit": "deadbeef",
        "commons_release": "20250801",
        "source_semantic_sha": "0" * 64,
        "concepts": {
            "m:Top": _entry(
                "https://example.org/Top",
                "top",
                [_term("top", "en", "S1"), _term("顶层", "zh", "S2", "metric:x")],
                children=[_edge("m:Mid"), _edge("m:Far", True)],
                definition="def top",
            ),
            "m:Mid": _entry(
                "https://example.org/Mid",
                "mid",
                [_term("mid", "en", "S1"), _term("中间", "zh", "S2", "dim:y")],
                parents=[_edge("m:Top")],
                children=[_edge("m:Leaf")],
            ),
            "m:Leaf": _entry(
                "https://example.org/Leaf",
                "leaf",
                [_term("leaf", "en", "S1"), _term("叶子", "zh", "S3")],
                parents=[_edge("m:Mid")],
            ),
            "m:Far": _entry(
                "https://example.org/Far",
                "far",
                [_term("far", "en", "S1"), _term("远处", "zh", "S3")],
                parents=[_edge("m:Top", True)],
            ),
            "m:Fee": _entry(
                "https://example.org/Fee",
                "fee",
                [
                    _term("fee", "en", "S1"),
                    _term("佣金", "zh", "S2", "metric:a"),
                    _term("佣金", "zh", "S2", "metric:b"),
                    _term("服务费", "zh", "S3"),
                ],
                parents=[_edge("m:Amount")],
                objects=[_object("metric:commission_revenue")],
                definition="charge for services",
            ),
            "m:Amount": _entry(
                "https://example.org/Amount",
                "amount",
                [
                    _term("交易额", "zh", "S2", "metric:c"),
                    _term("总交易额", "zh", "S3"),
                ],
                children=[_edge("m:Fee")],
            ),
            "m:P": _entry(
                "https://example.org/P",
                "p",
                [_term("成交额", "zh", "S2", "metric:p")],
            ),
            "m:Q": _entry(
                "https://example.org/Q",
                "q",
                [_term("营业额", "zh", "S2", "metric:q")],
            ),
        },
    }


class FixtureMixin(unittest.TestCase):
    """tmp 目录 fixture 基类：不触碰仓库真文件（同 test_value_domain 模式）。"""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        ci.clear_cache()

    def tearDown(self) -> None:
        ci.clear_cache()
        self._tmp.cleanup()

    def _write(self, doc: object, name: str = "atlas_finance.concept_index.json") -> Path:
        path = self.dir / name
        if isinstance(doc, str):
            path.write_text(doc, encoding="utf-8")
        else:
            path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return path

    def _index(self) -> ci.ConceptIndex:
        self._write(_fixture_doc())
        index = ci.load_concept_index(ontology_dir=self.dir)
        assert index is not None
        return index


class TestLoadConceptIndex(FixtureMixin):
    def test_missing_file_returns_none(self) -> None:
        """产物缺失（如 atlas_retail 无映射不生成）→ None = 全缝旁路。"""
        self.assertIsNone(ci.load_concept_index(ontology_dir=self.dir))
        self.assertIsNone(ci.load_concept_index(ontology_dir=self.dir / "nonexistent"))

    def test_other_model_returns_none(self) -> None:
        self._write(_fixture_doc())
        self.assertIsNone(ci.load_concept_index("atlas_retail", self.dir))

    def test_layer_off_values_return_none(self) -> None:
        """off/0/false（大小写与首尾空格容忍）→ None，即便文件存在。"""
        self._write(_fixture_doc())
        for value in ("off", "OFF", " Off ", "0", "false", "FALSE"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {ci.LAYER_ENV: value}):
                self.assertIsNone(ci.load_concept_index(ontology_dir=self.dir))

    def test_layer_on_values_load(self) -> None:
        """on 族与未知值 → 正常加载；未设置 / 空串同样加载（默认 on）。"""
        self._write(_fixture_doc())
        for value in ("on", "1", "true", "yes", "任意"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {ci.LAYER_ENV: value}):
                self.assertIsNotNone(ci.load_concept_index(ontology_dir=self.dir))
        with mock.patch.dict(os.environ, {ci.LAYER_ENV: ""}):
            self.assertIsNotNone(ci.load_concept_index(ontology_dir=self.dir))

    def test_cache_single_load(self) -> None:
        """进程内缓存：同路径重复 load 返回同一对象；clear_cache 后重读。"""
        self._write(_fixture_doc())
        first = ci.load_concept_index(ontology_dir=self.dir)
        self.assertIs(ci.load_concept_index(ontology_dir=self.dir), first)
        ci.clear_cache()
        second = ci.load_concept_index(ontology_dir=self.dir)
        self.assertIsNot(second, first)
        self.assertEqual(second, first)

    def test_malformed_json_raises_with_path(self) -> None:
        path = self._write("{ not json")
        with self.assertRaises(ValueError) as ctx:
            ci.load_concept_index(ontology_dir=self.dir)
        self.assertIn(str(path), str(ctx.exception))

    def test_missing_schema_version_raises_with_path(self) -> None:
        doc = _fixture_doc()
        del doc["schema_version"]  # type: ignore[attr-defined]
        path = self._write(doc)
        with self.assertRaises(ValueError) as ctx:
            ci.load_concept_index(ontology_dir=self.dir)
        self.assertIn(str(path), str(ctx.exception))

    def test_unknown_schema_version_raises(self) -> None:
        for version in (2, "1", True):
            with self.subTest(version=version):
                doc = _fixture_doc()
                doc["schema_version"] = version  # type: ignore[index]
                self._write(doc)
                ci.clear_cache()
                with self.assertRaises(ValueError):
                    ci.load_concept_index(ontology_dir=self.dir)


class TestParseValidation(FixtureMixin):
    """加载即强校验：畸形/缺键/类型错 → ValueError（消息含路径），不静默降级半层。"""

    def _assert_parse_error(self, doc: object, *, needs_copy: bool = True) -> None:
        payload = copy.deepcopy(doc) if needs_copy else doc
        path = self._write(payload)
        with self.assertRaises(ValueError) as ctx:
            ci.parse_concept_index(path)
        self.assertIn(str(path), str(ctx.exception))

    def test_top_level_shape(self) -> None:
        self._assert_parse_error([], needs_copy=False)
        doc = _fixture_doc()
        del doc["concepts"]  # type: ignore[attr-defined]
        self._assert_parse_error(doc, needs_copy=False)

    def test_concepts_shape(self) -> None:
        doc = _fixture_doc()
        doc["concepts"] = []  # type: ignore[index]
        self._assert_parse_error(doc, needs_copy=False)
        doc = _fixture_doc()
        doc["concepts"] = {"m:A": "not-a-dict"}  # type: ignore[index]
        self._assert_parse_error(doc, needs_copy=False)

    def test_concept_fields(self) -> None:
        def mutate(fn: object) -> None:
            doc = _fixture_doc()
            fn(doc["concepts"]["m:P"])  # type: ignore[index,operator]
            self._assert_parse_error(doc, needs_copy=False)

        mutate(lambda e: e.pop("iri"))
        mutate(lambda e: e.update(iri=""))
        mutate(lambda e: e.pop("label"))
        mutate(lambda e: e.update(definition=123))
        mutate(lambda e: e.update(terms="x"))

    def test_term_validation(self) -> None:
        def mutate(fn: object) -> None:
            doc = _fixture_doc()
            fn(doc["concepts"]["m:P"]["terms"])  # type: ignore[index]
            self._assert_parse_error(doc, needs_copy=False)

        mutate(lambda t: t.clear())
        mutate(lambda t: t.append("x"))
        mutate(lambda t: t[0].update(text=""))
        mutate(lambda t: t[0].update(lang="fr"))
        mutate(lambda t: t[0].update(source="S4"))
        mutate(lambda t: t[0].update(via=123))
        mutate(lambda t: t[0].update(source="S2", via=None))  # S2 必须有 provenance

    def test_edge_validation(self) -> None:
        doc = _fixture_doc()
        doc["concepts"]["m:P"]["children"] = [_edge("m:Ghost")]  # type: ignore[index]
        self._assert_parse_error(doc, needs_copy=False)
        doc = _fixture_doc()
        doc["concepts"]["m:P"]["children"] = [{"concept": "m:Q", "transitive": "yes"}]  # type: ignore[index]
        self._assert_parse_error(doc, needs_copy=False)

    def test_object_validation(self) -> None:
        doc = _fixture_doc()
        doc["concepts"]["m:Fee"]["objects"][0]["object"] = ""  # type: ignore[index]
        self._assert_parse_error(doc, needs_copy=False)
        doc = _fixture_doc()
        del doc["concepts"]["m:Fee"]["objects"][0]["match_type"]  # type: ignore[index]
        self._assert_parse_error(doc, needs_copy=False)


class TestMatchTerms(FixtureMixin):
    def test_zh_substring_hit_with_provenance(self) -> None:
        index = self._index()
        self.assertEqual(
            ci.match_terms(index, "本季度服务费合计", "zh"),
            (
                ci.ConceptHit(
                    term="服务费",
                    concept="m:Fee",
                    concept_iri="https://example.org/Fee",
                    source="S3",
                    via=None,
                ),
            ),
        )

    def test_en_case_insensitive(self) -> None:
        index = self._index()
        hits = ci.match_terms(index, "TOTAL FEE REPORT", "en")
        self.assertEqual([(h.term, h.source) for h in hits], [("fee", "S1")])

    def test_longest_hit_first(self) -> None:
        """ "2013年总交易额" 同时含 "总交易额" 与 "交易额" → 长者先。"""
        index = self._index()
        hits = ci.match_terms(index, "2013年总交易额", "zh")
        self.assertEqual([h.term for h in hits], ["总交易额", "交易额"])

    def test_same_length_by_index_order(self) -> None:
        index = self._index()
        hits = ci.match_terms(index, "成交额与营业额", "zh")
        self.assertEqual(
            [(h.term, h.concept) for h in hits], [("成交额", "m:P"), ("营业额", "m:Q")]
        )

    def test_no_dedup_multiple_provenance(self) -> None:
        """同文本不同 via 各保留一条（不去重；歧义由消费缝裁决）。"""
        index = self._index()
        hits = ci.match_terms(index, "佣金", "zh")
        self.assertEqual(
            [(h.term, h.via) for h in hits], [("佣金", "metric:a"), ("佣金", "metric:b")]
        )

    def test_locale_selects_language(self) -> None:
        """按问句 locale 取语种（ADR-0032 ③）：en 问句不命中 zh 术语，反之亦然。"""
        index = self._index()
        self.assertEqual([h.term for h in ci.match_terms(index, "服务费 fee", "en")], ["fee"])
        self.assertEqual([h.term for h in ci.match_terms(index, "服务费 fee", "zh")], ["服务费"])

    def test_no_hit_and_bad_locale(self) -> None:
        index = self._index()
        self.assertEqual(ci.match_terms(index, "完全无关", "zh"), ())
        with self.assertRaises(ValueError):
            ci.match_terms(index, "x", "fr")


class TestExpand(FixtureMixin):
    def test_self_and_descendants(self) -> None:
        index = self._index()
        self.assertEqual(ci.expand(index, ["m:Mid"], ancestors=0), ("m:Mid", "m:Leaf"))

    def test_transitive_edge_connected(self) -> None:
        """transitive 边照常连通：m:Top 的后代含经跨级边到达的 m:Far。"""
        index = self._index()
        self.assertEqual(
            ci.expand(index, ["m:Top"], ancestors=0),
            ("m:Top", "m:Mid", "m:Leaf", "m:Far"),
        )

    def test_ancestors_one_layer_only(self) -> None:
        index = self._index()
        self.assertEqual(ci.expand(index, ["m:Mid"], ancestors=1), ("m:Top", "m:Mid", "m:Leaf"))
        # 一层祖先不含更高层（m:Leaf 的祖先是 m:Mid，不含 m:Top）
        self.assertEqual(ci.expand(index, ["m:Leaf"], ancestors=1), ("m:Mid", "m:Leaf"))
        self.assertEqual(ci.expand(index, ["m:Fee"], ancestors=1), ("m:Fee", "m:Amount"))

    def test_multiple_seeds_dedup_and_order(self) -> None:
        index = self._index()
        self.assertEqual(
            ci.expand(index, ["m:Mid", "m:Far"], ancestors=0),
            ("m:Mid", "m:Leaf", "m:Far"),
        )
        self.assertEqual(ci.expand(index, [], ancestors=0), ())

    def test_bad_inputs(self) -> None:
        index = self._index()
        with self.assertRaises(ValueError):
            ci.expand(index, ["m:Mid"], ancestors=2)
        with self.assertRaises(ValueError):
            ci.expand(index, ["m:Unknown"], ancestors=0)


class TestConceptTerms(FixtureMixin):
    def test_concept_order_file_order_dedup(self) -> None:
        index = self._index()
        # 两条同文本 "佣金"（via 不同）去重为一条；S2/S3 均进（源过滤归消费缝）
        self.assertEqual(ci.concept_terms(index, ["m:Fee"], "zh", ancestors=0), ("佣金", "服务费"))
        # m:Amount 的后代 m:Fee（索引序在自身之前）——后代术语并入后仍按概念序 + 文件序
        self.assertEqual(
            ci.concept_terms(index, ["m:Amount"], "zh", ancestors=0),
            ("佣金", "服务费", "交易额", "总交易额"),
        )

    def test_locale_filter(self) -> None:
        index = self._index()
        self.assertEqual(ci.concept_terms(index, ["m:Fee"], "en", ancestors=0), ("fee",))
        self.assertEqual(ci.concept_terms(index, ["m:P"], "en", ancestors=0), ())

    def test_ancestors_terms_included(self) -> None:
        index = self._index()
        self.assertEqual(
            ci.concept_terms(index, ["m:Mid"], "en", ancestors=1),
            ("top", "mid", "leaf"),
        )
        with self.assertRaises(ValueError):
            ci.concept_terms(index, ["m:Mid"], "fr", ancestors=0)


class TestDetectLocale(FixtureMixin):
    # 覆盖：纯中文 / 纯英文 / 中英混排 / 数字符号 / 空串 / 全角标点 / 日文假名 / 混合
    SAMPLES = (
        "总交易额",
        "total trade value",
        "2013 Q2 总交易额",
        "ABC 123",
        "",
        "（全角）",
        "こんにちは",
        "mixed 混合",
    )

    def test_matches_planner_heuristic(self) -> None:
        """与 Planner._resolve_locale 的 None（启发式）分支逐例一致——测试锁。"""
        for sample in self.SAMPLES:
            with self.subTest(sample=sample):
                self.assertEqual(ci.detect_locale(sample), Planner._resolve_locale(sample, None))

    def test_direct(self) -> None:
        self.assertEqual(ci.detect_locale("纯中文"), "zh")
        self.assertEqual(ci.detect_locale("plain"), "en")
        self.assertEqual(ci.detect_locale(""), "en")


if __name__ == "__main__":
    unittest.main()
