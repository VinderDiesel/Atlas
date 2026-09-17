"""语义层 lint 整合入口（`make lint`）

依次执行：
1. ossie_validate     —— 语义模型结构/唯一性/血缘引用
2. governance_validate —— 治理扩展（schema / FIBO IRI 注册表 / 策略与黄金集引用 /
   域 ↔ 策略 ↔ 角色 ↔ claims 双向一致性，ADR-0021 决策 ⑥）
3. gold schema 校验   —— eval/gold/*.json 结构（FIBO 闭包深度校验见 eval/gold/validate_gold.py）
4. 权威源唯一性     —— semantic/ 下语义定义文件只允许住在权威目录（ADR-0002/0015）
5. values 快照锁定  —— semantic/values/*.json 可被运行时加载、且绑定当前锁定快照（ADR-0016）
6. analysis schema   —— eval/analysis/**/*.json 结构（ADR-0026 T01；schema.json 自身不当样本）

任一环节失败即返回非零退出码。

用法：.venv/bin/python -m semantic.lint --all
"""

from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from agent.value_domain import parse_profile
from data.identity import SNAPSHOT_DIR
from semantic import governance_validate, ossie_validate

REPO = Path(__file__).resolve().parent.parent
GOLD_SCHEMA = REPO / "eval" / "gold" / "schema.json"
ANALYSIS_SCHEMA = REPO / "eval" / "analysis" / "schema.json"
SEMANTIC_ROOT = REPO / "semantic"
VALUES_DIR = SEMANTIC_ROOT / "values"
# SNAPSHOT_DIR 从 data.identity 导入（ADR-0019 决策 ②：快照目录路径唯一权威）
# 语义定义文件（*.yaml / *.yml）允许存放的目录（ADR-0002：权威源唯一 = ossie/；
# ADR-0015：locale 同义词/形态词典在 synonyms/；行级策略声明在 policies/；
# ADR-0016：维度值域快照在 values/——机器生成，与词典/策略同一目录职责纪律）
_AUTHORITATIVE_DIRS = frozenset({"ossie", "synonyms", "policies", "values"})
# 不检查位置的非定义目录（`_*` 前缀归档区另走豁免分支）
_EXEMPT_DIRS = _AUTHORITATIVE_DIRS | {"__pycache__"}


def check_semantic_authority(root: Path | None = None) -> list[str]:
    """权威源唯一性：semantic/ 下不得存在权威目录之外的语义定义文件。

    背景（实测）：ADR-0002 之前的自研 DSL 残留（models/orders.yml、metrics/gmv.yml、
    dimensions/*.yml、synonyms/business_terms.yml）长期 `status: active` 且引用已不
    存在的表，而本 lint 不覆盖该目录——幽灵定义能一直存活。已集中到 _legacy/
    作设计演进对照（零代码引用）。

    规则：除 `_*` 前缀目录（归档区，如 `_legacy/`）外，*.yaml/*.yml 只允许出现在
    `ossie/`、`synonyms/`、`policies/`；JSON Schema 与 migrations 叙述性文档不受限。
    """
    base = root or SEMANTIC_ROOT
    errors: list[str] = []

    def rel(p: Path) -> str:
        return os.path.relpath(p, REPO)

    for p in sorted(list(base.glob("*.yml")) + list(base.glob("*.yaml"))):
        errors.append(
            f"语义定义文件不得直放于 semantic/ 根：{rel(p)}"
            f"（允许目录：{', '.join(sorted(_AUTHORITATIVE_DIRS))}）"
        )
    for sub in sorted(x for x in base.iterdir() if x.is_dir()):
        if sub.name.startswith("_") or sub.name in _EXEMPT_DIRS:
            continue
        for p in sorted(list(sub.glob("*.yml")) + list(sub.glob("*.yaml"))):
            errors.append(
                f"非权威目录下的语义定义文件：{rel(p)}"
                f"（权威源唯一 = semantic/ossie/；归档请迁至 semantic/_legacy/，"
                f"新增权威目录需先补 ADR）"
            )
    return errors


def check_gold_schema() -> list[str]:
    """黄金集样本结构校验（轻量，无 FIBO 依赖）。"""
    errors: list[str] = []
    schema = json.loads(GOLD_SCHEMA.read_text(encoding="utf-8"))
    # 目录化后跨 finance/ retail/ 域子目录（2026-09-05）
    for p in sorted(glob.glob(str(REPO / "eval" / "gold" / "*" / "gold-*.json"))):
        sample = json.loads(Path(p).read_text(encoding="utf-8"))
        try:
            jsonschema.validate(sample, schema)
        except jsonschema.ValidationError as exc:
            errors.append(f"{sample.get('id', p)}: 未通过 gold schema：{exc.message}")
    return errors


