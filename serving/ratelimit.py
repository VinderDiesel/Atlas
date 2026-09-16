"""端点限流（服务面硬化项 ③，ADR-0011 决策 2 serving/auth 硬化；两桶 ADR-0022 决策 ⑥）。

口径（诚实声明）
--------------------
- **per-token 单维 + 每桶共享**：业务桶（/api/v1/{plan,compile,ask,plan/execute}）
  与治理桶（/api/v1/governance/*）是**两个独立实例**，互不挤占（ADR-0022 决策 ⑥：
  一次治理面板浏览不再烧掉问数配额，反向亦然）；/health 公开、401 无有效 token，
  均不进限流路径。
- 进程内固定窗口计数器（uvicorn workers=1 前提，与会话状态同声明）；
  超限 → 429 + Retry-After（下一窗口起点秒数，HTTP 标准头）。
- **默认值是配置占位，不是实测统计边界**（KL #22 先例口径）：业务桶 60 次/分钟，
  治理桶 240 次/分钟（240 的依据是可算而非可测：治理页挂载 8 请求 + 平均 2 次
  钻取 ≈ 10 请求/次导航 → 240/min ≈ 24 次导航/分钟；不是压测结论）。
  env 覆盖：业务桶 `ATLAS_RATE_LIMIT_*`（现名沿用），治理桶
  `ATLAS_GOVERNANCE_RATE_LIMIT_*`；max=0 或 window=0 → 关闭（恒放行）。
"""

from __future__ import annotations

import math
import os
import time

ENV_MAX = "ATLAS_RATE_LIMIT_MAX"
ENV_WINDOW = "ATLAS_RATE_LIMIT_WINDOW_SECONDS"
# 治理桶独立命名空间（ADR-0022 决策 ⑥）：两个桶的 env 名与默认值互不影响
ENV_GOVERNANCE_MAX = "ATLAS_GOVERNANCE_RATE_LIMIT_MAX"
ENV_GOVERNANCE_WINDOW = "ATLAS_GOVERNANCE_RATE_LIMIT_WINDOW_SECONDS"
# 占位默认（配置化占位非实测阈值——真实容量边界需压测，见 README §9.1）
DEFAULT_MAX_REQUESTS = 60
DEFAULT_WINDOW_SECONDS = 60
DEFAULT_GOVERNANCE_MAX_REQUESTS = 240
DEFAULT_GOVERNANCE_WINDOW_SECONDS = 60


class RateLimiter:
    """进程内固定窗口限流器：token → (窗口起点, 计数)，过期窗口自动重置。"""

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, tuple[int, int]] = {}

    @classmethod
    def from_env(
        cls,
        *,
        env_max: str = ENV_MAX,
        env_window: str = ENV_WINDOW,
        default_max: int = DEFAULT_MAX_REQUESTS,
        default_window: int = DEFAULT_WINDOW_SECONDS,
    ) -> RateLimiter:
        """env 覆盖 + 关闭开关（max=0 或 window=0 = 关）。

        env 名与默认值可参数化（ADR-0022 决策 ⑥ 两桶）：业务桶用缺省参数
        （现名现值，零行为变化），治理桶传治理命名空间与 240/60 占位默认。
        类语义（固定窗口）不因参数化改变。
        """
        return cls(
            int(os.environ.get(env_max, str(default_max))),
            int(os.environ.get(env_window, str(default_window))),
        )

    @classmethod
    def governance_from_env(cls) -> RateLimiter:
        """治理桶（ADR-0022 决策 ⑥）：独立 env 命名空间与 240/60 占位默认。

        与 `from_env()`（业务桶）并列存在而不是复用同一实例：两桶独立计数、
        互不挤占是本类在 v2 的契约。
        """
        return cls.from_env(
            env_max=ENV_GOVERNANCE_MAX,
            env_window=ENV_GOVERNANCE_WINDOW,
            default_max=DEFAULT_GOVERNANCE_MAX_REQUESTS,
            default_window=DEFAULT_GOVERNANCE_WINDOW_SECONDS,
        )

    @property
    def enabled(self) -> bool:
        return self.max_requests > 0 and self.window_seconds > 0

    def check(self, token: str, *, now: float | None = None) -> tuple[bool, int | None]:
        """请求放行判定：窗口内自增计数；超限 → (False, retry_after 秒)。

        固定窗口语义：window_start = floor(now / window) * window；跨窗口首个
        请求自动清零（不保留上一窗口残留计数）。
        """
        if not self.enabled:
            return True, None
        now = time.time() if now is None else now
        window_start = int(now // self.window_seconds) * self.window_seconds
        prev_start, count = self._hits.get(token, (window_start, 0))
        if prev_start != window_start:
            prev_start, count = window_start, 0
        count += 1
        self._hits[token] = (prev_start, count)
        if count > self.max_requests:
            retry_after = max(1, math.ceil(window_start + self.window_seconds - now))
            return False, retry_after
        return True, None
