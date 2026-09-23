"""ADR-0031 发布制品的内容身份与内存固定；不执行导入代码或自动发布。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent.compiler import SemanticModel
from agent.flows.contracts import FlowDefinition
from agent.planner import LocaleRules
from data.identity import REPO_ROOT, git_full_sha

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitSha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
TOOL_REGISTRY_VERSION = "atlas-builtin-v1"
MAX_FILE_BYTES = 1024 * 1024
MAX_BUNDLE_BYTES = 16 * MAX_FILE_BYTES
MAX_FILES = 512
# 仓库内可进入制品/草稿的配置路径：草稿形状门（T08a）与发布装载共用同一事实源。
REPO_CONFIG_PATTERNS = (
    r"semantic/ossie/[A-Za-z0-9_-]+\.ossie\.yaml",
    r"semantic/synonyms/(?:patterns_)?(?:zh_cn|en_us)\.yml",
    r"semantic/policies/[A-Za-z0-9_-]+\.yml",
    r"semantic/values/[A-Za-z0-9_.-]+\.json",
    r"agent/prompts/[A-Za-z0-9_-]+\.yaml",
    r"agent/flows/(?:templates|configs)/[A-Za-z0-9_-]+\.json",
    r"indexes/[A-Za-z0-9_-]+\.json",
)


class BundleError(ValueError):
    """制品内容、路径或运行时版本不兼容。"""


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _json_object(raw: bytes, label: str) -> Any:
    """解析 JSON 对象并拒绝重复键（标准库默认后值覆盖前值，会掩盖篡改）。"""

    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in pairs]
        if len(set(keys)) != len(keys):
            raise BundleError(f"{label} 含重复键")
        return dict(pairs)

    try:
        return json.loads(raw, object_pairs_hook=hook)
    except json.JSONDecodeError as exc:
        raise BundleError(f"{label} 不是合法 JSON") from exc


def _check_path(name: str) -> None:
    if not any(re.fullmatch(pattern, name) for pattern in REPO_CONFIG_PATTERNS):
        raise BundleError("制品含非白名单配置路径")


class ReleaseManifest(BaseModel):
    """D04 完整版本合同；身份覆盖所有字段，读取者不能自行声称效果门禁通过。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    release_id: Digest
    content_digest: Digest
    source_git_sha: GitSha
    runtime_code_sha: GitSha
    flow_digest: Digest
    semantic_digest: Digest
    rule_digest: Digest
    prompt_digests: dict[str, Digest]
    tool_registry_version: str = Field(min_length=1, max_length=128)
    model_versions: dict[str, str]
    source_revision: str = Field(min_length=1, max_length=128)
    index_digest: Digest
    eval_evidence_ids: tuple[str, ...]

    @model_validator(mode="after")
    def verify_identity(self) -> Self:
        """复算内容 ID；字段被篡改时拒绝构造。"""
        expected = _digest(self.model_dump(mode="json", exclude={"release_id", "content_digest"}))
        if self.release_id != expected or self.content_digest != expected:
            raise ValueError("发布内容摘要不匹配")
        return self


def runtime_code_sha() -> str:
    """返回本地完整代码身份；Git 不可用时异常向上传播，不猜测版本。"""
    return git_full_sha(REPO_ROOT)


def _group_digests(files: dict[str, bytes]) -> dict[str, Any]:
    if not files or len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_BUNDLE_BYTES:
        raise BundleError("制品为空或超过容量上限")
    hashes: dict[str, str] = {}
    for name, content in files.items():
        _check_path(name)
        if len(content) > MAX_FILE_BYTES:
            raise BundleError("制品文件超过容量上限")
        hashes[name] = hashlib.sha256(content).hexdigest()
    semantic = {
        k: v for k, v in hashes.items() if k.startswith("semantic/") and "/synonyms/" not in k
    }
    return {
        "semantic_digest": _digest(semantic),
        "rule_digest": _digest(
            {k: v for k, v in hashes.items() if k.startswith("semantic/synonyms/")}
        ),
        "flow_digest": _digest({k: v for k, v in hashes.items() if k.startswith("agent/flows/")}),
        "prompt_digests": {k: v for k, v in hashes.items() if k.startswith("agent/prompts/")},
        "index_digest": _digest({k: v for k, v in hashes.items() if k.startswith("indexes/")}),
    }


