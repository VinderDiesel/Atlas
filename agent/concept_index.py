"""概念索引 load / match / expand（ADR-0032 ④/②）：构建期 AOT 产物的只读消费者

设计原则
--------
1. **无索引 = 全缝旁路**（ADR-0032 总原则）：产物文件缺失（`atlas_retail` 无
   `fibo_alignment` 映射、不生成文件）或 `ATLAS_CONCEPT_LAYER=off/0/false` →
   `load_concept_index` 返回 `None`；消费缝（检索/解析/解释）拿到 None 时行为与
   引入概念层前**逐字一致**（沿 ADR-0030 ⑤ 注入哲学）。这是契约，不是降级。
2. **加载即强校验，失败响亮**：JSON 畸形 / 缺键 / 类型错 → `ValueError`（消息含
   路径），不静默降级半层（ADR-0032 ④，镜像 `agent/value_domain.py::parse_profile`）。
3. **零本体解析依赖**：本模块只读静态 JSON——FIBO 闭包解析仅发生在构建期
   （`data/fibo/build_concept_index.py`，按需 `--with` 注入），运行时依赖树不含
   任何 RDF / 本体库。
4. **确定性查表**：匹配/扩展是纯函数（无 LLM、无推理、无网络）。`match_terms` 按
   问句 locale 取语种（ADR-0032 ③），最长命中优先、同长按索引序；**不去重多命中**
   ——歧义由消费缝的规则裁决（解析缝的"分层唯一性"按对象层映射做，不在术语层）。

已知边界（诚实声明）
------------------
- 匹配为**子串**语义（en 大小写不敏感、zh 原文子串）：S1 英文术语可能命中更长
  单词的一部分（如 `fee` ⊂ `coffee`）。术语源是 label/synonym 短语，误命中影响
  由 M2/M3 对照实测评估，不预设结论。
- 索引是**构建期快照**：ossie / 别名文件 / FIBO 闭包变更后未重建 = 漂移，由
  `make lint` 第 7 节与 `make ontology-build CHECK=1` 两级防线拦截（ADR-0032 ⑤）。
- `ConceptHit` 不携带 match_type / confidence——那是**对象级映射**的属性，解析缝
  的"分层唯一性"经 `ConceptIndex.concepts[hit.concept].objects` 消费（M3 落地）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ONTOLOGY_DIR = REPO / "semantic" / "ontology"

# 运行开关（ADR-0032 ⑤；命中 ATLAS_* 惯例）：默认 on，off 族 = 全缝旁路
LAYER_ENV = "ATLAS_CONCEPT_LAYER"
_LAYER_OFF = frozenset({"off", "0", "false"})

_TERM_LANGS = frozenset({"en", "zh"})
_TERM_SOURCES = frozenset({"S1", "S2", "S3"})
_REQUIRED_KEYS = (
    "schema_version",
    "generated_at",
    "fibo_commit",
    "commons_release",
    "source_semantic_sha",
    "concepts",
)


@dataclass(frozen=True)
class Term:
    """一条术语（`text` 为原文；`via` 为 S2 的对齐对象 provenance，其余为 None）。"""

    text: str
    lang: str
    source: str
    via: str | None


@dataclass(frozen=True)
class Edge:
    """层级边；`transitive=True` 表示跨过未注册中间类的归纳子图边（ADR-0032 ②）。"""

    concept: str
    transitive: bool


@dataclass(frozen=True)
class ObjectMapping:
    """对齐对象映射（objects 反向索引项；match_type/confidence 供解析缝分层用）。"""

    object: str
    match_type: str
    confidence: str
    note: str


@dataclass(frozen=True)
class ConceptEntry:
    iri: str
    label: str
    definition: str | None
    terms: tuple[Term, ...]
    parents: tuple[Edge, ...]
    children: tuple[Edge, ...]
    objects: tuple[ObjectMapping, ...]


@dataclass(frozen=True)
class ConceptIndex:
    """产物全量（concepts 保持 JSON 文件序 = 索引序）。"""

    schema_version: int
    generated_at: str
    fibo_commit: str
    commons_release: str
    source_semantic_sha: str
    concepts: dict[str, ConceptEntry]


@dataclass(frozen=True)
class ConceptHit:
    """术语命中：`term` 为术语原文，`concept` 为 registry 短名，`via` 为 S2 provenance。"""

    term: str
    concept: str
    concept_iri: str
    source: str
    via: str | None


# 进程内缓存：索引是只读构建产物，语义与 locale 词典一致（一次加载，永不热更）
_CACHE: dict[Path, ConceptIndex] = {}


def clear_cache() -> None:
    """清空加载缓存（仅测试用：tmp fixture 之间隔离同名文件）。"""
    _CACHE.clear()


def _layer_disabled() -> bool:
    """`ATLAS_CONCEPT_LAYER` 关闭族（off/0/false，大小写与首尾空格容忍）。"""
    raw = os.environ.get(LAYER_ENV)
    return raw is not None and raw.strip().casefold() in _LAYER_OFF


def load_concept_index(
    model_name: str = "atlas_finance", ontology_dir: Path = ONTOLOGY_DIR
) -> ConceptIndex | None:
    """读 `<model_name>.concept_index.json`；**规则性缺失 → None**（旁路，非错误）。

    返回 None 的两种契约情形（消费缝按"无概念层"处理）：
    1. 开关关闭（`ATLAS_CONCEPT_LAYER` off 族，ADR-0032 ⑤）；
    2. 产物文件不存在（如 `atlas_retail` 无映射 → 不生成，ADR-0032 ④）。

    文件存在但畸形 → ValueError（宁可启动即失败，不静默降级半层）。
    """
    if _layer_disabled():
        return None
    path = ontology_dir / f"{model_name}.concept_index.json"
    if not path.exists():
        return None
    if path in _CACHE:
        return _CACHE[path]
    index = parse_concept_index(path)
    _CACHE[path] = index
    return index


def parse_concept_index(path: Path) -> ConceptIndex:
    """解析并强校验产物（畸形/缺键/类型错/悬空边 → ValueError，消息含路径）。"""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"概念索引 JSON 解析失败：{path}（{exc}）") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"概念索引顶层必须是对象：{path}")
    missing = [key for key in _REQUIRED_KEYS if key not in doc]
    if missing:
        raise ValueError(f"概念索引缺键 {missing}：{path}")

    version = doc["schema_version"]
    if isinstance(version, bool) or version != 1:
        raise ValueError(f"概念索引 schema_version 未知（本加载器支持 1）：{version!r} {path}")

    raw_concepts = doc["concepts"]
    if not isinstance(raw_concepts, dict):
        raise ValueError(f"概念索引 concepts 必须是 对象：{path}")
    concepts: dict[str, ConceptEntry] = {}
    for name, raw in raw_concepts.items():
        concepts[str(name)] = _parse_entry(str(name), raw, path)
    for name, entry in concepts.items():
        for edge in (*entry.parents, *entry.children):
            if edge.concept not in concepts:
                raise ValueError(f"概念 {name} 的层级边指向未物化概念 {edge.concept!r}：{path}")

    return ConceptIndex(
        schema_version=version,
        generated_at=_require_str(doc, "generated_at", path),
        fibo_commit=_require_str(doc, "fibo_commit", path),
        commons_release=_require_str(doc, "commons_release", path),
        source_semantic_sha=_require_str(doc, "source_semantic_sha", path),
        concepts=concepts,
    )


def _require_str(doc: dict[str, object], key: str, path: Path) -> str:
    value = doc[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"概念索引 {key} 必须是非空字符串：{path}")
    return value


def _parse_entry(name: str, raw: object, path: Path) -> ConceptEntry:
    if not isinstance(raw, dict):
        raise ValueError(f"概念 {name} 必须是对象：{path}")
    iri = raw.get("iri")
    if not isinstance(iri, str) or not iri:
        raise ValueError(f"概念 {name} 的 iri 必须是非空字符串：{path}")
    label = raw.get("label")
    if not isinstance(label, str) or not label:
        raise ValueError(f"概念 {name} 的 label 必须是非空字符串：{path}")
    definition = raw.get("definition")
    if definition is not None and not isinstance(definition, str):
        raise ValueError(f"概念 {name} 的 definition 必须是字符串或 null：{path}")
    terms = tuple(_parse_term(name, item, path) for item in _as_list(raw, "terms", name, path))
    if not terms:
        # registry 概念至少有 rdfs:label（实测 29/29）——空术语表是不该存在的产物形态
        raise ValueError(f"概念 {name} 的 terms 不允许为空：{path}")
    return ConceptEntry(
        iri=iri,
        label=label,
        definition=definition,
        terms=terms,
        parents=tuple(
            _parse_edge(name, item, path) for item in _as_list(raw, "parents", name, path)
        ),
        children=tuple(
            _parse_edge(name, item, path) for item in _as_list(raw, "children", name, path)
        ),
        objects=tuple(
            _parse_object(name, item, path) for item in _as_list(raw, "objects", name, path)
        ),
    )


def _as_list(raw: dict[str, object], key: str, name: str, path: Path) -> list[object]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise ValueError(f"概念 {name} 的 {key} 必须是数组：{path}")
    return value


def _parse_term(concept: str, raw: object, path: Path) -> Term:
    if not isinstance(raw, dict):
        raise ValueError(f"概念 {concept} 的 term 必须是对象：{path}")
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"概念 {concept} 的 term.text 必须是非空字符串：{path}")
    lang = raw.get("lang")
    if lang not in _TERM_LANGS:
        raise ValueError(f"概念 {concept} 的 term.lang 未知（en/zh）：{lang!r} {path}")
    source = raw.get("source")
    if source not in _TERM_SOURCES:
        raise ValueError(f"概念 {concept} 的 term.source 未知（S1/S2/S3）：{source!r} {path}")
    via = raw.get("via")
    if via is not None and (not isinstance(via, str) or not via):
        raise ValueError(f"概念 {concept} 的 term.via 必须是非空字符串或 null：{path}")
    if source == "S2" and via is None:
        # S2 的定义就是"继承同义词 + via provenance"（ADR-0032 ②），缺 provenance 无法溯源
        raise ValueError(f"概念 {concept} 的 S2 术语缺 via provenance：{path}")
    return Term(text=text, lang=lang, source=source, via=via)


def _parse_edge(concept: str, raw: object, path: Path) -> Edge:
    if not isinstance(raw, dict):
        raise ValueError(f"概念 {concept} 的层级边必须是对象：{path}")
    target = raw.get("concept")
    if not isinstance(target, str) or not target:
        raise ValueError(f"概念 {concept} 的层级边缺 concept 短名：{path}")
    transitive = raw.get("transitive")
    if not isinstance(transitive, bool):
        raise ValueError(f"概念 {concept} 的层级边 transitive 必须是布尔：{path}")
    return Edge(concept=target, transitive=transitive)


def _parse_object(concept: str, raw: object, path: Path) -> ObjectMapping:
    if not isinstance(raw, dict):
        raise ValueError(f"概念 {concept} 的 object 必须是对象：{path}")
    obj = raw.get("object")
    if not isinstance(obj, str) or not obj:
        raise ValueError(f"概念 {concept} 的 object.object 必须是非空字符串：{path}")
    fields: dict[str, str] = {}
    for key in ("match_type", "confidence", "note"):
        value = raw.get(key)
        if not isinstance(value, str):
            raise ValueError(f"概念 {concept} 的 object.{key} 必须是字符串：{path}")
        fields[key] = value
    return ObjectMapping(
        object=obj,
        match_type=fields["match_type"],
        confidence=fields["confidence"],
        note=fields["note"],
    )


def match_terms(index: ConceptIndex, text: str, locale: str) -> tuple[ConceptHit, ...]:
    """术语命中（纯确定性查表；按问句 locale 取语种，ADR-0032 ③）。

    匹配语义：zh = 原文子串；en = casefold 子串。排序 = **最长命中优先、同长按
    索引序**（概念序 → 术语序）；**不去重多命中**（同文本不同 provenance 各保留
    一条）——歧义由消费缝的规则裁决。
    """
    if locale not in _TERM_LANGS:
        raise ValueError(f"locale 必须是 'zh' 或 'en'：{locale!r}")
    folded = text.casefold() if locale == "en" else text
    scored: list[tuple[int, int, ConceptHit]] = []
    seq = 0
    for name, entry in index.concepts.items():
        for term in entry.terms:
            if term.lang == locale:
                needle = term.text.casefold() if locale == "en" else term.text
                if needle in folded:
                    scored.append(
                        (
                            len(term.text),
                            seq,
                            ConceptHit(
                                term=term.text,
                                concept=name,
                                concept_iri=entry.iri,
                                source=term.source,
                                via=term.via,
                            ),
                        )
                    )
            seq += 1
    scored.sort(key=lambda item: (-item[0], item[1]))
    return tuple(hit for _, _, hit in scored)


def expand(index: ConceptIndex, concept_ids: Iterable[str], *, ancestors: int) -> tuple[str, ...]:
    """概念闭包：自身 + 全部后代；`ancestors=1` 时并入**最近一层祖先**（检索缝）。

    `ancestors=0` = 自身 + 后代（解析缝：不外推祖先，ADR-0032 ③）；transitive 边
    照常连通。输出按索引文件序（与输入序无关，确定序）；未注册短名 → ValueError。
    """
    if ancestors not in (0, 1):
        raise ValueError(f"ancestors 仅支持 0 / 1（防泛概念噪声，ADR-0032 ③）：{ancestors!r}")
    seeds = tuple(concept_ids)
    for name in seeds:
        if name not in index.concepts:
            raise ValueError(f"概念不在索引内：{name!r}")
    closure: set[str] = set()
    stack = list(seeds)
    while stack:
        name = stack.pop()
        if name in closure:
            continue
        closure.add(name)
        stack.extend(edge.concept for edge in index.concepts[name].children)
    if ancestors == 1:
        for name in seeds:
            closure.update(edge.concept for edge in index.concepts[name].parents)
    return tuple(name for name in index.concepts if name in closure)


def concept_terms(
    index: ConceptIndex, concept_ids: Iterable[str], locale: str, *, ancestors: int
) -> tuple[str, ...]:
    """扩展后的术语列表（按概念序 + 术语文件序，文本精确去重；locale 取语种）。

    术语源**不区分 S1/S2/S3**——消费缝自行裁决（检索缝全用；解析缝只用 S1+S3，
    ADR-0032 ③）。
    """
    if locale not in _TERM_LANGS:
        raise ValueError(f"locale 必须是 'zh' 或 'en'：{locale!r}")
    seen: set[str] = set()
    terms: list[str] = []
    for name in expand(index, concept_ids, ancestors=ancestors):
        for term in index.concepts[name].terms:
            if term.lang == locale and term.text not in seen:
                seen.add(term.text)
                terms.append(term.text)
    return tuple(terms)


def detect_locale(text: str) -> str:
    """问句 locale 启发式：与 `Planner._resolve_locale` 的 None 分支同规则（测试锁一致）。"""
    return "zh" if any("\u4e00" <= ch <= "\u9fff" for ch in text) else "en"
