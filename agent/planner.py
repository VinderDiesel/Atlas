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
- 语言（P6 locale 化，2026-09-05）：形态词表/正则按 locale 分片（zh/en），
  语言自动检测（句中含任意中文字符 → zh，否则 → en），CLI/API 无需显式参数；
  评测 runner 按样本 tags lang_en 显式传 locale（确定性优先）。英文同义词
  外置在 `semantic/synonyms/en_us.yml`（ADR-0015，由 compiler.load_locale_synonyms
  加载；YAML ai_context 是中文注记域，英文措辞属解析器形态层）；中英形态
  触发词（时间/分组/TopN/filter/追问）外置在 `semantic/synonyms/patterns_<locale>.yml`
  （ADR-0015 §②：B3a 中文 / B3b 英文，**纯搬运**，由 compiler.load_locale_patterns
  加载；ISO 日期/季度中英共享，英文侧以 `ref` 引用中文词典，不复制第二份）
- 英文 filter 边界：值须为**精确值**（"only for branch X"），裸实体复数词
  （customers/branches 等）作值后缀裁剪；无维度词短语（强调语）不产生 filter，
  与中文同构（见 _EN_ONLY_RE/_EN_EXCL_RE）
- 指标匹配：同义词子串命中必须唯一；派生指标同义词常含基础指标词
  （"平均每笔成交金额" ⊃ "成交金额"）→ 按**最长命中**取更具体口径
  （gold-156~162 派生指标补洞评测先行发现的真实缺陷，2026-09-04 修复）；
  互不为子串的多命中（gold-122/148 双指标问句）仍歧义反问
- filter 解析（ADR-0014 ①）：支持维度值等值（"只看/仅统计 X"）、排除
  （"排除/不含 X"）、度量阈值（"超过/低于 N"，HAVING 语义）；值含中文或
  过滤短语无法归属维度字段时不产生 filter（仅当维度词命中而**值**模糊时才
  反问，如 gold-155「核心分支」）；自由双指标比较不支持
- filter 值域校验（ADR-0016，批次 B4）：维度值 filter 的值在归属阶段对
  `semantic/values/` 的快照值域做校验——命中值本体 → 通过；命中人工别名或
  大小写折叠唯一命中 → **归一**（记录由 `plan_with_notices()` 返回，不改值本体
  类型）；既非值也非别名（含折叠多命中）→ ClarificationRequest 附候选值样例
  （与 KL #29 同一条原则，零新机制）。未注册与被跳过的列（大基数）**不校验**，
  值原样透传（安全默认：不为无证据的列制造反问，见 agent/value_domain.py）
- 指代消解（ADR-0014 ②，多轮追问）：followup() 仅处理**同构残句**——全量
  解析 unmatched（句内无指标词）且命中链接词形态（"那 X 呢 / 换成 X /
  按 X 呢"）时，复用上轮 Plan 的 metric/过滤/排序结构，只替换本轮解析出的
  时间/维度片段；自由代词（"它/这些"）与无法归属的碎片 → 反问完整重述，
  不猜测（换维 + 上轮带维度值过滤 = 口径作用域二义，同样反问）
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent import value_domain
from agent.compiler import (
    ComparisonSpec,
    Filter,
    OrderSpec,
    Plan,
    SemanticModel,
    TimeSpec,
    load_locale_patterns,
    load_locale_synonyms,
)

# ---------------------------------------------------------------------------
# 中文形态（zh，P6 locale 化后仍为主路径；行为与 2026-09-04 前逐字一致）
#
# 形态触发词已从本文件外置到 `semantic/synonyms/patterns_zh_cn.yml`（ADR-0015 §②，
# 批次 B3a）——**纯搬运**：常量名与类型不变，值来自词典，解析算法本体未动。
# 因此新场景增删触发词只改 YAML，不改 planner（B7 前提）。逐条等价性证明与
# 验收口径见 docs/design/adr-0015-pattern-lexicon-zh.md。
# ---------------------------------------------------------------------------

_ZH_PATTERNS = load_locale_patterns("zh_cn")

# 量级与中文编号映射（词典 magnitude 节）
# - 量级词优先匹配长形（千万 → 万）由 threshold 正则的交替顺序保证，
#   本表只做倍率换算，不参与匹配顺序
_CN_UNIT = _ZH_PATTERNS["magnitude"]["cn_units"]
# - 中文数字 → 阿拉伯数字（季度编号）；季度正则的取组仅限本表键，
#   若 YAML 改了 `[一二三四]` 而本表未跟进，解析会响亮报错（不静默降级）
_CN_NUM = _ZH_PATTERNS["magnitude"]["cn_numerals"]

