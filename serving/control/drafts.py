"""T08 语义草稿服务（ADR-0031 D02/D04/D13 管理面）。

设计口径
--------
- **草稿非权威**（合同原文）：创建/编辑/校验/审核/导出都不改变任何运行 Metric
  与发布指针；激活只能经 T08b 的显式 CAS 发布（D04）。本服务不导入制品、不执行
  任何代码、不写 Git。
- **形状门在服务层**：`content` 必须恰为 `{target, document}`；`target` 同时命中
  制品白名单（agent.runtime.bundle，与发布装载同一事实源）与 kind 前缀——不可
  导出的草稿不允许存在。
- **对象 ACL**：编辑仅限草稿所有者（他人 403）；读取要求草稿能力（edit/review/
  export）且 scope 已授权——审核需要跨人可见同域草稿（T08a-s2）。
- **base_git_sha 服务端取**：本地 HEAD 的完整 SHA，不采信客户端声称（配置即证据）。

校验与审核口径（T08a-s2）
-------------------------
- **validate 是确定性动作**：在临时文件上复跑 `make lint` 的三套校验器（结构
  ossie_validate / 治理 governance_validate / 策略一致性 check_policy_consistency），
  草稿不落语义目录、校验器零改动。同名 active（N8）用同目录**其他**模型播种
  seen_names（草稿是目标文件的下一版，不与自己比）；策略一致性必须传全部同目录
  文件——只传草稿会把其他策略误报为孤儿。
- **条件推进**：`passed` 且草稿仍在 `draft` 才推进 `validated`；重复校验只追加
  证据、不漂移状态（幂等重验）。非 semantic kind → 422（没有确定性校验器的草稿
  不得静默通过）。
- **审核只认 validated**：未校验或编辑后失效一律 409；approved 推进 `reviewed`、
  rejected 只落证据（同修订可重审）。证据都绑定 revision + 内容摘要——审核后
  篡改由 revision 递增自然作废（旧证据不复用）。

导出口径（export_patch）
------------------------
- base 文本 = `git show {base_git_sha}:{target}`（只读对象库，不读脏工作树）；
  **未变更片段逐字节保留原文**，只有变更/新增块重新发射——未编辑草稿导出空
  patch（最强锚），单字段改动只动该行。
- 新文件目标（base sha 中无此路径）→ `/dev/null` 新增形态，不伪造上下文。
- impact 按全局身份名给出增删改（指标 `name`、维度 `dataset.field`）。

边界（诚实声明）
----------------
- findings 只覆盖上述三套校验器的规则集，不声称语义层全量 lint（gold/values
  运行时一致性与编译器/守卫不在本批）。
- export_patch 只产生统一 diff 文本；把它变成发布是 T08b 的 ReleaseService
  （import → 门禁 → CAS 发布），本服务不激活、不写 Git。
"""

from __future__ import annotations

import difflib
import json
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agent.runtime.bundle import REPO_CONFIG_PATTERNS
from data.identity import REPO_ROOT, git_full_sha
from semantic import governance_validate, ossie_validate
from serving.control.auth import ControlForbidden, Principal, authorize
from serving.control.contracts import (
    Draft,
    DraftEditRequest,
    DraftPatchView,
    DraftRequest,
    DraftReview,
    DraftReviewRequest,
    DraftValidation,
    Owner,
    PatchImpact,
    ValidationFinding,
    ValidationStatus,
    canonical_json,
)
from serving.control.store import ControlStore

DRAFT_EDIT_CAPABILITY = "draft.edit"
DRAFT_VALIDATE_CAPABILITY = "draft.validate"
DRAFT_REVIEW_CAPABILITY = "draft.review"
DRAFT_EXPORT_CAPABILITY = "draft.export"
# 读取门：编辑/审核/导出（审核与导出都需要读取草稿；列表按域裁剪、不因人裁剪）。
_DRAFT_READ_CAPABILITIES = frozenset(
    {DRAFT_EDIT_CAPABILITY, DRAFT_REVIEW_CAPABILITY, DRAFT_EXPORT_CAPABILITY}
)
# kind → 目标前缀（与制品白名单双门；跨 kind 目标拒绝）。
_KIND_TARGET_PREFIXES: Mapping[str, tuple[str, ...]] = {
    "semantic": ("semantic/ossie/",),
    "flow": ("agent/flows/templates/", "agent/flows/configs/"),
    "node_config": ("agent/prompts/",),
}
_CONTENT_KEYS = frozenset({"target", "document"})
# 当前只有语义模型具备确定性校验器；其他 kind 明确 422 而非静默通过。
_VALIDATABLE_KIND = "semantic"
_OSSIE_DIR = REPO_ROOT / "semantic" / "ossie"


