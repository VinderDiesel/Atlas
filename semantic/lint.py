"""语义层 lint 整合入口（`make lint`）

依次执行：
1. ossie_validate     —— 语义模型结构/唯一性/血缘引用
2. governance_validate —— 治理扩展（schema / FIBO IRI 注册表 / 策略与黄金集引用）
3. gold schema 校验   —— eval/gold/*.json 结构（FIBO 闭包深度校验见 eval/gold/validate_gold.py）
4. 权威源唯一性     —— semantic/ 下语义定义文件只允许住在权威目录（ADR-0002/0015）

任一环节失败即返回非零退出码。

用法：.venv/bin/python -m semantic.lint --all
"""

from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

import jsonschema

from semantic import governance_validate, ossie_validate

REPO = Path(__file__).resolve().parent.parent
GOLD_SCHEMA = REPO / "eval" / "gold" / "schema.json"
SEMANTIC_ROOT = REPO / "semantic"
# 语义定义文件（*.yaml / *.yml）允许存放的目录（ADR-0002：权威源唯一 = ossie/；
# ADR-0015：locale 同义词/形态词典在 synonyms/；行级策略声明在 policies/）
_AUTHORITATIVE_DIRS = frozenset({"ossie", "synonyms", "policies"})
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
    print("✅ [authority] 语义定义文件仅在权威目录（ossie/ synonyms/ policies/）")

    print("\nlint 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
