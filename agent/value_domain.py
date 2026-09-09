"""维度值域注册表：只读加载 + filter 值归属解析（ADR-0016，批次 B4）

设计原则
--------
1. **数据驱动，不猜**：值本体由 `make profile-values` 从**当前锁定快照**执行
   `SELECT DISTINCT` 生成（`data/value_profile.py`），人工只追加 `aliases` 映射；
   本模块只读，不连库、不推断、不写文件。
2. **安全默认 = 不校验**：只有 `status=registered` 且文件存在的 profile 参与
   值校验。未注册 / `skipped`（大基数字段超阈值）/ 文件缺失 → 原样透传。
   反向推断（"没登记的值一定不存在"）会让无值域覆盖的列静默拒绝合法查询，
   比漏匹配更危险，因此宁缺不猜。
3. **归一形态封闭**：exact（大小写敏感精确命中，**不改值**）→ alias（人工别名表，
   归一 + 记录）→ case（大小写折叠**唯一**命中值本体，归一 + 记录）→ unknown
   （既非值也非别名，或折叠后多命中 → 交调用方澄清）。折叠多命中不归一：
   实测 Gender 列 `F`/`f`、`M`/`m` 并存，折叠匹配到有歧义的路。

已知边界（诚实声明）
------------------
- 值域是**快照态**而非实时：数据重新装载后 profile 过期，由 `semantic/lint.py`
  的 `[values]` 检查锁 `snapshot_sha` 强制重新生成（ADR-0016 §③）。
- 值一律以 DISTINCT 结果的字符串表示存储；数值维度列（如 Tier）的等值比较用
  `str(value)` 对照，命中后**保留原类型**（int 不被降级成 str，编译器出口不变）。
- profile 按**编译器实际绑定列**生成（`SemanticModel.find_field` 按 datasets
  顺序首匹配）。同名维度字段跨数据集时，绑定列可能不是直觉的那张表——
  该事实如实记在 profile 的 `bound_dataset`，编译器首匹配规则本批不改（ADR-0016 §④）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

REPO = Path(__file__).resolve().parent.parent
VALUES_DIR = REPO / "semantic" / "values"

# clarification 里列出的候选值上限（按频次降序取前 N，最可能的意图排前面）
CANDIDATE_SAMPLE = 10

ProfileStatus = Literal["registered", "skipped"]
ResolveKind = Literal["unregistered", "exact", "alias", "case", "unknown"]

_REQUIRED_KEYS = (
    "model",
    "field",
    "status",
    "snapshot_sha",
    "generated_at",
    "values",
    "aliases",
)


@dataclass(frozen=True)
class ValueProfile:
    """一个「模型.维度字段」的值域快照（semantic/values/<model>.<field>.json）。"""

    model: str
    field: str
    status: ProfileStatus
    snapshot_sha: str
    generated_at: str
    values: tuple[tuple[str, int], ...]  # (值, 频次)，频次降序 + 值升序
    aliases: dict[str, str]  # 别名原文（查表前 casefold）→ 值本体
    bound_dataset: str | None = None
    source_table: str | None = None
    source_column: str | None = None
    distinct_count: int = 0
    null_count: int | None = None
    skip_reason: str | None = None
    max_cardinality: int | None = None
    note: str = ""

    def filename(self) -> str:
        return f"{self.model}.{self.field}.json"

    def value_set(self) -> frozenset[str]:
        return frozenset(value for value, _ in self.values)

    def samples(self, limit: int = CANDIDATE_SAMPLE) -> tuple[str, ...]:
        """按频次降序的候选值样例（澄清文案用）。"""
        return tuple(value for value, _ in self.values[:limit])


@dataclass(frozen=True)
class Resolution:
    """值归属解析结果。`value` 是归一后的值（unknown 时保持原值，不做修改）。"""

    kind: ResolveKind
    value: object
    field: str
    profile: ValueProfile | None = None

    @property
    def normalized(self) -> bool:
        """是否发生了归一（alias/case）——调用方据此记录 notices。"""
        return self.kind in ("alias", "case")

    @property
    def candidates(self) -> tuple[str, ...]:
        return self.profile.samples() if self.profile is not None else ()


def profile_path(model: str, field: str, values_dir: Path = VALUES_DIR) -> Path:
    """值域文件路径（`<model>.<field>.json`）。"""
    return values_dir / f"{model}.{field}.json"


def display_path(model: str, field: str, values_dir: Path = VALUES_DIR) -> str:
    """值域文件的仓库相对路径（面向用户的澄清文案引用，不暴露绝对路径）。"""
    path = profile_path(model, field, values_dir)
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


# 进程内缓存：值域是只读配置，语义与 locale 词典一致（一次加载，永不热更）
_CACHE: dict[Path, ValueProfile] = {}


def clear_cache() -> None:
    """清空加载缓存（仅测试用：tmp fixture 之间隔离同名文件）。"""
    _CACHE.clear()


def load_profile(
    model: str, field: str, values_dir: Path = VALUES_DIR
) -> ValueProfile | None:
    """读单个值域文件；**文件不存在 → None**（未注册，不是错误）。

    存在但畸形 → ValueError（宁可启动即失败，不静默把某列降级成无校验）。
    """
    path = profile_path(model, field, values_dir)
    if not path.exists():
        return None
    if path in _CACHE:
        return _CACHE[path]
    profile = parse_profile(path)
    _CACHE[path] = profile
    return profile


def parse_profile(path: Path) -> ValueProfile:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"值域文件顶层必须是对象：{path}")
    missing = [key for key in _REQUIRED_KEYS if key not in doc]
    if missing:
        raise ValueError(f"值域文件缺键 {missing}：{path}")

    status = doc["status"]
    if status not in ("registered", "skipped"):
        raise ValueError(f"值域 status 未知（registered/skipped）：{status!r} {path}")
    raw_values = doc["values"]
    if not isinstance(raw_values, list):
        raise ValueError(f"值域 values 必须是数组：{path}")
    values: list[tuple[str, int]] = []
    for item in raw_values:
        if not isinstance(item, dict) or "value" not in item:
            raise ValueError(f"值域 values 每项必须是含 value 的对象：{path}")
        value = str(item["value"])
        count = item.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"值域 values.count 必须是非负整数：{path}")
        values.append((value, count))
    if len({value for value, _ in values}) != len(values):
        raise ValueError(f"值域存在重复 value：{path}")
    if status == "registered" and not values:
        raise ValueError(f"registered 值域不允许为空（空值域=该列无取值，应显式 skip）：{path}")
    if status == "skipped" and values:
        raise ValueError(f"skipped 值域不应携带 values：{path}")

    raw_aliases = doc["aliases"]
    if not isinstance(raw_aliases, dict):
        raise ValueError(f"值域 aliases 必须是 别名→值本体 映射：{path}")
    aliases: dict[str, str] = {}
    known = {value for value, _ in values}
    for key, target in raw_aliases.items():
        alias, mapped = str(key).strip(), str(target)
        if not alias:
            raise ValueError(f"值域 aliases 含空别名：{path}")
        if mapped not in known:
            # 悬空别名 = 值域重新生成后该值已不存在，静默放行会造出新漏匹配
            raise ValueError(f"别名 {alias!r} 指向未注册取值 {mapped!r}：{path}")
        folded = alias.casefold()
        if folded in {v.casefold() for v in known}:
            raise ValueError(
                f"别名 {alias!r} 大小写折叠后与值本体同名（应由 exact/case 命中，"
                f"不需要别名）：{path}"
            )
        if folded in aliases:
            raise ValueError(f"别名 {alias!r} 折叠后重复：{path}")
        aliases[folded] = mapped

    return ValueProfile(
        model=str(doc["model"]),
        field=str(doc["field"]),
        status=status,  # type: ignore[arg-type]
        snapshot_sha=str(doc["snapshot_sha"]),
        generated_at=str(doc["generated_at"]),
        values=tuple(values),
        aliases=aliases,
        bound_dataset=_opt_str(doc.get("bound_dataset")),
        source_table=_opt_str(doc.get("source_table")),
        source_column=_opt_str(doc.get("source_column")),
        distinct_count=_opt_int(doc.get("distinct_count")),
        null_count=None if doc.get("null_count") is None else _opt_int(doc["null_count"]),
        skip_reason=_opt_str(doc.get("skip_reason")),
        max_cardinality=None
        if doc.get("max_cardinality") is None
        else _opt_int(doc["max_cardinality"]),
        note=str(doc.get("note") or ""),
    )


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _opt_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"期望整数，实际为 {value!r}")
    return int(value)


def resolve(
    model: str, field: str, value: object, values_dir: Path = VALUES_DIR
) -> Resolution:
    """filter 值归属解析（ADR-0016 §②）。profile 缺失/未注册 → unregistered 透传。"""
    profile = load_profile(model, field, values_dir)
    if profile is None or profile.status != "registered":
        return Resolution("unregistered", value, field, profile)
    known = profile.value_set()
    text = str(value)
    if text in known:
        return Resolution("exact", value, field, profile)
    alias = profile.aliases.get(text.casefold())
    if alias is not None:
        return Resolution("alias", alias, field, profile)
    folded = [item for item in profile.values if item[0].casefold() == text.casefold()]
    if len(folded) == 1:
        return Resolution("case", folded[0][0], field, profile)
    return Resolution("unknown", value, field, profile)


def registered_dimensions(model: str, values_dir: Path = VALUES_DIR) -> tuple[str, ...]:
    """某模型已注册（registered）的维度字段名，按字典序（lint / 测试的完整性检查用）。"""
    found: list[str] = []
    for path in sorted(values_dir.glob(f"{model}.*.json")):
        profile = parse_profile(path)
        if profile.status == "registered":
            found.append(profile.field)
    return tuple(found)