class DraftContentInvalid(ValueError):
    """草稿 content 形状或目标路径不合法（HTTP 层投影为 422）。"""


def validate_content(kind: str, content: Mapping[str, object]) -> None:
    """形状门：键集恰为 {target, document}；target 命中制品白名单与 kind 前缀。

    Raises
    ------
    DraftContentInvalid
        未知键/缺键、document 非对象、target 非字符串或路径不在白名单。
    """
    if set(content) != _CONTENT_KEYS:
        raise DraftContentInvalid(f"content 键必须恰为 {sorted(_CONTENT_KEYS)}")
    target = content["target"]
    document = content["document"]
    if not isinstance(target, str) or not isinstance(document, dict):
        raise DraftContentInvalid("content.target 必须是字符串、content.document 必须是对象")
    if not any(target.startswith(prefix) for prefix in _KIND_TARGET_PREFIXES.get(kind, ())):
        raise DraftContentInvalid(f"{kind} 草稿目标不在允许目录：{target!r}")
    if not any(re.fullmatch(pattern, target) for pattern in REPO_CONFIG_PATTERNS):
        raise DraftContentInvalid(f"草稿目标不在制品白名单：{target!r}")


# ---------------------------------------------------------------------------
# 最小 diff 导出：原文块索引（PyYAML mark）+ 递归拼接 + 仓库风格发射
# ---------------------------------------------------------------------------
# 为什么自研发射器：实测 PyYAML 的默认风格与仓库不符（safe_dump 列表项与父键同
# 缩进、块标量缩进 4 空格；仓库是列表项 -2、块标量内容 +2），naive 全文件重发射
# 在 atlas_finance.ossie.yaml（1821 行）上产出上千行 diff——patch 不可用。因此
# 采用「未变更片段逐字节复制原文、仅变更块重发射」的拼接策略。

_BLOCK_MAP = "map"
_BLOCK_SEQ = "seq"
_BLOCK_SCALAR = "scalar"
_IDENTITY_KEYS = ("name", "vendor_name")


@dataclass(frozen=True)
class _Block:
    """原文行跨度与结构（0-based 半开区间）：只为最小 diff 服务，不做语义解释。

    - [start, content_end)：块占据的行区间（含尾随空行，PyYAML 的集合 end 语义）；
    - value：该块的解析值（与草稿对应子树比对，相等即原文复制）；
    - map：`keys[i]` 的条目区间 = [entry_starts[i], entry_starts[i+1])，
      最后一条到 content_end（entry_starts 是键行，值块起始在 children[i].start）；
    - seq：条目区间同理，entry_starts 是 `- ` 起始行。
    """

    kind: str
    start: int
    content_end: int
    value: Any
    keys: tuple[str, ...] = ()
    entry_starts: tuple[int, ...] = ()
    children: tuple[_Block, ...] = ()


def _block_index(node: yaml.Node, value: Any) -> _Block:
    """PyYAML 节点 + 解析值并行成块索引；mark 行号即原文行号（0-based）。"""
    start = node.start_mark.line
    end = max(node.end_mark.line, start + 1)
    if isinstance(node, yaml.MappingNode):
        keys: list[str] = []
        entry_starts: list[int] = []
        children: list[_Block] = []
        mapping = value if isinstance(value, dict) else {}
        for key_node, value_node in node.value:
            key = str(key_node.value)
            keys.append(key)
            entry_starts.append(key_node.start_mark.line)
            children.append(_block_index(value_node, mapping.get(key)))
        return _Block(
            _BLOCK_MAP,
            start,
            end,
            value,
            tuple(keys),
            tuple(entry_starts),
            tuple(children),
        )
    if isinstance(node, yaml.SequenceNode):
        item_starts: list[int] = []
        item_blocks: list[_Block] = []
        items = value if isinstance(value, list) else []
        for position, item_node in enumerate(node.value):
            item_starts.append(item_node.start_mark.line)
            item_value = items[position] if position < len(items) else None
            item_blocks.append(_block_index(item_node, item_value))
        return _Block(_BLOCK_SEQ, start, end, value, (), tuple(item_starts), tuple(item_blocks))
    return _Block(_BLOCK_SCALAR, start, end, value)


