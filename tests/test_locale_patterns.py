"""locale 形态触发词词典契约测试（ADR-0015 §②，批次 B3a 中文 / B3b 英文）。

口径：
- `load_locale_patterns` 与同义词加载器同一纪律——**封闭 locale 注册表 + 严格形态**：
  未注册 locale 拒、已注册但文件缺失拒、缺节/多节拒、模式串不可编译拒、词表含空词
  或重复拒、量级映射非整数拒（静默降级 = 某类问句在无人察觉时不再被解析）；
- `time.patterns` 的**声明顺序**与 kind ↔ 解析器实现的一致性必须锁定（顺序即语义：
  短模式先跑会截获长模式的输入，"2013 年 7 月" 会被读成年份）；
- 英文词典的两处新增机制同样受约束：`ref` 只能指向上游已声明的 kind 且解析为
  **同一已编译对象**（ISO 形态权威源唯一）、`flags` 只能配 pattern 且名字在封闭
  白名单内、`months` 表与 month 模式的交替串必须同源（双写漂移在加载期就报错）；
- planner 侧只做一条断言：形态常量的值**来自词典**（对象同一性），代码内不再有第二份
  字面量——这是"搬运而非重写"的可证伪形式。行为等价性由 tests/test_planner.py 的中英文
  用例（断言一字未改）与 `make eval --dry` 逐域比对共同锁定（设计页 §4）。

用 tmp 目录 + monkeypatch SYNONYMS_DIR 构造 fixture（不碰仓库真文件）。
"""

from __future__ import annotations

import itertools
import re
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

import agent.compiler as compiler_mod
import agent.planner as planner_mod
from agent.compiler import SemanticModel, TimeSpec, load_locale_patterns
from agent.planner import ClarificationRequest

# 解析器已实现的时间形态 kind（与 planner._TIME_DISPATCH / _EN_TIME_DISPATCH 一一对应）
TIME_KINDS = ("date", "iso_date", "quarter", "iso_quarter", "month", "year")
EN_TIME_KINDS = ("iso_date", "quarter", "iso_quarter", "month", "year_prep", "year_bare")
SECTIONS = (
    "time",
    "grouping",
    "topn",
    "filter_include",
    "filter_exclude",
    "threshold",
    "magnitude",
    "followup",
)
EN_SECTIONS = (
    "time",
    "months",
    "grouping",
    "topn",
    "topn_dim",
    "filter_include",
    "filter_exclude",
    "threshold",
    "magnitude",
    "followup",
)
MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _valid_doc() -> dict[str, Any]:
    """与 patterns_zh_cn.yml 同构的最小合法文档（只用于校验加载器形态，非仓库口径）。"""
    return {
        "time": {
            "relative_reject": {"words": ["上个月", "去年"]},
            "patterns": [
                {"kind": "date", "pattern": r"(\d{4}) 年 (\d{1,2}) 月 (\d{1,2}) 日"},
                {"kind": "iso_date", "pattern": r"(\d{4})-(\d{2})-(\d{2})"},
                {"kind": "quarter", "pattern": r"(\d{4}) 年?第?([一二三四])季度"},
                {"kind": "iso_quarter", "pattern": r"(\d{4})\s*[Qq]([1-4])"},
                {"kind": "month", "pattern": r"(\d{4}) 年 (\d{1,2}) 月"},
                {"kind": "year", "pattern": r"(\d{4}) 年"},
            ],
        },
        "grouping": {"pattern": r"按(.+?)(?:统计|分组)"},
        "topn": {"pattern": r"前\s*(\d+)\s*名"},
        "filter_include": {"pattern": r"(?:只看|仅统计)\s*(.+?)(?=的|，|$)"},
        "filter_exclude": {"pattern": r"(?:排除|不含)\s*(.+?)(?=的|，|$)"},
        "threshold": {
            "greater": {"pattern": r"(?:超过|大于)\s*([\d.]+)\s*(亿|千万|百万|万)?"},
            "less": {"pattern": r"(?:低于|不足)\s*([\d.]+)\s*(亿|千万|百万|万)?"},
        },
        "magnitude": {
            "cn_units": {"亿": 100_000_000, "千万": 10_000_000, "万": 10_000},
            "cn_numerals": {"一": 1, "二": 2, "三": 3, "四": 4},
        },
        "followup": {
            "prefixes": ["那", "那么", "换成"],
            "dim_pattern": r"(?:那|那么)?(?:换成|按)\s*(.+?)(?:统计|分组)?\s*呢?\s*$",
        },
    }


