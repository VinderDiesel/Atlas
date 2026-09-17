# ADR-0022：HTTP 契约 v2——`/api/v1` 前缀、`/plan/execute`、8 个只读治理端点与限流两桶

- 日期：2026-09-14
- 状态：accepted（决策经用户确认；落地批次 **P-2api**，须在 P-2sec（ADR-0021）之后、
  P0b（ADR-0018 工程边界）之前）
- 相关：
  - ADR-0012（HTTP 服务面 v1，本 ADR 是其契约演进；正文不改，末尾追加落地注记）
  - ADR-0018（前端控制台：本 ADR 提供其治理面板数据源与 SPA fallback 的前缀前提）
  - ADR-0019（`/health` 快照字段）、ADR-0020（`/health` 的 `boot_id`、会话身份指纹）
  - ADR-0021（`roles_for_policy` + `RoleSpec` 是决策 ⑤ 端点 6 的数据源；批次耦合见其代价 ⑦）
  - ADR-0011（安全分层：治理端点**只读**，不新增执行面；决策 5「被拒路径不外泄细节」不变）
  - 代码事实源：`serving/api.py:285-432`、`serving/ratelimit.py:20-33`、
    `serving/audit.py:34-43`、`agent/graph.py:323-376`（execute 唯一通道）、
    `semantic/governance_validate.py:71-89`（`iter_payloads`）

---

## 背景

### 现有 4 个端点全是业务面，治理面为 0

实测（2026-09-14）`serving/api.py` 全文 432 行，只注册 4 条路由：

```
serving/api.py:337   @app.get("/health")     公开
serving/api.py:347   @app.post("/plan")      Bearer
serving/api.py:363   @app.post("/compile")   Bearer
serving/api.py:382   @app.post("/ask")       Bearer
```

无 `APIRouter`、无版本前缀、无 `tags`；`FastAPI(version="0.1.0")`（`:287`）与
`pyproject.toml:9` 的 `0.1.4` 不同步（附带发现，决策 ⑦ 处理）。

治理面板要展示的数据**全部只存在于文件**，无任何结构化读出口。实测计数：

| 数据 | 位置 | 实测规模（2026-09-14） |
|---|---|---|
| 语义模型 | `semantic/ossie/*.ossie.yaml` | 2 份：金融 `atlas_finance_analytics` 8 datasets / 20 metrics / 12 relationships / 49 fields；零售 `atlas_retail_analytics` 4 / 5 / 3 / 20 |
| 治理扩展 | 同上 `custom_extensions`（vendor=ATLAS） | 金融 21 个 payload（1 模型级 + 20 指标级）；schema 顶层 7 节：`governance` / `lineage` / `freshness` / `policy` / `quality` / `fibo_alignment` / `time_dimension` |
| locale 词典 | `semantic/synonyms/*.yml` | 4 份：`en_us` 17 指标 + 15 维度同义词；`zh_cn` **0 / 0（空占位）**；`patterns_en_us` 10 节、`patterns_zh_cn` 8 节 |
| 值域注册表 | `semantic/values/*.json` | 20 份 = 9 `registered` + **11 `skipped`（`values: []`）** |
| 行级策略 | `semantic/policies/row_policy.yml` | 2 策略 / 6 角色条目（`hq_admin` 双域各声明一次）；`broker` 在 `:39-40` 有权威条件但 `ROLE_DIRECTORY` 未注册 |
| 评测报告 | `eval/reports/*.json` | 48 份 = 12 主报告（固定 7 键 `sha/created_at/dry/domains/summary/samples/notes`）+ 36 份分属 **18 种文件名模式** |
| 快照 | `data/snapshots/*.meta.json` | 17 份，9 键完全统一 |
| 黄金集 | `eval/gold/{finance,retail,paraphrase}` | 79 + 27 = **106**（主评测口径）+ 13 复述集 |

前端自行读文件 = 变成第二套事实源（与 ADR-0021 理由 5 同一论证）；不开端点，
治理面板只能硬编码，ADR-0018 决策 ⑦ 锁定的信息优先级无从落地。

### 无前缀的根路径与 SPA 同源部署直接冲突

ADR-0018 决策 ② 要求 prod 用 `StaticFiles` 挂 `frontend/dist` 与 API 同源，且
SPA catch-all fallback **必须注册在 API 路由之后**（否则接口 404 变 HTML 200，
前端 `JSON.parse` 静默失败）。当前 4 条路由全在根路径，与前端路由
（`/governance/metrics`、`/workbench`）同层，于是：

- 无法用前缀一刀切分开「API 命名空间」与「SPA 命名空间」；
- fallback 只能靠逐条排除已知 API 路径实现——**每加一个端点都要改 fallback 白名单**，
  漏一个即静默失败，且该失败在 CI 不暴露（无浏览器断言）。

### 限流是单桶，治理面板一次加载即撞穿

`serving/ratelimit.py:20-24`：`ATLAS_RATE_LIMIT_MAX` 默认 **60**、窗口 **60s**
（注释自承「配置占位非实测阈值」）；`api.py:303-322` 的 `_require_rate_limit` 让
`/plan /compile /ask` **共享一个桶**（key = `sub or role or "anonymous"`，`:309`）。

