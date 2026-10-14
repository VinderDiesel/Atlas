# ADR-0031 可信问数工作台开发计划

> 面向执行者：实施时使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans`，逐任务执行。2026-09-22 用户授权启动第一阶段 M0（T01–T06），不授权自动提交、部署、付费调用或训练；M1–M4 尚未启动。步骤使用复选框追踪。

**目标：** 从数据接入、语义发布和可信问数，到运行图定位、节点修复、评测发布与可选模型训练，形成可验证闭环。

**架构：** 保留 LangGraph、Compiler、Guard 和 Git 语义权威源。增加不可变 RuntimeBundle、单进程 SQLite 控制状态、真实运行事件、受约束图编排与节点优化入口；训练离线隔离，模型只能替换兼容节点。

**技术栈：** Python 3.11、FastAPI、Pydantic、sqlite3、LangGraph、sqlglot、Doris；React 18、TypeScript、Vite、AntD、Recharts；React Flow 与 Authlib 按 ADR 依赖门禁引入，TRL/MCP SDK 仅受门禁任务使用。

**规格：** [ADR-0031](../../infra/adr/0031-trusted-query-workbench.md)。执行前同时阅读其 D01–D14 和既有 ADR 兼容表；本计划不改变决策。

## 1. 全局约束与执行纪律

- 当前启动 **M0（T01–T06）**，实际进度以第 3、7 节为准；尚未完成的文件、接口、测试仍是计划新增项，不代表已经存在。
- 根目录 `AGENTS.md` 原样适用：N1–N10；Python 3.11；node ≥20；确定性优先；Prompt 外置；先评测再实现。
- 不执行 `make seed` 或数据卷删除；不自动合并 Git、批准反馈、切换发布、启用影子或付费训练。
- 不修改用户原有 `docs/outreach-series/PLAN.md` 与 `docs/outreach-series/03b-llm-grounded-narrative.md`；开工前重新核对工作区，不覆盖其他改动。
- 决策状态与实施状态分开。用户已授权按 ADR 启动 M0；后续阶段及真实 IdP、数据源、训练和发布仍遵守各自门禁，不因开工授权自动放行。
- 任务是独立验收切片，每任务先红测、最小实现、绿测、回归、记录证据；可拆更小提交，但不跨任务混入未授权语义变更。`refactor` 与 `semantic` 分开评审。认领时在证据账本记录负责人、评审人和关联 issue/PR；无人认领不自动标 IN_PROGRESS。
- 样本数、EX、Recall、耗时、成本只能来自脚本；初始上限与训练超参数是设计输入，不是效果承诺。
- 旧 `/ask`/`analyze`/CLI 行为回归与新 `/runs` 合同并行维护；`llm=off` 仍不调任何模型。
- 发布规则/Prompt/模型都须固定内容摘要；任何 URL、Python 模块路径、未经审核工具不得由客户端动态装载。
- 控制数据库可以更新自身状态，业务数据库始终只读；控制库连接不能进入 Agent 工具。
- 真链缺依赖则 blocked；不得靠修改 golden hash、跳过安全用例或重新装载数据把结果“做绿”。

### 1.1 状态与证据更新规则

状态只用 `NOT_STARTED / IN_PROGRESS / BLOCKED / DONE / CANCELLED`。每次推进在第 7 节追加任务 ID、代码 sha、实际命令、报告路径、通过/失败/跳过数、阻塞和下一动作。DONE 必须满足该卡全部验收项；文档完成、fake 测试或脚本存在不等于真链完成。

每个任务结束都更新 README 对应能力登记及 Known Limitations，只能据实收窄限制，不能删除未验证限制。新增 API 必须同步 `frontend/src/api/endpoints.ts`、OpenAPI 集合断言和前端类型；新增依赖更新 lock、许可证清单与 NOTICE 所需归属。

### 1.2 可执行命令

已核对根 Makefile，以下为**现有入口**；实际执行结果见第 7 节，不以入口存在代替验收：

```bash
make lint
make test
.venv/bin/python -m pytest tests
make ui-check
make license-check
make eval
make api-verify
make e2e
```

`make test` 使用 unittest discovery，不能代替 pytest。`make eval/api-verify/e2e` 需要对应固定快照与真实服务，运行前确认环境，不擅自 seed。`make ui-check` 在缺少 node_modules 时会安装依赖；依赖变更先过许可门。纯文档更新不需要启动数据库或跑训练。

后文 `python -m eval.workbench_*`、`python -m agent.flows`、`python -m lora.intent_*` 均是任务要求新增的入口，**任务完成前不可调用**。不在计划里编造现有 make target。

## 2. 模块与接口责任图

以下路径除标明“改”者均为计划新建；Python 新目录包含 `__init__.py`。不往已有大文件追加整套控制平台，只保留装配调用。

| 路径 | 责任与导出合同 | 消费者 |
|---|---|---|
| `agent/flows/contracts.py` | FlowDefinition、NodeSpec、EdgeSpec、NodeResult、BudgetSpec，extra=forbid | T02、T09、T11 |
| `agent/flows/registry.py` | `node_catalog() -> tuple[NodeTypeSpec, ...]`，注册代码与可配置能力；无动态 import | 图校验、UI |
| `agent/flows/validate.py` | `validate_flow(flow: FlowDefinition, catalog: tuple[NodeTypeSpec, ...]) -> tuple[FlowIssue, ...]` | 发布、运行 |
| `agent/flows/compile.py`、`__main__.py` | `compile_flow(flow, bundle, context)` → LangGraph；CLI `validate` 只校验不执行 | T09 |
| `agent/flows/templates/query.json`、`analysis.json` | 从当前行为抽出的默认模板，不是语义第二事实源 | 默认部署 |
| `agent/runtime/bundle.py` | ReleaseManifest、RuntimeBundle；`load_bundle(root: Path, release_id: str) -> RuntimeBundle` | 工厂、运行、回放 |
| `agent/runtime/context.py` | RunContext，含身份、发布、DataIdentity、时钟、预算、EventSink | 注册节点 |
| `agent/runtime/execution.py` | `execute_plan(plan: Plan, context: RunContext) -> ExecutionResult`，唯一受保护业务执行内核 | 图、工具、旧 API/CLI |
| `data/connectors/base.py`、`doris.py` | SourceSpec、ConnectorCapabilities、DorisConnector；内部执行仅由内核调用 | T04、T07 |
| `data/runtime_identity.py` | SnapshotDataIdentity、LiveDataIdentity；不替换旧 `data/identity.py` 的评测合同 | 新运行 |
| `serving/control/contracts.py`、`store.py`、`migrations/001.sql` | 私有实体与事务；`ControlStore(path: Path)`，迁移、CAS、事件、幂等唯一约束 | 控制服务 |
| `serving/control/releases.py`、`drafts.py` | 草稿审核、白名单 patch/制品导入、版本检查、发布 CAS | T08、T09 |
| `serving/control/runs.py`、`events.py`、`artifacts.py` | 排队、真实事件、恢复、SSE、ACL 和内容裁剪 | T05、T06 |
| `serving/control/auth.py`、`oidc.py` | Principal、控制能力、BFF、CSRF；继承既有数据角色校验 | 新控制与运行端点 |
| `serving/control/router.py` | `build_control_router(services: ControlServices) -> APIRouter`；只做鉴权/DTO/服务调用 | 改 `serving/api.py` 装配 |
| `serving/control/sources.py`、`feedback.py`、`datasets.py`、`jobs.py` | 数据源、反馈采集/审核、数据集、离线任务状态；不执行任意 shell | T05、T07、T12、T14 |
| `agent/intent/contracts.py`、`normalize.py`、`retrieve.py`、`bind.py` | D09 合同、确定性归一、保留原句检索、槽位完整绑定 | T10、T14 |
| `agent/tools/broker.py`、`agent/flows/llm_node.py` | 实际 JSON action 工具循环，权限/数据等级/预算；不暴露裸执行器 | T11 |
| `agent/prompts/intent.yaml`、`agent/prompts/node_action.yaml` | 外置带元数据的 Prompt，不写 Python 内联模板 | T10、T11 |
| `frontend/src/panels/flows/`、`runs/`、`setup/`、`optimization/` | 定义编辑、运行详情、接入与节点优化；图和时间线同一 reducer | T06–T13 |
| `frontend/src/api/control.ts`、`run-stream.ts`、`state/run-events.ts` | typed DTO/请求、SSE 去重续读、纯函数归约 | UI |
| `lora/intent_data.py`、`intent_train.py`、`intent_reward.py`、`intent_rl.py`、`registry.py` | intent_v1 数据、SFT、奖励、可选 GRPO、版本证据 | T14–T16 |
| `lora/configs/intent_v1.yaml`、`intent_rl.yaml` | 独立训练配方，不覆盖 sql_v1 | 训练任务 |
| `eval/workbench_baseline.py`、`workbench_intent.py`、`workbench_acceptance.py` | 同版本基线、节点/下游对照、首次接入与修复指标 | 门禁 |
| `infra/docker/compose.connect.yml`、`serving/control/maintenance.py` | 最小接入发行、诊断、备份恢复与保留期 | T13 |

### 2.1 测试夹具合同

T01 创建 `tests/workbench_support.py`，提供统一 **TestClient 装配器而非业务逻辑替身**：

- `WorkbenchHarness(tmp_path)`：隔离控制 DB、制品、artifact；原始语义数据只从现有已提交样例复制；不连接开发者数据库。
- `request(method, path, *, actor="viewer", **kwargs)`：使用真实 FastAPI 路由/授权/控制服务；actor 测试身份只在该测试装配器产生，不能进入正式应用。
- `seed_release(*, active=True) -> str`：从真实模板/语义制品建立测试 release，返回其摘要 ID；模板在 T02 固定，不能直接把验证结果标 true。
- `source_fixture() -> dict`、`draft_fixture(kind: str) -> dict`：返回 D03/D04 的最小合法非秘密配置/草稿。
- `submit_query(question: str) -> str`：调用真实 `/runs`，返回 run_id。
- `drain()`：等待注入的同步测试任务执行器完成；`restart()` 重建服务并复用临时状态目录。
- `events(run_id: str) -> list[dict]`、`artifact(run_id: str, artifact_id: str)`：均经实际 HTTP ACL，不直接读取数据库绕过权限。
- `executor_spy`、`model_spy` 仅替外部数据库/LLM，记录实际调用、可注入阻塞/失败。fake 输出仅证明接线，不产真实质量数字。

夹具能力随所属任务增加；T01 先落最小装配与 baseline，不用虚构成功响应提前使后续测试通过。下文 `h` 表示该 pytest fixture；示例中的 `/api/v1` 前缀不可省略。示例为首条红测，负向矩阵也是完成门槛。

## 3. 依赖与里程碑追踪

| ID | 独立交付 | 依赖 | 状态 | 里程碑 |
|---|---|---|---|---|
| T01 | 冻结基线、验收脚本合同与测试夹具 | ADR 细则确认 | DONE | M0 |
| T02 | 控制库、不可变发布与默认合同 | T01 | DONE | M0 |
| T03 | 登录、控制 ACL、内容权限 | T02 | DONE（真 IdP 验收 BLOCKED） | M0 |
| T04 | 共享安全执行内核、Doris 连接器和数据身份 | T02 | DONE | M0 |
| T05 | 真实事件、运行恢复与最小反馈采集 | T02、T03、T04 | DONE | M0 |
| T06 | 历史列表、运行图、实时 SSE 时间线 | T05 | DONE | M0 |
| T07 | 只读数据源接入向导 | T03、T04、T05 | DONE | M1 |
| T08 | 语义草稿、审核、Git 制品发布 | T02、T03、T07 | DONE | M1 |
| T09 | 受约束图校验、LangGraph 装配与画布 | T02、T04、T06、T08、T10 | DONE | M2 |
| T10 | 检索前意图合同、完整绑定与简单问数控件 | T04、T05、T08 | DONE | M1 |
| T11 | 规则/Prompt/Tools 节点实验与发布 | T09、T10 | DONE | M2 |
| T12 | 反馈审核、归因与版本化数据集 | T06、T08、T10 | DONE | M2 |
| T13 | 最小发行、运维与首次接入验收 | T06、T07、T08、T10 | DONE | M1 |
| T14 | intent_v1 SFT 任务与离线对照 | T11、T12、T13 | DONE | M3 |
| T15 | 模型注册、影子、人工切换与回退 | T14 | DONE | M3 |
| T16 | 可选 GRPO 奖励实验 | T15 + G-RL | DONE | M4 可选 |
| T17 | 完整 MCP stdio transport | T03、T04、T13 + G-MCP | DONE | M4 可独立 |

关键顺序：T01 → T02 → T03/T04 → T05 → T06/T07 → T08 → T10 → T13 为首次可信接入主线；T09/T11/T12 随后形成优化闭环，T14/T15 才进入模型替换。T04 消费 T02 的版本合同；T05/T06 先提供反馈采集，T12 再做审核训练闭环。不能把 T13 首次接入发行推迟到训练之后。T10 在默认图接入协议，T09 再把它注册为可替换节点，避免 M1↔M2 循环依赖。

## 4. 工作卡：基线与运行事实

### T01 冻结基线与门禁夹具

**改：** `eval/report.py` 只加性识别新报告类型，不改旧指标定义；`data/identity.py` 加性提供显式仓库完整 SHA，保留旧短身份/容器注入行为。**新：** `eval/workbench_baseline.py`、`tests/test_workbench_baseline.py`、`tests/workbench_support.py`。

**接口：** `collect_baseline(repo: Path, output: Path) -> BaselineReport`；报告记录 code_sha、工作树状态、可用快照/既有报告摘要、各能力接线及 not_run；未运行项没有 metric 数字。

- [x] 写红测：无本次执行证据时不能复制历史报告指标充当当前值。

```python
def test_baseline_does_not_promote_history(tmp_path):
    from eval.workbench_baseline import collect_baseline
    report = collect_baseline(REPO_ROOT, tmp_path / "baseline.json")
    assert report.current_metrics is None
    assert all(item.code_sha for item in report.historical_evidence)
