"""FIBO 注解属性统计（29 条 registry 概念覆盖 + 官方术语注解存量）

固化 2026-10-08 临时探针结论（N1：引用数字必须有脚本产物）。三个口径互不混用：
1. registry 概念级：iri_registry.json 全部概念逐条统计注解数量，输出 N/29 覆盖汇总；
2. 闭包图级：锁定闭包（check_iris 种子 + owl:imports）中带各注解属性的 owl:Class
   数量与注解三元组总数；
3. 源文件级：fibo-src checkout（FND+FBC+BE）内各注解属性的出现行数，用于复核
   FIBO 弃用 skos:altLabel、改荐 cmns-av:synonym / cmns-av:abbreviation 的存量。

用法：.venv/bin/python data/fibo/probe_annotations.py
产物：data/fibo/annotation_stats.json（入库，摘要打印 stdout）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import rdflib

# 复用 check_iris 的扩展种子与闭包加载（含 TransactionsExt / FBC 域）
from check_iris import load_graph  # type: ignore[import-not-found]
from rdflib import RDF, Graph, URIRef
from rdflib.namespace import OWL, RDFS, SKOS

ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / "iri_registry.json"
OUTPUT = ROOT / "annotation_stats.json"
FIBO_SRC = ROOT / "fibo-src"

CMNS_AV = "https://www.omg.org/spec/Commons/AnnotationVocabulary/"

# 统计的注解属性（键为产物 JSON 中的字段名；skos:altLabel 保留用于复核弃用情况）
PROPERTIES: dict[str, str] = {
    "rdfs_label": str(RDFS.label),
    "skos_definition": str(SKOS.definition),
    "skos_pref_label": str(SKOS.prefLabel),
    "skos_alt_label": str(SKOS.altLabel),
    "cmns_av_synonym": CMNS_AV + "synonym",
    "cmns_av_abbreviation": CMNS_AV + "abbreviation",
}

# 源文件级扫描的目标 token（含命名空间前缀，与被统计的图级属性一一对应）
SOURCE_TOKENS: dict[str, str] = {
    "cmns-av:synonym": "cmns-av:synonym",
    "cmns-av:abbreviation": "cmns-av:abbreviation",
    "skos:altLabel": "skos:altLabel",
    "skos:prefLabel": "skos:prefLabel",
}


def registry_coverage(g: Graph) -> dict[str, object]:
    """口径 1：registry 概念逐条注解数量 + N/29 覆盖汇总。"""
    registry = json.loads(REGISTRY.read_text())
    concepts: dict[str, dict[str, int]] = {}
    for name, iri in sorted(registry["concepts"].items()):
        uri = URIRef(iri)
        concepts[name] = {
            key: sum(1 for _ in g.objects(uri, URIRef(prop))) for key, prop in PROPERTIES.items()
        }
    summary = {key: sum(1 for counts in concepts.values() if counts[key] > 0) for key in PROPERTIES}
    return {
        "total_concepts": len(concepts),
        "summary": summary,
        "concepts": concepts,
    }


def closure_stats(g: Graph) -> dict[str, object]:
    """口径 2：闭包图级注解存量（owl:Class 数 / 带注解类数 / 三元组数）。"""
    classes = set(g.subjects(RDF.type, OWL.Class))
    annotations: dict[str, dict[str, int]] = {}
    for key, prop in PROPERTIES.items():
        subjects = set(g.subjects(URIRef(prop), None))
        triples = sum(1 for _ in g.triples((None, URIRef(prop), None)))
        annotations[key] = {
            "subjects": len(subjects),
            "class_subjects": len(subjects & classes),
            "triples": triples,
        }
    return {"owl_class_count": len(classes), "annotations": annotations}


def source_scan() -> dict[str, object]:
    """口径 3：fibo-src checkout 内各注解属性出现行数（grep 同口径，非闭包范围）。"""
    stats = {tok: {"lines": 0, "files": 0} for tok in SOURCE_TOKENS}
    file_count = 0
    for path in sorted(FIBO_SRC.rglob("*.rdf")):
        file_count += 1
        text = path.read_text(encoding="utf-8", errors="ignore")
        for tok in SOURCE_TOKENS:
            n = sum(1 for line in text.splitlines() if tok in line)
            if n:
                stats[tok]["lines"] += n
                stats[tok]["files"] += 1
    return {
        "scope": "fibo-src/**/*.rdf（sparse checkout FND+FBC+BE，含域外文件）",
        "rdf_file_count": file_count,
        "tokens": stats,
    }


def main() -> None:
    g = load_graph()

    coverage = registry_coverage(g)
    closure = closure_stats(g)
    source = source_scan()

    registry = json.loads(REGISTRY.read_text())
    artifact = {
        "description": (
            "FIBO 注解属性统计（registry 概念覆盖 + 闭包/源文件存量），由 probe_annotations.py 生成"
        ),
        "generated_by": "data/fibo/probe_annotations.py",
        "fibo_commit": registry["fibo_commit"],
        "commons_release": registry["commons_release"],
        "environment": {
            "python": sys.version.split()[0],
            "rdflib": rdflib.__version__,
        },
        "registry_coverage": coverage,
        "closure": closure,
        "source_scan": source,
    }
    OUTPUT.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    total = coverage["total_concepts"]
    print(f"[1/3] registry 概念注解覆盖（{total} 条）：")
    for key in PROPERTIES:
        n = coverage["summary"][key]  # type: ignore[index]
        print(f"    {key:22s} {n}/{total}")
    print("[2/3] 闭包图级：")
    print(f"    owl:Class 共 {closure['owl_class_count']} 个")  # type: ignore[index]
    for key, s in closure["annotations"].items():  # type: ignore[index,union-attr]
        print(f"    {key:22s} 带注解类 {s['class_subjects']:5d} 个 / 三元组 {s['triples']:5d} 条")
    print(f"[3/3] 源文件级（{source['rdf_file_count']} 个 .rdf）：")
    for tok, s in source["tokens"].items():  # type: ignore[union-attr]
        print(f"    {tok:22s} {s['lines']:5d} 行 / {s['files']:3d} 文件")
    print(f"\n产物已写入 {OUTPUT.relative_to(ROOT.parent.parent)}")


if __name__ == "__main__":
    main()
