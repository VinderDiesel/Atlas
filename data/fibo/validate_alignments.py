"""FIBO 概念对齐校验（ADR-0007 CI 校验点）

读取语义模型的 custom_extensions.fibo_alignment.mappings，
验证：
1. 内嵌 JSON 通过 atlas_governance.schema.json（含 fibo_alignment 结构约束）
2. 每个 concept IRI 真实存在于锁定版本的 FIBO/Commons 闭包（防引用失效概念）

用法：.venv/bin/python data/fibo/validate_alignments.py
     [semantic/ossie/atlas_finance.ossie.yaml ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import yaml
from check_iris import ROOT, load_graph  # type: ignore[import-not-found]
from rdflib import RDF, URIRef
from rdflib.namespace import OWL

REPO = ROOT.parent.parent
SCHEMA = REPO / "semantic" / "governance" / "atlas_governance.schema.json"


def extract_alignment(yaml_path: Path) -> list[dict]:
    """解析 ossie.yaml，返回 {file: {data_json, fibo_alignment}} 列表。"""
    with yaml_path.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    results = []
    for model in doc.get("semantic_model", []):
        for ext in model.get("custom_extensions", []):
            if ext.get("vendor_name") != "ATLAS":
                continue
            data = json.loads(ext["data"])
            if "fibo_alignment" in data:
                results.append({"file": yaml_path, "data": data, "model": model.get("name")})
    return results


def main() -> None:
    default_model = REPO / "semantic" / "ossie" / "atlas_finance.ossie.yaml"
    paths = [Path(p) for p in sys.argv[1:]] or [default_model]
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    g = load_graph()
    classes = {c for c in g.subjects(RDF.type, OWL.Class)}
    ok = True

    for p in paths:
        for item in extract_alignment(p):
            name = item["model"]
            # 1) governance schema 校验
            jsonschema.validate(item["data"], schema)
            print(f"✅ {name}: custom_extensions 通过 atlas_governance.schema.json")

            # 2) IRI 存在性校验
            mappings = item["data"]["fibo_alignment"]["mappings"]
            missing = [
                (key, m["concept"])
                for key, m in mappings.items()
                if URIRef(m["concept"]) not in classes
            ]
            if missing:
                ok = False
                for key, iri in missing:
                    print(f"  ❌ {name}.{key}: {iri} 不存在于 FIBO 闭包")
            else:
                commit = item["data"]["fibo_alignment"]["fibo_commit"]
                print(
                    f"  ✅ {name}: {len(mappings)} 条映射 IRI 全部存在于 FIBO 闭包"
                    f"（锁定 commit {commit}）"
                )

    if not ok:
        sys.exit(1)
    print("\n校验通过：所有 fibo_alignment 映射可审计。")


if __name__ == "__main__":
    main()
