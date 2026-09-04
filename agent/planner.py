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
  （固定快照评测下相对时间会漂移，见 eval/gold/README.md；ADR-0014 ③ 设计性不支持）
- 指标匹配：同义词子串命中必须唯一；派生指标同义词常含基础指标词
  （"平均每笔成交金额" ⊃ "成交金额"）→ 按**最长命中**取更具体口径
  （gold-156~162 派生指标补洞评测先行发现的真实缺陷，2026-09-04 修复）；
  互不为子串的多命中（gold-122/148 双指标问句）仍歧义反问
- filter 解析（ADR-0014 ①）：支持维度值等值（"只看/仅统计 X"）、排除
  （"排除/不含 X"）、度量阈值（"超过/低于 N"，HAVING 语义）；值含中文或
  过滤短语无法归属维度字段时不产生 filter（仅当维度词命中而**值**模糊时才
  反问，如 gold-155「核心分支」）；自由双指标比较不支持
- 指代消解（ADR-0014 ②，多轮追问）：followup() 仅处理**同构残句**——全量
  解析 unmatched（句内无指标词）且命中链接词形态（"那 X 呢 / 换成 X /
  按 X 呢"）时，复用上轮 Plan 的 metric/过滤/排序结构，只替换本轮解析出的
  时间/维度片段；自由代词（"它/这些"）与无法归属的碎片 → 反问完整重述，
  不猜测（换维 + 上轮带维度值过滤 = 口径作用域二义，同样反问）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from agent.compiler import Filter, OrderSpec, Plan, SemanticModel, TimeSpec

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

# filter 触发结构（ADR-0014 ①；顺序无关，逐形态独立扫描）
# - 维度值等值/排除：前缀词 + 目标短语，到分隔符截断
_EQ_FILTER_RE = re.compile(r"(?:只看|只统计|仅统计|仅看|仅保留|仅取)\s*(.+?)(?=的|，|,|$)")
_EXC_FILTER_RE = re.compile(r"(?:排除|不含|除去)\s*(.+?)(?=后|的|外|，|,|$)")
# - 度量阈值（HAVING 语义）：比较词 + 数值 + 可选中文量级（"1000 万"）
_THRESHOLD_GT_RE = re.compile(r"(?:超过|大于|高于|不小于|不低于)\s*([\d.]+)\s*(亿|千万|百万|万)?")
_THRESHOLD_LT_RE = re.compile(r"(?:低于|小于|不足|不超过|不高于)\s*([\d.]+)\s*(亿|千万|百万|万)?")
# 量级词须先匹配长形（千万 → 万），regex 交替顺序即优先级
_CN_UNIT = {"亿": 100_000_000, "千万": 10_000_000, "百万": 1_000_000, "万": 10_000}

# 指代追问（ADR-0014 ②；仅全量解析 unmatched 的残句才进入，见 followup()）：
# - 链接词开头（那/那么/换成/改成/改为/按）或"呢"结尾 = 口语残句形态
_FOLLOWUP_PREFIXES = ("那", "那么", "换成", "改成", "改为", "按")
# - 换维壳：换成/改成/改为/按 引导的短语（可带"那"前缀与"统计/分组/呢"尾缀）；
#   $ 锚防非贪婪截断过早（"按客户等级统计呢"须整体消费到句尾）
_FOLLOWUP_DIM_RE = re.compile(
    r"(?:那|那么)?(?:换成|改成|改为|按)\s*(.+?)(?:统计|分组)?\s*呢?\s*$"
)


@dataclass(frozen=True)
class ClarificationRequest:
    """歧义澄清请求：Planner 无法确定性解析时返回，Agent 应反问而不是猜（gold-104）。

    kind 语义（Day 43 图路由用，见 agent/graph.py）：
    - ambiguous：问句命中多个指标口径，歧义，反问（不可安全路由到候选链）
    - relative_time：相对时间（固定快照下会漂移），反问绝对时间
    - unmatched：未命中任何指标同义词（新措辞/超范围）——图可配置走候选链
      （retrieve → generate → validate），默认也是反问 + 附检索候选。
    """

    question: str
    reasons: tuple[str, ...]
    candidates: tuple[str, ...] = ()
    kind: Literal["ambiguous", "relative_time", "unmatched"] = "ambiguous"