def _valid_en_doc() -> dict[str, Any]:
    """与 patterns_en_us.yml 同构的最小合法文档（只用于校验加载器形态，非仓库口径）。"""
    return {
        "time": {
            "relative_reject": {"words": ["last month", "recent"]},
            "patterns": [
                {"kind": "iso_date", "ref": "iso_date"},
                {"kind": "quarter", "pattern": r"(?:([Qq][1-4])\s+(\d{4})|(\d{4})\s+([Qq][1-4]))"},
                {"kind": "iso_quarter", "ref": "iso_quarter"},
                {
                    "kind": "month",
                    "pattern": r"\b(" + "|".join(MONTH_NAMES) + r")\s*,?\s+(\d{4})\b",
                    "flags": ["IGNORECASE"],
                },
                {"kind": "year_prep", "pattern": r"\b(?:in|for|during|of|from)\s+(\d{4})\b"},
                {"kind": "year_bare", "pattern": r"\b(\d{4})\b"},
            ],
            "threshold_gate": {"words": ["over", "more than"]},
        },
        "months": {name.lower(): i + 1 for i, name in enumerate(MONTH_NAMES)},
        "grouping": {"pattern": r"\b(?:grouped by|by)\s+(.+?)(?=\s+\bin\b|$)"},
        "topn": {"pattern": r"\b(?:top|best)\s+(\d+)\b"},
        "topn_dim": {"pattern": r"\b(?:top|best)\s+\d+\s+(.+?)(?=\s+\bby\b|$)"},
        "filter_include": {"pattern": r"\bonly\s+(.+?)(?=\s+\bin\b|$)"},
        "filter_exclude": {"pattern": r"\b(?:excluding|except)\s+(.+?)(?=\s+\bin\b|$)"},
        "threshold": {
            "greater": {"pattern": r"\b(?:over|above)\s+([\d.]+)\s*(million|[KMB])?"},
            "less": {"pattern": r"\b(?:under|below)\s+([\d.]+)\s*(million|[KMB])?"},
        },
        "magnitude": {"en_units": {"billion": 1_000_000_000, "million": 1_000_000, "K": 1_000}},
        "followup": {
            "what": {"pattern": r"(?:what|how)\s+about\s+(.+?)\??\s*$"},
            "instead": {"pattern": r"(.+?)\s+instead\??\s*$"},
        },
    }