def build_manifest(
    files: dict[str, bytes],
    *,
    source_git_sha: str,
    runtime_code_sha: str,
    source_revision: str,
    eval_evidence_ids: tuple[str, ...],
) -> ReleaseManifest:
    """从配置字节计算 Manifest；不证明 Git 来源/评测通过，不写盘、不激活。

    上层导入服务必须验证显式 commit、审核与门禁；非法路径/内容抛 BundleError。
    当前只登记内置实现，不允许在制品里注册执行代码或模型后端。
    """
    body = {
        **_group_digests(files),
        "source_git_sha": source_git_sha,
        "runtime_code_sha": runtime_code_sha,
        "source_revision": source_revision,
        "tool_registry_version": TOOL_REGISTRY_VERSION,
        "model_versions": {},
        "eval_evidence_ids": eval_evidence_ids,
    }
    digest = _digest(body)
    return ReleaseManifest.model_validate({**body, "release_id": digest, "content_digest": digest})


def persist_bundle(root: Path, files: dict[str, bytes], manifest: ReleaseManifest) -> Path:
    """把已校验字节固化到内容寻址目录（`<root>/<release_id>/`）；不执行任何内容。

    同 ID 目录已存在时拒绝覆写（制品不可变）；调用方负责门禁与登记（HTTP 导入
    路径见 serving.control.releases，装载校验仍由 load_bundle 做）。

    Raises
    ------
    BundleError
        同 ID 制品已存在（内容寻址制品不可覆写）。
    """
    directory = root / manifest.release_id
    if directory.exists():
        raise BundleError("同 ID 制品已存在（内容寻址制品不可覆写）")
    directory.mkdir(parents=True)
    for name, content in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (directory / "manifest.json").write_text(manifest.model_dump_json())
    (directory / "files.json").write_text(
        json.dumps({name: hashlib.sha256(content).hexdigest() for name, content in files.items()})
    )
    return directory


