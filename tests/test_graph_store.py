"""语义关系图契约测试（NetworkX 可达性 + RRF 融合）。

图约束与 gold 维度样本对齐：13 组已实测样本全保留；跨实体错配
（现金域 × 证券维度）全过滤。语义层 rel 变更时此处会先于评测报警。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent.compiler import SemanticModel
from retrieval.fusion import rrf
from retrieval.graph_store import SemanticGraph

REPO = Path(__file__).resolve().parent.parent
MODEL = SemanticModel()
GRAPH = SemanticGraph(MODEL)

# 跨实体错配：现金域事实表（fact_cash_balances）不持有证券粒度
CASH_SECURITY_MISMATCHES = [
    ("cash_balance", "Issue"),
    ("cash_balance", "ExchangeID"),
    ("cash_balance", "Symbol"),
    ("cash_account_count", "Issue"),
    ("cash_account_count", "ExchangeID"),
    ("cash_account_count", "Symbol"),
]


class TestGraphStructure(unittest.TestCase):
    def test_node_and_edge_counts(self) -> None:
        """15 指标 + 8 数据集 + 维度字段都在图内，rel 边生效。"""
        self.assertEqual(len(MODEL.metrics), 15)
        self.assertEqual(len(MODEL.relationships), 12)
        for metric in MODEL.metrics:
            self.assertTrue(GRAPH.datasets_for(metric))

    def test_dimension_field_owner(self) -> None:
        self.assertEqual(GRAPH.dimension_table("Issue"), "dim_security")
        self.assertEqual(GRAPH.dimension_table("Branch"), "dim_broker")
        self.assertEqual(GRAPH.dimension_table("Gender"), "dim_customer")
        self.assertIsNone(GRAPH.dimension_table("不存在的字段"))


class TestGraphConsistencyWithGold(unittest.TestCase):
    def test_all_gold_dimension_samples_keep(self) -> None:
        """13 组已实测 gold 维度样本必须全保留（与评测集对齐，误杀即报警）。"""
        skipped = 0
        for path in sorted((REPO / "eval" / "gold").glob("gold-1*.json")):
            sample = json.loads(path.read_text(encoding="utf-8"))
            if sample.get("ambiguous", False):
                continue
            dims = sample.get("expected_dimensions", [])
            if not dims:
                skipped += 1
                continue
            self.assertTrue(
                all(GRAPH.can_group_by(sample["expected_metric"], d) for d in dims),
                f"{sample['id']} {sample['expected_metric']} x {dims} 被图误杀",
            )
        self.assertGreaterEqual(skipped, 20)  # 防误删样本导致测试空转


class TestGraphCrossEntityFilter(unittest.TestCase):
    def test_cash_metric_cannot_group_by_security(self) -> None:
        """现金域 × 证券维度 = 跨实体错配，图约束必须过滤（Day 23 验收）。"""
        for metric, dim in CASH_SECURITY_MISMATCHES:
            with self.subTest(metric=metric, dim=dim):
                self.assertFalse(GRAPH.can_group_by(metric, dim))

    def test_cash_metric_can_group_by_account_hierarchy(self) -> None:
        """现金域沿账户层级（dim_account→dim_customer/dim_broker）合法。"""
        for dim in ("Branch", "Office", "Gender", "Tier"):
            with self.subTest(dim=dim):
                self.assertTrue(GRAPH.can_group_by("cash_balance", dim))

    def test_filter_candidates_removes_only_mismatch(self) -> None:
        cands = ["cash_balance", "cash_account_count", "holdings_value", "trade_count"]
        kept = GRAPH.filter_candidates(cands, ["Issue"])
        self.assertEqual(kept, ["holdings_value", "trade_count"])
        self.assertEqual(GRAPH.filter_candidates(cands, []), cands)

    def test_detect_dimensions_from_question(self) -> None:
        self.assertIn("Branch", GRAPH.detect_dimensions("按分支统计 2013 年佣金收入"))
        self.assertEqual(GRAPH.detect_dimensions("2013 年总交易额是多少"), [])


class TestRrf(unittest.TestCase):
    def test_deterministic_and_rank_based(self) -> None:
        merged = rrf([["a", "b", "c"], ["b", "a", "c"]], k=60)
        self.assertEqual(merged[:2], ["a", "b"])
        self.assertEqual(rrf([["a", "b"], ["a", "b"]]), ["a", "b"])

    def test_present_in_all_rankings_outranks_single(self) -> None:
        # b 两路都靠前（rank1+rank2）应胜过只在单路第一的 a（rank1+rank3）
        merged = rrf([["a", "b", "c", "d", "e"], ["b", "c", "a"]], k=60)
        self.assertEqual(merged[0], "b")

    def test_empty_rankings(self) -> None:
        self.assertEqual(rrf([]), [])
        self.assertEqual(rrf([[], []]), [])


if __name__ == "__main__":
    unittest.main()