# 时间形态：`time.patterns` 是**有序** (kind, 已编译正则) 列表，解析按声明顺序
# 逐个尝试（短模式若先跑会截获长模式的输入，"2013 年 7 月"会被读成年份）。
_TIME_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = _ZH_PATTERNS["time"]["patterns"]


def _tp_date(m: re.Match[str]) -> TimeSpec:
    """中文全写日期"2013 年 7 月 5 日"→ 月/日补零后作 ISO date。"""
    return TimeSpec("date", f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}")


def _tp_iso_date(m: re.Match[str]) -> TimeSpec:
    """ISO 日期"2013-07-05"（中英共享）。"""
    return TimeSpec("date", m.group(0))


def _tp_quarter(m: re.Match[str]) -> TimeSpec:
    """中文季度"2013 年第三季度"。"""
    return TimeSpec("quarter", f"{m.group(1)}Q{_CN_NUM[m.group(2)]}")


def _tp_iso_quarter(m: re.Match[str]) -> TimeSpec:
    """ISO 季度"2013Q2" / "2013 Q2"（阿拉伯数字编号）。"""
    return TimeSpec("quarter", f"{m.group(1)}Q{m.group(2)}")


def _tp_month(m: re.Match[str]) -> TimeSpec:
    """年月"2013 年 7 月"（month = 年*100+月）。"""
    return TimeSpec("month", int(m.group(1)) * 100 + int(m.group(2)))


def _tp_year(m: re.Match[str]) -> TimeSpec:
    """年份兜底"2013 年"。"""
    return TimeSpec("year", int(m.group(1)))


# kind 分发表：解析器已实现形态的唯一权威。词典与本表不一致 = 某类时间形态
# 要么写了却没实现、要么实现了却没词——加载失败（不静默丢形态）。
_TIME_DISPATCH: dict[str, Callable[[re.Match[str]], TimeSpec]] = {
    "date": _tp_date,
    "iso_date": _tp_iso_date,
    "quarter": _tp_quarter,
    "iso_quarter": _tp_iso_quarter,
    "month": _tp_month,
    "year": _tp_year,
}
if {kind for kind, _ in _TIME_PATTERNS} != set(_TIME_DISPATCH):
    raise ValueError(
        "patterns_zh_cn.yml 的 time.patterns kind 与解析器实现不一致："
        f"词典 {sorted(kind for kind, _ in _TIME_PATTERNS)} vs "
        f"实现 {sorted(_TIME_DISPATCH)}（新增形态需同时补 _TIME_DISPATCH 分支）"
    )

# 相对时间词：命中即反问（固定快照评测下必然漂移，ADR-0014 ③ 设计性不支持）
_RELATIVE_TIME = _ZH_PATTERNS["time"]["relative_reject"]["words"]

# 时间智能触发词（B5 ADR-0017）：命中 + 时间已解析 → 设置 comparison；
# 命中 + 时间未解析 → ClarificationRequest（同比/环比/累计需要绝对时间锚点）
_COMPARISON_TRIGGERS_ZH: dict[str, tuple[str, ...]] = {
    "yoy": ("同比", "年同比"),
    "pop": ("环比", "月环比", "季环比"),
    "cumulative": ("累计", "年初至今"),
    "rank": ("排名", "排行"),
}
_COMPARISON_TRIGGERS_EN: dict[str, tuple[str, ...]] = {
    "yoy": ("year-over-year", "yoy", "yearly comparison"),
    "pop": ("period-over-period", "pop", "mom", "qoq"),
    "cumulative": ("cumulative", "ytd", "mtd", "running total"),
    "rank": ("rank", "ranking"),
}

# 显式分组结构词："按分支统计 / 按客户等级分组"
_GROUP_RE = _ZH_PATTERNS["grouping"]["pattern"]
_TOP_N_RE = _ZH_PATTERNS["topn"]["pattern"]

# filter 触发结构（ADR-0014 ①；顺序无关，逐形态独立扫描）
# - 维度值等值/排除：前缀词 + 目标短语，到分隔符截断
_EQ_FILTER_RE = _ZH_PATTERNS["filter_include"]["pattern"]
_EXC_FILTER_RE = _ZH_PATTERNS["filter_exclude"]["pattern"]
# - 度量阈值（HAVING 语义）：比较词 + 数值 + 可选中文量级（"1000 万"）
_THRESHOLD_GT_RE = _ZH_PATTERNS["threshold"]["greater"]["pattern"]
_THRESHOLD_LT_RE = _ZH_PATTERNS["threshold"]["less"]["pattern"]

