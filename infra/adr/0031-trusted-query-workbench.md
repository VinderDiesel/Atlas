# ADR-0031：可信问数产品闭环与受约束流程工作台

- 日期：2026-09-22
- 状态：**M0 实施已获用户授权（2026-09-22）；两轮产品方向已确认。T01–T06（M0）与 T07（M1 首任务）已交付；其余阶段未启动，实际完成状态以任务账本和验收证据为准**。
- 决策范围：单团队私有部署；接入、语义发布、受约束图编排、运行排查、节点优化与可选训练闭环。
- 文档证据基线：代码 `75a93c1`，原文档交付仅只读核验，无新增业务效果数字；后续 M0 开发与验证单独登记在任务账本。
- 实施与状态唯一入口：[开发任务表](../../docs/design/dev-plan-0031-trusted-query-workbench.md)。本文决定边界与合同，任务表记录任务状态、阻塞与验收证据；二者冲突以本文为准。
- 决策原则：继承 [AGENTS.md](../../AGENTS.md) 的 N1–N10；不自动审核、提交 Git、发布、训练或重置数据。

## 1. 背景与两轮需求合并

第一轮目标是“用更少精力完成接入、使用、运维、排查与反馈优化”，不是先建设训练平台。第二轮明确“完整流程可视化、每个节点可配置优化、受约束图编排”，并否决候选重排作为首个小模型试点。用户现要求两轮查漏合并，形成后续开发的可追踪决策。

目标定位：**面向单团队私有部署的可信问数产品，以金融和 FIBO 为首个标准场景；规则、LLM、自训练小模型共享同一语义与安全合同。** 通用化接入与优化机制，不宣称通用行业覆盖或通用 SOTA。

### 1.1 当前事实、缺口与实现证据

| 编号 | 已有资产 | 本次确认的边界；不得当成已交付目标 | 证据 |
|---|---|---|---|
| E01 | Ossie、FIBO、同义词、值域、RowPolicy | Git 维护；治理 HTTP 面只读，无草稿审核发布产品链 | [语义层](../../semantic/ossie/)、[治理入口](../../serving/governance.py) |
| E02 | 规则 Planner、Compiler、Guard、执行校验 | 确定性主路径已存在；不能换成模型直接生成并执行 SQL | [编排](../../agent/graph.py)、[Compiler](../../agent/compiler.py)、[Guard](../../agent/security/sql_guard.py) |
| E03 | 固定四步两期贡献分析、Decimal 综合 | 非任意分析 DAG；不推断业务因果，完整性资格不因可编排而取消 | [分析模块](../../agent/analysis.py)、[资格校验](../../eval/analysis_eligibility.py) |
| E04 | 工作台、会话、治理页面、分析步骤卡片 | 会话列表来自标签页内存，不是历史运行目录；没有图编辑器 | [App](../../frontend/src/App.tsx)、[会话面板](../../frontend/src/panels/sessions/SessionPanel.tsx)、[分析步骤](../../frontend/src/panels/workbench/AnalysisBlock.tsx) |
| E05 | Generator、LLM 分级、接地叙述 | HTTP `candidate-fallback` 测试只保证门控；默认真实工厂未装配完整候选链；可移植 chat 请求无 Tools 执行循环 | [服务入口](../../serving/api.py)、[Generator](../../agent/generator.py)、[服务测试](../../tests/test_api_llm_serving.py) |
| E06 | LangGraph checkpoint、OTel、审计、分析 SSE | checkpoint 不是运行索引；SSE 先计算再发送，不是实时进度 | [工厂](../../agent/factory.py)、[服务入口](../../serving/api.py)、[OTel](../../observability/otel.py) |
| E07 | Doris 执行、运行快照解析 | 在线工厂依赖 `eval.runner` 的执行与快照预算；不能直接配置持续变化的任意数据库 | [工厂](../../agent/factory.py)、[身份](../../data/identity.py)、[评测执行](../../eval/runner.py) |
| E08 | HS256、外部 RS256/JWKS 校验组件 | `require_bearer` 当前调用本地验签；外部校验组件不等于浏览器登录和 HTTP 接线已完成 | [认证](../../serving/auth.py)、[IdP 测试](../../tests/test_idp.py) |
| E09 | 反馈、人工审核、候选提议、SFT/QLoRA、推理回退 | 用户信号不是真值；未形成节点样本→训练→比较→替换闭环；GPU 分支不能仅凭脚本存在声称可用 | [反馈](../../agent/feedback.py)、[飞轮](../../lora/flywheel.py)、[训练](../../lora/train.py) |
| E10 | 图粗筛、BM25、重排、可插拔判别客户端 | 维度检测在召回前影响候选域；重排不能恢复漏召回；默认服务未见 Jev 装配 | [SchemaLinker](../../agent/tools/schema_linker.py)、[重排](../../retrieval/rerank.py)、[Jev](../../agent/jev_engine.py) |
| E11 | Plan JSON 校验、候选上下文 | 旧候选合同构造 `filters=()`；图和 Generator 各检索一次；Generator 还有全量 Metric 清单，不能说它只看 top-K | [Generator](../../agent/generator.py)、[Prompt](../../agent/prompts/generator_plan.yaml)、[图](../../agent/graph.py) |
| E12 | 本地 MCP 风格工具和 Scope | 没有 MCP transport，不是完整 MCP server | [工具封装](../../agent/tools/mcp_server.py) |
| E13 | 固定快照、Plan Acc/EX、历史报告 | [汇总](../../EVAL_REPORT.md) 与 [ccb4c8b 报告](../../eval/reports/ccb4c8b.json) 是历史证据，不是当前 HEAD 或小模型收益 | [评测方法](0010-eval-methodology.md) |

### 1.2 需求覆盖与归属

下列 R 编号永久保留；实现状态只在任务表维护。

| 需求 | 两轮合并后的交付 | 决策 | 开发任务 |
|---|---|---|---|
| R01 开箱即用与私有部署 | 接入/演示模式、首次使用向导、依赖诊断、无需 GPU 使用默认流程 | D01、D02 | T01、T03、T07、T13 |
| R02 数据源接入 | Doris 只读连接、选表、元数据预览、能力探测、凭据引用 | D03 | T04、T07 |
| R03 语义配置 | 草稿、口径审核、样例、Git 导入、发布、回退、影响面 | D04 | T08 |
| R04 简单问数 | 业务 Plan 卡片、结构化澄清、指标/时间/筛选控件、空结果与故障区分 | D02、D09 | T10、T13 |
| R05 全流程可见 | 定义图、实际运行图、时间线、重试、工具调用、分析子步骤 | D05、D07 | T05、T06、T09 |
| R06 可编辑图 | 允许节点增删、连线、分支、子流程、兼容实现替换；安全必经 | D05、D06 | T09 |
| R07 LLM 节点优化 | Prompt、上下文、Tools、模型、预算、同样本对照 | D08 | T11 |
| R08 规则节点优化 | 同义词/形态规则编辑、命中依据、冲突与回归 | D04、D08 | T08、T11 |
| R09 小模型节点优化 | 样本修正、SFT 配方、训练任务、模型版本、重新训练、切换 | D10、D11 | T12、T14、T15 |
| R10 历史排查与运维 | 搜索运行、身份裁剪、故障归因、保留期、备份恢复 | D02、D07、D12 | T03、T05、T06、T13 |
| R11 正反反馈飞轮 | 用户信号/审核真值分离、问题聚类、数据集版本、防泄漏 | D10 | T05、T06、T12 |
| R12 首个模型不做重排 | 检索前问句理解与检索规划，条件贯通到 Plan/SQL | D09、D11 | T10、T14 |
| R13 替换与回退 | 节点合同、不可变发布包、影子比较、人工批准、回退演练 | D04、D05、D11 | T02、T09、T15 |
| R14 可选强化学习 | SFT 基线、隔离训练、可验证奖励、资源门禁 | D11 | T16 |
| R15 接入生态 | HTTP 主入口、CLI 同源；完整 MCP 为后续受门禁增量 | D13 | T04、T17 |
| R16 可证实的投入与效果 | 接入/修复耗时、人工量、Plan Acc/EX、选择性风险、资源与成本分报 | D14 | T01、T10、T13–T16 |

