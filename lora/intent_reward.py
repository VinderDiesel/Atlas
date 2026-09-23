"""T16 意图奖励函数（严格 0.0/1.0 二值）。

职责
----
- IntentEvaluation：候选模型输出评估。
- IntentReference：人工真值参考。
- reward_intent：严格二值奖励——全对 1.0，否则 0.0。

红线
----
- 捷径（一律澄清/丢否定/搜一切/巧合匹配）不获奖。
- 安全违规零奖励。
- schema 非法零奖励。
- 不使用模型自评分，只从冻结人工真值与确定性评测生成。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IntentEvaluation:
    """候选模型输出的结构化评估。

    Parameters
    ----------
    schema_valid : bool
        输出 schema 是否合法。
    action : str
        模型选择的动作（query/clarify/unsupported 等）。
    slots : dict[str, Any]
        模型填充的槽位。
    added_slots : tuple[str, ...]
        模型额外添加的槽位（不在参考中）。
    retrieved_ids : tuple[str, ...]
        模型检索到的 ID 列表。
    budget_ok : bool
        检索是否在预算内。
    plan_match : bool
        计划是否与参考一致。
    execution_match : bool
        执行结果是否与参考一致。
    safety_valid : bool
        安全校验是否通过。
    """

    schema_valid: bool
    action: str
    slots: dict[str, Any]
    added_slots: tuple[str, ...]
    retrieved_ids: tuple[str, ...]
    budget_ok: bool
    plan_match: bool
    execution_match: bool
    safety_valid: bool


@dataclass(frozen=True)
class IntentReference:
    """人工真值参考。

    Parameters
    ----------
    action : str
        期望的动作。
    required_slots : set[str]
        必需槽位集合。
    ambiguity_slots : tuple[str, ...]
        歧义槽位（需要澄清时非空）。
    target_ids : tuple[str, ...]
        期望检索到的 ID 列表。
    required_filters : tuple[dict[str, Any], ...]
        必需的过滤器规格（含操作符，用于检测丢否定等捷径）。
    """

    action: str
    required_slots: set[str]
    ambiguity_slots: tuple[str, ...] = ()
    target_ids: tuple[str, ...] = ()
    required_filters: tuple[dict[str, Any], ...] = ()


def reward_intent(
    candidate: IntentEvaluation,
    reference: IntentReference,
) -> float:
    """计算奖励（严格 0.0/1.0）。

    全对返回 1.0，任何一项不满足返回 0.0。
    捷径（一律澄清/丢否定/搜一切/巧合匹配）不获奖。

    Parameters
    ----------
    candidate : IntentEvaluation
        候选模型输出评估。
    reference : IntentReference
        人工真值参考。

    Returns
    -------
    float
        0.0 或 1.0。
    """
    # 安全红线
    if not candidate.safety_valid:
        return 0.0

    # schema 必须合法
    if not candidate.schema_valid:
        return 0.0

    # 预算必须满足
    if not candidate.budget_ok:
        return 0.0

    # action 必须匹配
    if candidate.action != reference.action:
        return 0.0

    # 澄清动作：只有参考确实有歧义时才获奖
    # ——防止"一律澄清"捷径
    if candidate.action == "clarify":
        if not reference.ambiguity_slots:
            return 0.0
        return 1.0

    # 查询动作的完整校验
    if candidate.action == "query":
        # 必需槽位必须全部填充
        if not reference.required_slots.issubset(set(candidate.slots.keys())):
            return 0.0

        # 不能有额外槽位
        if candidate.added_slots:
            return 0.0

        # 检索 ID 必须匹配
        if set(candidate.retrieved_ids) != set(reference.target_ids):
            return 0.0

        # 过滤器规格必须匹配（检测丢否定等值错捷径）
        if reference.required_filters:
            candidate_filters = _extract_filters(candidate.slots)
            if candidate_filters != list(reference.required_filters):
                return 0.0

        # 计划必须匹配（巧合结果不获奖）
        if not candidate.plan_match:
            return 0.0

        # 执行必须匹配
        if not candidate.execution_match:
            return 0.0

        return 1.0

    # 其他动作（unsupported 等）：action 已匹配即获奖
    return 1.0


def _extract_filters(slots: dict[str, Any]) -> list[dict[str, Any]]:
    """从槽位中提取过滤器规格。"""
    filter_val = slots.get("filter")
    if filter_val is None:
        return []
    if isinstance(filter_val, dict):
        return [filter_val]
    if isinstance(filter_val, list):
        return list(filter_val)
    return []
