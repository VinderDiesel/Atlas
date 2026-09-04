"""治理扩展校验（Atlas 价值层）

校验 `semantic/ossie/*.ossie.yaml` 中 `custom_extensions`（vendor_name=ATLAS）：
1. data 是合法 JSON，且通过 `semantic/governance/atlas_governance.schema.json`
2. fibo_alignment：concept IRI 必须存在于权威注册表 `data/fibo/iri_registry.json`
   （轻量校验，无 rdflib 依赖，CI 可跑；闭包级深度校验见 data/fibo/validate_alignments.py）
3. policy.default_row_policy 引用的策略必须存在于 `semantic/policies/row_policy.yml`
4. quality.gold_test_cases 引用的黄金集用例必须存在于 `eval/gold/`
5. **指标级** payload 同样校验（老版只查模型级，指标级错位引用漏网）；
   且指标声明的 gold_test_cases 必须与 gold 集中 expected_metric 指向它的
   全部样本**双向一致**（防引用错位/声明过期，如 cash_balance 曾错引 gold-102，
   而 gold-102 实为 commission_revenue 用例）。
6. **supersedes 链语义校验**（跨文件）：取代目标必须存在且非自身；
   新版本号必须严格大于被取代版本（版本号递增天然防环）；
   被取代指标不得仍为 active（发布新版本前须先把旧指标置为 deprecated，
   呼应 AGENTS.md N8 的禁止同名 active 口径）。
7. **expected_value_snapshot_sha 锚定校验**（KL #16 收口，2026-09-04）：
   有黄金用例（gold_test_cases 非空）的指标，若已回填数值背书快照则必须
   指向 `data/snapshots/` 已锁定的快照 sha（防占位漂移/指向未锁快照）；
   占位 `<待填写>` 是机制未启用状态（如零售模型数据未装载）的如实声明，
   允许保留。

用法：.venv/bin/python -m semantic.governance_validate semantic/ossie/*.ossie.yaml
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml

REPO = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO / "semantic" / "governance" / "atlas_governance.schema.json"
REGISTRY_PATH = REPO / "data" / "fibo" / "iri_registry.json"
POLICY_PATH = REPO / "semantic" / "policies" / "row_policy.yml"
GOLD_DIR = REPO / "eval" / "gold"
SNAPSHOT_DIR = REPO / "data" / "snapshots"
PLACEHOLDER_SNAPSHOT_SHA = "<待填写>"


@dataclass(frozen=True)
class MetricGovernance:
    """指标级 governance 记录（supersedes 交叉校验的输入行）。"""

    metric_name: str
    version: int
    status: str
    supersedes: str | None
    prefix: str


def _json_payload(ext: dict[str, Any]) -> dict[str, Any] | None:
    """解析单个 ATLAS 扩展的 data；非 ATLAS 返回 None，非法 JSON 返回占位 dict。"""
    if ext.get("vendor_name") != "ATLAS":
        return None
    try:
        data = json.loads(ext["data"])
    except (KeyError, json.JSONDecodeError) as exc:
        return {"_parse_error": str(exc)}
    if not isinstance(data, dict):  # 合法 JSON 但非对象（如数组/标量）也属解析错误
        return {"_parse_error": "data 不是 JSON 对象"}
    return data


def iter_payloads(path: Path) -> Iterator[tuple[str, dict[str, Any], str | None]]:
    """遍历文件内所有 ATLAS payload，产出 (prefix, data, metric_name|None)。

    prefix 形如 `file.model`（模型级）或 `file.model.metric.<指标名>`（指标级）；
    metric_name 仅在指标级非 None，供 gold_test_cases 双向一致性检查使用。
    """
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for model in doc.get("semantic_model", []):
        model_name = model.get("name", "<unnamed>")
        for ext in model.get("custom_extensions", []):
            data = _json_payload(ext)
            if data is not None:
                yield f"{path.name}.{model_name}", data, None
        for metric in model.get("metrics", []):
            for ext in metric.get("custom_extensions", []):
                data = _json_payload(ext)
                if data is not None:
                    mname = metric.get("name", "<unnamed>")
                    yield f"{path.name}.{model_name}.metric.{mname}", data, mname


def load_governance_payloads(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """提取文件内模型级 ATLAS payload，返回 [(model_name, data)]（老版接口）。"""
    return [
        (prefix.split(".", 1)[1], data)
        for prefix, data, metric_name in iter_payloads(path)
        if metric_name is None
    ]


def check_snapshot_anchor(
    declared_cases: set[str],
    expected_sha: Any,
    locked_shas: set[str],
    prefix: str,
) -> list[str]:
    """数值背书快照锚定校验（KL #16 收口，2026-09-04）。

    gold_test_cases 非空的指标，若 expected_value_snapshot_sha 已回填（非占位）
    则必须指向 data/snapshots 已锁定快照（防占位漂移、防指向未锁快照）；
    占位 `<待填写>`/null/空是机制未启用状态的如实声明（如零售模型数据未装载，
    无可锚快照），允许保留。gold_test_cases 为空同样不强制。

    返回错误消息列表（空 = 通过）。纯函数，供契约测试直接调用。
    """
    if not declared_cases:
        return []
    if expected_sha in (None, "", PLACEHOLDER_SNAPSHOT_SHA):
        return []
    if expected_sha not in locked_shas:
        return [
            f"{prefix}: expected_value_snapshot_sha={expected_sha}"
            f" 不是已锁定快照（{SNAPSHOT_DIR.name} 下无对应 .meta.json）"
        ]
    return []


def _load_locked_snapshots(errors: list[str]) -> set[str] | None:
    """已锁定快照 sha 集合（data/snapshots/*.meta.json 文件名去 .meta.json 后缀）。"""
    if not SNAPSHOT_DIR.exists():
        errors.append(f"缺少快照目录 {SNAPSHOT_DIR}")
        return None
    # 不能用 Path.stem：`30b8344.meta.json` 的 stem 是 `30b8344.meta`（双后缀）
    return {p.name.removesuffix(".meta.json") for p in SNAPSHOT_DIR.glob("*.meta.json")}


def validate_file(path: Path, schema: dict[str, Any], errors: list[str]) -> None:
    """校验单个文件的所有治理扩展（模型级 + 指标级 payload）。"""
    registry = _load_registry(errors)
    policy_names = _load_policy_names(errors)
    gold_ids = _load_gold_ids(errors)
    gold_refs = _gold_refs_by_metric()
    locked_shas = _load_locked_snapshots(errors)

    for prefix, data, metric_name in iter_payloads(path):
        if "_parse_error" in data:
            errors.append(f"{prefix}: custom_extensions.data 不是合法 JSON：{data['_parse_error']}")
            continue
        _check_payload(
            prefix,
            data,
            metric_name,
            schema,
            registry,
            policy_names,
            gold_ids,
            gold_refs,
            locked_shas,
            errors,
        )


def _check_payload(
    prefix: str,
    data: dict[str, Any],
    metric_name: str | None,
    schema: dict[str, Any],
    registry: set[str] | None,
    policy_names: set[str] | None,
    gold_ids: set[str] | None,
    gold_refs: dict[str, set[str]] | None,
    locked_shas: set[str] | None,
    errors: list[str],
) -> None:
    """校验单个 ATLAS payload 的 schema 与各类引用（模型级 metric_name=None）。"""
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        errors.append(f"{prefix}: 未通过 atlas_governance.schema.json：{exc.message}")

    alignment = data.get("fibo_alignment")
    if alignment:
        if registry is None:
            errors.append(
                f"{prefix}: 缺少权威注册表 {REGISTRY_PATH.name}（运行 data/fibo 的注册表导出命令）"
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
    declared = set(data.get("quality", {}).get("gold_test_cases", []))
    for case_id in declared:
        if gold_ids is not None and case_id not in gold_ids:
            errors.append(f"{prefix}: gold_test_cases 引用的用例 {case_id} 不存在于 eval/gold/")
    # 双向一致性（仅指标级且声明非空；gold 对该指标无直引时跳过——
    # 零售模型用别名映射 gold-001 的 gmv → total_sales_price，无法按名直查）
    if metric_name is not None and gold_refs is not None and declared:
        refs = gold_refs.get(metric_name, set())
        if refs and declared != refs:
            errors.append(
                f"{prefix}: gold_test_cases 与 eval/gold 引用双向不一致"
                f"（gold 引用 {sorted(refs)}，YAML 声明 {sorted(declared)}）"
            )
    # 数值背书快照锚定（KL #16 收口，2026-09-04）：有用例的指标必须锚已锁快照
    if locked_shas is not None:
        errors.extend(
            check_snapshot_anchor(
                declared,
                data.get("quality", {}).get("expected_value_snapshot_sha"),
                locked_shas,
                prefix,
            )
        )


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


def _gold_paths() -> list[Path]:
    """全部 gold 样本路径（目录化后跨 finance/ retail/ 两个域子目录）。"""
    return sorted(GOLD_DIR.glob("*/gold-*.json"))