治理面板挂载即 8 个请求，加钻取与页面切换，**7~8 次导航就打满一分钟配额**；更糟
的是业务面与治理面互相挤占——一次面板浏览会让紧随其后的 `/ask` 直接 429。两类
负载成本结构不同：业务面走 Doris 真实执行，治理面只读文件（解析成本实测见决策 ④）。
同一阈值必然对一类过松、对另一类过严。

### Plan 只能看不能改：`/compile` 到此为止

`/compile`（`:363-380`）返回 `{sql}` 就结束，没有执行面；`/ask` 只接受自然语言。
故「前端展示 Plan → 用户改一个维度或时间 → 重跑」这条最基本的交互**无端点可走**。
`agent/graph.py:323-376` 的 `node_execute` 是「编译→Guard→执行 的唯一通道」（其
注释原文），而 `DataAgent` 只暴露 `ask(question, …)`（`:581`）——Plan 无法从外部
注入，`node_plan`（`:191-249`）每轮必调 Planner 并把 `plan` 冲刷为 `None`。

---

## 备选方案

| 方案 | 优势 | 劣势 |
|---|---|---|
| **`/api/v1` 前缀 + 硬切迁移 + 治理 router 独立文件（选定）** | 一次迁完无双份契约；SPA fallback 可用前缀一刀切；治理面不污染 `api.py` | 73 处调用点 + 26 处文档同批改（实测计数见决策 ②） |
| 双挂载（旧路径与 `/api/v1` 并存 + `Deprecation`/`Sunset` 头） | 外部消费者零中断 | **无外部消费者**（README KL #28：未生产部署）；openapi paths 翻倍、契约测试跑两遍、"删除日"永不到来 |
| 反向代理 / `root_path` 加前缀（代码不改） | 零代码改动 | 本地 `make serve` 无代理，前缀只存在于生产；测试与文档口径分裂；SPA fallback 问题原样存在 |
| 治理面另起进程/端口 | 与业务面物理隔离 | 破坏 ADR-0018 同源决策（引入 CORS 与凭据治理）；`workers=1` 声明翻倍；自用工具不值这个运维面 |
| `/plan/execute` 由 HTTP 层组合 `Compiler` + `enforce` + `executor` | 不改 graph | **复制 `node_execute` 的策略注入与二次只读校验**，两处逻辑一旦漂移即绕过 Guard（N3） |
| 治理端点带写能力（编辑指标 / 改策略） | 面板更"完整" | 与 ADR-0018「只读消费面」定位冲突；写路径需审批 + 审计 + N8 同名 active 检查，远超本批 |

---

## 决策

### ① `/api/v1` 前缀；`/health` 双挂，其余硬切

实现形态：两个 `APIRouter`，`app.include_router` 装配，**不用 `root_path`**。

```python
# serving/api.py：业务面（既有 4 条迁入）
router = APIRouter(prefix="/api/v1", tags=["business"])
# serving/governance.py：治理面（新文件，本 ADR 决策 ⑤）
gov_router = APIRouter(prefix="/api/v1/governance", tags=["governance"])
```

| 路径 | 处置 | 理由 |
|---|---|---|
| `GET /health` | **根路径保留** + 新增 `GET /api/v1/health`（同一 handler，两次 `add_api_route`） | 探针/负载均衡惯例在根路径；`docker-compose.yml:230-231` 的 healthcheck 硬编码 `http://127.0.0.1:8000/health`，**不改即零风险**（ADR-0018 决策 ⑤ 指出漏改会让容器永久 unhealthy，而 `:215/:217` 有两个服务用 `condition: service_healthy` 依赖它，且该失败在 CI 不暴露） |
| `POST /plan` `/compile` `/ask` | 只在 `/api/v1` 下，旧路径**直接删除** | 见决策 ② |
| `POST /api/v1/plan/execute` | 新增 | 决策 ③ |
| `GET /api/v1/governance/*` | 新增 8 + 2 | 决策 ⑤ |
| `/docs` `/redoc` `/openapi.json` | 保留在根（FastAPI 默认，不改） | 显式路由先于 catch-all fallback 匹配，无冲突 |

**「契约 v2」与 URL `/api/v1` 是两个不同的计数器，故意如此**：URL 版本是**对消费者的
兼容承诺**（`/api/v1` 一旦发布，只有破坏性变更才升 `/api/v2`），此前 4 条无前缀
路由从未做过版本承诺，所以首个受承诺的命名空间从 `v1` 起算；「契约 v2」指
ADR-0012 之后的第二版契约**文档**。ADR-0012 推翻条件第 3 条写的「LLM 引擎需要
服务化 → engine 参数开放（v2）」指的是后者语义下的能力扩展，**本 ADR 不开放
`engine` 参数**，该推翻条件仍未触发（落地注记中显式声明，避免术语撞车被读成已开放）。

`/api/v1/health` 的响应体 = ADR-0019 决策 ⑥ + ADR-0020 决策 ⑦ 扩展后的字段全集
（`status` / `head_sha` / `snapshot_sha` / `snapshot_source` / `snapshot_bound_to_head`
/ `snapshot_created_at` / `snapshot_tables` / `boot_id`），本 ADR **不再增删键**，
只改挂载路径。

