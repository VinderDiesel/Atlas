"""T16 意图奖励函数契约测试。

覆盖：
- IntentEvaluation / IntentReference 数据结构
- reward_intent：严格 0.0/1.0 二值奖励
- 捷径拒绝：一律澄清 / 丢否定 / 搜一切 / 巧合匹配
- 安全违规零奖励
- 正确澄清获奖
"""

from __future__ import annotations

import pytest

# ----------  数据结构  ----------


class TestIntentEvaluation:
    """IntentEvaluation 数据结构。"""

    def test_evaluation_creation(self) -> None:
        from lora.intent_reward import IntentEvaluation

        ev = IntentEvaluation(
            schema_valid=True,
            action="query",
            slots={"metric": "gmv"},
            added_slots=(),
            retrieved_ids=("id-1",),
            budget_ok=True,
            plan_match=True,
            execution_match=True,
            safety_valid=True,
        )
        assert ev.schema_valid is True
        assert ev.action == "query"
        assert ev.safety_valid is True

    def test_evaluation_frozen(self) -> None:
        from lora.intent_reward import IntentEvaluation

        ev = IntentEvaluation(
            schema_valid=True,
            action="query",
            slots={},
            added_slots=(),
            retrieved_ids=(),
            budget_ok=True,
            plan_match=True,
            execution_match=True,
            safety_valid=True,
        )
        with pytest.raises(AttributeError):
            ev.action = "clarify"  # type: ignore[misc]


class TestIntentReference:
    """IntentReference 数据结构。"""

    def test_reference_creation(self) -> None:
        from lora.intent_reward import IntentReference

        ref = IntentReference(
            action="query",
            required_slots={"metric"},
            ambiguity_slots=(),
            target_ids=("id-1",),
        )
        assert ref.action == "query"
        assert ref.required_slots == {"metric"}

    def test_reference_with_ambiguity(self) -> None:
        from lora.intent_reward import IntentReference

        ref = IntentReference(
            action="clarify",
            required_slots=set(),
            ambiguity_slots=("time_range",),
            target_ids=(),
        )
        assert ref.ambiguity_slots == ("time_range",)


# ----------  捷径拒绝（T16 合同核心）  ----------


class TestRewardRejectsShortcuts:
    """捷径不能获奖——T16 合同核心。"""

    @pytest.fixture()
    def reward_cases(self) -> dict:
        """构建 4 种捷径反例。"""
        from lora.intent_reward import IntentEvaluation, IntentReference

        return {
            # 一律澄清：问句明确可答，模型却要求澄清
            "always_clarify": (
                IntentEvaluation(
                    schema_valid=True,
                    action="clarify",
                    slots={},
                    added_slots=(),
                    retrieved_ids=(),
                    budget_ok=True,
                    plan_match=False,
                    execution_match=False,
                    safety_valid=True,
                ),
                IntentReference(
                    action="query",
                    required_slots={"metric", "time_range"},
                    ambiguity_slots=(),
                    target_ids=("m-gmv",),
                ),
            ),
            # 格式合法但丢否定：action/slots 结构对但值错（eq vs neq）
            "drop_negation": (
                IntentEvaluation(
                    schema_valid=True,
                    action="query",
                    slots={
                        "metric": "commission",
                        "filter": {"field": "market", "op": "eq", "value": "NYSE"},
                    },
                    added_slots=(),
                    retrieved_ids=("id-1",),
                    budget_ok=True,
                    plan_match=True,
                    execution_match=True,
                    safety_valid=True,
                ),
                IntentReference(
                    action="query",
                    required_slots={"metric", "filter"},
                    ambiguity_slots=(),
                    target_ids=("id-1",),
                    required_filters=(
                        {"field": "market", "op": "neq", "value": "NYSE"},
                    ),
                ),
            ),
            # 搜一切：超预算检索，不区分目标
            "search_everything": (
                IntentEvaluation(
                    schema_valid=True,
                    action="query",
                    slots={"metric": "gmv"},
                    added_slots=(),
                    retrieved_ids=("id-1", "id-2", "id-3", "id-4", "id-5", "id-6"),
                    budget_ok=False,
                    plan_match=False,
                    execution_match=False,
                    safety_valid=True,
                ),
                IntentReference(
                    action="query",
                    required_slots={"metric"},
                    ambiguity_slots=(),
                    target_ids=("id-1",),
                ),
            ),
            # 巧合匹配：结果碰巧对但计划错——不是真正理解
            "coincidental_equal_result": (
                IntentEvaluation(
                    schema_valid=True,
                    action="query",
                    slots={"metric": "gmv"},
                    added_slots=(),
                    retrieved_ids=("id-1",),
                    budget_ok=True,
                    plan_match=False,
                    execution_match=True,
                    safety_valid=True,
                ),
                IntentReference(
                    action="query",
                    required_slots={"metric"},
                    ambiguity_slots=(),
                    target_ids=("id-1",),
                ),
            ),
        }

    @pytest.mark.parametrize(
        "case",
        [
            "always_clarify",
            "drop_negation",
            "search_everything",
            "coincidental_equal_result",
        ],
    )
    def test_reward_rejects_shortcuts(
        self, case: str, reward_cases: dict
    ) -> None:
        from lora.intent_reward import reward_intent

        candidate, reference = reward_cases[case]
        assert reward_intent(candidate, reference) == 0.0