def _load_gold_ids(errors: list[str]) -> set[str] | None:
    if not GOLD_DIR.exists():
        errors.append(f"缺少黄金集目录 {GOLD_DIR}")
        return None
    return {p.stem for p in _gold_paths()}


def _gold_refs_by_metric() -> dict[str, set[str]] | None:
    """gold 集 → 每指标的真实引用样本 id 集（expected_metric 口径，歧义样本不计）。"""
    if not GOLD_DIR.exists():
        return None
    refs: dict[str, set[str]] = {}
    for p in _gold_paths():
        try:
            sample = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if sample.get("ambiguous"):
            continue
        metric = sample.get("expected_metric")
        if isinstance(metric, str) and metric:
            refs.setdefault(metric, set()).add(p.stem)
    return refs


def collect_metric_governance(paths: list[Path]) -> list[MetricGovernance]:
    """收集全部文件的指标级 governance 记录，供 supersedes 跨文件交叉校验。

    结构错误（governance 缺失/类型不符）已由 jsonschema 在 _check_payload 报错，
    此处直接跳过，避免重复报告。
    """
    records: list[MetricGovernance] = []
    for p in paths:
        for prefix, data, metric_name in iter_payloads(p):
            if metric_name is None or "_parse_error" in data:
                continue
            gov = data.get("governance")
            if not isinstance(gov, dict):
                continue
            version = gov.get("version")
            status = gov.get("status")
            supersedes = gov.get("supersedes")
            if not isinstance(version, int) or not isinstance(status, str):
                continue
            if supersedes is not None and not isinstance(supersedes, str):
                continue
            records.append(
                MetricGovernance(
                    metric_name=metric_name,
                    version=version,
                    status=status,
                    supersedes=supersedes,
                    prefix=prefix,
                )
            )
    return records


