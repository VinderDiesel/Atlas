"""schema linker 概念层注入契约测试（ADR-0032 M2-T1）：桥接召回与旁路不变量。

口径
----
- **注入式**：`SchemaLinker(..., concept_index=...)`；默认 `None` 与既有行为
  **逐字一致**（旁路不变量，ADR-0032 总原则「无索引 = 全缝旁路」）。
- **扩展只发生在查询词**（阶段 2 打分）：阶段 1 输出（dims/kept）与
  `SchemaLinkResult.question` 仍是原问句（ADR-0032 ③「只做术语扩展」）。
- fixture 为**合成小索引**（不依赖 AOT 产物文件）：术语经概念闭包
  （自身 + 后代 + 祖先 1 层）桥接到真实语义层指标 `total_trade_value`
  （其检索文档含同义词「交易额」——本测试据实锁定）。
"""

from __future__ import annotations

import unittest

from agent.compiler import SemanticModel
from agent.concept_index import (
    ConceptEntry,
    ConceptIndex,
    Edge,
    Term,
    load_concept_index,
)
from agent.tools.schema_linker import SchemaLinker, SchemaLinkResult

MODEL = SemanticModel()

# 桥接问句：「销售款」零重叠于全部指标文档（基线空召回——已在前置用例锁定）；
# 注入后经 MonetaryAmount →（父）Trade 闭包追加术语「交易额」→ 命中 ttv 文档。
QUESTION = "2013 年销售款是多少？"
QUESTION_DIM = "按分支机构统计 2013 年销售款，列出前 5 名"
TARGET = "total_trade_value"


def _term(text: str, source: str = "S3") -> Term:
    """构造 zh 术语（via 为 S2 provenance，fixture 全 None）。"""
    return Term(text=text, lang="zh", source=source, via=None)


def _fixture_index() -> ConceptIndex:
    """合成索引：销售款 → MonetaryAmount →（父）Trade（术语「交易额」）。"""
    return ConceptIndex(
        schema_version=1,
        generated_at="2026-10-14T00:00:00+08:00",
        fibo_commit="fixture",
        commons_release="fixture",
        source_semantic_sha="fixture",
        concepts={
            "Trade": ConceptEntry(
                iri="https://example.org/fibo/Trade",
                label="Trade",
                definition=None,
                terms=(_term("交易额", source="S1"),),
                parents=(),
                children=(Edge(concept="MonetaryAmount", transitive=False),),
                objects=(),
            ),
            "MonetaryAmount": ConceptEntry(
                iri="https://example.org/fibo/MonetaryAmount",
                label="MonetaryAmount",
                definition=None,
                terms=(_term("销售款"),),
                parents=(Edge(concept="Trade", transitive=False),),
                children=(),
                objects=(),
            ),
        },
    )


def _baseline(question: str, k: int = 5) -> SchemaLinkResult:
    """未注入基线（默认构造路径）。"""
    return SchemaLinker(MODEL).link(question, k=k)


def _injected(question: str, k: int = 5) -> SchemaLinkResult:
    """注入合成索引（每次独立 fixture 实例——确定性按同输入同输出断言）。"""
    return SchemaLinker(MODEL, concept_index=_fixture_index()).link(question, k=k)


class TestConceptInjectionBridge(unittest.TestCase):
    """注入合成索引：桥接术语使零重叠问句召回目标指标（ADR-0032 ③ 检索缝）。"""

    def test_baseline_zero_overlap_empty(self) -> None:
        """前置锁定：基线对「销售款」零重叠 → 空 scores / 空 candidates（诚实基线）。"""
        r = _baseline(QUESTION)
        self.assertEqual(r.scores, ())
        self.assertEqual(r.candidates, ())

    def test_injection_bridges_target_top1(self) -> None:
        """注入后：桥接术语使目标指标 top-1（基线空召回 → 命中；含维度问句同断言）。"""
        for q in (QUESTION, QUESTION_DIM):
            r = _injected(q)
            self.assertEqual(r.scores[0][0], TARGET, q)
            self.assertEqual(r.candidates[0], TARGET, q)

    def test_stage1_fields_unchanged(self) -> None:
        """阶段 1 输出不受扩展影响：dims / kept 与基线逐字一致（含维度问句）。"""
        for q in (QUESTION, QUESTION_DIM):
            base, inj = _baseline(q), _injected(q)
            self.assertEqual(inj.dims, base.dims, q)
            self.assertEqual(inj.kept, base.kept, q)

    def test_dim_question_has_signal(self) -> None:
        """前置锁定：维度问句确实检出 Branch（stage1 比对非空信号）。"""
        self.assertEqual(_baseline(QUESTION_DIM).dims, ("Branch",))

    def test_result_question_is_original(self) -> None:
        """扩展只发生在查询词：结果 question 字段仍是原问句（未追加术语）。"""
        self.assertEqual(_injected(QUESTION).question, QUESTION)

    def test_injection_deterministic(self) -> None:
        """注入路径确定性：两次链接（独立 fixture 实例）输出全字段相同。"""
        self.assertEqual(_injected(QUESTION), _injected(QUESTION))


class TestConceptDegradation(unittest.TestCase):
    """无索引旁路：不注入 / loader 返回 None → 与基线逐字一致（降级证明·单元层）。"""

    def test_none_identical_to_baseline(self) -> None:
        """显式 None 与默认构造逐字一致（SchemaLinkResult 全字段比对）。"""
        explicit = SchemaLinker(MODEL, concept_index=None).link(QUESTION, k=5)
        self.assertEqual(explicit, _baseline(QUESTION))

    def test_loader_none_identical_to_baseline(self) -> None:
        """loader 返回 None 情形（retail 无映射 → 不生成产物）→ 与基线逐字一致。"""
        idx = load_concept_index("atlas_retail")
        self.assertIsNone(idx)  # ADR-0032 ④：retail 无 fibo_alignment → 不生成文件
        for q in (QUESTION, QUESTION_DIM):
            self.assertEqual(SchemaLinker(MODEL, concept_index=idx).link(q, k=5), _baseline(q))


if __name__ == "__main__":
    unittest.main()
