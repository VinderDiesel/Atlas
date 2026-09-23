"""Jev System One 引擎测试（ADR-0030 ③④⑧）：无网络、零端点可跑的安全内核层。

覆盖三件事：
1. **原语收敛**：Choice/Score/Noul → `JevDecision`，置信度**原样透传不改写**；
2. **契约破裂显式失败**：未知原语 / 越界选项 / 缺判别项 / 置信度越界 → 抛错，
   绝不静默接受（脏值流下去比报错危险）；
3. **可切换性**：`MetaReranker` 注入 chooser 后——高置信才覆盖、异常/低置信
   完全退回确定性词典序、不注入时行为与 ADR-0030 前逐字一致。
"""

from __future__ import annotations

import unittest
from typing import Any

from agent.compiler import SemanticModel
from agent.jev_engine import (
    PRIMITIVE_CHOICE,
    PRIMITIVE_NOUL,
    PRIMITIVE_SCORE,
    JevEngine,
)
from retrieval.rerank import MetaReranker

MODEL = SemanticModel()


class _FakeClient:
    """假 SystemOne 客户端：回放预设 decisions，记录收到的问题（零网络）。"""

    def __init__(self, decisions: dict[str, Any]) -> None:
        self._decisions = decisions
        self.seen_questions: dict[str, Any] | None = None
        self.seen_state: str = ""

    def decide(
        self, state: str, questions: dict[str, Any], model: str
    ) -> tuple[dict[str, Any], dict[str, int]]:
        self.seen_state = state
        self.seen_questions = questions
        return self._decisions, {"prompt_tokens": 10, "completion_tokens": 0}


class _RaisingClient:
    """端点失败模拟：任何调用都抛（验证上层回落，不吞不包）。"""

    def decide(self, state: str, questions: dict[str, Any], model: str) -> Any:
        raise RuntimeError("端点不可达")


class TestJevChoice(unittest.TestCase):
    def test_choice_transfers_confidence_verbatim(self) -> None:
        """置信度必须原样透传——校准是 Jev 的核心价值，Atlas 不得二次加工。"""
        client = _FakeClient(
            {
                "metric": {
                    "type": PRIMITIVE_CHOICE,
                    "choice": "total_trade_value",
                    "confidence": 0.873,
                    "probs": {"total_trade_value": 0.873, "trade_count": 0.104},
                }
            }
        )
        d = JevEngine(client, "jev-1").choose(
            state="问句：今年总成交金额",
            question="metric",
            options=("total_trade_value", "trade_count"),
        )
        self.assertEqual(d.primitive, PRIMITIVE_CHOICE)
        self.assertEqual(d.choice, "total_trade_value")
        self.assertEqual(d.confidence, 0.873)  # 逐字，非近似
        self.assertEqual(d.probs["trade_count"], 0.104)

    def test_options_passed_as_structured_questions(self) -> None:
        """候选项经 questions 结构化传入（强类型约束在协议层，不在提示词层）。"""
        client = _FakeClient(
            {"metric": {"type": PRIMITIVE_CHOICE, "choice": "a", "confidence": 0.9}}
        )
        JevEngine(client, "jev-1").choose("s", "metric", ("a", "b"))
        assert client.seen_questions is not None
        self.assertEqual(client.seen_questions["metric"]["options"], ["a", "b"])

    def test_out_of_options_choice_rejected(self) -> None:
        """返回不在请求候选里的选项 = 契约破裂，绝不静默接受。"""
        client = _FakeClient(
            {"metric": {"type": PRIMITIVE_CHOICE, "choice": "ghost", "confidence": 0.99}}
        )
        with self.assertRaises(ValueError):
            JevEngine(client, "jev-1").choose("s", "metric", ("a", "b"))

    def test_primitive_mismatch_rejected(self) -> None:
        """请求 Choice 却回 Score = 原语不符，显式失败。"""
        client = _FakeClient({"metric": {"type": PRIMITIVE_SCORE, "confidence": 0.9}})
        with self.assertRaises(ValueError):
            JevEngine(client, "jev-1").choose("s", "metric", ("a", "b"))

    def test_missing_decision_rejected(self) -> None:
        """响应缺判别项 → 显式失败（不返回空决策让下游猜）。"""
        with self.assertRaises(ValueError):
            JevEngine(_FakeClient({}), "jev-1").choose("s", "metric", ("a",))

    def test_confidence_out_of_range_rejected(self) -> None:
        """置信度越界 [0,1] → JevDecision 构造即拒（脏值不入流通管道）。"""
        client = _FakeClient(
            {"metric": {"type": PRIMITIVE_CHOICE, "choice": "a", "confidence": 1.7}}
        )
        with self.assertRaises(ValueError):
            JevEngine(client, "jev-1").choose("s", "metric", ("a",))

    def test_bool_confidence_rejected(self) -> None:
        """bool 是 int 子类：True 不得被当作置信度 1.0 静默放行。"""
        client = _FakeClient(
            {"metric": {"type": PRIMITIVE_CHOICE, "choice": "a", "confidence": True}}
        )
        with self.assertRaises(ValueError):
            JevEngine(client, "jev-1").choose("s", "metric", ("a",))

    def test_option_cardinality_guard(self) -> None:
        """单选择字段基数上限 255（官方口径）：超出须先打分再选，不硬塞。"""
        with self.assertRaises(ValueError):
            JevEngine(_FakeClient({}), "jev-1").choose(
                "s", "metric", tuple(f"m{i}" for i in range(256))
            )

    def test_empty_options_rejected(self) -> None:
        with self.assertRaises(ValueError):
            JevEngine(_FakeClient({}), "jev-1").choose("s", "metric", ())

    def test_client_exception_propagates(self) -> None:
        """端点失败须向上抛，由调用点决定回落——本模块不改写为半成品结果。"""
        with self.assertRaises(RuntimeError):
            JevEngine(_RaisingClient(), "jev-1").choose("s", "metric", ("a",))