def _pad(indent: int) -> str:
    return " " * indent


def _plain_scalar(text: str) -> bool:
    """裸标量判定：回读仍是同一字符串（保仓库风格，否则转引号）。

    用 safe_load 直接验证「写了之后能读回」——resolver 单独引入会破坏 yaml 包
    的存根覆盖（import-untyped），且行为差异只体现在我们不覆盖的边缘形态上。
    """
    if not text or text != text.strip() or "\n" in text or text.endswith(":"):
        return False
    if text[0] in "-?:,[]{}#&*!|>'\"%@`" or ": " in text or " #" in text:
        return False
    try:
        return isinstance(yaml.safe_load(text), str)
    except yaml.YAMLError:
        return False


def _scalar_text(value: Any) -> str:
    """标量文本：能裸写就裸写（保仓库风格），否则 JSON 双引号转义（YAML 子集）。"""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    text = value if isinstance(value, str) else str(value)
    return text if _plain_scalar(text) else json.dumps(text, ensure_ascii=False)


def _block_literal(value: str, indent: int) -> list[str] | None:
    """多行字符串 → `|`/`|-` 块标量行；超过一个尾换行返回 None（走引号兜底）。"""
    if value.endswith("\n\n"):
        return None
    chomp = "|" if value.endswith("\n") else "|-"
    body = value.rstrip("\n").split("\n") if chomp == "|" else value.split("\n")
    content = [(_pad(indent) + line) if line else "" for line in body]
    return content


def _emit_entry(key: str, value: Any, indent: int) -> list[str]:
    """映射条目：`key: 标量` / `key:` + 子块 / `key: |` + 块标量内容。"""
    prefix = f"{_pad(indent)}{key}:"
    if isinstance(value, dict):
        return [f"{prefix} {{}}"] if not value else [prefix, *_emit(value, indent + 2)]
    if isinstance(value, list):
        return [f"{prefix} []"] if not value else [prefix, *_emit(value, indent + 2)]
    if isinstance(value, str) and "\n" in value:
        literal = _block_literal(value, indent + 2)
        if literal is not None:
            chomp = "|" if value.endswith("\n") else "|-"
            return [f"{prefix} {chomp}", *literal]
    return [f"{prefix} {_scalar_text(value)}"]


def _emit_item(item: Any, indent: int) -> list[str]:
    """序列条目：映射内联首键（`- key: ...`，与仓库风格一致）；标量/流式列表。"""
    if isinstance(item, dict):
        if not item:
            return [f"{_pad(indent)}- {{}}"]
        lines = _emit(item, indent + 2)
        lines[0] = f"{_pad(indent)}- {lines[0][indent + 2 :]}"
        return lines
    if isinstance(item, list):
        inner = ", ".join(_scalar_text(element) for element in item)
        return [f"{_pad(indent)}- [{inner}]"]
    return [f"{_pad(indent)}- {_scalar_text(item)}"]


def _emit(value: Any, indent: int = 0) -> list[str]:
    """仓库风格发射（列表项内联首键、块标量 |/|-、空容器 {}/[]）。"""
    if isinstance(value, dict):
        if not value:
            return [f"{_pad(indent)}{{}}"]
        lines: list[str] = []
        for key, item in value.items():
            lines.extend(_emit_entry(str(key), item, indent))
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{_pad(indent)}[]"]
        lines = []
        for item in value:
            lines.extend(_emit_item(item, indent))
        return lines
    return [f"{_pad(indent)}{_scalar_text(value)}"]


def _identity_key(block: _Block) -> str | None:
    """序列条目的身份键（name 优先、vendor_name 兜底）；不满足（标量/重名）返回 None。"""
    if not block.children:
        return None
    for key in _IDENTITY_KEYS:
        if not all(child.kind == _BLOCK_MAP and key in child.keys for child in block.children):
            continue
        names = [
            child.value.get(key) if isinstance(child.value, dict) else None
            for child in block.children
        ]
        if all(isinstance(name, str) and name for name in names) and len(set(names)) == len(names):
            return key
    return None