def check_analysis_schema(
    analysis_dir: Path | None = None,
    schema_path: Path | None = None,
) -> list[str]:
    """分析评测样本结构校验（ADR-0026 T01）：eval/analysis/**/*.json。

    规则：
    1. 除 schema.json 本身外的所有 *.json 都必须通过 eval/analysis/schema.json
       （schema.json 是校验器，不当样本扫描）；
    2. 样本 id 必须与文件名（去扩展名）一致；
    3. schema 文件缺失 → lint 错误条目（不抛 traceback，与其他检查的报告方式一致）。

    目录不存在 → 不报错（分析样本未启用）。analysis_dir/schema_path 参数化供测试
    指向 TemporaryDirectory（与 check_value_profiles 同一模式）。
    """
    base = analysis_dir or (REPO / "eval" / "analysis")
    schema_file = schema_path or ANALYSIS_SCHEMA
    if not base.exists():
        return []
    if not schema_file.exists():
        rel = (
            os.path.relpath(schema_file, REPO)
            if schema_file.is_relative_to(REPO)
            else str(schema_file)
        )
        return [f"analysis schema 缺失：{rel}（eval/analysis 样本结构校验无法执行）"]
    schema = json.loads(schema_file.read_text(encoding="utf-8"))
    errors: list[str] = []
    for p in sorted(base.rglob("*.json")):
        if p.name == "schema.json":
            continue
        rel = os.path.relpath(p, REPO) if p.is_relative_to(REPO) else str(p)
        try:
            sample = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{rel}: JSON 解析失败（{exc}）")
            continue
        sample_id = sample.get("id") if isinstance(sample, dict) else None
        if sample_id != p.stem:
            errors.append(f"{rel}: 样本 id {sample_id!r} 与文件名（{p.stem}）不一致")
        try:
            jsonschema.validate(sample, schema)
        except jsonschema.ValidationError as exc:
            errors.append(f"{rel}: 未通过 analysis schema：{exc.message}")
        except jsonschema.SchemaError as exc:
            errors.append(f"{rel}: analysis schema 本身非法：{exc.message}")
    return errors


