"""业务端点限流（服务面硬化项 ③，ADR-0011 决策 2 serving/auth 硬化）。

口径（诚实声明）
--------------------
- **per-token 单维 + 全业务端点共享桶**：/plan /compile /ask 同桶计数
  （/health 公开、401 无有效 token，均不进限流路径）。
- 进程内固定窗口计数器（uvicorn workers=1 前提，与会话状态同声明）；
  超限 → 429 + Retry-After（下一窗口起点秒数，HTTP 标准头）。
- **默认值是配置占位，不是实测统计边界**（KL #22 先例口径）：60 次/分钟，
  env `ATLAS_RATE_LIMIT_MAX` / `ATLAS_RATE_LIMIT_WINDOW_SECONDS` 覆盖，
  max=0 或 window=0 → 关闭（恒放行）。
"""

from __future__ import annotations

import math
import os
import time

ENV_MAX = "ATLAS_RATE_LIMIT_MAX"
ENV_WINDOW = "ATLAS_RATE_LIMIT_WINDOW_SECONDS"
# 占位默认（配置化占位非实测阈值——真实容量边界需压测，见 README §9.1）
DEFAULT_MAX_REQUESTS = 60
DEFAULT_WINDOW_SECONDS = 60


class RateLimiter:
    """进程内固定窗口限流器：token → (窗口起点, 计数)，过期窗口自动重置。"""

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, tuple[int, int]] = {}

    @classmethod
    def from_env(cls) -> RateLimiter:
        """env 覆盖 + 关闭开关（ATLAS_RATE_LIMIT_MAX=0 或 _WINDOW_SECONDS=0 = 关）。"""
        return cls(
            int(os.environ.get(ENV_MAX, str(DEFAULT_MAX_REQUESTS))),
            int(os.environ.get(ENV_WINDOW, str(DEFAULT_WINDOW_SECONDS))),
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
