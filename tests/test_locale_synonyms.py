"""agent/compiler.py::load_locale_synonyms 契约测试（ADR-0015 locale 词典加载器）。

口径：
- 加载器是**封闭注册表 + 严格形态**：未知 locale 拒、已注册但文件缺失拒、
  顶层多余键拒、值非"名称→字符串列表"拒、空措辞/重复措辞拒（宁可启动即失败，
  不静默降级出口径）；
- 声明顺序保留（解析器按顺序做最长命中，顺序影响歧义判定）；
- 同一 locale 只读一次文件（缓存）；
- 真实 en_us.yml 能加载且键均非空（内容契约由 tests/test_planner.py 锁）。

用 tmp 目录 + monkeypatch SYNONYMS_DIR 构造 fixture（不碰仓库真文件）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import agent.compiler as compiler_mod
from agent.compiler import load_locale_synonyms

REPO = Path(__file__).resolve().parent.parent


class LoaderTestCase(unittest.TestCase):
    """公共 setUp/tearDown：把词典目录切到 tmp，并清空加载缓存。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._saved_dir = compiler_mod.SYNONYMS_DIR
        self._saved_cache = dict(compiler_mod._LOCALE_CACHE)
        compiler_mod.SYNONYMS_DIR = self.dir
        compiler_mod._LOCALE_CACHE.clear()

    def tearDown(self) -> None:
        compiler_mod.SYNONYMS_DIR = self._saved_dir
        compiler_mod._LOCALE_CACHE.clear()
        compiler_mod._LOCALE_CACHE.update(self._saved_cache)
        self._tmp.cleanup()

    def _write(self, name: str, text: str) -> None:
        (self.dir / f"{name}.yml").write_text(text, encoding="utf-8")


class TestNormalLoad(LoaderTestCase):
    def test_sections_and_order(self) -> None:
        """两节都归一化为 tuple 且保序；缺省节 → 空映射。"""
        self._write(
            "en_us",
            "metric_synonyms:\n"
            "  gmv:\n"
            "    - gross merchandise value\n"
            "    - gmv\n"
            "    - sales\n",
        )
        loaded = load_locale_synonyms("en_us")
        self.assertEqual(
            loaded["metric_synonyms"]["gmv"],
            ("gross merchandise value", "gmv", "sales"),
        )
        self.assertEqual(loaded["dimension_synonyms"], {})

    def test_cached_single_read(self) -> None:
        """同一 locale 只读一次（第二次返回同一对象，不再访问文件系统）。"""
        self._write("en_us", "metric_synonyms: {gmv: [sales]}\n")
        first = load_locale_synonyms("en_us")
        (self.dir / "en_us.yml").unlink()  # 删文件后仍命中缓存 = 未重读
        self.assertIs(load_locale_synonyms("en_us"), first)

    def test_strips_whitespace(self) -> None:
        self._write("en_us", "metric_synonyms: {gmv: ['  sales  ']}")
        self.assertEqual(
            load_locale_synonyms("en_us")["metric_synonyms"]["gmv"], ("sales",)
        )


class TestRejections(LoaderTestCase):
    def test_unknown_locale_rejected(self) -> None:
        """locale 是封闭注册表：未注册名不拼接文件名（防路径注入/静默空表）。"""
        with self.assertRaises(ValueError) as ctx:
            load_locale_synonyms("../../etc/passwd")
        self.assertIn("未知 locale", str(ctx.exception))

    def test_registered_but_missing_file_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            load_locale_synonyms("zh_cn")
        self.assertIn("缺失", str(ctx.exception))

    def test_unknown_top_key_rejected(self) -> None:
        self._write("en_us", "metric_synonyms: {gmv: [sales]}\nverb: [show]\n")
        with self.assertRaises(ValueError) as ctx:
            load_locale_synonyms("en_us")
        self.assertIn("不支持的顶层键", str(ctx.exception))

    def test_bad_shapes_rejected(self) -> None:
        bad_docs = {
            "top-level-list": "- a\n- b\n",
            "section-not-map": "metric_synonyms: [gmv]\n",
            "value-not-list": "metric_synonyms: {gmv: sales}\n",
            "empty-word": "metric_synonyms: {gmv: [sales, '  ']}\n",
            "duplicate-word": "metric_synonyms: {gmv: [sales, sales]}\n",
        }
        for label, text in bad_docs.items():
            with self.subTest(case=label):
                compiler_mod._LOCALE_CACHE.clear()
                self._write("en_us", text)
                with self.assertRaises(ValueError):
                    load_locale_synonyms("en_us")


class TestRealRepoFiles(unittest.TestCase):
    def test_registered_locales_load(self) -> None:
        """仓库内两个已注册 locale 均可加载；en 表非空、zh 表恒空。"""
        en = load_locale_synonyms("en_us")
        self.assertTrue(en["metric_synonyms"])
        zh = load_locale_synonyms("zh_cn")
        self.assertEqual(zh, {"metric_synonyms": {}, "dimension_synonyms": {}})

    def test_registry_matches_files_on_disk(self) -> None:
        """注册表与实际文件一一对应（新增 YAML 不注册 = 加载不到，反之缺文件报错）。"""
        files = {p.name for p in compiler_mod.SYNONYMS_DIR.glob("*.yml")}
        self.assertEqual(files, set(compiler_mod._LOCALE_FILES.values()))


if __name__ == "__main__":
    unittest.main()