# 指代追问（ADR-0014 ②；仅全量解析 unmatched 的残句才进入，见 followup()）：
# - 链接词开头（那/那么/换成/改成/改为/按）或"呢"结尾 = 口语残句形态
_FOLLOWUP_PREFIXES = _ZH_PATTERNS["followup"]["prefixes"]
# - 换维壳：换成/改成/改为/按 引导的短语（可带"那"前缀与"统计/分组/呢"尾缀）；
#   $ 锚防非贪婪截断过早（"按客户等级统计呢"须整体消费到句尾）
_FOLLOWUP_DIM_RE = _ZH_PATTERNS["followup"]["dim_pattern"]


# ---------------------------------------------------------------------------
# 英文形态（en，P6 locale 化，2026-09-05）：与中文词表互斥分片，正则独立成族。
#
# 形态触发词已从本文件外置到 `semantic/synonyms/patterns_en_us.yml`（ADR-0015 §②，
# 批次 B3b）——**纯搬运**：常量名与类型不变，值来自词典，解析算法本体未动。
# ISO 日期/季度与中文同源（词典内 `ref` 引用，不复制第二份）。逐条等价性证明与
# 验收口径见 docs/design/adr-0015-pattern-lexicon-en.md。
#
# 时间语序（声明在词典的 time.patterns 里）：quarter 两向（Q2 2013 / 2013 Q2）、
# 月份名（May 2014）、ISO date（与 zh 共享）；裸年份须介词引导，无介词兜底仅在
# 句内无阈值词时启用（防 "over 5000" 之类阈值数字被误读成年份）。
# ---------------------------------------------------------------------------

_EN_PATTERNS = load_locale_patterns("en_us")

# 月份名 → 序号（词典 months 节；键小写，查表前对捕获组做 lower()）
_EN_MONTH_NUM = _EN_PATTERNS["months"]

# 时间形态：`time.patterns` 是**有序** (kind, 已编译正则) 列表，解析按声明顺序逐个
# 尝试（原实现的 if-chain 顺序：ISO date → quarter 两向 → ISO quarter → 月份名 →
# 介词年 → 裸年兜底）。
_EN_TIME_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = _EN_PATTERNS["time"][
    "patterns"
]


def _tp_en_iso_date(question: str, m: re.Match[str]) -> TimeSpec | None:
    """ISO 日期"2013-07-05"（ref 自 zh 词典，中英共享）。"""
    return TimeSpec("date", m.group(0))


def _tp_en_quarter(question: str, m: re.Match[str]) -> TimeSpec | None:
    """英文季度两向：语序 A"Q2 2013" 用 group(1)/(2)；语序 B"2013 Q2" 用 group(3)/(4)。"""
    if m.group(1):
        return TimeSpec("quarter", f"{m.group(2)}Q{m.group(1)[-1]}")
    return TimeSpec("quarter", f"{m.group(3)}Q{m.group(4)[-1]}")


def _tp_en_iso_quarter(question: str, m: re.Match[str]) -> TimeSpec | None:
    """ISO 季度"2013Q2"（ref 自 zh 词典，中英共享）。"""
    return TimeSpec("quarter", f"{m.group(1)}Q{m.group(2)}")


def _tp_en_month(question: str, m: re.Match[str]) -> TimeSpec | None:
    """月份名 + 年份"May 2014"（month = 年*100+月；大小写不敏感由词典 flags 声明）。"""
    return TimeSpec("month", int(m.group(2)) * 100 + _EN_MONTH_NUM[m.group(1).lower()])


def _tp_en_year_prep(question: str, m: re.Match[str]) -> TimeSpec | None:
    """介词引导年份"in 2013" / "during 2013"。"""
    return TimeSpec("year", int(m.group(1)))


def _tp_en_year_bare(question: str, m: re.Match[str]) -> TimeSpec | None:
    """无介词裸年兜底：句中含阈值词时不启用（防 "over 5000" 误读成年份）。

    返回 None = 本形态不启用、继续下一形态；`year_bare` 是表内**最后一项**，故
    "继续"必然落到解析尾部的 `return None`——与原 if-chain 的 gate 逐字等价。
    """
    if any(w in question.lower() for w in _EN_THRESHOLD_WORDS):
        return None
    return TimeSpec("year", int(m.group(1)))


# kind 分发表：英文解析器已实现形态的唯一权威，与词典 kind 集合不一致 = 加载失败
_EN_TIME_DISPATCH: dict[str, Callable[[str, re.Match[str]], TimeSpec | None]] = {
    "iso_date": _tp_en_iso_date,
    "quarter": _tp_en_quarter,
    "iso_quarter": _tp_en_iso_quarter,
    "month": _tp_en_month,
    "year_prep": _tp_en_year_prep,
    "year_bare": _tp_en_year_bare,
}
if {kind for kind, _ in _EN_TIME_PATTERNS} != set(_EN_TIME_DISPATCH):
    raise ValueError(
        "patterns_en_us.yml 的 time.patterns kind 与解析器实现不一致："
        f"词典 {sorted(kind for kind, _ in _EN_TIME_PATTERNS)} vs "
        f"实现 {sorted(_EN_TIME_DISPATCH)}（新增形态需同时补 _EN_TIME_DISPATCH 分支）"
    )

