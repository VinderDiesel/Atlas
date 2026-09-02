"""候选 FIBO IRI 存在性验证（fibo_alignment 映射的前置检查）

在已加载的 FIBO 闭包图中验证候选类 IRI 是否存在，输出 label 与 definition，
防止在语义模型中引用不存在的概念（诚实红线：映射必须可审计）。

扩展种子：AllFND 闭包不含 TransactionsExt 与 FBC 域（它们只作为被依赖方被 import），
交易/账户/证券映射需显式把对应模块加入种子队列。

用法：.venv/bin/python data/fibo/check_iris.py
"""

from __future__ import annotations

from pathlib import Path

from rdflib import RDF, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDFS, SKOS

# 复用冒烟脚本的加载逻辑
from smoke_fibo import build_internal_map, resolve  # type: ignore[import-not-found]

ROOT = Path(__file__).resolve().parent

# 种子模块：AllFND 基线 + 交易/账户/证券域（fibo_alignment 所需概念来源）
SEEDS: list[str] = [
    "fibo-src/FND/AllFND.rdf",
    "fibo-src/FND/TransactionsExt/MetadataFNDTransactionsExt.rdf",
    "fibo-src/FND/TransactionsExt/SecuritiesTransactions.rdf",
    "fibo-src/FND/TransactionsExt/REATransactions.rdf",
    "fibo-src/FBC/ProductsAndServices/ClientsAndAccounts.rdf",
    "fibo-src/FBC/FinancialInstruments/FinancialInstruments.rdf",
    "fibo-src/FBC/FunctionalEntities/FinancialServicesEntities.rdf",
]


def load_graph() -> Graph:
    """加载种子模块 + owl:imports 闭包，返回合并图。"""
    g = Graph()
    internal = build_internal_map()
    loaded: set[str] = set()
    queue = [str(ROOT / s) for s in SEEDS]
    while queue:
        path = queue.pop(0)
        if path in loaded:
            continue
        g.parse(path, format="xml")
        loaded.add(path)
        for imp in g.objects(None, OWL.imports):
            iri = str(imp)
            if iri in loaded:
                continue
            resolved = resolve(iri, internal)
            if resolved is None:
                continue
            if resolved not in loaded:
                queue.append(resolved)
    return g

CANDIDATES = {
    # 基础概念（Commons / FND，FIBO 2.0 已下沉至 OMG Commons）
    "cmns-pts:Party": "https://www.omg.org/spec/Commons/PartiesAndSituations/Party",
    "cmns-pts:PartyRole": "https://www.omg.org/spec/Commons/PartiesAndSituations/PartyRole",
    "cmns-org:Organization": "https://www.omg.org/spec/Commons/Organizations/Organization",
    "cmns-org:FormalOrganization": "https://www.omg.org/spec/Commons/Organizations/FormalOrganization",
    "cmns-org:LegalEntity": "https://www.omg.org/spec/Commons/Organizations/LegalEntity",
    "cmns-org:LegalPerson": "https://www.omg.org/spec/Commons/Organizations/LegalPerson",
    "cmns-org:OrganizationalSubUnit": "https://www.omg.org/spec/Commons/Organizations/OrganizationalSubUnit",
    "cmns-dt:Date": "https://www.omg.org/spec/Commons/DatesAndTimes/Date",
    "fibo-fnd-pty-pty:Person": "https://spec.edmcouncil.org/fibo/ontology/FND/AgentsAndPeople/People/Person",
    "fibo-fnd-agr-ctr:Contract": "https://spec.edmcouncil.org/fibo/ontology/FND/Agreements/Contracts/Contract",
    "fibo-fnd-acc-cur:MonetaryAmount": "https://spec.edmcouncil.org/fibo/ontology/FND/Accounting/CurrencyAmount/MonetaryAmount",
    # 交易 / 账户 / 证券 / 机构（FBC / FND 域，需扩展种子引入）
    "fibo-fnd-txn-mkt:MarketTransaction": "https://spec.edmcouncil.org/fibo/ontology/FND/TransactionsExt/MarketTransactions/MarketTransaction",
    "fibo-fnd-txn-mkt:TransactionCounterparty": "https://spec.edmcouncil.org/fibo/ontology/FND/TransactionsExt/MarketTransactions/TransactionCounterparty",
    "fibo-fbc-pas-ca:Account": "https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/Account",
    "fibo-fbc-pas-ca:CustomerAccount": "https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/CustomerAccount",
    "fibo-fbc-pas-ca:BrokerageAccount": "https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/BrokerageAccount",
    "fibo-fbc-pas-ca:AccountHolder": "https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/AccountHolder",
    "fibo-fbc-pas-ca:Fee": "https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/Fee",
    "fibo-fbc-pas-ca:Balance": "https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/Balance",
    "fibo-fbc-fi-fi:FinancialInstrument": "https://spec.edmcouncil.org/fibo/ontology/FBC/FinancialInstruments/FinancialInstruments/FinancialInstrument",
    "fibo-fbc-fi-fi:Security": "https://spec.edmcouncil.org/fibo/ontology/FBC/FinancialInstruments/FinancialInstruments/Security",
    "fibo-fbc-fi-fi:EquityInstrument": "https://spec.edmcouncil.org/fibo/ontology/FBC/FinancialInstruments/FinancialInstruments/EquityInstrument",
    "fibo-fbc-fi-fi:SecuritiesTransaction": "https://spec.edmcouncil.org/fibo/ontology/FBC/FinancialInstruments/FinancialInstruments/SecuritiesTransaction",
    "fibo-fbc-fse:BrokerageFirm": "https://spec.edmcouncil.org/fibo/ontology/FBC/FunctionalEntities/FinancialServicesEntities/BrokerageFirm",
    "fibo-be-fe:FunctionalEntity": "https://spec.edmcouncil.org/fibo/ontology/BE/FunctionalEntities/FunctionalEntities/FunctionalEntity",
    "fibo-fnd-plc-adr:Address": "https://spec.edmcouncil.org/fibo/ontology/FND/Places/Addresses/Address",
}


def main() -> None:
    g = load_graph()
    classes = {c for c in g.subjects(RDF.type, OWL.Class)}
    print(f"已加载 {len(classes)} 个 owl:Class，开始校验候选 IRI：\n")
    ok = 0
    for name, iri in CANDIDATES.items():
        uri = URIRef(iri)
        if uri in classes:
            label = ""
            for p in (RDFS.label, SKOS.prefLabel):
                for obj in g.objects(uri, p):
                    if isinstance(obj, Literal):
                        label = str(obj)
                        break
                if label:
                    break
            definition = ""
            for obj in g.objects(uri, SKOS.definition):
                if isinstance(obj, Literal):
                    definition = str(obj)[:90]
                    break
            ok += 1
            print(f"  ✅ {name:36s} label='{label}' | {definition}")
        else:
            print(f"  ❌ {name:36s} 不存在于当前闭包")
    print(f"\n结果：{ok}/{len(CANDIDATES)} 存在")


if __name__ == "__main__":
    main()