> **前提已由被引用方落地（2026-09-14，0019 工作项 6）**：上面这 8 键现已在**根路径
> `/health`** 上返回（实测 8 键，正常与降级两条路径同键集，键名与顺序无关）；
> 路径带 `/api/v1` 前缀仍是**本 ADR 自己的**未落地范围（P-2api）。也就是说
> 「键集已冻结、挂载路径待改」两件事现在分属两个批次，别把本段读成 `/api/v1/health`
> 已可访问。断言在 `tests/test_identity_echo.py`（0019 判据 13），本 ADR 落地时
> 只需把该断言的期望路径改为双挂，**不得**顺手改键集。

### ② 硬切迁移：73 处调用点 + 26 处文档同批改完，不留兼容层

实测迁移面（`grep` 计数，2026-09-14）。当前 `app.openapi()["paths"]` 实测 **4 条**
（`/ask` `/compile` `/health` `/plan`），迁移后为 **16 条**（判据 1）：

| 位置 | 处数 | 处置 |
|---|---|---|
| `tests/test_api.py` | 30 处 `client.post/get` | 引入模块级 `API = "/api/v1"` 常量后拼接，**不逐条硬写前缀**（下次改前缀只动一处） |
| `tests/test_api_hardening.py` | 30 处 | 同上 |
| `eval/api_acceptance.py` | 13 处 | 同上（A1~A7 报告字段不变，`endpoint` 值随路径更新） |
| `README.md` | `:505`、`:517-520`（端点表）、`:532`、`:535-537`、`:547`、`:564`、`:708` | 端点表加前缀列；curl 示例逐条改；限流段改写为两桶 |
| `README.en.md` | `:133`、`:139`、`:141`、`:147`、`:153`、`:162`、`:164` | 与中文同步（两文件是同一契约的双语副本，漏改即口径分裂） |
| `docker-compose.yml` | `:230-231` | **不改**（决策 ① 根 `/health` 保留）；注释补一句「根 `/health` 为探针契约，ADR-0022 决策 ①」 |
| `infra/docker/api/Dockerfile` | `:36` `EXPOSE 8000`、`:38` `CMD` | 不改（端口与启动命令不变） |
| `serving/audit.py` docstring | `:5`「每业务请求一行（/plan /compile /ask…）」 | 改为路径无关表述 + 治理面口径（决策 ⑥） |

合计文档 26 处引用（`README.md` + `README.en.md`，`docs/*.md` 实测 0 处）。

**不保留旧路径的理由**：README KL #28 明示未生产部署，唯一消费者是仓库内测试、
文档与即将新建的前端；双挂载会把 openapi paths 从 16 条变成 19 条（三条业务路径
各多一份），契约测试必须对两套路径各跑一遍（43 例 → 86 例），而收益是零个真实
调用者的兼容。按 AGENTS.md §10 第 5 条（简洁）与 N2（不留"看起来兼容其实没人用"
的声明），硬切。

**同批必须完成的顺序约束**：路由迁移与全部 73 处调用点在**同一提交**内改完——
半改状态下 `make test` 会大面积 404 红，而 404 与"端点行为回归"在日志里形态相似，
排查成本远高于一次改完。

### ③ 新增 `POST /api/v1/plan/execute`：Plan 直接执行，仍走 `node_execute` 唯一通道

请求体 = `CompileBody` 同构（`metric/dimensions/time/filters/order_by/limit/model`，
`api.py:118-127`）+ 两个可选键：

| 键 | 语义 |
|---|---|
| `session_id?` | 缺省 = 一次性会话（不落 checkpoint、不写 `last_plan`）；给定 = 与 `/ask` **同一会话空间**（ADR-0020 决策 ④ 的 `thread_id` 命名空间），同受身份指纹 422 约束 |
| `question?` | 仅用于审计与 `explanation` 展示（默认取 Plan 的规范化文本）；**不参与解析** |

实现（三处最小改动，不复制执行逻辑）：

1. `TurnState` 增 `plan_override: Plan | None`（`agent/state.py`）——`Plan` 已在
   `_CHECKPOINT_SERDE` 的 msgpack 白名单内（`agent/graph.py:100-107`），
   不违反 ADR-0020 决策 ②「serde 原样复用」；
2. `node_plan`（`graph.py:191`）**首行短路**：`plan_override` 是 `Plan` 时，仍执行
   既有冲刷（`:195-216` 全键置空），随后 `out["plan"] = override` 并直接 return，
   **不调 Planner、不走 unmatched/候选链**；
3. `DataAgent.run_plan(plan, *, session_id=None, identity=None)`（`graph.py:581` 旁）
   ——与 `ask()` 共享 `invoke` 配置组装、轮数计数与 `record_turn` 埋点，只差
   初始 state 多一个 `plan_override`。

响应 = `TurnResult` 全集（与 `/ask` 同构，`_turn_payload`），`kind ∈ answer |
blocked | error`——**不可能是 `clarify`**（没有自然语言解析面，就没有歧义面）；
`handoff` 同理不可达（不进候选链）。这两条由契约测试锁定（判据 6）。

**N3 论证**：执行仍只经 `node_execute`（`graph.py:323-376`）——编译、
`resolve_claims` 渲染行级策略、`enforce` 注入 + 二次只读校验、`ExecutionValidator`
全部原样复用。HTTP 层**不得**自行组合 `Compiler().compile()` + `enforce()` +
`executor()`：那会在 `serving/` 里复制一份策略注入逻辑，与 graph 内那份漂移时
即形成一条绕过 Guard 的旁路。

