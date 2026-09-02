"""治理扩展校验（Atlas 价值层）

校验 `semantic/ossie/*.ossie.yaml` 中 `custom_extensions`（vendor_name=ATLAS）：
1. data 是合法 JSON，且通过 `semantic/governance/atlas_governance.schema.json`
2. fibo_alignment：concept IRI 必须存在于权威注册表 `data/fibo/iri_registry.json`
   （轻量校验，无 rdflib 依赖，CI 可跑；闭包级深度校验见 data/fibo/validate_alignments.py）
3. policy.default_row_policy 引用的策略必须存在于 `semantic/policies/row_policy.yml`
4. quality.gold_test_cases 引用的黄金集用例必须存在于 `eval/gold/`

用法：.venv/bin/python -m semantic.governance_validate semantic/ossie/*.ossie.yaml
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import yaml

REPO = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO / "semantic" / "governance" / "atlas_governance.schema.json"
REGISTRY_PATH = REPO / "data" / "fibo" / "iri_registry.json"
POLICY_PATH = REPO / "semantic" / "policies" / "row_policy.yml"
GOLD_DIR = REPO / "eval" / "gold"


def load_governance_payloads(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """提取文件内所有 ATLAS 治理扩展 payload，返回 [(model_name, data)]。"""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    payloads: list[tuple[str, dict[str, Any]]] = []
    for model in doc.get("semantic_model", []):
        for ext in model.get("custom_extensions", []):
            if ext.get("vendor_name") != "ATLAS":
                continue
            try:
                data = json.loads(ext["data"])
            except (KeyError, json.JSONDecodeError) as exc:
                payloads.append((model.get("name", "<unnamed>"), {"_parse_error": str(exc)}))
                continue
            payloads.append((model.get("name", "<unnamed>"), data))
    return payloads


def validate_file(path: Path, schema: dict[str, Any], errors: list[str]) -> None:
    """校验单个文件的所有治理扩展。"""
    registry = _load_registry(errors)
    policy_names = _load_policy_names(errors)
    gold_ids = _load_gold_ids(errors)

    for model_name, data in load_governance_payloads(path):
        prefix = f"{path.name}.{model_name}"
        if "_parse_error" in data:
            errors.append(f"{prefix}: custom_extensions.data 不是合法 JSON：{data['_parse_error']}")
            continue
        try:
            jsonschema.validate(data, schema)
        except jsonschema.ValidationError as exc:
            errors.append(f"{prefix}: 未通过 atlas_governance.schema.json：{exc.message}")

        alignment = data.get("fibo_alignment")
        if alignment:
            if registry is None:
                errors.append(
                    f"{prefix}: 缺少权威注册表 {REGISTRY_PATH.name}"
                    "（运行 data/fibo 的注册表导出命令）"
                )
            else:
                for key, mapping in alignment.get("mappings", {}).items():
                    concept = mapping.get("concept")
                    if concept not in registry:
                        errors.append(
                            f"{prefix}.fibo_alignment.{key}: 概念 {concept}"
                            f" 不在权威注册表（{len(registry)} 条）"
                        )
        policy_ref = data.get("policy", {}).get("default_row_policy")
        if policy_ref and policy_names is not None and policy_ref not in policy_names:
            errors.append(
                f"{prefix}: default_row_policy 引用的策略 {policy_ref} 不存在于 {POLICY_PATH.name}"
            )
        for case_id in data.get("quality", {}).get("gold_test_cases", []):
            if gold_ids is not None and case_id not in gold_ids:
                errors.append(f"{prefix}: gold_test_cases 引用的用例 {case_id} 不存在于 eval/gold/")


def _load_registry(errors: list[str]) -> set[str] | None:
    if not REGISTRY_PATH.exists():
        errors.append(f"缺少权威注册表 {REGISTRY_PATH}，无法校验 FIBO IRI")
        return None
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return set(registry["concepts"].values())


def _load_policy_names(errors: list[str]) -> set[str] | None:
    if not POLICY_PATH.exists():
        errors.append(f"缺少策略文件 {POLICY_PATH}")
        return None
    doc = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    return {p["name"] for p in doc.get("policies", [])}


def _load_gold_ids(errors: list[str]) -> set[str] | None:
    if not GOLD_DIR.exists():
        errors.append(f"缺少黄金集目录 {GOLD_DIR}")
        return None
    return {p.stem for p in GOLD_DIR.glob("gold-*.json")}


def main(argv: list[str] | None = None) -> int:
    paths = [Path(p) for p in (argv or sys.argv[1:])]
    if not paths:
        paths = sorted(Path(REPO / "semantic" / "ossie").glob("*.ossie.yaml"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    errors: list[str] = []
    for p in paths:
        validate_file(p, schema, errors)

    for err in errors:
        print(f"  ❌ {err}")
    if errors:
        print(f"\n校验失败：{len(errors)} 个问题")
        return 1
    print(f"✅ 治理扩展校验通过：{len(paths)} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
