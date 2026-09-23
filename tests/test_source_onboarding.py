"""T07 只读数据源接入向导：源修订追加与受限探测（ADR-0031 D03/D13）。

红→绿纪律（TDD）：每批用例先于实现提交，先失败后转绿。装配复用
`WorkbenchHarness`（真实路由/合同/控制库；外部探测与业务执行器均为替身，
`executor_spy.calls == []` 是「配置源不执行任何业务 SQL」的证据）。

诚实边界（N2）：ProbeResult 是带时间戳的**证据**，不是永久保证；在线探测
结果不计 EX（工作卡）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.workbench_support import DEPLOYMENTS_PATH, SOURCES_PATH, WorkbenchHarness


def _secret(*, host: str = "127.0.0.1", port: int = 9030) -> str:
    """连接秘密的环境变量值（JSON 形态，测试用；不含真实凭据）。"""
    return json.dumps(
        {"host": host, "port": port, "user": "atlas_ro", "password": "sup3r-secret-pw"}
    )


@pytest.fixture()
def h(tmp_path: Path) -> Iterator[WorkbenchHarness]:
    with WorkbenchHarness(tmp_path) as harness:
        yield harness


# ---------------------------------------------------------------------------
# T07a 源修订：明文凭据拒绝、追加式版本、能力门（红→绿）
# ---------------------------------------------------------------------------


def test_source_rejects_inline_secret(h: WorkbenchHarness) -> None:
    """连接向导只接受环境引用而非明文凭据（工作卡红测逐字）。

    额外字段（password）在合同层被拒（422）——秘密字段根本不进入服务路径，
    且配置源不执行任何业务 SQL（executor 零调用）。
    """
    body = h.source_fixture()
    body["password"] = "not-a-real-secret"
    response = h.request("POST", SOURCES_PATH, actor="operator", json=body)
    assert response.status_code == 422
    assert h.executor_spy.calls == []


def test_source_secret_ref_must_be_env_reference(h: WorkbenchHarness) -> None:
    """完整 DSN 形态的引用一律拒绝（N9/D03：秘密只由环境变量解析）。"""
    body = h.source_fixture(secret_ref="mysql://atlas_ro:secret@10.0.0.5:9030")
    response = h.request("POST", SOURCES_PATH, actor="operator", json=body)
    assert response.status_code == 422
    assert h.executor_spy.calls == []


def test_source_only_registers_doris(h: WorkbenchHarness) -> None:
    """首版只接受 connector_kind=doris（D03；不把协议兼容当数据源支持）。"""
    body = h.source_fixture(connector_kind="mysql")
    response = h.request("POST", SOURCES_PATH, actor="operator", json=body)
    assert response.status_code == 422


def test_source_rejects_table_outside_allowed_catalogs(h: WorkbenchHarness) -> None:
    """表越界（配置自检）：allowed_tables 必须属于 allowed_catalogs（D03 接入前验证）。"""
    body = h.source_fixture(allowed_tables=["other.dwd.fact_trades"])
    response = h.request("POST", SOURCES_PATH, actor="operator", json=body)
    assert response.status_code == 422


def test_source_requires_operator_capability(h: WorkbenchHarness) -> None:
    """viewer 无 source.manage → 403；被拒提交零持久化（fail-closed）。"""
    response = h.request("POST", SOURCES_PATH, actor="viewer", json=h.source_fixture())
    assert response.status_code == 403
    listed = h.request("GET", SOURCES_PATH, actor="operator")
    assert listed.status_code == 200
    assert listed.json()["items"] == []


def test_source_revision_is_append_only(h: WorkbenchHarness) -> None:
    """同 source 新建 revision：版本递增、旧行不变、GET 只回最新版本（固定键）。"""
    first = h.request("POST", SOURCES_PATH, actor="operator", json=h.source_fixture())
    assert first.status_code == 201, first.text
    view = first.json()
    assert view["version"] == 1
    assert set(view) == {
        "source_id",
        "version",
        "revision",
        "connector_kind",
        "secret_ref",
        "allowed_catalogs",
        "allowed_tables",
        "timezone",
        "tls_policy",
        "query_budget",
        "created_by",
        "created_at",
        "last_probe",
    }
    assert view["last_probe"] is None
    assert view["created_by"] == {"issuer": "atlas-local", "subject": "operator"}

    second = h.request(
        "POST",
        SOURCES_PATH,
        actor="operator",
        json=h.source_fixture(revision="rev-2026-09-22-2", query_budget=20_000),
    )
    assert second.status_code == 201, second.text
    assert second.json()["version"] == 2

    listed = h.request("GET", SOURCES_PATH, actor="operator")
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["version"] == 2
    assert items[0]["query_budget"] == 20_000

    # 追加式：v1 行不变（不原地改变既有绑定所引用的修订）
    v1 = h.control_store.get_source_revision("doris-primary", 1)
    assert v1.query_budget == 10_000
    assert v1.revision == "rev-2026-09-22-1"


# ---------------------------------------------------------------------------
# T07b 受限探测：凭据/目标/TLS/白名单/只读确认（红→绿）
# ---------------------------------------------------------------------------

PROBES_PATH = f"{SOURCES_PATH}/doris-primary/probes"
_RESULT_KEYS = {
    "probe_id",
    "source_id",
    "version",
    "status",
    "blocked_reason",
    "observed_at",
    "engine_version",
    "schema_digest",
    "capabilities",
    "reproducible",
}
_CAPABILITY_KEYS = {
    "dialect",
    "read_only",
    "metadata_probe",
    "cancel_query",
    "snapshot_read",
    "consistent_analysis",
}


def _seed_source(h: WorkbenchHarness, **overrides: object) -> None:
    response = h.request(
        "POST", SOURCES_PATH, actor="operator", json=h.source_fixture(**overrides)
    )
    assert response.status_code == 201, response.text


def test_probe_blocks_when_credential_env_missing(h: WorkbenchHarness) -> None:
    """凭据缺失（env 引用未定义）→ blocked，不建立任何连接（spy 零调用）。"""
    _seed_source(h, secret_ref="env:ATLAS_MISSING_SOURCE")
    response = h.request("POST", PROBES_PATH, actor="operator")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "blocked"
    assert body["blocked_reason"] == "credential_missing"
    assert h.probe_spy.calls == []


def test_probe_forbids_cloud_metadata_and_linklocal_targets(h: WorkbenchHarness) -> None:
    """云元数据与 link-local 目标硬禁（即使误配进允许列表也拒绝，D03）。"""
    _seed_source(h)
    for forbidden in ("169.254.169.254", "169.254.9.9"):
        h.source_environment["ATLAS_TEST_DORIS"] = _secret(host=forbidden)
        response = h.request("POST", PROBES_PATH, actor="operator")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "blocked", forbidden
        assert body["blocked_reason"] == "target_forbidden", forbidden
    assert h.probe_spy.calls == []


def test_probe_rejects_unauthorized_resolved_target(h: WorkbenchHarness) -> None:
    """DNS 解析到未授权地址（重定向/漂移）→ blocked；连接前完成校验。"""
    _seed_source(h)
    h.source_environment["ATLAS_TEST_DORIS"] = _secret(host="evil.example", port=9030)
    h.source_dns["evil.example"] = ["203.0.113.7"]
    response = h.request("POST", PROBES_PATH, actor="operator")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "blocked"
    assert body["blocked_reason"] == "target_not_allowlisted"
    assert h.probe_spy.calls == []


def test_probe_connects_to_validated_ip_not_hostname(h: WorkbenchHarness) -> None:
    """防 DNS 重绑定：连接目标用校验过的 IP 字面量，不用域名。"""
    _seed_source(h)
    h.source_environment["ATLAS_TEST_DORIS"] = _secret(host="db.example", port=9030)
    h.source_dns["db.example"] = ["127.0.0.1"]
    response = h.request("POST", PROBES_PATH, actor="operator")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"
    target, _spec = h.probe_spy.calls[0]
    assert target.host == "127.0.0.1"
    assert target.port == 9030


def test_probe_maps_tls_failure(h: WorkbenchHarness) -> None:
    """TLS 握手失败（tls_policy=required）→ blocked tls_error。"""
    from agent.runtime.connectors.base import ProbeTlsError

    _seed_source(h, tls_policy="required")
    h.probe_spy.failure = ProbeTlsError("certificate verify failed")
    response = h.request("POST", PROBES_PATH, actor="operator")
    body = response.json()
    assert body["status"] == "blocked"
    assert body["blocked_reason"] == "tls_error"


def test_probe_maps_revoked_target_authorization(h: WorkbenchHarness) -> None:
    """数据侧撤销授权（账号被拒）→ blocked credential_rejected。"""
    from agent.runtime.connectors.base import ProbeAuthError

    _seed_source(h)
    h.probe_spy.failure = ProbeAuthError("access denied")
    response = h.request("POST", PROBES_PATH, actor="operator")
    body = response.json()
    assert body["status"] == "blocked"
    assert body["blocked_reason"] == "credential_rejected"


def test_probe_blocks_table_outside_whitelist(h: WorkbenchHarness) -> None:
    """表越界（存储层防御）：白名单表不属于允许目录 → blocked，不建立连接。"""
    from serving.control.contracts import Owner

    h.control_store.append_source_revision(
        source_id="doris-primary",
        revision="rev-out-of-bounds",
        connector_kind="doris",
        secret_ref="env:ATLAS_TEST_DORIS",
        allowed_catalogs=frozenset({"atlas"}),
        allowed_tables=frozenset({"other.dwd.fact_trades"}),
        timezone="+08:00",
        tls_policy="disabled",
        query_budget=10_000,
        owner=Owner(issuer="atlas-local", subject="operator"),
    )
    response = h.request("POST", PROBES_PATH, actor="operator")
    body = response.json()
    assert body["status"] == "blocked"
    assert body["blocked_reason"] == "table_out_of_whitelist"
    assert h.probe_spy.calls == []


def test_probe_blocks_read_only_unconfirmed(h: WorkbenchHarness) -> None:
    """只读能力未确认（权限声明含写权限）→ blocked；不得声称 read_only。"""
    _seed_source(h)
    h.probe_spy.observation = h.probe_observation(
        grant_statements=("GRANT ALL_PRIV ON atlas.* TO 'atlas_ro'@'%'",)
    )
    response = h.request("POST", PROBES_PATH, actor="operator")
    body = response.json()
    assert body["status"] == "blocked"
    assert body["blocked_reason"] == "read_only_unconfirmed"


def test_probe_blocks_missing_metadata(h: WorkbenchHarness) -> None:
    """元数据缺失（白名单表在源中不存在）→ blocked metadata_missing。"""
    _seed_source(h)
    h.probe_spy.observation = h.probe_observation(tables_seen=("atlas.dwd.fact_trades",))
    response = h.request("POST", PROBES_PATH, actor="operator")
    body = response.json()
    assert body["status"] == "blocked"
    assert body["blocked_reason"] == "metadata_missing"


def test_probe_rejects_request_body(h: WorkbenchHarness) -> None:
    """探测不接受请求体：不提供任意「测试 SQL」通道（D03）。"""
    _seed_source(h)
    response = h.request(
        "POST", PROBES_PATH, actor="operator", json={"sql": "SELECT 1"}
    )
    assert response.status_code == 422
    assert h.probe_spy.calls == []


def test_probe_requires_capability_and_known_source(h: WorkbenchHarness) -> None:
    """viewer 403；未知源 404（不泄露存在性）；探测不改业务执行器。"""
    _seed_source(h)
    assert h.request("POST", PROBES_PATH, actor="viewer").status_code == 403
    unknown = h.request("POST", f"{SOURCES_PATH}/ghost/probes", actor="operator")
    assert unknown.status_code == 404
    assert h.executor_spy.calls == []


def test_probe_records_evidence_and_lists_last_probe(h: WorkbenchHarness) -> None:
    """成功探测：证据化（时间戳/摘要/能力），列表聚合 last_probe，不泄漏凭据。"""
    _seed_source(h)
    response = h.request("POST", PROBES_PATH, actor="operator")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == _RESULT_KEYS
    assert body["status"] == "ok"
    assert body["blocked_reason"] is None
    assert body["source_id"] == "doris-primary"
    assert body["version"] == 1
    assert body["observed_at"] == h.now.isoformat()
    assert body["reproducible"] is False
    assert body["engine_version"] == "4.1.0-test"
    assert len(body["schema_digest"]) == 64
    assert set(body["capabilities"]) == _CAPABILITY_KEYS
    assert body["capabilities"]["read_only"] is True
    assert body["capabilities"]["metadata_probe"] is True
    assert body["capabilities"]["cancel_query"] is False
    assert "sup3r-secret-pw" not in response.text

    listed = h.request("GET", SOURCES_PATH, actor="operator")
    view = listed.json()["items"][0]
    assert view["last_probe"] == {
        "probe_id": body["probe_id"],
        "status": "ok",
        "blocked_reason": None,
        "observed_at": body["observed_at"],
    }
    assert "sup3r-secret-pw" not in listed.text


# ---------------------------------------------------------------------------
# T07c 部署绑定：创建 draft Deployment 供 T08 发布绑定（D13 管理面）
# ---------------------------------------------------------------------------

_DEPLOYMENT_KEYS = {
    "deployment_id",
    "scope",
    "source_id",
    "active_release_id",
    "revision",
    "created_by",
    "created_at",
    "updated_at",
}


def test_deployment_created_as_draft_before_first_release(h: WorkbenchHarness) -> None:
    """新部署在首次批准发布前 active_release_id=null（D13 逐字）；指针不移动。"""
    _seed_source(h)
    created = h.request("POST", DEPLOYMENTS_PATH, actor="operator", json=h.deployment_fixture())
    assert created.status_code == 201, created.text
    body = created.json()
    assert set(body) == _DEPLOYMENT_KEYS
    assert body["deployment_id"] == "finance-live"
    assert body["scope"] == "finance"
    assert body["source_id"] == "doris-primary"
    assert body["active_release_id"] is None
    assert body["revision"] == 1
    assert body["created_by"] == {"issuer": "atlas-local", "subject": "operator"}
    assert body["created_at"] == body["updated_at"]

    # 详情与创建回执逐键一致：创建后未发生任何激活（revision 仍为 1）
    detail = h.request("GET", f"{DEPLOYMENTS_PATH}/finance-live", actor="operator")
    assert detail.status_code == 200, detail.text
    assert detail.json() == body


def test_deployment_cannot_bind_release_at_creation(h: WorkbenchHarness) -> None:
    """创建请求不得夹带活动发布（未知字段 422）：首次发布属 T08 的 CAS 动作。"""
    _seed_source(h)
    body = h.deployment_fixture()
    body["active_release_id"] = "a" * 64
    response = h.request("POST", DEPLOYMENTS_PATH, actor="operator", json=body)
    assert response.status_code == 422
    assert h.request("GET", DEPLOYMENTS_PATH, actor="operator").json()["items"] == []


def test_deployment_requires_source_and_manage_capability(h: WorkbenchHarness) -> None:
    """viewer 无 deployment.manage → 403（先于源检查）；绑定未知源 → 404 零持久化。"""
    denied = h.request("POST", DEPLOYMENTS_PATH, actor="viewer", json=h.deployment_fixture())
    assert denied.status_code == 403
    unknown = h.request(
        "POST",
        DEPLOYMENTS_PATH,
        actor="operator",
        json=h.deployment_fixture(source_id="ghost-source"),
    )
    assert unknown.status_code == 404
    listed = h.request("GET", DEPLOYMENTS_PATH, actor="operator")
    assert listed.status_code == 200
    assert listed.json()["items"] == []


def test_deployment_duplicate_id_is_conflict(h: WorkbenchHarness) -> None:
    """重复 deployment_id → 409；已存在指针不被覆盖（无原地变更）。"""
    _seed_source(h)
    first = h.request("POST", DEPLOYMENTS_PATH, actor="operator", json=h.deployment_fixture())
    assert first.status_code == 201
    again = h.request("POST", DEPLOYMENTS_PATH, actor="operator", json=h.deployment_fixture())
    assert again.status_code == 409
    listed = h.request("GET", DEPLOYMENTS_PATH, actor="operator").json()["items"]
    assert [item["deployment_id"] for item in listed] == ["finance-live"]


def test_deployment_scope_must_be_authorized(h: WorkbenchHarness) -> None:
    """operator 只授 finance：创建 retail 部署 → 403（作用域未授权，D02 fail-closed）。"""
    _seed_source(h)
    denied = h.request(
        "POST",
        DEPLOYMENTS_PATH,
        actor="operator",
        json=h.deployment_fixture(deployment_id="retail-live", scope="retail"),
    )
    assert denied.status_code == 403


def test_deployment_list_trims_to_authorized_scopes(h: WorkbenchHarness) -> None:
    """列表按已授权领域裁剪；未授权领域详情 403、未知 404（管理面区分）。"""
    _seed_source(h)
    finance = h.request("POST", DEPLOYMENTS_PATH, actor="operator", json=h.deployment_fixture())
    assert finance.status_code == 201
    retail = h.request(
        "POST",
        DEPLOYMENTS_PATH,
        actor="ops_retail",
        json=h.deployment_fixture(deployment_id="retail-live", scope="retail"),
    )
    assert retail.status_code == 201, retail.text
    finance_items = h.request("GET", DEPLOYMENTS_PATH, actor="operator").json()["items"]
    assert [item["deployment_id"] for item in finance_items] == ["finance-live"]
    retail_items = h.request("GET", DEPLOYMENTS_PATH, actor="ops_retail").json()["items"]
    assert [item["deployment_id"] for item in retail_items] == ["retail-live"]
    assert (
        h.request("GET", f"{DEPLOYMENTS_PATH}/retail-live", actor="operator").status_code
        == 403
    )
    assert (
        h.request("GET", f"{DEPLOYMENTS_PATH}/ghost", actor="operator").status_code == 404
    )


def test_publisher_reads_deployment_but_cannot_create(h: WorkbenchHarness) -> None:
    """发布动作能力可读部署指针（CAS 前置），但不得创建（模板不隐式继承，D02）。"""
    _seed_source(h)
    created = h.request("POST", DEPLOYMENTS_PATH, actor="operator", json=h.deployment_fixture())
    assert created.status_code == 201
    detail = h.request("GET", f"{DEPLOYMENTS_PATH}/finance-live", actor="publisher")
    assert detail.status_code == 200, detail.text
    assert detail.json()["active_release_id"] is None
    assert h.request("GET", DEPLOYMENTS_PATH, actor="publisher").status_code == 200
    denied = h.request("POST", DEPLOYMENTS_PATH, actor="publisher", json=h.deployment_fixture())
    assert denied.status_code == 403
    assert (
        h.request("GET", f"{DEPLOYMENTS_PATH}/finance-live", actor="viewer").status_code == 403
    )


# ---------------------------------------------------------------------------
# T07e 连接器解析：Doris 4.x SHOW GRANTS 表格形态（真实 smoke 暴露；红→绿）
# ---------------------------------------------------------------------------
#
# 真实观察（2026-09-22，Doris 4.1）：`SHOW GRANTS` 不是 MySQL 的单列语句文本，
# 而是 16 列表格（身份/Roles + 各 *Privs 列），单元格值为 `<scope>: <Priv>; ...`。
# 原实现只识别「以 GRANT 开头的文本行」→ 真实环境提取为空 → 永远
# read_only_unconfirmed（T07b 服务层测试用替身注入 grant_statements，未覆盖该形态）。

_DORIS_GRANT_COLUMNS = (
    "UserIdentity",
    "Comment",
    "Password",
    "RequireSan",
    "Roles",
    "GlobalPrivs",
    "CatalogPrivs",
    "DatabasePrivs",
    "TablePrivs",
    "ColPrivs",
    "ResourcePrivs",
    "CloudClusterPrivs",
    "CloudStagePrivs",
    "StorageVaultPrivs",
    "WorkloadGroupPrivs",
    "ComputeGroupPrivs",
)


class _GrantCursor:
    """SHOW GRANTS 游标替身：只提供实现消费的 description/execute/fetchall。"""

    def __init__(self, columns: tuple[str, ...], rows: list[tuple[object, ...]]) -> None:
        self.description = tuple((name,) for name in columns)
        self._rows = rows

    def execute(self, sql: str) -> None:
        self._sql = sql

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


def _doris_grant_row(
    *,
    roles: str = "",
    database_privs: object = (
        "atlas.dwd: Select_priv; internal.information_schema: Select_priv;"
        " internal.mysql: Select_priv"
    ),
) -> tuple[object, ...]:
    """真实 Doris 4.1 行（列序与 `_DORIS_GRANT_COLUMNS` 对齐）。"""
    return (
        "'atlas_ro'@'%'",
        "",
        "No",
        None,
        roles,
        None,
        None,
        database_privs,
        None,
        None,
        None,
        None,
        None,
        None,
        "normal: Usage_priv",
        None,
    )


def test_grant_statements_parses_doris_table_shape() -> None:
    """表格形态：数据权限列条目 → 规范式；workload group 不参与数据面判定。"""
    from agent.runtime.connectors.doris import _grant_statements
    from serving.control.sources import _grants_are_read_only

    cursor = _GrantCursor(_DORIS_GRANT_COLUMNS, [_doris_grant_row()])
    statements = _grant_statements(cursor)
    assert statements == (
        "GRANT SELECT_PRIV ON atlas.dwd",
        "GRANT SELECT_PRIV ON internal.information_schema",
        "GRANT SELECT_PRIV ON internal.mysql",
    )
    assert _grants_are_read_only(statements) is True


def test_grant_statements_table_shape_rejects_write_privilege() -> None:
    """数据权限列含非只读动词（Load_priv）→ 条目保留且判定不确认（fail-closed）。"""
    from agent.runtime.connectors.doris import _grant_statements
    from serving.control.sources import _grants_are_read_only

    cursor = _GrantCursor(
        _DORIS_GRANT_COLUMNS, [_doris_grant_row(database_privs="atlas.dwd: Load_priv")]
    )
    statements = _grant_statements(cursor)
    assert statements == ("GRANT LOAD_PRIV ON atlas.dwd",)
    assert _grants_are_read_only(statements) is False


def test_grant_statements_table_shape_roles_column_fails_closed() -> None:
    """Roles 非空：role 权限不在 Privs 列展开（真实观察）→ 保留条目、不确认。

    宁可无法确认只读（blocked），不能因 role 权限不可见而凭空声称 read_only。
    """
    from agent.runtime.connectors.doris import _grant_statements
    from serving.control.sources import _grants_are_read_only

    cursor = _GrantCursor(_DORIS_GRANT_COLUMNS, [_doris_grant_row(roles="smoke_role")])
    statements = _grant_statements(cursor)
    assert statements
    assert _grants_are_read_only(statements) is False


def test_grant_statements_table_shape_unknown_privilege_column_fails_closed() -> None:
    """未识别的 *Privs 列非空 → 不猜测语义：保留条目、判定不确认（新列不 fail-open）。"""
    from agent.runtime.connectors.doris import _grant_statements
    from serving.control.sources import _grants_are_read_only

    cursor = _GrantCursor(
        (*_DORIS_GRANT_COLUMNS, "FuturePrivs"),
        [(*_doris_grant_row(), "atlas.dwd: Select_priv")],
    )
    statements = _grant_statements(cursor)
    assert statements
    assert _grants_are_read_only(statements) is False


def test_grant_statements_statement_shape_still_supported() -> None:
    """MySQL / 旧版 Doris 的语句文本形态：原样提取（现有行为回归保护）。"""
    from agent.runtime.connectors.doris import _grant_statements

    cursor = _GrantCursor(
        ("Grants for atlas_ro@%",),
        [("GRANT SELECT_PRIV ON atlas.dwd.* TO 'atlas_ro'@'%'",)],
    )
    assert _grant_statements(cursor) == (
        "GRANT SELECT_PRIV ON atlas.dwd.* TO 'atlas_ro'@'%'",
    )