class Planner:
    """问句 → Plan 的确定性解析器。"""

    def __init__(self, model: SemanticModel) -> None:
        self.model = model

    def plan(self, question: str) -> Plan | ClarificationRequest:
        """解析问句。返回 Plan；无法唯一确定时返回 ClarificationRequest。"""
        # 1. 指标匹配（同义词子串命中，必须唯一）
        # 子串包含消歧：派生指标同义词含基础指标词（"平均每笔成交金额" ⊃
        # "成交金额"）时，长命中是更具体口径（派生），短命中是冗余命中——
        # 丢弃被其他命中词真包含的命中词；互不为子串的多命中仍保留（真歧义）。
        metric_hits = sorted(
            {
                name
                for name, syns in self.model.metric_synonyms.items()
                for syn in syns
                if syn in question
                and not any(
                    syn in other and syn != other
                    for other_name, other_syns in self.model.metric_synonyms.items()
                    for other in other_syns
                    if other in question
                )
            }
        )
        if not metric_hits:
            return ClarificationRequest(
                question,
                ("无法确定指标口径（问句未命中任何指标同义词）",),
                kind="unmatched",
            )
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

        # 4. filter 解析（ADR-0014 ①）；值模糊 → 反问
        filters = self._parse_filters(question, metric)
        if isinstance(filters, ClarificationRequest):
            return filters

        # 5. "前 N 名" → 按指标降序 + limit
        order_by, limit = self._parse_top_n(question, metric)

        return Plan(
            metric=metric,
            dimensions=dimensions,
            time=time,
            filters=filters,
            order_by=order_by,
            limit=limit,
        )

    def followup(
        self, question: str, prev: Plan
    ) -> Plan | ClarificationRequest | None:
        """指代追问补全（ADR-0014 ②）：残句无指标词 → 复用上轮 Plan 结构。

        仅处理**同构追问**（调用方保证：planner.plan 已 unmatched——句内无指标
        词，且 prev 为同会话上轮成功采纳的 Plan）：命中链接词形态（"那 X 呢 / 换成
        X / 按 X 统计"等）时，继承 prev 的 metric/维度/过滤/排序，只替换本轮新
        解析出的时间/维度片段。返回 None = 非链接形态（调用方维持原 unmatched
        流程）；ClarificationRequest = 补全歧义/相对时间（不猜，反问完整重述）。

        确定性边界（不猜测）：
        - 自由代词（"那它呢/这些呢"）与剥壳后无片段 → 反问
        - 相对时间（"那去年呢"）→ 透传 relative_time 反问
        - 换维且上轮带维度值过滤 → 口径作用域二义（保留=子集，去掉=改口径）→ 反问
        """
        text = question.strip().strip("？?。!！，, ")
        # 链接词开头或"呢"结尾才算口语残句（否则维持原 unmatched 流程）
        if not (text.startswith(_FOLLOWUP_PREFIXES) or text.endswith("呢")):
            return None
        # 时间片段：壳词不影响既有时间正则；相对时间 → 透传澄清
        time = self._parse_time(text)
        if isinstance(time, ClarificationRequest):
            return time
        # 维度片段：换维壳命中后做维度字段子串匹配（多命中全取，与分组解析同风格）；
        # 壳误吃时间短语（"换成 2014 年"）时 new_dims 为空 → 不算换维，回落仅换时间
        new_dims: tuple[str, ...] = ()
        m = _FOLLOWUP_DIM_RE.search(text)
        if m:
            new_dims = self._dim_hits(m.group(1))
            if new_dims and prev.filters:
                return ClarificationRequest(
                    question,
                    (f"追问「{text}」要更换分组维度，但上轮口径带维度值过滤"
                     "（过滤作用域无法确定），请完整重述问句",),
                )
        # 无任何可替换片段（自由代词）→ 反问不猜
        if time is None and not new_dims:
            return ClarificationRequest(
                question,
                (f"追问「{text}」没有识别到可替换的时间/维度片段"
                 "（自由代词指代不支持），请完整重述问句",),
            )
        return Plan(
            metric=prev.metric,
            dimensions=new_dims if new_dims else prev.dimensions,
            time=time if isinstance(time, TimeSpec) else prev.time,
            filters=prev.filters,
            order_by=prev.order_by,
            limit=prev.limit,
        )

    # -- 内部实现 ----------------------------------------------------------

    def _parse_time(self, question: str) -> TimeSpec | None | ClarificationRequest:
        """绝对时间解析；相对时间返回 ClarificationRequest。"""
        if any(t in question for t in _RELATIVE_TIME):
            return ClarificationRequest(
                question,
                ("不支持相对时间（固定快照评测下会漂移，请使用绝对日期）",),
                kind="relative_time",
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
        return self._dim_hits(m.group(1))

    def _dim_hits(self, phrase: str) -> tuple[str, ...]:
        """短语内命中的维度字段（dim_* 非时间字段同义词子串，多命中全取）。"""
        return tuple(
            name
            for name, syns in self.model.dimension_synonyms.items()
            if any(s in phrase for s in syns)
        )

    def _parse_filters(
        self, question: str, metric: str
    ) -> tuple[Filter, ...] | ClarificationRequest:
        """filter 解析（ADR-0014 ①）。返回顺序固定：度量阈值在前，维度值在后。

        维度词命中而**值**模糊（含中文/为空）→ ClarificationRequest（gold-155）；
        过滤短语整体无法归属任何维度字段时不产生 filter（"只看/仅"等前缀
        也可能是强调语，不做过度澄清）——两类不猜测边界见模块 docstring。
        """
        filters: list[Filter] = []
        m = _THRESHOLD_GT_RE.search(question)
        if m:
            filters.append(Filter(metric, ">", self._cn_number(m.group(1), m.group(2))))
        m = _THRESHOLD_LT_RE.search(question)
        if m:
            filters.append(Filter(metric, "<", self._cn_number(m.group(1), m.group(2))))
        for prefix_re, op in ((_EQ_FILTER_RE, "="), (_EXC_FILTER_RE, "!=")):
            m = prefix_re.search(question)
            if not m:
                continue
            matched = self._match_dim_value(m.group(1))
            if matched is None:
                continue
            field, raw_value, has_prefix = matched
            if has_prefix:
                return ClarificationRequest(
                    question,
                    (f"「{m.group(1)}」的取值无法唯一确定"
                     "（维度词前带修饰，请给出精确值）",),
                )
            if raw_value == "":
                return ClarificationRequest(
                    question, (f"过滤目标缺少取值：{m.group(1)!r}，请给出精确值",)
                )
            if any("\u4e00" <= ch <= "\u9fff" for ch in raw_value):
                return ClarificationRequest(
                    question,
                    (f"「{raw_value}」无法对应到 {field} 的已注册值/同义词"
                     "（中文维度值暂不支持，请给出精确值）",),
                )
            value: str | int = raw_value
            if re.fullmatch(r"\d+", raw_value):
                value = int(raw_value)
            filters.append(Filter(field, op, value))
        return tuple(filters)

    def _match_dim_value(self, phrase: str) -> tuple[str, str, bool] | None:
        """过滤短语 → (维度字段名, 取值原文, 维度词前是否有修饰)。

        返回 None = 短语不含任何维度词（"只看/仅"可能是强调语，不产生 filter）；
        维度词前带修饰（如「核心分支」）→ has_prefix=True——修饰词可能是口语
        定语，值无法确定性确认（gold-155 反问载体），由调用方澄清。
        多 syn 命中取最长（"客户等级" 优先 "客户"）。
        """
        best: tuple[int, str, str, int] | None = None  # (syn 长, 字段名, 命中 syn, 位置)
        for field, syns in self.model.dimension_synonyms.items():
            for syn in syns:
                pos = phrase.find(syn)
                if pos >= 0 and (best is None or len(syn) > best[0]):
                    best = (len(syn), field, syn, pos)
        if best is None:
            return None
        _, field, syn, pos = best
        return field, phrase[pos + len(syn) :].strip(), pos > 0

    @staticmethod
    def _cn_number(num: str, unit: str | None) -> int | float:
        """"1000 万" → 10000000；有小数保留 float（1.5 亿 → 150000000.0 规约 int）。"""
        value = float(num) * (_CN_UNIT[unit] if unit else 1)
        return int(value) if value.is_integer() else value

    def _parse_top_n(self, question: str, metric: str) -> tuple[tuple[OrderSpec, ...], int]:
        """ "前 N 名" → 按指标降序 + limit=N；未命中则默认 limit=100。"""
        m = _TOP_N_RE.search(question)
        if not m:
            return (), 100
        return (OrderSpec(metric, desc=True),), int(m.group(1))