# ----------  奖励正例  ----------


class TestRewardPositive:
    """正确行为获奖。"""

    def test_perfect_match_gets_reward(self) -> None:
        """完全匹配 → 1.0。"""
        from lora.intent_reward import IntentEvaluation, IntentReference, reward_intent

        ev = IntentEvaluation(
            schema_valid=True,
            action="query",
            slots={"metric": "gmv"},
            added_slots=(),
            retrieved_ids=("id-1",),
            budget_ok=True,
            plan_match=True,
            execution_match=True,
            safety_valid=True,
        )
        ref = IntentReference(
            action="query",
            required_slots={"metric"},
            ambiguity_slots=(),
            target_ids=("id-1",),
        )
        assert reward_intent(ev, ref) == 1.0

    def test_correct_clarify_gets_reward(self) -> None:
        """正确澄清（参考有歧义）→ 1.0。"""
        from lora.intent_reward import IntentEvaluation, IntentReference, reward_intent

        ev = IntentEvaluation(
            schema_valid=True,
            action="clarify",
            slots={},
            added_slots=(),
            retrieved_ids=(),
            budget_ok=True,
            plan_match=True,
            execution_match=True,
            safety_valid=True,
        )
        ref = IntentReference(
            action="clarify",
            required_slots=set(),
            ambiguity_slots=("time_range",),
            target_ids=(),
        )
        assert reward_intent(ev, ref) == 1.0


# ----------  安全与 schema 红线  ----------


class TestRewardRedLines:
    """安全/schema 红线零奖励。"""

    def _base_eval(self, **overrides: bool):
        from lora.intent_reward import IntentEvaluation

        defaults = dict(
            schema_valid=True,
            action="query",
            slots={"metric": "gmv"},
            added_slots=(),
            retrieved_ids=("id-1",),
            budget_ok=True,
            plan_match=True,
            execution_match=True,
            safety_valid=True,
        )
        defaults.update(overrides)
        return IntentEvaluation(**defaults)

    def _base_ref(self):
        from lora.intent_reward import IntentReference

        return IntentReference(
            action="query",
            required_slots={"metric"},
            ambiguity_slots=(),
            target_ids=("id-1",),
        )

    def test_safety_violation_zero_reward(self) -> None:
        """安全违规 → 0.0（即使其他全对）。"""
        from lora.intent_reward import reward_intent

        ev = self._base_eval(safety_valid=False)
        assert reward_intent(ev, self._base_ref()) == 0.0

    def test_schema_invalid_zero_reward(self) -> None:
        """schema 非法 → 0.0。"""
        from lora.intent_reward import reward_intent

        ev = self._base_eval(schema_valid=False)
        assert reward_intent(ev, self._base_ref()) == 0.0

    def test_action_mismatch_zero_reward(self) -> None:
        """action 不匹配 → 0.0。"""
        from lora.intent_reward import IntentEvaluation, IntentReference, reward_intent

        ev = IntentEvaluation(
            schema_valid=True,
            action="query",
            slots={"metric": "gmv"},
            added_slots=(),
            retrieved_ids=("id-1",),
            budget_ok=True,
            plan_match=True,
            execution_match=True,
            safety_valid=True,
        )
        ref = IntentReference(
            action="clarify",
            required_slots=set(),
            ambiguity_slots=("time_range",),
            target_ids=(),
        )
        assert reward_intent(ev, ref) == 0.0

    def test_missing_required_slot_zero_reward(self) -> None:
        """缺必需槽 → 0.0。"""
        from lora.intent_reward import IntentReference, reward_intent

        ev = self._base_eval()
        # 参考需要 metric + time_range，但候选只有 metric
        ref_with_extra = IntentReference(
            action="query",
            required_slots={"metric", "time_range"},
            ambiguity_slots=(),
            target_ids=("id-1",),
        )
        assert reward_intent(ev, ref_with_extra) == 0.0

    def test_added_slot_zero_reward(self) -> None:
        """额外槽 → 0.0。"""
        from lora.intent_reward import IntentEvaluation, reward_intent

        ev = IntentEvaluation(
            schema_valid=True,
            action="query",
            slots={"metric": "gmv", "extra": "bad"},
            added_slots=("extra",),
            retrieved_ids=("id-1",),
            budget_ok=True,
            plan_match=True,
            execution_match=True,
            safety_valid=True,
        )
        assert reward_intent(ev, self._base_ref()) == 0.0

    def test_retrieved_ids_mismatch_zero_reward(self) -> None:
        """检索 ID 不匹配 → 0.0。"""
        from lora.intent_reward import IntentEvaluation, reward_intent

        ev = IntentEvaluation(
            schema_valid=True,
            action="query",
            slots={"metric": "gmv"},
            added_slots=(),
            retrieved_ids=("id-999",),
            budget_ok=True,
            plan_match=True,
            execution_match=True,
            safety_valid=True,
        )
        assert reward_intent(ev, self._base_ref()) == 0.0