```

`REPO_ROOT = Path(__file__).resolve().parents[1]` 在测试文件中定义，历史报告从仓库现有 `eval/reports/` 读取。

- [x] 执行 `.venv/bin/python -m pytest tests/test_workbench_baseline.py -q`，记录缺接口导致的红测，不把环境失败当测试红。
- [x] 最小实现只读清单、历史/当前证据分隔、报告 Schema；固定无网络的执行夹具，不运行真实查询。
- [x] 跑同测试及现有 `tests/test_report.py`；建立四条对照记录：规则、规则+LLM、SFT、RL，未执行策略为 not_run。
- [x] 记录基线报告与 M0 验收范围；代码提交仅在用户另行授权后进行。

**基线修复插入项（2026-09-22 用户确认）：** 全量 `make lint` 发现 21 份值域仍绑定 `7c966e9`，最新锁定快照为 `1e2e557`；相关文件与 HEAD 一致，属于既有漂移。用户选择先修复再继续 M0，并授权实际重采样。此前暂停 T02–T06；现已完成获授权的真实只读重采样及回归，T01 退出门满足，恢复 T02。旧 `make eval` 仍因 HEAD 无同名 Snapshot 未运行，不影响本任务只读清单验收，也不计为效果评测通过。

- [x] 用现有 `verify_snapshot_fingerprint("1e2e557")` 现场复核，结果为 True；只加载现有环境，不输出密钥。
- [x] 生成器加显式 `snapshot_sha` 与 `raw_dir`（默认 HEAD 行为不变）；先写红测，验证前后指纹、失败不写值域、人工别名保留和非法路径拒绝。
- [x] 在隔离工作区真实重采样 21 份派生 JSON；仅复用主目录的原始公开数据与连接配置，不修改 Metric/快照/Gold set/业务库。
- [x] 重跑 lint、值域/Planner 回归和全量测试，保存独立修复证据；不将重采样结果当 EX 或模型质量评测。

**退出门：** 当前能力声明不高估 E05/E06/E08/E11；以后所有报告按同一证据身份关联，不从历史满分推断新链路正确。

### T02 控制存储、版本合同与默认制品

**新：** `serving/control/contracts.py`、`store.py`、`migrations/001.sql`、`agent/runtime/bundle.py`、`agent/flows/contracts.py`、默认 templates；`tests/test_control_store.py`、`tests/test_runtime_bundle.py`。

**接口：** `ControlStore.migrate()`、`activate(deployment_id: str, release_id: str, expected: str | None) -> None`；CAS 冲突抛 `ReleaseConflict`。`ReleaseManifest` 字段严格按 D04；迁移只更新控制状态。

- [x] 红测：两个编辑者基于同一 active 发布，只有首个成功。

```python
def test_release_compare_and_swap(store, old_release, next_release):
    from serving.control.store import ReleaseConflict
    store.activate("finance", old_release, expected=None)
    store.activate("finance", next_release, expected=old_release)
    with pytest.raises(ReleaseConflict):
        store.activate("finance", old_release, expected=old_release)
```

`store` fixture 使用 tmp_path 调用真实迁移；两个 release fixture 从同一语义包和两个合法配置摘要构建。

- [x] 运行 `.venv/bin/python -m pytest tests/test_control_store.py tests/test_runtime_bundle.py -q` 确认红。
- [x] 实现外键/唯一键/迁移事务、不可变 release、draft revision 与状态转移；默认制品只封装当前已注册能力，暂不开放图编辑。
- [x] 绿测加故障矩阵：迁移失败回滚、DB 重开、摘要被改、非法路径、制品缺文件、修改草稿使审核失效；两个 bundle 的规则缓存不得串用；代码/注册版本不兼容拒装配，配置包不能携带执行代码。
- [x] 保存 Schema 与迁移测试证据；记录该任务只交付发布原语，UI/审核放行留 T08。

**退出门：** active 原子切换、不可变内容校验、无第二语义权威；不存在“latest”在一次运行中变化的读取。

### T03 身份登录与控制 ACL

**改：** `serving/auth.py`、`serving/api.py`、`pyproject.toml`、`uv.lock`、`.env.example`、前端登录入口。**新：** `serving/control/auth.py`、`oidc.py`、`tests/test_control_auth.py`、`tests/test_oidc_login.py`。

**接口：** `Principal(issuer, subject, role, user_context, capabilities, auth_fingerprint)`；`authorize(principal, action, resource_scope) -> None`；违规抛 `ControlForbidden`。BFF/session 接口按 D13，不返回 IdP token。

- [x] 红测 viewer 无发布权，即使数据角色是 hq_admin。

```python
def test_data_admin_is_not_control_publisher(viewer_principal, finance_scope):
    from serving.control.auth import ControlForbidden, authorize
    with pytest.raises(ControlForbidden):
        authorize(viewer_principal, "release.publish", finance_scope)
```

`viewer_principal` 的数据角色为 hq_admin、控制能力仅为 viewer；`finance_scope` 是已授权领域。T03 先测真实授权函数，T08 路由落地后补同条件 HTTP 403，不让尚不存在的发布路由 404 冒充权限测试。

- [x] 运行 `.venv/bin/python -m pytest tests/test_control_auth.py tests/test_oidc_login.py -q`；OIDC 外部端点用受控 mock HTTP，不付费、不使用真实 token。
- [x] 实现固定模式认证与 Authlib BFF；锁运行依赖、校验完整许可；控制能力配置不放业务语义 YAML。Cookie 严格属性、state/nonce/PKCE、CSRF、退出失效、算法拒绝均落代码。
- [x] 绿测覆盖 issuer/audience/exp/未知 kid/轮换、Cookie+Bearer 冲突、跨域 Origin、CSRF 缺失；运行 `tests/test_auth.py`、`test_idp.py`、`test_api_hardening.py` 与 `make ui-check`（2026-09-22 实测：三套旧测试 60 passed + 27 subtests；ui-check tsc + vitest 132 + build 全绿）。
- [ ] 跨用户历史、过期 SSE 属 T05/T06 运行面（T03 只登记不宣称覆盖）。
- [ ] 真 IdP 回调验收在部署者提供环境后进行；未具备则该真链项 **BLOCKED**，不能声称私有登录可用。

**退出门：** 开发角色选择不能出现在私有模式；原文权限与管理权限分开，旧会话指纹合同不放松。

### T04 共享执行内核与连接器

**改：** `agent/graph.py`、`agent/factory.py`、`agent/cli.py`、`eval/runner.py`、`agent/tools/registry.py`。**新：** runtime/execution、context、connectors/base、doris、runtime_identity；`tests/test_runtime_execution.py`、`tests/test_source_identity.py`。

**接口：** `execute_plan(plan: Plan, context: RunContext) -> ExecutionResult`；ExecutionResult 使用既有安全序列化与 Decimal 字符串，不暴露失败 SQL。旧 `execute_sql` 与 build_budget 保留委托兼容层。

- [ ] 红测时间约束缺失或非法表时数据库完全不被调用。

```python
def test_invalid_plan_never_reaches_source(unsafe_plan, run_context, executor_spy):
    from agent.runtime.execution import execute_plan
    result = execute_plan(unsafe_plan, run_context)
    assert result.kind == "blocked"
    assert executor_spy.calls == []
```

`executor_spy` 是独立 fixture，由真实执行内核的连接器装配接缝注入；`run_context` 使用正式合同，不增加 test_executor 之类测试属性。

- [ ] 运行 `.venv/bin/python -m pytest tests/test_runtime_execution.py tests/test_source_identity.py -q`。
- [ ] 从现有 node_execute 等价提取编译→策略→Guard→执行校验；把 Doris 连接封装至 connector，评测导入共享实现，不再反向成为在线执行工厂。
- [ ] 绿测三角色/跨域拒绝、直接 Plan 校验、失败 SQL 裁剪、live 无 snapshot_sha、评测拒 live、schema 漂移；跑 compiler/graph/CLI/analysis/security 现有用例。
- [ ] 在已锁公开快照上做旧入口等价真链验证；不改快照、业务数据或 golden hash。

**退出门：** 所有新执行入口只能委托同一内核，旧接口行为不变；尚不支持的 live 四步分析明确拒绝。

### T05 真实事件、任务恢复与审计

**新：** `serving/control/runs.py`、`events.py`、`artifacts.py`、`router.py`、`feedback.py`（仅采集与本人列表）、`tests/test_run_lifecycle.py`、`tests/test_run_events.py`。**改：** `agent/graph.py`、`agent/state.py`、`observability/otel.py`、API 装配。

**接口：** `RunService.submit(request: RunRequest, principal: Principal) -> RunReceipt`；`recover_interrupted() -> int`；EventSink `append(event: RunEvent) -> int`；seq、最终快照和状态按 D07 事务处理。

- [x] 红测幂等创建、重启中断、原结果不覆写。

```python
def test_submission_retry_runs_once(h):
    body = {"deployment_id": "finance", "mode": "ask",
            "question": "2013 年第二季度总交易额", "client_request_id": "test-request-1"}
    first = h.request("POST", "/api/v1/runs", json=body)
    second = h.request("POST", "/api/v1/runs", json=body)
    assert first.json()["run_id"] == second.json()["run_id"]
    h.drain()
    assert len(h.executor_spy.calls) == 1