## 2. 备选方案

| 议题 | 选定方案与理由 | 未选方案与原因 |
|---|---|---|
| 产品主线 | 产品闭环先行、局部模型增强；先产生可定位可审核的问题 | 模型训练优先：缺少真值与发布证据；只扩规则：不解决接入体验 |
| 编排 | 声明式受约束图 → LangGraph；画布只是编辑器 | 固定 DAG 仅调参数不满足用户选择；通用脚本工作流扩大执行攻击面 |
| 图交互 | React Flow 社区组件，仅处理布局/交互 | 自绘连接、拖拽和键盘系统维护面更大；成品 Agent 平台有第二运行时和迁移成本 |
| 状态存储 | 单进程本地 SQLite 控制面，与 checkpoint 分库 | 直接读 checkpoint 充当运行索引无法稳定授权；先上分布式队列/数据库超出单团队范围 |
| 配置权威 | Git 发布源 + SQLite 非权威草稿/运行状态 + 不可变发布制品 | 浏览器修改在线 active 语义会形成第二事实源；每次查询读可变工作树破坏重现 |
| 首个模型 | 检索前问句理解与检索规划 | 重排只能改善已召回项；全文 Plan 生成混合语言理解与物理绑定，难归因；叙述不能改善召回 |
| 训练顺序 | 规则/LLM 对照 → SFT → 有条件 RL | 直接套 Nimble 或直接 RL：任务合同、中文效果与奖励都没有本项目证据 |
| 在线数据 | 显式在线数据身份；评测固定快照保持独立 | 把在线变化伪装成快照会生成不可复现的 EX；强制用户重建湖仓妨碍接入 |

## 3. 决策

> 以下全部是目标合同，**不是现有 API、依赖或已运行功能**。预算中的固定上限为首版设计值，不是实测最优值；调整须改变配置版本并重做相同预算对照。

### D01 部署与范围

- 首版单团队、单 API 进程、单业务执行队列；保留现有锁的安全语义，不宣称并行查询能力。
- **接入模式**只需要 API+构建好的前端、SQLite 本地持久卷及已有 Doris；不依赖 Polaris/MinIO/Milvus/GPU 启动。BM25 为默认检索。
- **演示模式**沿用公开金融/零售数据及湖仓配置；不在启动、升级或健康检查中隐式装载/重置数据。`make seed` 仍需用户明确授权。
- 新接入 Compose 文件独立于现有 `docker-compose.yml`，先不重排已有全栈服务；向量、观测、训练各自可选。只发布已有能力与限制，不承诺“一条命令完成真实数据准备”。
- 金融是标准领域包，零售用于已有回归。第二种数据库、跨源 JOIN、多租户 SaaS、多 worker、通用任意分析 DAG、自动在线训练不在首版。

### D02 用户路径、身份与权限

用户路径：登录 → 选择已发布领域/部署 → 使用示例或结构化控件问数 → 口径卡片/结果 → 查看运行图 → 反馈。管理员路径：接入数据源 → 语义草稿 → 验证 → Git 落源 → 发布；优化者路径：定位节点 → 改草稿/标注 → 对照 → 批准发布/回退。普通用户无须编辑流程图。

身份方案：

1. 开发/本地演示继续使用 HS256 Bearer，无默认密钥，角色切换仅在演示模式可见；非 loopback 私有部署禁止该模式。
2. 私有部署使用现有 IdP 的 OIDC Authorization Code + PKCE，服务端 BFF 通过 Authlib 完成标准协议；固定 issuer/audience/redirect URI，校验 state、nonce、exp、签名、算法和 JWKS 轮换。未配置 IdP 显示配置阻塞，不伪装登录成功。
3. OIDC token 仅在服务端内存，浏览器仅收不透明 `HttpOnly; Secure; SameSite=Lax` 会话 Cookie；服务重启要求重新登录。Cookie 是本 ADR 对旧“零持久化”规则的狭义扩展，JS 不读写 Cookie、不持久化 token/会话内容，仍禁止 localStorage/sessionStorage/IndexedDB。
4. Cookie 写请求校验 CSRF token 与同源 Origin；身份同时来自 Cookie/Bearer 时拒绝，不混用。外部 HTTP/CLI 仍用 Bearer，接通固定模式的 RS256 验签；绝不按客户端 header.alg 自动选择信任模式。
5. 稳定所有者为 `(issuer, subject)`；运行还绑定角色、授权作用域与策略摘要。旧会话沿用全量 claims 指纹校验，换 token/身份仍新建会话，不在此放宽 checkpoint 会话合同。

控制面能力与数据角色正交，默认不自动把 `hq_admin` 升为发布者。部署方用受保护配置授权下列能力：

| 角色模板 | 控制面能力 | 数据可见性 |
|---|---|---|
| viewer | `run.create`、`run.read_own`、`feedback.submit` | 仍受领域/RowPolicy 限制 |
| operator | `source.manage`、`deployment.manage`、`ops.read`、`run.read_summary` | 只读运行摘要不等于可见问题、结果或 Prompt |
| editor | `draft.edit`、`draft.validate`、`draft.export`、`experiment.run`、`dataset.create` | 仅授权领域与被授权脱敏样本 |
| reviewer | `draft.review`、`feedback.review`、`dataset.approve`、`model.approve` | 审核范围单独授权 |
| publisher | `release.import`、`release.publish`、`release.rollback`、`shadow.manage`、`train.start`、`train.cancel` | 发布与训练不授予更大的查询权限 |

角色模板不隐式继承其他模板；对象列表/详情读取按对应能力与领域 scope 裁剪，原文读取还需独立内容授权。同一人可兼任多个角色，但编辑、审核、发布必须是不同显式动作并分别审计。查看历史内容要求所有者授权与当前数据权限同时满足；跨人只开放脱敏摘要，原文/结果需显式内容权限且保守校验旧授权作用域。权限变化无法证明兼容时只展示摘要，不返回旧结果；新查询使用当前身份与策略。

### D03 连接器与数据身份

连接器首版只注册 Doris，复用 PyMySQL，不把支持 MySQL 协议等同于支持 MySQL 数据源。

- `SourceSpec`：`source_id/revision/connector_kind/secret_ref/allowed_catalogs/allowed_tables/timezone/tls_policy/query_budget`；非秘密配置落受保护的部署配置，秘密只由环境变量引用解析。UI 不接收、不回显明文密码或完整 DSN。
- `ConnectorCapabilities`：`dialect/read_only/metadata_probe/cancel_query/snapshot_read/consistent_analysis`；连接探测结果是有时间戳的证据，不是永久保证。
- 连接器负责连接、受限元数据探测和**受信执行内核传入的已过 Guard 查询**；LLM、HTTP 请求和插件拿不到裸数据库连接。
- 元数据探测只使用固定、参数化的目录查询，限白名单目录；不可通过“测试连接”运行用户 SQL。不用业务写入测试验证只读权限。
- 连接地址由管理员允许列表限制；禁止云元数据、link-local、任意重定向及未授权目标，私网地址只能显式允许，TLS 校验默认打开。数据账号由部署方授予只读，Atlas 不自动 GRANT。
- 表白名单、允许关系、预算来自该发布源合同，不再借用不相干的评测快照；接入前验证语义物理表都属于允许集合，缺列/类型漂移则阻断受影响部署。

