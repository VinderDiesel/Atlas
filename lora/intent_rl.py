"""T16 GRPO 预检与训练入口。

职责
----
- GRPOSpec：GRPO 训练规格（含 SFT 基线冻结、奖励用例路径）。
- GRPOPreflightResult：预检结果。
- validate_grpo_preflight：前置门禁检查。

红线
----
- SFT 基线未冻结 → blocked。
- 无奖励用例 → blocked。
- 预算未批准 → blocked。
- 未许可基座模型 → blocked。
- 不使用模型自评分，只从冻结人工真值生成奖励。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# 模型许可白名单（与 intent_train.py / registry.py 一致）
_LICENSED_BASE_MODELS: frozenset[str] = frozenset(
    {
        "Qwen/Qwen2.5-7B-Instruct",  # Apache-2.0
        "Qwen/Qwen2.5-3B-Instruct",  # Apache-2.0（降级路径）
    }
)


@dataclass(frozen=True)
class GRPOSpec:
    """GRPO 训练规格。

    Parameters
    ----------
    job_id : str
        任务唯一标识。
    sft_baseline_frozen : bool
        SFT 基线是否已冻结。
    reward_cases_path : Path | None
        奖励用例文件路径。
    budget_approved : bool
        预算是否已批准。
    base_model : str
        基座模型名称。
    dataset_manifest_path : Path | None
        数据集 manifest 路径。
    recipe_path : Path | None
        配方文件路径。
    """

    job_id: str
    sft_baseline_frozen: bool
    reward_cases_path: Path | None
    budget_approved: bool
    base_model: str
    dataset_manifest_path: Path | None = None
    recipe_path: Path | None = None


@dataclass(frozen=True)
class GRPOPreflightResult:
    """GRPO 预检结果。

    Parameters
    ----------
    status : str
        'ready' 或 'blocked'。
    blocked_reasons : tuple[str, ...]
        阻塞理由集合。
    messages : tuple[str, ...]
        附加消息。
    """

    status: str
    blocked_reasons: tuple[str, ...]
    messages: tuple[str, ...] = ()


def validate_grpo_preflight(spec: GRPOSpec) -> GRPOPreflightResult:
    """GRPO 前置门禁检查。

    累积所有阻塞理由后一次性返回。

    Parameters
    ----------
    spec : GRPOSpec
        GRPO 训练规格。

    Returns
    -------
    GRPOPreflightResult
        预检结果。
    """
    blocked: list[str] = []

    # SFT 基线必须冻结
    if not spec.sft_baseline_frozen:
        blocked.append("sft_not_frozen")

    # 必须有奖励用例
    if spec.reward_cases_path is None:
        blocked.append("no_reward_cases")

    # 预算必须批准
    if not spec.budget_approved:
        blocked.append("budget_not_approved")

    # 基座模型必须许可
    if spec.base_model not in _LICENSED_BASE_MODELS:
        blocked.append("unlicensed_model")

    if blocked:
        return GRPOPreflightResult(
            status="blocked",
            blocked_reasons=tuple(blocked),
            messages=(f"GRPO preflight blocked: {', '.join(blocked)}",),
        )

    return GRPOPreflightResult(
        status="ready",
        blocked_reasons=(),
        messages=("GRPO preflight passed",),
    )
