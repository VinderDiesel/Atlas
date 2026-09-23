"""工作台测试装配：真实 API/Agent/Guard，仅替换外部执行器与后台线程。

能力按任务增加：T01 只读清单；T02 控制库/不可变制品；T05c 起 /runs 提交通道
（发布夹具、同步任务执行器、可变时钟）——不伪造尚未实现的接口。
"""

from __future__ import annotations

import ipaddress
import json
import secrets
import shutil
import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import yaml
from fastapi import Header, HTTPException
from fastapi.testclient import TestClient
from httpx import Response

from agent.compiler import SemanticModel
from agent.graph import DataAgent
from agent.llm_policy import LlmConfig
from agent.security.sql_guard import Budget
from data.identity import git_full_sha
from serving.api import API_PREFIX, create_app
from serving.audit import AUDIT_FILENAME, AuditLog
from serving.auth import AuthError, require_bearer, sign_token, verify_token
from serving.control.sources import SourceAllowlist
from serving.ratelimit import RateLimiter

if TYPE_CHECKING:
    from agent.runtime.connectors.base import ProbeObservation
    from serving.control.auth import Principal
    from serving.control.contracts import RunEventRecord, RunRecord, RunRequest
    from serving.control.runs import RunAgent

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS_PATH = f"{API_PREFIX}/runs"
SESSIONS_PATH = f"{API_PREFIX}/sessions"
SOURCES_PATH = f"{API_PREFIX}/manage/sources"
DEPLOYMENTS_PATH = f"{API_PREFIX}/manage/deployments"
DRAFTS_PATH = f"{API_PREFIX}/manage/drafts"
RELEASES_PATH = f"{API_PREFIX}/manage/releases"
RELEASE_IMPORTS_PATH = f"{RELEASES_PATH}/imports"
# 可变测试时钟起点（UTC）：窗口/计时类断言由测试显式推进，不依赖真实等待。
DEFAULT_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)

# T05c 控制面授予（ATLAS_CONTROL_GRANTS 同源格式）：viewer/other/operator 授
# finance；retailer 只有 retail（提交 finance 部署 = 作用域未授权）；outsider
# 不在表内 = 零授予（fail-closed 的 403 基线）。T07c 增 publisher（发布动作只
# 授 finance，用于「可读部署指针但不得创建」）与 ops_retail（operator 能力、
# 只授 retail，用于作用域裁剪的对称面）。T08a 增 editor/editor2/reviewer（草稿
# 编辑/审核分权与对象 ACL）与 editor_retail（editor 能力、只授 retail，域门对称面）。
# T08b 增 publisher_retail（publisher 能力、只授 retail，发布域门对称面：可发布
# 本域的制品，对 finance 草稿/部署一律 403）。
CONTROL_GRANTS = (
    "viewer|viewer|finance;other|viewer|finance;operator|operator|finance;retailer|viewer|retail"
    ";publisher|publisher|finance;ops_retail|operator|retail"
    ";editor|editor|finance;editor2|editor|finance;reviewer|reviewer|finance"
    ";editor_retail|editor|retail;publisher_retail|publisher|retail"
)
# T07 源凭据测试值（env 引用指向的 JSON 形态）：服务层解析后只在建连使用，
# 永不回显——断言 "sup3r-secret-pw" 不出现在任何响应文本里。
SOURCE_TEST_SECRET = json.dumps(
    {"host": "127.0.0.1", "port": 9030, "user": "atlas_ro", "password": "sup3r-secret-pw"}
)
# 测试身份（sign_token 的 subject）：与 CONTROL_GRANTS 条目一致，未登记一律拒绝。
# editor2：同域第二编辑（对象 ACL —— 只可读同域草稿，不可改他人草稿）。
TEST_ACTORS = frozenset(
    {
        "viewer",
        "other",
        "operator",
        "outsider",
        "retailer",
        "publisher",
        "ops_retail",
        "editor",
        "editor2",
        "reviewer",
        "editor_retail",
        "publisher_retail",
    }
)