身份下推与 `/ask` 完全一致（claims → `resolve_claims(identity, policy_name=…)`，
ADR-0021 决策 ③ 的新签名），`/plan/compile` 仍无执行面不注入。

### ④ 治理端点一律只读文件、绝不触发 DB 与 Agent 构造；只缓存贵的解析

| 约束 | 理由 |
|---|---|
| 不调 `_agent()`（`api.py:324-335`） | 治理面因此**不受快照 503 影响**：即使 `/ask` 因 `{HEAD}.meta.json` 缺失不可用（ADR-0019 背景），治理面板照常可用——这正是排查该故障时最需要看的面板 |
| 一律用 `REPO_ROOT` 绝对路径（沿 `api.py:68-73` 先例） | 不依赖进程 cwd（ADR-0012 代价段记录的 `SemanticModel()` cwd 依赖不扩散到治理面） |
| 复用既有解析器，**不新写 YAML 解析** | 治理扩展走 `semantic/governance_validate.iter_payloads`（`:71-89`，已是 lint 的事实源解析器）；指标表达式/同义词/owner 走 `SemanticModel`；角色走 ADR-0021 的 `roles_for_policy` + `RoleSpec`。零新增事实源 |

缓存策略按 **profile 而非直觉**（AGENTS.md §10 第 6 条）。实测（本机，2026-09-14）：

```
yaml.safe_load(atlas_finance.ossie.yaml, 64KB)   23.4 ms/次
iter_payloads(同文件)                            23.7 ms/次（内部各自 safe_load）
SemanticModel(finance) 构造                      23.9 ms
yaml.safe_load(atlas_retail.ossie.yaml, 18KB)     8.1 ms/次
20 份 semantic/values/*.json 全读                  1.0 ms/轮
48 份 eval/reports/*.json 全读（838 KB）           5.0 ms/轮
```

决策：**只缓存 ossie YAML 的 `safe_load` 结果与 `iter_payloads` 产物**，
key = `(path, st_mtime_ns, st_size)`——mtime 变化即失效，语义层改动立刻可见，
无需 TTL；`workers=1`（ADR-0020 决策 ⑧）故不存在跨进程失效问题。JSON 侧
（values 1.0 ms / reports 5.0 ms / snapshots）**直读不缓存**：收益不足以换取
一条失效逻辑的维护面。

报告索引（端点 7）**只解析 12 份主报告**，36 份非主报告仅取文件名 + `st_mtime` +
`st_size` + 模式标签，**不解析 body**——既快，又天然满足 ADR-0018 代价 ⑥
「不得伪造统一表头」。

### ⑤ 8 个治理集合端点 + 2 个钻取端点

「8」= ADR-0018 背景表所述「治理面板一次加载 8 端点」，即治理页挂载时的并发请求数；
钻取端点由用户点击触发，不计入挂载突发。

| # | 端点 | 数据源 | 关键载荷（全部来自实测字段） |
|---|---|---|---|
| 1 | `GET /api/v1/governance/models` | `semantic/ossie/*.ossie.yaml`（2） | `domain` / `model_name`（语义身份，与文件名解耦）/ `source_file` / datasets·metrics·relationships·fields 计数 / `time_dimension`（金融 single、零售 composite）/ `policy.default_row_policy` / `governance{owner,version,status,review_cycle_days}` / `lineage.source_tables` / `freshness{schedule,sla_minutes}` |
| 2 | `GET /api/v1/governance/metrics?model=` | 同上 + `iter_payloads` + `SemanticModel` | `name` / `expression`（ANSI_SQL 原文）/ `description` / `synonyms` / `owner` / `version` / `status` / `supersedes` / `lineage.source_columns` / `quality.gold_test_cases` / `quality.expected_value_snapshot_sha` / `fibo_alignment.mappings` |
| 3 | `GET /api/v1/governance/dimensions?model=` | `SemanticModel.datasets` + 值域注册表 | `dataset` / `field` / `physical` / `is_time` / `synonyms` / `value_domain`（`registered` \| `skipped` \| `none`） |
| 4 | `GET /api/v1/governance/synonyms?locale=` | `semantic/synonyms/*.yml`（4） | `metric_synonyms` / `dimension_synonyms` / `patterns`（分节原样） / **`empty_placeholder`** / `authority_note` |
| 5 | `GET /api/v1/governance/values` | `semantic/values/*.json`（20） | 清单：`model` / `field` / `status` / `values_count` / `skip_reason` / `snapshot_sha` / `generated_at` / `bound_dataset` / `source_table` / `source_column` |
| 6 | `GET /api/v1/governance/policies` | `row_policy.yml` + `ROLE_DIRECTORY` + `roles_for_policy` | 策略 → 角色 → `condition`（模板原文，含 `{{ user.X }}` 占位符）/ `default_deny` / `required_claims` / `list_claims` / **`registered`** / `declared_by_models[]` |
| 7 | `GET /api/v1/governance/reports` | `eval/reports/*.json`（48） | `name` / `pattern`（18 种模式之一）/ `structured`（仅主报告 true）/ `sha` / `created_at` / `dry` / `domains` / `size_bytes` / `mtime` |
| 8 | `GET /api/v1/governance/snapshots` | `data/snapshots/*.meta.json`（17） | `sha` / `created_at` / `source` / `data_range` / `raw_size_bytes` / `table_count` / `row_counts` 摘要 / `bound_to_head` / **`is_latest_by_created_at`** |

