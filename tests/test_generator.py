"""generator 契约测试（Day 31）：确定性校验兜底 + stub 引擎链路。

口径（与 eval/rag_eval.py 一致）：
- Generator 输出 Plan 候选必须过确定性校验（指标注册/维度存在/时间格式）；
- stub 引擎走 Planner（确定性），仅用于验证评测链路，不代表 LLM 能力；
- 本文件不调用网络（openai 引擎测试需要真实端点，不在契约内）。
"""

from __future__ import annotations

import unittest

from agent.compiler import Compiler, SemanticModel
from agent.generator import Generator, _validate_time_json

MODEL = SemanticModel()
COMPILER = Compiler(MODEL)


def stub_gen() -> Generator:
    """stub 引擎（确定性）生成器。"""
    return Generator(MODEL, engine="stub")


class TestStubEngine(unittest.TestCase):
    def test_gold102_plan_roundtrip(self) -> None:
        """gold-102 问句 stub → Plan 与确定性 Planner 一致（路由口径对齐）。"""
        r = stub_gen().generate("按分支统计 2013 年佣金收入，列出前 5 名")
        self.assertIsNotNone(r.plan)
        self.assertFalse(r.refused)
        assert r.plan is not None
        self.assertEqual(r.plan.metric, "commission_revenue")
        self.assertEqual(r.plan.dimensions, ("Branch",))
        assert r.plan.time is not None
        self.assertEqual(r.plan.time.granularity, "year")
        self.assertEqual(r.plan.limit, 5)

    def test_stub_plan_compiles(self) -> None:
        """stub Plan 可编译过 Guard（链路端到端契约）。"""
        r = stub_gen().generate("按分支统计 2013 年佣金收入，列出前 5 名")
        assert r.plan is not None
        sql, _ = COMPILER.compile(r.plan)
        self.assertIn("LIMIT 5", sql)
        self.assertIn("GROUP BY", sql)

    def test_ambiguous_question_refused(self) -> None:
        """歧义问句（gold-122 双指标）→ stub 返回 refusal（不猜）。"""
        r = stub_gen().generate("2013 年成交量和交易额分别是多少？")
        self.assertTrue(r.refused)
        self.assertIsNone(r.plan)


class TestPlanValidation(unittest.TestCase):
    """确定性校验关卡：LLM 编造任何字段都被拒绝。"""

    def _validate(self, obj: dict):
        return Generator(MODEL, engine="stub")._validate_plan(obj)

    def test_valid_plan(self) -> None:
        plan, reason = self._validate(
            {
                "metric": "commission_revenue",
                "dimensions": ["Branch"],
                "time": {"granularity": "year", "value": "2013"},
                "top_n": 5,
            }
        )
        self.assertIsNone(reason)
        assert plan is not None
        self.assertEqual(plan.metric, "commission_revenue")
        self.assertEqual(plan.limit, 5)
        self.assertEqual(plan.order_by[0].column, "commission_revenue")
        self.assertTrue(plan.order_by[0].desc)

    def test_unregistered_metric_rejected(self) -> None:
        """编造/未注册指标 → refuse（N8 同义红线：只认权威清单）。"""
        plan, reason = self._validate({"metric": "gmv", "top_n": None})
        self.assertIsNone(plan)
        self.assertIn("未注册", reason or "")

    def test_unknown_dimension_rejected(self) -> None:
        plan, reason = self._validate(
            {"metric": "commission_revenue", "dimensions": ["Region"], "top_n": None}
        )
        self.assertIsNone(plan)
        self.assertIn("dimension", reason or "")

    def test_bad_time_rejected(self) -> None:
        """相对时间/未知粒度/坏格式 → refuse。"""
        for t in ({"granularity": "week", "value": "1"}, {"granularity": "year", "value": "去年"}):
            plan, _ = self._validate({"metric": "commission_revenue", "time": t, "top_n": None})
            self.assertIsNone(plan)

    def test_bad_top_n_rejected(self) -> None:
        for n in (0, -3, "five", True):
            plan, _ = self._validate({"metric": "commission_revenue", "top_n": n})
            self.assertIsNone(plan)

    def test_time_shapes(self) -> None:
        """四种粒度形态合法（与 gold expected_time 口径一致）。"""
        cases = [
            ({"granularity": "year", "value": 2013}, "2013"),
            ({"granularity": "quarter", "value": "2013q2"}, "2013Q2"),
            ({"granularity": "month", "value": 201307}, "201307"),
            ({"granularity": "date", "value": "2017-07-07"}, "2017-07-07"),
        ]
        for raw, expected in cases:
            ts = _validate_time_json(raw)
            self.assertIsNotNone(ts)
            assert ts is not None
            self.assertEqual(str(ts.value), expected)

    def test_no_time_no_dim(self) -> None:
        plan, reason = self._validate(
            {"metric": "total_trade_value", "dimensions": [], "time": None, "top_n": None}
        )
        self.assertIsNone(reason)
        assert plan is not None
        self.assertIsNone(plan.time)
        self.assertEqual(plan.dimensions, ())


if __name__ == "__main__":
    unittest.main()
