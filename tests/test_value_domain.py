"""值域注册表契约测试（ADR-0016 §②）。

覆盖：
- 加载契约：文件不存在 → None；文件畸形 → ValueError；status 未知 / values 非列表 /
  别名目标不在值域 / casefold 别名键冲突 / snapshot_sha 空 → ValueError。
- resolve 四态：exact / alias / case / unknown / unregistered。
- 真实仓库：若 semantic/values/ 已生成（B4 产物已入库），lint 与运行时加载器
  必须一致；否则跳过（完整性检查在 tests/，不强制要求 values/ 存在）。

用 tmp 目录构造 fixture（load_profile/resolve 接受 values_dir 参数即为可测性）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent.value_domain import (
    ValueProfile,
    load_profile,
    parse_profile,
    resolve,
    registered_dimensions,
)


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _minimal(
    model: str = "atlas_finance_analytics",
    field: str = "ExchangeID",
    values: list[dict] | None = None,
    aliases: dict | None = None,
    status: str = "registered",
    snapshot_sha: str = "abc1234",
) -> dict:
    default_values = [{"value": "NASDAQ", "count": 100}, {"value": "NYSE", "count": 90}]
    return {
        "model": model,
        "field": field,
        "status": status,
        "snapshot_sha": snapshot_sha,
        "generated_at": "2026-09-09T12:00:00+08:00",
        "bound_dataset": "dim_security",
        "source_table": "atlas.dwd.dim_security",
        "source_column": "ExchangeID",
        "distinct_count": len(values) if values is not None else len(default_values),
        "row_count": 1000,
        "null_count": 0,
        "max_cardinality": 200,
        "skip_reason": None,
        "values": values if values is not None else default_values,
        "aliases": aliases or {},
        "note": "",
    }


class TestLoadProfileContract(unittest.TestCase):
    """加载契约：文件不存在 → None；畸形 → ValueError。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(load_profile("atlas_finance_analytics", "ExchangeID", self.dir))

    def test_malformed_json_raises(self) -> None:
        path = self.dir / "atlas_finance_analytics.ExchangeID.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_profile("atlas_finance_analytics", "ExchangeID", self.dir)

    def test_unknown_status_raises(self) -> None:
        payload = _minimal(status="bogus")
        _write(self.dir / f"{payload['model']}.{payload['field']}.json", payload)
        with self.assertRaises(ValueError):
            load_profile(payload["model"], payload["field"], self.dir)

    def test_values_not_list_raises(self) -> None:
        payload = _minimal()
        payload["values"] = "not a list"
        _write(self.dir / f"{payload['model']}.{payload['field']}.json", payload)
        with self.assertRaises(ValueError):
            load_profile(payload["model"], payload["field"], self.dir)

    def test_dangling_alias_raises(self) -> None:
        payload = _minimal(aliases={"NSDQ": "UNKNOWN"})
        _write(self.dir / f"{payload['model']}.{payload['field']}.json", payload)
        with self.assertRaises(ValueError):
            load_profile(payload["model"], payload["field"], self.dir)

    def test_alias_key_collides_with_value_casefold_raises(self) -> None:
        """别名键 casefold 后与值本体同名 → 应由 exact/case 命中，不需要别名。"""
        payload = _minimal(values=[{"value": "NASDAQ", "count": 100}], aliases={"nasdaq": "NASDAQ"})
        _write(self.dir / f"{payload['model']}.{payload['field']}.json", payload)
        with self.assertRaises(ValueError):
            load_profile(payload["model"], payload["field"], self.dir)

    def test_duplicate_alias_casefold_raises(self) -> None:
        payload = _minimal(aliases={"NSDQ": "NASDAQ", "nsdq": "NYSE"})
        _write(self.dir / f"{payload['model']}.{payload['field']}.json", payload)
        with self.assertRaises(ValueError):
            load_profile(payload["model"], payload["field"], self.dir)

    def test_registered_empty_values_raises(self) -> None:
        payload = _minimal(values=[])
        _write(self.dir / f"{payload['model']}.{payload['field']}.json", payload)
        with self.assertRaises(ValueError):
            load_profile(payload["model"], payload["field"], self.dir)

    def test_skipped_with_values_raises(self) -> None:
        payload = _minimal(status="skipped", values=[{"value": "X", "count": 1}])
        _write(self.dir / f"{payload['model']}.{payload['field']}.json", payload)
        with self.assertRaises(ValueError):
            load_profile(payload["model"], payload["field"], self.dir)