钻取（点击触发，2 条）：

- `GET /api/v1/governance/values/{model}.{field}` → 完整 `values` 数组 + 别名；
  `status: skipped` 的返回 `values: []` + `skip_reason` 原文（**不补空表说明以外的
  任何东西**，ADR-0016 §① 的阈值事实如实透传）；
- `GET /api/v1/governance/reports/{name}` → 主报告返回 7 键结构化；非主报告返回
  `{name, pattern, structured: false, raw: <原样 JSON>}`，由前端按「原始 JSON +
  模式标签」降级展示（ADR-0018 判据 10）。

**统一信封**（契约测试锁定键集）：

```json
{"kind": "governance.models", "count": 2,
 "sources": ["semantic/ossie/atlas_finance.ossie.yaml", "semantic/ossie/atlas_retail.ossie.yaml"],
 "items": [ ... ]}
```

`sources` 让每个面板都能显示「这来自哪个 Git 文件」——治理面的诚实性要求数据可溯源
到唯一事实源，而不是"服务端说的"。

`model` 查询参数取值沿用既有域白名单 `finance | retail`（复用 `_model_name`，
`api.py:82-88`，未知值 422），响应内同时给 `domain` 与 `model_name`
（`atlas_finance_analytics` / `atlas_retail_analytics`）——前者是 API 词汇，
后者是语义身份（ADR-0020 决策 ⑤、ADR-0021 决策 ② 同一区分）。

**黄金集不开端点**：106 条样本的逐条结果已在主报告的 `samples` 数组里
（实测 `samples[0]` 含 `id/question/ambiguous/lang/plan_ok/sql/…`）。再开一个
`/governance/gold` 就是把同一事实拆成两个出口，且黄金集的权威源是
`eval/gold/**/*.json` + `schema.json`，属评测面而非治理面。

### ⑥ 限流两桶 + 治理面审计

`RateLimiter`（`ratelimit.py:27-65`）类本身**不改**（固定窗口语义已锁定契约），
改为 `create_app` 持有两个实例：

| 桶 | 覆盖端点 | env（默认值） | 语义 |
|---|---|---|---|
| 业务桶 | `/api/v1/plan` `/compile` `/ask` `/plan/execute` | `ATLAS_RATE_LIMIT_MAX` / `ATLAS_RATE_LIMIT_WINDOW_SECONDS`（**沿用现名**，60 / 60） | 现有行为零变化：per-token 共享、429 + `Retry-After` |
| 治理桶 | `/api/v1/governance/*`（含钻取） | `ATLAS_GOVERNANCE_RATE_LIMIT_MAX` / `ATLAS_GOVERNANCE_RATE_LIMIT_WINDOW_SECONDS`（新增，**240 / 60**） | 独立计数，与业务桶互不挤占 |
| 豁免 | `/health`（根 + 前缀）、401 路径 | — | 与现状一致（公开面不限流；未认证请求不计数，不放大无效请求成本） |

两个默认值都是**配置占位，不是实测容量边界**（沿 `ratelimit.py:22` 与 KL #22
先例口径）。240 的依据是可算而非可测：治理页挂载 8 请求 + 平均 2 次钻取 ≈ 10
请求/次导航，240/min ≈ 24 次导航/分钟——**不是压测结论**，压测属 Phase 2。

429 响应 `detail` 标明桶名（`"请求过于频繁（治理面），请稍后再试"`），
`Retry-After` 语义不变；审计 `kind` 仍为 `rate_limited`，新增字段
`bucket ∈ business | governance`。

治理面审计：**每请求一行**，`kind = "governance_read"`，沿用 `AuditLog` 的 8 键
字段全集（`audit.py:34-43`），`session_id` / `row_count` / `latency_ms` 为 `null`
（治理面无会话无行数），`endpoint` 记完整路径（含 query 的 `model`，不含其它参数）。
开关沿用同一个 `ATLAS_AUDIT_DISABLED`（**不新增第二个开关**——两个开关会出现
"业务关了治理没关"的半开状态，排查成本高于收益）。代价：治理页每次挂载写 8 行
JSONL，见代价段 ④。

### ⑦ 诚实性标志位由端点携带，不由前端推断

ADR-0018 代价 ⑥ 列出三个「面板会误导人」的陷阱。本决策把判别信息**放进契约**，
使前端无法"忘记"展示：

