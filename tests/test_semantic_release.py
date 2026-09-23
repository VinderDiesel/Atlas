"""T08 语义草稿、审核与 Git 发布：草稿生命周期与发布 CAS（ADR-0031 D02/D04/D13）。

红→绿纪律（TDD）：每批用例先于实现提交，先失败后转绿。装配复用
`WorkbenchHarness`（真实路由/合同/控制库）；第一不变量是「编辑草稿不得改变
运行 Metric/发布指针」——账本 T08 红测逐字落在此文件。

诚实边界（N2）：草稿是非权威配置副本；创建/编辑/校验/审核/导出都不触发任何
执行器与发布动作。本批（T08a-s1/s2）覆盖草稿生命周期（PUT 修订 CAS）、确定性
校验（validate）、人工审核（review）与 patch 导出（export_patch）；ReleaseService
导入/发布/回退（T08b）不在此声称已实现。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.workbench_support import (
    DEPLOYMENTS_PATH,
    DRAFTS_PATH,
    REPO_ROOT,
    WorkbenchHarness,
)

_DRAFT_KEYS = {
    "draft_id",
    "kind",
    "owner",
    "scope",
    "base_git_sha",
    "revision",
    "status",
    "content",
    "content_digest",
    "created_at",
    "updated_at",
}


@pytest.fixture()
def h(tmp_path: Path) -> Iterator[WorkbenchHarness]:
    with WorkbenchHarness(tmp_path) as harness:
        yield harness


def _created(
    h: WorkbenchHarness, actor: str = "editor", **overrides: Any
) -> dict[str, Any]:
    """创建合法草稿并返回 201 视图（失败即断言，避免后续用例误判）。"""
    response = h.request("POST", DRAFTS_PATH, actor=actor, json=h.draft_fixture(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def _head_full_sha() -> str:
    """仓库真实 HEAD 完整 sha（独立预言机，供服务端 base_git_sha 断言对照）。

    测法沿用 T01 先例（`tests/test_workbench_baseline.py` 对 `git_full_sha` 的
    独立对照），两条纪律：① 不得 import data.identity——服务端默认即
    `git_full_sha(REPO_ROOT)`，与被测方同源会让断言退化成同义反复
    （ADR-0019 决策 ② 对测试侧独立预言机的裁定理由）；② 命令形态刻意用
    `show -s --format=%H`：HEAD 解析令牌受 ADR-0019 判据 5(a) 全仓检索约束，
    测试侧命中集是被裁定的精确集合（恰含 2 处预言机），写出来会把本文件
    变成未裁定命中项而判红（同 `test_container_identity.py` 的历史处置：
    改测试，不放宽判据）。
    """
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", "-s", "--format=%H", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


# ---------------------------------------------------------------------------
# T08a-s1 草稿生命周期：不激活、授权门、形状门、CAS（红→绿）
# ---------------------------------------------------------------------------


def test_editing_draft_does_not_activate(h: WorkbenchHarness) -> None:
    """账本 T08 红测逐字：创建草稿不改变部署的 active_release_id（草稿非权威）。"""
    active = h.seed_release()
    response = h.request(
        "POST", DRAFTS_PATH, actor="editor", json=h.draft_fixture("semantic")
    )
    assert response.status_code == 201
    current = h.request("GET", f"{DEPLOYMENTS_PATH}/finance", actor="publisher")
    assert current.json()["active_release_id"] == active


def test_draft_view_is_fixed_keys_and_server_stamped_base(h: WorkbenchHarness) -> None:
    """创建返回固定 11 键；base_git_sha 由服务端取本地 HEAD（不采信客户端声称）。"""
    view = _created(h)
    assert set(view) == _DRAFT_KEYS
    assert view["kind"] == "semantic"
    assert view["scope"] == "finance"
    assert view["status"] == "draft"
    assert view["revision"] == 1
    assert view["owner"] == {"issuer": "atlas-local", "subject": "editor"}
    assert view["base_git_sha"] == _head_full_sha()
    assert h.executor_spy.calls == []


def test_draft_creation_requires_editor_capability(h: WorkbenchHarness) -> None:
    """无 draft.edit 的角色（viewer/publisher/reviewer）一律 403；被拒零持久化。"""
    for actor in ("viewer", "publisher", "reviewer"):
        response = h.request("POST", DRAFTS_PATH, actor=actor, json=h.draft_fixture())
        assert response.status_code == 403, (actor, response.text)
    listed = h.request("GET", DRAFTS_PATH, actor="editor")
    assert listed.status_code == 200
    assert listed.json()["items"] == []
    assert h.executor_spy.calls == []


def test_draft_creation_requires_authorized_scope(h: WorkbenchHarness) -> None:
    """editor_retail 有 draft.edit 但 finance 域未授权 → 403；本域可建（域门对称）。"""
    denied = h.request(
        "POST", DRAFTS_PATH, actor="editor_retail", json=h.draft_fixture(scope="finance")
    )
    assert denied.status_code == 403
    allowed = _created(h, actor="editor_retail", domain="retail")
    assert allowed["scope"] == "retail"


def test_draft_creation_rejects_non_whitelisted_target(h: WorkbenchHarness) -> None:
    """路径穿越/非制品路径/跨 kind 目标/形状错误一律 422；被拒零持久化。

    制品白名单与发布装载（agent.runtime.bundle）同源：不可导出的草稿不允许存在。
    """
    bad_targets = (
        "../../etc/passwd",
        "semantic/ossie/../../x.ossie.yaml",
        "/abs/semantic/ossie/atlas_finance.ossie.yaml",
        "semantic/ossie/atlas_finance.ossie.yaml.bak",
        "agent/runtime/bundle.py",
    )
    for target in bad_targets:
        body = h.draft_fixture(content={"target": target, "document": {}})
        response = h.request("POST", DRAFTS_PATH, actor="editor", json=body)
        assert response.status_code == 422, (target, response.text)
    # document 必须是对象
    wrong_document = h.request(
        "POST",
        DRAFTS_PATH,
        actor="editor",
        json=h.draft_fixture(
            content={"target": "semantic/ossie/atlas_finance.ossie.yaml", "document": "text"}
        ),
    )
    assert wrong_document.status_code == 422
    # content 未知键拒绝（不静默持久化无法导出的字段）
    extra_key = h.request(
        "POST",
        DRAFTS_PATH,
        actor="editor",
        json=h.draft_fixture(
            content={
                "target": "semantic/ossie/atlas_finance.ossie.yaml",
                "document": {},
                "extra": 1,
            }
        ),
    )
    assert extra_key.status_code == 422
    # 跨 kind：flow 草稿不得指向语义文件（kind ↔ 目标前缀一致）
    cross_kind = h.request(
        "POST",
        DRAFTS_PATH,
        actor="editor",
        json=h.draft_fixture(
            kind="flow",
            content={"target": "semantic/ossie/atlas_finance.ossie.yaml", "document": {}},
        ),
    )
    assert cross_kind.status_code == 422
    # 未注册 kind 在合同层拒绝
    unknown_kind = h.request(
        "POST", DRAFTS_PATH, actor="editor", json=h.draft_fixture(kind="pipeline")
    )
    assert unknown_kind.status_code == 422
    assert h.request("GET", DRAFTS_PATH, actor="editor").json()["items"] == []


def test_draft_update_uses_if_match_revision_cas(h: WorkbenchHarness) -> None:
    """If-Match 必须携带且为修订 ETag；过期 409 内容不变，匹配则 revision+1。"""
    view = _created(h)
    draft_id = view["draft_id"]
    original = view["content"]
    edited = {
        "target": original["target"],
        "document": {**original["document"], "x-test-note": "edited"},
    }
    # 缺少 If-Match → 422（无 CAS 的盲写不允许；方法为 D13 目标表的 PUT）
    missing = h.request(
        "PUT", f"{DRAFTS_PATH}/{draft_id}", actor="editor", json={"content": edited}
    )
    assert missing.status_code == 422
    # 过期修订 → 409 且内容不变
    stale = h.request(
        "PUT",
        f"{DRAFTS_PATH}/{draft_id}",
        actor="editor",
        headers={"If-Match": f'"{view["revision"] + 1}"'},
        json={"content": edited},
    )
    assert stale.status_code == 409
    unchanged = h.request("GET", f"{DRAFTS_PATH}/{draft_id}", actor="editor")
    assert unchanged.json()["revision"] == 1
    # 匹配修订 → 200 且 revision+1、内容与摘要变更
    ok = h.request(
        "PUT",
        f"{DRAFTS_PATH}/{draft_id}",
        actor="editor",
        headers={"If-Match": f'"{view["revision"]}"'},
        json={"content": edited},
    )
    assert ok.status_code == 200, ok.text
    updated = ok.json()
    assert updated["revision"] == 2
    assert updated["status"] == "draft"
    assert updated["content"]["document"]["x-test-note"] == "edited"
    assert updated["content_digest"] != view["content_digest"]


def test_draft_detail_methods_are_get_and_put_only(h: WorkbenchHarness) -> None:
    """D13 目标表为 GET/PUT：详情路径不注册第二写入口（PATCH 不暴露）。

    不断言 HTTP 状态码：SPA catch-all（0018 决策 ②）把方法不匹配归并为 404，
    契约未承诺 405——方法集以 OpenAPI 声明为权威断言面。
    """
    methods = set(h.client.app.openapi()["paths"][f"{DRAFTS_PATH}/{{draft_id}}"])
    assert methods == {"get", "put"}


def test_draft_object_acl_and_scope_on_read(h: WorkbenchHarness) -> None:
    """对象 ACL：本人可改；同域第二编辑可读不可改；他域读取 403、未知 404。"""
    view = _created(h)
    draft_id = view["draft_id"]
    content = h.draft_fixture()["content"]
    foreign_edit = h.request(
        "PUT",
        f"{DRAFTS_PATH}/{draft_id}",
        actor="editor2",
        headers={"If-Match": '"1"'},
        json={"content": content},
    )
    assert foreign_edit.status_code == 403
    # 同域可读（列表与详情不因作者裁剪；审核需要跨人可见）
    assert h.request("GET", f"{DRAFTS_PATH}/{draft_id}", actor="editor2").status_code == 200
    assert h.request("GET", f"{DRAFTS_PATH}/{draft_id}", actor="reviewer").status_code == 200
    # 他域：详情 403、列表为空（服务端裁剪，不泄露他域存在性）
    assert (
        h.request("GET", f"{DRAFTS_PATH}/{draft_id}", actor="editor_retail").status_code
        == 403
    )
    assert h.request("GET", DRAFTS_PATH, actor="editor_retail").json()["items"] == []
    # 未知草稿 404；无草稿读取能力 403
    assert h.request("GET", f"{DRAFTS_PATH}/nonexistent", actor="editor").status_code == 404
    assert h.request("GET", DRAFTS_PATH, actor="operator").status_code == 403


# ---------------------------------------------------------------------------
# T08a-s2 确定性校验、人工审核与 patch 导出（红→绿）
# ---------------------------------------------------------------------------

_VALIDATION_KEYS = {
    "validation_id",
    "draft_id",
    "revision",
    "content_digest",
    "status",
    "findings",
    "actor",
    "created_at",
}
_REVIEW_KEYS = {
    "review_id",
    "draft_id",
    "revision",
    "content_digest",
    "decision",
    "comment",
    "actor",
    "created_at",
}
_PATCH_KEYS = {
    "draft_id",
    "revision",
    "content_digest",
    "base_git_sha",
    "target",
    "patch",
    "impact",
}
_IMPACT_KEYS = {
    "added_metrics",
    "removed_metrics",
    "changed_metrics",
    "added_dimensions",
    "removed_dimensions",
    "changed_dimensions",
}
# 夹具文档（git HEAD 的真实 finance 模型）锚点：首/末指标用于最小 diff 断言——
# 首指标变更时，文件末尾指标不得出现在 hunk 上下文里（判「未全文件重写」）。
_EDIT_METRIC = "total_trade_value"
_FAR_METRIC = "average_cash_balance"
_FINANCE_TARGET = "semantic/ossie/atlas_finance.ossie.yaml"


def _validations_path(draft_id: str) -> str:
    return f"{DRAFTS_PATH}/{draft_id}/validations"


def _reviews_path(draft_id: str) -> str:
    return f"{DRAFTS_PATH}/{draft_id}/reviews"


def _patch_path(draft_id: str) -> str:
    return f"{DRAFTS_PATH}/{draft_id}/patch"


def _validate(h: WorkbenchHarness, draft_id: str, *, actor: str = "editor") -> Any:
    return h.request("POST", _validations_path(draft_id), actor=actor)


def _review(
    h: WorkbenchHarness,
    draft_id: str,
    *,
    actor: str = "reviewer",
    decision: str = "approved",
    comment: str | None = None,
) -> Any:
    body: dict[str, Any] = {"decision": decision}
    if comment is not None:
        body["comment"] = comment
    return h.request("POST", _reviews_path(draft_id), actor=actor, json=body)


def _put(
    h: WorkbenchHarness,
    draft_id: str,
    revision: int,
    content: dict[str, Any],
    *,
    actor: str = "editor",
) -> Any:
    return h.request(
        "PUT",
        f"{DRAFTS_PATH}/{draft_id}",
        actor=actor,
        headers={"If-Match": f'"{revision}"'},
        json={"content": content},
    )


def _metrics(document: dict[str, Any]) -> list[dict[str, Any]]:
    return document["semantic_model"][0]["metrics"]


def _metric(document: dict[str, Any], name: str) -> dict[str, Any]:
    return next(m for m in _metrics(document) if m["name"] == name)


def _active_metric_names(domain: str) -> set[str]:
    """活跃模型的指标名集（从磁盘读取；N8 冲突源的动态锚）。"""
    document = yaml.safe_load(
        (REPO_ROOT / "semantic" / "ossie" / f"atlas_{domain}.ossie.yaml").read_text(
            encoding="utf-8"
        )
    )
    return {m["name"] for m in document["semantic_model"][0]["metrics"]}


def test_validation_passes_and_advances_to_validated(h: WorkbenchHarness) -> None:
    """合法草稿 → 201 passed/findings=[]，状态推进 validated（摘要与修订不变）。"""
    draft = _created(h)
    response = _validate(h, draft["draft_id"])
    assert response.status_code == 201, response.text
    view = response.json()
    assert set(view) == _VALIDATION_KEYS
    assert view["draft_id"] == draft["draft_id"]
    assert view["revision"] == draft["revision"]
    assert view["content_digest"] == draft["content_digest"]
    assert view["status"] == "passed"
    assert view["findings"] == []
    assert view["actor"] == {"issuer": "atlas-local", "subject": "editor"}
    current = h.request("GET", f"{DRAFTS_PATH}/{draft['draft_id']}", actor="editor").json()
    assert current["status"] == "validated"
    assert current["revision"] == draft["revision"]
    assert current["content_digest"] == draft["content_digest"]
    assert h.executor_spy.calls == []


def test_validation_recheck_records_again_without_state_drift(h: WorkbenchHarness) -> None:
    """通过后重复校验：追加新证据行、状态不漂移（幂等重验，不跳级）。"""
    draft = _created(h)
    first = _validate(h, draft["draft_id"])
    assert first.status_code == 201, first.text
    second = _validate(h, draft["draft_id"])
    assert second.status_code == 201, second.text
    assert second.json()["status"] == "passed"
    assert second.json()["validation_id"] != first.json()["validation_id"]
    current = h.request("GET", f"{DRAFTS_PATH}/{draft['draft_id']}", actor="editor").json()
    assert current["status"] == "validated"
    assert current["revision"] == draft["revision"]


def test_validation_gates_capability_scope_and_kind(h: WorkbenchHarness) -> None:
    """validate 门：能力（viewer/reviewer 403）、作用域（editor_retail 403）；
    同域他人可校验（editor2 201）；非 semantic kind 422；被拒零副作用。"""
    draft = _created(h)
    draft_id = draft["draft_id"]
    for actor in ("viewer", "reviewer"):
        denied = _validate(h, draft_id, actor=actor)
        assert denied.status_code == 403, (actor, denied.text)
    denied_scope = _validate(h, draft_id, actor="editor_retail")
    assert denied_scope.status_code == 403, denied_scope.text
    still = h.request("GET", f"{DRAFTS_PATH}/{draft_id}", actor="editor").json()
    assert still["status"] == "draft"
    assert still["revision"] == draft["revision"]
    # 同域第二编辑可校验（确定性动作、不改内容——审核前复核不要求本人）
    allowed = _validate(h, draft_id, actor="editor2")
    assert allowed.status_code == 201, allowed.text
    # 非 semantic kind 草稿无法确定性校验：422 明确拒绝，不静默通过
    flow = h.request(
        "POST",
        DRAFTS_PATH,
        actor="editor",
        json=h.draft_fixture(
            kind="flow",
            content={
                "target": "agent/flows/templates/example.json",
                "document": {"steps": []},
            },
        ),
    )
    assert flow.status_code == 201, flow.text
    unsupported = _validate(h, flow.json()["draft_id"])
    assert unsupported.status_code == 422, unsupported.text


def test_validation_rejects_same_name_active_metric(h: WorkbenchHarness) -> None:
    """N8 同名 active：草稿指标与其他活跃模型同名 → failed（structure），停留 draft。"""
    disjoint = sorted(_active_metric_names("retail") - _active_metric_names("finance"))
    assert disjoint, "前置：零售存在与金融不重叠的指标名（N8 冲突源）"
    fixture = h.draft_fixture()
    document = fixture["content"]["document"]
    assert all(m["name"] != disjoint[0] for m in _metrics(document))
    _metric(document, _EDIT_METRIC)["name"] = disjoint[0]
    draft = _created(h, content=fixture["content"])
    response = _validate(h, draft["draft_id"])
    assert response.status_code == 201, response.text
    view = response.json()
    assert view["status"] == "failed"
    assert {f["code"] for f in view["findings"]} <= {"structure", "governance", "policy"}
    assert any(
        f["code"] == "structure" and "指标重名" in f["message"] for f in view["findings"]
    ), view["findings"]
    still = h.request("GET", f"{DRAFTS_PATH}/{draft['draft_id']}", actor="editor").json()
    assert still["status"] == "draft"


def test_validation_rejects_unknown_relationship_dataset(h: WorkbenchHarness) -> None:
    """未知关系目标：relationship.to 指向不存在 dataset → structure findings。"""
    fixture = h.draft_fixture()
    document = fixture["content"]["document"]
    document["semantic_model"][0]["relationships"][0]["to"] = "ds_does_not_exist"
    draft = _created(h, content=fixture["content"])
    response = _validate(h, draft["draft_id"])
    assert response.status_code == 201, response.text
    view = response.json()
    assert view["status"] == "failed"
    assert any(
        f["code"] == "structure" and "ds_does_not_exist" in f["message"]
        for f in view["findings"]
    ), view["findings"]


def test_validation_rejects_missing_ansi_dialect(h: WorkbenchHarness) -> None:
    """方言能力：字段 expression 缺 ANSI_SQL → structure findings（不静默放行）。"""
    fixture = h.draft_fixture()
    document = fixture["content"]["document"]
    field = document["semantic_model"][0]["datasets"][0]["fields"][0]
    field["expression"]["dialects"] = [
        d for d in field["expression"]["dialects"] if d["dialect"] != "ANSI_SQL"
    ]
    draft = _created(h, content=fixture["content"])
    response = _validate(h, draft["draft_id"])
    assert response.status_code == 201, response.text
    view = response.json()
    assert view["status"] == "failed"
    assert any(
        f["code"] == "structure" and "ANSI_SQL" in f["message"] for f in view["findings"]
    ), view["findings"]


def test_validation_rejects_unknown_row_policy(h: WorkbenchHarness) -> None:
    """未知策略：模型 default_row_policy 引用不存在策略 → governance findings。"""
    fixture = h.draft_fixture()
    document = fixture["content"]["document"]
    model = document["semantic_model"][0]
    for ext in model["custom_extensions"]:
        if ext.get("vendor_name") != "ATLAS":
            continue
        data = json.loads(ext["data"])
        if "policy" in data:
            data["policy"]["default_row_policy"] = "rp_not_registered"
            ext["data"] = json.dumps(data, ensure_ascii=False)
            break
    else:
        raise AssertionError("模型级 ATLAS 扩展（含 policy）未找到")
    draft = _created(h, content=fixture["content"])
    response = _validate(h, draft["draft_id"])
    assert response.status_code == 201, response.text
    view = response.json()
    assert view["status"] == "failed"
    assert any(
        f["code"] == "governance" and "rp_not_registered" in f["message"]
        for f in view["findings"]
    ), view["findings"]


def test_review_requires_validated_status_and_binds_digest(h: WorkbenchHarness) -> None:
    """未校验不得审核（409）；approved → 201 固定 8 键 + 状态 reviewed。"""
    draft = _created(h)
    draft_id = draft["draft_id"]
    early = _review(h, draft_id)
    assert early.status_code == 409, early.text
    validated = _validate(h, draft_id)
    assert validated.status_code == 201, validated.text
    approved = _review(h, draft_id, comment="影响面与语义已核对")
    assert approved.status_code == 201, approved.text
    view = approved.json()
    assert set(view) == _REVIEW_KEYS
    assert view["draft_id"] == draft_id
    assert view["revision"] == draft["revision"]
    assert view["content_digest"] == draft["content_digest"]
    assert view["decision"] == "approved"
    assert view["comment"] == "影响面与语义已核对"
    assert view["actor"] == {"issuer": "atlas-local", "subject": "reviewer"}
    current = h.request("GET", f"{DRAFTS_PATH}/{draft_id}", actor="editor").json()
    assert current["status"] == "reviewed"


def test_review_rejected_records_without_advancing(h: WorkbenchHarness) -> None:
    """rejected 只落审核证据、不推进状态；随后 approved 仍可推进（同修订重审）。"""
    draft = _created(h)
    draft_id = draft["draft_id"]
    assert _validate(h, draft_id).status_code == 201
    rejected = _review(h, draft_id, decision="rejected", comment="影响面说明不足")
    assert rejected.status_code == 201, rejected.text
    assert rejected.json()["decision"] == "rejected"
    after = h.request("GET", f"{DRAFTS_PATH}/{draft_id}", actor="editor").json()
    assert after["status"] == "validated"
    approved = _review(h, draft_id)
    assert approved.status_code == 201, approved.text
    assert approved.json()["comment"] is None
    final = h.request("GET", f"{DRAFTS_PATH}/{draft_id}", actor="editor").json()
    assert final["status"] == "reviewed"


def test_review_gates_capability_and_decision_shape(h: WorkbenchHarness) -> None:
    """无 draft.review 403（editor/editor_retail/viewer）；decision 非法 422；未知 404。"""
    draft = _created(h)
    draft_id = draft["draft_id"]
    assert _validate(h, draft_id).status_code == 201
    for actor in ("editor", "editor_retail", "viewer"):
        denied = _review(h, draft_id, actor=actor)
        assert denied.status_code == 403, (actor, denied.text)
    invalid = _review(h, draft_id, decision="maybe")
    assert invalid.status_code == 422, invalid.text
    missing = h.request("POST", _reviews_path(draft_id), actor="reviewer", json={})
    assert missing.status_code == 422, missing.text
    unknown = _review(h, "nonexistent")
    assert unknown.status_code == 404, unknown.text


def test_edit_after_review_requires_fresh_validation(h: WorkbenchHarness) -> None:
    """审核后篡改失效（账本绿测）：编辑回 draft；旧审核不复用（409）；
    重新校验通过后审核回到 reviewed。"""
    draft = _created(h)
    draft_id = draft["draft_id"]
    assert _validate(h, draft_id).status_code == 201
    assert _review(h, draft_id).status_code == 201
    content = h.draft_fixture()["content"]
    content["document"]["x-test-note"] = "tampered"
    edited = _put(h, draft_id, draft["revision"], content)
    assert edited.status_code == 200, edited.text
    assert edited.json()["status"] == "draft"
    replay = _review(h, draft_id)
    assert replay.status_code == 409, replay.text
    assert _validate(h, draft_id).status_code == 201
    assert _review(h, draft_id).status_code == 201
    final = h.request("GET", f"{DRAFTS_PATH}/{draft_id}", actor="editor").json()
    assert final["status"] == "reviewed"


def test_patch_export_pristine_draft_is_empty_diff(h: WorkbenchHarness) -> None:
    """未修改草稿（== base）→ 空 patch、impact 全空（最小 diff 的下界锚）。"""
    draft = _created(h)
    response = h.request("GET", _patch_path(draft["draft_id"]), actor="editor")
    assert response.status_code == 200, response.text
    view = response.json()
    assert set(view) == _PATCH_KEYS
    assert view["draft_id"] == draft["draft_id"]
    assert view["revision"] == draft["revision"]
    assert view["content_digest"] == draft["content_digest"]
    assert view["base_git_sha"] == _head_full_sha()
    assert view["target"] == _FINANCE_TARGET
    assert view["patch"] == ""
    assert set(view["impact"]) == _IMPACT_KEYS
    assert all(view["impact"][key] == [] for key in _IMPACT_KEYS)


def test_patch_export_minimal_diff_for_metric_change(h: WorkbenchHarness) -> None:
    """单指标变更 → 只动该指标条目内字段；远端指标不入 patch（未全文件重写）。"""
    fixture = h.draft_fixture()
    content = fixture["content"]
    _metric(content["document"], _EDIT_METRIC)["description"] = "改后描述（T08a-s2 测试）"
    draft = _created(h, content=content)
    response = h.request("GET", _patch_path(draft["draft_id"]), actor="editor")
    assert response.status_code == 200, response.text
    view = response.json()
    patch = view["patch"]
    assert patch.startswith(f"--- a/{_FINANCE_TARGET}\n")
    assert f"+++ b/{_FINANCE_TARGET}" in patch
    assert "改后描述（T08a-s2 测试）" in patch
    changed = [
        line
        for line in patch.splitlines()
        if line[:1] in "+-" and not line.startswith(("---", "+++"))
    ]
    assert changed, "patch 必须含变更行"
    assert _FAR_METRIC not in patch  # 文件末尾指标不在 hunk 上下文内
    assert len(patch.splitlines()) < 20  # 字段级最小化（非整条目/整文件重写）
    assert view["impact"]["changed_metrics"] == [_EDIT_METRIC]
    assert view["impact"]["added_metrics"] == []
    assert view["impact"]["removed_metrics"] == []


def test_patch_export_add_remove_metrics_drive_impact(h: WorkbenchHarness) -> None:
    """增删指标：patch 含对应条目块，impact 三个集合精确。"""
    fixture = h.draft_fixture()
    content = fixture["content"]
    metrics = _metrics(content["document"])
    removed = metrics.pop()["name"]
    clone = dict(metrics[0])
    clone["name"] = "x_test_added_metric"
    clone["description"] = "新增指标（T08a-s2 测试）"
    metrics.append(clone)
    draft = _created(h, content=content)
    view = h.request("GET", _patch_path(draft["draft_id"]), actor="editor").json()
    assert view["impact"]["added_metrics"] == ["x_test_added_metric"]
    assert view["impact"]["removed_metrics"] == [removed]
    assert view["impact"]["changed_metrics"] == []
    patch = view["patch"]
    assert "x_test_added_metric" in patch
    assert f"name: {removed}" in patch
    assert view["base_git_sha"] == _head_full_sha()


def test_patch_impact_reports_dimension_addition(h: WorkbenchHarness) -> None:
    """模型新增维度字段（dimension 键）→ impact.added_dimensions 与 patch 同步。"""
    fixture = h.draft_fixture()
    content = fixture["content"]
    model = content["document"]["semantic_model"][0]
    dataset = next(d for d in model["datasets"] if d["name"].startswith("dim_"))
    dataset["fields"].append(
        {
            "name": "x_test_channel",
            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "''"}]},
            "datatype": "string",
            "dimension": {},
        }
    )
    draft = _created(h, content=content)
    view = h.request("GET", _patch_path(draft["draft_id"]), actor="editor").json()
    assert view["impact"]["added_dimensions"] == [f"{dataset['name']}.x_test_channel"]
    assert "x_test_channel" in view["patch"]
    assert view["impact"]["changed_metrics"] == []
    assert view["impact"]["added_metrics"] == []


def test_patch_export_applies_with_git_apply(h: WorkbenchHarness, tmp_path: Path) -> None:
    """可应用性（人肉 Git 往返的下界）：patch 在 base 树 apply 后语义等于草稿。"""
    fixture = h.draft_fixture()
    content = fixture["content"]
    _metric(content["document"], _EDIT_METRIC)["description"] = "应用性验证（T08a-s2 测试）"
    draft = _created(h, content=content)
    view = h.request("GET", _patch_path(draft["draft_id"]), actor="editor").json()
    tree = tmp_path / "apply-tree"
    target_file = tree / "semantic" / "ossie" / "atlas_finance.ossie.yaml"
    target_file.parent.mkdir(parents=True)
    base = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{draft['base_git_sha']}:{_FINANCE_TARGET}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    target_file.write_text(base, encoding="utf-8")
    patch_file = tmp_path / "draft.patch"
    patch_file.write_text(view["patch"] + "\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tree, check=True)
    applied = subprocess.run(
        ["git", "apply", str(patch_file)], cwd=tree, capture_output=True, text=True
    )
    assert applied.returncode == 0, applied.stderr
    result = yaml.safe_load(target_file.read_text(encoding="utf-8"))
    assert result == content["document"]


def test_patch_export_requires_export_capability(h: WorkbenchHarness) -> None:
    """无 draft.export 403（viewer/reviewer/publisher）；跨域 403；editor 200。"""
    draft = _created(h)
    draft_id = draft["draft_id"]
    for actor in ("viewer", "reviewer", "publisher"):
        denied = h.request("GET", _patch_path(draft_id), actor=actor)
        assert denied.status_code == 403, (actor, denied.text)
    assert h.request("GET", _patch_path(draft_id), actor="editor_retail").status_code == 403
    allowed = h.request("GET", _patch_path(draft_id), actor="editor")
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["content_digest"] == draft["content_digest"]


def test_patch_export_new_file_target_uses_dev_null(h: WorkbenchHarness) -> None:
    """新文件草稿（base sha 中无此路径）→ /dev/null 新增形态，而非伪造上下文。"""
    content = {
        "target": "semantic/ossie/atlas_test_probe.ossie.yaml",
        "document": {
            "version": "0.2.0.dev0",
            "semantic_model": [{"name": "atlas_test_probe", "metrics": []}],
        },
    }
    draft = _created(h, content=content)
    view = h.request("GET", _patch_path(draft["draft_id"]), actor="editor").json()
    assert view["patch"].startswith("--- /dev/null\n")
    assert "+++ b/semantic/ossie/atlas_test_probe.ossie.yaml" in view["patch"]
    assert "atlas_test_probe" in view["patch"]
    assert view["impact"]["added_metrics"] == []