数据身份为带判别字段的联合类型：

```text
DataIdentity =
  {mode: "snapshot", snapshot_sha, snapshot_digest, verified_at}
  | {mode: "live", source_id, source_revision, schema_digest,
     observed_at, source_version: string|null, reproducible: false}
```

`live` 无法固定版本时必须标不可精确重放；不能填写伪 snapshot_sha。运行包固定配置，不声称能冻结在线数据。新结构在 `/runs` 使用；旧固定快照 `/ask` 契约保留。评测继续只接受 `data/snapshots/` 的固定快照，启动前与结束后复核指纹；在线 smoke 是接入验收，不计 EX。

在线相对时间在接收请求时固定 `reference_time + timezone`，时间解析仍由确定性函数完成；评测注入固定时钟。首版支持“上个月/上季度/去年”，其他相对表达澄清。编译为明确区间，不能丢弃成全量时间。四步分析在在线源没有一致性读能力时返回 `analysis_consistency_unavailable`，不得用四次变化中的读取做虚假贡献对账。

### D04 草稿、Git 权威源与发布包

生命周期：`draft → validated → reviewed → source_imported → release_ready → published → retired`；修改内容返回 draft 并使旧验证/审核失效。

- 语义定义只落 `semantic/ossie/`；同义词/形态规则仍在 `semantic/synonyms/`，值域机器本体与人工别名分开；Prompt 仍在 `agent/prompts/*.yaml`，保留 version/owner/description/changelog。
- UI 编辑写 SQLite 非权威草稿；不写当前 active 文件。提供包含 base Git sha、内容摘要、变更影响面的 patch 下载。首版由人把 patch 合入 Git，再在界面导入**显式指定 commit 的制品**；服务不自动 commit/push/merge，也不读取脏工作树当发布源。
- 导入仅允许白名单相对路径和大小，拒绝路径穿越、符号链接、代码文件和压缩炸弹；不从上传包执行代码。草稿摘要与导入内容不一致须重新审核。
- 每次发布检查 Schema、同名 active Metric、血缘/关系、RowPolicy、值域绑定、FIBO 对齐、方言能力、样例编译与回归。发布 UI 显示受影响 Metric/报表/流程。
- 流程定义新增权威目录 `agent/flows/`（JSON，不进入 `semantic/`）；每次发布把 Prompt/规则/语义/流程与派生索引打包为不可变制品。索引从 Git 制品重建，键含语义/规则摘要，不作为第二权威源。
- `ReleaseManifest` 固定 `release_id/content_digest/source_git_sha/runtime_code_sha/flow_digest/semantic_digest/rule_digest/prompt_digests/tool_registry_version/model_versions/source_revision/index_digest/eval_evidence_ids`。`release_id` 是内容身份，不是可移动的 latest。
- `Deployment` 是 `(deployment_id, active_release_id, source_id)` 的原子指针，发布以 `expected_active_release_id` 做 CAS；冲突返回 409。回退指针到仍兼容的历史制品也需当前权限、源能力与安全门禁，不回退数据库内容或全局安全策略。
- 查询接受时固定整个 Manifest；活动发布改变不影响已运行实例。同一会话不跨发布混用上下文，切换要求新 session；不得在执行中热改 Prompt、规则、模型或索引。
- 配置制品不携带可执行代码。运行代码/节点注册版本与 Manifest 不兼容时拒绝装配；代码升级先排空运行，再迁移/部署。跨代码版本回退需恢复兼容镜像并验证控制库兼容性，不声称仅切 release 指针就能回退任意程序版本。
- `RuntimeBundle` 从制品显式构造 SemanticModel、规则与 Prompt；改造现有默认路径加载器和全局 locale 缓存，使缓存按内容摘要隔离。保留旧 CLI/测试缺省行为，避免不同发布串用同义词。

### D05 流程合同与运行时

`FlowDefinition`：`schema_version=1/flow_id/revision/entry/nodes/edges/budget`。节点字段：`node_id/node_type/type_version/implementation_id/config_ref/input_contract/output_contract`；配置引用为发布制品内摘要，不是 URL、Python import 路径或 shell。

节点实现是注册代码，不是用户上传代码：

| 节点类型 | 输入 → 输出 | 允许实现 |
|---|---|---|
| `rule_plan` | QuestionContext → PlanCandidate 或 Clarification 或 Unmatched | 规则 |
| `understand` | QuestionContext → SemanticIntent | 规则、LLM、自训练模型 |
| `retrieve` | RetrievalRequest → CandidateSet | BM25；已验证的可选检索后端 |
| `bind_plan` | SemanticIntent + CandidateSet → PlanCandidate 或 Clarification | 确定性绑定，受校验的模型候选 |
| `execute_plan` | PlanCandidate → ExecutionResult | **受保护复合节点** |
| `analysis` | 已注册 AnalysisPlan → AnalysisResult | 固定四步模板与资格校验 |
| `explain` | 已校验结果 → Explanation | 模板或接地 LLM |
| `chart` | 已校验结果 → ChartSpec | 既有确定性图表规则 |
| `clarify` / `handoff` | 原因码 + 候选 → 回合终态 | 确定性 |
| `switch` / `subflow` | 声明合同 → 声明合同 | 注册控制节点 |

默认模板路径：

```text
身份与发布固定（系统外壳）
  → rule_plan ─命中→ execute_plan → explain/chart → 回合终态
       ├明确歧义→ clarify
       └unmatched→ understand → retrieve → bind_plan → execute_plan
                                      └无证据/条件未绑定→ clarify/handoff
分析模板 → 资格校验 → 四个受保护子查询 → 对账/Decimal 综合 → explain/chart
所有真实调用 → 统一事件 → 运行图 / 时间线 / 审计
```

- 外层图为 DAG，允许增删注册节点、修改分支、组合注册子流程。首版分支只允许节点枚举状态的 `equals`/`in`，必须穷尽且互斥并有失败出口，不执行任意表达式。
- 重试是节点配置 `max_attempts`，不是任意回边；子流程不能递归，展开后仍做路径检查。首版共用总预算：模型调用最多 4 次、每节点尝试最多 2 次、每模型节点工具调用最多 3 次、一次运行 SQL 最多 4 次、运行期限 180 秒。规则单查询只消耗实际调用，不人为补满预算。
- 新模型节点单次输入最多 2048 token、输出最多 512 token，整次运行输入与输出累计最多 10240 token；工具输出也计入下次模型输入。超限拒绝并指出具体预算，不静默截断用户条件。旧接口预算不因本条改变。
- 这些是防无限循环的初始上限，不是性能承诺；额度耗尽输出明确原因，不能重置计数继续。
- `NodeImplementation.execute(input, context) -> NodeResult`；context 由服务器创建，含已验证身份、固定发布、数据身份、时钟、预算、事件 sink 和受限工具 broker，不允许节点覆写。
- `NodeResult` 固定 `status/output/reason_code/usage/evidence_refs`，status 为 `ok/clarify/unmatched/blocked/error`；output 按注册的 Pydantic 判别联合校验、extra=forbid。`PlanCandidate` 不等于已授权可执行 Plan。
- 图编译层继续构造 LangGraph，不另起引擎；默认模板先证明旧行为等价，再启用编辑图。analysis 保留父子运行关系和现有失败闭合，不能借图编辑升级成任意因果分析。