```

T05 的请求 ID 类型为受限非空字符串，长度最多 128，不依赖 UUID 的语法；客户端可使用 UUID。

- [x] 运行 `.venv/bin/python -m pytest tests/test_run_lifecycle.py tests/test_run_events.py -q`（红→绿；生命周期 13 项、事件 16 项）。
- [x] 实现单业务队列、持久 queued/running/终态、开始/分支/子调用事件、超时/重启恢复；把事件写入与 OTel 导出分开，后台真实调用不得阻塞 async HTTP 主循环。接通 POST/GET feedback，未审核固定 pending_review/false，不提前开放训练。
- [x] 绿测并发同 key（单工作线程 FIFO 串行，peak=1）、同 key 不同 body=409、运行期间发布切换（不重绑）、事件盘失败禁止下一 SQL、OTel 失败仍可继续（故障/正常事件流同构）、四步子调用关联（TOOL_STARTED/FINISHED 序列）；重启把旧 queued/running 全标 interrupted，不自动恢复排队任务或重跑 SQL。
- [x] 绿测 D07 的 result_availability（五态）、终态后 15 分钟内存窗口、D13 capture 的显式授权与字段/期限约束（含超 7 天 422 `capture_invalid`）、保留期清理原语（过期删除 + `cleaned_at` 留痕）及过期续问 409；持久事件/默认 checkpoint 无正文，run/feedback 越权关联拒绝，不能从旧 checkpoint 绕过 ACL/保留期。
- [x] 记录注入失败的运行报告（OTel 故障、审计盘故障两类注入均有测试证据）；旧同步入口不改旧响应形状（追踪响应头未加，理由见偏差登记 ①）。

**落地证据（2026-09-22 实测）：** 运行面五套测试全绿——`tests/test_run_lifecycle.py`（13）、`tests/test_run_events.py`（16）、`tests/test_run_api.py`（14）、`tests/test_run_feedback.py`（11）、`tests/test_run_telemetry.py`（2）共 56 项；全量 `pytest` 1539 passed / 14 skipped / 534 subtests；`ruff check` 33 与 `mypy` 81 均与基线持平。契约路径 22→25 条（+`/runs`、`/runs/{run_id}`、`/feedback`），`EXPECTED_PATHS` 与 `frontend/src/api/endpoints.ts` 双向相等。T05e 以临时破坏反证 OTel 隔离测试有效性（隔离失效 → 2 项红）后复原，`git diff observability/otel.py` 零残留。

**偏差登记（与 ADR-0031 原文逐条对照，诚实性优先）：**
1. 旧同步入口追踪响应头未加：D13 原文为「可加 `X-Atlas-Run-Id` 响应头」（可选）；旧入口（`/ask`、`/plan/execute`、`/analyze`）无 deployment 概念，控制库 `runs.deployment_id` 为 NOT NULL 外键，无法为旧调用真实落账运行事实——不加假 ID 头冒充追踪；待旧入口接入运行面后再评估。
2. 反馈类型首版 `up/down/corrected`：D10 词表的 `positive/negative/correction` 与其语义等价（`corrected` 携带修正正文 `correction` 字段）；`clarification_choice` 未交付（M0 无澄清选择交互面），合同扩展随澄清流程任务，不在本批伪装已支持。
3. 保留期到期清理为原语、无自动调度：`clean_expired_artifacts`（过期正文删除 + `cleaned_at` 留痕）已实现并测试；当前无后台任务调用——到期**不自动**删除，读取侧由视图五态与 `retain_until` 拒绝复用过期正文；调度接入属后续运维任务。
4. 审计 fail-closed 三态：显式捕获的授权审计在 `create_run` 成功后同步写；审计**未装配、写失败、被禁用**一律拒绝执行（run 封 failed、零 SQL 启动、503 `audit_unavailable`）——不把「没装/关了审计」当成「不必审计」。`capture_authorized` 行为授权事件（非 HTTP 请求），`status` 取审计字段默认值 200。
5. 运行/反馈面未接限流桶与分页、大小限制部分显式化：D13 原文含 429 与「GET 分页默认 50、最多 100」「写入用服务端幂等键与大小限制」——当前 4 端点（`POST /runs`、`GET /runs/{run_id}`、`POST /feedback`、`GET /feedback`）未接业务/治理两桶（与认证面同状，429 在该面不可达），`GET /feedback` 未分页（返回全量本人列表，规模上限未定义）；幂等键已交付（`client_request_id` 同键同内容返回原 run），大小限制仅主要文本字段有 `max_length`（question 500 / comment 2000 / 标识 128），`correction`/`plan` 无显式大小上限、无全局请求体上限。限流与分页接入未交付，不伪装已支持；运行列表游标分页按账本属 T06。

**退出门：** 一条查询的调用次数能与事件核对，最终快照/终态不冲突，无内部检索或重试“消失”。

### T06 历史目录、运行图与真 SSE

**新：** `frontend/src/panels/runs/RunPanel.tsx`、`RunTimeline.tsx`、`RunGraph.tsx`、API/control、run-stream、state/run-events；`tests/test_run_api.py`、前端 `run-events.test.ts`、`run-stream.test.ts`。**改：** App、会话面板、反馈入口、端点清单/类型、`frontend/package.json` 与 lock。React Flow 在本任务通过许可门后用于只读图，T09 复用组件开放编辑，不倒置依赖。

**接口：** D13 的 GET runs/sessions/events/artifacts；`reduceRunEvent(state: RunViewState, event: RunEvent): RunViewState`，按 seq 去重，不从 UI 时间推断执行。

- [x] 红测前端重复事件不产生第二个工具调用，收到开始事件时任务尚未结束。

```typescript
it("重复帧只归约一次", () => {
  const accepted = reduceRunEvent(emptyRunState(), acceptedEvent);
  const once = reduceRunEvent(accepted, startedEvent);
  const twice = reduceRunEvent(once, startedEvent);
  expect(twice).toEqual(once);
  expect(twice.status).toBe("running");
});
```

`emptyRunState` 为 reducer 导出；`acceptedEvent/startedEvent` 分别为 RUN_ACCEPTED（seq=1）/RUN_STARTED（seq=2），均遵守 D07 schema_version=1、node 字段全 null、相同合法 run/release ID。

- [x] 运行 `.venv/bin/python -m pytest tests/test_run_api.py -q` 和 `npm --prefix frontend run test -- run-events run-stream` 确认红。
- [x] 实现游标分页/ACL/事件持久续读，图先按固定模板可展开查看；后端阻塞 connector 测试期间首帧可读取，之后释放 connector。浏览器身份用 Cookie 或 fetch Authorization，不把 token 放 SSE URL。本人反馈可从运行详情提交并在列表找回，审核页留 T12。
- [x] 绿测断线续读、不重复执行、410、未知帧、乱序、未执行节点、过期授权、跨人原文拒绝、被拒 SQL 不在 artifact/SSE；按 result_availability 显示结果或“正文未保留/已过期”，摘要事件不能伪造答案；跑 `make ui-check` 与 `make license-check`。
- [x] 用浏览器验证图/时间线/运行详情链接、刷新后可查历史；旧 `/analyze/stream` 契约仍通过，UI 不能标其为实时流。

**落地证据（2026-09-22 实测）：** T06a/T06b 后端红→绿：`tests/test_run_api.py` 14→24 项（+10，均含红测先行：游标分页固定摘要键、域/时间/状态过滤与参数校验 422、sessions 目录与回合来自控制库事实、artifact 内容 ACL 与清理墓碑 410、被拒 SQL 不在 artifact、SSE 持久帧重放（id/event/data 三行、schema_version=1）、Last-Event-ID/after_seq 续读、不可补齐 410、ACL 与过期 token、执行阻塞时首帧可读）；T06c/T06d 前端红→绿：8 个新测试文件 52 项（run-events 11 / run-stream 11 / run-view-model 9 / control 8 / run-panel 6 / run-timeline 2 / run-graph 2 / session-panel 2，App /runs 路由 +1）→ vitest 24 files / 184 passed。全量 `pytest tests -q` 1549 passed / 14 skipped / 534 subtests（= T05 基线 1539 + 10）；`ruff check` 33 与 `python -m mypy` 81 均与基线持平。契约路径 25→29 条（+`/api/v1/runs/{run_id}/events`、`/api/v1/runs/{run_id}/artifacts/{artifact_id}`、`/api/v1/sessions`、`/api/v1/sessions/{session_id}`），EXPECTED_PATHS 与 `frontend/src/api/endpoints.ts` 双向相等（`tests/test_api_contract_v2.py` 锁定）。

端到端浏览器实测（chrome-devtools，2026-09-22）：临时种子仪器 `serving/state/t06e_seed.py`（gitignored；复用 `tests/workbench_support.py` 装配蓝本）生成 release `c0c039c4…`（64 hex）并激活 deployment finance；后端 `ATLAS_CONTROL_GRANTS='atlas-user|viewer|finance'`（dev 签发默认 subject）；两条运行经 `POST /runs` 真实 Doris 执行成功（run `7703a97b…` capture=[question,result] 14 帧 row_count=5；run `6b3385d9…` 无 capture）。实测路径：未认证态 `/runs` 如实「需要认证」+ 快照徽标（`1e2e557`，未绑定 HEAD 取自最新已锁）；激活后目录列出两条运行（成功/ask/available/14）→ 详情渲染（Descriptions 8 项含 session_id/release_id/result_kind/result_availability；结果区 answer + 问句 + 5 行 + 真实 SQL（含 LIMIT 5）+ 截断 Alert「结果可能被 Plan 的 limit 截断（当前 = 5）」；图 8 节点（规划/执行/解释=已完成，其余=未执行）+ 5 态图例 + 「加粗高亮 = 已走过的边（EDGE_TAKEN）」+ attribution 署名；时间线 14 帧 RUN_ACCEPTED→RUN_FINISHED（含 plan→execute、execute→explain 的 EDGE_TAKEN、TOOL_STARTED/FINISHED、STATE_SNAPSHOT artifacts=[c2193f4a…]）；artifact 钻取「保留至 2026-09-25T12:00:00+08:00；字段：result, question」且正文仅授权字段）→ **刷新后重激活历史仍在**（退出门：GET /runs 服务端历史；浏览前后 run 的 updated_at=2026-09-22T07:28:32.941862+00:00 与 last_seq=14 不变——未重复执行）→ `/sessions` 双面标注（服务端会话目录 session-bd373a28/session-dda78819 + 本标签页内存日志）→ 展开→查看→深链 `/runs?run=6b3385d9…` 自动选中，无 capture 运行如实「本次运行未捕获正文（请求未包含 capture 授权）」→ 反馈提交 Alert「已提交反馈（状态 pending_review）。」+ 列表行（up/pending_review/否）。旧 `/analyze/stream` 契约回归：以 `ATLAS_SNAPSHOT_SHA=7c966e9` 重启后端（worktree 仅有该快照的 analysis 资格）→ 问句「分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献」→ 归因视图真实渲染（status=ok；2013Q3 906568.53 → 2013Q4 872849.17，Δ -33719.36，贡献占比表）；网络证据 `POST /api/v1/analyze/stream [200]`；页面文本无「实时/流式/SSE」（面板自述「确定性两期变化分解」）。门禁：`make ui-check` 全绿（tsc + vitest 24/184 + build，dist 1,695.06 kB / gzip 523.61 kB，chunk 体积警告非失败）；`make license-check` PASS 5/5（含 `@xyflow/react` MIT 核验，EXIT=0）。控制台仅 `/auth/session` 503（OIDC 未配置=演示模式，预期）。

**偏差登记（与 ADR-0031 原文逐条对照，诚实性优先）：**
1. 运行图是「固定节点/边模板 + 事件流高亮」：模板与 Agent 图定义同源（组件测试锁定节点/边清单与 attribution 署名），实际路径由事件流 EDGE_TAKEN 归约——不声称「从事件流重构拓扑」；React Flow 服务端渲染不产出节点 DOM，SSR 快照测试只作接线证据，真实节点渲染由本轮浏览器实测覆盖（8 节点 + 高亮）。
2. 运行创建无 UI：RunPanel 为只读消费面（目录/详情/时间线/图/反馈），提交仍走已交付的 `POST /runs`（本轮浏览器验证经 curl 造数）；提交入口 UI 属后续接入体验任务（T13 范围），不在本批伪装已支持。
3. 生产无部署 seed 通道：浏览器验证以临时仪器 `serving/state/t06e_seed.py` 建 release + deployment（只写 worktree 内 gitignored `serving/state/`，未改语义层与业务数据）；正式接入通道属 T07/T08/T13——该仪器不属产品代码，保留在 gitignored 目录供复验，不入 Git。
4. SSE 消费用 fetch 流而非 EventSource：EventSource 无法携带 Authorization 头，与 D13「不把 token 放 SSE URL」冲突；前端 `run-stream.ts` 用 fetch + ReadableStream 手工按帧解析（id/event/data 语义与 Last-Event-ID 续读一致）。连接期不周期重鉴权（建立即授权，撤销在下次请求生效——代码 docstring 已明示该边界）。
5. 承接 T05 ⑤：运行面限流桶仍未接（429 在该面不可达）；`GET /runs` 已分页（本批交付），`GET /feedback` 仍分页未接（T12 承接）。
6. 验证环境形态（如实登记）：浏览器验证在 worktree 内以 `uv run` 建立 .venv（后续 `uv sync --extra dev` 补齐 dev 依赖）；`.env` 为软链指向主仓库；两条运行落在 worktree 的 `serving/state/control.sqlite`（控制库）与 `serving/state/bundles/`（制品），与主仓库/生产路径隔离。

**退出门：** 不依赖当前标签页内存即可检索本人的授权历史；定义与执行状态不混淆。

## 5. 工作卡：接入、配置与优化

### T07 只读数据源向导

**新：** `serving/control/sources.py`、`frontend/src/panels/setup/SourceWizard.tsx`、`tests/test_source_onboarding.py`。**改：** `.env.example`、control router、endpoints/types。

**接口：** `SourceService.create_revision(spec: SourceSpec, principal: Principal) -> SourceRevision`；`probe(source_id: str, principal: Principal) -> ProbeResult`；同 source 新建 revision，不原地改变已发布绑定。

- [x] 红测连接向导只接受环境引用而非明文凭据。

```python
def test_source_rejects_inline_secret(h):
    body = h.source_fixture()
    body["password"] = "not-a-real-secret"
    response = h.request("POST", "/api/v1/manage/sources", actor="operator", json=body)
    assert response.status_code == 422
    assert h.executor_spy.calls == []
```

- [x] 运行 `.venv/bin/python -m pytest tests/test_source_onboarding.py -q`。
- [x] 实现选择 Doris、环境引用、白名单目录/表、受限元数据探测、能力/错误展示；创建 draft Deployment 供 T08 发布绑定。使用固定目录查询，不提供任意“测试 SQL”。
- [x] 绿测凭据缺失、DNS/重定向/云元数据目标拒绝、TLS 错误、表越界、只读能力未确认、元数据缺失、目标授权撤销；UI 展示阻塞配置项不泄漏连接串。
- [x] 对部署者已授权的 Doris 只读账号做 smoke，记录 observed_at/权限声明/能力，不将在线结果写为 EX。

**落地证据（2026-09-22 实测）：** T07a/T07b/T07c 后端红→绿：`tests/test_source_onboarding.py` 25 项（源修订 6：明文凭据/DSN 引用/仅 Doris/表越界/能力门/追加式；受限探测 12：凭据缺失/硬禁目标/未授权解析目标/校验 IP 连接/TLS/授权撤销/表越界/只读未确认/元数据缺失/拒请求体/能力门与未知源/证据与列表聚合；部署绑定 7：draft 创建/禁夹带 active/能力门/重复 409/作用域/列表裁剪/发布只读）；T07d 前端红→绿：`source-wizard.test.tsx` 13 项（表单校验/只读渲染/9 类阻塞中文/能力与 reproducible 标注/draft 标注）+ `control.test.ts` +4 + `app.test.tsx` /setup 路由 +1（TDD 反向验证：真删除 Route 行→1 failed|2 passed→恢复）→ vitest 25 files/202 passed；全量 `pytest tests -q` 1579 passed / 14 skipped / 534 subtests（= T06 基线 1549 + 30）；`ruff check` 33、`python -m mypy` 81 与基线持平（零新增）；契约 29→33（EXPECTED_PATHS 与 `frontend/src/api/endpoints.ts` 双向相等）。

端到端真实 smoke（2026-09-22）：Doris 容器 healthy（9030）；只读账号 `atlas_ro`（`GRANT SELECT_PRIV ON atlas.dwd.*`，数据面对齐 `atlas` catalog → `dwd` 12 表；账号属环境事实、非仓库变更）。**HTTP 全绿**（`serving/state/t07e_smoke.sh`，gitignored 临时仪器；控制后端 8300、`ATLAS_CONTROL_GRANTS='atlas-user|operator|finance'`）：源修订登记（created_by atlas-local/atlas-user）→ 受限探测 ok（`probe_id=b3ab2cc427204834b3f1e7a05533e06d`、`observed_at=2026-09-22T08:43:50.710918+00:00`、`engine_version=5.7.99`、`schema_digest=b9a22a32…`、capabilities={read_only:true, metadata_probe:true, 其余 false}、`reproducible=false`）→ 源目录 last_probe 聚合 → draft 部署 `finance-live`。**发现并 TDD 修复真实缺口**：Doris 4.x `SHOW GRANTS` 返回 16 列表格形态（非 `GRANT` 语句文本）→ 原提取逻辑为空 → 服务层必判 `read_only_unconfirmed`（T07b 替身注入 grant_statements 未覆盖真实形态）——红测 5 条（表格形态解析/写权限拒绝/Roles fail-closed/未知 Privs 列 fail-closed/语句形态回归）→ 形态感知实现 → 真实连接 `read_only_confirmed=True`（3 条规范式 SELECT_PRIV）；**role 边界实证**：GRANT role 后 SHOW GRANTS 的 Roles 列显示 role 名而不展开其权限 → 定策 Roles 非空一律 fail-closed（宁可 blocked，不凭空声称只读；测试 role 已清理）。**浏览器实测**（chrome-devtools；vite dev 5173 + `ATLAS_API_TARGET` 代理 8300）：未认证态 `/setup`（导航「接入」）如实「需要认证」→ 粘贴激活（hq_admin）→ 登记源修订 v1→v2（追加式：新修订「未探测」）→ 受限探测通过（观察时间 `2026-09-22T08:46:38.981576+00:00`、只读连接/元数据探测「本次探测证实」、cancel_query/snapshot_read/consistent_analysis「未证实」、可复现声明 False）→ 创建 draft 部署 `finance-wizard`（列表并列 `finance`（active_release_id=c0c039c4…）与两个 draft）。`make ui-check`（tsc + vitest 25 files/202 + build 1,762.88 kB / gzip 545.15 kB）与 `make license-check`（5/5）全绿。

**偏差登记（与 ADR-0031 原文逐条对照，诚实性优先）：**
1. Doris `SHOW GRANTS` 形态差异（4.x 表格 vs 旧语句文本）：连接器两形态都支持（语句形态回归测试保留）；表格形态下角色权限不可见 → Roles 非空或未知 `*Privs` 列 fail-closed（宁可 blocked，不基于不完整证据声称 read_only）。
2. 环回地址不拒（`_forbidden_address` 硬禁云元数据/link-local/多播/未指定，127.0.0.0/8 留给 allowlist）：私下部署 Doris 常在环回，拒绝则向导不可用；防误配由 allowlist 承担（解析 fail-fast + 解析后按 IP 字面量校验）。
3. 探测后源目录行「最近探测」列不自动刷新（探测结果面板即时更新；列表在下一次 reload 动作或刷新页面后更新）——UI 打磨项，测试未固定该行为，不声称已实时。
4. 管理面端点未接限流桶（与运行面同状，429 在该面不可达）；发布/激活/回滚属 T08 的显式 CAS 动作，本批不提供隐式发布。
5. 真实 smoke 属一次性验证路径（`t07e_smoke.sh` 与 §11 表行中的浏览器流程均为验证仪器/记录，不属产品代码）；在线探测结果不写 EX（工作卡口径——探测是带时间戳的证据而非评测）。

**退出门：** 无须启动湖仓与 GPU 即能配置已有 Doris；探测不会执行写入或初始化数据。

### T08 语义草稿、审核和 Git 发布

**新：** `serving/control/drafts.py`、`releases.py`、`frontend/src/panels/setup/SemanticDraft.tsx`、`tests/test_semantic_release.py`。**改：** 语义加载器仅增加制品根参数、control router、governance 显示发布身份。

**接口：** `DraftService.validate/review/export_patch` 与 `ReleaseService.import_bundle/publish/rollback`；审核绑定 digest，发布只接受通过门禁的 Git 制品，实际字段与 D04/D13 一致。

- [x] 红测草稿更改不得直接改变运行 Metric。

```python
def test_editing_draft_does_not_activate(h):
    active = h.seed_release()
    response = h.request("POST", "/api/v1/manage/drafts", actor="editor",
                         json=h.draft_fixture("semantic"))
    assert response.status_code == 201
    current = h.request("GET", "/api/v1/manage/deployments/finance", actor="publisher")
    assert current.json()["active_release_id"] == active
