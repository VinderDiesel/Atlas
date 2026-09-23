"""Plan → SQL 确定性编译器（AGENTS.md 术语表：Compiler，区别于 LLM Generator）

设计原则
--------
1. **确定性优先**：给定 Plan 与语义模型，输出唯一 SQL；不经过 LLM、不做猜测。
2. **AST 构建**：全程 sqlglot AST 操作，禁止字符串拼接（AGENTS.md 7.2）。
3. **可解释**：返回 join 链说明（事实表 → 维度表路径），供 Agent 解释链路引用。
4. **行级策略不在此注入**：policy 注入是 Guard（agent/security/sql_guard.py）的职责，
   本模块输出纯净查询；guard.enforce 之后再执行。

已知边界（MVP，诚实声明）
------------------------
- 字段重命名不支持：field expression 必须等于物理列名（否则报错，不猜测）
- 时间维度声明化：时间表/列由模型 custom_extensions data JSON 的 `time_dimension`
  声明驱动（{table, mode: single|composite, columns: {year/quarter/month/date}}）——
  single = 每粒度一个 ID 列（金融 TPC-DI CalendarYearID 等）；composite = year 列
  拆位 + 季/月列双等值（零售 TPC-DS d_year + d_qoy/d_moy）；模型未声明而 Plan
  带时间时确定性报错
- 仅支持 ANSI_SQL 方言输出（Doris 转换见 guard / transpile 后续迭代）
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

REPO = Path(__file__).resolve().parent.parent
FINANCE_MODEL = REPO / "semantic" / "ossie" / "atlas_finance.ossie.yaml"
SYNONYMS_DIR = REPO / "semantic" / "synonyms"


class CompileError(Exception):
    """Plan 无法编译为 SQL（确定性错误，不是猜测后降级）。"""


# ---------------------------------------------------------------------------
# Plan 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimeSpec:
    """时间规格。granularity ∈ {year, quarter, month, date}。

    value：year=2013；quarter="2013Q2"（Q 大写）；month=201307；date="2017-07-07"。
    """

    granularity: str
    value: int | str


@dataclass(frozen=True)
class Filter:
    """简单过滤条件（MVP 支持等值与比较）。"""

    column: str  # 字段名（语义层）
    op: str  # = != < <= > >=
    value: object


@dataclass(frozen=True)
class OrderSpec:
    """排序规格。column 为 metric 名或维度字段名（按 Plan 解析规则）。"""

    column: str
    desc: bool = False


@dataclass(frozen=True)
class ComparisonSpec:
    """时间智能比较声明（B5 ADR-0017）。

    kind ∈ {yoy, pop, cumulative, rank}：
    - yoy：同比（year-over-year），与前一年同期比较
    - pop：环比（period-over-period），与上一期间比较（粒度随时间粒度）
    - cumulative：累计（YTD/MTD），年内逐月子粒度累加
    - rank：排名（RANK() OVER），组内排名编号列
    """

    kind: str


@dataclass(frozen=True)
class Plan:
    """指标计划：问句解析后的结构化意图（AGENTS.md 术语表：Plan）。"""

    metric: str
    dimensions: tuple[str, ...] = ()
    time: TimeSpec | None = None
    filters: tuple[Filter, ...] = ()
    order_by: tuple[OrderSpec, ...] = ()
    limit: int = 100
    comparison: ComparisonSpec | None = None


# ---------------------------------------------------------------------------
# 语义模型加载
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    name: str
    physical: str
    is_time: bool = False
    synonyms: tuple[str, ...] = ()


@dataclass(frozen=True)
class Dataset:
    name: str
    source: str
    fields: dict[str, Field]
    primary_key: tuple[str, ...] = ()


@dataclass(frozen=True)
class Relationship:
    name: str
    from_ds: str
    to_ds: str
    from_columns: tuple[str, ...]
    to_columns: tuple[str, ...]

    def reversed(self) -> Relationship:
        return Relationship(
            name=self.name,
            from_ds=self.to_ds,
            to_ds=self.from_ds,
            from_columns=self.to_columns,
            to_columns=self.from_columns,
        )


class SemanticModel:
    """从 ossie.yaml 加载的语义模型（Compiler / Planner 的只读输入）。"""

    def __init__(self, path: Path = FINANCE_MODEL, *, doc: dict[str, Any] | None = None) -> None:
        """doc：已 safe_load 的 ossie YAML（None = 从 path 读取，历史行为不变）。

        治理面（serving/governance.py）传入按 mtime 缓存的解析结果，避免每请求
        重复读取与解析（实测 23.4 ms/次，ADR-0022 决策 ④ 的缓存清单）；解析器
        本体（下方的转换逻辑）始终是本类，不因入口不同分成两份实现。
        """
        loaded_from_doc = doc is not None
        if doc is None:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        model = doc["semantic_model"][0]
        # 语义身份哈希（ADR-0026 T02）：资格证据按模型源内容绑定——模型文件任何
        # 字节变化都会使既有资格证据失效（load_eligibility 拒签 eligible=True）。
        # doc= 内存构造时对规范化序列化取哈希，保证同一 doc 稳定可复算。
        if loaded_from_doc:
            raw = yaml.safe_dump(doc, sort_keys=True, allow_unicode=True).encode("utf-8")
        else:
            raw = path.read_bytes()
        self.source_sha256 = hashlib.sha256(raw).hexdigest()
        # 模型名：值域注册表（semantic/values/<model>.<field>.json，ADR-0016）的
        # 绑定键。与 ossie 文件名解耦——文件名是部署约定，模型名是语义身份。
        self.name = str(model["name"])
        self.datasets: dict[str, Dataset] = {}
        self.relationships: list[Relationship] = []
        self.metrics: dict[str, str] = {}
        self.metric_descriptions: dict[str, str] = {}
        self.metric_synonyms: dict[str, tuple[str, ...]] = {}
        self.metric_owners: dict[str, str] = {}
        # ADR-0026 T02：指标 × 维度可加组合的封闭注册表（metric → 登记的归因维度）。
        # 未登记的组合一律不可做变更归因分析（eval/analysis_eligibility 是唯一消费者）。
        self.attribution_dimensions: dict[str, tuple[str, ...]] = {}
        self.dimension_synonyms: dict[str, tuple[str, ...]] = {}
        self.time_dimension: dict | None = None  # custom_extensions 声明（见下方解析）
        self.default_row_policy: str | None = None  # 域 → 策略事实源（ADR-0021 决策 ②）
        for ds in model["datasets"]:
            fields: dict[str, Field] = {}
            for f in ds["fields"]:
                physical = f["expression"]["dialects"][0]["expression"]
                if physical != f["name"]:
                    raise CompileError(
                        f"dataset {ds['name']} 字段 {f['name']} 的物理表达式"
                        f" {physical!r} ≠ 字段名，MVP 编译器暂不支持字段重命名"
                    )
                is_time = bool(f.get("dimension", {}).get("is_time"))
                synonyms = tuple(f.get("ai_context", {}).get("synonyms", []))
                fields[f["name"]] = Field(
                    name=f["name"], physical=physical, is_time=is_time, synonyms=synonyms
                )
            self.datasets[ds["name"]] = Dataset(
                name=ds["name"],
                source=ds["source"],
                fields=fields,
                primary_key=tuple(ds.get("primary_key", [])),
            )
            # 维度同义词索引：仅收集 dim_* 表（维度表约定）的非时间字段，
            # 避免事实表外键/度量字段（如 Commission、SK_AccountID）被误当作分组维度
            if ds["name"].startswith("dim_"):
                for f in ds["fields"]:
                    if f.get("dimension", {}).get("is_time"):
                        continue
                    synonyms = tuple(f.get("ai_context", {}).get("synonyms", []))
                    if synonyms:
                        self.dimension_synonyms[f["name"]] = synonyms

        for rel in model["relationships"]:
            self.relationships.append(
                Relationship(
                    name=rel["name"],
                    from_ds=rel["from"],
                    to_ds=rel["to"],
                    from_columns=tuple(rel["from_columns"]),
                    to_columns=tuple(rel["to_columns"]),
                )
            )

        for m in model["metrics"]:
            for dialect in m["expression"]["dialects"]:
                if dialect["dialect"] == "ANSI_SQL":
                    self.metrics[m["name"]] = dialect["expression"]
                    break
            desc = m.get("description")
            if isinstance(desc, str):
                self.metric_descriptions[m["name"]] = desc
            self.metric_synonyms[m["name"]] = tuple(m.get("ai_context", {}).get("synonyms", []))
            # ATLAS 治理扩展 → owner（供检索 rerank 的 owner 优先级信号使用）；
            # analysis.attribution.dimensions → 可加组合注册表（ADR-0026 T02）
            owner = ""
            for ext in m.get("custom_extensions", []):
                if ext.get("vendor_name") != "ATLAS":
                    continue
                try:
                    data = json.loads(ext["data"])
                except (KeyError, json.JSONDecodeError):
                    continue
                owner = str(data.get("governance", {}).get("owner", ""))
                analysis = data.get("analysis")
                if isinstance(analysis, dict):
                    dims = analysis.get("attribution", {}).get("dimensions", [])
                    if isinstance(dims, list):
                        self.attribution_dimensions[m["name"]] = tuple(str(d) for d in dims)
            self.metric_owners[m["name"]] = owner

        # time_dimension：模型级声明（compiler 时间谓词的时间表/列来源，不再硬编码
        # dim_date + CalendarYearID 等；金融 single / 零售 composite，见各 YAML 头注记）
        # default_row_policy：域 → 策略的唯一事实源（ADR-0021 决策 ②）。两者可能声明在
        # 不同 ATLAS 扩展块，故**不 break**；各自「先出现者生效」（None 才覆盖）。
        for ext in model.get("custom_extensions", []):
            if ext.get("vendor_name") != "ATLAS":
                continue
            try:
                data = json.loads(ext["data"])
            except (KeyError, json.JSONDecodeError) as exc:
                raise CompileError(f"custom_extensions data JSON 解析失败：{exc}") from exc
            td = data.get("time_dimension")
            if td is not None and self.time_dimension is None:
                self.time_dimension = td
            policy_block = data.get("policy")
            policy = (
                policy_block.get("default_row_policy") if isinstance(policy_block, dict) else None
            )
            if policy and self.default_row_policy is None:
                self.default_row_policy = str(policy)

    def find_field(self, field_name: str) -> tuple[str, Field] | None:
        """按逻辑字段名查找（维度解析：branch → dim_broker.Branch）。"""
        for ds in self.datasets.values():
            if field_name in ds.fields:
                return ds.name, ds.fields[field_name]
        return None


# ---------------------------------------------------------------------------
# locale 同义词表（代码外配置，ADR-0015）
# ---------------------------------------------------------------------------

_LOCALE_FILES = {"zh_cn": "zh_cn.yml", "en_us": "en_us.yml"}
_LOCALE_KEYS = frozenset({"metric_synonyms", "dimension_synonyms"})
_LOCALE_CACHE: dict[str, dict[str, dict[str, tuple[str, ...]]]] = {}


def parse_locale_synonyms_doc(doc: Any, source: str) -> dict[str, dict[str, tuple[str, ...]]]:
    """归一化同义词表文档（路径加载与制品装配共用同一验证，不缓存）。

    source 只用于错误定位（文件路径或制品内标签），不参与解析。
    """
    if not isinstance(doc, dict):
        raise ValueError(f"同义词表顶层必须是映射：{source}")
    unknown = set(doc) - _LOCALE_KEYS
    if unknown:
        raise ValueError(f"同义词表不支持的顶层键 {sorted(unknown)}：{source}")

    def _section(key: str) -> dict[str, tuple[str, ...]]:
        raw = doc.get(key) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"同义词表 {key} 必须是 名称→[措辞, ...] 映射：{source}")
        out: dict[str, tuple[str, ...]] = {}
        for name, items in raw.items():
            if isinstance(items, str) or not isinstance(items, (list, tuple)):
                raise ValueError(f"{key}.{name} 必须是字符串列表：{source}")
            words = tuple(str(x).strip() for x in items)
            if any(not w for w in words):
                raise ValueError(f"{key}.{name} 含空措辞：{source}")
            if len(set(words)) != len(words):
                raise ValueError(f"{key}.{name} 措辞重复：{source}")
            out[str(name)] = words
        return out

    return {
        "metric_synonyms": _section("metric_synonyms"),
        "dimension_synonyms": _section("dimension_synonyms"),
    }


def load_locale_synonyms(locale: str) -> dict[str, dict[str, tuple[str, ...]]]:
    """读 `semantic/synonyms/<locale>.yml` → 归一化同义词表（启动加载一次）。

    locale 是**封闭注册表**（_LOCALE_FILES）：不在表内直接 ValueError，不拼接
    任意文件名。语义模型仍是定义权威源（ossie/），本函数只加载解析器的 locale
    形态层补充（英文措辞在 en_us.yml；模型 ai_context.synonyms 为中文注记）。
    已注册但文件不存在 = 配置缺失 → ValueError（宁可启动即失败，不静默降级出
    口径）。**声明顺序保留**——解析器按“模型注记 + 本表”顺序做最长命中匹配，
    顺序影响歧义判定。制品装配不经本缓存（见 parse_locale_synonyms_doc）。
    """
    if locale in _LOCALE_CACHE:
        return _LOCALE_CACHE[locale]
    if locale not in _LOCALE_FILES:
        raise ValueError(
            f"未知 locale {locale!r}（已注册：{', '.join(sorted(_LOCALE_FILES))}；"
            f"新增 locale 需同步 ADR-0015 与本注册表）"
        )
    path = SYNONYMS_DIR / _LOCALE_FILES[locale]
    if not path.exists():
        raise ValueError(f"locale={locale} 的同义词表缺失：{path}")
    loaded = parse_locale_synonyms_doc(
        yaml.safe_load(path.read_text(encoding="utf-8")) or {}, str(path)
    )
    _LOCALE_CACHE[locale] = loaded
    return loaded


# ---------------------------------------------------------------------------
# locale 形态触发词词典（代码外配置，ADR-0015 §②，批次 B3a/B3b）
#
# 与同义词表同一纪律：locale 是封闭注册表，节缺失/形态串不可编译一律报错——
# 静默降级等于让某类问句在无人察觉时不再被解析（口径风险）。
# ---------------------------------------------------------------------------

_PATTERN_FILES = {"zh_cn": "patterns_zh_cn.yml", "en_us": "patterns_en_us.yml"}

# 每 locale 的必需节（缺任何一节 = 该能力形态失去触发词）与顺序无关的白名单
_PATTERN_SECTIONS: dict[str, tuple[str, ...]] = {
    "zh_cn": (
        "time",
        "grouping",
        "topn",
        "filter_include",
        "filter_exclude",
        "threshold",
        "magnitude",
        "followup",
    ),
    # 英文独有 topn_dim（名词后置提维度）与 months（月份名→序号）；time 节内多一张
    # threshold_gate（裸年份兜底的封锁表）
    "en_us": (
        "time",
        "months",
        "grouping",
        "topn",
        "topn_dim",
        "filter_include",
        "filter_exclude",
        "threshold",
        "magnitude",
        "followup",
    ),
}

# flags 声明式白名单（ADR-0015 §②）：只放行 re 的四个纯语法标志，不放行任意整数。
# 仅英文词典可用——中文形态一律不需要，不把未经验证的能力摊开。
_PATTERN_FLAGS = {
    "IGNORECASE": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "DOTALL": re.DOTALL,
    "VERBOSE": re.VERBOSE,
}

_PATTERNS_CACHE: dict[str, dict[str, Any]] = {}


def _pattern_doc_data(doc: Any, locale: str, source: str) -> dict[str, Any]:
    """粗校验已解析的形态词典文档（顶层映射 + 节白名单）。"""
    if not isinstance(doc, dict):
        raise ValueError(f"形态词典顶层必须是映射：{source}")
    required = _PATTERN_SECTIONS[locale]
    missing = [key for key in required if key not in doc]
    if missing:
        raise ValueError(f"形态词典缺节 {missing}（必需节：{list(required)}）：{source}")
    unknown = sorted(set(doc) - set(required))
    if unknown:
        raise ValueError(f"形态词典不支持的顶层键 {unknown}：{source}")
    return doc


def _compile_pattern(
    raw: Any, where: str, source: str, flags: int = 0
) -> re.Pattern[str]:
    """模式串 → 已编译正则。**不做 strip**：首尾空白在正则里有语义。"""
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{where} 必须是非空正则字符串：{source}")
    try:
        return re.compile(raw, flags)
    except re.error as exc:
        raise ValueError(f"{where} 不是合法正则（{exc}）：{source}") from exc


def _pattern_flags(raw: Any, where: str, source: str) -> int:
    """`flags: [IGNORECASE]` → re 标志位或值；未知/重复 flag 名报错。"""
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError(f"{where} 必须是非空 flag 名列表：{source}")
    merged = 0
    for name in raw:
        if not isinstance(name, str) or name not in _PATTERN_FLAGS:
            raise ValueError(
                f"{where} 含不支持的 flag {name!r}"
                f"（允许：{', '.join(sorted(_PATTERN_FLAGS))}）：{source}"
            )
        if merged & _PATTERN_FLAGS[name]:
            raise ValueError(f"{where} flag 重复：{name}（{source}）")
        merged |= _PATTERN_FLAGS[name]
    return merged


def _pattern_words(raw: Any, where: str, source: str) -> tuple[str, ...]:
    """词表（相对时间词/追问前缀词）→ 元组；禁止首尾空白与重复。"""
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError(f"{where} 必须是非空字符串列表：{source}")
    words = tuple(str(x) for x in raw)
    if any(not w or w != w.strip() for w in words):
        raise ValueError(f"{where} 含空词或首尾空白：{source}")
    if len(set(words)) != len(words):
        raise ValueError(f"{where} 词条重复：{source}")
    return words


def _pattern_map(raw: Any, where: str, source: str) -> dict[str, int]:
    """量级/中文编号映射 → dict[str, int]（bool 不算 int）。"""
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{where} 必须是 词→整数倍率 映射：{source}")
    out: dict[str, int] = {}
    for key, value in raw.items():
        name = str(key)
        if not name or name != name.strip():
            raise ValueError(f"{where} 含空键或首尾空白键：{source}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{where}.{name} 必须是整数：{source}")
        out[name] = value
    return out


def _single_node(node: Any, where: str, source: str) -> re.Pattern[str]:
    """单 pattern 节（`grouping` / `threshold.greater` 等）：必须恰含 pattern 一键。"""
    if not isinstance(node, dict) or set(node) != {"pattern"}:
        raise ValueError(f"{where} 必须恰含 pattern 一键：{source}")
    return _compile_pattern(node["pattern"], f"{where}.pattern", source)


def _time_pattern_items(
    items: Any, source: str, *, shared: dict[str, re.Pattern[str]] | None = None
) -> list[tuple[str, re.Pattern[str]]]:
    """`time.patterns` → **有序** (kind, 已编译正则) 列表（两 locale 共用）。

    顺序即语义：解析按声明顺序逐个尝试，短模式先跑会截获长模式的输入。
    `source` 只用于错误定位（文件路径或制品内标签），不参与解析。
    `shared is None`（中文词典）——条目必须恰含 `kind` 与 `pattern`：中文形态
    既不需要 flags，也没有可引用的上游。给出 `shared`（英文词典）时额外允许：
    - `{kind, ref}`：引用上游（zh 词典）同名 kind，复用**同一已编译对象**；
      ref 不携带 pattern，因此不存在"先抄一份再用 ref 校对"的半搬运动作；
    - `{kind, pattern, flags}`：声明式正则标志（见 _PATTERN_FLAGS）。
    """
    if isinstance(items, str) or not isinstance(items, (list, tuple)) or not items:
        raise ValueError(f"time.patterns 必须是 非空的 kind/pattern 列表：{source}")
    kinds: list[str] = []
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for index, item in enumerate(items):
        where = f"time.patterns[{index}]"
        if not isinstance(item, dict) or "kind" not in item:
            raise ValueError(f"{where} 必须是含 kind 的映射：{source}")
        kind = str(item["kind"])
        if not kind or kind != kind.strip():
            raise ValueError(f"{where}.kind 非法（空或含首尾空白）：{source}")
        if kind in kinds:
            raise ValueError(f"time.patterns kind 重复：{kind}（{source}）")
        keys = set(item) - {"kind"}
        if keys == {"pattern"}:
            regex = _compile_pattern(item["pattern"], f"{where}.pattern", source)
        elif shared is not None and keys == {"ref"}:
            target = str(item["ref"])
            if target not in shared:
                raise ValueError(
                    f"{where}.ref 指向不存在的形态 {target!r}"
                    f"（可引用：{', '.join(sorted(shared))}）：{source}"
                )
            regex = shared[target]  # 同一对象，不是副本
        elif shared is not None and keys == {"pattern", "flags"}:
            regex = _compile_pattern(
                item["pattern"],
                f"{where}.pattern",
                source,
                _pattern_flags(item["flags"], f"{where}.flags", source),
            )
        else:
            allowed = "kind+pattern" + ("/ref" if shared is not None else "")
            raise ValueError(
                f"{where} 必须恰含 {allowed}（flags 只能配 pattern）：{source}"
            )
        kinds.append(kind)
        compiled.append((kind, regex))
    return compiled


def _parse_zh_patterns(doc: dict[str, Any], source: str) -> dict[str, Any]:
    """中文形态词典（已解析文档）→ 归一化结构（节名与 planner 解析阶段一一对应）。

    `time.patterns` 保留为**有序** (kind, 已编译正则) 元组：解析按声明顺序逐个
    尝试，短模式先跑会截获长模式的输入（"2013 年 7 月" 被读成年份），顺序即语义。
    kind 与解析器实现的一致性由 planner 校验（分发表在此处不可见）。
    本函数不读文件、不缓存：路径加载与制品装配共用同一实现。
    """
    time_doc = doc["time"]
    if not isinstance(time_doc, dict) or set(time_doc) != {"relative_reject", "patterns"}:
        raise ValueError(f"time 必须恰含 relative_reject 与 patterns 两键：{source}")
    reject = time_doc["relative_reject"]
    if not isinstance(reject, dict) or set(reject) != {"words"}:
        raise ValueError(f"time.relative_reject 必须恰含 words 一键：{source}")
    compiled = _time_pattern_items(time_doc["patterns"], source)

    magnitude = doc["magnitude"]
    if not isinstance(magnitude, dict) or set(magnitude) != {"cn_units", "cn_numerals"}:
        raise ValueError(f"magnitude 必须恰含 cn_units 与 cn_numerals 两键：{source}")

    threshold = doc["threshold"]
    if not isinstance(threshold, dict) or set(threshold) != {"greater", "less"}:
        raise ValueError(f"threshold 必须恰含 greater 与 less 两键：{source}")

    followup = doc["followup"]
    if not isinstance(followup, dict) or set(followup) != {"prefixes", "dim_pattern"}:
        raise ValueError(f"followup 必须恰含 prefixes 与 dim_pattern 两键：{source}")

    return {
        "time": {
            "relative_reject": {
                "words": _pattern_words(
                    reject["words"], "time.relative_reject.words", source
                )
            },
            "patterns": tuple(compiled),
        },
        "grouping": {"pattern": _single_node(doc["grouping"], "grouping", source)},
        "topn": {"pattern": _single_node(doc["topn"], "topn", source)},
        "filter_include": {
            "pattern": _single_node(doc["filter_include"], "filter_include", source)
        },
        "filter_exclude": {
            "pattern": _single_node(doc["filter_exclude"], "filter_exclude", source)
        },
        "threshold": {
            side: {
                "pattern": _single_node(threshold[side], f"threshold.{side}", source)
            }
            for side in ("greater", "less")
        },
        "magnitude": {
            "cn_units": _pattern_map(
                magnitude["cn_units"], "magnitude.cn_units", source
            ),
            "cn_numerals": _pattern_map(
                magnitude["cn_numerals"], "magnitude.cn_numerals", source
            ),
        },
        "followup": {
            "prefixes": _pattern_words(
                followup["prefixes"], "followup.prefixes", source
            ),
            "dim_pattern": _compile_pattern(
                followup["dim_pattern"], "followup.dim_pattern", source
            ),
        },
    }


def _months_map(raw: Any, month_re: re.Pattern[str], source: str) -> dict[str, int]:
    """`months`（小写月份名 → 序号）+ 与 month 形态的交叉校验。

    月份名同时活在两处：`time.patterns` 里 month 模式的交替串、与本表的键——这是
    英文词典唯一一处"同一事实两个书写位置"（交替串是正则一部分，不能由代码重组）。
    加载期强制：本表每个键都必须能被该模式命中，且捕获组 lower() 后回落到同一名字
    （改了表没改模式、或改了模式没改表，都在此响亮报错，而不是解析时 KeyError）。
    """
    months = _pattern_map(raw, "months", source)
    for name in months:
        if name != name.lower():
            raise ValueError(
                f"months 键必须小写（查表前对捕获组做 lower()）：{name}（{source}）"
            )
        for probe in (name, name.capitalize()):
            hit = month_re.search(f"{probe} 2014")
            if hit is None or hit.group(1).lower() != name:
                raise ValueError(
                    f"months.{name} 与 time.patterns 的 month 模式不同源："
                    f"{probe!r} 未被该模式命中（{source}）"
                )
    return months


def _parse_en_patterns(
    doc: dict[str, Any], source: str, *, shared: dict[str, re.Pattern[str]]
) -> dict[str, Any]:
    """英文形态词典（已解析文档）→ 归一化结构（与中文同构的两处差异：ref / flags）。

    ISO 日期与 ISO 季度与语言无关，本词典以 `ref` 引用 zh 词典的同名形态，解析为
    **同一已编译对象**（权威源唯一，不会改一处漏一处）；`shared` 由调用方显式给出
    中文形态（路径加载传全局缓存结果，制品装配传本 bundle 内的 zh 词典）——制品
    装配不得回落全局缓存，否则不同发布的内容会经同一缓存串用。
    `months` 表与 month 模式的交替串交叉校验。
    """
    time_doc = doc["time"]
    expected = {"relative_reject", "patterns", "threshold_gate"}
    if not isinstance(time_doc, dict) or set(time_doc) != expected:
        raise ValueError(f"time 必须恰含 {sorted(expected)} 三键：{source}")
    words: dict[str, tuple[str, ...]] = {}
    for key in ("relative_reject", "threshold_gate"):
        node = time_doc[key]
        if not isinstance(node, dict) or set(node) != {"words"}:
            raise ValueError(f"time.{key} 必须恰含 words 一键：{source}")
        words[key] = _pattern_words(node["words"], f"time.{key}.words", source)
    compiled = _time_pattern_items(time_doc["patterns"], source, shared=shared)
    by_kind = dict(compiled)
    if "month" not in by_kind:
        raise ValueError(
            f"time.patterns 必须含 kind: month（与 months 表交叉校验）：{source}"
        )

    magnitude = doc["magnitude"]
    if not isinstance(magnitude, dict) or set(magnitude) != {"en_units"}:
        raise ValueError(f"magnitude 必须恰含 en_units 一键：{source}")

    threshold = doc["threshold"]
    if not isinstance(threshold, dict) or set(threshold) != {"greater", "less"}:
        raise ValueError(f"threshold 必须恰含 greater 与 less 两键：{source}")

    followup = doc["followup"]
    if not isinstance(followup, dict) or set(followup) != {"what", "instead"}:
        raise ValueError(f"followup 必须恰含 what 与 instead 两键：{source}")

    single = ("grouping", "topn", "topn_dim", "filter_include", "filter_exclude")
    return {
        "time": {
            "relative_reject": {"words": words["relative_reject"]},
            "patterns": tuple(compiled),
            "threshold_gate": {"words": words["threshold_gate"]},
        },
        "months": _months_map(doc["months"], by_kind["month"], source),
        **{key: {"pattern": _single_node(doc[key], key, source)} for key in single},
        "threshold": {
            side: {
                "pattern": _single_node(threshold[side], f"threshold.{side}", source)
            }
            for side in ("greater", "less")
        },
        "magnitude": {
            "en_units": _pattern_map(
                magnitude["en_units"], "magnitude.en_units", source
            )
        },
        "followup": {
            side: {
                "pattern": _single_node(followup[side], f"followup.{side}", source)
            }
            for side in ("what", "instead")
        },
    }


def load_locale_patterns(locale: str) -> dict[str, Any]:
    """读 `semantic/synonyms/patterns_<locale>.yml` → 形态触发词词典（一次加载）。

    返回按节组织的归一化结构，模式串已编译为 `re.Pattern`（顺序保留）。
    locale 封闭注册表：未注册 → ValueError（不拼接任意文件名）；已注册但文件
    缺失 → ValueError（宁可启动即失败，不出口径）。英文词典（en_us，B3b）可
    以 `ref` 引用中文词典的共享形态，因此加载 en 会连带加载 zh。
    制品装配不经本缓存（见 parse_locale_patterns_doc）。
    """
    if locale in _PATTERNS_CACHE:
        return _PATTERNS_CACHE[locale]
    if locale not in _PATTERN_FILES:
        raise ValueError(
            f"未知 patterns locale {locale!r}（已注册：{', '.join(sorted(_PATTERN_FILES))}；"
            f"新增 locale 需同步 ADR-0015 与本注册表）"
        )
    path = SYNONYMS_DIR / _PATTERN_FILES[locale]
    if not path.exists():
        raise ValueError(f"locale={locale} 的形态词典缺失：{path}")
    doc = _pattern_doc_data(
        yaml.safe_load(path.read_text(encoding="utf-8")) or {}, locale, str(path)
    )
    if locale == "en_us":
        shared = dict(load_locale_patterns("zh_cn")["time"]["patterns"])
        loaded = _parse_en_patterns(doc, str(path), shared=shared)
    else:
        loaded = _parse_zh_patterns(doc, str(path))
    _PATTERNS_CACHE[locale] = loaded
    return loaded


def parse_locale_patterns_doc(
    locale: str,
    doc: Any,
    source: str,
    *,
    shared_zh: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """已解析的形态词典文档 → 归一化结构（制品装配用；不读文件、不缓存）。

    `source` 只用于错误定位（文件路径或制品内标签），不参与解析。英文词典需要
    调用方显式传入中文形态结构 `shared_zh`（形如 load_locale_patterns("zh_cn") 的
    返回值），**不得回落全局缓存**——否则两个发布的中文形态会经同一缓存串用。
    """
    if locale not in _PATTERN_FILES:
        raise ValueError(
            f"未知 patterns locale {locale!r}（已注册：{', '.join(sorted(_PATTERN_FILES))}；"
            f"新增 locale 需同步 ADR-0015 与本注册表）"
        )
    checked = _pattern_doc_data(doc, locale, source)
    if locale == "en_us":
        if shared_zh is None:
            raise ValueError(
                f"英文形态词典必须显式给出中文共享形态（不读全局缓存）：{source}"
            )
        return _parse_en_patterns(
            checked, source, shared=dict(shared_zh["time"]["patterns"])
        )
    return _parse_zh_patterns(checked, source)


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------


class Compiler:
    """把 Plan 编译为只读 SQL。同一输入 → 同一输出（确定性）。"""

    def __init__(self, model: SemanticModel) -> None:
        self.model = model

    def compile(self, plan: Plan) -> tuple[str, list[str]]:
        """编译 Plan，返回 (sql, join_chain_notes)。"""
        metric_sql = self.model.metrics.get(plan.metric)
        if metric_sql is None:
            raise CompileError(f"指标不存在：{plan.metric}（可用：{sorted(self.model.metrics)}）")

        metric_ast = sqlglot.parse_one(metric_sql)
        self._resolve_refs(metric_ast)
        main_ds = self._main_dataset(metric_ast)

        # 维度与时间解析
        dim_targets: dict[str, str] = {}  # dataset -> 维度物理列
        dim_columns: list[tuple[str, str, str]] = []  # (dataset, 物理列, 语义名)
        for dim in plan.dimensions:
            found = self.model.find_field(dim)
            if found is None:
                raise CompileError(f"维度字段不存在：{dim}")
            ds_name, field = found
            if field.is_time:
                raise CompileError(f"维度 {dim} 是时间字段，请使用 Plan.time 指定时间范围")
            dim_targets[ds_name] = field.physical
            dim_columns.append((ds_name, field.physical, dim))

        time_predicates: list[exp.Expr] = []
        if plan.time is not None:
            time_predicates.append(self._time_predicate(plan.time))

        # filter 拆分（ADR-0014 ①）：维度过滤 → WHERE（其表须入 join 目标）；
        # 度量阈值（column == plan.metric）→ HAVING 聚合比较，无分组则无意义
        having_predicates: list[exp.Expr] = []
        where_filters: list[Filter] = []
        for f in plan.filters:
            if f.column == plan.metric:
                if not dim_targets:
                    raise CompileError(
                        f"度量阈值过滤（{f.column} {f.op} {f.value}）需要分组维度，"
                        "当前问句无可分组维度"
                    )
                having_predicates.append(self._compare(metric_ast.copy(), f.op, f.value))
                continue
            if self.model.find_field(f.column) is None:
                raise CompileError(f"过滤字段不存在：{f.column}")
            where_filters.append(f)

        # join 链（BFS 最短路径）；WHERE filter 引用的表也须可达
        target_ds = set(dim_targets)
        if time_predicates:
            # time_predicates 非空 ⟹ _time_predicate 已成功 ⟹ 声明必然存在
            target_ds.add(self.model.time_dimension["table"])
        for f in where_filters:
            found = self.model.find_field(f.column)
            if found is not None:
                target_ds.add(found[0])
        joins, notes = self._join_chain(main_ds, target_ds)

        # 组装 SELECT（AST 构建）：维度列在前（分组键可见），指标在后
        alias = exp.to_identifier(plan.metric)
        select_exprs: list[exp.Expr] = [
            self._column_ast(ds, col).as_(name) for ds, col, name in dim_columns
        ]
        select_exprs.append(metric_ast.as_(alias))
        # 实测：sqlglot 30.17 中 set('from', ...) 不会生效（FROM 丢失），必须构造时传入
        select = exp.Select(
            expressions=select_exprs,
            from_=exp.From(this=self._table_ast(main_ds)),
        )
        if joins:
            select.set("joins", joins)

        where_parts = time_predicates + [
            self._filter_predicate(f) for f in where_filters
        ]
        if where_parts:
            where_expr = where_parts[0]
            for p in where_parts[1:]:
                where_expr = exp.and_(where_expr, p)
            select.set("where", exp.Where(this=where_expr))

        if having_predicates:
            having_expr = having_predicates[0]
            for p in having_predicates[1:]:
                having_expr = exp.and_(having_expr, p)
            select.set("having", exp.Having(this=having_expr))

        if dim_targets:
            group_cols = [self._column_ast(ds_name, col) for ds_name, col in dim_targets.items()]
            select.set("group", exp.Group(expressions=group_cols))

        order_exprs: list[exp.Expr] = []
        for spec in plan.order_by:
            if spec.column == plan.metric:
                # 按指标排序：引用 SELECT 别名（聚合结果列不可引用物理列）
                order_exprs.append(exp.Ordered(this=exp.column(spec.column), desc=spec.desc))
            else:
                found = self.model.find_field(spec.column)
                if found is None:
                    raise CompileError(f"排序字段不存在：{spec.column}")
                ds_name, field = found
                if ds_name not in dim_targets:
                    raise CompileError(f"排序字段 {spec.column} 不在分组维度中")
                order_exprs.append(
                    exp.Ordered(this=self._column_ast(ds_name, field.physical), desc=spec.desc)
                )
        if order_exprs:
            select.set("order", exp.Order(expressions=order_exprs))

        select.set("limit", exp.Limit(expression=exp.Literal.number(plan.limit)))

        # 时间智能（B5 ADR-0017）：comparison 分支在基础 SELECT 组装后处理
        if plan.comparison is not None:
            return self._apply_comparison(select, plan, metric_ast, notes)

        sql = select.sql()
        # 规范要求：生成的 SQL 必须可被 sqlglot 往返解析
        roundtrip = sqlglot.parse_one(sql).sql()
        if roundtrip != sql:
            raise CompileError(f"生成 SQL 无法往返解析：\n{sql}\nvs\n{roundtrip}")
        return sql, notes

    def emitted_time_column(self, plan: Plan) -> str | None:
        """编译产物中承载时间轴语义的列别名（无则 None；ADR-0025 决策 ①）。

        入参：Plan（与 compile 同一实例）。返回：结果集首列别名（当 yoy/pop 取
        时间粒度列、cumulative 取降粒度 month 列），rank/plain 或缺少时间锚点时
        返回 None。不抛异常。`.lower()` 别名规则只在本方法内写一次；`_apply_lag`
        与 `_apply_cumulative` 必须消费本方法结果，不得自行推导（单一调用点）。
        """
        if plan.comparison is None or plan.time is None:
            return None
        td = self.model.time_dimension
        if td is None:
            return None
        columns: dict[str, str] = td["columns"]
        kind = plan.comparison.kind
        if kind in ("yoy", "pop"):
            column = columns.get(plan.time.granularity)
        elif kind == "cumulative":
            column = columns.get("month")
        else:
            return None
        return column.lower() if column else None

    # -- 内部实现 ----------------------------------------------------------

    def _table_ast(self, ds_name: str) -> exp.Table:
        """dataset.source → FROM 表（catalog.db.table AS dataset 名）。"""
        ds = self.model.datasets[ds_name]
        parts = ds.source.split(".")
        if len(parts) != 3:
            raise CompileError(
                f"dataset {ds_name} 的 source 必须是 catalog.db.table 形式：{ds.source}"
            )
        table = exp.Table(this=parts[2], db=parts[1], catalog=parts[0])
        table.set("alias", exp.TableAlias(this=exp.to_identifier(ds_name)))
        return table

    def _column_ast(self, ds_name: str, column: str) -> exp.Column:
        return exp.column(column, table=ds_name)

    def _resolve_refs(self, ast: exp.Expr) -> None:
        """校验 metric expression 中 dataset.field 引用，并统一为 alias.物理列。"""
        for col in ast.find_all(exp.Column):
            if col.table is None:
                continue
            ds = self.model.datasets.get(col.table)
            if ds is None:
                raise CompileError(f"expression 引用不存在的 dataset：{col.table}")
            if col.name not in ds.fields:
                raise CompileError(f"expression 引用不存在的字段：{col.table}.{col.name}")

    def _main_dataset(self, metric_ast: exp.Expr) -> str:
        """主表 = metric expression 中第一个被引用的 dataset（确定性约定）。"""
        for col in metric_ast.find_all(exp.Column):
            if col.table is not None and col.table in self.model.datasets:
                return col.table
        raise CompileError("metric expression 未引用任何 dataset")

    def _time_predicate(self, time: TimeSpec) -> exp.Expr:
        """时间谓词：按模型 time_dimension 声明构造（single/composite 两模式）。

        single（每粒度一列，金融 TPC-DI）：quarter "2013Q2" → CalendarQtrID = 20132
        （Q 固定在第 5 位）；month YYYYMM → YYYYM 无前导零换算；date → DATE cast；
        year → 列等值。composite（拆分列，零售 TPC-DS）：quarter → d_year = 2013
        AND d_qoy = 2；month 201305 → d_year = 2013 AND d_moy = 5；year → 列等值。
        """
        td = self.model.time_dimension
        if td is None:
            raise CompileError(
                "模型未声明 time_dimension（custom_extensions data JSON），无法编译时间谓词"
            )
        mode = td.get("mode", "single")
        if mode not in ("single", "composite"):
            raise CompileError(f"未知 time_dimension mode：{mode}（支持 single/composite）")
        table = td["table"]
        columns = td["columns"]
        dim_ds = self.model.datasets.get(table)
        if dim_ds is None:
            raise CompileError(f"time_dimension 声明的表不存在：{table}")
        if time.granularity not in columns:
            raise CompileError(
                f"不支持的粒度：{time.granularity}"
                f"（time_dimension 声明 {sorted(columns)}）"
            )
        column = columns[time.granularity]
        if column not in dim_ds.fields:
            raise CompileError(f"{table} 缺少时间列 {column}（granularity={time.granularity}）")

        # composite 拆位需要 year 列（quarter/month 拆出年再与季/月列双等值）
        if mode == "composite" and time.granularity in ("quarter", "month"):
            year_col = columns.get("year")
            if not year_col or year_col not in dim_ds.fields:
                raise CompileError(
                    f"{table} 缺少 composite 拆位所需 year 列：{year_col or '未声明'}"
                )

        if time.granularity == "quarter":
            # "2013Q2"：Q 固定在第 5 位（single 拼为 CalendarQtrID = 20132；
            # composite 拆为 d_year = 2013 AND d_qoy = 2）
            text = str(time.value).strip().upper()
            if len(text) != 6 or text[4] != "Q" or not (text[:4].isdigit() and text[5].isdigit()):
                raise CompileError(f"季度格式必须为 YYYYQn，如 2013Q2：{time.value!r}")
            if mode == "composite":
                return exp.and_(
                    exp.EQ(
                        this=self._column_ast(table, columns["year"]),
                        expression=exp.Literal.number(int(text[:4])),
                    ),
                    exp.EQ(
                        this=self._column_ast(table, column),
                        expression=exp.Literal.number(int(text[5:])),
                    ),
                )
            value = int(text[:4] + text[5:])
        elif time.granularity == "date":
            literal = exp.cast(exp.Literal.string(str(time.value)), to="DATE")
            return exp.EQ(this=self._column_ast(table, column), expression=literal)
        else:
            value = int(time.value)
            if time.granularity == "month":
                # single：TPC-DI dim_date.CalendarMonthID = YYYYM 拼接（2014 年 5 月 =
                # 20145，10-12 月 = 201410 等），无前导零；TimeSpec 用 YYYYMM（201405）
                # 承载月粒度（实测：直接 int 匹配 201405 命中 0 行）。composite：拆为
                # d_year = YYYY AND d_moy = M 双等值（d_moy 有前导零天然 1-12）
                y, m = value // 100, value % 100
                if mode == "composite":
                    return exp.and_(
                        exp.EQ(
                            this=self._column_ast(table, columns["year"]),
                            expression=exp.Literal.number(y),
                        ),
                        exp.EQ(
                            this=self._column_ast(table, column),
                            expression=exp.Literal.number(m),
                        ),
                    )
                value = int(f"{y}{m}")
        return exp.EQ(
            this=self._column_ast(table, column), expression=exp.Literal.number(value)
        )

    def _apply_comparison(
        self,
        select: exp.Select,
        plan: Plan,
        metric_ast: exp.Expr,
        notes: list[str],
    ) -> tuple[str, list[str]]:
        """时间智能 SQL 生成（B5 ADR-0017）：在基础 SELECT 上包裹窗口函数。

        rank：直接追加 RANK() OVER 列（不包裹 CTE）。
        yoy/pop：扩展时间范围 + CTE + LAG 窗口。
        cumulative：降粒度 + CTE + SUM OVER 累加。
        """
        kind = plan.comparison.kind
        if kind == "rank":
            return self._apply_rank(select, plan, metric_ast, notes)
        if kind in ("yoy", "pop"):
            return self._apply_lag(select, plan, kind, notes)
        if kind == "cumulative":
            return self._apply_cumulative(select, plan, notes)
        raise CompileError(f"不支持的 comparison kind：{kind}")

    def _apply_rank(
        self,
        select: exp.Select,
        plan: Plan,
        metric_ast: exp.Expr,
        notes: list[str],
    ) -> tuple[str, list[str]]:
        """排名：在 SELECT 列表追加 RANK() OVER (ORDER BY <指标聚合表达式> DESC)。

        窗口内 ORDER BY 放表达式本体而不是 SELECT 别名：实测 Doris 在窗口函数内
        不认同层别名（`Unknown column 'total_trade_value' in 'table list' in
        AGGREGATE clause`，2026-09-15 ccb4c8b 真链，gold-177/178/076/077 四条
        rank 样本全部执行失败）；yoy/pop/cumulative 不受影响，因其经 CTE 包裹后
        别名成为内层物化列。
        """
        rank_fn = exp.Anonymous(this="RANK", expressions=[])
        order = exp.Order(
            expressions=[exp.Ordered(this=metric_ast.copy(), desc=True)]
        )
        window = exp.Window(this=rank_fn, order=order)
        select.expressions.append(window.as_("rank"))
        # 外层默认按名次降序 + 维度 tie-breaker（ADR-0017 判据 4 真链锁）：
        # 无 ORDER BY 时「前 100 名」的截断点取决于引擎扫描序（200+ 维度值
        # 打满 LIMIT 实测 6 轮同 hash 属计划稳定巧合，非查询语义保证），且
        # 名次结果无序不可读。用户显式 order_by 已在 compile 阶段设置则尊重之。
        # 判定只看顶层 Order（exp.Window 内也含 exp.Order，不能对整树 find）。
        if select.args.get("order") is None:
            order_exprs: list[exp.Ordered] = [exp.Ordered(this=exp.column("rank"), desc=True)]
            order_exprs += [
                exp.Ordered(this=exp.column(dim), desc=False) for dim in plan.dimensions
            ]
            select.set("order", exp.Order(expressions=order_exprs))
        return self._finalize_select(select, notes)

    def _apply_lag(
        self,
        select: exp.Select,
        plan: Plan,
        kind: str,
        notes: list[str],
    ) -> tuple[str, list[str]]:
        """同比/环比：扩展时间范围 + CTE + LAG 窗口。"""
        if plan.time is None:
            raise CompileError(f"{kind} 需要绝对时间锚点（Plan.time 不可为 None）")
        td = self.model.time_dimension
        if td is None:
            raise CompileError("模型未声明 time_dimension，无法编译时间智能")
        mode = td.get("mode", "single")
        table = td["table"]
        columns = td["columns"]
        granularity = plan.time.granularity

        # 计算前驱期时间值
        prev_time = self._prev_period(plan.time)

        # 构造时间列名（按粒度）；别名规则收敛到 emitted_time_column（ADR-0025 决策 ①）
        time_col = columns.get(granularity)
        time_alias = self.emitted_time_column(plan)
        if not time_col or time_alias is None:
            raise CompileError(f"不支持的粒度：{granularity}")

        # 扩展 WHERE 时间谓词为 IN (prev, current)
        # 先移除原有的时间谓词，替换为 IN 谓词
        where = select.find(exp.Where)
        if where is not None:
            # 重建 WHERE：保留非时间谓词，替换时间谓词为 IN
            new_where = self._rebuild_where_with_in(
                where, plan.time, prev_time, table, time_col, granularity, mode, columns
            )
            select.set("where", new_where)
        else:
            # 无 WHERE，构造 IN 谓词
            in_pred = self._build_in_predicate(
                plan.time, prev_time, table, time_col, granularity, mode, columns
            )
            select.set("where", exp.Where(this=in_pred))

        # 添加时间列到 GROUP BY 和 SELECT（作为分组键）
        time_col_expr = self._column_ast(table, time_col)
        select.expressions.insert(0, time_col_expr.as_(time_alias))
        group = select.find(exp.Group)
        if group is not None:
            group.expressions.insert(0, time_col_expr.copy())
        else:
            select.set("group", exp.Group(expressions=[time_col_expr.copy()]))

        # 维度分组（ADR-0017 判据 4 真链锁）：base 必须全量——`ORDER BY 时间
        # LIMIT 100` 会把 200+ 维度值 × 2 期截成单期（gold-173 真跑实测 100 行
        # 全是 2014、2015 被截光）；外层改由 _wrap_with_lag_by_dims 处理
        if plan.dimensions:
            select.set("order", None)
            select.set("limit", None)
            return self._wrap_with_lag_by_dims(select, plan, time_alias, notes)

        # 无维度：时间列升序 + 保留 LIMIT（历史形态，已锚定样本字节不变）
        select.set("order", exp.Order(
            expressions=[exp.Ordered(this=time_col_expr.copy(), desc=False)]
        ))

        # 包裹为 CTE + LAG 外层
        return self._wrap_with_lag(select, plan, time_alias, notes)

    def _apply_cumulative(
        self,
        select: exp.Select,
        plan: Plan,
        notes: list[str],
    ) -> tuple[str, list[str]]:
        """累计：降粒度（year→month）+ CTE + SUM OVER 累加。"""
        if plan.time is None:
            raise CompileError("cumulative 需要绝对时间锚点（Plan.time 不可为 None）")
        td = self.model.time_dimension
        if td is None:
            raise CompileError("模型未声明 time_dimension，无法编译时间智能")
        mode = td.get("mode", "single")
        table = td["table"]
        columns = td["columns"]

        # 降粒度：year → month；别名规则收敛到 emitted_time_column（ADR-0025 决策 ①）
        sub_granularity = "month"
        sub_col = columns.get(sub_granularity)
        sub_alias = self.emitted_time_column(plan)
        if not sub_col or sub_alias is None:
            raise CompileError(f"模型未声明 {sub_granularity} 粒度列，无法编译累计")

        # 添加子粒度列到 SELECT 和 GROUP BY
        sub_col_expr = self._column_ast(table, sub_col)
        select.expressions.insert(0, sub_col_expr.as_(sub_alias))
        group = select.find(exp.Group)
        if group is not None:
            group.expressions.insert(0, sub_col_expr.copy())
        else:
            select.set("group", exp.Group(expressions=[sub_col_expr.copy()]))

        # 设置 ORDER BY 子粒度升序
        select.set("order", exp.Order(
            expressions=[exp.Ordered(this=sub_col_expr.copy(), desc=False)]
        ))

        # 包裹为 CTE + SUM OVER 外层
        return self._wrap_with_cumulative(select, plan, sub_alias, notes)

    def _prev_period(self, time: TimeSpec) -> TimeSpec:
        """计算前驱期时间规格。"""
        if time.granularity == "year":
            return TimeSpec("year", int(time.value) - 1)
        if time.granularity == "quarter":
            text = str(time.value).strip().upper()
            year = int(text[:4])
            qtr = int(text[5])
            if qtr == 1:
                return TimeSpec("quarter", f"{year - 1}Q4")
            return TimeSpec("quarter", f"{year}Q{qtr - 1}")
        if time.granularity == "month":
            ym = int(time.value)
            y, m = ym // 100, ym % 100
            if m == 1:
                return TimeSpec("month", (y - 1) * 100 + 12)
            return TimeSpec("month", y * 100 + (m - 1))
        raise CompileError(f"不支持的粒度：{time.granularity}（同比/环比仅支持 year/quarter/month）")

    def _build_in_predicate(
        self,
        time: TimeSpec,
        prev: TimeSpec,
        table: str,
        col: str,
        granularity: str,
        mode: str,
        columns: dict,
    ) -> exp.Expr:
        """构造两期时间谓词（yoy/pop 的 WHERE）。

        composite 季/月粒度：逐期 `(year = Y AND col = V)` 组合 or（ADR-0017
        判据 4 真链锁）——`_time_value_to_int` 对 composite 返回**年值**（为
        year 列设计），若直接 `col IN (prev, cur)` 会把年值塞进季度列产生
        `d_qoy IN (2001, 2001)` 值域错位恒不命中（gold-074 真跑 0 行）。组合
        谓词同时覆盖跨年场景 `(2014,Q4) OR (2015,Q1)`——取数范围正确（跨年
        prev 方向为已登记未支持边界）。

        single：`col IN (prev_value, cur_value)`（历史形态，已锚定样本字节不变）。
        """
        if mode == "composite" and granularity in ("quarter", "month"):
            year_col = columns.get("year")
            if not year_col:
                raise CompileError("composite 模式缺 year 列，无法构造两期谓词")

            def year_period(spec: TimeSpec) -> tuple[int, int]:
                if granularity == "quarter":
                    text = str(spec.value).strip().upper()
                    return int(text[:4]), int(text[5])
                ym = int(spec.value)
                return ym // 100, ym % 100

            py, pv = year_period(prev)
            cy, cv = year_period(time)
            return exp.or_(
                exp.and_(
                    exp.EQ(
                        this=self._column_ast(table, year_col),
                        expression=exp.Literal.number(py),
                    ),
                    exp.EQ(
                        this=self._column_ast(table, col),
                        expression=exp.Literal.number(pv),
                    ),
                ),
                exp.and_(
                    exp.EQ(
                        this=self._column_ast(table, year_col),
                        expression=exp.Literal.number(cy),
                    ),
                    exp.EQ(
                        this=self._column_ast(table, col),
                        expression=exp.Literal.number(cv),
                    ),
                ),
            )
        cur_val = self._time_value_to_int(time, granularity, mode, columns)
        prev_val = self._time_value_to_int(prev, granularity, mode, columns)
        col_expr = self._column_ast(table, col)
        return exp.In(
            this=col_expr,
            expressions=[exp.Literal.number(prev_val), exp.Literal.number(cur_val)],
        )

    def _rebuild_where_with_in(
        self,
        where: exp.Where,
        time: TimeSpec,
        prev: TimeSpec,
        table: str,
        col: str,
        granularity: str,
        mode: str,
        columns: dict,
    ) -> exp.Where:
        """重建 WHERE：保留非时间谓词，替换时间谓词为 IN。"""
        # 简化实现：直接用 IN 谓词替换整个 WHERE
        # （现有查询的时间谓词是等值，替换为 IN 即可）
        in_pred = self._build_in_predicate(time, prev, table, col, granularity, mode, columns)
        return exp.Where(this=in_pred)

    def _time_value_to_int(
        self,
        time: TimeSpec,
        granularity: str,
        mode: str,
        columns: dict,
    ) -> int:
        """将 TimeSpec 转为整数时间值（与 _time_predicate 同口径）。"""
        if granularity == "year":
            return int(time.value)
        if granularity == "quarter":
            text = str(time.value).strip().upper()
            if mode == "composite":
                # composite 模式用 year 列单独约束（IN 条件用 year 值）
                return int(text[:4])
            return int(text[:4] + text[5:])
        if granularity == "month":
            ym = int(time.value)
            y, m = ym // 100, ym % 100
            if mode == "composite":
                return y
            return int(f"{y}{m}")
        raise CompileError(f"不支持的粒度：{granularity}")

    def _wrap_with_lag(
        self,
        select: exp.Select,
        plan: Plan,
        time_alias: str,
        notes: list[str],
    ) -> tuple[str, list[str]]:
        """包裹基础 SELECT 为 CTE + LAG 外层。"""
        cte_name = "base"
        base_sql = select.sql()
        # 构造外层 SELECT：time_col, metric, LAG(metric) OVER (ORDER BY time_col)
        time_col_ref = exp.column(time_alias)
        metric_ref = exp.column(plan.metric)
        lag_fn = exp.Anonymous(
            this="LAG",
            expressions=[metric_ref.copy()],
        )
        lag_order = exp.Order(
            expressions=[exp.Ordered(this=time_col_ref.copy(), desc=False)]
        )
        lag_window = exp.Window(this=lag_fn, order=lag_order)
        outer_exprs = [
            time_col_ref.as_(time_alias),
            metric_ref.as_(plan.metric),
            lag_window.as_("prev_period_value"),
        ]
        # CTE
        cte = exp.CTE(
            this=exp.to_identifier(cte_name),
            kind="WITH",
        )
        # 用字符串拼接构造完整 SQL（CTE 包裹）
        outer_sql = f"WITH {cte_name} AS ({base_sql}) SELECT {time_alias}, {plan.metric}, LAG({plan.metric}) OVER (ORDER BY {time_alias}) AS prev_period_value FROM {cte_name} ORDER BY {time_alias} LIMIT {plan.limit}"
        # 往返校验
        roundtrip = sqlglot.parse_one(outer_sql).sql()
        if roundtrip != outer_sql:
            raise CompileError(f"生成 SQL 无法往返解析：\n{outer_sql}\nvs\n{roundtrip}")
        return outer_sql, notes

    def _wrap_with_lag_by_dims(
        self,
        select: exp.Select,
        plan: Plan,
        time_alias: str,
        notes: list[str],
    ) -> tuple[str, list[str]]:
        """维度感知的 LAG 外层（yoy/环比 + 分组维度，ADR-0017 判据 4 真链锁）。

        与无维度路径（_wrap_with_lag）的差异（2026-09-15 ccb4c8b 真跑发现）：
        - base 不带 ORDER BY/LIMIT（调用方已移除）：`ORDER BY 时间 LIMIT 100`
          会把 200+ 维度值 × 2 期截成单期（gold-173 实测 100 行全是 2014）；
        - 外层投影维度列：否则同时间多行无法辨识归属；
        - LAG `PARTITION BY 维度`：无分区时「前一行」是任意维度的值（跨维度
          串算；gold-173/073 六轮内分别出现 6/5 个不同 hash，行序不可复现）；
        - `ORDER BY 维度, 时间`：行序唯一（= 分组键 + 时间），LIMIT 截断点确定。
        """
        cte_name = "base"
        base_sql = select.sql()
        dims = ", ".join(plan.dimensions)
        outer_sql = (
            f"WITH {cte_name} AS ({base_sql}) "
            f"SELECT {dims}, {time_alias}, {plan.metric}, "
            f"LAG({plan.metric}) OVER (PARTITION BY {dims} ORDER BY {time_alias}) "
            f"AS prev_period_value FROM {cte_name} "
            f"ORDER BY {dims}, {time_alias} LIMIT {plan.limit}"
        )
        # 往返校验
        roundtrip = sqlglot.parse_one(outer_sql).sql()
        if roundtrip != outer_sql:
            raise CompileError(f"生成 SQL 无法往返解析：\n{outer_sql}\nvs\n{roundtrip}")
        return outer_sql, notes

    def _wrap_with_cumulative(
        self,
        select: exp.Select,
        plan: Plan,
        sub_alias: str,
        notes: list[str],
    ) -> tuple[str, list[str]]:
        """包裹基础 SELECT 为 CTE + SUM OVER 累加外层。"""
        cte_name = "monthly"
        base_sql = select.sql()
        # 外层 SELECT：sub_col, metric, SUM(metric) OVER (ORDER BY sub_col ROWS UNBOUNDED PRECEDING)
        outer_sql = (
            f"WITH {cte_name} AS ({base_sql}) "
            f"SELECT {sub_alias}, {plan.metric}, "
            f"SUM({plan.metric}) OVER (ORDER BY {sub_alias} ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cumulative_value "
            f"FROM {cte_name} ORDER BY {sub_alias} LIMIT {plan.limit}"
        )
        # 往返校验
        roundtrip = sqlglot.parse_one(outer_sql).sql()
        if roundtrip != outer_sql:
            raise CompileError(f"生成 SQL 无法往返解析：\n{outer_sql}\nvs\n{roundtrip}")
        return outer_sql, notes

    def _finalize_select(
        self,
        select: exp.Select,
        notes: list[str],
    ) -> tuple[str, list[str]]:
        """最终化 SELECT：生成 SQL + 往返校验。"""
        sql = select.sql()
        roundtrip = sqlglot.parse_one(sql).sql()
        if roundtrip != sql:
            raise CompileError(f"生成 SQL 无法往返解析：\n{sql}\nvs\n{roundtrip}")
        return sql, notes

    def _compare(self, left: exp.Expr, op: str, value: object) -> exp.Expr:
        """比较谓词（WHERE/HAVING 共用）：left op value，值按类型转字面量。"""
        ops = {
            "=": exp.EQ,
            "!=": exp.NEQ,
            "<": exp.LT,
            "<=": exp.LTE,
            ">": exp.GT,
            ">=": exp.GTE,
        }
        if op not in ops:
            raise CompileError(f"不支持的过滤操作符：{op}")
        if isinstance(value, (int, float)):
            right: exp.Expr = exp.Literal.number(value)
        else:
            right = exp.Literal.string(str(value))
        return ops[op](this=left, expression=right)

    def _filter_predicate(self, f: Filter) -> exp.Expr:
        found = self.model.find_field(f.column)
        if found is None:
            raise CompileError(f"过滤字段不存在：{f.column}")
        ds_name, field = found
        return self._compare(self._column_ast(ds_name, field.physical), f.op, f.value)

    def _join_chain(self, main_ds: str, targets: set[str]) -> tuple[list[exp.Join], list[str]]:
        """BFS 找最短 join 链；返回 (join AST 列表, 可解释说明)。

        多跳路径上的全部边都要进 join 列表（如 fact_trades → dim_account → dim_customer）。
        """
        targets = {t for t in targets if t != main_ds}
        joins: list[exp.Join] = []
        notes: list[str] = []
        added: set[tuple[str, str]] = set()  # (from_ds, to_ds) 去重，防不同路径共享边
        visited = {main_ds}
        queue: list[tuple[str, list[Relationship]]] = [(main_ds, [])]

        while queue:
            current, chain = queue.pop(0)
            for rel in self.model.relationships:
                edge = (
                    rel
                    if rel.from_ds == current
                    else (rel.reversed() if rel.to_ds == current else None)
                )
                if edge is None or edge.to_ds in visited:
                    continue
                new_chain = chain + [edge]
                if edge.to_ds in targets:
                    for e in new_chain:
                        key = (e.from_ds, e.to_ds)
                        if key not in added:
                            joins.append(self._join_ast(e))
                            notes.append(f"{e.from_ds} → {e.to_ds}（{e.name}）")
                            added.add(key)
                    targets.discard(edge.to_ds)
                visited.add(edge.to_ds)
                queue.append((edge.to_ds, new_chain))

        if targets:
            raise CompileError(f"无法从 {main_ds} 通过 relationships 到达：{sorted(targets)}")
        return joins, notes

    def _join_ast(self, edge: Relationship) -> exp.Join:
        on: exp.Condition | None = None
        for fc, tc in zip(edge.from_columns, edge.to_columns, strict=True):
            cond = exp.EQ(
                this=self._column_ast(edge.from_ds, fc),
                expression=self._column_ast(edge.to_ds, tc),
            )
            on = cond if on is None else exp.and_(on, cond)
        return exp.Join(this=self._table_ast(edge.to_ds), on=on, kind="INNER")
