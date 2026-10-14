#!/usr/bin/env python3
"""概念索引 AOT 构建器（ADR-0032 M1-T1）：FIBO 闭包 → 静态概念索引。

为什么存在
----------
FIBO 是项目的语义锚点（ADR-0007），但运行时不得解析本体（rdflib 仅构建期，
ADR-0032 ④）。本构建器在构建期一次性解析锁定闭包 + 读对齐映射 + 读别名文件，
编译为 `semantic/ontology/atlas_finance.concept_index.json`（入库提交），
运行时纯 JSON 查表。

口径（ADR-0032 ② ④）
---------------------
- 层级：仅物化 registry 概念集合内的归纳子图；跨过未注册中间类的边打
  `transitive: true`，不物化中间类；
- 术语三源：S1=本体注解（rdfs:label / cmns-av:synonym / cmns-av:abbreviation /
  skos:prefLabel；定义自 skos:definition，不参与匹配）、S2=对齐对象的
  `ai_context.synonyms`（带 via provenance）、S3=概念级人工别名
  （semantic/synonyms/concepts_zh_cn.yml，M1-T2 才建，缺省空）；
- 反向索引 objects：自 `fibo_alignment` 重排（31 条映射全量落位）；
- registry 外 IRI → **响亮失败**（先跑 check_iris.py 入 registry，再重建）；
- 产物全量排序（sort_keys + 列表内排序）+ `--check` 剔除 generated_at 比对，
  保证重建确定性与漂移可查。

纯函数层（无 rdflib，测试对象）
------------------------------
`induce_hierarchy` / `assemble_index` 可在无 rdflib 环境单测；接口中 `MappingEntry`
的 `synonyms` 字段与 `assemble_index` 的 `fibo_facts` 参数为 S2/S1 数据入口
（计划 §4.2 M1-T1 步骤以「术语三源合并测试 + 产物 schema」为准的补全，见提交说明）。

用法（从仓库根执行；rdflib 仅构建期）：
    uv run --with rdflib==7.6.0 python data/fibo/build_concept_index.py
    uv run --with rdflib==7.6.0 python data/fibo/build_concept_index.py --check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict

ROOT = Path(__file__).resolve().parent.parent.parent  # 仓库根
REGISTRY_PATH = ROOT / "data" / "fibo" / "iri_registry.json"
OSSIE_PATH = ROOT / "semantic" / "ossie" / "atlas_finance.ossie.yaml"
ALIASES_PATH = ROOT / "semantic" / "synonyms" / "concepts_zh_cn.yml"
OUTPUT_PATH = ROOT / "semantic" / "ontology" / "atlas_finance.concept_index.json"
TZ = timezone(timedelta(hours=8))
CMNS_AV = "https://www.omg.org/spec/Commons/AnnotationVocabulary/"


class MappingEntry(TypedDict):
    """一条 fibo_alignment 映射（object 键形如 metric:x / dim_d / dim_d.F / a->b）。"""

    object: str
    concept_iri: str
    match_type: str
    confidence: str
    note: str
    synonyms: list[str]  # S2 数据入口：对齐对象的 ai_context.synonyms（提取层填充）


class FiboFacts(TypedDict):
    """概念的本体侧素材（S1 数据入口：提取层从 rdflib 图采集后以纯数据传入）。"""

    label: str
    definition: str | None
    terms: list[str]


class HierarchyFacts(TypedDict):
    """registry 内归纳子图的单概念视图（短名 + transitive 布尔）。"""

    parents: list[tuple[str, bool]]
    children: list[tuple[str, bool]]


def _lang(text: str) -> str:
    """术语语种启发式（与 agent/planner._resolve_locale 同 CJK 规则）。"""
    return "zh" if any("\u4e00" <= ch <= "\u9fff" for ch in text) else "en"


def _nearest(
    first_layer: list[str], edges: dict[str, list[str]], is_reg: set[str]
) -> dict[str, bool]:
    """最近 registry 邻居归纳：直达 → False；经未注册中间类 → True（False 优先）。

    参数
    ----
    first_layer : 起点的直接邻居（IRI）。
    edges : 邻居方向的全量边（IRI → 下一跳 IRI 列表；含未注册节点）。
    is_reg : registry 概念 IRI 集合。

    返回
    ----
    最近 registry 邻居 → 是否跨级（transitive）。命中 registry 的路径在终点
    停下（不越过它继续），保证「最近」语义。
    """
    out: dict[str, bool] = {}
    stack: list[str] = []
    for node in first_layer:
        if node in is_reg:
            out[node] = False  # 直达优先；同节点稍后经跨级路径到达不翻转
        else:
            stack.append(node)
    seen: set[str] = set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        for nxt in edges.get(node, []):
            if nxt in is_reg:
                out.setdefault(nxt, True)
            elif nxt not in seen:
                stack.append(nxt)
    return out


def induce_hierarchy(
    edges: dict[str, list[str]], registry: dict[str, str]
) -> dict[str, HierarchyFacts]:
    """原始 subClassOf 边 → registry 内归纳子图（跨级边 transitive=True）。

    参数
    ----
    edges : 键与值均为 IRI 的父类邻接表（节点空间含 registry 概念与中间类）。
    registry : 概念短名 → IRI（仅 registry 内概念进输出）。

    返回
    ----
    短名 → {parents, children}；值均为「(短名, transitive)」列表并按短名排序，
    未注册中间类不出现。
    """
    is_reg = set(registry.values())
    iri_to_name = {iri: name for name, iri in registry.items()}
    down: dict[str, list[str]] = {}
    for child, parent_list in edges.items():
        for parent in parent_list:
            down.setdefault(parent, []).append(child)
    out: dict[str, HierarchyFacts] = {}
    for name in sorted(registry):
        iri = registry[name]
        parents = sorted(
            (iri_to_name[i], t) for i, t in _nearest(edges.get(iri, []), edges, is_reg).items()
        )
        children = sorted(
            (iri_to_name[i], t) for i, t in _nearest(down.get(iri, []), down, is_reg).items()
        )
        out[name] = {"parents": parents, "children": children}
    return out


def assemble_index(
    registry: dict[str, str],
    mapped: list[MappingEntry],
    hierarchy: dict[str, HierarchyFacts],
    aliases: dict[str, list[str]],
    fibo_facts: dict[str, FiboFacts],
) -> dict[str, Any]:
    """组装概念索引 body（`{"concepts": {...}}`，头部由调用方补齐）。

    参数
    ----
    registry : 概念短名 → IRI（权威集合）。
    mapped : fibo_alignment 映射（含 S2 synonyms）。
    hierarchy : induce_hierarchy 的产物。
    aliases : 概念短名 → 人工别名列表（S3）。
    fibo_facts : 概念短名 → 本体侧素材（S1）。

    返回
    ----
    `{"concepts": {短名: {iri, label, definition, terms, parents, children,
    objects}}}`；terms 按 (source, text, via) 排序、同 (text, source, via)
    去重；objects 按 object 键排序。

    抛什么
    ------
    ValueError : mapped 含 registry 外 IRI（消息含 IRI 清单——先跑
        check_iris.py 入 registry 再重建）。
    """
    known = set(registry.values())
    foreign = sorted({m["concept_iri"] for m in mapped} - known)
    if foreign:
        raise ValueError(
            "fibo_alignment 含 registry 外 IRI（先跑 check_iris.py 入 registry 再重建）："
            + ", ".join(foreign)
        )
    by_iri: dict[str, list[MappingEntry]] = {}
    for entry in mapped:
        by_iri.setdefault(entry["concept_iri"], []).append(entry)
    concepts: dict[str, Any] = {}
    for name in sorted(registry):
        iri = registry[name]
        facts = fibo_facts.get(
            name, {"label": iri.rsplit("/", 1)[-1], "definition": None, "terms": []}
        )
        terms = (
            [(text, "S1", None) for text in facts["terms"]]
            + [
                (text, "S2", entry["object"])
                for entry in by_iri.get(iri, [])
                for text in entry["synonyms"]
            ]
            + [(text, "S3", None) for text in aliases.get(name, [])]
        )
        unique = sorted(set(terms), key=lambda t: (t[1], t[0], t[2] or ""))
        concepts[name] = {
            "iri": iri,
            "label": facts["label"],
            "definition": facts["definition"],
            "terms": [
                {"text": text, "lang": _lang(text), "source": source, "via": via}
                for text, source, via in unique
            ],
            "parents": [
                {"concept": c, "transitive": t}
                for c, t in hierarchy.get(name, {"parents": [], "children": []})["parents"]
            ],
            "children": [
                {"concept": c, "transitive": t}
                for c, t in hierarchy.get(name, {"parents": [], "children": []})["children"]
            ],
            "objects": [
                {
                    "object": entry["object"],
                    "match_type": entry["match_type"],
                    "confidence": entry["confidence"],
                    "note": entry["note"],
                }
                for entry in sorted(by_iri.get(iri, []), key=lambda e: e["object"])
            ],
        }
    return {"concepts": concepts}


def _load_graph() -> Any:
    """加载 FIBO 闭包图（延迟 import：rdflib 仅构建期用，tests 导入本模块零依赖）。"""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    from check_iris import load_graph  # type: ignore[import-not-found]

    return load_graph()


def _extract_edges(g: Any, registry: dict[str, str]) -> dict[str, list[str]]:
    """registry 概念向上 BFS 的全部 subClassOf 边（含未注册中间类；bnode 跳过）。"""
    from rdflib import URIRef
    from rdflib.namespace import RDFS

    edges: dict[str, list[str]] = {}
    visited: set[str] = set()
    queue = list(registry.values())
    while queue:
        iri = queue.pop(0)
        if iri in visited:
            continue
        visited.add(iri)
        parents: list[str] = []
        for obj in g.objects(URIRef(iri), RDFS.subClassOf):
            if not isinstance(obj, URIRef):
                continue  # owl:Restriction 等 bnode 不是父类
            parent = str(obj)
            parents.append(parent)
            if parent not in visited:
                queue.append(parent)
        if parents:
            edges[iri] = parents
    return edges


def _extract_facts(g: Any, registry: dict[str, str]) -> dict[str, FiboFacts]:
    """S1 素材：label（rdfs:label → skos:prefLabel → IRI 尾段）、definition、术语四属性。"""
    from rdflib import URIRef
    from rdflib.namespace import OWL, RDF, RDFS, SKOS

    classes = set(g.subjects(RDF.type, OWL.Class))
    facts: dict[str, FiboFacts] = {}
    missing: list[str] = []
    for name in sorted(registry):
        iri = registry[name]
        uri = URIRef(iri)
        if uri not in classes:
            missing.append(iri)
            continue
        labels = _literals(g, uri, RDFS.label)
        prefs = _literals(g, uri, SKOS.prefLabel)
        syns = _literals(g, uri, URIRef(CMNS_AV + "synonym"))
        abbrs = _literals(g, uri, URIRef(CMNS_AV + "abbreviation"))
        defs = _literals(g, uri, SKOS.definition)
        label = (labels or prefs or [iri.rsplit("/", 1)[-1]])[0]
        facts[name] = {
            "label": label,
            "definition": defs[0] if defs else None,
            "terms": labels + syns + abbrs + prefs,
        }
    if missing:
        raise ValueError(
            "registry 概念不在锁定闭包图中（停批：回数据层排查，禁改 registry 掩盖）："
            + ", ".join(missing)
        )
    return facts


def _literals(g: Any, uri: Any, prop: Any) -> list[str]:
    """取 (uri, prop) 的全部 Literal 字符串（按值排序保证采集确定性）。"""
    from rdflib import Literal

    return sorted(str(o) for o in g.objects(uri, prop) if isinstance(o, Literal))


def _object_synonyms(
    obj: str,
    datasets: dict[str, Any],
    metrics: dict[str, Any],
    relationships: list[Any],
) -> list[str]:
    """对齐对象键 → 该对象在语义模型中的 ai_context.synonyms（定位失败响亮报错）。"""
    target: Any = None
    if obj.startswith("metric:"):
        target = metrics.get(obj.removeprefix("metric:"))
    elif "->" in obj:
        src, dst = obj.split("->", 1)
        target = next((r for r in relationships if r["from"] == src and r["to"] == dst), None)
    elif "." in obj:
        table, field = obj.split(".", 1)
        dataset = datasets.get(table)
        if dataset is not None:
            target = next((f for f in dataset["fields"] if f["name"] == field), None)
    else:
        target = datasets.get(obj)
    if target is None:
        raise ValueError(f"fibo_alignment 对象在语义模型中定位失败：{obj!r}")
    ai_context = target.get("ai_context") or {}
    return [str(s) for s in (ai_context.get("synonyms") or [])]


def _extract_mapped() -> list[MappingEntry]:
    """从 ossie custom_extensions.fibo_alignment 提取 31 条映射（含 S2 synonyms）。"""
    import yaml

    doc = yaml.safe_load(OSSIE_PATH.read_text(encoding="utf-8"))
    model = doc["semantic_model"][0]
    atlas = next(c for c in model["custom_extensions"] if c["vendor_name"] == "ATLAS")
    data = atlas["data"]
    alignment = (json.loads(data) if isinstance(data, str) else data)["fibo_alignment"]
    datasets = {d["name"]: d for d in model["datasets"]}
    metrics = {m["name"]: m for m in model["metrics"]}
    relationships = model["relationships"]
    mapped: list[MappingEntry] = []
    for obj in sorted(alignment["mappings"]):
        m = alignment["mappings"][obj]
        mapped.append(
            {
                "object": obj,
                "concept_iri": m["concept"],
                "match_type": m["match_type"],
                "confidence": m["confidence"],
                "note": m["note"],
                "synonyms": _object_synonyms(obj, datasets, metrics, relationships),
            }
        )
    return mapped


def _load_aliases() -> dict[str, list[str]]:
    """S3 人工别名（semantic/synonyms/concepts_zh_cn.yml；M1-T2 前缺省为空）。"""
    if not ALIASES_PATH.exists():
        return {}
    import yaml

    doc = yaml.safe_load(ALIASES_PATH.read_text(encoding="utf-8")) or {}
    return {
        str(name): [str(term) for term in (terms or [])]
        for name, terms in (doc.get("concepts") or {}).items()
    }


def build_report() -> dict[str, Any]:
    """完整构建：闭包 → 提取（S1/映射/边）→ 归纳 → 组装 → 补头部。"""
    registry_doc = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    registry = dict(registry_doc["concepts"])
    g = _load_graph()
    mapped = _extract_mapped()
    facts = _extract_facts(g, registry)
    hierarchy = induce_hierarchy(_extract_edges(g, registry), registry)
    body = assemble_index(registry, mapped, hierarchy, _load_aliases(), facts)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "fibo_commit": registry_doc["fibo_commit"],
        "commons_release": registry_doc["commons_release"],
        "source_semantic_sha": hashlib.sha256(OSSIE_PATH.read_bytes()).hexdigest(),
        **body,
    }


def _diff(rebuilt: Any, stored: Any, path: str = "$") -> list[str]:
    """递归比对重建值与入库产物，返回差异字段路径（空 = 一致）。"""
    lines: list[str] = []
    if isinstance(rebuilt, dict) and isinstance(stored, dict):
        for key in sorted(set(rebuilt) | set(stored)):
            if key not in rebuilt:
                lines.append(f"{path}.{key}: 产物多余")
            elif key not in stored:
                lines.append(f"{path}.{key}: 产物缺失")
            else:
                lines.extend(_diff(rebuilt[key], stored[key], f"{path}.{key}"))
        return lines
    if isinstance(rebuilt, list) and isinstance(stored, list):
        if len(rebuilt) != len(stored):
            return [f"{path}: 列表长度 重建={len(rebuilt)} 产物={len(stored)}"]
        for i, (a, b) in enumerate(zip(rebuilt, stored, strict=True)):
            lines.extend(_diff(a, b, f"{path}[{i}]"))
        return lines
    if rebuilt != stored:
        return [f"{path}: 重建={rebuilt!r} 产物={stored!r}"]
    return lines


def main(argv: list[str] | None = None) -> int:
    """入口：默认写产物；`--check` 重建并与入库产物比对（忽略 generated_at）。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="重建并比对入库产物（忽略 generated_at）"
    )
    args = parser.parse_args(argv)
    report = build_report()
    if args.check:
        if not OUTPUT_PATH.exists():
            print(
                f"[check] 产物不存在：{OUTPUT_PATH.relative_to(ROOT)}（先跑 make ontology-build）",
                file=sys.stderr,
            )
            return 1
        stored = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        rebuilt = {k: v for k, v in report.items() if k != "generated_at"}
        stored_cmp = {k: v for k, v in stored.items() if k != "generated_at"}
        diffs = _diff(rebuilt, stored_cmp)
        if diffs:
            print(f"[check] 漂移 {len(diffs)} 处（重建 vs 产物）：", file=sys.stderr)
            for line in diffs[:50]:
                print(f"  {line}", file=sys.stderr)
            return 1
        print("[check] 无漂移（剔除 generated_at 后一致）")
        return 0
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    n_objects = sum(len(c["objects"]) for c in report["concepts"].values())
    print(
        f"[done] {OUTPUT_PATH.relative_to(ROOT)}："
        f"concepts={len(report['concepts'])}，objects={n_objects}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
