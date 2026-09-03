"""FIBO 子集冒烟验证脚本（ADR-0007 验证清单第 1 项）

验证目标：
1. FIBO FND 域 + 闭包内模块可被 rdflib 完整加载（含 OMG Commons/LCC 外部依赖）
2. TBox 查询可用：概念层级、类统计、关键金融概念（Party/Organization/Contract）
3. 记录加载时间与缺失模块（若有）

用法：uv run python data/fibo/smoke_fibo.py
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET

from rdflib import RDF, RDFS, Graph, Literal, URIRef
from rdflib.namespace import OWL, SKOS

ROOT = "/Users/yangxinlong/gitee/Atlas/data/fibo"
FIBO_SRC = f"{ROOT}/fibo-src"
CATALOG = f"{FIBO_SRC}/catalog-v001.xml"
ENTRY = "https://spec.edmcouncil.org/fibo/ontology/FND/AllFND/"

CATALOG_NS = "{urn:oasis:names:tc:entity:xmlns:xml:catalog}"


def build_internal_map() -> dict[str, str]:
    """解析 catalog-v001.xml：FIBO 本体 IRI → 本地相对路径。

    说明：xml.etree 默认不解析外部实体（Python 3.8+ 对 entity expansion 有防护），
    catalog 为 FIBO 仓库内固定文件（git 管理、非用户输入），XXE 风险面为 0。
    """
    tree = ET.parse(CATALOG)
    mapping: dict[str, str] = {}
    for uri in tree.iter(f"{CATALOG_NS}uri"):
        name = uri.get("name")
        target = uri.get("uri")
        if name and target and name.startswith("https://spec.edmcouncil.org/"):
            mapping[name] = target.lstrip("./")
    return mapping


def resolve(iri: str, internal: dict[str, str]) -> str | None:
    """IRI → 本地文件路径；外部依赖走 vendor/，内置标准忽略。"""
    if iri in internal:
        return f"{FIBO_SRC}/{internal[iri]}"
    if iri.startswith("https://www.omg.org/spec/Commons/"):
        module = iri.rstrip("/").split("/")[-1]
        return f"{ROOT}/vendor/Commons/{module}.rdf"
    if iri.startswith("https://www.omg.org/spec/LCC/"):
        sub = iri.rstrip("/").split("/")[5:]
        return f"{ROOT}/vendor/LCC/{'/'.join(sub)}.rdf"
    return None


def label_of(g: Graph, uri: URIRef) -> str:
    for p in (RDFS.label, SKOS.prefLabel):
        for obj in g.objects(uri, p):
            if isinstance(obj, Literal):
                return str(obj)
    return uri.split("/")[-1]


def definition_of(g: Graph, uri: URIRef) -> str:
    for obj in g.objects(uri, SKOS.definition):
        if isinstance(obj, Literal):
            return str(obj)[:120]
    return ""


def subclasses_of(g: Graph, uri: URIRef) -> list[URIRef]:
    return [s for s in g.subjects(RDFS.subClassOf, uri)]


def main() -> None:
    t0 = time.time()
    internal = build_internal_map()
    print(f"[1/4] catalog 映射加载完成：{len(internal)} 个 FIBO 内部 IRI")

    g = Graph()
    queue = [ENTRY]
    loaded: set[str] = set()
    missing: list[str] = []
    while queue:
        iri = queue.pop(0)
        if iri in loaded:
            continue
        path = resolve(iri, internal)
        if path is None:
            missing.append(iri)
            continue
        try:
            g.parse(path, format="xml")
        except Exception as exc:  # noqa: BLE001 - 冒烟脚本需报告所有失败
            print(f"    解析失败 {iri}: {exc}")
            missing.append(iri)
            continue
        loaded.add(iri)
        # 扫描全图收集新 import（无论主语，稳妥兜底）
        for imp in g.objects(None, OWL.imports):
            if str(imp) not in loaded:
                queue.append(str(imp))
    elapsed_load = time.time() - t0

    print(
        f"[2/4] 闭包加载完成：{len(loaded)} 个本体文件，{len(g):,} 条三元组，"
        f"耗时 {elapsed_load:.1f}s"
    )
    if missing:
        print(f"    ⚠️ 未解析的 import（{len(missing)}）：")
        for m in missing[:15]:
            print(f"      - {m}")
    else:
        print("    ✅ import 闭包完整，无缺失")

    classes = list(g.subjects(RDF.type, OWL.Class))
    props = list(g.subjects(RDF.type, OWL.ObjectProperty)) + list(
        g.subjects(RDF.type, OWL.DatatypeProperty)
    )
    sub_of = list(g.subjects(RDFS.subClassOf, None))
    print(
        f"[3/4] TBox 统计：owl:Class={len(classes):,}，属性={len(props):,}，"
        f"subClassOf={len(sub_of):,}"
    )

    print("[4/4] 关键概念验证：")
    # 注意：FIBO 2.0 已将 Party 等基础概念下沉至 OMG Commons
    # （FIBO 的 Parties.rdf 中 Party 引用 cmns-pts:Party，而非自建 IRI）
    party = URIRef("https://www.omg.org/spec/Commons/PartiesAndSituations/Party")
    org = URIRef("https://www.omg.org/spec/Commons/Organizations/Organization")
    contract = URIRef("https://spec.edmcouncil.org/fibo/ontology/FND/Agreements/Contracts/Contract")

    class_set = set(classes)
    checks = {
        "Party (FND)": party,
        "Organization (Commons)": org,
        "Contract (FND)": contract,
    }
    for name, uri in checks.items():
        present = uri in class_set
        sub = subclasses_of(g, uri)
        print(
            f"    {'✅' if present else '❌'} {name}: label='{label_of(g, uri)}' "
            f"| 直接子类 {len(sub)} 个 | definition: {definition_of(g, uri)}"
        )
        for s in sub[:5]:
            print(f"        ↳ {label_of(g, s)}  ({s.split('/')[-1]})")

    # 派生类传递闭包示例：Organization → FormalOrganization → ...
    fo = URIRef(
        "https://spec.edmcouncil.org/fibo/ontology/FND/Organizations/FormalOrganizations/FormalOrganization"
    )
    if fo in class_set:
        chain = subclasses_of(g, fo)
        print(
            f"    ✅ FormalOrganization 直接子类 {len(chain)} 个："
            + ", ".join(label_of(g, s) for s in chain[:8])
        )

    print(f"\n总计耗时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
