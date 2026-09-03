"""semantic/export_dbt.py 契约测试（Day 53，批次 E）。

断言口径：Ossie 20 指标的分类是可复现常量（agg 14 / ratio 3 / unmapped 3，
见模块 docstring 三态说明）——防止语义层变更时导出悄悄漏指标或改映射语义；
YAML round-trip 合法；measure 名唯一；unmapped 逐条带理由。
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from typing import Any

import yaml

from semantic.export_dbt import export_document

REPO = Path(__file__).resolve().parent.parent
FINANCE = REPO / "semantic" / "ossie" / "atlas_finance.ossie.yaml"


def _head_sha() -> str:
    """当前 git HEAD 短 sha（export 产物绑定 HEAD，断言须随 HEAD 前进）。"""
    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


# 20 指标三态分布（2026-09-03 盘点，见模块 docstring）
AGG_METRICS = (
    "commission_revenue",
    "holdings_value",
    "cash_balance",
    "trade_count",
    "total_trade_quantity",
    "total_trade_tax",
    "avg_trade_price",
    "holdings_quantity",
    "active_account_count",
    "traded_security_count",
    "active_customer_count",
    "holding_account_count",
    "holding_security_count",
    "cash_account_count",
)
RATIO_METRICS = (
    "average_commission_per_trade",
    "average_holding_value",
    "average_cash_balance",
)
UNMAPPED_METRICS = ("total_trade_value", "average_trade_value", "commission_rate")


class TestExportDbt(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        doc = yaml.safe_load(FINANCE.read_text(encoding="utf-8"))
        cls.report: dict[str, Any] = {}
        cls.rendered = export_document(doc, cls.report)
        cls.classifications = cls.report

    def test_all_twenty_metrics_classified(self) -> None:
        self.assertEqual(self.classifications["metric_total"], 20)
        self.assertEqual(sorted(self.classifications["mapped"]["agg"]), sorted(AGG_METRICS))
        self.assertEqual(sorted(self.classifications["mapped"]["ratio"]), sorted(RATIO_METRICS))
        unmapped = {u["name"] for u in self.classifications["unmapped"]}
        self.assertEqual(sorted(unmapped), sorted(UNMAPPED_METRICS))

    def test_unmapped_carries_reason_and_sql(self) -> None:
        for u in self.classifications["unmapped"]:
            self.assertGreater(len(u["reason"]), 10)
            self.assertIn("SUM", u["sql"])

    def test_rendered_yaml_roundtrip(self) -> None:
        text = yaml.safe_dump(self.rendered, allow_unicode=True)
        doc = yaml.safe_load(text)
        self.assertEqual(len(doc["semantic_models"]), 8)  # 8 个 dataset
        self.assertEqual(len(doc["metrics"]), 17)  # 14 agg + 3 ratio

    def test_measure_names_unique_and_referenced(self) -> None:
        measures: list[str] = []
        for sm in self.rendered["semantic_models"]:
            measures += [m["name"] for m in sm["measures"]]
        self.assertEqual(len(measures), len(set(measures)), "measure 名必须唯一")
        for metric in self.rendered["metrics"]:
            tp = metric["type_params"]
            if metric["type"] == "agg":
                self.assertIn(tp["measure"], measures)
            else:
                self.assertIn(tp["numerator"], measures)
                self.assertIn(tp["denominator"], measures)

    def test_metricflow_measure_shapes(self) -> None:
        """导出的 measure 只含 MetricFlow 合法形状（单列 + agg）。"""
        for sm in self.rendered["semantic_models"]:
            for m in sm["measures"]:
                self.assertIn(m["agg"], ("sum", "average", "count", "count_distinct", "min", "max"))
                self.assertIsInstance(m["expr"], str)
                self.assertTrue(m["name"].endswith(f"_{m['agg']}"), m["name"])

    def test_ratio_metrics_use_count_distinct_denominator_measures(self) -> None:
        """户均类 ratio 的分母必须是 count_distinct measure（口径：户数=去重账户）。"""
        measures = {m["name"]: m for sm in self.rendered["semantic_models"] for m in sm["measures"]}
        for metric in self.rendered["metrics"]:
            if metric["name"] in ("average_holding_value", "average_cash_balance"):
                den = measures[metric["type_params"]["denominator"]]
                self.assertEqual(den["agg"], "count_distinct")


class TestExportCli(unittest.TestCase):
    def test_cli_writes_artifacts(self) -> None:
        import tempfile

        from semantic.export_dbt import main

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "dbt.yml"
            report = Path(tmp) / "report.json"
            rc = main(["--in", str(FINANCE), "--out", str(out), "--report", str(report)])
            self.assertEqual(rc, 0)
            yaml.safe_load(out.read_text(encoding="utf-8"))
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["metric_total"], 20)
            # 产物 sha 必须等于当前 HEAD（2026-09-03 CI 修复：硬编码 7d48dcb
            # 在 HEAD 前进后失真；绑定语义是「与快照/评测同一 HEAD」）
            self.assertEqual(data["sha"], _head_sha())


if __name__ == "__main__":
    unittest.main()