# 相对时间词：命中即反问（固定快照评测下必然漂移，ADR-0014 ③ 设计性不支持）
_EN_RELATIVE_TIME = _EN_PATTERNS["time"]["relative_reject"]["words"]
# 阈值词：裸年兜底封锁表（句中含任意阈值词时不启用无介词年份解析）
_EN_THRESHOLD_WORDS = _EN_PATTERNS["time"]["threshold_gate"]["words"]

# 显式分组结构词（by/grouped by/group by/broken down by + 短语；截到时间介词/连词）
_EN_GROUP_RE = _EN_PATTERNS["grouping"]["pattern"]
_EN_TOP_N_RE = _EN_PATTERNS["topn"]["pattern"]
# TopN 后短语提维度（"top 3 categories by sales in 1999" → categories），
# 截到排序词 by / 时间介词 / 连词（与中文"前 N 名"只给数字不同，英文名词后置）
_EN_TOP_N_DIM_RE = _EN_PATTERNS["topn_dim"]["pattern"]
# filter 触发结构（en）：only/排除 + 值短语；值后裸实体复数词（tier 3 customers）
# 作后缀裁剪；介词 for/of/from 剥除；lookahead 防值吞掉时间/分组短语
_EN_ONLY_RE = _EN_PATTERNS["filter_include"]["pattern"]
_EN_EXCL_RE = _EN_PATTERNS["filter_exclude"]["pattern"]
# 度量阈值（HAVING 语义）：over/above/more than… + 数值 + 可选英文量级词/K-M-B 后缀
_EN_THRESHOLD_GT_RE = _EN_PATTERNS["threshold"]["greater"]["pattern"]
_EN_THRESHOLD_LT_RE = _EN_PATTERNS["threshold"]["less"]["pattern"]
_EN_UNIT = _EN_PATTERNS["magnitude"]["en_units"]
# 指代追问（en）：what about / how about X？与 by X instead（换维壳）；
# 与中文同构：自由代词与无法归属的碎片 → 反问完整重述
_EN_FOLLOWUP_WHAT_RE = _EN_PATTERNS["followup"]["what"]["pattern"]
_EN_FOLLOWUP_INSTEAD_RE = _EN_PATTERNS["followup"]["instead"]["pattern"]

# 同义词表外置（ADR-0015）：zh = 语义模型 ai_context 注记（实测全中文，唯一
# 例外 GMV/AOV 中英通用）；en = 模型注记 ∪ `semantic/synonyms/en_us.yml`。
# planner 内不保留任何硬编码措辞表——新场景接入英文措辞只改 YAML（B7 前提）。
_LOCALE_SYNONYM_FILE = {"zh": "zh_cn", "en": "en_us"}


def _locale_synonyms(locale: str, section: str) -> dict[str, tuple[str, ...]]:
    """取本 locale 的同义词补充表（指标 metric_synonyms / 维度 dimension_synonyms）。

    zh_cn.yml 当前为空占位（中文注记全部活在模型里，搬进词典属 B3a），
    因此 en 以外的合并恒等于模型注记本体——行为与外置前逐字一致。
    """
    return load_locale_synonyms(_LOCALE_SYNONYM_FILE[locale])[section]


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


@dataclass(frozen=True)
class ValueNotice:
    """值域归一记录（ADR-0016 §②）：问句原文写法 → 值域归一后的值。

    只记录**发生了归一**的 filter 值（kind=alias/case）；精确命中与未注册列不记录
    （无信息量）。消费方（Agent 解释链路 / 评测报告）据此向用户展示
    "你说 NSDQ，我按值域理解为 NASDAQ"，不静默改写口径。
    """

    field: str
    raw: str
    value: object
    kind: Literal["alias", "case"]


