"""LoRA 语料飞轮 + 影子推理测试（不需 GPU，CPU 可跑）。

- 蒸馏：build_pairs --distill 用确定性编译器作 teacher 生成 SFT 语料，合法且不碰 gold。
- 影子推理：lora/infer.py 在未配置 LoRA 端点时回落确定性编译器，全链路可执行。
"""

from __future__ import annotations

import unittest

from agent.compiler import SemanticModel
from agent.generator import validate_plan_json
from lora.build_pairs import build_distillation_pairs, gold_templates, metric_aliases
from lora.infer import LoRAGenerator


class TestDistillationCorpus(unittest.TestCase):
    def setUp(self) -> None:
        self.model = SemanticModel()
        self.protected = gold_templates(self.model)
        self.aliases = metric_aliases(self.model)

    def test_distillation_produces_valid_corpus(self) -> None:
        stats, kept = build_distillation_pairs(self.model, self.protected, self.aliases)
        self.assertGreater(stats.kept, 0, "蒸馏应产出非空合规语料（去空语料）")
        for row in kept:
            obj = __import__("json").loads(row["answer"])
            plan, reason = validate_plan_json(self.model, obj)
            self.assertIsNotNone(plan, reason)

    def test_distillation_avoids_gold_leak(self) -> None:
        _stats, kept = build_distillation_pairs(self.model, self.protected, self.aliases)
        for row in kept:
            self.assertNotIn(row["question"], self.protected)


class TestShadowInference(unittest.TestCase):
    def setUp(self) -> None:
        self.gen = LoRAGenerator(SemanticModel())

    def test_registered_question_compiles_via_shadow(self) -> None:
        result = self.gen.generate("2013 年第二季度总交易额是多少？")
        self.assertFalse(result.generator_used)  # 影子兜底，非真·LoRA
        self.assertIsNotNone(result.sql)
        self.assertIsNone(result.refusal)

    def test_unregistered_question_refuses(self) -> None:
        result = self.gen.generate("2013 年第二季度股票买卖总金额")
        self.assertIsNotNone(result.refusal)


if __name__ == "__main__":
    unittest.main()