class LoaderTestCase(unittest.TestCase):
    """公共 setUp/tearDown：把词典目录切到 tmp，并清空形态词典缓存。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._saved_dir = compiler_mod.SYNONYMS_DIR
        self._saved_cache = dict(compiler_mod._PATTERNS_CACHE)
        compiler_mod.SYNONYMS_DIR = self.dir
        compiler_mod._PATTERNS_CACHE.clear()

    def tearDown(self) -> None:
        compiler_mod.SYNONYMS_DIR = self._saved_dir
        compiler_mod._PATTERNS_CACHE.clear()
        compiler_mod._PATTERNS_CACHE.update(self._saved_cache)
        self._tmp.cleanup()

    def _write(self, doc: dict[str, Any]) -> Path:
        path = self.dir / "patterns_zh_cn.yml"
        path.write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return path

    def _reject(self, mutate: Callable[[dict[str, Any]], None], label: str) -> None:
        """合法文档改一处 → 加载必须报 ValueError（带节名定位）。"""
        doc = _valid_doc()
        mutate(doc)
        self._write(doc)
        compiler_mod._PATTERNS_CACHE.clear()
        with self.subTest(case=label), self.assertRaises(ValueError):
            load_locale_patterns("zh_cn")

    def _write_en(self, doc: dict[str, Any]) -> Path:
        """写英文词典，并补一份合法中文词典（en 的 `ref` 需上游 zh 词典在场）。"""
        self._write(_valid_doc())
        path = self.dir / "patterns_en_us.yml"
        path.write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return path

    def _reject_en(
        self, mutate: Callable[[dict[str, Any]], None], label: str
    ) -> None:
        doc = _valid_en_doc()
        mutate(doc)
        self._write_en(doc)
        compiler_mod._PATTERNS_CACHE.clear()
        with self.subTest(case=label), self.assertRaises(ValueError):
            load_locale_patterns("en_us")


class TestNormalLoad(LoaderTestCase):
    def test_sections_shape_and_types(self) -> None:
        """各节齐备：模式串 → 已编译正则，词表 → 元组，量级 → dict[str, int]。"""
        self._write(_valid_doc())
        loaded = load_locale_patterns("zh_cn")
        self.assertEqual(tuple(loaded), SECTIONS)
        self.assertEqual(loaded["time"]["relative_reject"]["words"], ("上个月", "去年"))
        self.assertIsInstance(loaded["grouping"]["pattern"], re.Pattern)
        self.assertEqual(
            loaded["threshold"]["greater"]["pattern"].pattern,
            r"(?:超过|大于)\s*([\d.]+)\s*(亿|千万|百万|万)?",
        )
        self.assertEqual(loaded["magnitude"]["cn_units"]["千万"], 10_000_000)
        self.assertEqual(loaded["magnitude"]["cn_numerals"]["三"], 3)
        self.assertEqual(loaded["followup"]["prefixes"], ("那", "那么", "换成"))

    def test_time_patterns_order_preserved(self) -> None:
        """time.patterns 是**有序**列表——顺序即语义，加载器不得排序或去序。"""
        self._write(_valid_doc())
        self.assertEqual(
            tuple(kind for kind, _ in load_locale_patterns("zh_cn")["time"]["patterns"]),
            TIME_KINDS,
        )
        doc = _valid_doc()
        doc["time"]["patterns"] = list(reversed(doc["time"]["patterns"]))
        self._write(doc)
        compiler_mod._PATTERNS_CACHE.clear()
        self.assertEqual(
            tuple(kind for kind, _ in load_locale_patterns("zh_cn")["time"]["patterns"]),
            tuple(reversed(TIME_KINDS)),
        )

    def test_loaded_once_per_locale(self) -> None:
        """同一 locale 只读一次文件（缓存）；文件随后消失仍能取到。"""
        self._write(_valid_doc())
        first = load_locale_patterns("zh_cn")
        (self.dir / "patterns_zh_cn.yml").unlink()
        self.assertIs(first, load_locale_patterns("zh_cn"))

    def test_en_sections_shape_and_order(self) -> None:
        """英文词典：节齐与 time.patterns 顺序保留（顺序即语义，与中文同纪律）。"""
        self._write_en(_valid_en_doc())
        loaded = load_locale_patterns("en_us")
        self.assertEqual(tuple(loaded), EN_SECTIONS)
        self.assertEqual(
            tuple(kind for kind, _ in loaded["time"]["patterns"]), EN_TIME_KINDS
        )
        self.assertEqual(
            loaded["time"]["threshold_gate"]["words"], ("over", "more than")
        )
        self.assertEqual(loaded["months"]["may"], 5)
        self.assertEqual(loaded["magnitude"]["en_units"]["K"], 1_000)
        self.assertEqual(
            loaded["followup"]["instead"]["pattern"].pattern, r"(.+?)\s+instead\??\s*$"
        )

    def test_en_ref_reuses_zh_object(self) -> None:
        """`ref` 解析为上游的**同一已编译对象**（ISO 形态不存在第二份权威源）。"""
        self._write_en(_valid_en_doc())
        zh = dict(load_locale_patterns("zh_cn")["time"]["patterns"])
        en = dict(load_locale_patterns("en_us")["time"]["patterns"])
        self.assertIs(en["iso_date"], zh["iso_date"])
        self.assertIs(en["iso_quarter"], zh["iso_quarter"])

    def test_en_flags_are_declared_not_rebuilt(self) -> None:
        """flags 只作为编译参数参入：模式串逐字保持，未因 flag 而被改写。"""
        self._write_en(_valid_en_doc())
        month = dict(load_locale_patterns("en_us")["time"]["patterns"])["month"]
        self.assertEqual(
            month.pattern, r"\b(" + "|".join(MONTH_NAMES) + r")\s*,?\s+(\d{4})\b"
        )
        self.assertEqual(month.flags & ~re.UNICODE, re.IGNORECASE)
        self.assertIsNotNone(month.search("may 2014"))


class TestLoaderRejects(LoaderTestCase):
    """一切形态偏差都在加载期报错——不允许静默丢掉某类形态的触发词。"""

    def test_missing_section(self) -> None:
        for section in SECTIONS:
            self._reject(lambda doc, s=section: doc.pop(s), f"缺节 {section}")

    def test_unknown_section(self) -> None:
        self._reject(lambda doc: doc.update({"order_by": {"pattern": "x"}}), "多余顶层节")

    def test_bad_regex(self) -> None:
        self._reject(
            lambda doc: doc["grouping"].update({"pattern": "按(.+?"}), "正则不可编译"
        )

    def test_pattern_not_string(self) -> None:
        self._reject(lambda doc: doc.update({"topn": {"pattern": 5}}), "模式串非字符串")
        self._reject(lambda doc: doc.update({"topn": {}}), "缺 pattern 键")
        self._reject(
            lambda doc: doc.update({"topn": {"pattern": "", "flags": "i"}}),
            "空模式串 + 多余键",
        )

    def test_nested_section_shape(self) -> None:
        self._reject(
            lambda doc: doc["threshold"]["greater"].update({"flag": "i"}),
            "threshold.greater 多余键",
        )
        self._reject(
            lambda doc: doc["followup"].update({"time_pattern": "x"}),
            "followup 多余键",
        )
        self._reject(
            lambda doc: doc["threshold"].pop("less"), "threshold 缺 less"
        )
        self._reject(
            lambda doc: doc["time"].pop("relative_reject"), "time 缺 relative_reject"
        )
        self._reject(
            lambda doc: doc["time"]["relative_reject"].update({"extra": 1}),
            "relative_reject 多余键",
        )

    def test_time_patterns_shape(self) -> None:
        self._reject(lambda doc: doc["time"].update({"patterns": []}), "空 patterns")
        self._reject(
            lambda doc: doc["time"].update({"patterns": ["date"]}), "项不是映射"
        )
        self._reject(
            lambda doc: doc["time"]["patterns"][0].pop("pattern"), "项缺 pattern 键"
        )
        self._reject(
            lambda doc: doc["time"]["patterns"].append(
                {"kind": "date", "pattern": r"(\d{4}) 年"}
            ),
            "kind 重复",
        )
        self._reject(
            lambda doc: doc["time"]["patterns"].append(
                {"kind": " week", "pattern": "x"}
            ),
            "kind 含首尾空白",
        )

    def test_word_list_rules(self) -> None:
        self._reject(
            lambda doc: doc["time"]["relative_reject"].update({"words": []}), "空词表"
        )
        self._reject(
            lambda doc: doc["time"]["relative_reject"].update(
                {"words": ["去年", "去年"]}
            ),
            "词条重复",
        )
        self._reject(
            lambda doc: doc["followup"].update({"prefixes": ["那 ", "换成"]}),
            "词含首尾空白",
        )
        self._reject(
            lambda doc: doc["followup"].update({"prefixes": "那"}), "词表不是列表"
        )

    def test_magnitude_rules(self) -> None:
        self._reject(
            lambda doc: doc["magnitude"].update({"cn_units": {"亿": "100000000"}}),
            "倍率非整数",
        )
        self._reject(
            lambda doc: doc["magnitude"].update({"cn_numerals": {"一": True}}),
            "编号映射值为 bool",
        )
        self._reject(lambda doc: doc["magnitude"].pop("cn_numerals"), "magnitude 缺子键")

    def test_unknown_locale(self) -> None:
        with self.assertRaises(ValueError):
            load_locale_patterns("fr_fr")

    def test_registered_but_missing_file(self) -> None:
        """已注册 locale 的文件缺失 = 配置缺失 → 报错（不降级为"该 locale 无形态词"）。"""
        with self.assertRaises(ValueError):
            load_locale_patterns("zh_cn")

    def test_en_missing_and_unknown_section(self) -> None:
        for section in EN_SECTIONS:
            self._reject_en(lambda doc, s=section: doc.pop(s), f"en 缺节 {section}")
        self._reject_en(
            lambda doc: doc.update({"months_alias": {"x": 1}}), "en 多余顶层节"
        )

    def test_en_time_section_shape(self) -> None:
        self._reject_en(
            lambda doc: doc["time"].pop("threshold_gate"), "en time 缺 threshold_gate"
        )
        self._reject_en(
            lambda doc: doc["time"]["threshold_gate"].update({"extra": 1}),
            "threshold_gate 多余键",
        )

    def test_en_ref_rules(self) -> None:
        """ref 只能指向上游已声明 kind，且不携带 pattern（不允许"抄一份再用 ref 校对"）。"""
        self._reject_en(
            lambda doc: doc["time"]["patterns"][0].update({"ref": "week"}),
            "ref 指向不存在的 kind",
        )
        self._reject_en(
            lambda doc: doc["time"]["patterns"][0].update(
                {"pattern": r"(\d{4})/(\d{2})/(\d{2})"}
            ),
            "ref 与 pattern 共存",
        )
        self._reject_en(
            lambda doc: doc["time"]["patterns"][0].pop("ref"), "ref 条目缺 ref"
        )

    def test_en_flags_rules(self) -> None:
        self._reject_en(
            lambda doc: doc["time"]["patterns"][0].update({"flags": ["IGNORECASE"]}),
            "flags 无 pattern（配 ref）",
        )
        self._reject_en(
            lambda doc: doc["time"]["patterns"][3].update({"flags": ["LCASE"]}),
            "flag 名不在白名单",
        )
        self._reject_en(
            lambda doc: doc["time"]["patterns"][3].update({"flags": []}),
            "flag 空列表",
        )
        self._reject_en(
            lambda doc: doc["time"]["patterns"][3].update(
                {"flags": ["IGNORECASE", "IGNORECASE"]}
            ),
            "flag 重复",
        )

    def test_en_months_rules(self) -> None:
        """months 表里的名字必须能被 month 模式命中（改了模式没改表 → 加载期报错）。

        反方向（交替串里的名字都得在表里）靠 test_months_table_matches_pattern_alternation
        逐名逐量锁定：交替串是正则的一部分，加载器不解析正则、不重组模式。
        """
        self._reject_en(
            lambda doc: doc["months"].update({"May": 5}), "months 键非小写"
        )
        self._reject_en(
            lambda doc: doc["months"].update({"yanuary": 1}), "months 键错拼（模式不命中）"
        )
        self._reject_en(
            lambda doc: doc["months"].update({"may": "5"}), "months 值非整数"
        )
        self._reject_en(
            lambda doc: doc["time"]["patterns"][3].update(
                {"pattern": r"\b(May|June)\s*,?\s+(\d{4})\b", "flags": ["IGNORECASE"]}
            ),
            "months 含模式命中不到的名字",
        )
        self._reject_en(
            lambda doc: doc["time"]["patterns"].pop(3), "缺 kind: month 形态"
        )

    def test_zh_lexicon_rejects_ref_and_flags(self) -> None:
        """ref/flags 是英文词典专有能力：中文词典用了依旧报错（不把未验证能力摊开）。"""
        self._reject(
            lambda doc: doc["time"]["patterns"][0].update({"flags": ["IGNORECASE"]}),
            "zh 条目带 flags",
        )
        self._reject(
            lambda doc: doc["time"]["patterns"][1].update({"ref": "iso_date"}),
            "zh 条目用 ref",
        )


class TestRealRepoLexicon(unittest.TestCase):
    """仓库真实词典：结构基线 + planner 接线（值来自词典，代码无第二份）。"""

    def test_lexicon_content_baseline(self) -> None:
        """词表与量级映射的内容基线（改动即语义变化，须单独评审，不随搬运批混入）。"""
        loaded = load_locale_patterns("zh_cn")
        self.assertEqual(
            tuple(loaded["time"]["relative_reject"]["words"]),
            (
                "上个月",
                "上季度",
                "上月",
                "上旬",
                "去年",
                "今年",
                "最近",
                "本月",
                "本周",
                "昨天",
                "今天",
                "近",
            ),
        )
        self.assertEqual(
            tuple(loaded["followup"]["prefixes"]),
            ("那", "那么", "换成", "改成", "改为", "按"),
        )
        self.assertEqual(
            loaded["magnitude"]["cn_units"],
            {"亿": 100_000_000, "千万": 10_000_000, "百万": 1_000_000, "万": 10_000},
        )
        self.assertEqual(
            loaded["magnitude"]["cn_numerals"], {"一": 1, "二": 2, "三": 3, "四": 4}
        )

    def test_time_kinds_match_parser_implementation(self) -> None:
        """词典 kind 集合 == planner 分发表（新增形态要两侧同时落，否则导入即失败）。"""
        patterns = load_locale_patterns("zh_cn")["time"]["patterns"]
        self.assertEqual(tuple(kind for kind, _ in patterns), TIME_KINDS)
        self.assertEqual(set(planner_mod._TIME_DISPATCH), {kind for kind, _ in patterns})

    def test_planner_constants_come_from_lexicon(self) -> None:
        """planner 形态常量与词典对象同一——B3a"纯搬运"的可证伪断言。"""
        lex = load_locale_patterns("zh_cn")
        self.assertIs(planner_mod._TIME_PATTERNS, lex["time"]["patterns"])
        self.assertIs(
            planner_mod._RELATIVE_TIME, lex["time"]["relative_reject"]["words"]
        )
        self.assertIs(planner_mod._GROUP_RE, lex["grouping"]["pattern"])
        self.assertIs(planner_mod._TOP_N_RE, lex["topn"]["pattern"])
        self.assertIs(planner_mod._EQ_FILTER_RE, lex["filter_include"]["pattern"])
        self.assertIs(planner_mod._EXC_FILTER_RE, lex["filter_exclude"]["pattern"])
        self.assertIs(
            planner_mod._THRESHOLD_GT_RE, lex["threshold"]["greater"]["pattern"]
        )
        self.assertIs(
            planner_mod._THRESHOLD_LT_RE, lex["threshold"]["less"]["pattern"]
        )
        self.assertIs(planner_mod._CN_UNIT, lex["magnitude"]["cn_units"])
        self.assertIs(planner_mod._CN_NUM, lex["magnitude"]["cn_numerals"])
        self.assertIs(planner_mod._FOLLOWUP_PREFIXES, lex["followup"]["prefixes"])
        self.assertIs(planner_mod._FOLLOWUP_DIM_RE, lex["followup"]["dim_pattern"])
        # ISO 形态中英共享，同源于 zh 词典（英文侧不复制第二份，避免双权威源）
        by_kind = dict(lex["time"]["patterns"])
        self.assertIs(
            dict(planner_mod._TIME_PATTERNS)["iso_date"], by_kind["iso_date"]
        )

    def test_lexicon_content_baseline_en(self) -> None:
        """英文侧词表/映射的内容基线（同中文：改动即语义变化，需单独评审）。"""
        lex = load_locale_patterns("en_us")
        self.assertEqual(
            tuple(lex["time"]["relative_reject"]["words"]),
            (
                "last month",
                "last quarter",
                "last year",
                "last week",
                "this month",
                "this quarter",
                "this year",
                "this week",
                "recent",
                "yesterday",
                "today",
            ),
        )
        self.assertEqual(
            tuple(lex["time"]["threshold_gate"]["words"]),
            (
                "over",
                "above",
                "more than",
                "greater than",
                "exceeding",
                "under",
                "below",
                "less than",
                "fewer than",
            ),
        )
        self.assertEqual(
            lex["months"], {name.lower(): i + 1 for i, name in enumerate(MONTH_NAMES)}
        )
        self.assertEqual(
            lex["magnitude"]["en_units"],
            {
                "billion": 1_000_000_000,
                "million": 1_000_000,
                "thousand": 1_000,
                "B": 1_000_000_000,
                "M": 1_000_000,
                "K": 1_000,
            },
        )

    def test_time_kinds_match_parser_implementation_en(self) -> None:
        """英文词典 kind 集合 == planner 英文分发表（与中文同纪律）。"""
        patterns = load_locale_patterns("en_us")["time"]["patterns"]
        self.assertEqual(tuple(kind for kind, _ in patterns), EN_TIME_KINDS)
        self.assertEqual(
            set(planner_mod._EN_TIME_DISPATCH), {kind for kind, _ in patterns}
        )

    def test_planner_en_constants_come_from_lexicon(self) -> None:
        """planner 英文形态常量与词典对象同一——B3b"纯搬运"的可证伪断言。"""
        lex = load_locale_patterns("en_us")
        self.assertIs(planner_mod._EN_TIME_PATTERNS, lex["time"]["patterns"])
        self.assertIs(
            planner_mod._EN_RELATIVE_TIME, lex["time"]["relative_reject"]["words"]
        )
        self.assertIs(
            planner_mod._EN_THRESHOLD_WORDS, lex["time"]["threshold_gate"]["words"]
        )
        self.assertIs(planner_mod._EN_MONTH_NUM, lex["months"])
        self.assertIs(planner_mod._EN_GROUP_RE, lex["grouping"]["pattern"])
        self.assertIs(planner_mod._EN_TOP_N_RE, lex["topn"]["pattern"])
        self.assertIs(planner_mod._EN_TOP_N_DIM_RE, lex["topn_dim"]["pattern"])
        self.assertIs(planner_mod._EN_ONLY_RE, lex["filter_include"]["pattern"])
        self.assertIs(planner_mod._EN_EXCL_RE, lex["filter_exclude"]["pattern"])
        self.assertIs(
            planner_mod._EN_THRESHOLD_GT_RE, lex["threshold"]["greater"]["pattern"]
        )
        self.assertIs(
            planner_mod._EN_THRESHOLD_LT_RE, lex["threshold"]["less"]["pattern"]
        )
        self.assertIs(planner_mod._EN_UNIT, lex["magnitude"]["en_units"])
        self.assertIs(
            planner_mod._EN_FOLLOWUP_WHAT_RE, lex["followup"]["what"]["pattern"]
        )
        self.assertIs(
            planner_mod._EN_FOLLOWUP_INSTEAD_RE, lex["followup"]["instead"]["pattern"]
        )
        # ISO 形态经 ref 同源于 zh 词典（planner 的英文表里就是中文词典那个对象）
        zh = dict(load_locale_patterns("zh_cn")["time"]["patterns"])
        en = dict(planner_mod._EN_TIME_PATTERNS)
        self.assertIs(en["iso_date"], zh["iso_date"])
        self.assertIs(en["iso_quarter"], zh["iso_quarter"])

    def test_months_table_matches_pattern_alternation(self) -> None:
        """交叉不变量（设计页 #3/#4）：month 模式的交替串 == months 表键。"""
        lex = load_locale_patterns("en_us")
        pattern = dict(lex["time"]["patterns"])["month"].pattern
        names = pattern.split("(")[1].split(")")[0].split("|")
        self.assertEqual(len(names), len(lex["months"]))
        self.assertEqual({name.lower() for name in names}, set(lex["months"]))

    def test_month_names_are_prefix_free(self) -> None:
        """交替串顺序无关的前提：月份名互不为前缀（前提破了必须显式改设计）。"""
        names = list(load_locale_patterns("en_us")["months"])
        for first, second in itertools.permutations(names, 2):
            with self.subTest(prefix=first, other=second):
                self.assertFalse(second.startswith(first))

    def test_registry_matches_files_on_disk(self) -> None:
        """patterns_*.yml 与 patterns 注册表一一对应（B3b 新增 en 词典须同步注册）。"""
        files = {p.name for p in compiler_mod.SYNONYMS_DIR.glob("patterns_*.yml")}
        self.assertEqual(files, set(compiler_mod._PATTERN_FILES.values()))