def bundle_files() -> dict[str, bytes]:
    """从 git HEAD 读取真实配置样例字节（制品装配输入，不代表发布审核通过）。

    已提交文件以 HEAD 为准（夹具 = 已提交内容，T01）；本分支新增未提交文件回退
    工作树，提交后自动转为 HEAD 冻结。
    """
    paths = [
        "semantic/ossie/atlas_finance.ossie.yaml",
        "semantic/synonyms/zh_cn.yml",
        "semantic/synonyms/en_us.yml",
        "semantic/synonyms/patterns_zh_cn.yml",
        "semantic/synonyms/patterns_en_us.yml",
        "semantic/policies/row_policy.yml",
        "agent/prompts/generator_plan.yaml",
        "agent/flows/templates/query.json",
        "agent/flows/templates/analysis.json",
    ]
    return {path: _source_bytes(path) for path in paths}


def _source_bytes(path: str) -> bytes:
    committed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"HEAD:{path}"], capture_output=True
    )
    if committed.returncode == 0:
        return committed.stdout
    return (REPO_ROOT / path).read_bytes()


def write_bundle(root: Path, files: dict[str, bytes], *, revision: str = "1") -> str:
    """在 tmp 目录写出可装载制品；随后必须用 load_bundle 校验后才能算装配成功。

    与 HTTP 导入（serving.control.releases）共用同一落盘原语 `persist_bundle`：
    夹具与生产写出的制品字节/清单结构一致，不另立第二套写盘逻辑。
    """
    from agent.runtime.bundle import build_manifest, persist_bundle, runtime_code_sha

    manifest = build_manifest(
        files,
        source_git_sha=runtime_code_sha(),
        runtime_code_sha=runtime_code_sha(),
        source_revision=revision,
        eval_evidence_ids=(),
    )
    persist_bundle(root, files, manifest)
    return manifest.release_id


class ExecutorSpy:
    """只替外部数据库调用，调用前的 Planner/Compiler/Guard 仍为生产实现。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.rows: list[tuple[object, ...]] = [(1,)]
        self.columns = ["v"]
        self.failure: Exception | None = None

    def __call__(self, sql: str) -> tuple[list[tuple[object, ...]], list[str]]:
        self.calls.append(sql)
        if self.failure is not None:
            raise self.failure
        return list(self.rows), list(self.columns)


class ProbeSpy:
    """只替外部探测连接：目标校验与判定仍为生产实现（与 ExecutorSpy 同纪律）。

    `observation` 是成功路径的默认观察；`failure` 驱动异常映射场景（TLS/授权）。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self.observation: Any = None
        self.failure: Exception | None = None

    def __call__(self, target: Any, spec: Any) -> Any:
        self.calls.append((target, spec))
        if self.failure is not None:
            raise self.failure
        return self.observation


class SyncTaskRunner:
    """同步测试任务执行器：任务先排队，`drain()` 时按 FIFO 执行。

    只替「后台线程」这一个外部时序事实：入队调用与生产单业务队列同构，
    执行时机由测试显式驱动（无 sleep、无竞态）。
    """

    def __init__(self) -> None:
        self._pending: list[Callable[[], None]] = []

    def submit(self, task: Callable[[], None]) -> None:
        self._pending.append(task)

    def drain(self) -> None:
        """执行全部已排队任务（执行过程中新入队的任务同批执行到空）。"""
        while self._pending:
            task = self._pending.pop(0)
            task()


