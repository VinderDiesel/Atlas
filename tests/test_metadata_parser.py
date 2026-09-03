"""metadata/parser.py 契约测试：注释/结构抽取与 known 对照。

纯本地单测（不连 Doris / Polaris）：断言集中在抽取规则的确定性行为上。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from metadata.parser import (
    analyze_text,
    known_registry,
    loader_ddl_files,
    run,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

FIXTURE = """-- =====================================================================
-- fixture_orders.sql
-- 功能：订单事实样例（单测 fixture，非真实表）
-- 重跑：INSERT OVERWRITE 幂等
-- 口径：Amount 为含税成交额，剔除已取消订单
-- =====================================================================
SELECT 'x--y' AS literal_test;  -- 行尾注释不应混入头部

CREATE DATABASE IF NOT EXISTS atlas.dwd;

CREATE TABLE IF NOT EXISTS atlas.dwd.fixture_orders (
    OrderID INT,
    SK_CustomerID INT,
    Amount DECIMAL(18, 2),
    Tax DECIMAL(12, 2),
    IsCancelled BOOLEAN,
    Qty INT
);

INSERT OVERWRITE TABLE atlas.dwd.fixture_orders
SELECT
    o.id AS OrderID,
    o.cid AS SK_CustomerID,
    o.amt AS Amount,
    o.tax AS Tax,
    (o.flag = 1) AS IsCancelled,
    o.qty AS Qty
FROM atlas.tpcdi.orders o
WHERE o.status <> 'CANCEL';

CREATE TABLE IF NOT EXISTS atlas.dwd.fixture_stats (
    SK_CustomerID INT,
    TotalAmount DECIMAL(20, 2),
    OrderCount INT
);

INSERT OVERWRITE TABLE atlas.dwd.fixture_stats
SELECT
    cid AS SK_CustomerID,
    SUM(o.amt) AS total_amount,
    COUNT(*) AS order_count,
    SUM(o.tax) OVER (PARTITION BY cid) AS tax_window