| 陷阱 | 端点携带的标志位 | 实测依据 |
|---|---|---|
| 值域 11 份空壳被当成"已覆盖" | 端点 5/钻取的 `status: "skipped"` + `skip_reason` 原文 | 20 份中 11 份 `values: []`，`skip_reason` 形如 `distinct=2715 超过阈值 200` |
| 36 份非主报告被伪造统一表头 | 端点 7 的 `structured: false` + `pattern` | 48 份 = 12 主报告 + 36 份 / 18 种模式 |
| dry 报告的 `ex: "n/a"` 被读成 EX=0 | 端点 7 透传主报告 `dry` 布尔 | 主报告 7 键含 `dry` |
| `zh_cn.yml` 空表被读成"中文无同义词" | 端点 4 的 `empty_placeholder: true` + `authority_note` | `zh_cn.yml` 实测 `metric_synonyms: {}` / `dimension_synonyms: {}`，其文件头注释说明中文同义词权威源在 ossie 的 `ai_context.synonyms`（避免双权威源，ADR-0015） |
| `broker` 角色看起来可用 | 端点 6 的 `registered: false` | `sign_token('broker', …)` → `AuthError: 角色未注册`（实测；ADR-0011 落地注记与 ADR-0021 决策 ⑤ 将在 P-2sec 兑现） |
| 「最新快照」按文件名取错 | 端点 8 按 `created_at` 降序 + `is_latest_by_created_at` | 实测字典序最大是 `dc4f350`（2026-09-04），而 `created_at` 最新是 `a11d779`（2026-09-09）——**两者不同**，正是 ADR-0019 背景里那类错误 |

附带修正：`FastAPI(version=…)`（`api.py:287`）改为读 `importlib.metadata.version("atlas")`，
与 `pyproject.toml:9` 单一事实源同步（实测两者今天分别是 `0.1.0` 与 `0.1.4`，
属 N2 型漂移：OpenAPI 页面自称的版本是假的）。

---

## 理由

1. **前缀是 SPA 同源的必要条件，不是美学**：ADR-0018 决策 ② 的 fallback 只有在
   「API 全部在一个前缀下」时才能写成一条规则（`if not path.startswith("/api/")`）。
   否则 fallback 变成一张随端点增长的手工白名单，而漏项的症状是**前端拿到 200 HTML**
   ——最难归因的一类故障。
2. **根 `/health` 双挂是用一行代码换掉一个真实事故面**：compose healthcheck 漏改
   → 容器永久 unhealthy → 两个 `service_healthy` 依赖方起不来 → 而 CI 不跑 compose
   （ADR-0018 决策 ⑥：4 个 workflow 从未执行过一次）。这个失败链条没有自动兜底，
   能不改就不改。
3. **`/plan/execute` 必须复用 `node_execute`**：安全语义（策略渲染 + 注入 + 二次
   只读校验）只应存在一份实现。HTTP 层复制一份，短期看是"少改 graph"，长期看是
   给 N3 开一条静默旁路——两份实现漂移时不会有任何测试失败，只会让某个角色的
   谓词悄悄不生效。
4. **治理端点开在业务面同一进程**：ADR-0018 已裁定同源永久零 CORS；另起端口会把
   CORS、凭据、`workers=1` 声明全部翻倍，而隔离收益对自用工具为零。
5. **缓存只加在实测贵的地方**：ossie YAML 解析 23.4 ms/次 × 8 端点一轮 ≈ 150 ms
   纯解析，值得缓存；JSON 侧 1~5 ms 全量，加缓存只是多一条失效逻辑。
6. **两桶而不是抬阈值**：把 60 抬到 240 会让业务面（真实 Doris 执行）也失去
   保护；两桶让"面板浏览"与"问数"各自有独立配额，且互不掩盖。

---

## 代价与限制

① **破坏性契约变更，且无兼容期**：73 处调用点 + 26 处文档必须同批改完
（决策 ②）。任何在途的外部脚本（用户自己的 curl 收藏、`docs/atlas_query_test_cases.md`
之类的手工用例）会直接 404。补偿：README 端点表与 curl 示例同批更新，
`make api-verify` A1~A7 全绿作为迁移完成的唯一判据。

② **治理端点把策略条件模板与角色目录暴露给任何已认证角色**：`condition` 模板
（含 `{{ user.region }}` 占位符，**不含渲染值**）与 `required_claims` 契约对所有
Bearer 可见。今天这与公开 Git 仓库（gitee）的暴露面等同，故不构成新增泄漏；
但接入 IdP / 多租户后需要新增「治理读」权限位并按角色收敛——列入推翻条件。
决策 5（ADR-0011）的「被拒路径不外泄细节」不受影响：治理面返回的是**模板**，
不是某次被拒的谓词值。

③ **治理面无分页**：端点 2 返回 20 个指标全量、端点 7 返回 48 条索引全量。
按实测规模（ossie 64 KB、报告索引不解析 body）单响应体在数十 KB 量级，够用；
但报告数会随每次 commit 单调增长（ADR-0010 要求每 commit 产报告），
**一年后约 400 份**，届时索引需分页或按 `pattern` 过滤。这是可预期的债务，
不在本批解决（避免为未到来的规模设计）。

④ **治理面审计放大 JSONL 体积**：每次治理页挂载 8 行。按每天 20 次导航估算
约 160 行/天、每行 ~200 B → ~32 KB/天。审计文件无轮转（`audit.py` 现状），
本批**不加轮转**（超出范围），但记入此处的量级估算，便于将来判断。

⑤ **`/plan/execute` 让"绕过 Planner"成为一条正式路径**：调用方可以直接提交任意
Plan（含语义层里不存在的指标名）——此时 `Compiler` 抛 `CompileError`，
表现为 `kind: "error"`（不是 422）。这是有意的：与 `/ask` 的字段全集保持一致，
让前端只处理一套响应形态；但意味着**契约测试必须显式覆盖"非法 Plan"这一例**，
否则错误形态无人锁定（判据 6）。

