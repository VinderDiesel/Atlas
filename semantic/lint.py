"""语义层 lint 整合入口（`make lint`）

依次执行：
1. ossie_validate     —— 语义模型结构/唯一性/血缘引用
2. governance_validate —— 治理扩展（schema / FIBO IRI 注册表 / 策略与黄金集引用 /
   域 ↔ 策略 ↔ 角色 ↔ claims 双向一致性，ADR-0021 决策 ⑥）
3. gold schema 校验   —— eval/gold/*.json 结构（FIBO 闭包深度校验见 eval/gold/validate_gold.py）
4. 权威源唯一性     —— semantic/ 下语义定义文件只允许住在权威目录（ADR-0002/0015）
5. values 快照锁定  —— semantic/values/*.json 可被运行时加载、且绑定当前锁定快照（ADR-0016）
6. analysis schema   —— eval/analysis/**/*.json 结构（ADR-0026 T01；schema.json 自身不当样本）
7. 概念索引与别名     —— semantic/ontology/ 索引产物与 semantic/synonyms/ 别名文件的
   漂移防线（ADR-0032 ⑤：改源不重建 = 红）

任一环节失败即返回非零退出码。

用法：.venv/bin/python -m semantic.lint --all
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from agent.concept_index import parse_concept_index
from agent.value_domain import parse_profile

# 概念层校验与构建器同口径的单一事实源：路径常量与对象定位 / 映射提取逻辑直接
# 复用（私有名引用是有意的——双份实现 = 漂移风险；重组接口须先改构建器再改此处）
from data.fibo.build_concept_index import (
    OSSIE_PATH,
    REGISTRY_PATH,
    _extract_mapped,
    _object_synonyms,
)
from data.identity import SNAPSHOT_DIR
from semantic import governance_validate, ossie_validate

REPO = Path(__file__).resolve().parent.parent
GOLD_SCHEMA = REPO / "eval" / "gold" / "schema.json"
ANALYSIS_SCHEMA = REPO / "eval" / "analysis" / "schema.json"
SEMANTIC_ROOT = REPO / "semantic"
VALUES_DIR = SEMANTIC_ROOT / "values"
ONTOLOGY_DIR = SEMANTIC_ROOT / "ontology"
SYNONYMS_DIR = SEMANTIC_ROOT / "synonyms"
# 第 7 节目标文件名（构建器/别名文件的唯一产物名，见 data/fibo/build_concept_index.py）
_INDEX_FILENAME = "atlas_finance.concept_index.json"
_ALIAS_FILENAME = "concepts_zh_cn.yml"
# SNAPSHOT_DIR 从 data.identity 导入（ADR-0019 决策 ②：快照目录路径唯一权威）
# 语义定义文件（*.yaml / *.yml）允许存放的目录（ADR-0002：权威源唯一 = ossie/；
# ADR-0015：locale 同义词/形态词典在 synonyms/；行级策略声明在 policies/；
# ADR-0016：维度值域快照在 values/——机器生成，与词典/策略同一目录职责纪律；
# ADR-0032：概念索引 AOT 产物在 ontology/——清单数据与 AGENTS §7.3 同源维护）
_AUTHORITATIVE_DIRS = frozenset({"ossie", "synonyms", "policies", "values", "ontology"})
# 不检查位置的非定义目录（`_*` 前缀归档区另走豁免分支）
_EXEMPT_DIRS = _AUTHORITATIVE_DIRS | {"__pycache__"}


def check_semantic_authority(root: Path | None = None) -> list[str]:
    """权威源唯一性：semantic/ 下不得存在权威目录之外的语义定义文件。

    背景（实测）：ADR-0002 之前的自研 DSL 残留（models/orders.yml、metrics/gmv.yml、
    dimensions/*.yml、synonyms/business_terms.yml）长期 `status: active` 且引用已不
    存在的表，而本 lint 不覆盖该目录——幽灵定义能一直存活。已集中到 _legacy/
    作设计演进对照（零代码引用）。

    规则：除 `_*` 前缀目录（归档区，如 `_legacy/`）外，*.yaml/*.yml 只允许出现在
    `ossie/`、`synonyms/`、`policies/`、`values/`、`ontology/`；JSON Schema 与
    migrations 叙述性文档不受限。
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


def _registry() -> dict[str, str]:
    """iri_registry.json 的概念集合（短名 → IRI；data/fibo 为唯一权威）。"""
    doc = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return dict(doc["concepts"])


def _load_alias_terms(synonyms_dir: Path) -> dict[str, list[str]]:
    """别名文件 → 概念短名 → 术语列表（第 7 节 S3 比对的合并视图）。

    与 `data/fibo/build_concept_index.py::_load_aliases` 同口径（单文件
    concepts_zh_cn.yml；扩多 locale 时两处同步演进）。文件缺失 / 畸形 → {}；
    畸形由 _check_alias_file 另行响亮上报，本函数只供比对。
    """
    alias_path = synonyms_dir / _ALIAS_FILENAME
    if not alias_path.exists():
        return {}
    try:
        doc = yaml.safe_load(alias_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    if not isinstance(doc, dict):
        return {}
    concepts = doc.get("concepts") or {}
    if not isinstance(concepts, dict):
        return {}
    return {str(name): [str(t) for t in (terms or [])] for name, terms in concepts.items()}


def _check_alias_file(synonyms_dir: Path) -> list[str]:
    """别名文件校验（ADR-0032 ⑤）：键 ∈ registry；术语非空、同概念去重；
    同一术语不得映射多个概念（同 locale 歧义 = curation bug）。

    文件不存在 → 跳过（别名未启用是合法形态）。
    """
    alias_path = synonyms_dir / _ALIAS_FILENAME
    if not alias_path.exists():
        return []
    try:
        doc = yaml.safe_load(alias_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"{_ALIAS_FILENAME}：YAML 解析失败（{exc}）"]
    if doc is not None and not isinstance(doc, dict):
        return [f"{_ALIAS_FILENAME}：顶层必须是映射（concepts: {{概念短名: [术语, ...]}}）"]
    concepts = (doc or {}).get("concepts") or {}
    if not isinstance(concepts, dict):
        return [f"{_ALIAS_FILENAME}：concepts 必须是映射（概念短名 → 术语列表）"]
    known = set(_registry())
    errors: list[str] = []
    owners: dict[str, set[str]] = {}
    for raw_name, terms in concepts.items():
        name = str(raw_name)
        if name not in known:
            errors.append(
                f"{_ALIAS_FILENAME}：概念 {name!r} 不在 iri_registry.json"
                "（先跑 check_iris.py 入 registry 再改名文件）"
            )
        if not isinstance(terms, list):
            errors.append(f"{_ALIAS_FILENAME}：概念 {name!r} 的术语必须是列表")
            continue
        seen: set[str] = set()
        for i, raw_term in enumerate(terms):
            term = str(raw_term).strip()
            if not term:
                errors.append(f"{_ALIAS_FILENAME}：概念 {name!r} 含空术语（第 {i + 1} 条）")
                continue
            if term in seen:
                errors.append(f"{_ALIAS_FILENAME}：概念 {name!r} 术语重复：{term!r}")
            seen.add(term)
            owners.setdefault(term, set()).add(name)
    for term, names in sorted(owners.items()):
        if len(names) > 1:
            errors.append(
                f"{_ALIAS_FILENAME}：术语 {term!r} 同时映射到多个概念：{sorted(names)}"
                "（同 locale 歧义 = curation bug）"
            )
    return errors


def _ossie_object_space() -> tuple[dict[str, Any], dict[str, Any], list[Any]]:
    """finance 语义模型的（数据集 / 指标 / 关系）——_object_synonyms 的定位空间。"""
    doc = yaml.safe_load(OSSIE_PATH.read_text(encoding="utf-8"))
    model = doc["semantic_model"][0]
    return (
        {d["name"]: d for d in model["datasets"]},
        {m["name"]: m for m in model["metrics"]},
        list(model["relationships"]),
    )


def _check_concept_index(ontology_dir: Path, synonyms_dir: Path) -> list[str]:
    """概念索引漂移校验（ADR-0032 ⑤：两级漂移防线的 CI 面）。

    规则：结构合法（复用运行时 parse_concept_index 强校验）；概念 IRI ∈
    iri_registry；objects 可在语义模型定位（复用构建器定位逻辑，口径同源）；
    与 ossie fibo_alignment 双向一致（孤儿 / 漏配）；source_semantic_sha =
    当前 ossie 文件 sha256；索引内 S3 术语 = 当前别名文件合并结果——
    「改源不重建 = 红」。

    索引文件不存在 → 跳过（atlas_retail 无映射不生成产物，是合法形态）。
    """
    index_path = ontology_dir / _INDEX_FILENAME
    if not index_path.exists():
        return []
    try:
        index = parse_concept_index(index_path)
    except ValueError as exc:
        return [str(exc)]
    errors: list[str] = []
    known_iris = set(_registry().values())
    datasets, metrics, relationships = _ossie_object_space()

    # a) 概念 IRI ∈ registry（ADR-0032 ⑤）
    for name, entry in index.concepts.items():
        if entry.iri not in known_iris:
            errors.append(f"概念 {name} 的 iri 不在 iri_registry.json：{entry.iri}")

    # b) objects 可在语义模型定位 + 收集索引侧映射集
    index_mapped: set[tuple[str, str]] = set()
    for name, entry in index.concepts.items():
        for obj_map in entry.objects:
            index_mapped.add((obj_map.object, entry.iri))
            try:
                _object_synonyms(obj_map.object, datasets, metrics, relationships)
            except ValueError as exc:
                errors.append(f"概念 {name} 的对象不在语义模型：{obj_map.object!r}（{exc}）")

    # c) 与 ossie fibo_alignment 双向一致（孤儿 = 索引有 ossie 无；漏配 = 反之）
    ossie_mapped = {(m["object"], m["concept_iri"]) for m in _extract_mapped()}
    for obj, iri in sorted(index_mapped - ossie_mapped):
        errors.append(f"索引对象未对齐 fibo_alignment（孤儿）：{obj!r} → {iri}")
    for obj, iri in sorted(ossie_mapped - index_mapped):
        errors.append(f"fibo_alignment 映射未落位索引（漏配）：{obj!r} → {iri}")

    # d) source_semantic_sha = 当前 ossie 文件 sha256（改源不重建 = 红）
    current_sha = hashlib.sha256(OSSIE_PATH.read_bytes()).hexdigest()
    if index.source_semantic_sha != current_sha:
        errors.append(
            f"索引 source_semantic_sha {index.source_semantic_sha} ≠ 当前 ossie 文件 "
            "sha256（改源不重建，请重跑 make ontology-build）"
        )

    # e) 索引内 S3 术语 = 当前别名文件合并结果（改源不重建 = 红）
    aliases = _load_alias_terms(synonyms_dir)
    for name, entry in index.concepts.items():
        indexed = {t.text for t in entry.terms if t.source == "S3"}
        declared = set(aliases.get(name, []))
        if indexed != declared:
            errors.append(
                f"概念 {name} 的 S3 术语与 {_ALIAS_FILENAME} 合并结果不一致"
                f"（改源不重建，请重跑 make ontology-build）："
                f"索引多 {sorted(indexed - declared)}，别名多 {sorted(declared - indexed)}"
            )
    return errors


def check_concept_layer(
    ontology_dir: Path | None = None,
    synonyms_dir: Path | None = None,
) -> list[str]:
    """概念索引与别名校验（`make lint` 第 7 节；ADR-0032 ⑤）。

    组合两个子检查——各自在目标文件缺失时跳过不报错（概念层未启用的合法形态）：
    1. 别名文件：键 ∈ registry；术语非空 / 去重；同术语不得跨概念歧义；
    2. 索引：结构 / IRI / objects 定位 / 双向一致 / sha / S3 合并。

    ontology_dir / synonyms_dir 参数化供测试指向 TemporaryDirectory（与
    check_value_profiles 同一模式）。
    """
    synonyms = synonyms_dir or SYNONYMS_DIR
    errors = _check_alias_file(synonyms)
    errors.extend(_check_concept_index(ontology_dir or ONTOLOGY_DIR, synonyms))
    return errors


def _concept_layer_counts() -> tuple[int, int]:
    """第 7 节成功行的计数（概念数 / 别名术语条数）；文件缺失按 0。"""
    index_path = ONTOLOGY_DIR / _INDEX_FILENAME
    n_concepts = 0
    if index_path.exists():
        doc = json.loads(index_path.read_text(encoding="utf-8"))
        n_concepts = len(doc.get("concepts") or {})
    aliases = _load_alias_terms(SYNONYMS_DIR)
    return n_concepts, sum(len(terms) for terms in aliases.values())


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
    # 成功消息动态引用清单（原先写死四目录，ADR-0032 加 ontology/ 时暴露漂移风险）
    print(f"✅ [authority] 语义定义文件仅在权威目录（{'/ '.join(sorted(_AUTHORITATIVE_DIRS))}/）")

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

    # 7) 概念索引与别名校验（ADR-0032 ⑤：改源不重建 = 红；两级漂移防线的 CI 面）
    errors = check_concept_layer()
    for err in errors:
        print(f"  ❌ [concept] {err}")
    if errors:
        print(f"\n[concept] 校验失败：{len(errors)} 个问题")
        return 1
    n_concepts, n_aliases = _concept_layer_counts()
    if n_concepts:
        print(f"✅ [concept] 概念索引与别名校验通过（{n_concepts} 概念 / {n_aliases} 条别名）")
    else:
        print("✅ [concept] 概念层未启用（无索引产物，跳过校验）")

    print("\nlint 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