### D06 安全必经路径与静态校验

`execute_plan` 内核不可拆线替换：Plan/关系/条件完整性校验 → Compiler → 当前身份与 RowPolicy 解析 → Guard（含 LIMIT/时间/白名单/预算）→ 数据库只读执行 → 执行结果校验。图中可以展开观察内部步骤，不能删掉 Guard 或注入“已校验”布尔值取得执行权。

- 调试、Plan 直执、工具调用、分析子步骤、评测与 MCP 统一委托内核；只有内核持有查询执行器。新流程插件没有裸 SQL 工具。现有 MCP `execute_readonly` 暂不向新 LLM 节点开放。
- 静态校验：注册类型、配置 Schema、唯一 ID、端口兼容、数据依赖在所有路径存在、连线可达、无非法环、预算可界定、执行支配关系、错误/澄清出口、工具 Scope、模型能力与数据等级。未知节点/版本/权限一律拒绝发布。
- 运行时再次校验，不能只信前端校验或发布期结论；所有成功答案须来自受保护结果或明确澄清，禁止自建文本节点伪装查询答案。
- 被拒 SQL、底层异常、密钥、未校验模型输出不出网；新增轨迹不得比既有结果接口泄漏更多。内部持久化也不保存被拒 SQL 原文。
- 当前安全策略收紧可中止旧 release，不能因为固定发布而冻结已撤销权限。查询开始及每次受保护执行前检查授权仍有效。

### D07 运行事实、事件、实时进度与历史

存储：`serving/state/control.sqlite`，标准库 sqlite3、事务、WAL、busy timeout；只部署在本机持久盘，不放网络文件系统。checkpoint 仍使用独立数据库。SQLite 中的写操作是应用控制状态，不是业务源 SQL，绝不能把控制库写连接暴露成问数工具。

核心实体：`drafts/reviews/releases/deployments/sessions/runs/run_events/artifacts/feedback/datasets/jobs/model_versions`；每个记录含对象 ID、owner/scope、版本、创建时间。迁移用编号 SQL、schema_version 和迁移前备份，禁止原地破坏历史记录。

`RunView` 固定返回 `run_id/session_id/release_id/status/result/result_availability/data_identity/replay_of/last_seq/trace_summary`。status 为 queued/running/succeeded/blocked/failed/interrupted；result 仅在终态、内容仍可用且当前读取者有权限时返回既有安全 TurnPayload 投影，否则为 null。result_availability 为 pending/available/not_retained/expired/restricted：未终态为 pending，授权内容可读取为 available，默认未持久化且内存已失为 not_retained，曾捕获但已清理为 expired，对象可见但正文无权读取为 restricted；对象本身不可见仍返回 404。succeeded 表示处理完成，不代表业务答对；具体 answer/clarify/handoff 取自 result.kind，内容不可用时仅在 trace_summary.result_kind 保留枚举，不能伪造答案。`client_request_id` 是长度 1–128 的非空字符串，浏览器默认生成 UUID；禁止前端自行指定 run_id。

事件合同：

```text
RunEvent {
  schema_version: 1, run_id, seq, event_id, occurred_at,
  node_id: string|null, node_run_id: string|null,
  parent_node_run_id: string|null, attempt: integer|null,
  event_type, release_id, payload: SafeEventPayload
}
```

`event_type` 为 `RUN_ACCEPTED/RUN_STARTED/NODE_STARTED/NODE_FINISHED/NODE_FAILED/NODE_SKIPPED/EDGE_TAKEN/TOOL_STARTED/TOOL_FINISHED/FALLBACK/STATE_SNAPSHOT/RUN_FINISHED/RUN_INTERRUPTED`。每次重试有独立 node_run_id；seq 在同一 run 内事务递增且唯一。最终快照与终态同事务写入，只有一条终态。STATE_SNAPSHOT 只持久化脱敏状态、结果种类和 artifact 引用，不内嵌问句、SQL、结果行或 Prompt；事件续读只能恢复仍获授权且未过期的内容。

- 图与时间线由相同事件归约；定义图上未到达的节点显示“未执行”，只有确定的分支裁决才能产生 skipped，不编造开始/结束时间。内部检索、模型和工具子调用同样留事件，消除“图一查、生成器又查却不显示”的差异。
- 创建运行先持久化 queued，后台单业务队列执行；API 不在 async 事件循环内阻塞执行。订阅在计算完成前就能收到真实事件；事件先提交再通知，SSE 断线仅断开订阅，不重做查询。
- SSE 按 `Last-Event-ID`/`after_seq` 续读，客户端按 seq 去重。持久事件是权威，内存通知可丢。无法补齐已过保留期事件返回 410；未知事件不崩溃且不得据此伪造完成。
- 启动恢复：上次进程的 queued/running 均标 interrupted，因为旧登录态与授权可能已失效；**不自动重跑 SQL**。显式重新运行生成新 run_id、当前授权，关联 `replay_of`；旧记录不覆写。
- `client_request_id` 在同所有者/部署内唯一；相同请求重试返回原 run，内容不同返回 409；并发创建也不能执行两次。
- 控制状态/强制审计写失败时 fail closed，下一次 SQL 不得启动；已有查询结果无法完成审计则不按成功交付。OTel 导出仍可失败且不影响查询，不能把可选观测与必需运行事实混为一谈。
- 默认只持久化脱敏摘要、输入/输出摘要哈希与版本；哈希不是匿名化证明。为交付当前答案，问句、结果和会话上下文可临时存在进程内存，终态后最多 15 分钟（会话按最近回合计时），退出/权限撤销提前清除，进程重启不恢复。GET RunView 可在此窗口按内容 ACL 返回 result；SSE 只通知状态与引用，前端另取结果，不持久化浏览器内容。
- 人工授权的调试捕获才把允许原文保存为私有 artifact，原始 CoT 永不采集；敏感 payload 不直接塞持久事件/OTel。新 `/runs` 默认使用内存 checkpoint，只有显式内容保留授权才允许持久 checkpoint，保留期不得超过关联 artifact；历史读取禁止回退到旧 checkpoint 找正文。上下文已过期的续问返回 409 `session_context_expired`，要求新 session，不悄悄丢弃上下文执行。
- 私有目录权限为 0700、内容文件为 0600，备份同级保护；磁盘加密由部署方配置，不宣称应用 ACL 等于静态加密。未捕获或已清理正文的历史只提供摘要，artifact 读取为 410；无内容权限为 403/404。未保留原文的运行不支持原样节点回放，需用户重新提供输入并标记为新样本。
- 首版保留策略：运行摘要/事件 30 天，显式捕获的原文/结果 7 天，安全审计 90 天；训练批准样本按数据集生命周期保留并记录授权。都是部署默认值，不是法规结论。超过期限删除正文并保留删除标记；撤销训练授权使相关数据集/模型不可再发布，重新训练或停用，不声称已从现有权重中“删除记忆”。

### D08 节点配置、Tools 与实验

节点侧栏统一“配置 / 调试 / 样本 / 评测 / 版本 / 发布”，但仅展示该节点支持的能力。Compiler/Guard 可看参数和诊断，不允许 Prompt 化或训练替换。