⑥ **`plan_override` 与追问补全的交互**：带 `session_id` 调用 `/plan/execute` 时，
`node_explain`（`graph.py:406-408`）会把该 Plan 写进 `last_plan`，于是它成为
下一轮残句追问（"那 2014 年呢"）的补全基线。这是**期望行为**（用户手改的 Plan
就是他心里的问题），但需要在 README 写明，否则"我没提问为什么它记住了"会成为
困惑源。不带 `session_id` 时用一次性 thread，不写任何会话态。

⑦ **OpenAPI 面仍无认证**：`/docs` `/openapi.json` 在根路径公开（FastAPI 默认），
任何能访问端口的人都能看到完整契约（含治理端点清单）。同源自用场景可接受；
生产化需 `docs_url=None` + 单独暴露——列入推翻条件。

⑧ **前端类型与 OpenAPI 的漂移防线是文本比对**：判据 9 用正则从
`frontend/src/api/endpoints.ts` 提取路径常量与 `app.openapi()["paths"]` 比对，
这是弱耦合（TS 文件换写法即失效），但换来的是**零新增依赖、零生成物入库**
（与 ADR-0018 决策 ⑤ 反对产物入库同理）。若将来引入 `openapi-typescript`，
本判据应替换为类型编译期检查。

---

## 什么情况下应该推翻

- **出现外部 API 消费者**（其它系统调用 Atlas）→ 决策 ② 的硬切口径作废，需引入
  兼容期双挂载 + `Deprecation`/`Sunset` 头 + 变更公告流程；
- **接 IdP / 多租户**（ADR-0011「gateway 认证先行」推翻条件触发）→ 决策 ⑤ 的
  「任何已认证角色可见全部治理面」必须收敛为按角色授权（新增治理读权限位），
  且端点 6 的 `condition` 模板需按可见性裁剪；
- **生产化部署**（对外网暴露）→ 决策 ⑦ 代价 ⑦ 的 `/docs` 公开面必须关闭，
  限流两桶的占位阈值必须替换为压测结论（当前 60 / 240 均非实测）；
- **治理面板需要写操作** → 本 ADR 的只读定位作废，与 ADR-0018 推翻条件第 3 条
  同时触发，需重新设计审批 + 审计 + N8 同名 active 检查；
- **报告数增长到索引响应体不可接受**（实测 > 1 MB 或 > 2000 份）→ 决策 ⑤ 端点 7
  必须加分页/过滤，代价段 ③ 的债务到期；
- **`/plan/execute` 被用于绕过澄清**（用户直接构造 Plan 跳过 Planner 的宁缺不猜
  策略，导致口径错误无人拦截）→ 需在该端点增加"Plan 必须能由某个问句解析产生"
  的可追溯要求（例如强制带 `question` 并记录两者的差异），本 ADR 未设此约束。

---

## 验证方式

**P-2api 批次判据（全部可脚本化，进 `make test` / `make api-verify`）**：

1. `app.openapi()["paths"]` 的路径集合 = **16 条**（根 `/health` + `/api/v1/health` +
   `/api/v1/{plan,compile,ask,plan/execute}` + 8 治理集合 + 2 钻取；实测迁移前为 4 条），
   **不含任何无前缀的业务路径**（`/plan` `/compile` `/ask` 返回 404 而非 200）；
2. `GET /health` 与 `GET /api/v1/health` 返回**同一 body**（含 ADR-0019 决策 ⑥ +
   ADR-0020 决策 ⑦ 的全部字段），且 `docker-compose.yml:230-231` 的 healthcheck
   字符串**逐字未变**（`git diff` 断言）；
3. 既有 43 例契约测试（`test_api.py` 26 + `test_api_hardening.py` 17）迁移后
   **全绿且断言强度不降**（不得为了通过而放宽任何断言；429 / 422 / 401 / 503 /
   Decimal→str 形态逐条保留）；
4. 限流两桶独立性：同 token 先打满治理桶（240 次）→ 紧接一次 `/api/v1/ask`
   **仍 200**；反向亦然。429 的 `detail` 含桶名、`Retry-After` 存在，
   审计行含 `bucket` 字段；
5. 治理端点在**快照缺失时仍 200**：临时改名 `data/snapshots/{HEAD}.meta.json`
   → `/api/v1/ask` 503 而 8 个治理端点全部 200（决策 ④ 的"不触发 Agent 构造"）；
6. `/api/v1/plan/execute`：
   - 合法 Plan（gold-102 同构）→ `kind: "answer"`，`sql` 含 `LIMIT` 与时间谓词，
     `explanation` 的 **13 个固定键**（实测 `graph.py:383-400`）形态与 `/ask` 一致；
   - 带 identity（`branch_manager`）→ 出口 SQL 含分支谓词 + `policy_effect` 生效句
     （与 `test_api_hardening.py:106` 同断言形态）；
   - 非法 Plan（指标名不存在）→ `kind: "error"`，**不是 422、不是 500**；
   - `kind` 永不为 `clarify` / `handoff`（对歧义问句对应的 Plan 亦如此）；
   - 无 `session_id` 时 `agent.sessions` 不新增条目（不落会话）；
