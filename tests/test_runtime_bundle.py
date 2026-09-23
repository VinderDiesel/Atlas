"""T02 不可变制品：真实文件与已提交语义样例；不冒充发布审核或效果评测。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.workbench_support import REPO_ROOT, bundle_files, write_bundle


def test_bundle_pins_content_and_rejects_changed_file(tmp_path: Path) -> None:
    from agent.runtime.bundle import BundleError, load_bundle

    files = bundle_files()
    release_id = write_bundle(tmp_path, files)
    bundle = load_bundle(tmp_path, release_id)
    model = bundle.semantic_model("semantic/ossie/atlas_finance.ossie.yaml")
    assert "commission_revenue" in model.metrics
    path = tmp_path / release_id / "semantic/ossie/atlas_finance.ossie.yaml"
    path.write_text("tampered")
    assert (
        "commission_revenue"
        in bundle.semantic_model("semantic/ossie/atlas_finance.ossie.yaml").metrics
    )
    with pytest.raises(BundleError):
        load_bundle(tmp_path, release_id)


@pytest.mark.parametrize(
    "name", ["../escape.py", "/tmp/code.py", "agent/evil.py", "semantic/ossie/x.py"]
)
def test_bundle_rejects_non_configuration_paths(tmp_path: Path, name: str) -> None:
    from agent.runtime.bundle import BundleError, build_manifest, runtime_code_sha

    files = bundle_files()
    files[name] = b"raise RuntimeError('must never execute')"
    with pytest.raises(BundleError):
        build_manifest(
            files,
            source_git_sha=runtime_code_sha(),
            runtime_code_sha=runtime_code_sha(),
            source_revision="1",
            eval_evidence_ids=(),
        )


def test_bundle_rejects_missing_file_and_links(tmp_path: Path) -> None:
    from agent.runtime.bundle import BundleError, load_bundle

    files = bundle_files()
    release_id = write_bundle(tmp_path, files)
    path = tmp_path / release_id / "agent/prompts/generator_plan.yaml"
    moved = tmp_path / "outside.yaml"
    path.rename(moved)
    with pytest.raises(BundleError):
        load_bundle(tmp_path, release_id)
    path.symlink_to(moved)
    with pytest.raises(BundleError):
        load_bundle(tmp_path, release_id)


@pytest.mark.parametrize(
    "field,value",
    [("runtime_code_sha", "a" * 40), ("tool_registry_version", "unknown")],
)
def test_incompatible_manifest_rejected_even_with_correct_digest(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    from agent.runtime.bundle import BundleError, load_bundle

    release_id = write_bundle(tmp_path, bundle_files())
    directory = tmp_path / release_id
    manifest_path = directory / "manifest.json"
    doc = json.loads(manifest_path.read_text())
    doc[field] = value
    body = {k: v for k, v in doc.items() if k not in {"release_id", "content_digest"}}
    digest = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    doc.update(release_id=digest, content_digest=digest)
    manifest_path.write_text(json.dumps(doc))
    directory.rename(tmp_path / digest)
    with pytest.raises(BundleError):
        load_bundle(tmp_path, digest)


def test_manifest_cannot_be_mutated_through_runtime_bundle(tmp_path: Path) -> None:
    from agent.runtime.bundle import load_bundle

    release_id = write_bundle(tmp_path, bundle_files())
    bundle = load_bundle(tmp_path, release_id)
    manifest = bundle.manifest
    manifest.prompt_digests.clear()
    assert bundle.manifest.prompt_digests
    model = bundle.semantic_model("semantic/ossie/atlas_finance.ossie.yaml")
    model.metrics.clear()
    assert bundle.semantic_model("semantic/ossie/atlas_finance.ossie.yaml").metrics


def test_bundle_rejects_undeclared_extra_files(tmp_path: Path) -> None:
    from agent.runtime.bundle import BundleError, load_bundle

    release_id = write_bundle(tmp_path, bundle_files())
    directory = tmp_path / release_id
    # 白名单形态但未声明的文件同样拒绝：声明清单是唯一权威
    (directory / "semantic/ossie/extra.ossie.yaml").write_bytes(b"extra: 1")
    with pytest.raises(BundleError):
        load_bundle(tmp_path, release_id)
    (directory / "agent/evil.py").write_text("print('never runs')")
    with pytest.raises(BundleError):
        load_bundle(tmp_path, release_id)


def _with_duplicate_first_key(raw: bytes) -> bytes:
    """在 JSON 文本首位复制首个键值对：语义等价但含重复键。"""
    doc = json.loads(raw)
    key, value = next(iter(doc.items()))
    head = json.dumps({key: value}, ensure_ascii=False, separators=(",", ":"))[:-1]
    return (head + "," + raw.decode("utf-8")[1:]).encode("utf-8")


def test_bundle_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    from agent.runtime.bundle import BundleError, load_bundle

    release_id = write_bundle(tmp_path, bundle_files())
    directory = tmp_path / release_id
    for name in ("manifest.json", "files.json", "agent/flows/templates/query.json"):
        path = directory / name
        original = path.read_bytes()
        path.write_bytes(_with_duplicate_first_key(original))
        with pytest.raises(BundleError):
            load_bundle(tmp_path, release_id)
        path.write_bytes(original)


def test_bundle_without_default_flow_templates_is_rejected(tmp_path: Path) -> None:
    from agent.runtime.bundle import BundleError, load_bundle

    files = {
        name: raw for name, raw in bundle_files().items() if not name.startswith("agent/flows/")
    }
    release_id = write_bundle(tmp_path, files)
    with pytest.raises(BundleError):
        load_bundle(tmp_path, release_id)


def test_bundle_with_unregistered_flow_node_is_rejected(tmp_path: Path) -> None:
    from agent.runtime.bundle import BundleError, load_bundle

    name = "agent/flows/templates/query.json"
    doc = json.loads((REPO_ROOT / name).read_bytes())
    doc["nodes"][0]["node_type"] = "understand"  # D09 未实现：未注册能力不得装配
    files = {**bundle_files(), name: json.dumps(doc).encode()}
    release_id = write_bundle(tmp_path, files)
    with pytest.raises(BundleError):
        load_bundle(tmp_path, release_id)


def test_default_bundle_packages_default_flow_templates(tmp_path: Path) -> None:
    from agent.runtime.bundle import load_bundle

    files = bundle_files()
    expected = {"agent/flows/templates/query.json", "agent/flows/templates/analysis.json"}
    assert expected <= set(files)
    bundle = load_bundle(tmp_path, write_bundle(tmp_path, files))
    assert bundle.manifest.flow_digest != hashlib.sha256(b"{}").hexdigest()