def check_supersedes_chains(records: list[MetricGovernance], errors: list[str]) -> None:
    """跨文件校验 supersedes 链语义：目标存在、非自身、版本递增、被取代者不得 active。

    版本号严格递增的约束同时阻断循环链（A(v2)→B(v1) 后 B 不可能再回指 A，
    因为要求 B.version > A.version 恒假）。
    """
    by_name: dict[str, MetricGovernance] = {}
    for rec in records:
        prev = by_name.get(rec.metric_name)
        if prev is not None:
            errors.append(f"指标治理记录重名：{rec.metric_name}（{prev.prefix} 与 {rec.prefix}）")
        by_name[rec.metric_name] = rec

    for rec in records:
        target_name = rec.supersedes
        if not target_name:
            continue
        target = by_name.get(target_name)
        if target is None:
            errors.append(f"{rec.prefix}: supersedes 指向不存在的指标 {target_name}")
            continue
        if target_name == rec.metric_name:
            errors.append(f"{rec.prefix}: supersedes 不能指向自身")
            continue
        if rec.version <= target.version:
            errors.append(
                f"{rec.prefix}: supersedes {target_name}（v{target.version}）时"
                f"新版本 v{rec.version} 必须严格大于被取代版本"
            )
        if target.status == "active":
            errors.append(
                f"{rec.prefix}: 被取代指标 {target_name} 仍为 active——"
                f"发布新版本前须先将旧指标置为 deprecated（禁止同名 active 并存）"
            )


def main(argv: list[str] | None = None) -> int:
    paths = [Path(p) for p in (argv or sys.argv[1:])]
    if not paths:
        paths = sorted(Path(REPO / "semantic" / "ossie").glob("*.ossie.yaml"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    errors: list[str] = []
    for p in paths:
        validate_file(p, schema, errors)
    # supersedes 需要跨文件全量视图（可能跨模型/文件取代），在文件级校验后单独跑
    check_supersedes_chains(collect_metric_governance(paths), errors)

    for err in errors:
        print(f"  ❌ {err}")
    if errors:
        print(f"\n校验失败：{len(errors)} 个问题")
        return 1
    print(f"✅ 治理扩展校验通过：{len(paths)} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