7. 治理端点诚实性标志位逐条断言（决策 ⑦ 表格 6 行）：
   `values` 中 `status == "skipped"` 的条目数 = 11 且各含非空 `skip_reason`；
   `reports` 中 `structured == true` 的条目数 = 12、`pattern` 去重后 = 18 种；
   `synonyms?locale=zh_cn` 的 `empty_placeholder is True`；
   `policies` 中 `broker` 的 `registered is False`（P-2sec 落地后此断言**必须反转**，
   同批修改，不得留着变成假绿）；
   `snapshots` 首条 `sha == "a11d779"`（按 `created_at` 降序）且
   `is_latest_by_created_at is True`，同时断言 `dc4f350` **不是**首条；
8. `make api-verify` A1~A7 全绿并产出 `eval/reports/api-acceptance-<sha>.json`，
   新增 A8（治理面一轮挂载 8 请求全 200 + 不撞业务桶）与 A9（`/plan/execute`
   真链 EX 与快照一致）；
9. 契约防漂移：`tests/test_api_contract_v2.py` 断言 openapi paths 集合 ==
   从 `frontend/src/api/endpoints.ts` 正则提取的路径常量集合（该 TS 文件由 P0b
   建立；P-2api 阶段先落 Python 侧断言与一份路径清单常量，P0b 接入 TS 侧）；
10. `FastAPI(version=…)` 与 `importlib.metadata.version("atlas")` 相等
    （决策 ⑦ 附带修正），且 `pyproject.toml` 版本变更后 OpenAPI 自动跟随。

**文档判据**：

11. README `:517-520` 端点表扩为 13 行（含前缀列、认证列、限流桶列），
    curl 示例逐条可复制执行；`README.en.md` 同步（两文件端点表行数一致）；
12. ADR-0012 末尾追加落地注记（不改正文）：决策 2 的端点清单被本 ADR 取代；
    **推翻条件第 3 条（engine 参数开放）未触发**——本 ADR 的「v2」是 URL 契约
    版本，不开放 `engine`；
13. ADR-0018 决策 ⑤ 第 4 条（compose healthcheck 必须同步）**由本 ADR 决策 ①
    收窄为「不需要同步」**，理由与判据 2 的 `git diff` 断言一并记入该 ADR 的
    落地注记，避免两份 ADR 对同一行代码给出相反指令；
14. `.env.example` 增 `ATLAS_GOVERNANCE_RATE_LIMIT_MAX` /
    `ATLAS_GOVERNANCE_RATE_LIMIT_WINDOW_SECONDS` 两行（只列名不含值，N9），
    并注明两个默认值均为配置占位非实测阈值。

**证伪条件**：若判据 5 无法达成（治理端点在快照缺失时仍 503），说明治理面某处
隐式触发了 Agent 构造，决策 ④ 的隔离前提被推翻 → 必须把治理端点的数据加载
全部改为显式文件读取并重新实测；若判据 4 无法达成（两桶互相挤占），说明
`RateLimiter` 的 key 空间设计有误，决策 ⑥ 回退为「单桶 + 抬高阈值」并在
README KL 如实记录业务面保护被削弱。

## 落地注记（2026-09-16，ADR-0026 新增 /analyze 端点）

> 本节为落地注记，不重写上文历史正文。记录 ADR-0026 固定四步贡献分析落地后对本契约的增量。

**新增端点**：`POST /api/v1/analyze`（ADR-0026）。请求体与 `/ask` 同构（`AskBody`：`question`、可选 `session_id`/`model`），Bearer 认证，走业务限流桶；无分析意图时按 ADR-0026 决策③回落普通 ask（`kind=answer`）或澄清，两种回落 `analysis=null`。

**analysis 响应形态**：顶层新增 `analysis` 键——17 键投影（schema_version, intent, status, metric, dimension, baseline, current, filters, snapshot_sha, semantic_sha256, recipe_version, totals, items, steps, reason_code, text, elapsed_ms），键恒在；非分析轮整体为 `null`。数值（totals/items 的 value、delta、contribution_pct）一律 Decimal 字符串化或 `null`；`analysis.status ∈ ok/unavailable/blocked/error` 全终态并带 `reason_code`（clarify 轮整体为 null、不投影 status）；`steps[]` 成功步携带 role/kind/sql/columns/rows/latency_ms，失败步 `sql/columns/rows` 置空只留 role/kind/latency_ms/reason_code（被拒 SQL 不出服务边界）；分析轮父 turn 记账沿用本契约 `turns_in_session`（一个用户逻辑轮计一轮，子步不计）。

**纠正「随机 ID ≠ 无存储」**：缺省 `session_id` 随机生成仍带 checkpointer 落 checkpoint——`agent/graph.py` `_invoke_turn` 对缺省 session_id 只是随机生成 ID，仍调用带 checkpointer 的图（ADR-0026 背景证据）；一次性随机 ID ≠ 不落盘。

**422 会话身份冲突（C2）对 /analyze 同样生效**：跨身份复用 session_id 返回 422「会话身份冲突，请换新 session_id」，与 `/ask` 同规则（api-verify A10a~A10d 实测覆盖，含 A10d 跨身份 422）。快照缺失时 503 语义与既有端点一致。