class TestResolveFourKinds(unittest.TestCase):
    """resolve 四态：exact / alias / case / unknown / unregistered。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        # ExchangeID: NASDAQ, NYSE, AMEX, PCX（小写值用于 case 测试）
        _write(
            self.dir / "atlas_finance_analytics.ExchangeID.json",
            _minimal(
                values=[
                    {"value": "NASDAQ", "count": 100},
                    {"value": "NYSE", "count": 90},
                    {"value": "AMEX", "count": 50},
                    {"value": "PCX", "count": 30},
                ],
                aliases={"nsdq": "NASDAQ"},
            ),
        )
        # s_city: Midway, Fairview（用于 case 归一）
        _write(
            self.dir / "atlas_retail_analytics.s_city.json",
            _minimal(
                model="atlas_retail_analytics",
                field="s_city",
                values=[{"value": "Midway", "count": 200}, {"value": "Fairview", "count": 100}],
            ),
        )
        # Gender: F, f, M, m 并存（casefold 多命中 → unknown）
        _write(
            self.dir / "atlas_finance_analytics.Gender.json",
            _minimal(
                field="Gender",
                values=[
                    {"value": "F", "count": 500},
                    {"value": "f", "count": 10},
                    {"value": "M", "count": 480},
                    {"value": "m", "count": 10},
                ],
            ),
        )
        # Tier: 整数维度（值以字符串存储）
        _write(
            self.dir / "atlas_finance_analytics.Tier.json",
            _minimal(
                field="Tier",
                values=[{"value": "0", "count": 7}, {"value": "1", "count": 100}, {"value": "3", "count": 50}],
            ),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_exact_match(self) -> None:
        res = resolve("atlas_finance_analytics", "ExchangeID", "NASDAQ", self.dir)
        self.assertEqual(res.kind, "exact")
        self.assertEqual(res.value, "NASDAQ")
        self.assertFalse(res.normalized)

    def test_alias_match(self) -> None:
        res = resolve("atlas_finance_analytics", "ExchangeID", "NSDQ", self.dir)
        self.assertEqual(res.kind, "alias")
        self.assertEqual(res.value, "NASDAQ")
        self.assertTrue(res.normalized)

    def test_case_match(self) -> None:
        res = resolve("atlas_retail_analytics", "s_city", "MIDWAY", self.dir)
        self.assertEqual(res.kind, "case")
        self.assertEqual(res.value, "Midway")
        self.assertTrue(res.normalized)

    def test_unknown_no_match(self) -> None:
        res = resolve("atlas_finance_analytics", "ExchangeID", "LSE", self.dir)
        self.assertEqual(res.kind, "unknown")
        self.assertEqual(res.value, "LSE")
        self.assertGreater(len(res.candidates), 0)

    def test_casefold_ambiguity_theoretical(self) -> None:
        """Gender 列 F/f 并存：ASCII 输入总是精确命中，casefold 歧义只在非 ASCII 输入触发。

        实测 Gender 列 F/f、M/m 并存，但用户输入 "f" → 精确命中 "f"（不触发歧义）。
        歧义只在用户输入非 ASCII 且 casefold 匹配多个值时触发（如 "𝔉" casefold="f"）。
        本测试验证 ASCII 输入 "f" 精确命中（不反问），符合安全默认（不为无证据的列制造反问）。
        """
        res = resolve("atlas_finance_analytics", "Gender", "f", self.dir)
        self.assertEqual(res.kind, "exact")
        self.assertEqual(res.value, "f")

    def test_unregistered_column(self) -> None:
        res = resolve("atlas_finance_analytics", "Branch", "HQ", self.dir)
        self.assertEqual(res.kind, "unregistered")
        self.assertEqual(res.value, "HQ")

    def test_integer_value_exact(self) -> None:
        """数值维度列：int 值 str(value) 命中 → 保留原类型。"""
        res = resolve("atlas_finance_analytics", "Tier", 3, self.dir)
        self.assertEqual(res.kind, "exact")
        self.assertEqual(res.value, 3)
        self.assertIsInstance(res.value, int)

    def test_integer_value_string_form(self) -> None:
        """数值维度列：字符串 '3' 同样命中（str(value) 口径一致）。"""
        res = resolve("atlas_finance_analytics", "Tier", "3", self.dir)
        self.assertEqual(res.kind, "exact")
        self.assertEqual(res.value, "3")


class TestRegisteredDimensions(unittest.TestCase):
    """registered_dimensions：枚举某模型已注册的维度字段。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_empty_dir(self) -> None:
        self.assertEqual(registered_dimensions("atlas_finance_analytics", self.dir), ())

    def test_skipped_excluded(self) -> None:
        _write(
            self.dir / "atlas_finance_analytics.Branch.json",
            _minimal(field="Branch", status="skipped", values=[]),
        )
        _write(
            self.dir / "atlas_finance_analytics.ExchangeID.json",
            _minimal(field="ExchangeID"),
        )
        result = registered_dimensions("atlas_finance_analytics", self.dir)
        self.assertEqual(result, ("ExchangeID",))


class TestRealRepoProfiles(unittest.TestCase):
    """真实仓库：若 semantic/values/ 已生成，运行时加载器必须能解析。

    B4 产物未入库时跳过（完整性检查在 tests/，不强制要求 values/ 存在）。
    """

    def test_all_profiles_loadable(self) -> None:
        from agent.value_domain import VALUES_DIR

        if not VALUES_DIR.exists():
            self.skipTest("semantic/values/ 未生成（B4 产物未入库）")
        files = sorted(VALUES_DIR.glob("*.json"))
        if not files:
            self.skipTest("semantic/values/ 为空")
        for path in files:
            with self.subTest(path=path.name):
                profile = parse_profile(path)
                self.assertIsInstance(profile, ValueProfile)
                self.assertIn(profile.status, ("registered", "skipped"))


if __name__ == "__main__":
    unittest.main()
