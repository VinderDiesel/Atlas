"""T08b 发布导入与 CAS 激活：制品门禁与部署指针切换（ADR-0031 D04/D13）。

红→绿纪律（TDD）：用例先于实现提交。装配复用 `WorkbenchHarness`（真实路由/
合同/控制库）；发布源是**隔离 tmp Git 仓库**的显式 commit——产品仓库的工作树/
索引/引用零接触（D04：服务不自动 commit/push/merge，也不读脏工作树当发布源）。

断言面（账本 T08 绿测逐字）：同名 active、未知关系/策略/方言、值域错误、
审核后篡改、路径穿越、符号链接、非白名单/超大文件、导入代码、运行中发布/回退。
未知关系/方言由 T08a-s2 的 validate 用例覆盖（同一校验器、同一代码路径）；
本文件覆盖制品整体门禁（策略丢失、同名 active、值域锁定）与制品完整性。

诚实边界（N2）：本批覆盖 import/publish/rollback 与制品门禁；草稿推进止于
release_ready（published/retired 的推进不在本批）。published 之前的"审核后
篡改"由 T08a-s2 的编辑失效用例与本文的导入内容一致性用例共同覆盖。
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent.runtime.bundle import BundleError, load_bundle
from serving.control.contracts import Owner
from tests.workbench_support import (
    DEPLOYMENTS_PATH,
    DRAFTS_PATH,
    RELEASE_IMPORTS_PATH,
    RELEASES_PATH,
    REPO_ROOT,
    RUNS_PATH,
    SOURCES_PATH,
    WorkbenchHarness,
    bundle_files,
    write_bundle,
)

SOURCE_ID = "doris-primary"
DEPLOYMENT_ID = "finance-live"
# 目标语义模型（draft_fixture 默认域）：导入夹具的目标路径。
TARGET = "semantic/ossie/atlas_finance.ossie.yaml"

_IMPORT_KEYS = {
    "release_id",
    "content_digest",
    "draft_id",
    "draft_revision",
    "status",
    "target",
    "created_at",
}
_ACTIVATION_KEYS = {
    "deployment_id",
    "action",
    "previous_release_id",
    "active_release_id",
    "revision",
    "updated_at",
}
_RELEASE_KEYS = {
    "release_id",
    "content_digest",
    "scope",
    "source_id",
    "source_revision",
    "manifest",
    "created_by",
    "created_at",
}
_MANIFEST_KEYS = {
    "release_id",
    "content_digest",
    "source_git_sha",
    "runtime_code_sha",
    "flow_digest",
    "semantic_digest",
    "rule_digest",
    "prompt_digests",
    "tool_registry_version",
    "model_versions",
    "source_revision",
    "index_digest",
    "eval_evidence_ids",
}


@pytest.fixture()
def h(tmp_path: Path) -> Iterator[WorkbenchHarness]:
    with WorkbenchHarness(tmp_path) as harness:
        yield harness


# ---------------------------------------------------------------------------
# 装配辅助：源 + 探测证据 + 部署 + 已审核草稿 + 隔离 Git 提交
# ---------------------------------------------------------------------------


def _prepare_source(h: WorkbenchHarness) -> None:
    """登记只读源修订（import 的 source_id 必须已配置）。"""
    response = h.request("POST", SOURCES_PATH, actor="operator", json=h.source_fixture())
    assert response.status_code == 201, response.text


def _probe_source(h: WorkbenchHarness, *, read_only: bool = True) -> None:
    """受限探测一行证据；read_only=False 时驱动只读未确认（blocked）。

    观察值每次都显式重置：重探场景（阻塞 → 确认只读）必须覆盖上一次观察。
    """
    h.probe_spy.observation = h.probe_observation(**({} if read_only else {"grant_statements": ()}))
    response = h.request("POST", f"{SOURCES_PATH}/{SOURCE_ID}/probes", actor="operator")
    assert response.status_code == 200, response.text


def _prepare_deployment(h: WorkbenchHarness) -> None:
    response = h.request("POST", DEPLOYMENTS_PATH, actor="operator", json=h.deployment_fixture())
    assert response.status_code == 201, response.text


def _reviewed_draft(
    h: WorkbenchHarness, *, document: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any]]:
    """完整草稿管线：创建 → 校验 → 审核通过（reviewed）；返回 (draft_id, document)。

    `document` 给定时以它为草稿内容（变体发布用例：内容变 → 制品 ID 变）。
    """
    body = h.draft_fixture("semantic")
    if document is not None:
        body["content"] = {"target": TARGET, "document": document}
    created = h.request("POST", DRAFTS_PATH, actor="editor", json=body)
    assert created.status_code == 201, created.text
    draft_id = created.json()["draft_id"]
    validated = h.request("POST", f"{DRAFTS_PATH}/{draft_id}/validations", actor="editor")
    assert validated.status_code == 201, validated.text
    review = h.request(
        "POST",
        f"{DRAFTS_PATH}/{draft_id}/reviews",
        actor="reviewer",
        json={"decision": "approved"},
    )
    assert review.status_code == 201, review.text
    return str(draft_id), dict(created.json()["content"]["document"])


def _commit_draft(
    h: WorkbenchHarness,
    *,
    document: dict[str, Any],
    replace: dict[str, bytes] | None = None,
    symlinks: dict[str, str] | None = None,
    target: str = TARGET,
) -> str:
    """在隔离仓库提交发布夹具（目标文件 = 草稿 document）；返回完整 commit sha。"""
    files = h.release_files(target=target, document=document, replace=replace)
    return h.commit_release_tree(files, symlinks=symlinks)


def _import(
    h: WorkbenchHarness,
    draft_id: str,
    source_git_sha: str,
    *,
    actor: str = "publisher",
    source_id: str = SOURCE_ID,
):
    return h.request(
        "POST",
        RELEASE_IMPORTS_PATH,
        actor=actor,
        json={
            "draft_id": draft_id,
            "source_git_sha": source_git_sha,
            "source_id": source_id,
        },
    )


def _publish(
    h: WorkbenchHarness,
    release_id: str,
    *,
    expected: str | None = None,
    actor: str = "publisher",
    deployment_id: str = DEPLOYMENT_ID,
):
    return h.request(
        "POST",
        f"{DEPLOYMENTS_PATH}/{deployment_id}/releases",
        actor=actor,
        json={"release_id": release_id, "expected_active_release_id": expected},
    )


def _rollback(
    h: WorkbenchHarness,
    release_id: str,
    *,
    expected: str | None = None,
    actor: str = "publisher",
    deployment_id: str = DEPLOYMENT_ID,
):
    return h.request(
        "POST",
        f"{DEPLOYMENTS_PATH}/{deployment_id}/rollbacks",
        actor=actor,
        json={"release_id": release_id, "expected_active_release_id": expected},
    )


def _document(h: WorkbenchHarness) -> dict[str, Any]:
    """草稿夹具的语义模型文档（HEAD 原文）：变体与对照制品的基线。"""
    return dict(h.draft_fixture("semantic")["content"]["document"])


def _variant(document: dict[str, Any], marker: str) -> dict[str, Any]:
    """内容变体：只改一处 metric 描述——结构不变，制品 ID 随内容变化。"""
    changed = copy.deepcopy(document)
    model = changed["semantic_model"][0]
    metrics = [dict(metric) for metric in model["metrics"]]
    metrics[0]["description"] = f"{metrics[0].get('description', '')}（{marker}）"
    model["metrics"] = metrics
    return changed


def _import_release(h: WorkbenchHarness, *, marker: str | None = None) -> str:
    """草稿→审核→隔离仓库提交→导入；返回 release_id（不发布）。

    marker=None 用 HEAD 原文（首发）；给定时用变体（切换/回退用例的第二制品）。
    """
    document = _document(h)
    if marker is not None:
        document = _variant(document, marker)
    draft_id, reviewed = _reviewed_draft(h, document=document)
    sha = _commit_draft(h, document=reviewed)
    imported = _import(h, draft_id, sha)
    assert imported.status_code == 201, imported.text
    return str(imported.json()["release_id"])


def _released(h: WorkbenchHarness, *, marker: str | None = None) -> str:
    """全链路（源→探测→部署→草稿→审核→导入→发布）；返回 release_id。"""
    _prepare_source(h)
    _probe_source(h)
    _prepare_deployment(h)
    release_id = _import_release(h, marker=marker)
    published = _publish(h, release_id, expected=None)
    assert published.status_code == 200, published.text
    return release_id


def _register_fixture(h: WorkbenchHarness, *, scope: str, source_id: str) -> str:
    """store 原语登记对照制品（不激活）：域门/读面用例的对照数据。"""
    release_id = write_bundle(
        h.bundles_root,
        h.release_files(target=TARGET, document=_document(h)),
        revision=f"{scope}:{source_id}",
    )
    h.control_store.register_release(
        load_bundle(h.bundles_root, release_id),
        owner=Owner(issuer="atlas-local", subject="publisher"),
        scope=scope,
        source_id=source_id,
    )
    return release_id


def _release_rows(h: WorkbenchHarness, *, actor: str = "publisher") -> list[dict[str, Any]]:
    response = h.request("GET", RELEASES_PATH, actor=actor)
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


def _release_ids(h: WorkbenchHarness, *, actor: str = "publisher") -> list[str]:
    return [row["release_id"] for row in _release_rows(h, actor=actor)]


# ---------------------------------------------------------------------------
# A. 导入：生命周期与内容门禁
# ---------------------------------------------------------------------------


def test_import_requires_reviewed_draft(h: WorkbenchHarness) -> None:
    """只接受 reviewed 草稿：draft/validated 一律 409，且零制品登记。"""
    _prepare_source(h)
    draft_id, document = (lambda r: (r.json()["draft_id"], r.json()["content"]["document"]))(
        h.request("POST", DRAFTS_PATH, actor="editor", json=h.draft_fixture("semantic"))
    )
    sha = _commit_draft(h, document=document)
    unvalidated = _import(h, draft_id, sha)
    assert unvalidated.status_code == 409, unvalidated.text
    assert unvalidated.json()["error"]["code"] == "revision_conflict"
    validated = h.request("POST", f"{DRAFTS_PATH}/{draft_id}/validations", actor="editor")
    assert validated.status_code == 201, validated.text
    only_validated = _import(h, draft_id, sha)
    assert only_validated.status_code == 409, only_validated.text
    assert _release_ids(h) == []
    assert (
        h.request("GET", f"{DRAFTS_PATH}/{draft_id}", actor="editor").json()["status"]
        == "validated"
    )


def test_import_advances_draft_to_release_ready_and_registers_release(
    h: WorkbenchHarness,
) -> None:
    """导入成功：201 固定 7 键 → 草稿 release_ready → 制品写盘可装载 → 列表可见。"""
    _prepare_source(h)
    _prepare_deployment(h)
    draft_id, document = _reviewed_draft(h)
    sha = _commit_draft(h, document=document)
    response = _import(h, draft_id, sha, actor="publisher")
    assert response.status_code == 201, response.text
    view = response.json()
    assert set(view) == _IMPORT_KEYS
    assert view["draft_id"] == draft_id
    assert view["status"] == "release_ready"
    assert view["target"] == TARGET
    release_id = view["release_id"]
    assert len(release_id) == 64
    assert view["content_digest"] == release_id
    current = h.request("GET", f"{DRAFTS_PATH}/{draft_id}", actor="editor")
    assert current.json()["status"] == "release_ready"
    assert release_id in _release_ids(h)
    # 制品必须可被运行时装载（门禁的实体：写盘的制品通过 load_bundle 全检）
    bundle = load_bundle(h.bundles_root, release_id)
    assert bundle.manifest.release_id == release_id
    assert bundle.manifest.source_git_sha == sha
    # 制品文件清单 = commit 里命中白名单的文件集（白名单外零收集）
    manifest_files = json.loads((h.bundles_root / release_id / "files.json").read_text())
    assert set(manifest_files) == set(bundle_files())


def test_import_rejects_content_mismatch_with_reviewed_draft(h: WorkbenchHarness) -> None:
    """审核后篡改（导入侧）：commit 内容 ≠ 已审核草稿 → 409，零登记、状态不变。"""
    _prepare_source(h)
    _prepare_deployment(h)
    draft_id, document = _reviewed_draft(h)
    tampered = dict(document)
    model = dict(tampered["semantic_model"][0])
    metrics = [dict(metric) for metric in model["metrics"]]
    metrics[0]["description"] = "已审核后又改动的口径"
    model["metrics"] = metrics
    tampered["semantic_model"] = [model]
    sha = _commit_draft(h, document=tampered)
    response = _import(h, draft_id, sha)
    assert response.status_code == 409, response.text
    assert _release_ids(h) == []
    assert (
        h.request("GET", f"{DRAFTS_PATH}/{draft_id}", actor="editor").json()["status"] == "reviewed"
    )
    # 零制品落盘：bundles 根下没有该内容的目录
    if h.bundles_root.exists():
        assert [p.name for p in h.bundles_root.iterdir()] == []


def test_import_rejects_malformed_or_unreachable_commit(h: WorkbenchHarness) -> None:
    """显式 commit 必须是完整 40 hex 且对象库可达；短 sha/未知 sha 一律 422。"""
    _prepare_source(h)
    _prepare_deployment(h)
    draft_id, document = _reviewed_draft(h)
    good = _commit_draft(h, document=document)
    cases = [good[:12], "f" * 40, "not-a-sha"]
    for value in cases:
        response = _import(h, draft_id, value)
        assert response.status_code == 422, (value, response.text)
    assert _release_ids(h) == []
    # 校验失败的提交不影响后续合法导入（同一草稿仍 reviewed）
    assert _import(h, draft_id, good).status_code == 201


def test_import_gates_capability_scope_and_draft_existence(h: WorkbenchHarness) -> None:
    """能力门（viewer/editor/reviewer/operator）、域门（publisher_retail）、未知草稿 404。"""
    _prepare_source(h)
    _prepare_deployment(h)
    draft_id, document = _reviewed_draft(h)
    sha = _commit_draft(h, document=document)
    for actor in ("viewer", "editor", "reviewer", "operator"):
        response = _import(h, draft_id, sha, actor=actor)
        assert response.status_code == 403, (actor, response.text)
    domain = _import(h, draft_id, sha, actor="publisher_retail")
    assert domain.status_code == 403, domain.text
    assert _import(h, "no-such-draft", sha).status_code == 404
    assert _release_ids(h) == []
    assert _import(h, draft_id, sha).status_code == 201


def test_import_rejects_missing_target_and_symlink_paths(h: WorkbenchHarness) -> None:
    """commit 缺目标文件或白名单路径是符号链接 → 422；随后合法导入仍成功。"""
    _prepare_source(h)
    _prepare_deployment(h)
    draft_id, document = _reviewed_draft(h)
    without_target = {k: v for k, v in bundle_files().items() if k != TARGET}
    missing_sha = h.commit_release_tree(without_target)
    missing = _import(h, draft_id, missing_sha)
    assert missing.status_code == 422, missing.text
    symlink_sha = _commit_draft(
        h,
        document=document,
        symlinks={"semantic/policies/row_policy.yml": "../../etc/passwd"},
    )
    linked = _import(h, draft_id, symlink_sha)
    assert linked.status_code == 422, linked.text
    assert "符号链接" in linked.text
    assert _import(h, draft_id, _commit_draft(h, document=document)).status_code == 201


def test_import_collects_only_whitelisted_files(h: WorkbenchHarness) -> None:
    """导入代码：非白名单/代码文件不进制品；制品被塞入未声明文件时装载拒绝。"""
    _prepare_source(h)
    _prepare_deployment(h)
    draft_id, document = _reviewed_draft(h)
    sha = _commit_draft(
        h,
        document=document,
        replace={
            "agent/evil.py": b"print('pwn')\n",
            "semantic/ossie/hack.py": b"import os\n",
        },
    )
    response = _import(h, draft_id, sha)
    assert response.status_code == 201, response.text
    release_id = response.json()["release_id"]
    declared = set(json.loads((h.bundles_root / release_id / "files.json").read_text()))
    assert declared == set(bundle_files())
    assert "agent/evil.py" not in declared
    assert "semantic/ossie/hack.py" not in declared
    # 纵深防御：制品目录被注入未声明代码文件 → 装载拒绝（完整性清单唯一权威）
    injected = h.bundles_root / release_id / "agent" / "evil.py"
    injected.parent.mkdir(parents=True, exist_ok=True)
    injected.write_text("print('pwn')\n")
    with pytest.raises(BundleError):
        load_bundle(h.bundles_root, release_id)


def test_import_rejects_oversized_whitelisted_file(h: WorkbenchHarness) -> None:
    """压缩炸弹/超大文件：白名单路径上超过单文件上限 → 422，零登记。"""
    _prepare_source(h)
    _prepare_deployment(h)
    draft_id, document = _reviewed_draft(h)
    oversized = b"[" + b"0," * 700_000 + b"0]"  # > 1 MiB
    sha = _commit_draft(
        h,
        document=document,
        replace={"semantic/values/atlas_finance_analytics.Big.json": oversized},
    )
    response = _import(h, draft_id, sha)
    assert response.status_code == 422, response.text
    assert _release_ids(h) == []


def test_import_gates_whole_bundle_not_only_target(h: WorkbenchHarness) -> None:
    """发布检查的是制品整体：目标文件合法但同制品内策略丢失 → 422。"""
    _prepare_source(h)
    _prepare_deployment(h)
    draft_id, document = _reviewed_draft(h)
    sha = _commit_draft(h, document=document, replace={"semantic/policies/row_policy.yml": b"{}\n"})
    response = _import(h, draft_id, sha)
    assert response.status_code == 422, response.text
    assert "策略" in response.text
    assert _release_ids(h) == []
    assert (
        h.request("GET", f"{DRAFTS_PATH}/{draft_id}", actor="editor").json()["status"] == "reviewed"
    )


def test_import_rejects_duplicate_metric_across_bundle(h: WorkbenchHarness) -> None:
    """同名 active（N8）：制品内第二个模型与目标模型指标重名 → 422。"""
    _prepare_source(h)
    _prepare_deployment(h)
    draft_id, document = _reviewed_draft(h)
    sha = _commit_draft(
        h,
        document=document,
        replace={
            "semantic/ossie/atlas_dup.ossie.yaml": h.release_files(
                target=TARGET, document=document
            )[TARGET]
        },
    )
    response = _import(h, draft_id, sha)
    assert response.status_code == 422, response.text
    assert "重名" in response.text
    assert _release_ids(h) == []


def test_import_rejects_broken_value_profile(h: WorkbenchHarness) -> None:
    """值域错误：制品内值域快照的字段不在模型 dim_* 数据集 → 422（ADR-0016 锁定）。"""
    _prepare_source(h)
    _prepare_deployment(h)
    draft_id, document = _reviewed_draft(h)
    source = REPO_ROOT / "semantic" / "values" / "atlas_finance_analytics.Branch.json"
    profile = json.loads(source.read_text(encoding="utf-8"))
    profile["field"] = "BogusDimension"
    blob = json.dumps(profile, ensure_ascii=False).encode("utf-8")
    sha = _commit_draft(
        h,
        document=document,
        replace={"semantic/values/atlas_finance_analytics.BogusDimension.json": blob},
    )
    response = _import(h, draft_id, sha)
    assert response.status_code == 422, response.text
    assert "BogusDimension" in response.text
    assert _release_ids(h) == []


# ---------------------------------------------------------------------------
# B. 发布：部署指针 CAS 与证据门禁
# ---------------------------------------------------------------------------


def test_publish_switches_pointer_with_cas(h: WorkbenchHarness) -> None:
    """首发 expected=null → revision 2；再发 expected=A → revision 3（指针原子切换）。"""
    _prepare_source(h)
    _probe_source(h)
    _prepare_deployment(h)
    first = _import_release(h)
    initial = _publish(h, first, expected=None)
    assert initial.status_code == 200, initial.text
    view = initial.json()
    assert set(view) == _ACTIVATION_KEYS
    assert view["deployment_id"] == DEPLOYMENT_ID
    assert view["action"] == "publish"
    assert view["previous_release_id"] is None
    assert view["active_release_id"] == first
    assert view["revision"] == 2
    second = _import_release(h, marker="second")
    assert second != first  # 内容变 → 制品 ID 变
    switched = _publish(h, second, expected=first)
    assert switched.status_code == 200, switched.text
    assert set(switched.json()) == _ACTIVATION_KEYS
    assert switched.json()["previous_release_id"] == first
    assert switched.json()["active_release_id"] == second
    assert switched.json()["revision"] == 3
    current = h.request("GET", f"{DEPLOYMENTS_PATH}/{DEPLOYMENT_ID}", actor="operator").json()
    assert current["active_release_id"] == second
    assert current["revision"] == 3


def test_publish_rejects_stale_expected_pointer(h: WorkbenchHarness) -> None:
    """陈旧 expected（null 或旧 ID）一律 409，指针与修订不变（不静默覆盖）。"""
    first = _released(h)  # 指针=A（revision 2）
    second = _import_release(h, marker="second")
    stale_null = _publish(h, second, expected=None)
    assert stale_null.status_code == 409, stale_null.text
    assert stale_null.json()["error"]["code"] == "release_conflict"
    assert _publish(h, second, expected=first).status_code == 200
    stale_old = _publish(h, first, expected=first)  # 指针已是 second
    assert stale_old.status_code == 409, stale_old.text
    assert stale_old.json()["error"]["code"] == "release_conflict"
    current = h.request("GET", f"{DEPLOYMENTS_PATH}/{DEPLOYMENT_ID}", actor="operator").json()
    assert current["active_release_id"] == second
    assert current["revision"] == 3


def test_publish_rejects_unregistered_and_foreign_release(h: WorkbenchHarness) -> None:
    """未登记 64 hex → 404；已登记但 (scope, source) 与部署不匹配 → 409。"""
    first = _released(h)
    unknown = _publish(h, "f" * 64, expected=first)
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["error"]["code"] == "not_found"
    foreign_scope = _register_fixture(h, scope="retail", source_id=SOURCE_ID)
    mismatch_scope = _publish(h, foreign_scope, expected=first)
    assert mismatch_scope.status_code == 409, mismatch_scope.text
    assert mismatch_scope.json()["error"]["code"] == "release_conflict"
    foreign_source = _register_fixture(h, scope="finance", source_id="legacy-warehouse")
    mismatch_source = _publish(h, foreign_source, expected=first)
    assert mismatch_source.status_code == 409, mismatch_source.text
    assert mismatch_source.json()["error"]["code"] == "release_conflict"
    current = h.request("GET", f"{DEPLOYMENTS_PATH}/{DEPLOYMENT_ID}", actor="operator").json()
    assert current["active_release_id"] == first
    assert current["revision"] == 2


def test_publish_gates_capability_scope_and_deployment_existence(
    h: WorkbenchHarness,
) -> None:
    """无发布能力（含 operator）403、跨域 publisher 403、未知部署 404、正例 200。"""
    _prepare_source(h)
    _probe_source(h)
    _prepare_deployment(h)
    release_id = _import_release(h)
    for actor in ("viewer", "operator", "editor", "reviewer"):
        response = _publish(h, release_id, actor=actor)
        assert response.status_code == 403, (actor, response.text)
    assert _publish(h, release_id, actor="publisher_retail").status_code == 403
    assert _publish(h, release_id, deployment_id="no-such-deployment").status_code == 404
    current = h.request("GET", f"{DEPLOYMENTS_PATH}/{DEPLOYMENT_ID}", actor="operator").json()
    assert current["active_release_id"] is None  # 门禁拒绝零副作用
    assert _publish(h, release_id, expected=None).status_code == 200


def test_publish_requires_current_source_evidence(h: WorkbenchHarness) -> None:
    """证据门禁（D13）：无探测 / 只读未确认 → 409；重新确认只读后可发布。"""
    _prepare_source(h)
    _prepare_deployment(h)
    release_id = _import_release(h)
    missing = _publish(h, release_id, expected=None)
    assert missing.status_code == 409, missing.text
    assert missing.json()["error"]["code"] == "evidence_required"
    _probe_source(h, read_only=False)
    blocked = _publish(h, release_id, expected=None)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "evidence_required"
    _probe_source(h)  # 重探：本次观察覆盖上次阻塞（证据是时间戳事实）
    assert _publish(h, release_id, expected=None).status_code == 200
    current = h.request("GET", f"{DEPLOYMENTS_PATH}/{DEPLOYMENT_ID}", actor="operator").json()
    assert current["active_release_id"] == release_id


# ---------------------------------------------------------------------------
# C. 回退：指针向后切换与同等门禁
# ---------------------------------------------------------------------------


def test_rollback_switches_back_with_cas(h: WorkbenchHarness) -> None:
    """A→B→回退 A（expected=B）→ revision 4；陈旧 expected 409 且指针不变。"""
    first = _released(h)
    second = _import_release(h, marker="second")
    assert _publish(h, second, expected=first).status_code == 200
    reverted = _rollback(h, first, expected=second)
    assert reverted.status_code == 200, reverted.text
    view = reverted.json()
    assert set(view) == _ACTIVATION_KEYS
    assert view["action"] == "rollback"
    assert view["previous_release_id"] == second
    assert view["active_release_id"] == first
    assert view["revision"] == 4
    stale = _rollback(h, second, expected=second)  # 指针已是 first
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "release_conflict"
    current = h.request("GET", f"{DEPLOYMENTS_PATH}/{DEPLOYMENT_ID}", actor="operator").json()
    assert current["active_release_id"] == first
    assert current["revision"] == 4


def test_rollback_gates_capability_registration_and_evidence(
    h: WorkbenchHarness,
) -> None:
    """回退同样受能力/域/登记/证据门禁（D04“也需”）：403、404、409。"""
    first = _released(h)
    second = _import_release(h, marker="second")
    assert _publish(h, second, expected=first).status_code == 200
    assert _rollback(h, first, actor="editor").status_code == 403
    assert _rollback(h, first, actor="publisher_retail").status_code == 403
    assert _rollback(h, "f" * 64, expected=second).status_code == 404
    _probe_source(h, read_only=False)  # 新的阻塞证据：只读不再确认
    blocked = _rollback(h, first, expected=second)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "evidence_required"
    _probe_source(h)  # 重探确认只读
    assert _rollback(h, first, expected=second).status_code == 200
    current = h.request("GET", f"{DEPLOYMENTS_PATH}/{DEPLOYMENT_ID}", actor="operator").json()
    assert current["active_release_id"] == first


# ---------------------------------------------------------------------------
# D. 运行中切换：已运行实例固定接受时的制品
# ---------------------------------------------------------------------------


def test_running_instance_keeps_release_on_switch(h: WorkbenchHarness) -> None:
    """已执行实例固定接受时的 release（D04）；新提交跟随新指针。"""
    first = _released(h)
    run = h.submit_query("2013 年第二季度总交易额", deployment_id=DEPLOYMENT_ID)
    h.drain()
    second = _import_release(h, marker="second")
    assert _publish(h, second, expected=first).status_code == 200
    kept = h.request("GET", f"{RUNS_PATH}/{run}", actor="viewer")
    assert kept.status_code == 200, kept.text
    assert kept.json()["release_id"] == first
    assert kept.json()["status"] == "succeeded"
    fresh = h.submit_query("2013 年第二季度总交易额", deployment_id=DEPLOYMENT_ID)
    h.drain()
    after = h.request("GET", f"{RUNS_PATH}/{fresh}", actor="viewer")
    assert after.status_code == 200, after.text
    assert after.json()["release_id"] == second


# ---------------------------------------------------------------------------
# E. 读取面：域裁剪、固定键与脱敏
# ---------------------------------------------------------------------------


def test_release_reads_are_scope_scoped_and_desensitized(
    h: WorkbenchHarness,
) -> None:
    """发布历史与详情（D13）：域裁剪 + 固定键；秘密/模型正文不回显。"""
    finance = _released(h)
    retail = _register_fixture(h, scope="retail", source_id="retail-doris")
    for actor in ("viewer", "editor", "reviewer"):
        assert h.request("GET", RELEASES_PATH, actor=actor).status_code == 403
    finance_rows = _release_rows(h, actor="publisher")
    assert [row["release_id"] for row in finance_rows] == [finance]
    assert set(finance_rows[0]) == _RELEASE_KEYS
    assert [row["release_id"] for row in _release_rows(h, actor="operator")] == [finance]
    assert [row["release_id"] for row in _release_rows(h, actor="publisher_retail")] == [retail]
    assert [row["release_id"] for row in _release_rows(h, actor="ops_retail")] == [retail]
    detail = h.request("GET", f"{RELEASES_PATH}/{finance}", actor="publisher")
    assert detail.status_code == 200, detail.text
    view = detail.json()
    assert set(view) == _RELEASE_KEYS
    assert set(view["manifest"]) == _MANIFEST_KEYS
    assert view["scope"] == "finance"
    assert view["source_revision"] == "rev-2026-09-22-1"
    assert "sup3r-secret-pw" not in detail.text
    assert "metrics" not in detail.text  # 脱敏详情不含模型正文
    assert h.request("GET", f"{RELEASES_PATH}/{retail}", actor="publisher").status_code == 403
    assert (
        h.request("GET", f"{RELEASES_PATH}/{finance}", actor="publisher_retail").status_code == 403
    )
    assert h.request("GET", f"{RELEASES_PATH}/{'f' * 64}", actor="publisher").status_code == 404
    assert (
        h.request("GET", f"{RELEASES_PATH}/{retail}", actor="publisher_retail").status_code == 200
    )
