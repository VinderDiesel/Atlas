"""T15 影子策略与运行管理。

职责
----
- ShadowSpec：影子策略规格（候选模型/抽样率/预算）。
- ShadowPolicy：影子策略管理（默认关闭，启用需已批准模型）。
- ShadowRunner：影子执行器（不改变用户结果、不额外跑 SQL、独立 lineage）。

红线
----
- 影子默认关闭。
- 影子不改变用户结果或会话。
- 影子不执行额外在线 SQL。
- 影子运行单独关联父 run，不入用户会话或自动标签。
- 启用未批准模型 → 拒绝。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from serving.control.contracts import Owner

TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ShadowSpec:
    """影子策略规格（不可变）。

    Parameters
    ----------
    candidate_model_id : str
        候选模型 ID（必须是已批准模型）。
    sample_rate : float
        抽样率（0.0 = 不运行，1.0 = 每次运行）。
    budget_limit : int
        影子运行预算上限（token/次数）。
    """

    candidate_model_id: str
    sample_rate: float = 0.0
    budget_limit: int = 100

    @property
    def enabled(self) -> bool:
        """是否启用（sample_rate > 0）。"""
        return self.sample_rate > 0.0


@dataclass
class ShadowResult:
    """影子运行结果。

    与主结果独立，不改变用户答案。
    """

    shadow_run_id: str
    parent_run_id: str
    shadow_model_id: str
    sql_executed: bool = False
    candidate_output: dict[str, Any] = field(default_factory=dict)
    isolated: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(TZ).isoformat(timespec="seconds"))


class ShadowPolicy:
    """影子策略管理器。

    默认关闭；启用需要已批准模型 + 显式批准人。
    """

    def __init__(
        self,
        *,
        approved_model_ids: frozenset[str] | None = None,
    ) -> None:
        """初始化影子策略。

        Parameters
        ----------
        approved_model_ids : frozenset[str] | None
            已批准模型 ID 集合；None 表示接受任意（测试用）。
        """
        self._spec: ShadowSpec | None = None
        self._approver: Owner | None = None
        self._approved_model_ids = approved_model_ids

    @property
    def is_enabled(self) -> bool:
        """影子是否启用。"""
        return self._spec is not None and self._spec.enabled

    def get_active_spec(self) -> ShadowSpec | None:
        """返回当前影子规格（未启用返回 None）。"""
        return self._spec

    def enable(self, spec: ShadowSpec, *, approver: Owner) -> None:
        """启用影子策略。

        Parameters
        ----------
        spec : ShadowSpec
            影子规格。
        approver : Owner
            批准人。

        Raises
        ------
        ValueError
            候选模型未批准。
        """
        # 校验候选模型是否已批准
        if (
            self._approved_model_ids is not None
            and spec.candidate_model_id not in self._approved_model_ids
        ):
                raise ValueError(
                    f"候选模型 {spec.candidate_model_id!r} 未批准；"
                    f"已批准：{sorted(self._approved_model_ids)}"
                )

        self._spec = spec
        self._approver = approver

    def disable(self) -> None:
        """禁用影子策略。"""
        self._spec = None
        self._approver = None


class ShadowRunner:
    """影子执行器：运行候选模型但不改变主结果。

    影子运行：
    - 不执行额外 SQL（sql_executed=False）。
    - 有独立 shadow_run_id（不入用户会话）。
    - 标记为隔离（isolated=True）。
    """

    def __init__(self, policy: ShadowPolicy) -> None:
        self._policy = policy

    def run_shadow(
        self,
        *,
        question: str,
        main_result: dict[str, Any],
        parent_run_id: str,
    ) -> ShadowResult | None:
        """运行影子。

        Parameters
        ----------
        question : str
            用户问句。
        main_result : dict
            主运行结果（不被修改）。
        parent_run_id : str
            父运行 ID。

        Returns
        -------
        ShadowResult | None
            影子结果；影子关闭或抽样率=0 时返回 None。
        """
        spec = self._policy.get_active_spec()
        if spec is None:
            return None

        # 抽样率=0 → 不运行
        if spec.sample_rate <= 0.0:
            return None

        # 影子运行（不执行 SQL，只记录候选模型输出）
        shadow_run_id = f"shadow-{uuid.uuid4().hex[:12]}"
        return ShadowResult(
            shadow_run_id=shadow_run_id,
            parent_run_id=parent_run_id,
            shadow_model_id=spec.candidate_model_id,
            sql_executed=False,
            candidate_output={},  # 实际候选输出待 GPU 环境填充
            isolated=True,
        )