@dataclass(frozen=True)
class RuntimeBundle:
    """已校验字节的固定副本；不保留 latest 指针或可变工作树读取接缝。"""

    _manifest_json: str
    _files: tuple[tuple[str, bytes], ...]

    @property
    def manifest(self) -> ReleaseManifest:
        """返回独立 Manifest 值对象，嵌套映射修改不会影响本运行。"""
        return ReleaseManifest.model_validate_json(self._manifest_json)

    def semantic_model(self, path: str) -> SemanticModel:
        """从固定配置构造独立 SemanticModel；缺文件/非法内容抛 BundleError。"""
        if not re.fullmatch(REPO_CONFIG_PATTERNS[0], path):
            raise BundleError("不是语义模型路径")
        try:
            raw = dict(self._files)[path]
            model = SemanticModel(doc=yaml.safe_load(raw))
            model.source_sha256 = hashlib.sha256(raw).hexdigest()
            return model
        except (KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
            raise BundleError("语义模型无法装配") from exc

    def locale_rules(self) -> LocaleRules:
        """从制品固定字节装配解析规则（ADR-0031 D04）；缺文件/非法内容抛 BundleError。

        同一内容命中内容寻址缓存（两次装载同一发布 → 同一对象）；不同发布
        互不串用。不读取工作树，也不写入进程默认规则单例。
        """
        files = dict(self._files)
        try:
            zh_patterns = files["semantic/synonyms/patterns_zh_cn.yml"]
            en_patterns = files["semantic/synonyms/patterns_en_us.yml"]
            zh_synonyms = files["semantic/synonyms/zh_cn.yml"]
            en_synonyms = files["semantic/synonyms/en_us.yml"]
        except KeyError as exc:
            raise BundleError("制品缺少解析规则文件") from exc
        try:
            return LocaleRules.from_documents(zh_patterns, en_patterns, zh_synonyms, en_synonyms)
        except ValueError as exc:
            raise BundleError("制品解析规则无法装配") from exc


def _read_file(root: Path, name: str) -> bytes:
    parts = PurePosixPath(name).parts
    if name.startswith("/") or any(part in {".", ".."} for part in parts):
        raise BundleError("非法制品路径")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise BundleError("制品路径不得含符号链接")
    if not path.is_file():
        raise BundleError("制品缺少普通文件")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise BundleError("制品文件超过容量上限")
    return raw


def _check_no_extra_files(directory: Path, declared: set[str]) -> None:
    """未在 files.json 声明的文件一律拒绝；声明清单是制品内容的唯一权威。"""
    allowed = declared | {"manifest.json", "files.json"}
    for path in directory.rglob("*"):
        relative = path.relative_to(directory).as_posix()
        if path.is_symlink() or (not path.is_dir() and relative not in allowed):
            raise BundleError("制品含未声明文件")


def _verify_flows(files: dict[str, bytes]) -> None:
    """默认流程必须存在且只声明已注册能力（未知节点/版本一律拒绝装配）。"""
    templates = [name for name in files if name.startswith("agent/flows/templates/")]
    if not templates:
        raise BundleError("制品缺少默认流程定义")
    for name in templates:
        try:
            FlowDefinition.model_validate(_json_object(files[name], name))
        except ValidationError as exc:
            raise BundleError(f"{name} 不是已注册的流程定义") from exc


def load_bundle(root: Path, release_id: str) -> RuntimeBundle:
    """按内容 ID 装配固定制品；校验路径/完整性/运行代码，失败抛 BundleError。

    容器代码身份与默认流程的完整装配仍由后续部署接线提供；本函数不做 Git 导入审核。
    """
    if not re.fullmatch(r"[0-9a-f]{64}", release_id):
        raise BundleError("release_id 必须是内容摘要")
    directory = root / release_id
    if root.is_symlink() or directory.is_symlink() or not directory.is_dir():
        raise BundleError("制品根目录不存在或是符号链接")
    try:
        manifest = ReleaseManifest.model_validate(
            _json_object(_read_file(directory, "manifest.json"), "manifest.json")
        )
        if manifest.release_id != release_id:
            raise BundleError("制品目录与 Manifest 身份不一致")
        if manifest.runtime_code_sha != runtime_code_sha():
            raise BundleError("运行代码版本不兼容")
        if manifest.tool_registry_version != TOOL_REGISTRY_VERSION or manifest.model_versions:
            raise BundleError("工具或模型注册版本不兼容")
        hashes = _json_object(_read_file(directory, "files.json"), "files.json")
        if not isinstance(hashes, dict) or not hashes or len(hashes) > MAX_FILES:
            raise BundleError("文件清单无效")
        files: dict[str, bytes] = {}
        total = 0
        for name, digest in hashes.items():
            _check_path(name)
            content = _read_file(directory, name)
            total += len(content)
            if total > MAX_BUNDLE_BYTES or hashlib.sha256(content).hexdigest() != digest:
                raise BundleError("制品文件摘要不匹配或超过容量上限")
            files[name] = content
        groups = _group_digests(files)
        if any(getattr(manifest, key) != value for key, value in groups.items()):
            raise BundleError("Manifest 与文件清单不一致")
        _check_no_extra_files(directory, set(files))
        _verify_flows(files)
        return RuntimeBundle(manifest.model_dump_json(), tuple(sorted(files.items())))
    except (OSError, ValidationError, json.JSONDecodeError) as exc:
        raise BundleError("无法加载完整制品") from exc