class TestJevScoreAndNoul(unittest.TestCase):
    def test_score_returns_level_scores(self) -> None:
        client = _FakeClient(
            {"risk": {"type": PRIMITIVE_SCORE, "scores": {"low": 0.8}, "confidence": 0.8}}
        )
        d = JevEngine(client, "jev-1").score("s", "risk", ("low", "high"))
        self.assertEqual(d.primitive, PRIMITIVE_SCORE)
        self.assertEqual(d.scores["low"], 0.8)

    def test_noul_probability_is_confidence(self) -> None:
        """Noul 的「真概率」本身即校准输出，置信度与之一致（不另造第二个数）。"""
        client = _FakeClient({"statement": {"type": PRIMITIVE_NOUL, "probability": 0.62}})
        d = JevEngine(client, "jev-1").noul("s", "该问句属于单指标查询")
        self.assertEqual(d.primitive, PRIMITIVE_NOUL)
        self.assertEqual(d.probability, 0.62)
        self.assertEqual(d.confidence, 0.62)

    def test_noul_out_of_range_rejected(self) -> None:
        client = _FakeClient({"statement": {"type": PRIMITIVE_NOUL, "probability": 1.4}})
        with self.assertRaises(ValueError):
            JevEngine(client, "jev-1").noul("s", "陈述")