def _aligned_items(new_value: list[Any], identity: str) -> bool:
    """新列表每个条目都带唯一非空身份键（否则整块重发射，不猜对齐）。"""
    names = [item.get(identity) if isinstance(item, dict) else None for item in new_value]
    return all(isinstance(name, str) and name for name in names) and len(set(names)) == len(names)


def _splice_entry(
    key: str,
    key_line: int,
    child: _Block,
    new_value: Any,
    lines: list[str],
    entry_ceil: int,
    indent: int,
) -> list[str]:
    """单个映射条目（键行..entry_ceil）：值未变则整条原文复制。"""
    if new_value == child.value:
        return lines[key_line:entry_ceil]
    if child.kind == _BLOCK_MAP and isinstance(new_value, dict):
        return [
            *lines[key_line : child.start],
            *_splice_map(child, new_value, lines, entry_ceil, indent + 2),
        ]
    if child.kind == _BLOCK_SEQ and isinstance(new_value, list):
        return [
            *lines[key_line : child.start],
            *_splice_seq(child, new_value, lines, entry_ceil, indent + 2),
        ]
    # 标量替换或结构类型变化：重发射该条目；尾行（空行/注释）照旧保留
    return [*_emit_entry(key, new_value, indent), *lines[child.content_end : entry_ceil]]


def _splice_map(
    block: _Block, new_value: Mapping[str, Any], lines: list[str], ceil: int, indent: int
) -> list[str]:
    """逐键拼接：同键递归、删键丢弃、新增键末尾追加（既有键序不变）。"""
    out: list[str] = []
    for position, key in enumerate(block.keys):
        if key not in new_value:
            continue  # 删除：条目区间（含尾随空行/注释）随键一起移除
        entry_ceil = (
            block.entry_starts[position + 1]
            if position + 1 < len(block.keys)
            else block.content_end
        )
        out.extend(
            _splice_entry(
                key,
                block.entry_starts[position],
                block.children[position],
                new_value[key],
                lines,
                entry_ceil,
                indent,
            )
        )
    for key, value in new_value.items():
        if key not in block.keys:
            out.extend(_emit_entry(str(key), value, indent))
    out.extend(lines[block.content_end : ceil])
    return out


def _splice_seq(
    block: _Block, new_value: list[Any], lines: list[str], ceil: int, indent: int
) -> list[str]:
    """按身份键对齐条目：未变条目原文复制、删除丢弃、新增按仓库风格追加（空行分隔）。"""
    identity = _identity_key(block)
    if identity is None or not _aligned_items(new_value, identity):
        return [*_emit(new_value, indent), *lines[block.content_end : ceil]]
    remaining: dict[str, Any] = {
        str(item[identity]): item for item in new_value if isinstance(item, dict)
    }
    out: list[str] = []
    for position, child in enumerate(block.children):
        name = str(child.value[identity]) if isinstance(child.value, dict) else ""
        if name not in remaining:
            continue  # 删除条目
        item_ceil = (
            block.entry_starts[position + 1]
            if position + 1 < len(block.children)
            else block.content_end
        )
        out.extend(_splice(child, remaining.pop(name), lines, item_ceil, indent + 2))
    for item in remaining.values():
        if out and out[-1].strip():
            out.append("")  # 仓库风格：条目之间空行分隔
        out.extend(_emit_item(item, indent))
    out.extend(lines[block.content_end : ceil])
    return out


def _splice(block: _Block, new_value: Any, lines: list[str], ceil: int, indent: int) -> list[str]:
    """把 lines[block.start:ceil] 替换为 new_value 的表达；未变更片段原文复制。

    indent 是块内容的基准列（map 为键列、seq 为 `- ` 列）。每个分支都必须把
    [block.content_end, ceil) 的尾行（空行/注释）原样带回。
    """
    if new_value == block.value:
        return lines[block.start:ceil]
    if block.kind == _BLOCK_MAP and isinstance(new_value, dict):
        return _splice_map(block, new_value, lines, ceil, indent)
    if block.kind == _BLOCK_SEQ and isinstance(new_value, list):
        return _splice_seq(block, new_value, lines, ceil, indent)
    return [*_emit(new_value, indent), *lines[block.content_end : ceil]]


