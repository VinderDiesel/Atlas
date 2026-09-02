"""问句 → Plan 的确定性解析器（AGENTS.md 术语表：Planner，区别于 LLM Generator）

设计原则
--------
1. **确定性优先**：规则 + 语义模型同义词匹配，不经过 LLM；同一问句 → 同一 Plan。
2. **评测对齐**：gold-101~104 的 question 与 expected_* 标注对照（Plan Acc 评测载体）。
3. **歧义即澄清**：无法唯一确定时返回 ClarificationRequest，不猜测（AGENTS.md 决策优先级）。

已知边界（MVP，诚实声明）
------------------------
- 维度解析要求**显式分组结构词**（"按X统计/分组"），且 X 命中 dim_* 维度表的
  非时间字段同义词；"账户/佣金/年"等通用词在无显式结构时不触发维度（避免误分组）
- 相对时间（"上个月/最近"）不支持，返回 ClarificationRequest
  （固定快照评测下相对时间会漂移，见 eval/gold/README.md）
- filter 解析未实现（gold 用例未涉及），Plan.filters 恒为空
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent.compiler import OrderSpec, Plan, SemanticModel, TimeSpec

# 中文数字 → 阿拉伯数字（季度编号）
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4}

# 时间解析正则（顺序敏感：date → quarter → month → year，避免短模式先截获长模式）
_DATE_RE = re.compile(r"(\d{4}) 年 (\d{1,2}) 月 (\d{1,2}) 日")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_QUARTER_RE = re.compile(r"(\d{4}) 年?第?([一二三四])季度")
_ISO_QUARTER_RE = re.compile(r"(\d{4})[Qq]([1-4])")
_MONTH_RE = re.compile(r"(\d{4}) 年 (\d{1,2}) 月")
_YEAR_RE = re.compile(r"(\d{4}) 年")
_RELATIVE_TIME = (
    "上个月",
    "上季度",
    "上月",
    "上旬",
    "去年",
    "今年",
    "最近",
    "本月",
    "本周",
    "昨天",
    "今天",
    "近",
)

# 显式分组结构词："按分支统计 / 按客户等级分组"
_GROUP_RE = re.compile(r"按(.+?)(?:统计|分组|维度|来看|看)")
_TOP_N_RE = re.compile(r"前\s*(\d+)\s*名")


@dataclass(frozen=True)
class ClarificationRequest:
    """歧义澄清请求：Planner 无法确定性解析时返回，Agent 应反问而不是猜（gold-104）。"""

    question: str
    reasons: tuple[str, ...]
    candidates: tuple[str, ...] = ()


class Planner:
    """问句 → Plan 的确定性解析器。"""

    def __init__(self, model: SemanticModel) -> None:
        self.model = model

    def plan(self, question: str) -> Plan | ClarificationRequest:
        """解析问句。返回 Plan；无法唯一确定时返回 ClarificationRequest。"""
        # 1. 指标匹配（同义词子串命中，必须唯一）
        metric_hits = [
            name
            for name, syns in self.model.metric_synonyms.items()
            if any(s in question for s in syns)
        ]
        if not metric_hits:
            return ClarificationRequest(question, ("无法确定指标口径（问句未命中任何指标同义词）",))
        if len(metric_hits) > 1:
            return ClarificationRequest(
                question, ("指标口径歧义（命中多个指标）",), tuple(metric_hits)
            )
        metric = metric_hits[0]

        # 2. 时间解析（相对时间 → 澄清）
        time = self._parse_time(question)
        if isinstance(time, ClarificationRequest):
            return time

        # 3. 维度解析（显式分组结构 + 维度表同义词）
        dimensions = self._parse_dimensions(question)

        # 4. "前 N 名" → 按指标降序 + limit
        order_by, limit = self._parse_top_n(question, metric)

        return Plan(
            metric=metric,
            dimensions=dimensions,
            time=time,
            filters=(),
            order_by=order_by,
            limit=limit,
        )

    # -- 内部实现 ----------------------------------------------------------

    def _parse_time(self, question: str) -> TimeSpec | None | ClarificationRequest:
        """绝对时间解析；相对时间返回 ClarificationRequest。"""
        if any(t in question for t in _RELATIVE_TIME):
            return ClarificationRequest(
                question,
                ("不支持相对时间（固定快照评测下会漂移，请使用绝对日期）",),
            )
        m = _DATE_RE.search(question)
        if m:
            return TimeSpec("date", f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}")
        m = _ISO_DATE_RE.search(question)
        if m:
            return TimeSpec("date", m.group(0))
        m = _QUARTER_RE.search(question)
        if m:
            return TimeSpec("quarter", f"{m.group(1)}Q{_CN_NUM[m.group(2)]}")
        m = _ISO_QUARTER_RE.search(question)
        if m:
            return TimeSpec("quarter", f"{m.group(1)}Q{m.group(2)}")
        m = _MONTH_RE.search(question)
        if m:
            return TimeSpec("month", int(m.group(1)) * 100 + int(m.group(2)))
        m = _YEAR_RE.search(question)
        if m:
            return TimeSpec("year", int(m.group(1)))
        return None

    def _parse_dimensions(self, question: str) -> tuple[str, ...]:
        """显式分组结构（"按X统计/分组"）内的维度表字段匹配。"""
        m = _GROUP_RE.search(question)
        if not m:
            return ()
        phrase = m.group(1)
        hits = [
            name
            for name, syns in self.model.dimension_synonyms.items()
            if any(s in phrase for s in syns)
        ]
        return tuple(hits)

    def _parse_top_n(self, question: str, metric: str) -> tuple[tuple[OrderSpec, ...], int]:
        """ "前 N 名" → 按指标降序 + limit=N；未命中则默认 limit=100。"""
        m = _TOP_N_RE.search(question)
        if not m:
            return (), 100
        return (OrderSpec(metric, desc=True),), int(m.group(1))