- 规则：编辑已有声明式同义词、模式顺序、优先级和允许阈值；正则保存前做结构检查，发布测试在受时限的隔离子进程覆盖反例，查询输入限制长度。新增算法走代码版本，浏览器不执行 Python。
- LLM：选择服务端注册的模型 profile、Prompt 版本、上下文来源和工具集合，输出 Schema 由节点合同固定；不能自由填 URL、密钥或删除 Schema。数据级别由实际内容的最高级别传播，问句/筛选值/历史也可能敏感，**不再把“无结果数值”自动等同于可出云**。
- `result-bearing` 与未明确批准出境的业务问句仅自托管；模型/工具任一步产生更高敏感数据都向后传播，不能由 Prompt 降级标签。云服务必须管理员按领域明确许可并通过字段级最小化；不可用就澄清/模板回退，不静默升级云。
- 首版 Tools 走受约束 JSON action 循环，保留 `chat/completions` 可移植字段，不默认要求原生 function calling。响应判别：`{"action":"tool","name":...,"arguments":...}` 或 `{"action":"final","output":...}`，extra=forbid；broker 校验注册名、输入 Schema、Scope、预算、数据等级，再真实调用并记录事件。
- `understand` 仅可使用语义目录摘要、已授权语义描述工具，**不能检索候选或执行 SQL**，避免悄悄把预检索试点变成重排。`bind_plan` 可 describe_metric，执行仍只能走保护节点。工具描述/返回视作不可信内容，不得修改系统策略。
- 原生 function calling、任意 MCP 工具、任意 Python 工具不纳入首版；若新增后端能力须独立适配测试，不能只在 UI 勾选即声称支持。
- 实验固定输入快照、release 与其他节点，仅替换一个配置；单节点回放不默认执行数据库。需要下游结果的端到端对照只在批准快照或明确授权的在线 smoke 进行，另记消耗。
- 调试样本不自动成为训练集；一次修改必须重跑节点样例和端到端门禁，保存草稿不改变在线行为。

### D09 检索前语义意图与条件贯通

新增 `SemanticIntent` 与完整 Plan 分开；模型只描述业务意图，不创造 Metric 名或物理列。所有对象 `schema_version=1`、extra=forbid。

```text
QuestionContext {question, locale, authorized_context, catalog_digest,
                 catalog_summary, reference_time, timezone, capabilities}
SemanticIntent {
  task: query|compare|attribution|clarify|unsupported,
  metric_mentions: Mention[], groups: Mention[],
  filters: IntentFilter[], time_mentions: Mention[],
  sort: {direction: asc|desc, by: Mention}|null, limit: integer|null,
  ambiguities: {slot, reason_code, evidence: EvidenceSpan[]}[],
  retrieval_queries: {text, evidence: EvidenceSpan[]}[]
}
Mention {text, evidence: EvidenceSpan[]}
EvidenceSpan {source_id, start, end}  # 原文字符半开区间，必须可校验
IntentFilter {subject: Mention, op: eq|neq|in|not_in|gt|gte|lt|lte, values: Mention[]}
CandidateSet {catalog_digest, candidates: [{semantic_id, rank, sources, evidence_refs}]}
BindingResult {plan_candidate: Plan|null, slot_bindings, unresolved_slots, reason_code}
```

- 来源只能是原问题或已授权上下文；引用不存在、越界、否定丢失、添加条件都拒绝。EvidenceSpan 证明“引用存在”，不证明“理解正确”，后者仍需标注评测。
- 时间数值换算、阈值量纲、排序与 limit 规范化由确定性代码完成。首版绑定范围为已支持的单 Metric、Dimension、筛选、时间比较及固定贡献模板；其他任务澄清，不静默降为普通查询。
- 检索预算：原句必保留，可增加最多 2 个检索表达；每表达召回最多 5 个，合并去重后最终 K=5。评测对照固定总调用、候选和 token 预算，另做原句单路消融，不把多花预算伪装成模型收益。
- 权限、注册目录是硬过滤；语言推断的 Dimension 在语义绑定确认前只作软信号。检索目标是整个**授权且已注册**目录，不能局限于旧模型先前召回的 top-K；最终关系可达性仍是硬检查。
- `retrieve` 输出成为 `bind_plan` 唯一候选输入，移除该路径 Generator 内隐式二次检索；旧 Generator 适配器仍可用于旧入口对照，并如实记录其全量清单上下文，不混称公平 top-K 对照。
- `slot_bindings` 将每个意图槽位映射到 Plan 字段；有未绑定条件只能澄清。Plan 保留现有 filters、comparison、order_by、limit，不走旧 `filters=()` 简化合同。
- 规则明确命中继续原路径；unmatched 才进新节点；规则已判真实歧义必须澄清。对规则覆盖样本可影子诊断，不自动覆盖其答案。

### D10 反馈、真值与数据集

- 反馈类型包含 positive/negative/correction/clarification_choice；关联 `run_id/node_run_id/release_id`。不允许客户端伪造其他人的运行归属。M1 在 T05 先交付提交/本人列表/运行关联，状态固定 pending_review、training_eligible=false；T12 才开放审核、归因与数据集，避免首次接入依赖训练闭环。无原文保留授权时只保存反馈种类/原因码/摘要，不能靠提交反馈把已过期正文自动恢复或长期保存。
- 状态 `pending_review → approved/rejected`，审核必须记录 reviewer、时间、证据、`failure_category` 与修正合同；修改标签使衍生数据集失效。正反馈也需审核，不能“点赞即真值”。
- 归因枚举：`source_data/semantic_definition/synonym_value/language_understanding/retrieval/binding/narrative/authorization/infrastructure`。同因聚类可自动建议，合并与修复由人工确认。
- 数据错误修数据、语义错误改 Git、规则/Prompt 错误走配置修复，只有适合节点学习的审核样本进入训练。反馈中的原始 SQL 不作为可执行训练真值。
- 数据集为不可变 manifest：来源、用途授权、脱敏状态、sample IDs/摘要、标签合同、split、source_family、评测保留标记、审核版本。用户数据默认不写入仓库 `eval/failures/`；在线内容落私有状态目录，导出需审核。
- 训练/验证/测试按来源族、会话、改写模板及语义组合隔离；相同最小对比样本对不得跨 split。Gold set 和冻结测试始终保留，不把评测失败自动并入训练；新的人工学习样本进入下一数据集版本，评测保留集不被反复调参消耗。
- 学习问题表达与条件，不能让模型背当前 Metric 清单。规则教师可用于已覆盖结构的冷启动，不能作为泛化收益的独立真值。

### D11 小模型、SFT、影子发布与可选 RL

首个 adapter 名 `intent_v1`，挂 `understand` 节点，不覆盖历史 `sql_v1`、不优先接重排。首个基座沿用 ADR-0008 的 `Qwen/Qwen2.5-7B-Instruct`，QLoRA NF4；中文表达和资源适配必须实测。训练环境与 API 隔离，不把 GPU 包装进基础镜像。不继承“3B 必为相同许可证”的假设，换基座另做许可及预算校验。

训练配方初始值：LoRA rank=16、alpha=32、dropout=0.05，max_length=2048、micro_batch=1、gradient_accumulation=16、learning_rate=0.0002、epochs=1、seed=42，assistant 输出部分计算 loss。以上仅为可执行起点；超长样本拒绝或显式分片，不静默截掉条件。精确基座 revision、依赖 lock、设备和显存实测写训练 manifest。