def _unified_diff(before: list[str], after: list[str], *, fromfile: str, tofile: str) -> str:
    """统一 diff 文本（行尾不含换行；调用方按 git apply 需求自行带尾换行）。"""
    return "\n".join(
        difflib.unified_diff(before, after, fromfile=fromfile, tofile=tofile, lineterm="")
    )


def _render_patch(
    base_text: str | None, document: Mapping[str, Any], target: str
) -> tuple[str, Any]:
    """生成 (统一 diff, base 解析文档)；base_text=None（新文件）→ /dev/null 形态。"""
    if base_text is None:
        patch = _unified_diff(
            [], _emit(document, 0), fromfile="/dev/null", tofile=f"b/{target}"
        )
        return patch, None
    base_lines = base_text.splitlines()
    node: yaml.Node | None = None
    parsed: Any = None
    try:
        node = yaml.compose(base_text)
        parsed = yaml.safe_load(base_text)
    except yaml.YAMLError:
        node, parsed = None, None
    if not isinstance(node, yaml.MappingNode) or not isinstance(parsed, dict):
        # 非映射基线（或解析失败）：整文件重发射，不猜结构
        patch = _unified_diff(
            base_lines, _emit(document, 0), fromfile=f"a/{target}", tofile=f"b/{target}"
        )
        return patch, parsed
    block = _block_index(node, parsed)
    rendered = [
        *base_lines[: block.start],
        *_splice(block, document, base_lines, len(base_lines), 0),
    ]
    patch = _unified_diff(
        base_lines, rendered, fromfile=f"a/{target}", tofile=f"b/{target}"
    )
    return patch, parsed


def _git_show_file(sha: str, relative: str) -> str | None:
    """读取 base sha 下的目标文件文本；路径不存在返回 None（按新文件处理）。

    只读对象库（`git show`），不读工作树——脏树不是发布源（ADR-0031 D04）。
    """
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{sha}:{relative}"],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _metrics_by_name(document: Any) -> dict[str, Any]:
    """全模型指标索引（name → 指标对象）；非映射文档返回空表。"""
    index: dict[str, Any] = {}
    if not isinstance(document, Mapping):
        return index
    for model in document.get("semantic_model") or []:
        if not isinstance(model, Mapping):
            continue
        for metric in model.get("metrics") or []:
            if isinstance(metric, Mapping) and isinstance(metric.get("name"), str):
                index[metric["name"]] = metric
    return index


def _dimensions_by_name(document: Any) -> dict[str, Any]:
    """维度索引（`dataset.field` → 字段对象）：带 dimension 键的字段才是维度。"""
    index: dict[str, Any] = {}
    if not isinstance(document, Mapping):
        return index
    for model in document.get("semantic_model") or []:
        if not isinstance(model, Mapping):
            continue
        for dataset in model.get("datasets") or []:
            if not isinstance(dataset, Mapping):
                continue
            dataset_name = dataset.get("name")
            for field_value in dataset.get("fields") or []:
                if not isinstance(field_value, Mapping) or "dimension" not in field_value:
                    continue
                field_name = field_value.get("name")
                if isinstance(dataset_name, str) and isinstance(field_name, str):
                    index[f"{dataset_name}.{field_name}"] = field_value
    return index


def _diff_names(before: Mapping[str, Any], after: Mapping[str, Any]) -> PatchImpact:
    """(added, removed, changed) 三集合 → PatchImpact（排序稳定，changed 按规范 JSON）。"""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(
        name
        for name in set(before) & set(after)
        if canonical_json(before[name]) != canonical_json(after[name])
    )
    return PatchImpact(
        added_metrics=added,
        removed_metrics=removed,
        changed_metrics=changed,
        added_dimensions=[],
        removed_dimensions=[],
        changed_dimensions=[],
    )


def _impact(base_document: Any, document: Any) -> PatchImpact:
    """导出影响面：指标与维度分别给出增删改（base 为 None 时全部视为新增）。"""
    metrics = _diff_names(_metrics_by_name(base_document), _metrics_by_name(document))
    dimensions = _diff_names(_dimensions_by_name(base_document), _dimensions_by_name(document))
    return PatchImpact(
        added_metrics=metrics.added_metrics,
        removed_metrics=metrics.removed_metrics,
        changed_metrics=metrics.changed_metrics,
        added_dimensions=dimensions.added_metrics,
        removed_dimensions=dimensions.removed_metrics,
        changed_dimensions=dimensions.changed_metrics,
    )


