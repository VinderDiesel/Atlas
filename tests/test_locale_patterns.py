"""中文形态触发词词典契约测试（ADR-0015 §②，批次 B3a：纯搬运的机械锁定）。

口径：
- `load_locale_patterns` 与同义词加载器同一纪律——**封闭 locale 注册表 + 严格形态**：
  未注册 locale 拒、已注册但文件缺失拒、缺节/多节拒、模式串不可编译拒、词表含空词
  或重复拒、量级映射非整数拒（静默降级 = 某类问句在无人察觉时不再被解析）；
- `time.patterns` 的**声明顺序**与 kind ↔ 解析器实现的一致性必须锁定（顺序即语义：
  短模式先跑会截获长模式的输入，"2013 年 7 月" 会被读成年份）；
- planner 侧只做一条断言：形态常量的值**来自词典**（对象同一性），代码内不再有第二份
  字面量——这是"搬运而非重写"的可证伪形式。行为等价性由 tests/test_planner.py 的中英文
  用例（断言一字未改）与 `make eval --dry` 逐域比对共同锁定（设计页 §4）。

用 tmp 目录 + monkeypatch SYNONYMS_DIR 构造 fixture（不碰仓库真文件）。
"""

from __future__ import annotations

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

# 解析器已实现的时间形态 kind（与 planner._TIME_DISPATCH 一一对应）
TIME_KINDS = ("date", "iso_date", "quarter", "iso_quarter", "month", "year")
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
        self.assertIs(planner_mod._ISO_DATE_RE, by_kind["iso_date"])
        self.assertIs(planner_mod._ISO_QUARTER_RE, by_kind["iso_quarter"])

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


if __name__ == "__main__":
    unittest.main()
