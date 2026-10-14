"""降级证明固化测试（ADR-0032 ⑥ 降级证明行；计划 M2-T3）：无概念层 → 消费缝逐字一致。

三情形（`load_concept_index` 返回 None 的三种来源，ADR-0032 总原则「无索引 =
全缝旁路」）：
1. **retail 域**：无 fibo_alignment 映射 → 不生成产物文件 → None（ADR-0032 ④）；
2. **索引文件缺失**：空 ontology_dir（等价「删除索引文件」）→ None；
3. **运行开关 off**：`ATLAS_CONCEPT_LAYER` off 族 → None（ADR-0032 ⑤）。

口径（计划 M2-T3）：测试内先以「绝不注入」路径（默认构造、无 concept_index
实参）生成输出作为期望（确定性），再在「注入但旁路成立」路径（concept_index =
loader 实返 = None）比对——两路都必须绿。断言为**逐字比对**（SchemaLinkResult
全字段相等），非抽样。

覆盖范围（诚实声明）：本卡 = M2 阶段已接入的概念层注入面（schema_linker，
M2-T1 交付，含检索缝全部消费路径）。planner 的 `concept_index` 注入口属 M3
交付（M3-T1/T2）——其降级证明在本文件随 M3 注入落地时按同法补齐；计划 M2-T3
步骤文本含 planner，属接口缺口，裁定与登记见计划账本（M2-T3 行）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.compiler import SemanticModel
from agent.concept_index import LAYER_ENV, ConceptIndex, load_concept_index
from agent.tools.schema_linker import SchemaLinker, SchemaLinkResult

MODEL = SemanticModel()

# 固定问句集（gold 原句，防链路漂移）：zh 直接命中 / zh 维度信号 / en
QUESTIONS = (
    "2013 年第二季度总交易额是多少？",
    "按分支统计 2013 年佣金收入，列出前 5 名",
    "What was the total cash balance on 2014-03-31?",
)


def _baseline(question: str) -> SchemaLinkResult:
    """「绝不注入」路径：默认构造（不传 concept_index = 生产默认路径）。"""
    return SchemaLinker(MODEL).link(question, k=5)


def _injected(question: str, index: ConceptIndex | None) -> SchemaLinkResult:
    """「注入但旁路成立」路径：concept_index 传入 loader 实返值。"""
    return SchemaLinker(MODEL, concept_index=index).link(question, k=5)


def _assert_identical(tc: unittest.TestCase, index: ConceptIndex | None) -> None:
    """逐字比对：loader 实返（预期 None）注入 vs 绝不注入基线（全问句集）。"""
    tc.assertIsNone(index)
    for q in QUESTIONS:
        tc.assertEqual(_injected(q, index), _baseline(q), q)


class TestPrecondition(unittest.TestCase):
    """防混淆前置：确保「情形 None」由情形所致，而非默认 arm 本就坏。"""

    def test_default_arm_index_loadable(self) -> None:
        """默认 arm（finance）索引在库且可加载——三情形单测的前提。"""
        index = load_concept_index()
        self.assertIsNotNone(index)
        self.assertGreater(len(index.concepts), 0)

    def test_question_set_informative(self) -> None:
        """比对有信息量：问句集基线召回非空且 top1 锁定（防全空退化比对）。"""
        expected_top1 = {
            QUESTIONS[0]: "total_trade_value",
            QUESTIONS[1]: "commission_revenue",
            QUESTIONS[2]: "cash_balance",
        }
        for q, top1 in expected_top1.items():
            self.assertEqual(_baseline(q).candidates[0], top1, q)

    def test_baseline_deterministic(self) -> None:
        """期望值可信：绝不注入路径同问句两次独立构造输出全字段相同。"""
        for q in QUESTIONS:
            self.assertEqual(_baseline(q), _baseline(q), q)


class TestRetailNoIndex(unittest.TestCase):
    """情形 1：retail 无映射 → 不生成产物 → loader None → 旁路逐字一致。"""

    def test_loader_returns_none(self) -> None:
        self.assertIsNone(load_concept_index("atlas_retail"))

    def test_linker_identical_to_baseline(self) -> None:
        _assert_identical(self, load_concept_index("atlas_retail"))


class TestMissingIndexFile(unittest.TestCase):
    """情形 2：空 ontology_dir（等价删除索引文件）→ loader None → 旁路逐字一致。"""

    def test_loader_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_concept_index("atlas_finance", Path(tmp)))

    def test_linker_identical_to_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _assert_identical(self, load_concept_index("atlas_finance", Path(tmp)))


class TestLayerEnvOff(unittest.TestCase):
    """情形 3：`ATLAS_CONCEPT_LAYER=off` → loader None → 旁路逐字一致。"""

    def test_loader_returns_none(self) -> None:
        with mock.patch.dict(os.environ, {LAYER_ENV: "off"}):
            self.assertIsNone(load_concept_index())

    def test_linker_identical_to_baseline(self) -> None:
        with mock.patch.dict(os.environ, {LAYER_ENV: "off"}):
            _assert_identical(self, load_concept_index())


if __name__ == "__main__":
    unittest.main()
