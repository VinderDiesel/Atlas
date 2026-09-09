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

import json
import re
from collections.abc import Callable
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
class Plan:
    """指标计划：问句解析后的结构化意图（AGENTS.md 术语表：Plan）。"""

    metric: str
    dimensions: tuple[str, ...] = ()
    time: TimeSpec | None = None
    filters: tuple[Filter, ...] = ()
    order_by: tuple[OrderSpec, ...] = ()
    limit: int = 100


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

    def __init__(self, path: Path = FINANCE_MODEL) -> None:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        model = doc["semantic_model"][0]
        self.datasets: dict[str, Dataset] = {}
        self.relationships: list[Relationship] = []
        self.metrics: dict[str, str] = {}
        self.metric_descriptions: dict[str, str] = {}
        self.metric_synonyms: dict[str, tuple[str, ...]] = {}
        self.metric_owners: dict[str, str] = {}
        self.dimension_synonyms: dict[str, tuple[str, ...]] = {}
        self.time_dimension: dict | None = None  # custom_extensions 声明（见下方解析）
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
            self.datasets[ds["name"]] = Dataset(name=ds["name"], source=ds["source"], fields=fields)
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
            # ATLAS 治理扩展 → owner（供检索 rerank 的 owner 优先级信号使用）
            owner = ""
            for ext in m.get("custom_extensions", []):
                if ext.get("vendor_name") == "ATLAS":
                    try:
                        gov = json.loads(ext["data"]).get("governance", {})
                    except (KeyError, json.JSONDecodeError):
                        continue
                    owner = str(gov.get("owner", ""))
            self.metric_owners[m["name"]] = owner

        # time_dimension：模型级声明（compiler 时间谓词的时间表/列来源，不再硬编码
        # dim_date + CalendarYearID 等；金融 single / 零售 composite，见各 YAML 头注记）
        for ext in model.get("custom_extensions", []):
            if ext.get("vendor_name") != "ATLAS":
                continue
            try:
                data = json.loads(ext["data"])
            except (KeyError, json.JSONDecodeError) as exc:
                raise CompileError(f"custom_extensions data JSON 解析失败：{exc}") from exc
            td = data.get("time_dimension")
            if td is not None:
                self.time_dimension = td
                break

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


