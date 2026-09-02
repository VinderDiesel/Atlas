"""探查 FIBO 闭包中指定命名空间的真实类名（映射前的事实核查）

对失败候选的命名空间前缀，列出其中所有 owl:Class 的 label 与 IRI 末段，
用于确定 fibo_alignment 的正确映射目标（不猜测类名）。

用法：.venv/bin/python data/fibo/probe_namespaces.py
"""

from __future__ import annotations

# 复用 check_iris 的扩展种子与闭包加载（含 TransactionsExt / FBC 域）
from check_iris import load_graph  # type: ignore[import-not-found]
from rdflib import RDF, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDFS, SKOS

# 需要探查的命名空间前缀（失败候选的来源域）
PREFIXES = [
    "https://www.omg.org/spec/Commons/PartiesAndSituations/",
    "https://www.omg.org/spec/Commons/Organizations/",
    "https://www.omg.org/spec/Commons/DatesAndTimes/",
    "https://spec.edmcouncil.org/fibo/ontology/FND/AgentsAndPeople/People/",
    "https://spec.edmcouncil.org/fibo/ontology/FND/Organizations/FormalOrganizations/",
    "https://spec.edmcouncil.org/fibo/ontology/FND/TransactionsExt/MarketTransactions/",
    "https://spec.edmcouncil.org/fibo/ontology/FND/TransactionsExt/SecuritiesTransactions/",
    "https://spec.edmcouncil.org/fibo/ontology/FND/Accounting/ISO4217-CurrencyCodes/",
    "https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/",
    "https://spec.edmcouncil.org/fibo/ontology/FBC/FinancialInstruments/FinancialInstruments/",
    "https://spec.edmcouncil.org/fibo/ontology/FBC/FunctionalEntities/FinancialServicesEntities/",
    "https://spec.edmcouncil.org/fibo/ontology/FND/DatesAndTimes/BusinessDates/",
]


def label_of(g: Graph, uri: URIRef) -> str:
    for obj in g.objects(uri, RDFS.label):
        if isinstance(obj, Literal):
            return str(obj)
    for obj in g.objects(uri, SKOS.prefLabel):
        if isinstance(obj, Literal):
            return str(obj)
    return ""


def main() -> None:
    g = load_graph()
    classes: set[URIRef] = set(g.subjects(RDF.type, OWL.Class))
    for prefix in PREFIXES:
        hits = sorted(
            (c for c in classes if str(c).startswith(prefix)),
            key=lambda c: str(c),
        )
        print(f"\n=== {prefix}")
        if not hits:
            print("    （闭包中无此类）")
            continue
        for c in hits:
            print(f"    {label_of(g, c):28s} {str(c).removeprefix(prefix)}")


if __name__ == "__main__":
    main()