- `TrainingJob`：dataset_version、recipe_digest、base_model_revision、代码版本、预算批准、输出合同、状态与 artifact_digest；状态为 queued/running/succeeded/failed/blocked/cancelled。无合法数据、CUDA、许可或预算批准时 blocked，不能生成成功模型版本。
- 训练调用只读取冻结数据集，不拿线上数据库凭据；取消必须终止工作进程并记录产物未发布。服务不因重启重复训练，费用不因重试被自动放大。
- `ModelVersion`：基座、adapter/权重摘要、tokenizer/运行镜像版本、节点合同、训练数据与配方、评测证据、许可证、状态。用户可以选择兼容的批准版本，但不能任意改后端越过出境限制。
- 先离线对照，再抽样影子；影子默认关闭，开启需明确资源限额。独立 ShadowPolicy 固定 candidate_model_id、sample_rule、budget 与批准记录，通过受授权控制接口 CAS 更新；每次运行固定该策略版本，不改变主 release。影子仅运行理解/检索/绑定，不自动重复在线 SQL；需要 EX 的比较走固定快照。影子输出不能改变用户答案、会话或训练标签；影子运行单独关联父 run，遵守相同数据保留与出境约束。
- 达标后人工切换发布包，保留旧模型/规则 fallback。上线发现新增安全违规立即停用，质量回归人工回退；“回退成功”必须有实际运行证据。模型变化不变更 Compiler/Guard。