class WorkbenchHarness:
    """临时目录装配真实应用；测试身份只在本工具内生成，不读取开发者密钥。

    控制面（T05c）：控制库落在临时目录 `state/control.sqlite`，任务执行器换
    同步实现（drain 驱动），时钟可变（`now`）——`restart()` 只丢进程内状态
    （代理缓存/队列/内存窗口），磁盘状态与 executor_spy 保留。
    """

    def __init__(self, root: Path, *, now: datetime | None = None) -> None:
        self.root = root
        self.now = now if now is not None else DEFAULT_NOW
        self.executor_spy = ExecutorSpy()
        self.audit_path = root / "audit" / AUDIT_FILENAME
        self.audit = AuditLog(root / "audit")
        self.capture_audit_failure: Exception | None = None  # T05d 审计 fail-closed 注入位
        self._agents: dict[str, DataAgent] = {}
        self._run_agents: dict[str, DataAgent] = {}
        self._secret = secrets.token_urlsafe(32)
        self._tokens: dict[str, str] = {}
        self._bundles = root / "bundles"
        self.bundles_root = self._bundles
        # T08b 发布源仓库：隔离 tmp Git 仓库——导入只接受显式 commit，夹具在
        # 这里真提交（不碰产品仓库的工作树/索引/引用）。
        self.release_repo = root / "release-repo"
        self._revision = 0
        self._deployed = False
        # T07 源接入探测替身与可注入环境：目标校验/判定链走真实生产代码，
        # 只有「连出去」这一步被替身代替（与 ExecutorSpy 同纪律）。
        self.probe_spy = ProbeSpy()
        self.probe_spy.observation = self.probe_observation()
        self.source_environment: dict[str, str] = {"ATLAS_TEST_DORIS": SOURCE_TEST_SECRET}
        self.source_dns: dict[str, list[str]] = {}
        self.source_allowlist = SourceAllowlist.parse("127.0.0.1:9030")
        self._build()

    def _clock(self) -> datetime:
        return self.now

    def _audit_capture(self, principal: Principal, request: RunRequest) -> None:
        """capture 授权审计（T05d）：写失败向上抛 → RunService 以 fail-closed 拒绝提交。

        注入位 `capture_audit_failure` 驱动「审计故障 → 下一次 SQL 不得启动」的验收
        （D07）；生产装配在 serving/api.py，审计行与业务面同格式（bucket=business）。
        """
        if self.capture_audit_failure is not None:
            raise self.capture_audit_failure
        self.audit.record(
            endpoint=RUNS_PATH,
            claims=principal.user_context,
            kind="capture_authorized",
            bucket="business",
        )

    def _build(self) -> None:
        """（重）建控制面与 HTTP 客户端：磁盘状态复用，进程内缓存为零。"""
        from serving.control.auth import parse_control_grants
        from serving.control.deployments import DeploymentService
        from serving.control.drafts import DraftService
        from serving.control.feedback import FeedbackService
        from serving.control.releases import ReleaseService
        from serving.control.router import ControlServices
        from serving.control.runs import RunService
        from serving.control.sources import SourceService
        from serving.control.store import ControlStore

        self.control_store = ControlStore(self.root / "state" / "control.sqlite")
        self.control_store.migrate()
        self.runner = SyncTaskRunner()
        self.runs = RunService(
            self.control_store,
            agent_resolver=self._run_agent,
            runner=self.runner,
            clock=self._clock,
            capture_audit=self._audit_capture,
        )
        # 控制库里的遗留 queued/running 一律封存（重启不自动重跑，D07）
        self.runs.recover_interrupted()
        self.control_services = ControlServices(
            store=self.control_store,
            runs=self.runs,
            feedback=FeedbackService(self.control_store),
            sources=SourceService(
                self.control_store,
                probe_runner=self.probe_spy,
                environ=self.source_environment,
                allowlist=self.source_allowlist,
                resolver=self.source_resolve,
                clock=self._clock,
            ),
            grants=parse_control_grants(CONTROL_GRANTS),
            deployments=DeploymentService(self.control_store),
            drafts=DraftService(self.control_store),
            releases=ReleaseService(
                self.control_store,
                bundle_root=self.bundles_root,
                repo_root=self.release_repo,
            ),
        )
        self.client = TestClient(
            create_app(
                agent_factory=self._agent,
                audit=self.audit,
                rate_limiter=RateLimiter(max_requests=10_000, window_seconds=60),
                governance_rate_limiter=RateLimiter(max_requests=10_000, window_seconds=60),
                llm_config=LlmConfig(),
                control_services_factory=lambda: self.control_services,
            )
        )
        self.client.app.dependency_overrides[require_bearer] = self._authenticate

    def _load_model(self, domain: str) -> SemanticModel:
        """从 git HEAD 读取该域语义模型（测试夹具 = 已提交事实，T01 口径）。"""
        relative = f"semantic/ossie/atlas_{domain}.ossie.yaml"
        content = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", f"HEAD:{relative}"],
            check=True,
            capture_output=True,
        ).stdout
        model_path = self.root / relative
        model_path.parent.mkdir(parents=True, exist_ok=True)
        model_path.write_bytes(content)
        return SemanticModel(model_path)

    def _budget(self, model: SemanticModel) -> Budget:
        return Budget(
            dialect="doris",
            max_rows=10_000,
            allowed_tables=frozenset(ds.source for ds in model.datasets.values()),
        )

    def _agent(self, domain: str) -> DataAgent:
        if domain not in self._agents:
            model = self._load_model(domain)
            self._agents[domain] = DataAgent(
                model=model, executor=self.executor_spy, budget=self._budget(model)
            )
        return self._agents[domain]

    def _run_agent(self, record: RunRecord) -> RunAgent:
        """按部署 scope 解析运行代理；真实事件 sink 接入控制库（无外部数据库）。"""
        from serving.control.events import StoreEventSink
        from serving.control.runs import RunAgent

        domain = record.scope
        agent = self._run_agents.get(domain)
        if agent is None:
            model = self._load_model(domain)
            agent = DataAgent(
                model=model,
                executor=self.executor_spy,
                budget=self._budget(model),
                event_sink=StoreEventSink(self.control_store),
            )
            self._run_agents[domain] = agent
        # 测试代理未绑定运行时快照：数据身份如实为 None（不假称已绑定，D03）
        return RunAgent(agent=agent, data_identity=None)

    def _authenticate(self, authorization: str | None = Header(default=None)) -> dict[str, object]:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401)
        try:
            return verify_token(authorization.removeprefix("Bearer ").strip(), secret=self._secret)
        except AuthError as exc:
            raise HTTPException(status_code=401) from exc

    def request(
        self, method: str, path: str, *, actor: str | None = "viewer", **kwargs: Any
    ) -> Response:
        """经过实际路由；actor=None 不附身份，未知 actor 拒绝而非默认提权。"""
        headers = dict(kwargs.pop("headers", {}))
        if actor is not None:
            if actor not in TEST_ACTORS:
                raise ValueError(f"未注册测试身份：{actor}")
            if actor not in self._tokens:
                self._tokens[actor] = sign_token("hq_admin", {}, secret=self._secret, subject=actor)
            headers.setdefault("Authorization", f"Bearer {self._tokens[actor]}")
        return self.client.request(method, path, headers=headers, **kwargs)

    def seed_release(self, *, active: bool = True) -> str:
        """从真实模板/语义制品（git HEAD 字节）登记测试发布；返回 release_id。

        active=True 时激活为金融部署的当前发布（注册与激活是两步显式动作，
        每次调用产生新 release_id——供「运行期间切换发布」用例使用）。
        """
        from agent.runtime.bundle import load_bundle
        from serving.control.contracts import Owner

        publisher = Owner(issuer="atlas-local", subject="publisher")
        self._revision += 1
        release_id = write_bundle(
            self._bundles, bundle_files(), revision=str(self._revision)
        )
        self.control_store.register_release(
            load_bundle(self._bundles, release_id),
            owner=publisher,
            scope="finance",
            source_id="doris",
        )
        if not self._deployed:
            self.control_store.create_deployment(
                "finance", owner=publisher, scope="finance", source_id="doris"
            )
            self._deployed = True
        if active:
            current = self.control_store.active_manifest("finance")
            self.control_store.activate(
                "finance", release_id, expected=current.release_id if current else None
            )
        return release_id

    def source_fixture(self, **overrides: Any) -> dict[str, Any]:
        """合法源接入 body（T07 工作卡；字段与 D03 SourceSpec 逐字对应）。

        缩窄场景用 overrides 覆盖单字段（如注入 `password` 验证明文凭据被拒）；
        `secret_ref` 只收 `env:<NAME>` 引用，测试不连真实环境变量。
        """
        body: dict[str, Any] = {
            "source_id": "doris-primary",
            "revision": "rev-2026-09-22-1",
            "connector_kind": "doris",
            "secret_ref": "env:ATLAS_TEST_DORIS",
            "allowed_catalogs": ["atlas"],
            "allowed_tables": ["atlas.dwd.fact_trades", "atlas.dwd.dim_account"],
            "timezone": "+08:00",
            "tls_policy": "disabled",
            "query_budget": 10_000,
        }
        body.update(overrides)
        return body

    def deployment_fixture(self, **overrides: Any) -> dict[str, Any]:
        """合法部署创建 body（T07c；D13：新部署在首次发布前 active_release_id=null）。

        source 必须已配置（先经 POST /manage/sources）——测试用 overrides 覆盖
        deployment_id/scope/source_id 验证冲突、作用域与源存在性。
        """
        body: dict[str, Any] = {
            "deployment_id": "finance-live",
            "scope": "finance",
            "source_id": "doris-primary",
        }
        body.update(overrides)
        return body

    def draft_fixture(
        self, kind: str = "semantic", *, domain: str = "finance", **overrides: Any
    ) -> dict[str, Any]:
        """合法语义草稿创建 body（T08a；target 命中制品白名单、document 取真实模型）。

        document 来自 git HEAD 的 atlas_<domain>.ossie.yaml 解析结果（与 T01 夹具
        同口径：夹具 = 已提交事实）；缩窄场景用 overrides 覆盖 scope/content/
        kind 验证形状门与作用域门，不构造无法导出的假草稿。
        """
        document = yaml.safe_load(_source_bytes(f"semantic/ossie/atlas_{domain}.ossie.yaml"))
        assert isinstance(document, dict)
        body: dict[str, Any] = {
            "kind": kind,
            "scope": domain,
            "content": {
                "target": f"semantic/ossie/atlas_{domain}.ossie.yaml",
                "document": document,
            },
        }
        body.update(overrides)
        return body

    def release_files(
        self,
        *,
        target: str,
        document: Mapping[str, Any],
        replace: Mapping[str, bytes] | None = None,
    ) -> dict[str, bytes]:
        """发布夹具文件集：HEAD 真实白名单文件字节 + 目标文件替换为草稿版本。

        document 按仓库风格序列化（sort_keys=False）——与草稿校验/导出口径一致；
        replace 用于注入白名单内的额外文件（值域快照）或整文件破坏（策略丢失）。
        """
        files = dict(bundle_files())
        files[target] = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode(
            "utf-8"
        )
        files.update(replace or {})
        return files

    def commit_release_tree(
        self,
        files: Mapping[str, bytes],
        *,
        symlinks: Mapping[str, str] | None = None,
        message: str = "release-fixture",
    ) -> str:
        """在隔离 tmp Git 仓库提交发布夹具，返回完整 commit sha。

        每次提交重建整个工作树（不增量）——保证「本次夹具 == 本次参数」；产品
        仓库的工作树/索引/引用零接触（发布源是显式 commit，不是脏树）。
        symlinks：路径 → 目标，以 Git 符号链接模式（120000）记录，用于拒绝用例。
        """
        links = dict(symlinks or {})
        repo = self.release_repo
        if not (repo / ".git").exists():
            repo.mkdir(parents=True, exist_ok=True)
            self._git_repo("init", "-q")
        for entry in repo.iterdir():
            if entry.name == ".git":
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        for name, content in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            link_target = links.get(name)
            if link_target is not None:
                path.symlink_to(link_target)
            else:
                path.write_bytes(content)
        self._git_repo("add", "-A")
        self._git_repo(
            "-c",
            "user.email=atlas-test@invalid",
            "-c",
            "user.name=atlas-test",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            message,
        )
        # 取 sha 复用集中式 `git_full_sha`，不自造 HEAD 解析副本（ADR-0019 判据 5(a)：
        # 该命令在测试侧只允许裁定的 2 处独立预言机）；不开 ATLAS_GIT_SHA 注入路径——
        # 发布源必须是夹具仓库的真实 commit，不是容器身份。
        return git_full_sha(self.release_repo)

    def _git_repo(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        """在发布源夹具仓库执行 git（夹具错误立即暴露，不静默继续）。"""
        return subprocess.run(
            ["git", "-C", str(self.release_repo), *args], capture_output=True, check=True
        )

    def probe_observation(self, **overrides: Any) -> ProbeObservation:
        """成功探测的默认观察（覆盖单字段驱动 metadata_missing / 只读判定场景）。

        `tables_seen`/`catalogs_seen` 覆盖夹具白名单；`grant_statements` 默认
        只含 SELECT_PRIV（只读确认的正例）。
        """
        from agent.runtime.connectors.base import ProbeObservation

        values: dict[str, Any] = {
            "engine_version": "4.1.0-test",
            "grant_statements": ("GRANT SELECT_PRIV ON atlas.* TO 'atlas_ro'@'%'",),
            "catalogs_seen": ("atlas",),
            "tables_seen": ("atlas.dwd.fact_trades", "atlas.dwd.dim_account"),
            "queries_run": (
                "select_version",
                "show_grants",
                "catalog_databases",
                "catalog_tables",
            ),
        }
        values.update(overrides)
        return ProbeObservation(**values)

    def source_resolve(self, host: str) -> list[str]:
        """测试 DNS：IP 字面量直通；域名查 source_dns（未登记 = 解析失败）。

        与生产解析器同契约（返回地址列表；失败抛 OSError），服务层的硬禁/
        允许列表校验与「用校验过的 IP 建连」因此走同一条真实代码路径。
        """
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if host not in self.source_dns:
                raise OSError(f"测试 DNS 未登记：{host}") from None
            return list(self.source_dns[host])
        return [host]

    def submit_query(
        self,
        question: str,
        *,
        actor: str = "viewer",
        deployment_id: str = "finance",
        client_request_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """经真实 /api/v1/runs 提交问句并返回 run_id（202 形状在调用处断言）。"""
        body: dict[str, Any] = {
            "deployment_id": deployment_id,
            "mode": "ask",
            "question": question,
            "client_request_id": client_request_id or uuid4().hex,
        }
        if session_id is not None:
            body["session_id"] = session_id
        response = self.request("POST", RUNS_PATH, actor=actor, json=body)
        assert response.status_code == 202, response.text
        return str(response.json()["run_id"])

    def drain(self) -> None:
        """等待注入的同步测试任务执行器完成（真实 /runs 已入队）。"""
        self.runner.drain()

    def events(self, run_id: str) -> list[RunEventRecord]:
        """读该 run 的真实事件流（控制库直读；HTTP 时间线属 T06）。"""
        return self.control_store.list_events(run_id)

    def restart(self) -> None:
        """模拟进程重启：丢弃进程内状态（代理缓存/队列/内存窗口），磁盘复用。"""
        # 旧客户端的 lifespan/portal 随其 exit_stack 关闭（starlette TestClient
        # 的 exit_stack 在 __enter__ 中创建）——不能只 close()（那是 httpx 层）
        self.client.__exit__(None, None, None)
        self._agents.clear()
        self._run_agents.clear()
        self._build()
        # 重建的客户端必须重新进入上下文，否则 harness 退出时无 exit_stack 可关
        self.client.__enter__()

    def __enter__(self) -> WorkbenchHarness:
        self.client.__enter__()
        return self

    def __exit__(
        self,
        typ: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.client.__exit__(typ, value, traceback)
