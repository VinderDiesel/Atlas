"""schema linker 契约测试（Day 29）：确定性、图约束、KL#18 场景回归。

口径（与 eval/schema_link_eval.py 一致）：
- 输出对象是指标候选（top-K），维度约束来自 SemanticGraph.detect_dimensions；
- 本文件用真实语义层（atlas_finance.ossie.yaml，20 指标），问句取 gold 原句——
  防语料/语义层变更导致检索链路漂移（与 test_bm25/test_rerank 同哲学）。
"""

from __future__ import annotations

import unittest

from agent.compiler import SemanticModel
from agent.tools.schema_linker import SchemaLinker

MODEL = SemanticModel()


def link(question: str, k: int = 5):
    """测试辅助：全量热度（非留一）下链接，避免留一逻辑干扰断言。"""
    return SchemaLinker(MODEL).link(question, k=k)


class TestSchemaLinkerDeterminism(unittest.TestCase):
    def test_same_question_same_output(self) -> None:
        """确定性：同一问句两次链接输出完全一致（组件全部确定性）。"""
        q = "按分支统计 2013 年佣金收入，列出前 5 名"
        r1 = link(q)
        r2 = link(q)
        self.assertEqual(r1, r2)

    def test_gold102_commission_top1(self) -> None:
        """gold-102 问句 → top1 = commission_revenue（与 P1 验收载体一致）。"""
        r = link("按分支统计 2013 年佣金收入，列出前 5 名")
        self.assertEqual(r.candidates[0], "commission_revenue")
        self.assertIn("Branch", r.dims)

    def test_k118_holding_value_question(self) -> None:
        """KL#18 场景回归：市值合计问句 top1 = holdings_value（不被同域指标抢）。"""
        r = link("2017 年 7 月 7 日全账户持仓市值合计是多少？")
        self.assertEqual(r.candidates[0], "holdings_value")


class TestSchemaLinkerGraphConstraint(unittest.TestCase):
    def test_cash_x_security_pruned(self) -> None:
        """跨实体错配（现金域 × 证券类型维度）在阶段 1 被图约束剔除。

        gold-142 的「分支×持仓市值」是合法组合（dim_broker 可达），不产生
        剔除；本用例用真错配（Day 23 验收同款：cash × Issue）验证收窄。
        """
        r = link("按证券类型统计 2013 年各账户现金余额")
        self.assertIn("Issue", r.dims)
        self.assertLess(len(r.kept), 20)
        self.assertNotIn("cash_balance", r.kept)  # 现金域不可按证券分组

    def test_gold142_branch_dimension_detected(self) -> None:
        """gold-142 合法组合：检出 Branch 且 holdings_value 仍在 top-K。"""
        r = link("按分支统计 2014 年 12 月 31 日持仓市值，列出前 5 名")
        self.assertIn("Branch", r.dims)
        self.assertIn("holdings_value", r.candidates)

    def test_no_dimension_signal_keeps_all(self) -> None:
        """无维度信号时不过滤（退化为全量域召回）。"""
        r = link("2013 年总交易额是多少？")
        self.assertEqual(r.dims, ())
        self.assertEqual(len(r.kept), 20)


class TestSchemaLinkerTables(unittest.TestCase):
    def test_top1_tables_cover_gold102_truth(self) -> None:
        """表级展开：commission_revenue top1 可达表覆盖 gold-102 真值表。"""
        r = link("按分支统计 2013 年佣金收入，列出前 5 名")
        tables = set(r.tables_top1)
        self.assertIn("fact_trades", tables)
        self.assertIn("dim_broker", tables)
        self.assertIn("dim_date", tables)


if __name__ == "__main__":
    unittest.main()