def _ossie_dimension_fields() -> dict[str, set[str]]:
    """ossie 模型名 → dim_* 数据集的非时间字段名（值域文件允许的归属域）。

    只做**存在性**判定（字段是否真在该模型的维度表里），不复制 planner 的
    "必须有同义词" 规则——那条完整性检查在 tests/test_value_domain.py 用运行时
    加载器做（覆盖 = 注册维度全集有 profile 文件）。
    """
    index: dict[str, set[str]] = {}
    for path in sorted((REPO / "semantic" / "ossie").glob("*.ossie.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for model in doc.get("semantic_model", []):
            fields: set[str] = set()
            for ds in model.get("datasets", []):
                if not str(ds.get("name", "")).startswith("dim_"):
                    continue
                for f in ds.get("fields", []):
                    if f.get("dimension", {}).get("is_time"):
                        continue
                    fields.add(str(f["name"]))
            index[str(model["name"])] = fields
    return index


def _latest_snapshot_meta(snapshot_dir: Path = SNAPSHOT_DIR) -> dict[str, Any] | None:
    """最新锁定快照 meta（按 created_at）——值域 snapshot_sha 的比对基准。"""
    metas: list[dict[str, Any]] = [
        json.loads(p.read_text(encoding="utf-8")) for p in sorted(snapshot_dir.glob("*.meta.json"))
    ]
    if not metas:
        return None
    return max(metas, key=lambda m: str(m.get("created_at", "")))


def check_value_profiles(
    values_dir: Path | None = None,
    snapshot_dir: Path | None = None,
) -> list[str]:
    """值域快照绑定校验（ADR-0016 §③）。

    规则：
    1. 文件名必须为 `<model>.<field>.json`，且 model/field 与内容一致；
    2. 运行时加载器能解析（_parse_profile 的强校验契约）；
    3. model 必须是已知 ossie 模型，field 必须是该模型 dim_* 数据集的非时间字段；
    4. snapshot_sha 必须等于当前最新锁定 meta 的 sha（漂移即红，重跑
       `make profile-values` 同步）；
    5. registered 值域的 source_table 必须在最新锁定快照的表白名单内。

    目录不存在或为空 → 不报错（值域未启用，完整性检查在 tests/）。
    """
    base = values_dir or VALUES_DIR
    snap_dir = snapshot_dir or SNAPSHOT_DIR
    if not base.exists():
        return []
    files = sorted(base.glob("*.json"))
    if not files:
        return []
    errors: list[str] = []
    index = _ossie_dimension_fields()
    meta = _latest_snapshot_meta(snap_dir)
    if meta is None:
        return [f"值域文件存在但无锁定快照 meta 可比对：{snap_dir}"]
    allowed_tables = {
        f"atlas.{ns}.{table}"
        for ns, tables in meta.get("row_counts", {}).items()
        for table in tables
    }
    for path in files:
        stem = path.stem
        parts = stem.split(".")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            errors.append(f"{path.name}：文件名必须是 <model>.<field>.json")
            continue
        expected_model, expected_field = parts
        try:
            profile = parse_profile(path)
        except ValueError as exc:
            errors.append(f"{path.name}：运行时加载失败（{exc}）")
            continue
        if profile.model != expected_model or profile.field != expected_field:
            errors.append(
                f"{path.name}：内容 model/field ({profile.model}/{profile.field}) "
                f"与文件名 ({expected_model}/{expected_field}) 不一致"
            )
        if profile.model not in index:
            errors.append(f"{path.name}：模型 {profile.model!r} 不在已知 ossie 模型内")
            continue
        if profile.field not in index[profile.model]:
            errors.append(
                f"{path.name}：字段 {profile.field!r} 不在模型 {profile.model!r} 的 "
                "dim_* 数据集内（非时间字段）"
            )
        if profile.snapshot_sha != str(meta["sha"]):
            errors.append(
                f"{path.name}：snapshot_sha {profile.snapshot_sha!r} ≠ 最新锁定快照 "
                f"{meta['sha']!r}（请重跑 make profile-values 同步）"
            )
        if (
            profile.status == "registered"
            and profile.source_table is not None
            and profile.source_table not in allowed_tables
        ):
            errors.append(
                f"{path.name}：source_table {profile.source_table!r} 不在锁定快照 "
                "的表白名单内（快照可能已变，请重跑 make profile-values）"
            )
    return errors


def main() -> int:
    files = sorted(Path(REPO / "semantic" / "ossie").glob("*.ossie.yaml"))

    # 1) 结构校验
    errors: list[str] = []
    seen_names: dict[str, str] = {}
    for p in files:
        errors.extend(ossie_validate.validate_file(p, seen_names))
    for err in errors:
        print(f"  ❌ [ossie] {err}")
    if errors:
        print(f"\n[ossie] 校验失败：{len(errors)} 个问题")
        return 1
    print(f"✅ [ossie] 语义模型结构校验通过：{len(files)} 个文件")

    # 2) 治理扩展校验
    schema = json.loads(governance_validate.SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = []
    for p in files:
        governance_validate.validate_file(p, schema, errors)
    # 域 ↔ 策略 ↔ 角色 ↔ claims 契约双向一致性（ADR-0021 决策 ⑥，跨文件视图；
    # ROLE_DIRECTORY 延迟导入：semantic 不设 serving 顶层依赖）
    from serving.auth import ROLE_DIRECTORY

    governance_validate.check_policy_consistency(
        governance_validate.collect_referenced_policies(files),
        governance_validate.load_policies_by_name(),
        ROLE_DIRECTORY,
        errors,
    )
    for err in errors:
        print(f"  ❌ [governance] {err}")
    if errors:
        print(f"\n[governance] 校验失败：{len(errors)} 个问题")
        return 1
    print(f"✅ [governance] 治理扩展校验通过：{len(files)} 个文件")

    # 3) 黄金集结构校验
    errors = check_gold_schema()
    for err in errors:
        print(f"  ❌ [gold] {err}")
    if errors:
        print(f"\n[gold] 校验失败：{len(errors)} 个问题")
        return 1
    n_gold = len(glob.glob(str(REPO / "eval" / "gold" / "*" / "gold-*.json")))
    print(f"✅ [gold] 黄金集结构校验通过：{n_gold} 条样本")

    # 4) 权威源唯一性（语义定义文件位置）
    errors = check_semantic_authority()
    for err in errors:
        print(f"  ❌ [authority] {err}")
    if errors:
        print(f"\n[authority] 校验失败：{len(errors)} 个问题")
        return 1
    print("✅ [authority] 语义定义文件仅在权威目录（ossie/ synonyms/ policies/ values/）")

    # 5) 值域快照绑定
    errors = check_value_profiles()
    for err in errors:
        print(f"  ❌ [values] {err}")
    if errors:
        print(f"\n[values] 校验失败：{len(errors)} 个问题")
        return 1
    n_values = len(sorted(VALUES_DIR.glob("*.json")))
    if n_values:
        print(f"✅ [values] 值域快照绑定校验通过：{n_values} 个文件")
    else:
        print("✅ [values] 值域快照绑定校验通过（目录为空，完整性检查在 tests/）")

    # 6) 分析样本结构校验（ADR-0026 T01：eval/analysis/**/*.json）
    errors = check_analysis_schema()
    for err in errors:
        print(f"  ❌ [analysis] {err}")
    if errors:
        print(f"\n[analysis] 校验失败：{len(errors)} 个问题")
        return 1
    n_analysis = len(
        [p for p in sorted((REPO / "eval" / "analysis").rglob("*.json")) if p.name != "schema.json"]
    )
    print(f"✅ [analysis] 分析样本 schema 校验通过：{n_analysis} 条样本")

    print("\nlint 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
