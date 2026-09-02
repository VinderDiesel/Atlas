"""语义模型结构校验（Ossie 0.2.0.dev0 核心结构）

校验 `semantic/ossie/*.ossie.yaml`：
1. 文档结构：version / semantic_model / datasets / relationships / metrics 必备字段
2. 唯一性：dataset 名、metric 名全局唯一（禁止同名 active 指标并存，AGENTS.md N8）
3. 血缘引用：relationship 引用的 dataset 与字段必须存在；
   metric expression 中 `dataset.field` 形式的引用必须存在（正则提取，AST 解析见 compiler）

不校验治理扩展（owner/lineage/fibo_alignment 等）——那是 governance_validate 的职责。

用法：.venv/bin/python -m semantic.ossie_validate semantic/ossie/*.ossie.yaml
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent

# 提取 expression 中 `dataset.field` 形式的引用（dataset 名限定为小写 snake_case）
FIELD_REF = re.compile(r"(?<![\w.])([a-z][a-z0-9_]*\.[a-zA-Z][a-zA-Z0-9_]*)")

REQUIRED_MODEL_KEYS = ("name", "description", "ai_context", "custom_extensions", "datasets", "relationships", "metrics")
REQUIRED_DATASET_KEYS = ("name", "source", "primary_key", "fields")
REQUIRED_FIELD_KEYS = ("name", "expression", "datatype")
REQUIRED_METRIC_KEYS = ("name", "description", "expression", "datatype", "ai_context")


def validate_model(model: dict, errors: list[str], seen_names: dict[str, str]) -> None:
    """校验单个 semantic_model 的结构与内部引用。"""
    name = model.get("name")
    if not name:
        errors.append("semantic_model 缺少 name")
        return
    if name in seen_names:
        errors.append(f"语义模型重名：{name}（首次出现在 {seen_names[name]}）")
    seen_names[name] = "<当前文件>"

    for key in REQUIRED_MODEL_KEYS:
        if key not in model:
            errors.append(f"{name}: 缺少必需字段 {key}")

    datasets = model.get("datasets", [])
    ds_names: set[str] = set()
    fields_by_ds: dict[str, set[str]] = {}
    for ds in datasets:
        ds_name = ds.get("name")
        if not ds_name:
            errors.append(f"{name}: dataset 缺少 name")
            continue
        if ds_name in ds_names:
            errors.append(f"{name}: dataset 重名 {ds_name}")
        ds_names.add(ds_name)
        for key in REQUIRED_DATASET_KEYS:
            if key not in ds:
                errors.append(f"{name}.{ds_name}: 缺少必需字段 {key}")
        if not isinstance(ds.get("primary_key"), list) or not ds["primary_key"]:
            errors.append(f"{name}.{ds_name}: primary_key 必须是非空列表")
        field_names: set[str] = set()
        for field in ds.get("fields", []):
            fname = field.get("name")
            if not fname:
                errors.append(f"{name}.{ds_name}: field 缺少 name")
                continue
            if fname in field_names:
                errors.append(f"{name}.{ds_name}: 字段重名 {fname}")
            field_names.add(fname)
            for key in REQUIRED_FIELD_KEYS:
                if key not in field:
                    errors.append(f"{name}.{ds_name}.{fname}: 缺少必需字段 {key}")
            dialects = field.get("expression", {}).get("dialects", [])
            if not any(d.get("dialect") == "ANSI_SQL" for d in dialects):
                errors.append(f"{name}.{ds_name}.{fname}: expression 缺少 ANSI_SQL 方言")
        # primary_key / unique_keys 引用的字段必须已声明（键与字段定义一致性）
        for key_field in ds.get("primary_key", []):
            if key_field not in field_names:
                errors.append(f"{name}.{ds_name}: primary_key 引用未声明的字段 {key_field}")
        for key_group in ds.get("unique_keys", []):
            for key_field in key_group:
                if key_field not in field_names:
                    errors.append(f"{name}.{ds_name}: unique_keys 引用未声明的字段 {key_field}")
        fields_by_ds[ds_name] = field_names

    rel_names: set[str] = set()
    for rel in model.get("relationships", []):
        rel_name = rel.get("name")
        if rel_name in rel_names:
            errors.append(f"{name}: relationship 重名 {rel_name}")
        rel_names.add(rel_name)
        for side in ("from", "to"):
            ds_ref = rel.get(side)
            if ds_ref not in fields_by_ds:
                errors.append(f"{name}.{rel_name}: {side} 引用的 dataset {ds_ref} 不存在")
        for col in rel.get("from_columns", []):
            if col not in fields_by_ds.get(rel.get("from"), set()):
                errors.append(f"{name}.{rel_name}: from_columns 引用字段 {col} 不存在于 {rel.get('from')}")
        for col in rel.get("to_columns", []):
            if col not in fields_by_ds.get(rel.get("to"), set()):
                errors.append(f"{name}.{rel_name}: to_columns 引用字段 {col} 不存在于 {rel.get('to')}")

    for metric in model.get("metrics", []):
        mname = metric.get("name")
        if not mname:
            errors.append(f"{name}: metric 缺少 name")
            continue
        if mname in seen_names:
            errors.append(f"指标重名：{mname}（首次出现在 {seen_names[mname]}）")
        seen_names[mname] = name
        for key in REQUIRED_METRIC_KEYS:
            if key not in metric:
                errors.append(f"{name}.{mname}: 缺少必需字段 {key}")
        # expression 中 dataset.field 引用的存在性（血缘完整性）
        for ref in FIELD_REF.findall(str(metric.get("expression", ""))):
            ds_part, field_part = ref.split(".", 1)
            if ds_part not in fields_by_ds:
                errors.append(f"{name}.{mname}: expression 引用不存在的 dataset {ds_part}")
            elif field_part not in fields_by_ds[ds_part]:
                errors.append(f"{name}.{mname}: expression 引用不存在的字段 {ref}")


def validate_file(path: Path, seen_names: dict[str, str]) -> list[str]:
    """校验单个 ossie.yaml 文件，返回错误列表。"""
    errors: list[str] = []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"{path}: YAML 解析失败：{exc}"]

    if not isinstance(doc, dict) or "version" not in doc:
        return [f"{path}: 缺少顶层 version 字段"]
    if not isinstance(doc.get("semantic_model"), list) or not doc["semantic_model"]:
        return [f"{path}: semantic_model 必须是非空列表"]

    for model in doc["semantic_model"]:
        validate_model(model, errors, seen_names)
    return errors


def main(argv: list[str] | None = None) -> int:
    paths = [Path(p) for p in (argv or sys.argv[1:])] or []
    if not paths:
        paths = sorted(Path(REPO / "semantic" / "ossie").glob("*.ossie.yaml"))
    if not paths:
        print("未找到语义模型文件")
        return 1

    all_errors: list[str] = []
    seen_names: dict[str, str] = {}
    for p in paths:
        all_errors.extend(validate_file(p, seen_names))

    for err in all_errors:
        print(f"  ❌ {err}")
    if all_errors:
        print(f"\n校验失败：{len(all_errors)} 个问题")
        return 1
    print(f"✅ 语义模型结构校验通过：{len(paths)} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