class TestRerankChooserSeam(unittest.TestCase):
    """可插拔接缝（ADR-0030 ⑤）：Jev 只建议，确定性词典序无条件兜底。"""

    def test_no_chooser_behaviour_unchanged(self) -> None:
        """不注入 chooser → 与 ADR-0030 前逐字一致（既有 44/44 口径不动）。"""
        ranked = ["trade_count", "total_trade_quantity"]
        top = MetaReranker(MODEL, popularity={}).rerank(ranked, "2014 年总成交量是多少？")
        self.assertEqual(top[0], "total_trade_quantity")

    def test_high_confidence_choice_leads(self) -> None:
        """高置信判别把选中项提到首位（其余保持确定性序）。"""
        client = _FakeClient(
            {"metric": {"type": PRIMITIVE_CHOICE, "choice": "trade_count", "confidence": 0.95}}
        )
        reranker = MetaReranker(MODEL, popularity={}, chooser=JevEngine(client, "jev-1"))
        top = reranker.rerank(["total_trade_quantity", "trade_count"], "2014 年总成交量是多少？")
        self.assertEqual(top[0], "trade_count")
        self.assertEqual(top[1], "total_trade_quantity")

    def test_low_confidence_keeps_deterministic_order(self) -> None:
        """低置信不覆盖——Jev 不确定时，原逻辑胜出（可切换语义的核心）。"""
        client = _FakeClient(
            {"metric": {"type": PRIMITIVE_CHOICE, "choice": "trade_count", "confidence": 0.51}}
        )
        reranker = MetaReranker(MODEL, popularity={}, chooser=JevEngine(client, "jev-1"))
        top = reranker.rerank(["total_trade_quantity", "trade_count"], "2014 年总成交量是多少？")
        self.assertEqual(top[0], "total_trade_quantity")

    def test_endpoint_failure_falls_back_silently(self) -> None:
        """端点失败 → 退回确定性序、不抛给调用方（重排是增益位，fail-open）。"""
        reranker = MetaReranker(MODEL, popularity={}, chooser=JevEngine(_RaisingClient(), "jev-1"))
        top = reranker.rerank(["total_trade_quantity", "trade_count"], "2014 年总成交量是多少？")
        self.assertEqual(top[0], "total_trade_quantity")

    def test_ghost_choice_falls_back(self) -> None:
        """越界选项在重排层同样安全：退回确定性序（异常被收敛）。"""
        client = _FakeClient(
            {"metric": {"type": PRIMITIVE_CHOICE, "choice": "ghost", "confidence": 0.99}}
        )
        reranker = MetaReranker(MODEL, popularity={}, chooser=JevEngine(client, "jev-1"))
        top = reranker.rerank(["total_trade_quantity", "trade_count"], "2014 年总成交量是多少？")
        self.assertEqual(top[0], "total_trade_quantity")

    def test_zero_choice_falls_back(self) -> None:
        """choice=None（无法唯一确定）→ 不覆盖。"""
        client = _FakeClient({"metric": {"type": PRIMITIVE_CHOICE, "confidence": 0.99}})
        reranker = MetaReranker(MODEL, popularity={}, chooser=JevEngine(client, "jev-1"))
        top = reranker.rerank(["total_trade_quantity", "trade_count"], "2014 年总成交量是多少？")
        self.assertEqual(top[0], "total_trade_quantity")

    def test_state_is_schema_only(self) -> None:
        """state 只含候选描述——**不得含业务结果数值**（ADR-0030 ② 出境红线）。"""
        client = _FakeClient(
            {"metric": {"type": PRIMITIVE_CHOICE, "choice": "a", "confidence": 0.95}}
        )
        reranker = MetaReranker(MODEL, popularity={}, chooser=JevEngine(client, "jev-1"))
        reranker.rerank(["total_trade_value", "trade_count"], "2014 年总成交金额是多少？")
        self.assertIn("total_trade_value", client.seen_state)
        # 无任何结果数值：state 由候选名/同义词/说明 + 问句拼成
        self.assertNotIn("__CANDIDATES__", client.seen_state)
        self.assertNotIn("__QUESTION__", client.seen_state)

    def test_single_candidate_skips_chooser(self) -> None:
        """单候选无需判别：不触发端点（省一次调用）。"""
        reranker = MetaReranker(MODEL, popularity={}, chooser=JevEngine(_RaisingClient(), "jev-1"))
        self.assertEqual(reranker.rerank(["total_trade_value"], "任意问句"), ["total_trade_value"])


if __name__ == "__main__":
    unittest.main()