Nimble 参考边界：[项目](https://github.com/bespokelabsai/nimble)、[训练说明](https://github.com/bespokelabsai/nimble/blob/main/docs/NIMBLE_TRAINING.md)、[模型卡](https://huggingface.co/bespokelabs/Bespoke-Nimble-9B)。已核验方法为有限 enum/boolean 的 LoRA 监督训练与候选 logits 交叉熵，**不是 RL**。仅借鉴最小对比样本、来源族切分、不强猜；不复制许可尚未完整核清的源码，不移植其榜单数字，不把 9B/其工具环境视为 Atlas 已适配。

RL 为独立受门禁任务，不是首版前置：

1. 先冻结 SFT、规则、现有 LLM 对照和目标错误分片；只有存在审核后可学习的残留错误、奖励可独立复核且预算获批才启动。
2. 采用隔离 TRL GRPO，初始每 prompt 4 个 completion、max_completion_length=512、微批次 1、梯度累积 16、learning_rate=0.000005、seed=42、最多 1 epoch；资源预检失败即 blocked，不擅自增加 GPU。
3. 奖励先做可审计二值版：可回答样本只有“合同合法 + 所有真值槽位保持 + 无新增条件 + 固定预算内目标召回 + 下游 Plan/固定快照 EX 正确”才得 1，否则 0；应澄清样本只有澄清动作与标注缺失/歧义槽位一致才得 1。一律澄清在可回答样本得 0。
4. 安全违规候选始终不执行并判训练无效样本事件，不允许用其他奖励抵消；样本组必须保留失败计数，不能丢弃失败只报告成功子集。
5. 独立奖励测试包括“全目录检索”“格式正确但错条件”“碰巧同结果”“删除否定”“一律拒答”。不以模型自评为唯一奖励；无可验证参考的样本不进 RL。
6. 比较时同数据划分、同推理预算、同下游，另报 RL 额外训练成本。没有可重复增益就不发布 RL，保留 SFT；稀疏奖励学不动也是有效实验结论，不改写为成功。

### D12 运维与生命周期

- `/health` 继续存活语义；新增受授权诊断展示源连接、语义/流程版本、发布完整性、状态盘/审计可写、可选模型可达、队列与预算，不能把 LLM 未配置判整个默认规则服务不健康。
- 启动检查环境引用、制品摘要、Schema、持久路径权限、数据库迁移版本；错误指向配置对象与恢复动作，不输出秘密。源不可用时治理/历史/诊断仍可用。
- 只在暂停新发布与新任务、排空执行队列后做一致性备份：控制库、checkpoint、发布制品与私有 artifact 一起记录 manifest；用 SQLite backup API，不直接复制活跃 WAL 文件。备份不含环境密钥，密钥由部署方单独保护。
- 恢复先隔离验证制品/DB 摘要与权限，再恢复服务；不自动恢复未完成 SQL、训练或旧登录态。运维界面明确展示 backup/restore 的人工操作记录。
- 制品清理不得删除仍被活动 release/运行保留期/审计关联的数据；安全删除/授权撤销优先于重放便利。历史摘要继续标记证据已不可用。checkpoint、artifact、内存缓存与备份执行同一内容截止期；恢复时先应用删除/撤销标记及当前权限，不从备份复活过期正文。

### D13 服务面与兼容迁移

新 API 使用 `/api/v1`，但不改旧请求/响应字段含义；端点常量、OpenAPI 与测试清单按集合比对，不硬写旧路径数量。下表是目标接口，不可在实现前对外宣称存在。

| 方法与路径 | 合同与权限 |
|---|---|
| `GET /auth/login`、`GET /auth/callback`、`POST /auth/logout` | OIDC/BFF；回调严格验证；logout/写操作 CSRF |
| `GET /auth/session` | 当前身份与能力、CSRF token；不返回 IdP token |
| `POST /runs` | `deployment_id/mode/question或plan/session_id?/client_request_id/capture?` → 202 `{run_id,status,release_id}`；mode=ask/analyze/execute_plan；字段组合严格校验 |
| `GET /runs`、`GET /runs/{run_id}` | 游标分页、按域/时间/状态/节点原因码搜索；服务端 ACL，不暴露未授权对象是否存在 |
| `GET /runs/{run_id}/artifacts/{artifact_id}` | 输入/输出详情仅按内容 ACL 展示；清理后返回 410，不从 checkpoint 绕过保留期 |
| `GET /runs/{run_id}/events` | SSE，从持久事实续读；当前身份重新授权，过期会话关闭连接 |
| `POST /runs/{run_id}/replays` | 显式创建新运行，不是 GET 的副作用；默认仅节点离线回放；live_full 需确认当前数据变化 |
| `GET /sessions`、`GET /sessions/{session_id}` | 有权限的历史目录与回合，非直接 dump checkpoint |
| `POST /feedback`、`GET /feedback` | 提交与本人反馈列表；返回 status/training_eligible，M1 尚未审核时固定 pending_review/false |
| `GET /manage/feedback`、`POST /manage/feedback/{id}/reviews` | 审核队列与显式审核，按领域和内容授权裁剪，拒绝越权关联 |
| `GET/POST /manage/sources`、`POST /manage/sources/{id}/probes` | 配置引用与受限探测；只读业务源 |
| `GET/POST /manage/drafts`、`GET/PUT /manage/drafts/{id}` | kind=semantic/flow/node_config，If-Match 草稿版本并发控制 |
| `POST /manage/drafts/{id}/validations`、`POST /manage/drafts/{id}/reviews` | 校验与人工审核，均绑定草稿摘要 |
| `GET /manage/drafts/{id}/patch`、`POST /manage/releases/imports` | Git patch 与已合入制品导入；不可执行上传内容 |
| `GET/POST /manage/deployments`、`GET /manage/deployments/{id}` | 创建/查看部署绑定；新部署在首次批准发布前 active_release_id=null |
| `POST /manage/deployments/{id}/releases`、`POST /manage/deployments/{id}/rollbacks` | expected_active_release_id CAS、证据门禁与当前安全检查 |
| `GET/PUT /manage/deployments/{id}/shadow-policy` | T15；If-Match 策略版本、批准模型和预算；shadow.manage，不改变 active_release_id |
| `GET /manage/node-types`、`GET /manage/releases`、`GET /manage/releases/{id}` | 合同/兼容实现、发布历史与脱敏详情 |
| `POST /manage/experiments`、`GET /manage/experiments/{id}` | 固定样本、release、单节点替换，异步 job |
| `GET/POST /manage/datasets`、`POST /manage/datasets/{id}/approvals` | 数据集版本创建与批准分开 |
| `GET/POST /manage/training-jobs`、`GET /manage/training-jobs/{id}`、`POST /manage/training-jobs/{id}/cancel` | 显式训练、列表/状态、取消；没有自动训练计划 |
| `GET /manage/models`、`POST /manage/models/{id}/approvals` | 模型证据与人工批准，不直接改 active |
| `GET /manage/diagnostics` | 管理面摘要，不返回 DSN、密钥或原始 Prompt |

`capture` 省略或为 null 表示不持久化正文；显式值为 `{purpose, fields, retain_until}`，fields 仅允许 question/node_io/result，需当前数据权限与对应内容保留授权，retain_until 不得超过 D07 的捕获保留期。表单说明用途后由用户确认，服务校验并审计授权，不把请求中填了字段当成权限授予。

所有表内路径加 `/api/v1` 前缀。新控制 API 统一 `{"error":{"code","message","request_id"}}`，不含底层异常；401 无认证、403 能力不足、404 对象不可见、409 版本/状态冲突、422 合同不合法、429 限流、503 依赖不可用。GET 分页默认 50、最多 100；写入用服务端幂等键与大小限制，查询/管理/训练限额分开。后台任务立即返回 job ID，不阻塞 HTTP 等训练结束。

旧 `/ask`、`/plan/execute`、`/analyze` 继续快照默认入口，内部逐步接入同一执行内核并可加 `X-Atlas-Run-Id` 响应头；旧 `/analyze/stream` 保留 compute-then-stream 行为与事件形态。新前端运行工作台改用 `/runs` 真事件，旧流不冒充实时。旧 `llm=off` 仍零模型调用；旧默认结果合同及 CLI exit code 不变。候选路径先在新流程真实接通并测量，不悄悄改变旧旗标语义。

完整 MCP transport 延后到 T17：以官方 Python SDK 的 stdio 传输作为首个增量，进程启动绑定固定最小身份，再代理同一 HTTP/内核；不允许客户端工具参数改身份、不暴露裸 SQL 执行。远程 MCP/OAuth 另行设计，现有本地封装不标兼容。

### D14 评测、发布门禁与 SOTA 口径

不替换 Plan Acc、EX，新增分片和运营指标。所有报告绑定代码、release、数据集、快照、Prompt/模型、预算；缺依赖写 blocked/not_run 与原因，不计通过、不填零冒充实测。

- 回归：既有 finance/retail 注册场景、澄清、时间比较、筛选、RowPolicy、Guard、分析资格/对账、LLM 出境、接地叙述、CLI/HTTP 合同。
- 泛化：陌生措辞、最小否定对、组合条件、时间/指代、目录变更、真实歧义、未注册指标；按中文/英文、来源族与任务类型分报。
- 节点：槽位保持率、条件遗漏/新增率、固定预算 Recall@K、正确澄清/错误强答；下游按同样本 Plan Acc 和 EX 对照，不能只看检索排名。
- 选择性回答：自动作答覆盖率=自动答案数/全部合法请求数；错误自动作答率=错误自动答案数/自动答案数；分母为零记 null。合理澄清单独列分母；回答变少不能伪装整体变好。
- 运营：接入首次可信答案耗时、人工配置/审核动作数、失败定位到修复发布耗时；由验收脚本时间戳和人工动作事件计算，使用非项目作者执行记录，不靠作者主观估时。
- 资源：P50/P95、输入/输出 token、工具/SQL 调用、显存峰值、训练时长、模型存储；货币成本仅在有提供方账单或带版本单价表时计算，不把 token_cost 当货币。
- 模型发布门禁：安全与出境用例无新增违规；既有冻结回归无新增错误；目标错误分片严格减少，条件遗漏/新增不得恶化；同时展示覆盖率与错误自动作答率，不能靠拒答扩张取得通过。预先冻结评测，重复实验报告样本量及不确定区间，不宣称统计显著而无计算。
- SFT/RL 对照必须分别显示新增预算；基线同输入/目录/权限/下游。未满足门禁保留旧版，不因实验完成而强制替换。
- Spider/BIRD 仅历史参照，不重新新增榜单接入、不与企业场景混报。“SOTA”只在明确任务/公开对照/同预算脚本证据齐备后讨论，本 ADR 不承诺名次或效果数字。

## 4. 与既有 ADR 的兼容关系

| 既有决策 | 本 ADR 的处理 |
|---|---|
| [0002](0002-ossie-as-semantic-spec.md)、[0007](0007-fibo-semantic-alignment.md)、[0015](0015-scenario-decoupling-locale.md)、[0016](0016-dimension-value-domain.md) | 保留 Ossie/FIBO/Git 权威；草稿非权威；不新增 semantic YAML 权威目录 |
| [0003](0003-readonly-sql-gateway.md)、[0011](0011-security-layering.md)、[0021](0021-row-policy-two-dimensional-source.md) | 不放松 Guard/身份/二维策略；新入口与 replay 同源 |
| [0004](0004-apache-stack.md)、[0009](0009-agent-framework.md) | 保留数据平台与 LangGraph；仅拆可选部署，不更换湖仓选型 |
| [0008](0008-lora-base-model.md) | 历史 sql_v1 保留；新增 intent_v1 独立合同与数据集，不冒充旧 Plan adapter |
| [0010](0010-eval-methodology.md)、[0014](0014-phase2a-capability-ruling.md) | 保留 EX/Plan Acc 与公开集分界，运营/节点指标加性扩展 |
| [0012](0012-serving-http-api.md)、[0022](0022-http-contract-v2.md) | 显式新增控制/运行 API 与授权；旧路径语义不变，更新路径集合测试 |
| [0018](0018-frontend-console.md)、[0028](0028-frontend-oss-borrowing.md) | 解锁历史列表与非权威编辑；增加受管 Cookie 狭义例外；新 /runs 真流不改旧 SSE 契约；不声称 AG-UI 兼容 |
| [0019](0019-runtime-snapshot-resolution.md) | 新 /runs 增 live 数据身份；旧固定快照入口及评测不变 |
| [0020](0020-session-persistence-sqlite-checkpoint.md) | checkpoint 与控制数据库分工；旧身份指纹不放松；会话新增长期索引但不靠浏览器存储 |
| [0026](0026-multi-step-task-planning.md) | 四步分析注册为受保护子流程，不扩张成任意分析；live 一致性不足则拒绝 |
| [0027](0027-feedback-flywheel.md) | 保留人工闸口、proposal、禁止自动写 Git/索引；在线私有样本另存私有状态目录 |
| [0029](0029-llm-engine-serving.md) | 保留服务端选后端/结果不得出云；加严问句敏感度；以 JSON action 实现 Tools，不改变可移植字段；不将旧门控当候选链 |
| [0030](0030-jev-system-one-engine.md) | 保留 Jev 协议/重排接缝；**本产品首个自训练试点改为理解与检索规划**，不删除既有实现、不把 Jev 当新试点前置 |

既有 0029/0030 等文档中的“未启动”与当前代码存在状态漂移。此处用 E 表区分代码存在、默认接线和真实模型效果；不在本次顺手改写历史记录。后续每任务只追加带证据的落地注记，不把整份历史 ADR 无差别标成完成。

## 5. 依赖、许可与预算

本次只记录选择，不安装依赖。新增依赖在对应任务中必须锁定精确版本、验证传递依赖并运行许可守卫；任何许可失败阻断任务，不能把本表当供应链检查结果。

| 依赖 | 用途/范围 | 已核验依据与待执行门禁 | 预算 |
|---|---|---|---|
| `@xyflow/react` 12.x | 流程画布；保留版权信息，不用付费 Pro | [上游 LICENSE](https://github.com/xyflow/xyflow/blob/main/LICENSE) 已读取 MIT；安装后核验具体包/依赖、React 18/TS/Vite 构建及键盘操作 | 社区许可，无新增服务；资源待构建测量 |
| Authlib 1.x、HTTPX | OIDC BFF；HTTPX 从现有 dev 使用提升到运行依赖 | [Authlib LICENSE](https://github.com/lepture/authlib/blob/main/LICENSE) 已读取 BSD-3-Clause；核验实际锁版本及其加密依赖 | 复用已有 IdP，无新托管身份服务采购 |
| TRL | 仅隔离 RL extra/训练镜像，T16 门禁后加入 | [上游 LICENSE](https://github.com/huggingface/trl/blob/main/LICENSE) 已读取 Apache-2.0 开头；完整包许可与 vLLM/torch/transformers 版本联测仍须执行 | GPU 小时/费用上限需部署者显式批准；无批准 blocked |
| 官方 MCP Python SDK | 仅 T17 的 stdio transport | 本次未安装/未完成该 SDK 版本许可核验，T17 先完成再接入 | 不建远程 MCP 集群 |
| Qwen2.5-7B + 现有 QLoRA 栈 | intent_v1，沿用 0008 | 训练前重新固定模型 revision/模型卡/许可/完整性摘要；不以 Nimble 许可代替基座许可 | 单卡 24G 为约束，不是已验证适配；显存/时长预检超限即停 |

控制库 sqlite3、事件队列和图静态分析复用标准库、Pydantic、NetworkX、LangGraph；不新增 ORM、消息中间件、分布式工作流引擎或模型管理平台。

## 6. 理由与代价

1. **先把配置和运行事实分开**，才能回答某次问数到底用了哪版规则，而不是只画当前最新版流程。
2. **保护复合执行节点而不是依赖画布约定**，使拖拽、调试、工具、重放都不能绕过可信内核。
3. **Git 落源保留人工步骤**，换取唯一权威与审计；首版 UI 不是“编辑即上线”，管理员仍需要一次导出/合入/导入。
4. **单进程 SQLite 和单执行队列降低运维面**，代价是吞吐与高可用上限；本 ADR 不提供跨进程锁/分布式限流保证。
5. **检索前模型能影响原先未命中的检索输入**，但不能修复未注册 Metric、错误业务定义或缺数据；也不能凭结构合法证明语义正确。
6. **新身份与管理面扩大了权限设计范围**，必须先实现 ACL/CSRF/审计，不能先做可写画布再补权限。
7. **在线数据可变与隐私保留期限制重放**，历史可见不等于可再次得到同值；已经删除的原文不伪造恢复。
8. **用户没有授权任意付费调用或训练**；训练及影子开关、预算上限需要部署者操作确认。RL 无增益也是可接受终态。

## 7. 什么情况下应该推翻

- 单进程吞吐/恢复时间经 profile 无法满足已记录 SLO → 单独设计 PostgreSQL 控制库、分布式队列/锁和限流迁移，不局部开启多 worker。
- 第二种数据库真实试点要求明确 → 单独补连接器、方言、只读/权限/一致性测试，不开放 generic SQL URL。
- 人工 Git 往返成为已测瓶颈 → 可另议“服务创建分支/PR、人工合并”，不默认自动改 active/merge。
- 图静态合同无法表达已确认且安全的业务子流程 → 补节点类型与验证证明；不开放任意脚本回避合同。
- 同预算理解节点没有减少目标漏召回/条件错误 → 不发布该 adapter；检查目录/绑定问题，必要时重审试点，不包装重排收益冒充理解收益。
- SFT 满足需求且 RL 不能覆盖额外成本，或奖励不能独立验真 → 关闭 T16，保留 SFT/规则。
- 任何新增入口绕过 Guard、出境分级、人工真值或发布门禁 → 暂停相关功能，恢复最后安全发布；禁止用更高 EX 抵消安全缺陷。

## 8. 验证方式与里程碑

完整用例、运行命令、文件责任及依赖见 [开发任务表](../../docs/design/dev-plan-0031-trusted-query-workbench.md)。里程碑按可验收能力划分，不写未经估算的工期：

| 里程碑 | 必需任务 | 业务验收 |
|---|---|---|
| M0 可追踪基线 | T01–T06 | 同一问数的身份、发布、节点、工具、重试与终态可查；重启不伪造继续执行 |
| M1 首个可信接入闭环 | M0 + T07、T08、T10、T13 | 非作者接入 Doris、审核发布语义、默认流程问数、结构化澄清、提交并找到反馈；失败可定位 |
| M2 可视化配置与优化闭环 | M1 + T09、T11、T12 | 从失败节点修改规则/Prompt→同样本比较→安全发布→回退；可以编辑允许的图，不只是改参数 |
| M3 自训练模型替换 | M2 + T14、T15 | intent_v1 真机训练/评测、影子和人工切换完成；无资源就停在 blocked，不影响 M2 |
| M4 条件性扩展 | M3 + T16；T17 可独立受门禁开展 | RL 相对 SFT 有可复现实证才发布；MCP transport 通过身份/执行同源测试才宣称兼容 |

最终端到端验收故事：**新团队接入数据 → 发布语义/默认流程 → 问数失败 → 运行图定位 → 分类修复规则/Prompt 或审核样本 → 回归比较 → 发布新版本 → 旧运行仍可解释 → 新运行验证 → 可回退。**

所有实现任务开始前先补测试/评测；PR 报告含评测、性能、安全与破坏性变更摘要。`make lint && make test && make eval` 是项目门禁，但 `make test` 使用 unittest，不覆盖纯 pytest 函数，因此还需 `.venv/bin/python -m pytest tests`；前端变更跑 `make ui-check`。真实快照/数据库缺失时记录 blocked，绝不重置数据凑通过。

## 9. Known Limitations（本 ADR 交付时）

- 原文档交付时未实现流程编辑、控制库、连接向导、OIDC 登录、节点实验、实时事件或新训练路径；现仅授权启动 M0，不代表这些能力已完成。
- 默认历史保留摘要，不保证重启后仍有结果正文；完整历史详情与原样节点回放需要显式捕获授权，且受保留期限制。
- 当前运行质量只有既有历史证据，本次没有测出新的 EX、Recall、延迟、资源或 SOTA 结论。
- Git 导入发布仍需要管理员；固定图中的安全步骤只能观察/配置受允许参数，不能任意替换。
- 首版只接 Doris、单团队单进程；在线四步一致性不具备时拒绝分析，不提供跨源计算。
- 数字接地不等于因果正确，意图证据跨度不等于理解正确，模型置信度不等于校准后的正确率。
- 新依赖的具体锁版本、IdP 真链和 GPU 预算门禁尚未执行；Nimble 全仓源码许可未完成核验。
- M0 开工授权不替代依赖许可、真实 IdP/数据源验收与后续发布批准；实施状态只按任务证据更新，不批量勾选完成。