class Planner:
    """问句 → Plan 的确定性解析器。"""

    def __init__(
        self, model: SemanticModel, values_dir: Path | None = None
    ) -> None:
        """values_dir：值域注册表目录，缺省 = `semantic/values/`（仅测试注入 tmp 目录）。"""
        self.model = model
        self.values_dir = values_dir or value_domain.VALUES_DIR

    def plan(
        self, question: str, locale: str | None = None
    ) -> Plan | ClarificationRequest:
        """解析问句。返回 Plan；无法唯一确定时返回 ClarificationRequest。

        需要同时拿到值域归一记录（ADR-0016）时调 `plan_with_notices()`；本入口
        保持历史签名不变（CLI / HTTP / 图路由 / 521 条契约测试零改动）。
        """
        result, _ = self.plan_with_notices(question, locale)
        return result

    def plan_with_notices(
        self, question: str, locale: str | None = None
    ) -> tuple[Plan | ClarificationRequest, tuple[ValueNotice, ...]]:
        """plan() 的全部行为 + filter 值域归一记录（ADR-0016 §②）。

        notices 只包含发生归一的 filter 值；反问路径上可能已追加过记录
        （前面的 filter 已归一、后面的 filter 触发澄清），调用方仅在拿到
        Plan 时才读它。
        """
        notices: list[ValueNotice] = []
        result = self._plan_impl(question, locale, notices)
        return result, tuple(notices)

    def _plan_impl(
        self,
        question: str,
        locale: str | None,
        notices: list[ValueNotice],
    ) -> Plan | ClarificationRequest:
        """解析主体（五阶段：指标 → 时间 → 维度 → filter → TopN）。

        locale："zh"/"en" 显式指定（评测 runner 按样本 tags lang_en 传，确定性
        优先）；缺省 None → 自动检测（句中含任意中文字符 → zh，否则 → en，
        CLI/API 便利）。notices 由 filter 值域校验就地追加，不入返回值（本函数
        在反问路径上也只 return ClarificationRequest，调用方靠 notices 列表取记录）。
        """
        locale = self._resolve_locale(question, locale)
        # 1. 指标匹配（同义词子串命中，必须唯一）
        # 子串包含消歧：派生指标同义词含基础指标词（"平均每笔成交金额" ⊃
        # "成交金额"）时，长命中是更具体口径（派生），短命中是冗余命中——
        # 丢弃被其他命中词真包含的命中词；互不为子串的多命中仍保留（真歧义）。
        synsets = self._metric_synsets(locale)
        metric_hits = sorted(
            {
                name
                for name, syns in synsets.items()
                for syn in syns
                if syn in question
                and not any(
                    syn in other and syn != other
                    for other_name, other_syns in synsets.items()
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
        time = self._parse_time(question, locale)
        if isinstance(time, ClarificationRequest):
            return time

        # 2.5 时间智能触发词检测（B5 ADR-0017）
        comparison = self._detect_comparison(question, time, locale)
        if isinstance(comparison, ClarificationRequest):
            return comparison

        # 3. 维度解析（显式分组结构 + 维度表同义词）
        dimensions = self._parse_dimensions(question, locale)

        # 4. filter 解析（ADR-0014 ①）；值模糊 → 反问
        filters = self._parse_filters(question, metric, locale, notices)
        if isinstance(filters, ClarificationRequest):
            return filters

        # 5. "前 N 名" → 按指标降序 + limit
        order_by, limit = self._parse_top_n(question, metric, locale)

        return Plan(
            metric=metric,
            dimensions=dimensions,
            time=time,
            filters=filters,
            order_by=order_by,
            limit=limit,
            comparison=comparison,
        )

    def followup(
        self, question: str, prev: Plan, locale: str | None = None
    ) -> Plan | ClarificationRequest | None:
        """指代追问补全（ADR-0014 ②）：残句无指标词 → 复用上轮 Plan 结构。

        仅处理**同构追问**（调用方保证：planner.plan 已 unmatched——句内无指标
        词，且 prev 为同会话上轮成功采纳的 Plan）：命中链接词形态（中文"那 X 呢 /
        换成 X / 按 X 统计"；英文"what about X / how about X / by X instead"）
        时，继承 prev 的 metric/维度/过滤/排序，只替换本轮新解析出的时间/维度
        片段。返回 None = 非链接形态（调用方维持原 unmatched 流程）；
        ClarificationRequest = 补全歧义/相对时间（不猜，反问完整重述）。

        确定性边界（不猜测，zh/en 同构）：
        - 自由代词（"那它呢/这些呢"；"what about those"）与剥壳后无片段 → 反问
        - 相对时间（"那去年呢"；"what about last year"）→ 透传 relative_time 反问
        - 换维且上轮带维度值过滤 → 口径作用域二义（保留=子集，去掉=改口径）→ 反问
        """
        locale = self._resolve_locale(question, locale)
        if locale == "zh":
            return self._followup_zh(question, prev)
        return self._followup_en(question, prev)

    def _followup_zh(
        self, question: str, prev: Plan
    ) -> Plan | ClarificationRequest | None:
        """中文指代追问（原实现，2026-09-04 前逐字一致）。"""
        text = question.strip().strip("？?。!！，, ")
        # 链接词开头或"呢"结尾才算口语残句（否则维持原 unmatched 流程）
        if not (text.startswith(_FOLLOWUP_PREFIXES) or text.endswith("呢")):
            return None
        # 时间片段：壳词不影响既有时间正则；相对时间 → 透传澄清
        time = self._parse_time(text, "zh")
        if isinstance(time, ClarificationRequest):
            return time
        # 维度片段：换维壳命中后做维度字段子串匹配（多命中全取，与分组解析同风格）；
        # 壳误吃时间短语（"换成 2014 年"）时 new_dims 为空 → 不算换维，回落仅换时间
        new_dims: tuple[str, ...] = ()
        m = _FOLLOWUP_DIM_RE.search(text)
        if m:
            new_dims = self._dim_hits(m.group(1), "zh")
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

    def _followup_en(
        self, question: str, prev: Plan
    ) -> Plan | ClarificationRequest | None:
        """英文指代追问：what about X / how about X / by X instead（与中文同构）。"""
        text = question.strip().strip("？?。!！，, ")
        m = _EN_FOLLOWUP_WHAT_RE.search(text)
        if m:
            inner = m.group(1).strip()
        else:
            m = _EN_FOLLOWUP_INSTEAD_RE.search(text)
            if not m or not m.group(1).strip():
                return None
            inner = m.group(1).strip()
        # 时间片段（inner 内解析；相对时间 → 透传澄清）
        time = self._parse_time(inner, "en")
        if isinstance(time, ClarificationRequest):
            return time
        # 维度片段：换维壳短语内做维度字段子串匹配（多命中全取）
        new_dims: tuple[str, ...] = ()
        mg = _EN_GROUP_RE.search(inner)
        if mg:
            new_dims = self._dim_hits(mg.group(1), "en")
            if new_dims and prev.filters:
                return ClarificationRequest(
                    question,
                    (f"Follow-up 「{text}」 changes the grouping dimension but the "
                     "previous question had a dimension-value filter (filter scope "
                     "ambiguous); please restate the full question",),
                )
        # 无任何可替换片段（自由代词）→ 反问不猜
        if time is None and not new_dims:
            return ClarificationRequest(
                question,
                (f"Follow-up 「{text}」 has no replaceable time/dimension fragment "
                 "(free pronouns are not supported); please restate the full question",),
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

    @staticmethod
    def _resolve_locale(question: str, locale: str | None) -> str:
        """locale 解析：显式 zh/en 白名单；None → 中文字符启发式检测。"""
        if locale is not None:
            if locale not in ("zh", "en"):
                raise ValueError(f"locale 必须是 'zh' 或 'en'：{locale!r}")
            return locale
        return (
            "en"
            if not any("\u4e00" <= ch <= "\u9fff" for ch in question)
            else "zh"
        )

    def _metric_synsets(self, locale: str) -> dict[str, tuple[str, ...]]:
        """指标同义词集：zh=模型中文注记；en=模型 ∪ 英文同义词表（en_us.yml）。

        en 并集而非替换：模型同义词为中文，纯英文问句不可能命中，并集恒安全
        （zh 不并入 en——中文问句含英文维度值词时防误命中）。
        """
        if locale == "zh":
            return dict(self.model.metric_synonyms)
        extra = _locale_synonyms(locale, "metric_synonyms")
        return {
            name: syns + extra.get(name, ())
            for name, syns in self.model.metric_synonyms.items()
        }

    def _dim_synsets(self, locale: str) -> dict[str, tuple[str, ...]]:
        """维度字段同义词集（与 _metric_synsets 同构）。"""
        if locale == "zh":
            return dict(self.model.dimension_synonyms)
        extra = _locale_synonyms(locale, "dimension_synonyms")
        return {
            name: syns + extra.get(name, ())
            for name, syns in self.model.dimension_synonyms.items()
        }

    def _detect_comparison(
        self, question: str, time: TimeSpec | None, locale: str
    ) -> ComparisonSpec | None | ClarificationRequest:
        """时间智能触发词检测（B5 ADR-0017）。

        触发词命中 + time 已解析 → 返回 ComparisonSpec；
        触发词命中 + time 为 None → ClarificationRequest（需绝对时间锚点）；
        触发词未命中 → None（既有行为零变化）。
        """
        triggers = _COMPARISON_TRIGGERS_EN if locale == "en" else _COMPARISON_TRIGGERS_ZH
        text = question if locale == "en" else question
        text_lower = text.lower() if locale == "en" else text
        for kind, words in triggers.items():
            for w in words:
                target = text_lower if locale == "en" else text
                if w in target:
                    if time is None:
                        if locale == "en":
                            return ClarificationRequest(
                                question,
                                (f"'{w}' requires an absolute time anchor "
                                 "(drifts under frozen-snapshot evaluation); "
                                 "please specify a year",),
                                kind="relative_time",
                            )
                        return ClarificationRequest(
                            question,
                            (f"「{w}」需要绝对时间锚点（固定快照评测下会漂移，"
                             "请指定具体年份）",),
                            kind="relative_time",
                        )
                    return ComparisonSpec(kind=kind)
        return None

    def _parse_time(
        self, question: str, locale: str
    ) -> TimeSpec | None | ClarificationRequest:
        """绝对时间解析（zh/en 分片）；相对时间返回 ClarificationRequest。

        zh 侧按词典声明顺序逐个尝试形态（顺序即语义，见 `_TIME_PATTERNS` 注记），
        命中后按 kind 分发到对应构造函数——与原「逐正则 if-m」链逐字等价。
        """
        if locale == "en":
            return self._parse_time_en(question)
        if any(t in question for t in _RELATIVE_TIME):
            return ClarificationRequest(
                question,
                ("不支持相对时间（固定快照评测下会漂移，请使用绝对日期）",),
                kind="relative_time",
            )
        for kind, regex in _TIME_PATTERNS:
            m = regex.search(question)
            if m:
                return _TIME_DISPATCH[kind](m)
        return None

    def _parse_time_en(
        self, question: str
    ) -> TimeSpec | None | ClarificationRequest:
        """英文绝对时间解析（形态顺序见 patterns_en_us.yml 的 time.patterns）。

        与中文同构：按声明顺序逐个尝试，命中后按 kind 分发——与原函数体的
        if-chain（ISO date → quarter 两向 → ISO quarter → 月份名 → 介词年 → 裸年
        兜底）逐字等价；只有 `year_bare` 的 handler 会返回 None（词面命中但
        句含阈值词 → 本形态不启用），且它是表内末项，因此行为与原 gate 一致。
        """
        lowered = question.lower()
        if any(t in lowered for t in _EN_RELATIVE_TIME):
            return ClarificationRequest(
                question,
                ("Relative time is not supported (it drifts under frozen-snapshot "
                 "evaluation); please use an absolute date",),
                kind="relative_time",
            )
        for kind, regex in _EN_TIME_PATTERNS:
            m = regex.search(question)
            if not m:
                continue
            spec = _EN_TIME_DISPATCH[kind](question, m)
            if spec is not None:
                return spec
        return None

    def _parse_dimensions(self, question: str, locale: str) -> tuple[str, ...]:
        """显式分组结构内的维度表字段匹配（zh：按X统计/分组；en：by/grouped by）。"""
        dims: tuple[str, ...] = ()
        if locale == "en":
            m = _EN_GROUP_RE.search(question)
            if m:
                dims = self._dim_hits(m.group(1), "en")
            # TopN 后短语提维度（"top 3 categories by sales" → categories）
            m = _EN_TOP_N_DIM_RE.search(question)
            if m:
                dims = tuple(dict.fromkeys(dims + self._dim_hits(m.group(1), "en")))
            return dims
        m = _GROUP_RE.search(question)
        if not m:
            return ()
        return self._dim_hits(m.group(1), "zh")

    def _dim_hits(self, phrase: str, locale: str) -> tuple[str, ...]:
        """短语内命中的维度字段（dim_* 非时间字段同义词子串，多命中全取）。"""
        return tuple(
            name
            for name, syns in self._dim_synsets(locale).items()
            if any(s in phrase for s in syns)
        )

    def _parse_filters(
        self,
        question: str,
        metric: str,
        locale: str,
        notices: list[ValueNotice],
    ) -> tuple[Filter, ...] | ClarificationRequest:
        """filter 解析（ADR-0014 ①）。返回顺序固定：度量阈值在前，维度值在后。

        维度词命中而**值**模糊（含中文/为空）→ ClarificationRequest（gold-155）；
        过滤短语整体无法归属任何维度字段时不产生 filter（"只看/仅"等前缀
        也可能是强调语，不做过度澄清）——两类不猜测边界见模块 docstring。
        值已归属列后还要过一道值域校验（ADR-0016），归一记到 notices。
        """
        if locale == "en":
            return self._parse_filters_en(question, metric, notices)
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
            matched = self._match_dim_value(m.group(1), "zh")
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
            resolved = self._resolve_filter_value(
                field, value, locale, question, notices
            )
            if isinstance(resolved, ClarificationRequest):
                return resolved
            filters.append(Filter(field, op, resolved))
        return tuple(filters)

    def _parse_filters_en(
        self, question: str, metric: str, notices: list[ValueNotice]
    ) -> tuple[Filter, ...] | ClarificationRequest:
        """英文 filter 解析（only/excluding + 维度值；over/under… + 量级词）。

        与中文同构：阈值在前、维度值在后；修饰词（"only the main branch"）与
        无维度词短语（强调语）边界同 docstring 的 zh 说明。
        """
        filters: list[Filter] = []
        for regex, op, unit_map in (
            (_EN_THRESHOLD_GT_RE, ">", _EN_UNIT),
            (_EN_THRESHOLD_LT_RE, "<", _EN_UNIT),
        ):
            m = regex.search(question)
            if m:
                value = float(m.group(1)) * (unit_map[m.group(2)] if m.group(2) else 1)
                filters.append(
                    Filter(metric, op, int(value) if value.is_integer() else value)
                )
        for prefix_re, op in ((_EN_ONLY_RE, "="), (_EN_EXCL_RE, "!=")):
            m = prefix_re.search(question)
            if not m:
                continue
            matched = self._match_dim_value(m.group(1), "en")
            if matched is None:
                continue
            field, raw_value, has_prefix = matched
            if has_prefix:
                return ClarificationRequest(
                    question,
                    (f"The value of 「{m.group(1)}」 cannot be uniquely determined "
                     "(the dimension word has a qualifier; please give the exact value)",),
                )
            if raw_value == "":
                return ClarificationRequest(
                    question,
                    (f"Filter target has no value: {m.group(1)!r}; please give the "
                     "exact value",),
                )
            if any("\u4e00" <= ch <= "\u9fff" for ch in raw_value):
                return ClarificationRequest(
                    question,
                    (f"「{raw_value}」 does not map to a registered value/synonym of "
                     f"{field} (Chinese dimension values are not supported; please "
                     "give the exact value)",),
                )
            value: str | int = raw_value
            if re.fullmatch(r"\d+", raw_value):
                value = int(raw_value)
            resolved = self._resolve_filter_value(
                field, value, "en", question, notices
            )
            if isinstance(resolved, ClarificationRequest):
                return resolved
            filters.append(Filter(field, op, resolved))
        return tuple(filters)

    def _resolve_filter_value(
        self,
        field: str,
        value: str | int,
        locale: str,
        question: str,
        notices: list[ValueNotice],
    ) -> object | ClarificationRequest:
        """值域校验/归一（ADR-0016 §②）；返回归一后的值或 ClarificationRequest。

        未注册/skipped 列（无 profile 或超基数阈值）→ 原值透传，不产生反问；
        unknown → 反问并附按频次排序的候选值样例（用户可直接改成其中一个重问）。
        """
        res = value_domain.resolve(
            self.model.name, field, value, self.values_dir
        )
        if res.kind in ("unregistered", "exact"):
            return res.value
        if res.kind in ("alias", "case"):
            notices.append(
                ValueNotice(field=field, raw=str(value), value=res.value, kind=res.kind)
            )
            return res.value
        profile = res.profile
        path = value_domain.display_path(self.model.name, field, self.values_dir)
        listed = len(profile.values) if profile is not None else 0
        if locale == "en":
            return ClarificationRequest(
                question,
                (f"'{value}' is neither a registered value nor a known alias of "
                 f"{field} ({listed} registered values in the locked snapshot: "
                 f"{', '.join(res.candidates)}). See {path}",),
                candidates=tuple(res.candidates),
            )
        return ClarificationRequest(
            question,
            (f"「{value}」不是 {field} 的已注册取值，也不是已登记别名"
             f"（当前快照共 {listed} 个取值，候选：{', '.join(res.candidates)}；"
             f"完整值域见 {path}）",),
            candidates=tuple(res.candidates),
        )

    def _match_dim_value(
        self, phrase: str, locale: str
    ) -> tuple[str, str, bool] | None:
        """过滤短语 → (维度字段名, 取值原文, 维度词前是否有修饰)。

        返回 None = 短语不含任何维度词（"只看/仅"可能是强调语，不产生 filter）；
        维度词前带修饰（如「核心分支」）→ has_prefix=True——修饰词可能是口语
        定语，值无法确定性确认（gold-155 反问载体），由调用方澄清。
        多 syn 命中取最长（"客户等级" 优先 "客户"）。
        """
        synsets = self._dim_synsets(locale)
        best: tuple[int, str, str, int] | None = None  # (syn 长, 字段名, 命中 syn, 位置)
        for field, syns in synsets.items():
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

    def _parse_top_n(
        self, question: str, metric: str, locale: str
    ) -> tuple[tuple[OrderSpec, ...], int]:
        """TopN → 按指标降序 + limit（zh：前 N 名；en：top N / best N）。

        未命中则默认 limit=100。
        """
        regex = _EN_TOP_N_RE if locale == "en" else _TOP_N_RE
        m = regex.search(question)
        if not m:
            return (), 100
        return (OrderSpec(metric, desc=True),), int(m.group(1))