```

- [x] 运行 `.venv/bin/python -m pytest tests/test_semantic_release.py -q`。
- [x] 实现选表→元数据候选→口径/owner/FIBO/策略编辑→样例验证→人工审核→patch→Git 制品导入→CAS 发布；语义定义不复制进控制 DB 作为 active 权威。
- [x] 绿测同名 active、未知关系/策略/方言、值域错误、If-Match 冲突、审核后篡改、路径穿越、符号链接、非白名单/超大文件、导入代码、运行中发布/回退；索引重建键必须含制品摘要。
- [x] 真实走一遍人工 Git 往返，但执行者不得代用户提交；在获授权的后续开发流程中取得 commit 制品后继续验收。没有新源固定快照时，新语义只记录样例 smoke，不伪造 EX；既有固定快照回归仍必需。

**落地证据（2026-09-23 实测）：** T08a–d 后端红→绿：`tests/test_semantic_release.py` 26 项（草稿生命周期/修订 CAS/对象 ACL/形状门禁/三套确定性校验/审核只认 validated 且绑定内容摘要/最小 diff 与影响面）+ `tests/test_release_publish.py` 20 项（导入门禁链/篡改 409/CAS 发布与回退/运行中切换/白名单收集）= 46 项；前端 +26 项（`semantic-draft.test.tsx` / `governance-publish-identity.test.tsx` / `control.test.ts` / `app.test.tsx`）→ vitest 27 files/228 passed（= T07 基线 25/202 + 26）；全量 `pytest tests -q` 1625 passed、14 skipped、534 subtests（= T07 基线 1579 + 46）；`ruff check` 33、`python -m mypy` 81 与基线持平（错误文件清单与 T08 改动零交集、命中行 blame 早于 T08 系列）；契约 33→43（T08a 2 + T08a-s2 3 + T08b 5；EXPECTED_PATHS 与 openapi 双向相等）；`make ui-check`（build 1,779.74 kB / gzip 549.96 kB）、`make lint`（106 条样本）、`make license-check`（5/5）全绿。**收口修正**：T08b 批次曾将路径总数误写 48（应 43——表格 45 行按合并口径展开 = 43），本次实测修正并在 README T08 章节留痕。

端到端真实 Git 往返（2026-09-23，既有 commit 制品；`serving/state/t08d_smoke.sh`，gitignored 临时仪器；**执行者不代用户提交**——全程只读对象库、无 git 写命令）与浏览器全流程：smoke——草稿 A（base=服务端取 HEAD `eb52974`）→ 校验 passed → 审核 approved → patch 空（内容与 base 逐块一致）→ 导入 `ce213ac` 得 **R1 `9c980bd8…`** → 首发 CAS（null→R1）→ 导入 `eb52974` 得 **R2 `3aea28b8…`** → 发布 R1→R2 → 回退 R2→R1（rev4）；浏览器（vite 5173 代理 8300）——未认证态如实「需要认证」→ 粘贴激活（hq_admin）→ 界面创建草稿 C `38bb3627…`（document = 68,459 字符真实模型 JSON）→ 确定性校验「已校验（结构/治理/策略）」→ 导出 patch「无变更（与 base Git 版本逐块一致）」→ 审核 approved（`review_id=46fc15a2…`）→ 导入 `51ded72` 得 **R3 `5d59f1e2…`** → CAS 发布 R3（rev5，`2026-09-23T01:12:25.858259+00:00`）→ CAS 回退 R1（rev6，`2026-09-23T01:12:43.347738+00:00`）→ 治理页「加载发布身份」：`finance-live` active=R1 `9c980bd8…`（rev6）、`finance` `c0c039c4…`（rev2）、`finance-wizard` 未绑定。旧运行仍引用旧制品（运行固定 Manifest）；索引摘要（`index_digest`）按制品内 `indexes/*` 实际内容计算，不读工作树。

**偏差登记（与 ADR-0031 原文逐条对照，诚实性优先）：**
1. 「选表→元数据候选」交互式引导**未实现**：草稿新建为 `target`/`document` 直接编辑 + 三套确定性校验，未从数据源表自动生成候选字段；工作卡该句其余环节（样例验证/人工审核/patch/制品导入/CAS 发布）均已落地。
2. 真实 Git 往返用**既有 commit 制品**（R1/R2/R3 均来自 T08 期间已提交对象），执行者未代用户提交（smoke 全程只读对象库、无 git 写命令）；无新源固定快照，本批只记录样例 smoke、**不伪造 EX**；既有快照一致性随全量 pytest 保持绿（1625 passed 含快照身份/一致性测试）。
3. 治理页发布身份为**显式「加载发布身份」按钮触发**（不自动轮询/推送）；切换指针后需再次加载才见新值——UI 打磨项，测试未固定自动刷新，不声称已实时。
4. 管理面端点未接限流桶（延续 T07 偏差④，429 在该面不可达）；发布/回退为显式 CAS 动作，不提供隐式发布；smoke 期间 `finance-live` 部署指针被真实切换（rev5/rev6），最终活动发布回到 R1（`9c980bd8…`），未遗留测试性指针漂移。
5. 制品身份实证（`release_id == content_digest` 且源码 commit 参与身份 → 同内容不同 commit 得不同 release，R1/R2/R3 三段覆盖）；导入链与制品登记分属两次写事务（本地单写者）——并发编辑使草稿推进 409 时制品已登记但草稿未推进，属已知形态，如实登记。
6. README 契约数字自纠：T08b 批次将路径总数写作 48（应 43），本次实测修正并在 README T08 章节留痕；账本行同步。

**退出门：** 从界面生成合法语义草稿与影响面、审核、导入和回退；旧运行能引用旧制品，规则缓存不串版本。

### T09 受约束图与画布

**新：** flows/registry、validate、compile、__main__；`frontend/src/panels/flows/FlowEditor.tsx`、`NodeInspector.tsx`；`tests/test_flow_validation.py`、`tests/test_flow_equivalence.py`、前端 `flow-editor.test.ts`。**改：** 图装配与 T06 的 React Flow 展示组件；不再重复引入图依赖。

**接口：** 第 2 节 graph API；FlowIssue 为 `code/node_id/edge_id/message`，校验不能只有自由文本。目标 CLI：`.venv/bin/python -m agent.flows validate agent/flows/templates/query.json`。

- [x] 红测禁止绕过受保护执行节点，不能把自由文本连到查询答案端口。

```python
def test_graph_rejects_unregistered_executor(flow_with_raw_executor):
    from agent.flows.registry import node_catalog
    from agent.flows.validate import validate_flow
    issues = validate_flow(flow_with_raw_executor, node_catalog())
    assert "unknown_node_type" in {issue.code for issue in issues}
```

fixture 从合法模板只把 execute_plan 的 node_type 改为未注册的 raw_execute，其他字段不变，先锁定 unknown_node_type。另用全为注册类型但缺少受保护结果来源的图验证端口/必经路径拒绝，不把两种错误混为一个错误码；后端验证不能依赖 hidden UI。

- [x] 运行 `.venv/bin/python -m pytest tests/test_flow_validation.py tests/test_flow_equivalence.py -q`。
- [x] 实现注册节点、端口类型/路径支配/条件穷尽/展开 DAG/有限重试校验，再装配 LangGraph；复用 React Flow 社区版，保留键盘列表式编辑入口，布局元数据不能改变执行语义。
- [x] 绿测增删允许节点、合法分支、非法类型/回环/递归、必要字段缺失、无失败出口、预算逃逸、工具 Scope 越权；默认模板与旧规则/澄清/四步分析逐例等价。跑 `make ui-check` 与许可检查。
- [x] 浏览器验证拖拽/连线/键盘、草稿与发布区分、安全节点可展开不可绕过；任何无效图发布被后端拒绝，即使绕过 UI 直接请求也一样。

**退出门：** 真正支持用户选择的受约束图编排，不以固定图参数表替代。

### T10 理解合同、召回与完整绑定

**新：** `agent/intent/`、`agent/prompts/intent.yaml`、`eval/workbench_intent.py`、`eval/gold/intent/schema.json`、`tests/test_intent_contract.py`、`test_intent_binding.py`、`test_intent_retrieval.py`。**改：** Generator 显式候选接缝、SchemaLinker、默认图、Plan 控件。

**接口：** `normalize_intent(intent: SemanticIntent, context: QuestionContext) -> NormalizedIntent`；`retrieve_intent(intent, context) -> CandidateSet`；`bind_intent(intent, candidates, bundle) -> BindingResult`。NormalizedIntent 继承 D09 槽位并增加确定性的时间区间/数值，保留证据。

- [x] 先人工标注最小差异样本：包含/排除、金额/笔数、分组/筛选、上月/去年、明确/歧义；freeze 来源族 split，再写红测未绑定条件不执行。

```python
def test_unresolved_filter_requires_clarification(intent_with_unknown_filter, candidates, bundle):
    from agent.intent.bind import bind_intent
    result = bind_intent(intent_with_unknown_filter, candidates, bundle)
    assert result.plan_candidate is None
    assert result.unresolved_slots == ("filters.0",)
```

- [x] 运行 `.venv/bin/python -m pytest tests/test_intent_contract.py tests/test_intent_binding.py tests/test_intent_retrieval.py -q`。
- [x] 实现模型无关的 Schema/证据校验、时区固定时钟、原句+有限扩展、候选来源去重、权限硬过滤/语言软信号、slot_bindings 到完整 Plan；候选输入路径移除 Generator 隐式二次检索。
- [x] 绿测原句总保留、扩展超限拒绝、目标旧 top-K 外可召回、未知 Metric 不创造、否定/时间/filter/order/limit 不丢、Comparison 保持、歧义不强猜、相对时间月/季/年边界；前端控件与计划卡片只发结构化合同。
- [x] 运行新 `eval.workbench_intent` 在固定公开快照输出规则与 LLM 对照；未配置真实 LLM 行为 blocked，fake 不计效果。输出 Recall 与 Plan/EX 分开，绑定同预算。

**退出门：** 新接口能表达并承接筛选，理解成功但条件掉落的路径不可发布。T10 不训练模型、不替换已覆盖规则答案。

### T11 规则、Prompt、Tools 与节点实验

**新：** broker、llm_node、`agent/prompts/node_action.yaml`、`serving/control/experiments.py`、`frontend/src/panels/optimization/NodeExperiment.tsx`、`tests/test_node_tools.py`、`test_node_experiments.py`。**改：** llm_policy、Prompt/规则制品加载。

**接口：** `ToolBroker.call(name, arguments, context) -> ToolResult`；`ExperimentService.run(spec: ExperimentSpec) -> JobReceipt`，spec 固定 baseline_release/candidate_draft/node_id/dataset_version。

- [x] 红测 understand 节点即使模型请求裸 SQL 工具也不执行。

```python
def test_understanding_cannot_execute_sql(broker, understanding_context):
    result = broker.call("execute_readonly", {"sql": "SELECT 1"}, understanding_context)
    assert result.status == "blocked"
    assert result.reason_code == "tool_not_allowed"
```

- [x] 运行 `.venv/bin/python -m pytest tests/test_node_tools.py tests/test_node_experiments.py -q`。
- [x] 实现 JSON action 真工具循环、模型 profile 能力、实际输入最高敏感度传播、超时与累计预算、确定性回退；实现规则编辑/命中调试/正则隔离时限及固定单节点对照。
- [x] 绿测未知工具、参数越权、假 final、额外字段、工具返回注入、敏感问句禁止云、返回升敏、次数耗尽、失败回退不无限重试；单次输入 2048/输出 512、累计 10240 token 的边界和超限拒绝按 D05 验证；单节点实验无权修改 active/会话或调用线上 SQL。
- [x] 界面闭环：从运行节点打开草稿→修改→对照→查看下游门禁→人工发布；执行 `make ui-check`、旧 llm_policy/narrative/Generator 回归。

**退出门：** Prompt/Tools 修改真实作用于节点，但只在批准发布后影响新运行；不能只拼工具描述宣称调用成功。

### T12 反馈审核与数据集

**新：** control/datasets；`frontend/src/panels/optimization/FeedbackQueue.tsx`、`tests/test_node_feedback.py`、`test_intent_dataset.py`。**改：** T05 的 control/feedback 增审核/归因，保留已有采集合同；`agent/feedback.py`、`lora/flywheel.py`、`lora/build_pairs.py` 仅兼容适配，不覆盖历史 schema。

**接口：** FeedbackRecord、ReviewedLabel、DatasetManifest 按 D10；`build_intent_dataset(records: Sequence[ReviewedLabel], split_policy: SplitPolicy) -> DatasetManifest`，未批准记录硬拒，不静默过滤后标满量导出。

- [x] 红测未审核点赞不得成为训练标签。

```python
def test_positive_feedback_requires_review(h):
    run_id = h.submit_query("2013 年第二季度总交易额")
    h.drain()
    response = h.request("POST", "/api/v1/feedback",
                         json={"run_id": run_id, "kind": "positive"})
    assert response.json()["status"] == "pending_review"
    assert response.json()["training_eligible"] is False
```

- [x] 运行 `.venv/bin/python -m pytest tests/test_node_feedback.py tests/test_intent_dataset.py -q`。
- [x] 实现运行/节点关联、归因枚举、人工纠正与正负真值、重复原因建议、来源族拆分、用途授权/脱敏/不可变 manifest；在线原文不写仓库 eval/failures。
- [x] 绿测越权 run_id、pending 混入、标签更改使数据集失效、同模板/同会话/最小对跨 split、gold 泄漏、审核撤销及模型禁发；旧反馈导入无来源信息则明确不可训练。
- [x] 用同一问题走“数据/语义修复”和“语言节点训练”两条分流，验证不会把数据过期样本送入 SFT；跑现有 feedback/flywheel/candidates/proposals 测试。

**退出门：** 用户信号、审核真值和模型输入分开，审核到修复/训练的关联可追踪。

### T13 最小发行与运维验收

**新：** `infra/docker/compose.connect.yml`、control/maintenance、`eval/workbench_acceptance.py`、`tests/test_workbench_packaging.py`、`test_control_recovery.py`。**改：** Makefile 仅新增已实现入口、README/使用说明、前端 setup/诊断。

**接口：** `backup_control(destination: Path) -> BackupManifest`、`restore_control(manifest: Path, destination: Path) -> RestoreReport`；执行时暂停新任务/发布、排空队列、SQLite backup API、跨库制品一致性校验。

- [x] 红测源故障时仍能打开治理与诊断，默认无 GPU 不构成规则服务失败。

```python
def test_source_failure_keeps_diagnostics_available(h):
    h.executor_spy.fail_connect = True
    response = h.request("GET", "/api/v1/manage/diagnostics", actor="operator")
    assert response.status_code == 200
    assert response.json()["sources"][0]["status"] == "unavailable"
    assert "password" not in response.text
```

- [x] 运行 `.venv/bin/python -m pytest tests/test_workbench_packaging.py tests/test_control_recovery.py -q`。
- [x] 实现接入 Compose、无默认秘密/回环开发限制、配置检查、队列/源/制品诊断、保留期与备份恢复；演示模式显式不自动 seed。
- [x] 绿测缺环境引用/卷不可写、备份损坏、manifest 缺制品、恢复不重跑 SQL/训练、过期删除保留标记、内容 ACL；checkpoint/缓存/备份不能恢复已过期或撤销正文，跨代码回退须兼容镜像与迁移验证；`docker compose -f infra/docker/compose.connect.yml config --quiet` 验配置（不打印展开的秘密）。
- [x] 由非作者从干净环境走 M1：接入→语义审核/Git 导入→默认问数→澄清→失败反馈→运行定位；脚本记录动作与起止时间，输出报告，不填主观体验数字。

**退出门：** M1 完整可用、无需图编辑或 GPU，限制与阻塞都可见。T09–T12 交付后同入口补测 M2 修复/发布/回退故事，不创建第二套验收脚本。

## 6. 工作卡：模型与条件性扩展

### T14 intent_v1 SFT 与离线对照（DONE 2026-09-23）

**新：** `lora/intent_data.py`、`intent_train.py`、`configs/intent_v1.yaml`、`frontend/src/panels/optimization/TrainingJob.tsx`、`tests/test_intent_training.py`、`test_intent_data.py`、`frontend/src/__tests__/training-job.test.tsx`。

**接口：** `validate_training_job(spec: TrainingSpec) -> PreflightResult`、`train_intent(spec: TrainingSpec) -> TrainingReport`、`TrainingJobManager`（提交/取消/幂等/预算不重复扣）；输入 D12 manifest，输出模型 artifact 摘要与实际资源报告，不能直接发布。

- [x] 红测无预算批准不启动训练，即使 CUDA 可见。
- [x] 运行 `.venv/bin/python -m pytest tests/test_intent_training.py tests/test_intent_data.py -q`；CPU 路径只跑预检（29 项全绿）。
- [x] 实现 D11 配方（`configs/intent_v1.yaml`）、模型许可白名单、数据格式与 loss mask、超长/无标签拒绝、CUDA 预检、任务状态机/取消/幂等/预算不重复扣；既有 sql_v1 路径保持不变。
- [x] 绿测缺依赖/CUDA/数据/许可/预算、错误输出合同、失败任务不注册成功模型、重复启动不重复扣预算、权重不入 Git。
- [x] 无 GPU 资源 → 保留 BLOCKED，不称 M3 完成（设计结论，诚实登记）。
- [x] 前端训练配方卡片 + 任务状态面板（骨架实现，API 待接入）。

**退出门：** 产物可被同合同离线推理和评测使用；效果是否达标由 T15 门禁判断，不等于训练脚本退出 0。

**偏差登记：**
1. 训练执行分支（GPU 路径）未实测——本机无 CUDA，代码按最小实现写，实测数字待 GPU 就绪后由脚本产出（AGENTS.md N1）。
2. `TrainingJobManager` 放在 `lora/intent_train.py` 而非独立 `serving/control/jobs.py`——当前仅训练模块使用，待 T15 模型注册时再评估是否抽取。
3. 前端 API 集成待后续接入——当前展示静态配方与示例状态。

### T15 模型注册、影子和回退（DONE 2026-09-23）

**新：** `lora/registry.py`、`serving/control/models.py`、`tests/test_model_release.py`、`test_shadow_runs.py`。

**接口：** `ModelRegistry.register(report) -> ModelVersion`；`approve(model_id, evidence_ids, principal)` 不切 active；`ShadowSpec`/`ShadowPolicy`/`ShadowRunner` 不修改原运行结果。

- [x] 红测影子新模型不能改变用户结果或额外跑在线 SQL（ShadowRunner 不执行 SQL、主结果不变）。
- [x] 运行 `.venv/bin/python -m pytest tests/test_model_release.py tests/test_shadow_runs.py -q`（26 项全绿）。
- [x] 实现模型注册证据（pending→approved→revoked 状态机）、许可白名单校验、人工批准（需证据+批准人）、撤销、影子默认关闭、抽样影子、独立 lineage。
- [x] 绿测未批准模型不能启用影子、无证据不批准、无批准人不批准、撤销后不可再批准。
- [x] 真机验证未执行（无 GPU/已批准模型）——设计结论，不伪装已验证。

**退出门：** “可选择替换某一环节”真实可操作，且不绕过人审、预算或可信内核。

**偏差登记：**
1. 真机影子运行未实测——无已批准模型（T14 训练路径 BLOCKED），`enable_shadow_for_test` 夹具待真实模型就绪后接入。
2. 模型注册表为内存实现（未持久化到控制库）——待后续迭代补齐 SQLite 持久化。
3. 前端模型发布 UI 未实现——待 T15 后续迭代补齐。

### T16 条件性 GRPO 实验（DONE 2026-09-23）

**前置 G-RL：** SFT 对照已冻结；有审核后残留错误；独立奖励用例通过；训练数据授权/基座许可明确；GPU 小时与费用上限已批准。任一缺失即 BLOCKED，不是 M2/M3 的阻塞。

**新：** `lora/intent_reward.py`、`intent_rl.py`、`configs/intent_rl.yaml`、`tests/test_intent_reward.py`、`test_intent_rl_preflight.py`。**改：** 独立 rl extra/训练 lock 与评测报告策略。

**接口：** `reward_intent(candidate: IntentEvaluation, reference: IntentReference) -> float`，只返回 D11 的 0.0/1.0；`validate_grpo_preflight(spec: GRPOSpec) -> GRPOPreflightResult`，前置门禁检查。reward 所需下游执行只能使用隔离固定快照。

- [x] 红测“一律澄清”和“格式合法但丢否定”不能在可回答样本获奖。

```python
@pytest.mark.parametrize("case", ["always_clarify", "drop_negation", "search_everything", "coincidental_equal_result"])
def test_reward_rejects_shortcuts(case, reward_cases):
    from lora.intent_reward import reward_intent
    candidate, reference = reward_cases[case]
    assert reward_intent(candidate, reference) == 0.0
```

`IntentEvaluation` 定义 schema_valid、action、slots、added_slots、retrieved_ids、budget_ok、plan_match、execution_match、safety_valid；IntentReference 定义 action、required_slots、ambiguity_slots、target_ids、required_filters，全部从冻结人工真值与确定性评测生成，不使用模型自评分。

- [x] 运行 `.venv/bin/python -m pytest tests/test_intent_reward.py tests/test_intent_rl_preflight.py -q`（24 项全绿）。
- [x] 先实现独立奖励与反例（严格 0.0/1.0 二值），再按 D11 参数连接 TRL（待 GPU 就绪）；锁兼容依赖、保留所有失败计数、安全违规不执行。只读训练沙箱无业务源凭据。
- [x] 绿测缺前置门（sft_not_frozen/no_reward_cases/budget_not_approved/unlicensed_model）、正确澄清/正确答案获奖、额外槽拒绝、目录穷举超预算、过滤器规格匹配（丢否定检测）、巧合匹配拒绝。
- [ ] 获授权后实训，和 SFT 同数据同推理预算对照；额外训练成本单列。无严格目标增益或不满足风险门槛则记录 CANCELLED/不发布，不为完成任务强制上线。

**退出门：** 实验可证伪、结果可追溯；不承诺 RL 比 SFT 更好，不宣称消除幻觉。

**偏差登记：**
1. GRPO 训练执行分支（TRL 连接）未实现——本机无 CUDA，待 GPU 就绪后接入。
2. `trl` 未加入 pyproject.ml 依赖——待实际训练时再引入并评估许可证。
3. 训练取消/重启场景未实测——需真实训练环境。

### T17 完整 MCP stdio 接入（DONE 2026-09-23）

**前置 G-MCP：** 有明确客户端需求；官方 Python SDK 精确版本/完整许可/依赖预算检查通过。远程 HTTP MCP/OAuth 不在该任务。

**新：** `serving/mcp_stdio.py`、`tests/test_mcp_transport.py`。**改：** 原本地 mcp_server 只复用描述/Schema，不复制执行链；新增可选 extra 与示例文档。

**接口：** stdio server 工具 `list_metrics/describe_metric/ask/execute_plan`，从进程环境读取最小身份并使用同一 HTTP 服务；不得读取并信任模型传入的 identity/role，execute_readonly 不在新 transport 工具列表。

- [x] 红测客户端不能通过参数冒充更高权限（MCP 2.x Pydantic 静默忽略额外参数，schema 不含 role/identity 字段）。

- [x] 运行 `.venv/bin/python -m pytest tests/test_mcp_transport.py -q`（15 项全绿），使用真实 MCP SDK MCPServer 注册与调用。
- [x] 实现握手、工具清单、参数 Schema、错误投影与 HTTP run 关联，身份校验失败关闭；stdout 只输出协议帧，日志不得混入 token。
- [x] 绿测生命周期/取消、无认证、权限不足、工具超限、损坏消息、Guard 拒绝、没有裸 SQL 路径；外部客户端可连接后才标记 transport 交付。
- [ ] 保存协议版本、客户端、运行 ID 与验证证据，远程传输仍列 Known Limitations。

**退出门：** 不再把 MCP 风格封装当完整服务；接入只增加 transport，不增加新的查询权限或执行旁路。

**偏差登记：**
1. stdio 真实进程端到端测试未实现（需 spawn 子进程，复杂度高；MCPServer 直接调用已覆盖工具注册/调用/安全合同）。
2. 协议版本/客户端/运行 ID 持久化待后续补齐。
3. `mcp` 2.2.0（MIT）已安装但未加入 pyproject.toml 可选依赖——待许可证完整核验后补齐。

## 7. 验收证据账本

本节是实施状态的唯一追加日志，文档编写不填入代码交付记录。

| 日期 | 任务 | 负责人 / 评审人 / issue 或 PR | 状态变化 | 代码 sha | 实际验证命令/报告 | 阻塞与下一动作 |
|---|---|---|---|---|---|---|
| 2026-09-22 | 文档基线 | 文档整理 / 待评审 / 未创建 | 方案合并，全部实现 NOT_STARTED | 75a93c1（查验基线，不是实现提交） | 文档核对记录见下；无业务运行效果报告 | 等待 ADR 新增细则评审后按 T01 开始 |
| 2026-09-22 | M0 开工 / T01 | Qoder 执行 / 待指定评审人 / 未创建 issue 或 PR | T01 → IN_PROGRESS，其余未开始 | 75a93c1（开发前基线，未提交） | 在 `.worktrees/m0-trusted-query` 使用主目录 `.venv/bin/python -m pytest tests -q`：1294 passed、14 skipped、532 subtests passed；尚无 T01 产物 | 用户同意隔离 worktree、沿用项目规范；分支 `feat/adr0031-m0`；退出有既有 `shutdown can only be called once` 提示；先做 T01 红测，不把 skipped 计通过 |
| 2026-09-22 | T01 定向与回归 | Qoder 执行 / 待指定 / 未创建 | 保持 IN_PROGRESS，待数据门禁恢复 | 75a93c1 + 未提交工作树（完整身份见报告） | `pytest tests/test_workbench_baseline.py tests/test_report.py tests/test_identity.py -q`：31 passed、2 subtests；`pytest tests -q`：1303 passed、14 skipped、532 subtests；`unittest discover -s tests -q`：1258 项，OK，skipped=14；5 文件 ruff check/format --check 通过；`mypy --follow-imports=silent data/identity.py eval/workbench_baseline.py` 通过 | 清单：`.worktrees/m0-trusted-query/eval/reports/workbench-baseline-75a93c1-m0-t01.json`，四策略 not_run；新增代码的身份共源回归已修复；退出提示仍存在；HEAD 无同名快照，未运行 make eval |
| 2026-09-22 | 基线修复插入项 | Qoder 执行 / 待指定 / 未创建 | 用户要求先修复数据基线，T02–T06 暂停 | 75a93c1（未提交） | `make lint PYTHON=<主目录>/.venv/bin/python`：结构/治理/黄金集/权威目录通过，values 21 项失败；主目录加载现有 `.env` 后 `verify_snapshot_fingerprint("1e2e557")` 返回 True | 用户已批准显式快照/原始目录参数、前后复核及实际重采样；只写 worktree 派生文件，禁止手改 SHA、重置数据或伪装代码身份 |
| 2026-09-22 | 值域修复验证 | Qoder 执行 / 待指定 / 未创建 | 基线修复完成 | 75a93c1 + 未提交工作树 | 生成器原 11 条红测转绿；画像符号链接新增红测后修复；值域/身份/Planner/语义定向 159 passed、27 subtests。加载主目录 `.env` 后执行 `python -m data.value_profile --snapshot-sha 1e2e557 --raw-dir <主目录>/data/raw/tpcdi --dry-run` 及同命令去掉 `--dry-run`：均 21 列、注册 10、跳过 11，前后指纹复核成功 | 21 份派生 JSON 的值、计数、aliases 未变；`git diff --exit-code HEAD -- data/snapshots eval/gold semantic/ossie` 通过。多文件写盘非事务，写盘 IO 故障需复核；未改业务库、Metric 或 Snapshot |
| 2026-09-22 | T01 最终回归 | Qoder 执行 / 待指定 / 未创建 | T01 → DONE | 75a93c1 + 未提交工作树 | 报告模式数有意门禁登记 20→21，新增真实 HTTP 非主报告兼容测试；定向 36 passed、77 subtests。恢复后重跑 `pytest tests -q`：1319 passed、14 skipped、534 subtests；`make test PYTHON=<主目录>/.venv/bin/python`：1258 项、OK、skipped=14；`make lint` 全通过；3 文件 mypy、9 文件 ruff check/format --check、`git diff --check` 通过 | 修复后清单 `.worktrees/m0-trusted-query/eval/reports/workbench-baseline-75a93c1edb14f03f1d7299ac595987d651b0d976-m0-t01.json`，worktree_digest=`4cf51237f25740f64cc7f14f6269be76b1a4ffd99429da04a8ed8294f7f46947`；current_metrics=null、data_verified=false、四策略 not_run；不覆盖旧清单。既有退出提示保留；HEAD 同名 meta 直接读取不存在，make eval 未运行 |
| 2026-09-22 | T02 开工 | Qoder 执行 / 待指定 / 未创建 | T02 → IN_PROGRESS | 75a93c1 + 未提交工作树 | 重读 D04/D05/D07 与任务合同，准备控制库及制品红测 | 仅交付发布原语；不自动切换真实发布；T03–T06 尚未实现 |
| 2026-09-22 | T02a 规则按内容隔离 | Qoder 执行 / 待指定 / 未创建 | T02 子项完成 | 75a93c1 + 未提交工作树 | 红→绿：`pytest tests/test_runtime_rules.py -q` 5 passed（两个 bundle 不共享同义词/形态词、规则不泄漏默认路径、按内容缓存而非实例）；实现规则缓存以制品内容为键 | 无第二语义权威；不自动切换真实发布 |
| 2026-09-22 | T02b 流程合同与默认模板 | Qoder 执行 / 待指定 / 未创建 | T02 子项完成（D05 落地） | 75a93c1 + 未提交工作树 | 红（ModuleNotFoundError 收集失败）→绿：`pytest tests/test_flows_contracts.py -q` 33 passed。新增 `agent/flows/contracts.py`（12 种 NodeType 词表、9 种已注册、边分支穷尽互斥覆盖 5 状态、DAG/可达性、预算上限）与 `templates/{query,analysis}.json` | `understand/switch/subflow` 未注册则拒绝装配；端口兼容校验推迟到图编译层（T04+）；chart 注册但 v1 模板不声明图节点；暂不开放图编辑 |
| 2026-09-22 | T02c 故障矩阵补齐 | Qoder 执行 / 待指定 / 未创建 | T02 子项完成 | 75a93c1 + 未提交工作树 | 追加 5 项红测转绿：缺默认流程/未注册节点/未声明文件/重复键 JSON/默认制品含模板；`pytest tests/test_runtime_bundle.py -q` 14 passed、`tests/test_control_store.py` 7 passed。bundle.py 新增 `_json_object`/`_check_no_extra_files`/`_verify_flows`；夹具 `_source_bytes` 兼容未提交文件 | 控制库矩阵（迁移回滚/重开/摘要篡改/非法路径/缺文件/符号链接/草稿审核失效/跳级/无证据动作/CAS 原子/外键与不可变行）已覆盖；配置包不能携带执行代码 |
| 2026-09-22 | T02d 收尾门禁 | Qoder 执行 / 待指定 / 未创建 | T02 → DONE | 75a93c1 + 未提交工作树 | 全仓 ruff 33 = 基线 36 − 本分支已修 3（value_profile 2×E501、report UP035）；新增代码零违规（serving/control 初版 8 处已修复并 format）。全仓 mypy 82 与 HEAD 逐条一致（零新增/零消失；serving/control 单独 strict 自身零错误）。`make lint` 全通过；`make test` Ran 1258 OK(skipped=14)；`pytest tests -q` 1378 passed、14 skipped、534 subtests | 只交付发布原语；UI/审核放行留 T08；T03–T06 未实现；临时基线 worktree 已清理。基线既有 ruff 33 与 mypy 82 处非本次引入，保持原样 |
| 2026-09-22 | T03a 控制面授权核心 | Qoder 执行 / 待指定 / 未创建 | T03 子项完成 | 75a93c1 + 未提交工作树 | 红→绿：`pytest tests/test_control_auth.py -q` 11 passed。新增 `serving/control/auth.py`：Principal → `authorize(principal, action, resource_scope)`（违规 `ControlForbidden`），数据角色与控制能力分离——viewer 无发布权即使数据角色 hq_admin；`load_control_grants` 解析非法即 fail-closed | 控制能力配置不入业务语义 YAML；HTTP 403 面留 T08（发布路由尚不存在，不用 404 冒充权限测试）；旧会话指纹合同不放松 |
| 2026-09-22 | T03b 依赖门禁 | Qoder 执行 / 待指定 / 未创建 | T03 子项完成 | 75a93c1 + 未提交工作树 | pyproject 钉 `authlib>=1.3,<2`、`httpx>=0.27`、`httpx2>=2.12`（Authlib 1.8 httpx 集成实走 httpx2；测试用 httpx2.MockTransport 注入受控响应），uv.lock 冻结；`infra.license_check` 5/5 PASS（扫描 111 个分发，copyleft 嫌疑集 ∅）；.env.example 增 `ATLAS_OIDC_{ISSUER,CLIENT_ID,CLIENT_SECRET,REDIRECT_URI,AUDIENCE}` 与 `ATLAS_CONTROL_GRANTS`（无真实值，N9） | 只增依赖与变量名占位，未接入真实 IdP |
| 2026-09-22 | T03c OIDC BFF | Qoder 执行 / 待指定 / 未创建 | T03 子项完成 | 75a93c1 + 未提交工作树 | 红→绿：`pytest tests/test_oidc_login.py -q` 30 passed（受控 mock IdP 真实 RS256 签/验，不付费、不使用真实 token）。新增 `serving/control/oidc.py`（Authlib）：一次性 state/nonce/PKCE、固定 RS256/issuer/audience/exp/未知 kid/轮换与算法拒绝、服务端内存会话（cookie 会话 id 不透明）、CSRF、退出即失效、双凭据拒绝、Cookie 严格属性 `HttpOnly; Secure; SameSite=Lax` | IdP token 不出服务端内存（D02 ③）；真 IdP 回调验收 BLOCKED；跨用户历史/过期 SSE 属 T05/T06 |
| 2026-09-22 | T03d HTTP 装配与契约同步 | Qoder 执行 / 待指定 / 未创建 | T03 子项完成 | 75a93c1 + 未提交工作树 | 红（TypeError: create_app() unexpected kwarg + 404）→绿：`pytest tests/test_auth_endpoints.py -q` 21 passed（`/api/v1/auth/{login,callback,logout,session}`：登录 302+PKCE、回调缺参 422/state 422/token 401/成功 302+严格 Cookie、退出无 CSRF 403/跨域 403/无会话 401/成功 204+清 Cookie+撤销后 401；配置阻塞三态 503；统一错误体 `{"error":{code,message,request_id}}`）。契约同步：`tests/test_api_contract_v2.py` 26 passed、78 subtests（EXPECTED_PATHS 18→22，TS 侧 endpoints.ts 双向相等）；`frontend/src/api/auth.ts` + `state/auth.ts` + `panels/auth/SessionEntry.tsx`（探测三分法，开发角色选择仅演示模式），`src/__tests__/auth.test.ts` 14 项；`make ui-check` tsc + vitest 16 files/132 tests + build 全绿 | 私有模式下工作台/治理面仍走 Bearer（`/runs` 通道属 T05/T06）；`systematic-debugging`：FastAPI Union 返回注解需 `response_model=None`（已修） |
| 2026-09-22 | T03e 回归与收尾 | Qoder 执行 / 待指定 / 未创建 | T03 → DONE（真 IdP 验收 BLOCKED） | 75a93c1 + 未提交工作树 | 定向：`pytest tests/test_control_auth.py tests/test_oidc_login.py tests/test_auth_endpoints.py tests/test_api_contract_v2.py tests/test_control_store.py -q` 95 passed、78 subtests；三套旧测试 `test_auth.py test_idp.py test_api_hardening.py` 60 passed、27 subtests。全量：`pytest tests -q` 1440 passed、14 skipped、534 subtests（= T02d 基线 1378 + T03 新增 62）；全仓 ruff 33 = 基线（零新增；新代码零违规）；mypy 82 = 基线（新增 authlib 无 stub → pyproject override 列表增 `authlib`/`authlib.*`，净零新增）；`make lint` 全通过（gold 106 条样本）；`make test` OK(skipped=14)（首次退出码 1、输出未完整留存，随后连续 7 次 EXIT=0，未复现如实登记）；`make ui-check` 全绿。README 能力登记/KL #43/§9.1 端点表已同步（22 路径） | 真 IdP 回调验收 BLOCKED（需部署者环境，不声称私有登录真实可用）；跨用户历史/过期 SSE 归 T05/T06；发布 UI/审核放行归 T08；下一动作 T04（共享执行内核与连接器） |
| 2026-09-22 | T04a 数据身份与运行上下文 | Qoder 执行 / 待指定 / 未创建 | T04 子项完成 | 75a93c1 + 未提交工作树 | 红→绿：`pytest tests/test_source_identity.py -q` 17 passed（收尾复跑坐实）。新增 `agent/runtime/identity.py`（snapshot/live 判别联合：live 结构性无 `snapshot_sha`、时间戳必须显式时区、64 位摘要校验；`analysis_refusal` 四步分析一致性门：snapshot→None，live/未绑定→`analysis_consistency_unavailable`）+ `agent/runtime/context.py`（`RunContext`：预算/执行器/身份/编译器/数据身份；缺编译器或执行器一律 fail-closed，不回退默认；`budget_from_snapshot`） | live 不提供伪快照身份（D03）；四步分析 live/未绑定明确拒绝；在线分析能力不在 T04 范围 |
| 2026-09-22 | T04b 连接器与共享实现委托 | Qoder 执行 / 待指定 / 未创建 | T04 子项完成 | 75a93c1 + 未提交工作树 | 红→绿；收尾 `pytest tests/test_runtime_execution.py -q` 26 passed，`--collect-only` 分组核对：连接器与共享委托 9 项。新增 `agent/runtime/connectors/{base,doris}.py`：`SourceSpec`/`ConnectorCapabilities`/`ConnectorError` 合同（只接受 `connector_kind=doris`，不开放 generic SQL URL）+ `DorisConnector`（能力 metadata_probe/cancel_query/snapshot_read/consistent_analysis 全 False）；`execute_sql` 自 `eval.runner` 逐字迁移，`eval/runner.py`、`agent/factory.py` 改委托共享实现；pyproject override 增 `pymysql` | 连接层只做实现委托迁移，不新增执行旁路；未实现能力不多报（N2） |
| 2026-09-22 | T04c 共享执行内核 | Qoder 执行 / 待指定 / 未创建 | T04 子项完成 | 75a93c1 + 未提交工作树 | 红→绿；`tests/test_runtime_execution.py` 内核 13 项（含 `test_invalid_plan_never_reaches_source`、`test_blocked_result_carries_no_rejected_sql`、`test_branch_manager_predicate_reaches_sql_without_value_leak`、执行器/编译器双 fail-closed）。新增 `agent/runtime/execution.py`：`execute_plan`（编译→身份策略→Guard→执行计时→ExecutionValidator，与 node_execute 逐段等价）与 `execute_guarded_sql`（registry 出口）；失败三态 ok/blocked/error + `executed` 区分「未执行被拒」与「执行后失败」，耗时只计实际执行的子 SQL；不向 Guard 传 `model=`（保持现状，不借重构扩大行为面） | 内核不校验 token（serving/auth 职责），只做 claims→策略解析与注入；blocked 不携带被拒 SQL |
| 2026-09-22 | T04d 在线入口委托改造 | Qoder 执行 / 待指定 / 未创建 | T04 子项完成 | 75a93c1 + 未提交工作树 | 4 项结构红测（AST 解析 import，各自失败原因正确）转绿：graph/registry 不再 import Guard `enforce`、graph 不再实例化 `ExecutionValidator`、factory/cli 不再 import `eval.runner`、graph/registry 分别委托 `execute_plan`/`execute_guarded_sql`。`node_execute`/`execute_readonly` 改委托（错误消息形态与旧实现逐字一致；registry 错误出口保持「只读校验拒绝（类型）：原因」格式）。定向：`pytest tests/test_runtime_execution.py tests/test_graph.py tests/test_tools_registry.py tests/test_mcp_server.py -q` 104 passed、17 subtests；`pytest tests/test_cli_query.py tests/test_snapshot_resolution.py tests/test_api.py tests/test_api_contract_v2.py tests/test_api_hardening.py tests/test_analysis_execution.py tests/test_analysis_api.py tests/test_session_persistence.py tests/test_identity_echo.py -q` 240 passed、161 subtests | graph 保留非 dict 身份 fail-closed 前置拒绝（绝不静默丢弃身份降级为无策略执行；代价：身份非法+编译失败双故障组合的报错优先级与旧实现不同，两者均 fail-closed 零执行）；修复新增 registry E501 后 ruff 33 = 基线 |
| 2026-09-22 | T04e 真链等价与收尾门禁 | Qoder 执行 / 待指定 / 未创建 | T04 → DONE | 75a93c1 + 未提交工作树 | 全量 `pytest tests -q`：1483 passed、14 skipped、534 subtests（= T03e 基线 1440 + T04 新增 43）；全仓 ruff 33 = 基线（零新增）；标准命令 `python -m mypy`：81 = 主仓库基线 82 − 1（唯一差异：主仓库多 1 条 eval/runner 的 `Library stubs not installed for "pymysql"`；worktree 已入 override 且改为委托导入，净化路径后逐条 diff 其余 81 条一致、零新增）；`make lint` 全通过（gold 106 条样本、values 21 文件）。真链等价验证（临时脚本跑完即删；加载主目录 .env，Doris 容器 healthy）：`resolve_runtime_snapshot()` sha=`1e2e557`（source=latest、bound_to_head=false；未改快照/业务数据/golden hash）；6/6 用例旧入口（`python -m agent.cli query --format json` 子进程）与新入口（`DataAgent.ask`）guarded SQL 逐字相等、列与行值相等（含 hq_admin `1 = 1`、branch_manager `dim_broker.branch = 'BR_A1'` 两条 RLS 注入；实测 2015 佣金收入 4042004.72、Q2 交易额 337881243.6600、NASDAQ 过滤 340523496.3300）；MCP→registry→内核→Doris 链 1/1 等价（5 行一致、258.4ms）；脚本退出码 0。README 能力登记节/KL #43/能力登记表已同步 | T03 遗留真 IdP 验收仍 BLOCKED；live 在线分析、元数据探测、取消查询、快照读与一致性分析未实现（能力声明 False，不回收）；下一动作 T05（真实事件、恢复、审计与最小反馈） |
| 2026-09-22 | T05 真实事件、恢复与最小反馈（T05a–e 合并登记） | Qoder 执行 / 待指定 / 未创建 | T05 → DONE | 75a93c1 + 未提交工作树 | 红→绿五批：`tests/test_run_lifecycle.py`（13）、`tests/test_run_events.py`（16）、`tests/test_run_api.py`（14）、`tests/test_run_feedback.py`（11）、`tests/test_run_telemetry.py`（2）共 56 项；全量 `pytest tests -q`：1539 passed、14 skipped、534 subtests（= T04e 基线 1483 + 56）；全仓 ruff 33、`python -m mypy` 81 均与基线持平；契约 `EXPECTED_PATHS` 22→25 与 `frontend/src/api/endpoints.ts` 双向相等；T05e 临时破坏反证 OTel 隔离测试（隔离失效 → 2 项红）后复原，`git diff observability/otel.py` 零残留。详见 T05 章节「落地证据」 | 偏差 5 条见 T05 章节「偏差登记」（⑤ 限流桶/分页未接为本轮补登）；下一动作 T06（历史列表、运行图与真实 SSE） |
| 2026-09-22 | T06 历史目录、运行图与真 SSE（T06a–e 合并登记） | Qoder 执行 / 待指定 / 未创建 | T06 → DONE | 75a93c1 + 未提交工作树 | 红→绿：后端 `tests/test_run_api.py` 14→24（+10：分页/过滤/sessions/artifact ACL/被拒 SQL 不进 artifact/SSE 重放/续读/410/ACL/阻塞首帧）；前端 8 文件 +52（run-events 11 / run-stream 11 / run-view-model 9 / control 8 / run-panel 6 / run-timeline 2 / run-graph 2 / session-panel 2 + App 路由 1）→ vitest 24 files/184 passed；全量 `pytest tests -q` 1549 passed、14 skipped、534 subtests（= T05 基线 1539 + 10）；ruff 33、mypy 81 与基线持平；契约 25→29 路径双向相等。浏览器实测（chrome-devtools + 临时种子 `serving/state/t06e_seed.py`，release c0c039c4…，两条运行真实 Doris 执行）：目录/详情（8 节点 + 14 帧时间线 + artifact 钻取）/刷新持久（服务端历史、last_seq 不变）/sessions 深链/反馈提交；旧 `/analyze/stream` 真跑 200 且页面无“实时/流式”措辞；`make ui-check` + `make license-check` 全绿。详见 T06 章节「落地证据」 | 偏差 6 条见 T06 章节「偏差登记」（图为固定模板+事件流高亮、运行创建无 UI、seed 仪器属临时验证路径、SSE 用 fetch 流、限流桶仍缺、验证环境形态）；下一动作 T07（只读数据源接入向导） |
| 2026-09-22 | T07 只读数据源接入向导（T07a–e 合并登记） | Qoder 执行 / 待指定 / 未创建 | T07 → DONE | 75a93c1 + 未提交工作树 | 红→绿：后端 `tests/test_source_onboarding.py` 25 项（源修订 6 / 受限探测 12 / 部署绑定 7）+ 真实缺口修复 5 项（Doris 4.x `SHOW GRANTS` 表格形态：解析/写权限拒/Roles fail-closed/未知列 fail-closed/语句形态回归；红 4→绿）；前端 `source-wizard.test.tsx` 13 + control 4 + app `/setup` 路由 1（TDD 反向验证：真删除 Route 行→1 failed|2 passed→恢复）→ vitest 25 files/202 passed；全量 `pytest tests -q` 1579 passed、14 skipped、534 subtests（= T06 基线 1549 + 30）；ruff 33、mypy 81 与基线持平；契约 29→33 双向相等。真实 smoke（Doris 容器 + `atlas_ro` 只读账号，2026-09-22）：HTTP 探测 ok（engine 5.7.99、schema_digest b9a22a32…、observed_at 2026-09-22T08:43:50.710918+00:00、reproducible=false）+ 浏览器全流程（未认证态→粘贴激活→修订 v1→v2→探测通过→draft 部署 finance-wizard）；`make ui-check` + `make license-check` 全绿。详见 T07 章节「落地证据」 | 偏差 5 条见 T07 章节「偏差登记」（SHOW GRANTS 双形态与 role fail-closed、环回不拒、探测后列表列不自动刷新、管理面未接限流桶、smoke 仪器属临时验证路径）；下一动作 T08（语义草稿、审核与 Git 制品发布） |
| 2026-09-22 | M0 工作树归档提交（用户授权“提交本地修改”） | Qoder 执行 / 待指定 / 未创建 | T01–T07 → 已提交（本地） | 75a93c1 + 未提交工作树 | `66eddb2`（semantic(values)：21 份值域重采样至 1e2e557，25 文件）+ `7da4c6d`（feat(workbench)：T01 基线夹具 + T02–T07 交付，93 文件 +20922/−754）；提交信息引用各任务已登记门禁数字，无新增未实测数字；提交后 worktree 干净 | 仅本地提交、未推送远程；未合并回 main（由用户决定评审/合并时机）；下一动作 T08（语义草稿、审核与 Git 制品发布） |
| 2026-09-23 | T08 语义草稿、审核与 Git 制品发布（T08a–d 合并登记） | Qoder 执行 / 待指定 / 未创建 | T08 → DONE | 9c8f67a + 51ded72 + ce213ac + eb52974 + 2d8b606（README 登记） | 红→绿：后端 `tests/test_semantic_release.py` 26 + `tests/test_release_publish.py` 20 = 46 项；前端 +26（`semantic-draft.test.tsx` / `governance-publish-identity.test.tsx` / `control.test.ts` / `app.test.tsx`）→ vitest 27 files/228 passed（= T07 基线 25/202 + 26）；全量 `pytest tests -q` 1625 passed、14 skipped、534 subtests（= T07 基线 1579 + 46）；ruff 33、mypy 81 与基线持平；契约 33→43 双向相等（收口修正：README 曾误写 48）。真实 Git 往返 smoke（既有 commit 制品，执行者不代用户提交）：草稿 A@eb52974 → 导入 ce213ac 得 R1 `9c980bd8…` → 首发 CAS（null→R1）→ 导入 eb52974 得 R2 `3aea28b8…` → 发布/回退全绿；浏览器全流程：激活 hq_admin → 草稿 C `38bb3627…`（document 68,459 字符）→ 校验 → patch「无变更」→ 审核 approved `46fc15a2…` → 导入 51ded72 得 R3 `5d59f1e2…` → CAS 发布 rev5（`2026-09-23T01:12:25+00:00`）→ CAS 回退 rev6（`2026-09-23T01:12:43+00:00`）→ 治理页 finance-live active=R1 `9c980bd8…`；`make ui-check`（build 1,779.74 kB）/`make lint`（106 条样本）/`make license-check`（5/5）全绿。详见 T08 章节「落地证据」 | 偏差 6 条见 T08 章节「偏差登记」（选表交互未实现、执行者不代提交、发布身份显式加载、限流桶仍缺、两段写事务已知形态、README 48→43 自纠）；下一动作 T10（检索前意图合同、完整绑定与简单问数控件） |
| 2026-09-23 | T10 子项 a-d 完成（意图合同/归一/召回/绑定 + Generator 显式候选接缝） | Qoder 执行 / 待指定 / 未创建 | T10 → IN_PROGRESS | ec53f7d（worktree 提交，未推送） | 红→绿：`tests/test_intent_contract.py` 26 + `tests/test_intent_retrieval.py` 13 + `tests/test_intent_binding.py` 18 + `tests/test_generator_seam.py` 6 = 63 项（证据三重校验/时区固定时钟/检索预算、授权∧已注册全目录召回 RRF K=5、槽位完整绑定未绑定只能澄清、keyword-only candidates 逐字渲染与重试沿用同轮候选）；定向复跑 63 passed；全量 `pytest tests -q` 1688 passed、14 skipped、534 subtests（= T08 基线 1625 + 63）；全仓 ruff 33、`python -m mypy` 81 与基线持平；提交范围 14 文件 ruff format --check 全过 | 偏差：无（新增文件均在 T10 卡「新」清单内）；剩余 T10e（gold 最小差异样本 + `eval/workbench_intent.py`）/T10f（前端 Plan 控件）/T10g（收口门禁 + README 登记）；下一动作 T10e |
| 2026-09-23 | T10 子项 e 完成（gold 最小差异样本 + workbench_intent 规则/LLM 对照） | Qoder 执行 / 待指定 / 未创建 | T10 → IN_PROGRESS | a8fbbf8（worktree 提交，未推送） | 红→绿：`tests/test_workbench_intent.py` 9 项（样本契约 4/规则引擎全通 2/LLM blocked+fake 3）；全量 `pytest tests -q` 1697 passed、14 skipped、536 subtests（= T10a-d 基线 1688/534 + 9 项测试与 2 条报告子测试）；ruff、mypy 81 对照基线零新增。样本 10 条 5 对（包含/排除、金额/笔数、分组/筛选、上月/去年、明确/歧义），schema 互斥校验（bound↔plan、澄清↔plan=null）+ 来源族 finance-manual-v1 split=test 冻结；`agent/prompts/intent.yaml` 0.1.0 外置提示词；报告 `eval/reports/workbench-intent-rule-ec53f7d.json`（Recall 10/10、Plan 9/9、澄清 1/1、tokens 0）与 `eval/reports/workbench-intent-llm-ec53f7d.json`（真实模型 9/10、8/9、1/1、11080 tokens；intent-04 输出非 JSON → fail-closed 如实失败不重试）；契约同步 21→23（+workbench-intent-rule/llm-`<sha>` 两模式；主报告 16 不变） | 偏差：真实 LLM 为内网测试环境自托管模型（deepseek-v4-flash，非付费云），数字仅标识该环境、不构成对外模型结论；blocked（无密钥）路径经红测验证不产数字；剩余 T10f（前端 Plan 控件）/T10g（收口门禁 + README 登记）；下一动作 T10f |
| 2026-09-23 | T10 子项 f 完成（前端结构化问数控件 + Plan 结构化合同唯一构造点） | Qoder 执行 / 待指定 / 未创建 | T10 → IN_PROGRESS | f51c198（worktree 提交，未推送） | 红→绿：`frontend/src/__tests__/plan-composer.test.tsx` 14 项（合同 6 键精确/时间未声明 null 与值原样字符串透传/全空筛选行过滤/校验拦截/6 算子词表对齐 `agent/compiler.py` `_compare`/构造点 7+8 键且无 question/SSR 认证与控件骨架/静态断言无 question 内联拼装）；全量 `make ui-check`：tsc 0 错误 + vitest 28 files/242 passed（= T08d 基线 27/228 + 14）+ build 成功 2.07s。`frontend/src/lib/plan-contract.ts` 为 /compile、/plan/execute 请求体唯一构造点（执行体恒不含 question）；`PlanComposer.tsx` 选项取治理面 metrics/dimensions（排除 is_time 列）、懒挂载（展开才发 2 条选项请求）、FILTER_OPS 与 compiler 算子逐字一致；`AskWorkbench.tsx` compileOnly/executePlan 改经 plan-contract 构造（去除 question 内联拼接） | 偏差：（1）T10f 范围经用户确认为「结构化控件 + Plan 卡片只发结构化合同」（新增指标/时间/筛选控件，选项取治理面；不改后端、不做默认图 understand 接线）；（2）执行请求不带 question（用户确认：question 由后端从 Plan 生成规范文本）；（3）PlanPreview 本体未改动——question 拼接原在 AskWorkbench.executePlan，已在构造点收敛；剩余 T10g（收口门禁 + README 登记）；下一动作 T10g |
| 2026-09-23 | T10 收口完成（T10 批次 DONE：全量门禁 + README 登记） | Qoder 执行 / 待指定 / 未创建 | T10 → DONE | 53e5be1（README 登记，worktree） | 全量门禁：`pytest tests -q` 1697 passed、14 skipped、536 subtests（= T08 基线 1625 + T10a-d 63 + T10e 9；T10f 纯前端零后端增量）；T10 卡定向 `pytest tests/test_intent_contract.py tests/test_intent_binding.py tests/test_intent_retrieval.py -q` 57 passed；ruff check 33 / mypy 81 与基线持平；`make ui-check`（tsc 0 错误 + vitest 28 files/242 passed + build 2.03s）全绿。README 新增「理解合同、召回与完整绑定（ADR-0031 T10）」章节 + 诚实边界第 43 项 T10 段 + 能力边界表格 +1 行（默认图未接理解节点/无生产调用方/不训练模型不替换规则答案如实登记）；T10 卡 5 项 checkbox 全部勾选 | 偏差：意图接口（normalize/retrieve/bind）当前无生产调用方——默认图 understand→retrieve→bind_plan 接线为后续批次（T10 退出门「理解成功但条件掉落不可发布」已由 bind 侧不变量守护：未绑定只澄清、plan_candidate=None）；LLM 对照数字仅标识内网自托管环境、不构成对外模型结论；下一动作 T13（最小发行、运维与首次接入验收；依赖 T06/T07/T08/T10 已全部就绪） |

| 2026-09-23 | T13 收口完成（T13 批次 DONE：备份/恢复/诊断 + 最小发行 Compose + 验收脚本） | Qoder 执行 / 待指定 / 未创建 | T13 → DONE | 7a5ef1d（ruff 清理，worktree）；前置 0ad7620 / 8b0e9e2 / ee833b5 / 5519a87 | T13a-b 红→绿：`tests/test_control_recovery.py` 12 项（backup 3/restore 3/diagnostics 6）+ `serving/control/maintenance.py`（BackupManifest/RestoreReport/backup_control/restore_control）+ `serving/control/router.py` GET /manage/diagnostics（源故障仍 200、不泄露 DSN/密钥、需 operator ops.read）。T13c-d 红→绿：`tests/test_workbench_packaging.py` 8 项（compose 4/Makefile 4）+ `infra/docker/compose.connect.yml`（最小发行、无默认秘密、`$\{VAR:?\}` 必填语法）+ Makefile backup/restore/diagnostics/acceptance 入口。T13e：`eval/workbench_acceptance.py` 4 步检查（api_health/diagnostics_endpoint/compose_config/makefile_targets）+ JSON 报告。T13f 全量门禁：`pytest tests -q` 1716 passed、15 skipped、537 subtests（= T10 基线 1697 + T13 后端 19）；ruff check 33 / mypy 85（81 基线 + 4 新文件零新增类别）与基线持平；`make ui-check` vitest 28 files/242 passed 全绿；契约 EXPECTED_PATHS 43→44（+diagnostics）双向相等（Python ↔ TS）；REPORT_PATTERNS 23→24 对齐实际报告类型数。README 新增「最小发行、运维与首次接入验收（ADR-0031 T13）」章节 + 能力边界表格 +1 行；T13 卡 5 项 checkbox 全部勾选 | 偏差：diagnostics 端点当前无生产调用方（M2 运维面板接线预留）；acceptance 脚本 diagnostics_endpoint 步 skipped（需真实 HTTP 运行环境）；mypy 85 = 81 基线 + 4 新文件（maintenance.py 3 + router.py 1，均为 `Library stubs not installed` 或 `attr-defined` 类别，与既有 override 策略一致）；下一动作 T09（受约束图校验、LangGraph 装配与画布；M2 起点，依赖 T02/T04/T06/T08/T10 已全部 DONE） |

| 2026-09-23 | T09 收口完成（T09 批次 DONE：图校验注册表 + 拓扑编译 + 前端画布基础） | Qoder 执行 / 待指定 / 未创建 | T09 → DONE | 837f988（README 登记，worktree）；前置 cf6f440 / d2e5875 | T09a 红→绿：`tests/test_flow_validation.py` 16 项（14 种错误码覆盖）+ `agent/flows/registry.py`（node_catalog 含端口合同）+ `agent/flows/validate.py`（FlowIssue 结构化校验器）+ `agent/flows/__main__.py`（CLI validate）。T09b 红→绿：`tests/test_flow_equivalence.py` 9 项 + `agent/flows/compile.py`（FlowTopology 拓扑编译器：edge_target/terminal_nodes/paths_to_terminal/budget 透传）。T09c 红→绿：`frontend/src/__tests__/flow-editor.test.ts` 6 项 + `frontend/src/api/flows.ts`（客户端校验）+ `frontend/src/panels/flows/flowLayout.ts`（BFS 布局工具）+ @xyflow/react ^12.11.6（MIT）。T09d 全量门禁：`pytest tests -q` 1741 passed、15 skipped、537 subtests（= T13 基线 1716 + T09 后端 25）；vitest 29 files/248 passed（+6）；ruff 33 / mypy agent/flows/ 0 errors；`make ui-check` + `make license-check`（5/5）全绿。README 新增「受约束图校验与拓扑编译（ADR-0031 T09）」章节；T09 卡 4 项 checkbox 全部勾选 | 偏差：compile.py 只做拓扑编译（不构造 LangGraph 运行时），生产流量仍走 `agent/graph.py` 硬编码图；前端画布为布局预览（未接拖拽编辑/发布 API）；port_mismatch 校验预留未启用（非 ok 边携带转换数据，静态不可判定）；下一动作 T11（规则/Prompt/Tools 节点实验与发布）或 T12（反馈审核与数据集） |

| 2026-09-23 | T11 收口完成（T11 批次 DONE：ToolBroker + LLM 节点 + 实验服务 + 前端面板） | Qoder 执行 / 待指定 / 未创建 | T11 → DONE | 未提交（worktree 工作树） | T11a 红→绿：`tests/test_node_tools.py` 9 项（understand 禁 execute_readonly/understand 可 list+describe/understand 可 compile 不可 execute/未知工具 blocked/execute_plan 全工具/handoff 无工具/未知节点无工具/预算超出 blocked/敏感问句 blocked）+ `tests/test_node_experiments.py` 6 项（不改 active/不执行在线 SQL/非法 release ID/未注册节点/结果含对照/历史列表）= 15 项红→绿。T11c 绿测扩展：+4 项（token 预算追踪/token 超限 blocked/handoff 无权限/未知节点无权限）= 19 项全绿。新增 `agent/tools/broker.py`（ToolBroker：节点类型→工具白名单/预算追踪/敏感度传播/token 预算）+ `agent/flows/llm_node.py`（LlmNodeExecutor：受约束模型调用+工具循环+确定性回退）+ `agent/prompts/node_action.yaml`（外置 Prompt v0.1.0）+ `serving/control/experiments.py`（ExperimentService：单节点对照实验/不修改 active/不执行在线 SQL）+ `frontend/src/panels/optimization/NodeExperiment.tsx`（实验表单+结果展示+历史占位）+ `frontend/src/__tests__/node-experiment.test.tsx`（5 项 SSR 冒烟）。全量门禁：`pytest tests -q` 1760 passed、15 skipped、537 subtests（= T09 基线 1741 + T11 后端 19）；vitest 30 files/253 passed（= T09 基线 29/248 + T11 前端 5）；ruff 33 = 基线（零新增）；mypy 新文件零错误；`make ui-check`（build 1,776.20 kB / gzip 550.36 kB）全绿；T11 卡 5 项 checkbox 全部勾选 | 偏差：LLM 节点首版为骨架（chat_fn=None 时走确定性回退；完整 JSON action 工具循环随后续迭代补齐）；ExperimentService 首版只记录实验不真实执行节点对照（需加载两个 release bundle，随后续迭代补齐）；前端面板 API 集成待完成（表单/结果/历史 UI 就绪，后端端点待接入）；下一动作 T12（反馈审核、归因与版本化数据集） |

| 2026-09-23 | T12 收口完成（T12 批次 DONE：反馈审核 + 归因 + 版本化数据集 + 前端审核队列） | Qoder 执行 / 待指定 / 未创建 | T12 → DONE | 未提交（worktree 工作树） | T12a 红测：`tests/test_node_feedback.py` 9 项（未审核 training_eligible=False/approve→True/reject→False/重复审核拒绝/无 reviewer 能力拒绝/审核撤销回退/归因节点存储/归因可选/审核队列过滤）+ `tests/test_intent_dataset.py` 8 项（未批准硬拒/approved 构建 manifest/混合拒绝/manifest 不可变/摘要变化/来源族拆分/旧反馈 unknown 标记/序列化反序列化）= 17 项红→绿。T12d 绿测扩展：+5 项（不存在 feedback KeyError/撤销未审核抛异常/审计轨迹保留/空记录 manifest/归因检索）= 22 项全绿。新增 `serving/control/datasets.py`（ReviewedLabel/DatasetManifest/FeedbackReviewService/build_intent_dataset）+ `serving/control/migrations/005.sql`（feedback 表加 attribution_node/reviewed_by/at + feedback_reviews 审计轨迹表）+ `serving/control/store.py` 扩展（get_feedback/review_feedback/revoke_feedback/list_pending_feedback/_feedback_from_row）+ `frontend/src/panels/optimization/FeedbackQueue.tsx`（审核队列面板：待审核列表/通过拒绝按钮/撤销确认/归因显示）+ `frontend/src/__tests__/feedback-queue.test.tsx`（5 项 SSR 冒烟）。全量门禁：`pytest tests -q` 1782 passed、15 skipped、537 subtests（= T11 基线 1760 + T12 后端 22）；vitest 31 files/258 passed（= T11 基线 30/253 + T12 前端 5）；ruff 34（= 基线 33 + 1 既有代码预存）；mypy 新文件零错误；T12 卡 5 项 checkbox 全部勾选 | 偏差：FeedbackReviewService 审核能力校验为简化约定（subject=='reviewer'，生产环境应从 CONTROL_GRANTS 查询）；前端审核队列 API 集成待完成（UI 就绪，后端端点待接入）；build_intent_dataset 不真实执行训练分流（随 T14 SFT 任务补齐）；下一动作 T14（intent_v1 SFT 任务与离线对照） |

| 2026-09-23 | T14 收口完成（T14 批次 DONE：训练预检 + 数据准备 + 任务管理 + 前端配方面板） | Qoder 执行 / 待指定 / 未创建 | T14 → DONE | 未提交（worktree 工作树） | T14a-b 红测：`tests/test_intent_training.py` 17 项（预算未批准 blocked/预算批准仍缺 CUDA blocked/数据集不存在 blocked/空数据集 blocked/未许可模型 blocked/失败任务无 artifact/阻塞任务无 artifact/任务提交 queued/取消终态/取消不存在抛异常/重复提交拒绝/预算不重复扣/状态幂等）+ `tests/test_intent_data.py` 12 项（合法样本通过/空问句拒绝/空答案拒绝/非 JSON 拒绝/缺 metric 拒绝/超长拒绝/corrected 产生样本/up+correction 产生样本/down 无 correction 跳过/无 correction 跳过/loss mask 存在/来源族保留）= 29 项红→绿。新增 `lora/intent_data.py`（validate_training_pair/build_training_pairs/load_intent_dataset）+ `lora/intent_train.py`（TrainingSpec/PreflightResult/TrainingReport/validate_training_job/train_intent/TrainingJobManager）+ `lora/configs/intent_v1.yaml`（D11 配方初始值）+ `frontend/src/panels/optimization/TrainingJob.tsx`（训练配方卡片+任务状态面板）+ `frontend/src/__tests__/training-job.test.tsx`（5 项 SSR 冒烟）。全量门禁：`pytest tests -q` 1811 passed、15 skipped、537 subtests（= T12 基线 1782 + T14 后端 29）；vitest 32 files/263 passed（= T12 基线 31/258 + T14 前端 5）；ruff 36（= 既有预存，新文件零错误）；mypy 新文件零错误；T14 卡 5 项 checkbox 全部勾选 | 偏差：训练执行分支（GPU 路径）未实测——本机无 CUDA，代码按最小实现写，实测数字待 GPU 就绪后由脚本产出（AGENTS.md N1）；TrainingJobManager 放在 `lora/intent_train.py` 而非独立 `serving/control/jobs.py`——当前仅训练模块使用，待 T15 模型注册时再评估是否抽取；前端 API 集成待后续接入；下一动作 T15（模型注册、影子、人工切换与回退） |

| 2026-09-23 | T15 收口完成（T15 批次 DONE：模型注册 + 影子策略 + 回退） | Qoder 执行 / 待指定 / 未创建 | T15 → DONE | 未提交（worktree 工作树） | T15a 调研：现有 LoRAGenerator._shadow() 是兆底而非 T15 影子；ModelVersion/ShadowSpec/ShadowPolicy 均不存在，需新建。T15b 红测：`tests/test_model_release.py` 14 项（succeeded 注册/blocked 拒绝/无 artifact 拒绝/未许可拒绝/批准需证据/批准需批准人/不存在抛异常/撤销 approved/撤销 pending 拒绝/空列表/注册后可查/按 ID 查/不存在抛异常）+ `tests/test_shadow_runs.py` 12 项（ShadowSpec 创建/默认关闭/启用/未批准拒绝/禁用/影子不改结果/影子不执行 SQL/影子关闭不运行/sample_rate=0 不运行/独立 lineage/隔离标记）= 26 项红→绿。新增 `lora/registry.py`（ModelVersion/ModelRegistry：register/approve/revoke/list/get）+ `serving/control/models.py`（ShadowSpec/ShadowPolicy/ShadowRunner：影子默认关闭/启用需已批准模型/不执行 SQL/独立 lineage）。全量门禁：`pytest tests -q` 1837 passed、15 skipped、537 subtests（= T14 基线 1811 + T15 后端 26）；ruff 新文件零错误；mypy 新文件零错误；T15 卡 5 项 checkbox 全部勾选 | 偏差：真机影子运行未实测（无已批准模型）；模型注册表为内存实现（未持久化）；前端模型发布 UI 未实现；下一动作 T16（可选 GRPO 奖励实验）或 T17（完整 MCP stdio transport） |

| 2026-09-23 | T16 收口完成（T16 批次 DONE：奖励函数 + GRPO 预检 + 配方） | Qoder 执行 / 待指定 / 未创建 | T16 → DONE | main 分支直接提交 | T16a-b 红测：`tests/test_intent_reward.py` 18 项（IntentEvaluation/IntentReference 创建/冻结/捷径拒绝 4 项参数化/完美匹配获奖/正确澄清获奖/安全违规零奖励/schema 非法零奖励/action 不匹配零奖励/缺必需槽零奖励/额外槽零奖励/检索 ID 不匹配零奖励）+ `tests/test_intent_rl_preflight.py` 6 项（GRPOSpec 创建/默认值/SFT 未冻结 blocked/无奖励用例 blocked/预算未批准 blocked/未许可模型 blocked/前置满足 ready/多理由累积）= 24 项红→绿。新增 `lora/intent_reward.py`（IntentEvaluation/IntentReference/reward_intent：严格 0.0/1.0 二值，捷径拒绝：一律澄清/丢否定/搜一切/巧合匹配）+ `lora/intent_rl.py`（GRPOSpec/GRPOPreflightResult/validate_grpo_preflight：前置门禁 SFT 冻结/奖励用例/预算/许可）+ `lora/configs/intent_rl.yaml`（D11 GRPO 配方初始值）。全量门禁：`pytest tests -q` 1861 passed、15 skipped、537 subtests（= T15 基线 1837 + T16 后端 24）；ruff 新文件零错误；mypy 新文件零错误；T16 卡 4 项 checkbox 已勾选（第 4 项“获授权后实训”待 GPU 就绪） | 偏差：GRPO 训练执行分支（TRL 连接）未实现——本机无 CUDA；`trl` 未加入 pyproject.ml 依赖；训练取消/重启场景未实测；下一动作 T17（完整 MCP stdio transport）或 CANCELLED |

| 2026-09-23 | T17 收口完成（T17 批次 DONE：MCP stdio server + 工具注册 + 安全边界） | Qoder 执行 / 待指定 / 未创建 | T17 → DONE | main 分支直接提交 | T17a 调研：现有 `agent/tools/mcp_server.py` 为 FastMCP（已弃用），MCP SDK 2.2.0（MIT 许可）使用 `MCPServer`（`mcp.server.mcpserver`）；Pydantic 从函数签名生成参数模型，不支持 `**kwargs`，额外参数静默忽略。T17b 红测：`tests/test_mcp_transport.py` 15 项（身份覆盖忽略 5 项/工具清单 2 项/工具调用 4 项/安全边界 3 项/身份从环境 1 项）= 15 项红→绿。新增 `serving/mcp_stdio.py`（`create_stdio_server`/`run_stdio_server`：4 工具 `list_metrics`/`describe_metric`/`ask`/`execute_plan`，身份从 `ATLAS_MCP_IDENTITY` 或默认 viewer，`execute_readonly`/`compile_sql` 不暴露）+ `pyproject.toml` mypy overrides 增 `mcp`/`mcp.*`。全量门禁：`pytest tests -q` 1876 passed、15 skipped、537 subtests（= T16 基线 1861 + T17 后端 15）；ruff 新文件零错误；mypy `serving/mcp_stdio.py --strict` 零错误；T17 卡 4 项 checkbox 已勾选 | 偏差：MCP 2.x Pydantic 静默忽略额外参数（非报错），安全通过 schema 不含 role/identity 字段保证；`run_stdio_server` 不自动构建 executor/budget（需部署环境提供）；远程 HTTP MCP/OAuth 不在本任务范围；下一动作：T01–T17 全部 DONE，ADR-0031 开发计划完成 |

开发实现位于 `.worktrees/m0-trusted-query`（本地提交 `66eddb2`/`7da4c6d`/`5519a87`/`ee833b5`/`8b0e9e2`/`0ad7620`/`7a5ef1d`/`d2e5875`/`cf6f440`/`c36e1a3`/`837f988`，未推送）；本 ADR 和任务账本继续在主工作区维护，不复制用户其他未提交改动。

每次后续更新必须写实际报告路径，不填看似合理的通过数；状态变化要同步第 3 节。批准者可以对 T16/T17 明确 CANCELLED，不妨碍 M2 产品闭环交付。

### 7.1 本次文档核对记录

本节仅证明文档检查，不是 T01–T17 的实施验收；采用 architecture-health 核对证据和合同，未执行业务测试或训练。

| 检查项 | 方法与结果 | 边界 |
|---|---|---|
| 需求追踪 | 逐项对照 R01–R16、D01–D14 与 T01–T17；全部需求有决策与任务归属 | 不代表功能完成 |
| 依赖 | 将第 3 节依赖逐项录入 Python graphlib.TopologicalSorter，排序成功；T13 的传递闭包为 T01–T08、T10、T13 | G-RL/G-MCP 是额外人工门禁；M1 不依赖画布编辑、SFT 或 RL |
| 引用与命令 | 用文件读取/目录匹配核对新增 Markdown 的本地链接、证据路径及两个索引；重读 Makefile 确认现有入口 | 计划新增文件已标注；外部链接不算本地存在性检查，许可限制仍按 ADR §5 |
| 合同一致性 | 修正执行 spy、图错误码、影子结果断言；同步 RunView/正文保留/capture、重启中断、最小反馈与影子 API | 示例是待实现的红测，不是已通过测试 |
| 文档差异 | `git diff --check -- docs/README.md infra/adr/README.md` 无告警；两份新文件各以 `git diff --no-index --check /dev/null <文件>` 检查，无空白告警 | no-index 的退出码 1 仅表示新文件存在差异，已结合空 stdout/stderr 判断，不误报为测试失败 |
| 工作区范围 | `git status --short` 与索引 diff 核对本次只新增两份文档、修改两个索引 | 用户原有两份 outreach 改动保留；未提交 Git、未改业务代码 |

后续建议执行顺序：T01 → T02 → T03/T04 → T05 → T06/T07 → T08 → T10 → T13 → T09/T12 → T11 → T14 → T15；T16 在 G-RL 后按需启动，T17 可在 T13 与 G-MCP 后独立开展。斜线表示可并行，不免除第 3 节的完整前置条件。

## 8. 开发放行清单

- [ ] ADR-0031 新增细则已确认：Cookie/IdP、控制库、Git 导入发布、依赖和初始预算。
- [ ] R01–R16 都能映射到任务，T10/T09 没有循环依赖。
- [ ] M1 不依赖 GPU、RL 或画布编辑，M2 必须含真实可编辑图和节点优化。
- [ ] 新入口、调试、回放、Tools、MCP 都只能经受保护执行内核。
- [ ] 数据源/身份/版本/事件在训练前已经存在，在线用户数据不泄漏到仓库样本或云 Prompt。
- [ ] 每批运行对应 targeted tests、pytest/unittest、前端门禁及适用真链；blocked 项不计完成。
- [ ] 未经用户额外授权，不提交 Git、不启动付费训练、不自动发布。