FROM atlas.tpcdi.orders o
GROUP BY cid;
"""


def _analyze_fixture() -> dict:
    return analyze_text(FIXTURE, "sql/fixture.sql")


class TestHeaderComments(unittest.TestCase):
    def test_header_meta_function_and_rerun(self) -> None:
        report = _analyze_fixture()
        self.assertEqual(report["header_meta"]["功能"], "订单事实样例（单测 fixture，非真实表）")
        self.assertEqual(report["header_meta"]["重跑"], "INSERT OVERWRITE 幂等")
        self.assertEqual(report["header_lines"], 6)

    def test_inline_double_dash_not_treated_as_comment(self) -> None:
        """字符串常量里的 '--' 与行尾注释不得污染头部注释块。"""
        report = _analyze_fixture()
        meta = report["header_meta"]
        self.assertNotIn("--y", meta.get("功能", ""))
        self.assertNotIn("行尾注释", meta.get("功能", ""))
        # 头部只有 6 行文件头，字符串与行尾注释未被当作注释行收录
        self.assertEqual(report["header_lines"], 6)

    def test_no_function_key_keeps_meta_empty(self) -> None:
        """无白名单键的注释（如 loader 生成 DDL）meta 为空但不报错。"""
        report = analyze_text("-- 一行无键注释\nSELECT 1;", "x.sql")
        self.assertEqual(report["header_meta"], {})
        self.assertGreaterEqual(report["header_lines"], 1)


class TestStructureExtraction(unittest.TestCase):
    def test_create_database_skipped(self) -> None:
        report = _analyze_fixture()
        names = [d["name"] for d in report["datasets"]]
        self.assertNotIn("atlas.dwd", names)
        self.assertEqual(len(report["datasets"]), 2)

    def test_measures_exclude_ids_and_flags(self) -> None:
        report = _analyze_fixture()
        orders = [m["column"] for m in report["measures"] if m["table"].endswith("fixture_orders")]
        self.assertNotIn("OrderID", orders)  # PascalCase 主键
        self.assertNotIn("SK_CustomerID", orders)  # 代理键
        self.assertEqual(orders, ["Amount", "Tax", "Qty"])  # IsCancelled 是 BOOL

    def test_measure_hint_annotated(self) -> None:
        report = _analyze_fixture()
        qty = next(
            m
            for m in report["measures"]
            if m["table"].endswith("fixture_orders") and m["column"] == "Qty"
        )
        self.assertEqual(qty["hint"], "qty")
        self.assertEqual(qty["type"], "INT")

    def test_aggregate_metric_candidates_only(self) -> None:
        """GROUP BY 聚合 AS 别名 → metric 候选；窗口函数与无别名聚合排除。"""
        report = _analyze_fixture()
        names = [m["name"] for m in report["metrics"]]
        self.assertIn("total_amount", names)  # SUM(o.amt) GROUP BY
        self.assertIn("order_count", names)  # COUNT(*) AS
        self.assertNotIn("tax_window", names)  # 窗口函数剔除
        self.assertEqual(len(report["metrics"]), 2)

    def test_lineage_cross_statement(self) -> None:
        """INSERT 源表关联到同名 CREATE 数据集（跨语句配对）。"""
        report = _analyze_fixture()
        stats = next(d for d in report["datasets"] if d["name"].endswith("fixture_stats"))
        self.assertEqual(stats["source_tables"], ["atlas.tpcdi.orders"])
        orders = next(d for d in report["datasets"] if d["name"].endswith("fixture_orders"))
        self.assertIn("atlas.tpcdi.orders", orders["source_tables"])


class TestKnownRegistry(unittest.TestCase):
    def test_real_ossie_registry_loaded(self) -> None:
        """真实语义层注册表：dwd 数据集与字段应存在（与 ossie yaml 强一致）。"""
        registry = known_registry()
        self.assertIn("fact_trades", registry["datasets"])
        self.assertIn("Commission", registry["fields"]["fact_trades"])
        self.assertIn("total_trade_value", registry["metrics"])

    def test_known_tagging_on_real_dwd_file(self) -> None:
        """known 标注在 run() 汇总层（需要语义层注册表对照）。"""
        path = REPO_ROOT / "sql" / "dwd" / "fact_trades.sql"
        report = run([(str(path.relative_to(REPO_ROOT)), path.read_text(encoding="utf-8"))])
        file_report = report["files"][0]
        commission = next(m for m in file_report["measures"] if m["column"] == "Commission")
        self.assertTrue(commission["known"])  # 已在语义层注册
        datasets = {d["name"]: d["known"] for d in file_report["datasets"]}
        self.assertTrue(datasets["atlas.dwd.fact_trades"])


class TestLoaderDdlFiles(unittest.TestCase):
    def test_loader_ddl_splits_17_files(self) -> None:
        files = loader_ddl_files()
        self.assertEqual(len(files), 17)
        sources = [source for source, _ in files]
        self.assertTrue(any(s.endswith("#atlas.tpcdi.trade") for s in sources))

    def test_loader_ddl_measures_extracted(self) -> None:
        files = loader_ddl_files()
        trade = next(text for source, text in files if source.endswith("#atlas.tpcdi.trade"))
        report = analyze_text(trade, "data/loader.py --emit-ddl #atlas.tpcdi.trade")
        columns = [m["column"] for m in report["measures"]]
        expected = ["t_qty", "t_bid_price", "t_trade_price", "t_chrg", "t_comm", "t_tax"]
        self.assertEqual(columns, expected)
        self.assertNotIn("t_id", columns)


class TestRunReport(unittest.TestCase):
    def test_run_summary_counts_and_jsonable(self) -> None:
        report = run([("sql/fixture.sql", FIXTURE)])
        counts = report["counts"]
        self.assertEqual(counts["files"], 1)
        self.assertEqual(counts["datasets"]["total"], 2)
        self.assertGreaterEqual(counts["measures"]["total"], 3)
        self.assertEqual(counts["metrics"]["total"], 2)
        json.dumps(report, ensure_ascii=False)  # 报告必须可 JSON 化（写盘依赖）
        self.assertRegex(report["sha"], r"^[0-9a-f]{7}$")

    def test_corpus_defaults_declared(self) -> None:
        report = run([("sql/fixture.sql", FIXTURE)])
        self.assertEqual(report["corpus"]["dwd_sql"], 8)
        self.assertEqual(report["corpus"]["tpcdi_ods_ddl"], 17)


if __name__ == "__main__":
    unittest.main()