class DraftService:
    """草稿服务：能力/作用域门 + 形状门 + owner ACL + 修订 CAS + 校验/审核/导出。"""

    def __init__(
        self, store: ControlStore, *, base_sha: Callable[[], str] | None = None
    ) -> None:
        self._store = store
        self._base_sha = base_sha if base_sha is not None else lambda: git_full_sha(REPO_ROOT)

    def create(self, principal: Principal, request: DraftRequest) -> Draft:
        """创建非权威草稿（revision=1，status=draft）；返回固定 11 键草稿。

        Raises
        ------
        ControlForbidden
            缺 `draft.edit` 或 scope ∉ principal.scopes（D02）。
        DraftContentInvalid
            content 形状/目标路径不合法或超过容量上限（HTTP 422）。
        """
        authorize(principal, DRAFT_EDIT_CAPABILITY, request.scope)
        validate_content(request.kind, request.content)
        try:
            return self._store.create_draft(
                kind=request.kind,
                owner=Owner(issuer=principal.issuer, subject=principal.subject),
                scope=request.scope,
                base_git_sha=self._base_sha(),
                content=request.content,
            )
        except ValueError as exc:  # 容量上限等非法内容（RevisionConflict 是 RuntimeError）
            raise DraftContentInvalid(str(exc)) from exc

    def edit(
        self, principal: Principal, draft_id: str, request: DraftEditRequest, *, expected: int
    ) -> Draft:
        """CAS 编辑本人草稿；改内容即撤销放行状态（store 原语），不触碰发布指针。

        Raises
        ------
        KeyError
            草稿不存在（路由层投影为 404）。
        ControlForbidden
            缺 `draft.edit`、scope 未授权或非草稿所有者（对象 ACL）。
        DraftContentInvalid
            内容不合法或超过容量上限（HTTP 422）。
        RevisionConflict
            修订已变化（HTTP 409）。
        """
        draft = self._store.get_draft(draft_id)
        authorize(principal, DRAFT_EDIT_CAPABILITY, draft.scope)
        if draft.owner != Owner(issuer=principal.issuer, subject=principal.subject):
            raise ControlForbidden("草稿对象 ACL：只允许编辑本人草稿")
        validate_content(draft.kind, request.content)
        try:
            return self._store.update_draft(draft_id, request.content, expected=expected)
        except ValueError as exc:  # 容量上限等非法内容
            raise DraftContentInvalid(str(exc)) from exc

    def view(self, principal: Principal, draft_id: str) -> Draft:
        """草稿详情；未知抛 KeyError，未授权域抛 ControlForbidden（不裁剪）。"""
        self._require_read(principal)
        draft = self._store.get_draft(draft_id)
        if draft.scope not in principal.scopes:
            raise ControlForbidden(f"作用域未授权：{draft.scope!r}")
        return draft

    def list_drafts(self, principal: Principal) -> list[Draft]:
        """已授权域的草稿列表（服务端裁剪；零授权 = 空表）。"""
        self._require_read(principal)
        return [draft for draft in self._store.list_drafts() if draft.scope in principal.scopes]

    def validate(self, principal: Principal, draft_id: str) -> DraftValidation:
        """确定性校验草稿（结构/治理/策略）；通过且仍在 draft 时同事务推进 validated。

        Raises
        ------
        KeyError
            草稿不存在（路由层投影为 404）。
        ControlForbidden
            缺 `draft.validate` 或 scope 未授权（403）。
        DraftContentInvalid
            非 semantic 草稿（当前只有语义模型有确定性校验器，422）。
        RevisionConflict
            校验期间草稿被编辑（证据不绑定未校验内容，409）。
        """
        draft = self._store.get_draft(draft_id)
        authorize(principal, DRAFT_VALIDATE_CAPABILITY, draft.scope)
        if draft.kind != _VALIDATABLE_KIND:
            raise DraftContentInvalid(
                f"确定性校验目前只覆盖 {_VALIDATABLE_KIND} 草稿：{draft.kind!r}"
            )
        findings = self._semantic_findings(draft)
        status: ValidationStatus = "failed" if findings else "passed"
        return self._store.record_validation(
            draft_id,
            expected=draft.revision,
            status=status,
            findings=findings,
            actor=Owner(issuer=principal.issuer, subject=principal.subject),
            advance=status == "passed",
        )

    def review(
        self, principal: Principal, draft_id: str, request: DraftReviewRequest
    ) -> DraftReview:
        """人工审核：只认 validated 状态；approved 推进 reviewed、rejected 只落证据。

        Raises
        ------
        KeyError
            草稿不存在（404）。
        ControlForbidden
            缺 `draft.review` 或 scope 未授权（403）。
        RevisionConflict
            草稿未处于 validated（未校验或编辑后失效），HTTP 409。
        """
        draft = self._store.get_draft(draft_id)
        authorize(principal, DRAFT_REVIEW_CAPABILITY, draft.scope)
        return self._store.record_review(
            draft_id,
            decision=request.decision,
            comment=request.comment,
            actor=Owner(issuer=principal.issuer, subject=principal.subject),
        )

    def export_patch(self, principal: Principal, draft_id: str) -> DraftPatchView:
        """导出最小统一 diff + 影响面；base 取草稿的 base_git_sha，不读脏工作树。

        Raises
        ------
        KeyError
            草稿不存在（404）。
        ControlForbidden
            缺 `draft.export` 或 scope 未授权（403）。
        """
        draft = self._store.get_draft(draft_id)
        authorize(principal, DRAFT_EXPORT_CAPABILITY, draft.scope)
        target = str(draft.content["target"])
        document = draft.content["document"]
        if not isinstance(document, Mapping):
            raise DraftContentInvalid("草稿 document 必须是对象（形状门在创建/编辑已拦，此处防御）")
        base_text = _git_show_file(draft.base_git_sha, target)
        patch, base_document = _render_patch(base_text, document, target)
        return DraftPatchView(
            draft_id=draft.draft_id,
            revision=draft.revision,
            content_digest=draft.content_digest,
            base_git_sha=draft.base_git_sha,
            target=target,
            patch=patch,
            impact=_impact(base_document, document),
        )

    @staticmethod
    def _semantic_findings(draft: Draft) -> list[ValidationFinding]:
        """在临时文件上复跑三套 lint 校验器；草稿不落语义目录、校验器零改动。

        seen_names 用同目录**其他**模型播种（草稿是目标文件的下一版，只与其他
        活跃模型比 N8 同名）；策略一致性传全部同目录文件（只传草稿会把其他
        策略误报为孤儿）。
        """
        target = str(draft.content["target"])
        document = draft.content["document"]
        peers = [p for p in sorted(_OSSIE_DIR.glob("*.ossie.yaml")) if p.name != Path(target).name]
        structure: list[str] = []
        governance: list[str] = []
        policy: list[str] = []
        with tempfile.TemporaryDirectory(prefix="atlas-draft-validate-") as scratch:
            candidate = Path(scratch) / Path(target).name
            try:
                candidate.write_text(
                    yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
            except yaml.YAMLError as exc:
                return [
                    ValidationFinding(code="structure", message=f"草稿 document 无法序列化：{exc}")
                ]
            seen_names: dict[str, str] = {}
            for peer in peers:
                ossie_validate.validate_file(peer, seen_names)
            structure = ossie_validate.validate_file(candidate, seen_names)
            schema = json.loads(governance_validate.SCHEMA_PATH.read_text(encoding="utf-8"))
            governance_validate.validate_file(candidate, schema, governance)
            # 延迟导入：semantic 不设 serving 顶层依赖（与 semantic.lint 同纪律）
            from serving.auth import ROLE_DIRECTORY

            governance_validate.check_policy_consistency(
                governance_validate.collect_referenced_policies([*peers, candidate]),
                governance_validate.load_policies_by_name(),
                ROLE_DIRECTORY,
                policy,
            )
        return [
            ValidationFinding(code="structure", message=message) for message in structure
        ] + [
            ValidationFinding(code="governance", message=message) for message in governance
        ] + [ValidationFinding(code="policy", message=message) for message in policy]

    @staticmethod
    def _require_read(principal: Principal) -> None:
        if not _DRAFT_READ_CAPABILITIES & principal.capabilities:
            raise ControlForbidden(
                "控制能力不足：读取草稿需要 draft.edit/draft.review/draft.export"
            )