def load_locale_synonyms(locale: str) -> dict[str, dict[str, tuple[str, ...]]]:
    """读 `semantic/synonyms/<locale>.yml` → 归一化同义词表（启动加载一次）。

    locale 是**封闭注册表**（_LOCALE_FILES）：不在表内直接 ValueError，不拼接
    任意文件名。语义模型仍是定义权威源（ossie/），本函数只加载解析器的 locale
    形态层补充（英文措辞在 en_us.yml；模型 ai_context.synonyms 为中文注记）。
    已注册但文件不存在 = 配置缺失 → ValueError（宁可启动即失败，不静默降级出
    口径）。**声明顺序保留**——解析器按“模型注记 + 本表”顺序做最长命中匹配，
    顺序影响歧义判定。
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
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"同义词表顶层必须是映射：{path}")
    unknown = set(doc) - _LOCALE_KEYS
    if unknown:
        raise ValueError(f"同义词表不支持的顶层键 {sorted(unknown)}：{path}")

    def _section(key: str) -> dict[str, tuple[str, ...]]:
        raw = doc.get(key) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"同义词表 {key} 必须是 名称→[措辞, ...] 映射：{path}")
        out: dict[str, tuple[str, ...]] = {}
        for name, items in raw.items():
            if isinstance(items, str) or not isinstance(items, (list, tuple)):
                raise ValueError(f"{key}.{name} 必须是字符串列表：{path}")
            words = tuple(str(x).strip() for x in items)
            if any(not w for w in words):
                raise ValueError(f"{key}.{name} 含空措辞：{path}")
            if len(set(words)) != len(words):
                raise ValueError(f"{key}.{name} 措辞重复：{path}")
            out[str(name)] = words
        return out

    loaded = {
        "metric_synonyms": _section("metric_synonyms"),
        "dimension_synonyms": _section("dimension_synonyms"),
    }
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


def _pattern_doc(path: Path, locale: str) -> dict[str, Any]:
    """读并粗校验形态词典文件（存在性 + 顶层映射 + 节白名单）。"""
    if not path.exists():
        raise ValueError(f"locale={locale} 的形态词典缺失：{path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"形态词典顶层必须是映射：{path}")
    required = _PATTERN_SECTIONS[locale]
    missing = [key for key in required if key not in doc]
    if missing:
        raise ValueError(f"形态词典缺节 {missing}（必需节：{list(required)}）：{path}")
    unknown = sorted(set(doc) - set(required))
    if unknown:
        raise ValueError(f"形态词典不支持的顶层键 {unknown}：{path}")
    return doc


def _compile_pattern(
    raw: Any, where: str, path: Path, flags: int = 0
) -> re.Pattern[str]:
    """模式串 → 已编译正则。**不做 strip**：首尾空白在正则里有语义。"""
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{where} 必须是非空正则字符串：{path}")
    try:
        return re.compile(raw, flags)
    except re.error as exc:
        raise ValueError(f"{where} 不是合法正则（{exc}）：{path}") from exc


def _pattern_flags(raw: Any, where: str, path: Path) -> int:
    """`flags: [IGNORECASE]` → re 标志位或值；未知/重复 flag 名报错。"""
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError(f"{where} 必须是非空 flag 名列表：{path}")
    merged = 0
    for name in raw:
        if not isinstance(name, str) or name not in _PATTERN_FLAGS:
            raise ValueError(
                f"{where} 含不支持的 flag {name!r}"
                f"（允许：{', '.join(sorted(_PATTERN_FLAGS))}）：{path}"
            )
        if merged & _PATTERN_FLAGS[name]:
            raise ValueError(f"{where} flag 重复：{name}（{path}）")
        merged |= _PATTERN_FLAGS[name]
    return merged


def _pattern_words(raw: Any, where: str, path: Path) -> tuple[str, ...]:
    """词表（相对时间词/追问前缀词）→ 元组；禁止首尾空白与重复。"""
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError(f"{where} 必须是非空字符串列表：{path}")
    words = tuple(str(x) for x in raw)
    if any(not w or w != w.strip() for w in words):
        raise ValueError(f"{where} 含空词或首尾空白：{path}")
    if len(set(words)) != len(words):
        raise ValueError(f"{where} 词条重复：{path}")
    return words


def _pattern_map(raw: Any, where: str, path: Path) -> dict[str, int]:
    """量级/中文编号映射 → dict[str, int]（bool 不算 int）。"""
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{where} 必须是 词→整数倍率 映射：{path}")
    out: dict[str, int] = {}
    for key, value in raw.items():
        name = str(key)
        if not name or name != name.strip():
            raise ValueError(f"{where} 含空键或首尾空白键：{path}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{where}.{name} 必须是整数：{path}")
        out[name] = value
    return out


def _single_node(node: Any, where: str, path: Path) -> re.Pattern[str]:
    """单 pattern 节（`grouping` / `threshold.greater` 等）：必须恰含 pattern 一键。"""
    if not isinstance(node, dict) or set(node) != {"pattern"}:
        raise ValueError(f"{where} 必须恰含 pattern 一键：{path}")
    return _compile_pattern(node["pattern"], f"{where}.pattern", path)


def _time_pattern_items(
    items: Any, path: Path, *, shared: dict[str, re.Pattern[str]] | None = None
) -> list[tuple[str, re.Pattern[str]]]:
    """`time.patterns` → **有序** (kind, 已编译正则) 列表（两 locale 共用）。

    顺序即语义：解析按声明顺序逐个尝试，短模式先跑会截获长模式的输入。
    `shared is None`（中文词典）——条目必须恰含 `kind` 与 `pattern`：中文形态
    既不需要 flags，也没有可引用的上游。给出 `shared`（英文词典）时额外允许：
    - `{kind, ref}`：引用上游（zh 词典）同名 kind，复用**同一已编译对象**；
      ref 不携带 pattern，因此不存在"先抄一份再用 ref 校对"的半搬运动作；
    - `{kind, pattern, flags}`：声明式正则标志（见 _PATTERN_FLAGS）。
    """
    if isinstance(items, str) or not isinstance(items, (list, tuple)) or not items:
        raise ValueError(f"time.patterns 必须是 非空的 kind/pattern 列表：{path}")
    kinds: list[str] = []
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for index, item in enumerate(items):
        where = f"time.patterns[{index}]"
        if not isinstance(item, dict) or "kind" not in item:
            raise ValueError(f"{where} 必须是含 kind 的映射：{path}")
        kind = str(item["kind"])
        if not kind or kind != kind.strip():
            raise ValueError(f"{where}.kind 非法（空或含首尾空白）：{path}")
        if kind in kinds:
            raise ValueError(f"time.patterns kind 重复：{kind}（{path}）")
        keys = set(item) - {"kind"}
        if keys == {"pattern"}:
            regex = _compile_pattern(item["pattern"], f"{where}.pattern", path)
        elif shared is not None and keys == {"ref"}:
            target = str(item["ref"])
            if target not in shared:
                raise ValueError(
                    f"{where}.ref 指向不存在的形态 {target!r}"
                    f"（可引用：{', '.join(sorted(shared))}）：{path}"
                )
            regex = shared[target]  # 同一对象，不是副本
        elif shared is not None and keys == {"pattern", "flags"}:
            regex = _compile_pattern(
                item["pattern"],
                f"{where}.pattern",
                path,
                _pattern_flags(item["flags"], f"{where}.flags", path),
            )
        else:
            allowed = "kind+pattern" + ("/ref" if shared is not None else "")
            raise ValueError(
                f"{where} 必须恰含 {allowed}（flags 只能配 pattern）：{path}"
            )
        kinds.append(kind)
        compiled.append((kind, regex))
    return compiled


def _load_zh_patterns(path: Path) -> dict[str, Any]:
    """中文形态词典 → 归一化结构（节名与 planner 解析阶段一一对应）。

    `time.patterns` 保留为**有序** (kind, 已编译正则) 元组：解析按声明顺序逐个
    尝试，短模式先跑会截获长模式的输入（"2013 年 7 月" 被读成年份），顺序即语义。
    kind 与解析器实现的一致性由 planner 校验（分发表在此处不可见）。
    """
    doc = _pattern_doc(path, "zh_cn")

    time_doc = doc["time"]
    if not isinstance(time_doc, dict) or set(time_doc) != {"relative_reject", "patterns"}:
        raise ValueError(f"time 必须恰含 relative_reject 与 patterns 两键：{path}")
    reject = time_doc["relative_reject"]
    if not isinstance(reject, dict) or set(reject) != {"words"}:
        raise ValueError(f"time.relative_reject 必须恰含 words 一键：{path}")
    compiled = _time_pattern_items(time_doc["patterns"], path)

    magnitude = doc["magnitude"]
    if not isinstance(magnitude, dict) or set(magnitude) != {"cn_units", "cn_numerals"}:
        raise ValueError(f"magnitude 必须恰含 cn_units 与 cn_numerals 两键：{path}")

    threshold = doc["threshold"]
    if not isinstance(threshold, dict) or set(threshold) != {"greater", "less"}:
        raise ValueError(f"threshold 必须恰含 greater 与 less 两键：{path}")

    followup = doc["followup"]
    if not isinstance(followup, dict) or set(followup) != {"prefixes", "dim_pattern"}:
        raise ValueError(f"followup 必须恰含 prefixes 与 dim_pattern 两键：{path}")

    return {
        "time": {
            "relative_reject": {
                "words": _pattern_words(
                    reject["words"], "time.relative_reject.words", path
                )
            },
            "patterns": tuple(compiled),
        },
        "grouping": {"pattern": _single_node(doc["grouping"], "grouping", path)},
        "topn": {"pattern": _single_node(doc["topn"], "topn", path)},
        "filter_include": {
            "pattern": _single_node(doc["filter_include"], "filter_include", path)
        },
        "filter_exclude": {
            "pattern": _single_node(doc["filter_exclude"], "filter_exclude", path)
        },
        "threshold": {
            side: {
                "pattern": _single_node(threshold[side], f"threshold.{side}", path)
            }
            for side in ("greater", "less")
        },
        "magnitude": {
            "cn_units": _pattern_map(magnitude["cn_units"], "magnitude.cn_units", path),
            "cn_numerals": _pattern_map(
                magnitude["cn_numerals"], "magnitude.cn_numerals", path
            ),
        },
        "followup": {
            "prefixes": _pattern_words(followup["prefixes"], "followup.prefixes", path),
            "dim_pattern": _compile_pattern(
                followup["dim_pattern"], "followup.dim_pattern", path
            ),
        },
    }


def _months_map(raw: Any, month_re: re.Pattern[str], path: Path) -> dict[str, int]:
    """`months`（小写月份名 → 序号）+ 与 month 形态的交叉校验。

    月份名同时活在两处：`time.patterns` 里 month 模式的交替串、与本表的键——这是
    英文词典唯一一处"同一事实两个书写位置"（交替串是正则一部分，不能由代码重组）。
    加载期强制：本表每个键都必须能被该模式命中，且捕获组 lower() 后回落到同一名字
    （改了表没改模式、或改了模式没改表，都在此响亮报错，而不是解析时 KeyError）。
    """
    months = _pattern_map(raw, "months", path)
    for name in months:
        if name != name.lower():
            raise ValueError(
                f"months 键必须小写（查表前对捕获组做 lower()）：{name}（{path}）"
            )
        for probe in (name, name.capitalize()):
            hit = month_re.search(f"{probe} 2014")
            if hit is None or hit.group(1).lower() != name:
                raise ValueError(
                    f"months.{name} 与 time.patterns 的 month 模式不同源："
                    f"{probe!r} 未被该模式命中（{path}）"
                )
    return months


def _load_en_patterns(path: Path) -> dict[str, Any]:
    """英文形态词典 → 归一化结构（与中文同构的两处差异：ref / flags）。

    ISO 日期与 ISO 季度与语言无关，本词典以 `ref` 引用 zh 词典的同名形态，加载期
    解析为**同一已编译对象**（权威源唯一，不会改一处漏一处）；因此 zh 词典必须先
    加载（由本函数触发，进程内缓存）。`months` 表与 month 模式的交替串交叉校验。
    """
    doc = _pattern_doc(path, "en_us")

    shared = dict(load_locale_patterns("zh_cn")["time"]["patterns"])
    time_doc = doc["time"]
    expected = {"relative_reject", "patterns", "threshold_gate"}
    if not isinstance(time_doc, dict) or set(time_doc) != expected:
        raise ValueError(f"time 必须恰含 {sorted(expected)} 三键：{path}")
    words: dict[str, tuple[str, ...]] = {}
    for key in ("relative_reject", "threshold_gate"):
        node = time_doc[key]
        if not isinstance(node, dict) or set(node) != {"words"}:
            raise ValueError(f"time.{key} 必须恰含 words 一键：{path}")
        words[key] = _pattern_words(node["words"], f"time.{key}.words", path)
    compiled = _time_pattern_items(time_doc["patterns"], path, shared=shared)
    by_kind = dict(compiled)
    if "month" not in by_kind:
        raise ValueError(f"time.patterns 必须含 kind: month（与 months 表交叉校验）：{path}")

    magnitude = doc["magnitude"]
    if not isinstance(magnitude, dict) or set(magnitude) != {"en_units"}:
        raise ValueError(f"magnitude 必须恰含 en_units 一键：{path}")

    threshold = doc["threshold"]
    if not isinstance(threshold, dict) or set(threshold) != {"greater", "less"}:
        raise ValueError(f"threshold 必须恰含 greater 与 less 两键：{path}")

    followup = doc["followup"]
    if not isinstance(followup, dict) or set(followup) != {"what", "instead"}:
        raise ValueError(f"followup 必须恰含 what 与 instead 两键：{path}")

    single = ("grouping", "topn", "topn_dim", "filter_include", "filter_exclude")
    return {
        "time": {
            "relative_reject": {"words": words["relative_reject"]},
            "patterns": tuple(compiled),
            "threshold_gate": {"words": words["threshold_gate"]},
        },
        "months": _months_map(doc["months"], by_kind["month"], path),
        **{key: {"pattern": _single_node(doc[key], key, path)} for key in single},
        "threshold": {
            side: {
                "pattern": _single_node(threshold[side], f"threshold.{side}", path)
            }
            for side in ("greater", "less")
        },
        "magnitude": {
            "en_units": _pattern_map(
                magnitude["en_units"], "magnitude.en_units", path
            )
        },
        "followup": {
            side: {"pattern": _single_node(followup[side], f"followup.{side}", path)}
            for side in ("what", "instead")
        },
    }


_PATTERN_LOADERS: dict[str, Callable[[Path], dict[str, Any]]] = {
    "zh_cn": _load_zh_patterns,
    "en_us": _load_en_patterns,
}


def load_locale_patterns(locale: str) -> dict[str, Any]:
    """读 `semantic/synonyms/patterns_<locale>.yml` → 形态触发词词典（一次加载）。

    返回按节组织的归一化结构，模式串已编译为 `re.Pattern`（顺序保留）。
    locale 封闭注册表：未注册 → ValueError（不拼接任意文件名）；已注册但文件
    缺失 → ValueError（宁可启动即失败，不出口径）。英文词典（en_us，B3b）可
    以 `ref` 引用中文词典的共享形态，因此加载 en 会连带加载 zh（见 _PATTERN_LOADERS）。
    """
    if locale in _PATTERNS_CACHE:
        return _PATTERNS_CACHE[locale]
    if locale not in _PATTERN_FILES:
        raise ValueError(
            f"未知 patterns locale {locale!r}（已注册：{', '.join(sorted(_PATTERN_FILES))}；"
            f"新增 locale 需同步 ADR-0015 与本注册表）"
        )
    path = SYNONYMS_DIR / _PATTERN_FILES[locale]
    loaded = _PATTERN_LOADERS[locale](path)
    _PATTERNS_CACHE[locale] = loaded
    return loaded


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

        sql = select.sql()
        # 规范要求：生成的 SQL 必须可被 sqlglot 往返解析
        roundtrip = sqlglot.parse_one(sql).sql()
        if roundtrip != sql:
            raise CompileError(f"生成 SQL 无法往返解析：\n{sql}\nvs\n{roundtrip}")
        return sql, notes

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
