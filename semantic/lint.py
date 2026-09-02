"""语义层 lint 整合入口（`make lint`）

依次执行：
1. ossie_validate     —— 语义模型结构/唯一性/血缘引用
2. governance_validate —— 治理扩展（schema / FIBO IRI 注册表 / 策略与黄金集引用）
3. gold schema 校验   —— eval/gold/*.json 结构（FIBO 闭包深度校验见 eval/gold/validate_gold.py）

任一环节失败即返回非零退出码。

用法：.venv/bin/python -m semantic.lint --all
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import jsonschema

from semantic import governance_validate, ossie_validate

REPO = Path(__file__).resolve().parent.parent
GOLD_SCHEMA = REPO / "eval" / "gold" / "schema.json"


def check_gold_schema() -> list[str]:
    """黄金集样本结构校验（轻量，无 FIBO 依赖）。"""
    errors: list[str] = []
    schema = json.loads(GOLD_SCHEMA.read_text(encoding="utf-8"))
    for p in sorted(glob.glob(str(REPO / "eval" / "gold" / "gold-*.json"))):
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
    n_gold = len(glob.glob(str(REPO / "eval" / "gold" / "gold-*.json")))
    print(f"✅ [gold] 黄金集结构校验通过：{n_gold} 条样本")

    print("\nlint 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