class TestTimeFormsResolve(unittest.TestCase):
    """时间形态 6 kind 各有用例（设计页第 1 条的"新增用例"，不改既有断言）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.planner = planner_mod.Planner(SemanticModel())

    def _time(self, question: str) -> Any:
        return self.planner._parse_time(question, "zh")

    def test_each_kind(self) -> None:
        cases = {
            "2013 年 7 月 5 日的成交金额": ("date", "2013-07-05"),
            "2013-07-05 的成交金额": ("date", "2013-07-05"),
            "2013 年第三季度的成交金额": ("quarter", "2013Q3"),
            "2013Q2 的成交金额": ("quarter", "2013Q2"),
            "2013 Q2 的成交金额": ("quarter", "2013Q2"),
            "2013 年 7 月的成交金额": ("month", 201307),
            "2013 年的成交金额": ("year", 2013),
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                spec = self._time(question)
                self.assertIsInstance(spec, TimeSpec)
                assert isinstance(spec, TimeSpec)
                self.assertEqual((spec.granularity, spec.value), expected)

    def test_order_sensitivity(self) -> None:
        """顺序被破坏时在此暴露：年/月形态不得截获 date 与 quarter 的输入。"""
        self.assertEqual(self._time("2013 年 7 月 5 日").granularity, "date")
        self.assertEqual(self._time("2013 年 7 月").value, 201307)
        self.assertEqual(self._time("2013 年").value, 2013)

    def test_relative_time_rejected(self) -> None:
        spec = self._time("上个月的成交金额")
        self.assertIsInstance(spec, ClarificationRequest)
        assert isinstance(spec, ClarificationRequest)
        self.assertEqual(spec.kind, "relative_time")

    def test_no_time_form(self) -> None:
        self.assertIsNone(self._time("总交易额是多少"))


class TestEnglishTimeFormsResolve(unittest.TestCase):
    """英文时间形态 6 kind 各有用例（B3b 新增，不改既有断言）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.planner = planner_mod.Planner(SemanticModel())

    def _time(self, question: str) -> Any:
        return self.planner._parse_time(question, "en")

    def test_each_kind(self) -> None:
        cases = {
            "total commission on 2013-07-05": ("date", "2013-07-05"),
            "total commission in Q2 2013": ("quarter", "2013Q2"),
            "total commission in 2013 Q2": ("quarter", "2013Q2"),
            "total commission in 2013Q2": ("quarter", "2013Q2"),
            "total commission in May 2014": ("month", 201405),
            "total commission in may 2014": ("month", 201405),
            "total commission in December, 2014": ("month", 201412),
            "total commission for 2013": ("year", 2013),
            "total commission 2013": ("year", 2013),
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                spec = self._time(question)
                self.assertIsInstance(spec, TimeSpec)
                assert isinstance(spec, TimeSpec)
                self.assertEqual((spec.granularity, spec.value), expected)

    def test_bare_year_gated_by_threshold_words(self) -> None:
        """裸年兜底封锁表：句中含阈值词时数字不得被读成年份。"""
        for question in (
            "accounts with over 5000 trades",
            "accounts with more than 5000 trades",
        ):
            with self.subTest(question=question):
                self.assertIsNone(self._time(question))

    def test_order_sensitivity(self) -> None:
        """月份名形态不得被裸年兜底截获（顺序被破坏时在此暴露）。"""
        self.assertEqual(self._time("total commission in May 2014").granularity, "month")
        self.assertEqual(self._time("total commission in Q2 2013").granularity, "quarter")

    def test_relative_time_rejected(self) -> None:
        spec = self._time("total commission last quarter")
        self.assertIsInstance(spec, ClarificationRequest)
        assert isinstance(spec, ClarificationRequest)
        self.assertEqual(spec.kind, "relative_time")

    def test_no_time_form(self) -> None:
        self.assertIsNone(self._time("total commission by branch"))


if __name__ == "__main__":
    unittest.main()
