"""黄金集校验：schema 结构 + FIBO 概念标注 IRI 存在性

验证 eval/gold/*.json 全部符合 schema.json，且 fibo_concepts 中
每个 concept IRI 都真实存在于锁定版本的 FIBO/Commons 闭包
（ADR-0007：黄金集问句人工标注 FIBO 概念，标注不得引用失效概念）。

注意：本脚本只校验"标注可审计"，概念映射准确率（模型输出 vs 标注的一致率）
由评测运行产生，与 EX / Plan Acc 分开报告。

用法：.venv/bin/python eval/gold/validate_gold.py
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import jsonschema
from rdflib import RDF, URIRef
from rdflib.namespace import OWL

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "data" / "fibo"))
from check_iris import load_graph  # type: ignore[import-not-found]

GOLD_DIR = Path(__file__).resolve().parent
SCHEMA = GOLD_DIR / "schema.json"


def main() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    g = load_graph()
    classes = {c for c in g.subjects(RDF.type, OWL.Class)}
    ok = True
    files = sorted(glob.glob(str(GOLD_DIR / "gold-*.json")))

    for p in files:
        sample = json.loads(Path(p).read_text(encoding="utf-8"))
        jsonschema.validate(sample, schema)
        concepts = sample.get("fibo_concepts", [])
        missing = [c["concept"] for c in concepts if URIRef(c["concept"]) not in classes]
        if missing:
            ok = False
            for iri in missing:
                print(f"  ❌ {sample['id']}: {iri} 不存在于 FIBO 闭包")
        else:
            print(f"✅ {sample['id']}: schema 通过，FIBO 概念标注 {len(concepts)} 条全部有效")

    if not ok:
        sys.exit(1)
    print(f"\n校验通过：{len(files)} 条黄金集样本可审计。")


if __name__ == "__main__":
    main()
