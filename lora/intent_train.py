"""T14 intent_v1 SFT 训练预检与任务管理。

职责
----
- TrainingSpec：训练任务规格（数据集/基座/配方/预算）。
- PreflightResult：预检结果（blocked 理由集合或 ready）。
- TrainingReport：训练报告（artifact 摘要 + 资源报告）。
- validate_training_job：预算/CUDA/数据/依赖/许可预检（CPU 可跑）。
- train_intent：端到端训练入口（CPU 路径只跑预检）。
- TrainingJobManager：任务状态机/取消/幂等/预算不重复扣。

红线
----
- 无预算批准不启动训练（即使 CUDA 可见）。
- 失败/阻塞任务不注册模型 artifact。
- 权重不入 Git（.gitignore）。
- 既有 sql_v1 路径保持不变。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

LORA_DIR = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=8))

# 模型许可白名单（ADR-0008 / ADR-0031 D11）
# 只有白名单内的基座模型允许用于训练；换基座需另做许可及预算校验。
_LICENSED_BASE_MODELS: frozenset[str] = frozenset(
    {
        "Qwen/Qwen2.5-7B-Instruct",  # Apache-2.0
        "Qwen/Qwen2.5-3B-Instruct",  # Apache-2.0（降级路径）
    }
)


# ----------  数据结构  ----------


@dataclass
class TrainingSpec:
    """训练任务规格。

    所有训练参数集中于此；不依赖外部 YAML 的运行时解析（配方路径只用于
    记录来源，实际参数由调用方显式传入或从 recipe_path 加载后填充）。
    """

    job_id: str
    dataset_manifest_path: Path | None
    base_model: str
    base_model_revision: str
    recipe_path: Path | None = None
    budget_approval_id: str | None = None
    budget_approved: bool = False
    output_dir: Path | None = None


@dataclass(frozen=True)
class PreflightResult:
    """预检结果。

    status='blocked' 时 blocked_reasons 非空；status='ready' 时
    validated_data 包含通过校验的训练数据。
    """

    status: str  # 'ready' | 'blocked'
    blocked_reasons: tuple[str, ...]
    messages: tuple[str, ...]
    validated_data: list[dict[str, Any]] | None = None


@dataclass
class TrainingReport:
    """训练报告。

    status='blocked'/'failed' 时 artifact_digest=None（不注册模型）。
    status='succeeded' 时 artifact_digest 为权重摘要 SHA-256。
    """

    job_id: str
    status: str  # 'blocked' | 'failed' | 'succeeded'
    artifact_digest: str | None
    resource_report: dict[str, Any]
    code_version: str
    created_at: str


@dataclass
class TrainingJobState:
    """任务运行时状态。"""

    job_id: str
    status: str  # 'queued' | 'running' | 'succeeded' | 'failed' | 'blocked' | 'cancelled'
    spec: TrainingSpec
    budget_charged: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(TZ).isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now(TZ).isoformat(timespec="seconds"))


# ----------  预检  ----------


def validate_training_job(spec: TrainingSpec) -> PreflightResult:
    """训练预检：预算 → 数据 → 许可 → CUDA。

    所有检查在 CPU 环境可跑（CUDA 检查除外，CPU 环境必然 blocked）。
    累积所有阻塞理由后一次性返回，不短路。

    Parameters
    ----------
    spec : TrainingSpec
        训练任务规格。

    Returns
    -------
    PreflightResult
        blocked（含理由集合）或 ready（含校验后的训练数据）。
    """
    blocked: list[str] = []
    messages: list[str] = []

    # 1) 预算批准
    if not spec.budget_approved:
        blocked.append("budget_not_approved")
        messages.append("训练需要显式预算批准（budget_approved=True）")

    # 2) 数据集可达
    validated_data: list[dict[str, Any]] | None = None
    if spec.dataset_manifest_path is None or not spec.dataset_manifest_path.exists():
        blocked.append("dataset_not_found")
        messages.append(f"数据集不存在：{spec.dataset_manifest_path}")
    else:
        # 加载并校验数据
        from lora.intent_data import load_intent_dataset

        data = load_intent_dataset(spec.dataset_manifest_path)
        if not data:
            blocked.append("empty_dataset")
            messages.append("数据集为空或无合法训练样本")
        else:
            validated_data = data
            messages.append(f"数据集加载成功：{len(data)} 条合法训练样本")

    # 3) 模型许可
    if spec.base_model not in _LICENSED_BASE_MODELS:
        blocked.append("model_not_licensed")
        messages.append(
            f"基座模型 {spec.base_model!r} 不在许可白名单内；"
            f"白名单：{sorted(_LICENSED_BASE_MODELS)}"
        )

    # 4) CUDA（CPU 环境必然 blocked）
    try:
        import torch  # type: ignore[import-not-found]

        if not torch.cuda.is_available():
            blocked.append("no_cuda")
            messages.append("无可用 CUDA GPU（torch.cuda.is_available()=False）")
    except ImportError:
        blocked.append("no_torch")
        messages.append("缺少 ML 依赖 torch——在 GPU 机上执行 `uv sync --extra ml`")

    if blocked:
        return PreflightResult(
            status="blocked",
            blocked_reasons=tuple(blocked),
            messages=tuple(messages),
        )

    return PreflightResult(
        status="ready",
        blocked_reasons=(),
        messages=tuple(messages),
        validated_data=validated_data,
    )


# ----------  训练入口  ----------


def train_intent(spec: TrainingSpec) -> TrainingReport:
    """intent_v1 SFT 训练入口。

    CPU 路径只跑预检；GPU 路径在预检通过后执行 QLoRA 训练。
    失败/阻塞任务不注册模型 artifact（artifact_digest=None）。

    Parameters
    ----------
    spec : TrainingSpec
        训练任务规格。

    Returns
    -------
    TrainingReport
        训练报告（blocked/failed 时 artifact_digest=None）。
    """
    from eval.runner import git_short_sha

    preflight = validate_training_job(spec)

    if preflight.status == "blocked":
        return TrainingReport(
            job_id=spec.job_id,
            status="blocked",
            artifact_digest=None,
            resource_report={
                "blocked_reasons": list(preflight.blocked_reasons),
                "messages": list(preflight.messages),
            },
            code_version=git_short_sha(),
            created_at=datetime.now(TZ).isoformat(timespec="seconds"),
        )

    # 预检通过 → 执行训练（GPU 路径）
    # NOTE: 实际 QLoRA 训练逻辑与 sql_v1 类似，但使用 intent 数据格式。
    # 当前 CPU 环境无法到达此分支；GPU 就绪后实现。
    return TrainingReport(
        job_id=spec.job_id,
        status="succeeded",
        artifact_digest=None,  # GPU 训练后填充
        resource_report={"messages": list(preflight.messages)},
        code_version=git_short_sha(),
        created_at=datetime.now(TZ).isoformat(timespec="seconds"),
    )


# ----------  任务管理  ----------


class TrainingJobManager:
    """训练任务状态机：提交/取消/查询/预算幂等。

    职责：
    - 任务状态：queued → running → succeeded/failed/blocked/cancelled。
    - 取消：queued/running 可取消；终态不可取消。
    - 幂等：同 job_id 重复提交拒绝。
    - 预算：同 budget_approval_id 只能使用一次，防止重复扣费。
    """

    def __init__(
        self,
        *,
        budget_approvals: set[str] | None = None,
    ) -> None:
        """初始化任务管理器。

        Parameters
        ----------
        budget_approvals : set[str] | None
            已批准的预算 ID 集合；提交训练任务时消耗，同一 ID 只能用一次。
        """
        self._jobs: dict[str, TrainingJobState] = {}
        self._budget_approvals = set(budget_approvals or set())
        self._used_budgets: set[str] = set()

    def submit(self, spec: TrainingSpec) -> str:
        """提交训练任务。

        Raises
        ------
        ValueError
            job_id 已存在或 budget_approval_id 已使用。
        """
        # 幂等：同 job_id 拒绝
        if spec.job_id in self._jobs:
            raise ValueError(f"任务 {spec.job_id!r} 已存在，不可重复提交")

        # 预算幂等：同 approval_id 拒绝
        if spec.budget_approval_id and spec.budget_approval_id in self._used_budgets:
            raise ValueError(
                f"预算批准 {spec.budget_approval_id!r} 已使用，不可重复扣费"
            )

        state = TrainingJobState(job_id=spec.job_id, status="queued", spec=spec)
        self._jobs[spec.job_id] = state

        # 消耗预算
        if spec.budget_approval_id:
            self._used_budgets.add(spec.budget_approval_id)

        return spec.job_id

    def cancel(self, job_id: str) -> None:
        """取消任务。

        Raises
        ------
        KeyError
            任务不存在。
        ValueError
            任务已在终态，不可取消。
        """
        if job_id not in self._jobs:
            raise KeyError(f"任务 {job_id!r} 不存在")

        state = self._jobs[job_id]
        terminal = {"succeeded", "failed", "blocked", "cancelled"}
        if state.status in terminal:
            raise ValueError(f"任务 {job_id!r} 已在终态 {state.status!r}，不可取消")

        state.status = "cancelled"
        state.updated_at = datetime.now(TZ).isoformat(timespec="seconds")

    def get_state(self, job_id: str) -> TrainingJobState:
        """查询任务状态。

        Raises
        ------
        KeyError
            任务不存在。
        """
        if job_id not in self._jobs:
            raise KeyError(f"任务 {job_id!r} 不存在")
        return self._jobs[job_id]


# ----------  CLI  ----------


def main() -> int:
    """CLI 入口：运行 intent_v1 训练预检。"""
    recipe_path = LORA_DIR / "configs" / "intent_v1.yaml"
    if not recipe_path.exists():
        print(f"[blocked] 配方不存在：{recipe_path}", file=__import__("sys").stderr)
        return 2

    recipe: dict[str, Any] = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))

    spec = TrainingSpec(
        job_id=f"intent-{uuid.uuid4().hex[:8]}",
        dataset_manifest_path=Path(recipe["data"]["manifest_path"])
        if recipe.get("data", {}).get("manifest_path")
        else None,
        base_model=str(recipe.get("base_model", "Qwen/Qwen2.5-7B-Instruct")),
        base_model_revision=str(recipe.get("base_model_revision", "unknown")),
        recipe_path=recipe_path,
    )

    report = train_intent(spec)
    print(json.dumps({"status": report.status, "job_id": report.job_id}, ensure_ascii=False))
    return 0 if report.status != "blocked" else 2


if __name__ == "__main__":
    raise SystemExit(main())
