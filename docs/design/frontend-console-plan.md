# 前端控制台细化设计：批次、面板、契约与目录

对应决策：`infra/adr/0018-frontend-console.md`（技术栈 / 同源部署 / 工程边界 / 信息优先级）
上游依赖：`0017`（时间智能）、`0019`（运行时快照口径）、`0020`（会话持久化）、
`0021`（行级策略二维事实源）、`0022`（HTTP 契约 v2）、`0023`（许可证统一）、
`0025`（图表 spec 接入与时间轴列事实源，状态 **accepted**，2026-09-14 用户确认）——本页
§6.1 已由「建议新建 ADR」改为「裁定摘要 + 实现级细化」
逐批执行顺序、开工/收口条件与提交拆分见 `dev-plan-0017-0025.md`（本页不重复）

**状态：设计定稿，全部未落地（2026-09-14 实测）。** 本页描述的任何界面、目录、
make 目标、端点消费关系**当前都不存在**：

```
ls frontend                              → ls: frontend: No such file or directory
app.openapi()["paths"]                   → 4 条（/ask /compile /health /plan，无 /api/v1 前缀）
ROLE_DIRECTORY（serving/auth.py:46-76）  → 5 键，无 broker
grep -nE "^(ui-[a-z]+|serve-dev):" Makefile → 0 命中（ui-check/ui-dev/ui-build/serve-dev 均不存在）
grep -rn render_chart --include="*.py"   → 仅 agent/tools/chart.py 自身与 tests/test_chart.py
```

本页纪律（沿同目录 `docs/design/adr-0015-pattern-lexicon-zh.md` 的既有惯例）：

1. **不重复 ADR 裁定**——ADR 已裁定的内容只引用不转述，本页只补 ADR 未展开的
   实现级细节（面板划分、端点—组件映射、文件级目录、逐批复验断言）；
2. **所有计数标注测量方式与日期**，未实测一律写 `<待实测后填写>`（AGENTS.md §9.3）；
3. **不把设计写成已完成**（N2）：本页的每个"将"字都对应一个尚未开工的批次；
4. **不拍板 ADR 未裁定的选型**：遇缺口进 §6 登记并给出建 ADR 的命令，不在本页决定。
   §6.1 已按该纪律走完全程：建 ADR-0025 → 以其裁定回填本页（包括**推翻本页初稿的
   倾向 2**），本页不保留任何与 ADR 相左的表述。

---

## 0. 本页与 ADR 的分工

| ADR 已裁定（本页不重复） | 本页细化（ADR 未展开） |
|---|---|
| 技术栈 6 包与版本约束（0018 ①） | 每个包在哪个文件里被 import、为什么需要它 |
| 同源部署 + SPA fallback 必须在 `/api/v1` 之后（0018 ②） | fallback 的具体注册位置与 4 个路由的挂载顺序 |
| dev 双档 `ATLAS_API_TARGET`/`ATLAS_API_BASE` 成对切换、禁 `localhost`（0018 ③） | 两档在 `.env.example` 的行形态与 vite.config 的读取方式 |
| `frontend/.gitignore` 局部忽略、npm + lock 入库、`.dockerignore` 补两条、tsconfig 严格度（0018 ④） | 文件级目录树与每个文件的存在理由（§4.1） |
| 多阶段构建、条件挂载、`GIT_SHA` 三处默认值统一（0018 ⑤） | Dockerfile 阶段名与 `COPY --from` 的具体行 |
| 本地 make 是唯一真实门槛、`NODE_BIN` 探测、pre-commit exclude（0018 ⑥） | Makefile 4 个新目标的 recipe 形态与失败指引文案 |
| 渲染顺序锁定：指标口径 → 行数/耗时 → 出口 SQL → 数据 → 截断声明（0018 ⑦） | 该顺序对应的组件树与四态分支（§3.4） |
| 16 条端点、统一信封、6 类诚实性标志位、限流两桶（0022 ①⑤⑥⑦） | 端点 → 面板 → 组件的映射表（§3.1）与标志位的逐条渲染义务（§3.3） |
| 角色矩阵 6×2、切角色换 `session_id`、`category_analyst` 只能走 HTTP JSON（0021 ⑤） | 角色切换器的表单生成逻辑与 token 存放位置（§3.5） |
| `/health` 8 键（0019 ⑥ + 0020 ⑦ + 0022 ①） | 快照 sha 常驻展示的具体位置与 4 种取值的文案 |
| 批次唯一合法序 P-1 → P-2sec → P-2api → P0a → P0b → P1~P3（0018 落地注记 3） | 每批的判据编号区间、开工前置、复验命令与允许的失败形态（§1、§5） |
| — | **5 个面板的具体划分**（0018 理由 1 只给计数未列举，§2） |
| chart spec 的挂载位置、时间轴列事实源、frozenset 兜底口径、点数上限、是否进 checkpoint（0025 ①~⑥，状态 **accepted**） | 该 spec 在面板 5 的组件映射与「未落地前不建目录」的结构约束（§2、§4.1、§6.1） |
| — | **未裁定项登记**（治理面分页、前端可观测、npm 许可证扫描域、limit 截断标志，§6.2~§6.5）。**原第 5 项「图表接入」已由 ADR-0025 裁定**，§6.1 相应改为裁定摘要 |
| — | **Known Limitations 增量草案**（§7） |

---

## 1. 批次依赖与开工前置

### 1.1 唯一合法顺序与判据编号区间

顺序本身由 ADR-0018 落地注记第 3 条裁定，本页只补**每批要跑哪些判据**（编号取自
各 ADR 的「验证方式」段，逐条核对于 2026-09-14）：

| 批次 | ADR | 判据编号区间 | 判据性质 | 前端相关性 |
|---|---|---|---|---|
| **P-1** | 0017 | 1~7 | 1~3 契约、4~6 真链（需 Doris）、7 文档 | 无直接关系；但判据 4（**13 条非歧义样本** EX 锚定回填；另 2 条歧义样本只改字段、不产生 hash）**会改变 P0a 判据 5 的分母**，见 §1.3 歧义 4 |
| | 0019 | 1~6 契约 / 7~9 真链 / 文档判据 2 条（无编号） | — | **强相关**：`/health` 从 3 键（`api.py:338-345`）扩到 8 键，工作台顶栏的快照徽标消费其中 5 键 |
| | 0020 | 1~2 依赖与许可证前置 / 3~9 契约 / 10~12 真链 / 13~14 文档 | — | **强相关**：`turns` 单一事实源与 `boot_id`；会话时间线面板的数据源 |
| **P-2sec** | 0021 | 1~7 契约 / 8~9 lint / 10~12 真链 / 13~14 文档 | 必须独立 `sec` 提交（AGENTS.md §8） | **强相关**：`RoleSpec` + `roles_for_policy` 是角色切换器的表单生成源；`broker` 注册后矩阵才满 6 行 |
| **P-2api** | 0022 | 1~10 契约与真链 / 11~14 文档 | 路由迁移与 73 处调用点**同一提交**（0022 ②） | **前置**：`/api/v1` 前缀 + 10 条治理端点 + 治理桶；未落地则治理面板无数据源（0018 落地注记 4） |
| **P0a** | 0023 | 1~11 代码 / 12~15 文档 | — | **前置**：项目许可证基准；未统一则 P0b 判据 1 的第三方包许可证回填无判定基准（0018 落地注记 3） |
| **P0b** | 0018 | 1~5 | 工程边界，不含任何界面 | 建立 `frontend/` 骨架、`make ui-*`、pre-commit、Dockerfile 阶段、条件挂载 |
| **P1~P3** | 0018 | 6~10 | 能力 | 见 §2 的面板—批次分配 |
| **P3**（图表部分） | **0025** | 1~8 | 1~3、5、7 契约（P-1 后即可落）/ 4 真链（需 Doris，**P-1 之后**）/ 6 延后探针 / 8 文档 | **强相关**：面板 5 的唯一数据源是 `chart` 键；决策 ⑤ 把 UI 部分**硬阻塞于 P-1** |

**不在本链内的 ADR**（防误读）：`0024-cube-export-target-engine-gate.md`（Cube 定位为
离线导出目标，状态 **accepted**，2026-09-14 用户确认）不属于上表任何批次——它裁定的
`make export-cube` + `exports/cube/` 是**离线导出器**，其决策 ② 明写「查询路径逐字
不变」（compiler / Guard / 只读红线 / 行级策略来源均不动），故与前端批次**无顺序
依赖**。但有一条单向约束值得登记：0024 背景 §3 把「ADR-0018 前端走 REST、用不到
Cube 的 SQL-API 与 GraphQL」当作「无具名消费者」的证据之一，而 0024 的证伪条件是
「若发现已存在具名 BI / GraphQL 消费者则重评引擎化」——**前端一旦引入任何非 REST
数据源（如直连 Postgres-wire），就会触发 0024 的重评**。本页 §3.1 的端点表因此
也是 0024 门禁的间接证据，修改它时需同步考量。

**判据 9（0022）跨两批**：它要求 openapi paths 集合 == 从
`frontend/src/api/endpoints.ts` 正则提取的路径常量集合，而该 TS 文件由 P0b 建立。
0022 判据 9 原文已裁定分段落地（"P-2api 阶段先落 Python 侧断言与一份路径清单
常量，P0b 接入 TS 侧"），本页给出具体形态：

- P-2api：`tests/test_api_contract_v2.py` 内定义 `EXPECTED_PATHS: frozenset[str]`
  （16 条字面量）+ 断言 `set(app.openapi()["paths"]) == EXPECTED_PATHS`；
- P0b：`frontend/src/api/endpoints.ts` 以 `export const API = { health: "/api/v1/health", … } as const`
  形态落地，同一测试文件改为**读该 TS 文件正则提取**并与 `EXPECTED_PATHS`
  双向断言相等——`EXPECTED_PATHS` 保留作为"正则提错东西"的兜底。

**正则必须同时覆盖根 `/health`**（本页补的坑）：16 条里有一条是**无前缀的根
`/health`**（0022 决策 ① 为探针契约而双挂），而前端**不消费它**（§3.1）。若正则
只写 `/\/api\/v1\/[a-z0-9_\/{}.-]+/g` 就只能提到 15 条，集合相等断言**永远红**；
若为了讨好看而把断言改成"提取集 ⊆ paths"，则丢失了反方向（前端写了不存在的
端点也能过）——两者均不可。故：

- `endpoints.ts` 必须显式包含 `export const PROBE_HEALTH = "/health"`，并注释
  「**前端不调用**；仅为 0022 判据 9 的集合相等而存在（探针契约，决策 ①）」；
- 提取正则用 `/"(\/(?:api\/v1|health)[a-z0-9_\/{}.-]*)"/g`（仅匹配双引号字面量，
  避免把注释里的路径也算进去）；
- 断言必须是**双向相等**，不是子集。

### 1.2 硬依赖的实测证据

每条都是"不修则对应面板不可用"，测量方式随附（2026-09-14）：

| 面板 | 阻塞事实 | 测量方式 | 解除批次 |
|---|---|---|---|
| 问数工作台 | ~~`/ask` 在当前 HEAD 503：`factory.py:40` 要求 `{HEAD}.meta.json` 存在，而 HEAD 无该文件~~ **成因已消除**（2026-09-14 0019 工作项 3：`create_live_agent()` 实测能构建并绑 `a11d779`；`/health` 该键不再是 `null`，回显 `snapshot_source=latest`）。**该端点已真机验通**（2026-09-14 `make serve` + `curl`：200、8 键、`head=ccb4c8b`、`snapshot=a11d779`、`bound_to_head=false`、`tables=29`），但本行不得改判「已解除」：`/ask` 端到端返回 answer 需 Doris，属 0019 真链判据 7/8，只有 P-1 收口跑通后才能宣称——**2026-09-16 收口已跑通**（`/ask` 双域返回 answer、含 `snapshot_sha` 回显，见 0019 判据 7 收口落地注），本行按诺改判**已解除** | `curl 127.0.0.1:8000/health` → `snapshot_sha: null`（0018 背景表已录，**该值已过时**；现按 8 键集核，定位命令见 dev-plan §0 起点表） | P-1（0019 决策 ① 三级优先，第 3 级回落 latest）**→ 已解除（2026-09-16 收口真链验通）** |
| 问数工作台 | 响应体 19 键（`_turn_payload`，撰写时 `api.py:183-213`）无快照溯源字段 **→ 已落（2026-09-14 工作项 6）：实测 21 键**，两键为 `snapshot_sha` / `snapshot_bound_to_head`，未绑定时为 `null` 而非缺失 | 源码逐行数键 → 现为行为断言（`tests/test_identity_echo.py`，0019 判据 13） | P-1（0019 增两键 → 21 键）**已解除** |
| 会话时间线 | ~~`MemorySaver` 进程内，重启静默失忆~~ **→ 代码面已落（2026-09-14~15 工作项 7~9）：设 `ATLAS_CHECKPOINT_DB` 后 SQLite checkpointer，`turns`/身份指纹入 checkpoint；真链跨重启已验通（2026-09-16 收口：`turns` 续至 5、`boot_id` 变化而其余 7 键不变，见 0020 判据 10/11/12 收口注）** | 0020 背景段实测 | P-1（0020）**已解除（2026-09-16 收口真链验通）** |
| 角色切换器 | `broker` 签发即 `AuthError`；`hq_admin` 在零售域误报 `rp_branch_visible` | `sign_token('broker', …)` 实测抛错；`ROLE_DIRECTORY` 5 键 | P-2sec（0021 决策 ④⑤） |
| 角色切换器 | claims 契约（`required_claims`/`list_claims`）无机器可读出口 | 0021 决策 ⑤ 理由 5 | P-2sec（`RoleSpec`）+ P-2api（治理端点 6） |
| 治理面板 | 零只读结构化端点 | `app.openapi()["paths"]` = 4 条，全为业务面 | P-2api（0022 决策 ⑤，8 集合 + 2 钻取） |
| 治理面板 | per-token 共享桶 60/min，一次挂载 8 请求即撞穿 | `ratelimit.py:22-24`（`DEFAULT_MAX_REQUESTS = 60`，`:22` 注释自述为"配置化占位非实测阈值"） | P-2api（0022 决策 ⑥，治理桶 240/min） |
| 图表面板 | `render_chart` 未接进任何响应；时间轴列识别在**两域都失效**（根因是 `compiler.py:991` 的 `time_alias = time_col.lower()`，不只是 frozenset 缺零售列名） | `grep -rn render_chart --include="*.py"` 仅命中 `chart.py` 与 `tests/test_chart.py`；ADR-0025 背景 3 的 10 组合实测表（双域 × 5 形态，用 `eval.runner.build_budget()` 构造真实预算）：`type` 全为 `bar`，comparison 形态下 y 轴变成时间列本身、真度量落进 `note` | **已由 ADR-0025 裁定**（accepted）→ §6.1 摘要；落地批次 P3，**硬阻塞于 P-1**（0025 决策 ⑤） |
| 任何面板 | 项目许可证三方矛盾，第三方包兼容性无基准 | `LICENSE` = MIT 全文 / `pyproject.toml:13` = Apache-2.0 / `README.md` §12 代码行 = Apache-2.0（初稿记 `:756`，2026-09-14 复测已漂到 `:860`；以 `grep -n "^## 12" README.md` 定位） | P0a（0023 决策 ①） |

### 1.3 四处顺序歧义的消解

**歧义 1：ADR-0020 判据 1（许可证前置）在 P-1，而项目许可证统一在 P0a——是否倒置？**

不倒置。0020 判据 1 的动作是"读 3 个新装包的 `License-Expression`，**任一非
permissive → 触发推翻条件第 3 条**"，这是 permissive / 非 permissive 的**二分判定**，
不需要知道项目自身许可证是什么。P0a 需要项目基准的是**兼容性判断**（0018 判据 1：
5 个前端包许可证能否被项目许可证包含）。两者判据形态不同，故 P-1 的许可证前置
在 P0a 之前执行不构成循环依赖。

**歧义 2：`make license-check`（P0a）的扫描域是否覆盖 P0b 引入的 npm 包？**

不覆盖，且这是**设计使然而非疏漏**：0023 决策 ⑦ 的断言 4 走
`importlib.metadata.distributions()`，只看得见 Python 分发元数据；`frontend/node_modules`
不在其中。后果必须显式登记：**P0b 之后项目多出一套 `make license-check` 看不见的
第三方依赖面**（实测回填 2026-09-16：直接依赖 **9 个**（dependencies 4 +
devDependencies 5），全树 **186 包**、lock **234 条目 / 115150 字节**；原记
**「6 个直接包」**系撰写时预估、以 `frontend/package.json` 实装为准；参照量级
是 remotion 的 152 包 / lock 142KB），其许可证只由 0018 判据 1 的**人工逐包回填**覆盖。
是否把 npm 纳入自动检查 → §6.4。

**歧义 3：0018 决策 ⑤ 第 4 条（compose healthcheck 必须同步）是否仍需在 P0b 执行？**

不需要。已由 0022 决策 ①（根 `/health` 双挂）收窄，见 0018 落地注记第 1 条与
0022 判据 2 的 `git diff` 断言。本页 §4.2 的仓库侧改动清单因此**不含**
`docker-compose.yml:230-231`。

**歧义 4：P-1 会改变 P0a 判据 5 的分母（已回写 ADR-0023，分母已二次纠正为 97）**

0023 判据 5 初稿写死「84 条锚定 hash 逐条不变」（撰写时实测：`eval/gold/` 119 个
样本文件中 84 条同时具备真值 `result_hash` + 实测 `snapshot_sha`）。但合法序是
**P-1 在 P0a 之前**，而 0017 判据 4 要求 P-1 把双占位样本锚定回填。本页初稿据此
推出「分母应为 99」（= 84 + 15 条双占位），**该推论错了**：2026-09-14 逐文件
重测并按定义分类后，15 条双占位 = **13 条非歧义**（金融 gold-172~178、零售
gold-072~077）+ **2 条歧义**（gold-179「交易额同比」、gold-078「销售额同比」）；
后者按 0017 决策 ② 走澄清、**无结果行**，设计上永不产生 `result_hash`，其占位符
是永不可能兑现的承诺。故 **P0a 开工时分母应为 97**（= A 84 + B 13）。
五类完整定义与逐条归属见 ADR-0023 决策 ④ 的实测表。

若仍拿 84 当分母，恰好把时间智能这批**最复杂的窗口函数 SQL** 排除在驱动回归
比对之外——而它们正是 DECIMAL / 数值类型差异最可能暴露的地方（0023 决策 ④
的风险点）。

已回写 0023 决策 ④ 与判据 5：把分母从写死数字改为**公式**（「同时具备真值
`result_hash`（64 位十六进制）与实测 `snapshot_sha` 的样本数」），P0a 开工当日
重测并写入报告；重测值 ≠ 97 必须先归因（P-1 是否部分失败）再跑驱动对比。
0017 判据 4 亦同批从「15 条」改为「13 条非歧义 + 2 条歧义改字段」。

**对本页的通用教训（已升级）**：凡是跨批引用的计数，**一律写成公式 + 撰写时实测值 +
重测时点**，不写死数字。但歧义 4 证明**光有公式不够**：本页初稿已经遵循了
「公式 + 实测值 + 重测时点」三要素，公式写对了，**代入值仍错**（把 15 条占位符
当成 15 条可锚定样本）。故纪律补第四条：**实测值必须按公式的定义逐条验证归属**，
不得用另一个看似等价的计数代替（本例：「占位符个数」≠「可锚定个数」，因为歧义
样本按设计就不该有 hash）。本页自身遵循该纪律的三处：`/health` 键数（3 → 8）、
`_turn_payload` 键数（19 → 21）、`ROLE_DIRECTORY` 键数（5 → 6）。

---

## 2. 五个面板

0018 理由 1 只说"5 个面板的主体是表格/树/描述列表/标签页/筛选器"，**未列举是哪 5 个**。
本节的划分是本页的细化提案：若日后发现与 0018 的计数意图不符，**改本页不改 ADR**
（面板划分属 UI 分解，不属架构决策）。

5 个面板中 **3 个是路由页、2 个是嵌入式组件区**（"面板"≠"路由"）：

| # | 面板 | 形态 | 路由 | 消费端点 | 数据源 ADR | 批次 | 阻塞项 |
|---|---|---|---|---|---|---|---|
| 1 | **问数工作台** | 路由页 | `/ask` | `POST /api/v1/ask`、`POST /api/v1/plan`、`POST /api/v1/plan/execute`、`GET /api/v1/health` | 0019（快照 5 键）、0020（`turns_in_session`）、0017（对比态 Plan） | **P1** | 无（P-1/P-2api 落地后即可开工） |
| 2 | **角色切换器** | 全局顶栏组件区（所有路由共享） | — | `GET /api/v1/governance/policies` | 0021 决策 ⑤（`RoleSpec` + `roles_for_policy`）、0022 端点 6 | **P2** | P-2sec 未落地则矩阵只有 5 行且 `broker` 不可签发 |
| 3 | **会话时间线** | 路由页 | `/sessions` | `GET /api/v1/health`（`boot_id`）+ 工作台响应的 `session_id`/`turns_in_session` | 0020 决策 ④⑤（`thread_id` 命名空间、`turns` 单一事实源） | **P2** | 无独立端点：**0022 未开 `/governance/sessions`**，故本面板只能展示"当前浏览器标签内本次运行累积的会话"，跨重启的历史会话列表**无数据源**（见 §7 KL 草案 #33） |
| 4 | **治理面板** | 路由页（8 个子路由） | `/governance/{models,metrics,dimensions,synonyms,values,policies}` + `/governance/{reports,snapshots}` | 治理端点 1~8 + 2 钻取 | 0022 决策 ⑤⑦ | **P2**（前 6 子页）/ **P3**（reports、snapshots 两子页） | P-2api 未落地则零数据源 |
| 5 | **图表面板** | 工作台内嵌组件区（数据表之后，0018 ⑦） | — | 消费 `/api/v1/ask` 响应内独立的 `chart` 键（0025 决策 ②） | 0025 ①~⑥（状态 accepted）；上游 0017（comparison 形态的时间列由编译器产出） | **P3** | **已由 ADR-0025 裁定**（§6.1 摘要）；但 0025 决策 ⑤ 定为**硬阻塞于 P-1**：P-1 前只落契约测试不落 UI，否则 100% 出 bar（见 0025 背景 3 实测表） |

**为什么 reports/snapshots 两子页排 P3 而非 P2**：它们是治理面板里唯一需要**降级
渲染**的两页（0018 代价 ⑥：48 份报告中 36 份结构互不相同、18 种文件名模式，不得
伪造统一表头），逻辑量大于前 6 页之和；而前 6 页是"字段 → 表格列"的直接映射。
先交付直接映射的 6 页，能在 P2 就形成可用的治理浏览能力。

**为什么图表排最后**：它是唯一曾有未裁定缺口的面板（现已由 ADR-0025 裁定，§6.1），
且 0018 ⑦ 已锁定它"置于数据之后"——即使不做，工作台的信息完整性不受损（数据表已在）。
0025 决策 ⑤ 进一步把它**硬阻塞于 P-1**：在 Guard 对 CTE 名豁免（0017 判据 1）落地前，
同比/环比/累计三类问句根本过不了 Guard，图表只能拿到 plain/rank 两种形态；此时先交付 UI
等于对外隐含声称"支持时间序列图表"（N2 风险）。

---

## 3. HTTP 契约 → TS 类型 → 组件

### 3.1 16 条端点与消费方

端点集合由 0022 判据 1 锁定为 16 条（根 `/health` + `/api/v1/health` + 4 业务 +
8 治理集合 + 2 钻取）。迁移前实测 4 条。下表补**谁消费**（0022 未展开）：

| 端点 | 方法 | 认证 | 限流桶 | 消费组件 | 批次 |
|---|---|---|---|---|---|
| `/health` | GET | 无 | 豁免 | 不消费（探针契约，0022 决策 ①） | 现状 |
| `/api/v1/health` | GET | 无 | 豁免 | `SnapshotBadge`（顶栏常驻）、`SessionPanel`（`boot_id`） | P-2api |
| `/api/v1/plan` | POST | Bearer | 业务 | `PlanPreview`（工作台的"只看计划不执行"） | P-2api |
| `/api/v1/compile` | POST | Bearer | 业务 | `SqlPreview`（工作台的"只编译不执行"） | P-2api |
| `/api/v1/ask` | POST | Bearer | 业务 | `AskWorkbench` 主路径 | P-2api |
| `/api/v1/plan/execute` | POST | Bearer | 业务 | `PlanEditor`（手改 Plan 后直接执行；0022 决策 ③） | P1 |
| `/api/v1/governance/models` | GET | Bearer | 治理 | `GovernanceLayout` 挂载即取（域切换器数据源） | P-2api |
| `/api/v1/governance/metrics?model=` | GET | Bearer | 治理 | `MetricsTable` | P-2api |
| `/api/v1/governance/dimensions?model=` | GET | Bearer | 治理 | `DimensionsTable` | P-2api |
| `/api/v1/governance/synonyms?locale=` | GET | Bearer | 治理 | `SynonymsPanel`（含 `empty_placeholder` 横幅） | P-2api |
| `/api/v1/governance/values` | GET | Bearer | 治理 | `ValuesTable`（含 `skipped` 分组） | P-2api |
| `/api/v1/governance/values/{model}.{field}` | GET | Bearer | 治理 | `ValuesDrawer`（钻取） | P-2api |
| `/api/v1/governance/policies` | GET | Bearer | 治理 | `PoliciesTable` + `RoleSwitcher` | P-2api |
| `/api/v1/governance/reports` | GET | Bearer | 治理 | `ReportsTable`（`structured` 分组） | P-2api |
| `/api/v1/governance/reports/{name}` | GET | Bearer | 治理 | `ReportDrawer`（主报告结构化 / 其余原始 JSON 降级） | P-2api |
| `/api/v1/governance/snapshots` | GET | Bearer | 治理 | `SnapshotsTable`（`is_latest_by_created_at` 徽标） | P-2api |

**治理页挂载即发 8 请求**（0022 决策 ⑤ 的"8"就是这个数），2 条钻取由点击触发。
前端**不得**为"减少请求数"而合并或懒加载掉这 8 条中的任何一条：0022 决策 ⑥ 的
240/min 治理桶正是按"8 请求 + 平均 2 次钻取 ≈ 10 请求/次导航"算出来的，改请求
数即改该推导的前提。

### 3.2 统一信封与 TS 类型骨架

治理端点的信封由 0022 决策 ⑤ 锁定为 4 键。TS 侧对应：

```ts
// frontend/src/api/types.ts —— 键集必须与 0022 判据 1/7 的断言逐字对齐
export interface Envelope<K extends string, T> {
  kind: K;              // 如 "governance.models"
  count: number;        // == items.length，前端不得自行计算后覆盖
  sources: string[];    // Git 文件路径，面板必须显示（诚实性：可溯源到唯一事实源）
  items: T[];
}
```

`sources` 的渲染义务：每个治理子页的页头必须显示"数据来自 N 个 Git 文件"并可展开
路径列表。**不得**把 `sources` 只在 hover tooltip 里给——0022 决策 ⑤ 的理由是
"治理面的诚实性要求数据可溯源到唯一事实源，而不是『服务端说的』"，藏进 tooltip
等于弱化该理由。

业务面（`/ask` `/plan/execute`）**不用信封**，直接是 `_turn_payload` 的平铺 21 键
（P-1 后；当前 19 键）。两套形态并存是既有事实，前端不得为"统一"而在客户端包一层
信封——那会制造第三个事实源。

### 3.3 六类诚实性标志位的逐条渲染义务

0022 决策 ⑦ 把判别信息放进契约，使前端"无法忘记"展示。本页给出**每条标志位对应的
组件与文案**（文案是渲染义务的一部分，改文案即改诚实性表述）：

| 标志位 | 实测值（2026-09-14） | 组件 | 必须出现的文案 | 禁止的呈现 |
|---|---|---|---|---|
| `values[].status == "skipped"` + `skip_reason` | 20 份中 11 份 `values: []`，`skip_reason` 形如 `distinct=2715 超过阈值 200` | `ValuesTable` 分两组（已覆盖 9 / 已跳过 11），跳过组默认**展开** | 「值域未采集：<skip_reason 原文>」 | 平铺 20 条、把空数组显示为"0 个值"、把跳过组折叠进"高级" |
| `reports[].structured == false` + `pattern` | 48 份 = 12 主报告 + 36 份 / 18 种模式 | `ReportsTable` 的 `pattern` 列 + `ReportDrawer` 降级分支 | 「非统一结构报告（模式：<pattern>），以下为原始 JSON」 | 伪造统一表头、把 `raw` 摊平成表格列 |
| `reports[].dry == true` | 主报告 7 键含 `dry` | `ReportsTable` 的 `dry` 标签列 | 「dry 运行：未连 DB，EX 不参与统计」 | 把 `ex: "n/a"` 渲染为 `0` 或 `0%` |
| `synonyms.empty_placeholder == true` + `authority_note` | `zh_cn.yml` 实测 `metric_synonyms: {}` / `dimension_synonyms: {}` | `SynonymsPanel` 顶部横幅 | 直接引用 `authority_note` 原文（权威源在 ossie `ai_context.synonyms`） | 显示"中文暂无同义词"（那是错的：有，只是不在这个文件） |
| `policies[].registered == false` | `broker` 当前为 `false`（P-2sec 后**必须反转为 true**） | `RoleSwitcher` 的角色项禁用态 + `PoliciesTable` 的注册列 | 「角色未注册，无法签发 token」 | 让未注册角色出现在可选列表里（会在点击后 500） |
| `snapshots[].is_latest_by_created_at` | 字典序最大 `dc4f350`（2026-09-04）≠ `created_at` 最新 `a11d779`（2026-09-09） | `SnapshotsTable` 的"最新"徽标 + `SnapshotBadge` | 「最新（按 created_at）」 | 按文件名排序后给首行加"最新"徽标——正是 0019 背景里那类错误 |

**注**：`broker` 一条在 P-2sec 落地后会变成 `registered: true`，0022 判据 7 已要求
"同批修改，不得留着变成假绿"。前端侧同理：`RoleSwitcher` 的禁用态**必须由字段驱动**
而不是硬编码 `broker` 字符串，否则 P-2sec 落地后界面继续禁用一个已可用的角色。

### 3.4 四态渲染分支与字段全集

响应字段全集（实测）：

- `_turn_payload`（`serving/api.py` 的 `_turn_payload()`，**不写行号区间**——函数会
  增长，撰写时实测在 `:183-213`，0019 工作项 6 落地后已漂到 `:193` 起）
  基线 **19 键**：`kind` `session_id`
  `question` `turns_in_session` `metric` `sql` `columns` `rows` `row_count`
  `latency_ms` `engine` `path` `usage` `validation_issues` `explanation`
  `clarification` `block_reason` `error` `handoff_reason`；
- P-1（0019）后 **+2 键** → **21 键**：`snapshot_sha`、`snapshot_bound_to_head`；
  **已落地并实测（2026-09-14 工作项 6）**：`/ask` 的 answer 与 clarify 两条轮次均回显，
  未绑定 agent 时两键为 `null` 而非缺失（键集恒定由 `tests/test_identity_echo.py` 锁，
  不是靠这段文字保证）；
- `explanation`（`agent/graph.py` 的 `node_explain()`，撰写时实测 `:383-400`；
  2026-09-14 复测该函数起于 `:380`——**同一漂移原因，行号只作历史坐标**）
  **13 个固定键**：`metric`
  `metric_expression` `dimensions` `time` `filters` `sql` `tables` `row_count`
  `latency_ms` `path` `engine` `data_version` `data_refreshed_at`；**+1 条件键**
  `policy_effect`（仅策略生效轮追加，`graph.py:403-405`）；
- `chart` 键**不存在**（`grep -n chart serving/api.py agent/graph.py agent/state.py`
  = 0 命中）→ 已由 **ADR-0025 决策 ②** 裁定为独立 `chart` 键（P3 落地）：
  **+1 键**，故本页所有键数公式再顺延一档（19 → **20**；0019 落地后 21 → **22**，
  0025 判据 7 的断言公式同源）。**注意区分两层可空性**：`chart` 键本身恒存
  （`kind != "answer"` 时为 `null` 而非缺失，0025 判据 7），故 TS 侧写
  `ChartSpec | null`；而 spec **内部**的 `note` 在 `bar`/`line` 分支是条件键
  （实测 `chart.py:241-242`），故写 `note?: string`——两者不矛盾，见 §6.1。

TS 侧必须把"kind 无关字段为 null"这一既有契约表达为**可空类型而非可选类型**：

```ts
// 19/21 键是"字段全集稳定输出"（api.py:184 docstring 原文），
// 故用 `| null` 不用 `?:`——可选类型会让 `payload.sql` 的 undefined 与 null
// 两种形态在组件里各写一遍分支，而服务端只会发 null。
export interface TurnPayload {
  kind: "answer" | "clarify" | "blocked" | "error" | "handoff";
  sql: string | null;
  rows: unknown[][] | null;
  // …其余键同构，逐键与 _turn_payload 对齐
}
```

四态分支（`kind`）与渲染顺序（0018 ⑦ 锁定）：

| `kind` | 视觉分支 | 渲染内容（顺序即 0018 ⑦） | 沿用的 CLI 文案源 |
|---|---|---|---|
| `answer` | 主分支 | ① 指标口径（`explanation.metric` + `metric_expression` + `dimensions`/`time`/`filters`）→ ② 行数/耗时（`row_count` + `latency_ms`）→ ③ 出口 SQL（`sql`，可折叠但**默认展开**）→ ④ 数据（`columns` + `rows`）→ ⑤ 截断声明 → ⑥ 图表（P3，消费独立 `chart` 键，0025 决策 ②；`chart === null` 时**不渲染占位也不推断原因**，只能照实展示后端给的 `note`） | `agent/cli.py` 的 `[answer] 指标=X \| 行数=Y \| 执行 Zms` |
| `clarify` | 反问分支 | `clarification` 全文 + 可点击的候选项（点击即回填输入框重发） | `cli.py` 的 clarify 输出 |
| `blocked` | 安全分支 | `block_reason` 原文 + `validation_issues` 逐条 | `cli.py` 的 blocked 输出 |
| `error` | 错误分支 | `error` 原文，**不美化不摘要**（N4 同源纪律） | `cli.py` 的 error 输出 |
| `handoff` | 降级分支 | `handoff_reason` + `path: "candidate"` 的候选链痕迹 | `cli.py` 的 handoff 输出 |

**⑤ 截断声明的形态（本页初稿在此出错，已按实测重写）**

先厘清四个**互不相同**的 500/上限（实测 2026-09-14）：

| 位置 | 值 | 作用域 |
|---|---|---|
| `agent/cli.py:257-258` | 字面量 `500` | **CLI 显示层**截断，不影响数据；完整数据走 `--format json`（`cli.py:429`） |
| `agent/tools/chart.py:37` `MAX_TABLE_ROWS` | `500` | **chart spec 的 table 降级上限**（`:174/:186` 切片，`:178/:189` 给 `skipped` 计数）——**只约束表格，不约束图表点数** |
| `agent/tools/chart.py:35` `MAX_CATEGORIES` | `200` | **图表点数上限**（判定点 `:170`：`len(rows) > 200` 直接降级为 `table`）——ADR-0025 决策 ④ 据此裁定「无需新增点数上限」，且使图表 `data` 恒 ≤ 200 点 |
| `serving/api.py` | **无任何行数上限** | `grep -n "500\|MAX_" serving/api.py` 仅命中 `:92` 的 `question` 字符长度限制 |

故 **HTTP 响应不截断**，行数上限来自 Plan 本身：`Plan.limit` 默认 **100**
（`agent/compiler.py:100`）且编译器总是发出 `LIMIT`（`:778`，对比态的 CTE 形态
`:1158/:1180` 同样带）；`CompileBody.limit` 的 Pydantic 上限是 **`le=10_000`**
（`api.py:126`）。`QuestionBody` 只有 `question` + `model`（`api.py:91-93`），**无
limit 字段**，故 `/ask` 的行数由 Planner 产出的 `Plan.limit` 决定（缺省 100，
TopN 问句除外）；而 `/plan/execute` 可直推 10 000 行。

由此得出前端的真实义务：

- 表格渲染上限是**前端自己的常量**，建议沿用 500 以与 CLI 视觉一致，但文案
  **不得声称它是后端上限**（它不是）；
- 文案必须区分两件事：**已加载多少行**（= `row_count`）与**渲染了多少行**。
  建议：「已加载全部 <row_count> 行，表格仅渲染前 500 行（前端渲染上限）」；
- 当 `row_count` == `limit` 时必须额外提示「结果可能被 Plan 的 `limit` 截断（当前
  = <limit>）」——否则用户会把「恰好 100 行」读成「全量就是 100 行」，这是与
  §3.3 六类标志位同源的诚实性陷阱，但**当前契约未携带该标志**（无
  `truncated_by_limit` 字段）→ §6.5；
- 完整数据的出口只能是 `atlas query --format json`（**同一只读网关**）或收窄
  Plan 的 `limit` 后重发。**不得**写成"点击加载全部"（前端加载不了更多），更
  **不得**引导用户"复制 SQL 到只读网关外执行"——那是直接违反 N3（本页初稿曾
  如此建议，已删）。

**快照徽标常驻**（0018 ⑦ 的"绑定快照 sha 常驻可见"）：顶栏固定显示
`snapshot_sha` 与 `snapshot_bound_to_head`，4 种取值各有文案：

| `snapshot_source`（0019 决策 ①） | `bound_to_head` | 徽标文案 |
|---|---|---|
| `env` | — | 「快照 <sha>（环境变量指定）」 |
| `head` | true | 「快照 <sha>（与 HEAD 绑定）」 |
| `latest` | false | 「快照 <sha>（**未绑定 HEAD**，取自最新已锁）」——必须警示色 |
| `health.status != "ok"`（degraded） | — | 「快照不可用（/api/v1/health 报 degraded）」——error 色，**不得**显示"数据加载中" |

> **实测纠错（2026-09-16，P1 落地时）**：上表第 4 行原写「服务 503，前端显示 0019
> 的错误原文」，与实现不符——`/health` 在快照不可用时**恒 200**，走
> `status="degraded"` + 快照 5 键全 null 的降级分支（`serving/api.py` 的
> `except SnapshotUnavailable`，键集与 ok 路径恒同）；503 只出现在**业务端点**
> 构造 agent 绑定快照失败时（如 `/ask`），不是 `/health` 的形态。故前端按
> 「状态事实」渲染（报出 `status` 值、error 色），仍满足 0019 决策 ⑥ 的
> 「不得显示"数据加载中"」。另有两类**契约外组合的防御态**同走 error 色且
> 禁用绑定声明：`ok` 但 sha/source 为空、`source` 与 `bound` 对不上（`sha.ts`
> 宁可报异常也不猜「与 HEAD 绑定」）。

### 3.5 角色切换器

矩阵由 0021 决策 ⑤ 裁定为 6 角色 × 2 域，**前端不得硬编码该矩阵**：

- 角色清单走 `GET /api/v1/governance/policies` 的 `roles[]` + `registered`；
- claims 表单走同一响应的 `required_claims`（标量）与 `list_claims`（列表）；
- 按当前域过滤：域来自工作台的 `model` 选择（`finance | retail`，0022 决策 ⑤ 的
  `_model_name` 白名单）。

三条硬约束：

1. **切换角色必须换 `session_id`**（0021 决策 ⑤ 末段 + 0020 决策 ⑥ 的身份指纹跨
   重启校验）。UI 上表现为：切角色即开新会话，**不允许**在同一会话中途换身份；
   若用户尝试，前端必须显式提示"切换角色将开启新会话"而不是静默新建。
2. **`category_analyst` 的列表 claims 只能走 HTTP JSON**（`agent/cli.py:_parse_role_ctx`
   不解析列表值，0021 决策 ⑤ 已录）。前端是**唯一**能 convenient 地使用该角色的入口，
   这一点要写进面板的帮助文本，否则用户会以为 CLI 坏了。
3. **token 只存内存态**（React state / module-scope 变量），**不落 `localStorage`
   也不落 cookie**。这与 0020 的"重启不失忆"形成**刻意的不对称**：会话状态持久化在
   服务端 SQLite（刷新页面续接），凭据不持久化在客户端（刷新页面需重新签发）。
   理由是 N9 的延伸——构建产物与浏览器存储都可能被复制/同步，而 dev token 虽非
   生产密钥，一旦落盘就会出现"别人的浏览器里有我的 token"这类难以归因的状态。
   代价：刷新页面后需重新点一次角色（可接受，dev 场景）。

   > **P2 落地注记（2026-09-16）**：上句「点一次」在落地时**收窄为「重新粘贴一次」**。
   > P2 收口复验实测：治理端点**一律要求 Bearer**（无 token 请求
   > `/api/v1/governance/policies`，直连 `127.0.0.1:8000` 与经 vite 代理**均 401**）——
   > 而角色矩阵（`policies.roles[]` 与签发所需的 `required_claims`）恰恰只存在于该响应，
   > 即刷新后 RoleSwitcher **取不到矩阵，也就无法就地签发**。故刷新后的激活路径固定为：
   > 先经顶栏粘贴一次 `make token` 输出，token 到位后矩阵才可见、角色才可就地切换。
   > 这与本约束的理由同源（凭据不落客户端 → 刷新即失凭据 → 先凭据后矩阵），属预期代价；
   > 偏差已登记 ADR-0018「落地注记：P2 批次」第 3 节，README KL #34 第 ② 条。

dev 签发路径：`make token ROLE=<role> CONTEXT='<json>'`（`Makefile:140-141`，
调 `serving.auth.sign_token`）。前端**不得**内置任何签发逻辑或密钥（0018 约束 N9）；
dev 便利由 vite middleware 承担（0018 推翻条件第 2 条已裁定：出现多用户/真实鉴权
需求时该 middleware 必须废除）。

---

## 4. `frontend/` 目录结构与仓库侧改动

### 4.1 文件级结构（每个文件的存在理由）

```text
frontend/
├── .gitignore                 # 局部忽略 node_modules/dist/coverage（0018 ④；不改根 .gitignore）
├── package.json               # private:true（0018 ④，避免 npm 侧许可证声明与 0023 打架）
├── package-lock.json          # 入库（0018 ④，沿 remotion 惯例）；pre-commit 需 exclude large-file
├── tsconfig.json              # 沿 remotion 严格度：strict/target ES2020/module ESNext/
│                              #   moduleResolution Bundler/jsx react-jsx/skipLibCheck
├── vite.config.ts             # server.proxy + 双档 target；文件头必须有"禁 localhost"注释（0018 ③）
├── index.html                 # SPA 入口
└── src/
    ├── main.tsx               # AntD ConfigProvider（中文 locale）；BrowserRouter 属 P2（P1 无路由）
    ├── App.tsx                # 顶栏（SnapshotBadge；token 粘贴为 P1 临时通道）+ 工作台；
    │                          #   路由表与 RoleSwitcher 属 P2
    ├── api/
    │   ├── endpoints.ts       # 路径常量唯一出口 —— 0022 判据 9 的正则提取对象（§1.1）；
    │   │                      #   含 PROBE_HEALTH = "/health"（前端不调用，仅为集合相等）
    │   ├── client.ts          # fetch 封装：Bearer 注入、429 的 Retry-After 处理、错误原文透传
    │   └── types.ts           # Envelope / TurnPayload / 各治理 item 类型（§3.2、§3.4）
    ├── lib/
    │   ├── honesty.ts         # 纯函数：skipped 分组、structured 分组、dry 判定、
    │   │                      #   empty_placeholder 判定、is_latest 判定 —— vitest 断言对象（0018 判据 10）
    │   ├── order.ts           # 纯函数：渲染顺序（0018 ⑦）与四态分支判定 —— vitest 断言对象（0018 判据 9）
    │   └── sha.ts             # 纯函数：sha 展示格式（短 sha + 绑定态文案，§3.4 表格）
    ├── state/
    │   ├── role.ts            # 角色 + token（内存态，§3.5 约束 3）
    │   └── session.ts         # session_id 生成与"切角色即换会话"（§3.5 约束 1）
    ├── components/            # P1 落地新增（原树未预留）：跨面板共用件
    │   └── SnapshotBadge.tsx  # 顶栏快照徽标（消费 /health 的 4 键，§3.4 表格）
    ├── panels/
    │   ├── workbench/         # 面板 1：AskWorkbench / PlanPreview / SqlPreview /
    │   │                      #   DataTable / TruncationNote / ExplanationBlock
    │   ├── sessions/          # 面板 3：SessionPanel（§2 已登记其数据源缺口）
    │   ├── chart/             # 面板 5：spec → Recharts 的确定性映射（**阻塞于 P-1**，0025 决策 ⑤）
    │   ├── governance/        # 面板 4：8 个子页 + 2 个 Drawer
    │   └── role/              # 面板 2：RoleSwitcher（顶栏组件区）
    └── __tests__/             # vitest：只测 lib/ 与 state/ 的纯函数，不测渲染快照（0018 判据 9/10）
```

三条结构性纪律：

- **`lib/` 必须是纯函数**（无 React、无 fetch）。0018 判据 9/10 明确"由 vitest 对
  纯函数断言（不依赖渲染快照）"，把诚实性判定与顺序判定抽成纯函数是唯一能让该判据
  可执行的结构；
- **组件不得直接写路径字符串**，一律从 `api/endpoints.ts` 导入。这是 0022 判据 9
  防漂移的前提（若有第二处硬写路径，正则提取就漏）；
- **`panels/chart/` 在 P-1 落地前不建目录**（0025 决策 ⑤：P-1 前只落契约测试不落 UI）。
  建了空目录会诱导"先写起来"，而图表类型决策属后端 `chart.py`（0018 ⑦、
  0025 决策 ①），在前端先写等于把决策点搬错位置。

**P1 落地注记（2026-09-16，两处结构偏差登记）**：① 新增 `components/` 目录（原树
未预留）——顶栏 `SnapshotBadge` 是四个消费面板的共用件，放 `panels/` 任一子目录
都会造成跨面板反向依赖；该目录内组件只接收 props、不 fetch（数据获取留在各区
容器组件），判定逻辑仍在 `lib/sha.ts`；② `main.tsx` / `App.tsx` 的 P2 组成
（BrowserRouter、路由表、RoleSwitcher）未落，P1 为单面板无路由形态。本批已落：
`api/` 三件、`lib/` 三件、`state/session.ts`、`panels/workbench/` 六件、
`components/SnapshotBadge.tsx`；`state/role.ts` 与其余 `panels/` 子目录属 P2/P3。
判据 9/10 的 vitest 断言实装于 `__tests__/`（6 文件 36 用例，含 P0b 的
endpoints/app 两文件）。

**P2 落地注记（2026-09-16，三处结构补记）**：P1 注记遗留的「`state/role.ts` 与其余
`panels/` 子目录属 P2/P3」已在 P2 落定——本批新增 `state/role.ts`、`panels/role/`
（`RoleSwitcher`）、`panels/sessions/`（`SessionPanel`，含「仅当前运行」限制横幅）、
`panels/governance/`（六子页 + `GovernanceSection` / `GovernanceLayout` / `ValuesDrawer`
共 9 文件；`reports` / `snapshots` 子页与 `ReportDrawer` 属 P3）、`api/devsign.ts`、
`components/CopyBlock.tsx` / `ErrorNote.tsx`；`main.tsx` / `App.tsx` 的 P2 组成
（BrowserRouter、路由表、全局 `domain` / `identity` / `sessionState` 单源、
`AskWorkbench` 受控化）已落。三点补记：① **Drawer 2 → 1**：原树「8 个子页 + 2 个
Drawer」在本批实落 `ValuesDrawer` 一件（挂 values 子页内、非全局）——`ReportDrawer`
依赖 P3 的 `reports` 子页数据源，2 件属终态口径；② **依赖引入**：`react-router-dom`
7.18.4（MIT）——兑现 P1 回执偏差 ⑤ 的「若 P2 需要则补登记」，版本与许可证实证登记于
ADR-0018 注记 P2 批次第 2 节；③ **`vite.config.ts` 职责扩展**：新增 dev-only
`POST /__dev/sign` 中间件（`spawn uv run --env-file <repo>/.env` 调
`serving.auth.sign_token`，与 `make token` 逐字同构，**vite 进程不读密钥**）——兑现
§3.5「dev 便利由 vite middleware 承担」的预设计，仅 `apply:"serve"` 生效，四态实测
（200/405/400/400）见 ADR-0018 注记 P2 批次第 3 节。判据 10 断言面扩至 `__tests__/`
8 文件 60 用例（新增 `role.test.ts` 19 / `no-persist.test.ts` 1；`honesty.test.ts`
14→15、`session.test.ts` 2→5）。

**P3 落地注记（2026-09-16，树终态补记）**：P1/P2 注记遗留项全部落定——①
`panels/chart/` 建成（`ChartBlock.tsx`；纪律 3 的「P-1 前不建目录」以 P-1 落地为
前提达成，本批为终态交付）；② `panels/governance/` 补齐终态 8 子页 + 2 个 Drawer
（新增 `ReportsSection.tsx` / `SnapshotsSection.tsx` / `ReportDrawer.tsx`，原树
「8 个子页 + 2 个 Drawer」口径兑现）；③ 新增 `lib/chart.ts` 与 `lib/report.ts`
（原树未预留，沿 `lib/` 纯函数纪律：零推断映射与降级展示判定均为无 React、无
fetch 的纯函数，vitest 断言对象）；`__tests__/` 扩至 10 文件 82 用例（新增
`chart.test.ts` 11 / `report.test.ts` 9；`honesty.test.ts` 15→17、`order.test.ts`
补 chart 段断言）。判据 9/10 的断言面至此覆盖全部五面板（经 `lib/` / `state/`
纯函数）。

### 4.2 仓库侧改动清单（P0b）

| 文件 | 改动 | 依据 | 备注 |
|---|---|---|---|
| `Makefile` | 新增 `NODE_BIN` 探测 + `export PATH`；新增 `ui-check` / `ui-dev` / `ui-build` / `serve-dev` 四目标 | 0018 ③⑥ | 实测当前 0 命中；探测不到 node 必须 `exit 1` 并打印指引，**不静默跳过** |
| `Makefile` | `.PHONY` 同步增 4 项 | 既有惯例 | 实测口径（2026-09-14 复测）：`Makefile:1` 单行 `.PHONY` 声明 **43** 项；`grep -cE "^[a-zA-Z0-9_.-]+:" Makefile` = **44** 行 = `.PHONY:` 本身 + **43 个真实目标**（无变量行命中），**差集已归零**（见下行）。**0018:16 写的"Makefile（36 目标）"取的是 `.PHONY` 口径且已过期**，本页沿用该口径但更新基数：P0b 后 `.PHONY` = **47**、真实目标 = **47**（2026-09-16 复测更正：P0b 落定后实测 **48 / 48**——本页预测漏计 P0a 期新增的 `license-check` 目标，P0b 开工基线实为 44/44；纠错记录见 dev-plan §2.5 回执） |
| `Makefile`（附带发现，**已修**） | 本页初稿登记为"不修，仅登记"；用户 2026-09-14 已批准当轮修正，**7 个已有目标已补进 `.PHONY`** | — | 原实测差集（43 真实目标 − 36 `.PHONY`）= `baseline` `compare` `demo` `lora-infer` `query` `rag-eval` `schema-link`。修正前仓内无同名文件/目录，故行为无差异；但一旦新建同名目录（如 `demo/`）该目标会被 make 当作"已最新"而跳过。修正后实测（`comm -23` 双向差集）：`.PHONY` 43 项 = 真实目标 43 项，**零重复、零孤儿声明**；属 `chore` 且与前端无关，**不得混进 P0b 提交**（AGENTS.md §8） |
| `.pre-commit-config.yaml` | `check-added-large-files` 增 `exclude: ^frontend/package-lock\.json$` | 0018 ⑥ | **只 exclude 该文件，不抬 maxkb**（抬高会削弱二进制误提交防线） |
| `.dockerignore` | 增 `frontend/node_modules`、`frontend/dist` | 0018 ④ | docker 不读 `.gitignore` 也不读嵌套的 `frontend/.gitignore`；Dockerfile 是 `COPY . .` |
| `infra/docker/api/Dockerfile` | 增 `node:20-alpine` 阶段跑 `npm ci && npm run build`，`COPY --from` 进 `python:3.11-slim`；统一 `GIT_SHA` 默认值并删掉撒谎注释 | 0018 ⑤ | 实测（2026-09-14 复测行号）：`Dockerfile:33` `ARG GIT_SHA=b933e20` 与 `:11` 注释自称"最新 29 表全量数据版本"；`docker-compose.yml:211` 生效值 `7d48dcb` 与 `:198-199` 注释自称"b933e20（data/snapshots/ 最新锁定 meta）"；而 `data/snapshots/` 实测 17 份 meta 中按 `created_at` 最新是 `a11d779`、字典序最大是 `dc4f350`。compose `args` 覆盖 `ARG`，故实际生效 `7d48dcb` |
| `docker-compose.yml:225` | **不改端口映射**，但其注释是 0018 ③ 双档设计的仓内旁证 | 0018 ③ | 实测原文：`- "8010:8000" # 宿主机 8000 被用户资产配置服务占用，映射到 8010`——与 0018 ③ 实测的 `curl localhost:8000/health` 命中他人项目（IPv6 `::1`）互相印证：容器在 8001、宿主机 uvicorn 在 8000（IPv4）、`::1:8000` 是第三个服务 |
| `serving/api.py` | 条件挂载 `StaticFiles(frontend/dist)` + SPA catch-all fallback，**注册在 `/api/v1` 路由之后**；`dist` 不存在则跳过并打 warning | 0018 ②⑤ | 0018 判据 7 的对应断言：SPA 深层路径刷新返回 200 HTML，而 `GET /api/v1/<不存在路径>` 必须返回 **404 JSON** 而非 HTML 200 |
| `.github/workflows/ui.yml` | 照写（`paths: ['frontend/**']` + `setup-node@v4` + npm 缓存 + `npm ci` → `tsc --noEmit` → `vitest run` → `npm run build`） | 0018 ⑥ | **不作为验收依据**：实测 4 个 workflow 从未执行（remote 唯一为 gitee，无 `.workflow/`，`git ls-remote --tags` 空） |
| `.env.example` | 增 `ATLAS_API_TARGET` / `ATLAS_API_BASE` 两行（只列名不含值，N9），注明 8010 档"仅旧契约对照用" | 0018 ③ + 代价 ④ | 0022 判据 14 的两个限流变量在 P-2api 已增，本批不重复 |
| `AGENTS.md` §4 | 登记 `frontend/` 目录职责 | 0018 ④ + AGENTS.md §4 | **走 `contract` 提交，与 P0b 代码变更分开**（同 0023 判据 15 的处理方式） |
| `AGENTS.md` §5 | 技术栈锁定表增前端行，并把标题措辞辨析为"数据与后端基础设施 Apache 全栈，前端为 permissive 许可的社区栈" | 0018 代价 ① | 否则 §5 标题「Apache 全栈」本身成为不实声明 |
| `README.md` | **§3.1 前置条件**依赖表扩 node ≥ 20 一行（实测现为 6 行：Docker / Python / uv 或 pip / Git / 内存 / 显卡）+ `make ui-build`；三处 CI 声称改为"当前远程为 gitee，GitHub Actions 不执行；执行点为本地 make + pre-commit"；KL 增 §7 的 3 条（编号 **#32/#33/#34** 仅为草案标识，**实际编号按写入当日顺延、不得预留**，因 README KL #31 已被黄金集条目占用，且 P1 若因 G3 降级需写 KL 会插在 #32 与 #33 之间，见开发计划 §2.6） | 0018 ⑥ + 代价 ②⑤ | 实测三处（`grep -n "CI 经\|GitHub Actions" README.md`）：`:179` 行末与 `:320` 均为「CI 经 `make lint` 自动执行」、`:463` 目录树注「GitHub Actions（lint / eval 回归 / tag 自动版本锚点）」（三行号已于 2026-09-14 复测；本页初稿记的 `:316`/`:458` 已因 README 增行漂移 +4/+5）——**第三处是 0018 ⑥ 未列举的新发现**，与 `AGENTS.md §4` 同一行文本，同批改。`:191` 自身已诚实登记「部署目标 GitHub Actions」，**不改** |
| `docker-compose.yml:230-231` | **不改** | 0022 决策 ① + 0018 落地注记 1 | 见 §1.3 歧义 3 |

---

## 5. 逐批复验表

每批的入口命令、判据引用与**允许的失败形态**。最后一列是纪律：skip 必须显式标注，
**不得冒充通过**（AGENTS.md §9 同源）。

| 批次 | 入口命令 | 判据引用 | 关键复验断言 | 允许的失败形态 |
|---|---|---|---|---|
| P-1 | `make lint && make test && make eval && make api-verify` | 0017 1~7、0019 1~13 + 文档 2 条（判据数随落地逐批扩充，原为 1~9；区间权威见 dev-plan §0 起点表）、0020 1~14 | `curl 127.0.0.1:8000/health` 返回 8 键且 `snapshot_source` ∈ {env,head,latest}；`_turn_payload` = 21 键。**两条按需不需要 Doris 分档（2026-09-14）**：前者**已真机验通**（`/health` 不构造 agent，`make serve` + `curl` 即得 8 键与预期取值，见 0019 真链判据 7 落地注）；后者**已真链验通（2026-09-16 收口）**：`/ask` 双域返回 `kind="answer"` 后取到 21 键（见 0019 判据 7 收口落地注）——**两条均已验通** | 0020 判据 1 若发现非 permissive 许可证 → **必须停批**并触发推翻条件第 3 条，不得"先开工后补" |
| P-2sec | `make lint && make test && make rls-verify` | 0021 1~14 | `sign_token("broker", {"brokerid": 1})` 成功；`ROLE_DIRECTORY` = 6 键；`roles_for_policy("rp_dept_visible")` = 3 角色 | 0021 代价 ④ 已登记：SF0.1 的 brokerid 基数**未实测**，若"broker 行数 < hq_admin 行数"不成立，按 `rls_verify.py:24-26` 先例**如实报告而非伪造差异** |
| P-2api | `make test && make api-verify` | 0022 1~14 | `len(app.openapi()["paths"]) == 16`；治理桶打满 240 后业务面仍 200；快照缺失时治理端点仍 200；6 类标志位逐条断言 | 0022 证伪条件：若判据 5 失败（治理面 503）说明隐式触发了 Agent 构造 → 决策 ④ 隔离前提被推翻，需改为显式文件读取并重新实测 |
| P0a | `make license-check && make lint && make test && make eval` | 0023 1~15 | `head -1 LICENSE` 含 `Apache License`；`NOTICE` 存在；`git grep psycopg -- "*.py"` = 0；**`git diff eval/gold/` 为空**（全部锚定样本逐条 hash 不变；分母按 0023 决策 ④ 的公式当日重测，撰写时实测 84、P-1 落地后预期 **97**——本页初稿误写 99，纠正过程与五类归属见 §1.3 歧义 4） | 0023 证伪条件：锚定样本中任一条 hash 变化且无法归因于数据变更 → 回退 PyMySQL 迁移，保留 GPLv2 + Universal FOSS Exception 并落 3 项持续义务 |
| P0b | `sh -c 'make ui-check'`（**非交互 shell**） | 0018 1~5 | 5 个 `node_modules/*/LICENSE*` 已回填 0018 决策 ① 表格的 ⬜ → ✅；`git check-ignore -v frontend/node_modules frontend/dist` 命中局部文件；`.dockerignore` 含两条；移除 node 后目标 `exit 1` 不静默跳过 | 若某个 npm 包实测为非 permissive → 触发 0018 推翻条件第 4 条，优先替换图表库（Recharts 替换面小），AntD 需单独立项 |
| P1 | `make ui-build && make serve-dev` + 浏览器 | 0018 6~9 | vite proxy 命中 `127.0.0.1:8000`（用 `/api/v1/health` 的 `head_sha` 断言，不是 ::1 的他人服务）；SPA 深层路径刷新 200 HTML 且 `/api/v1/<不存在>` 404 JSON；Network 面板零 CORS 预检。**三条均已验通（2026-09-16 收口）**：direct vs proxied 的 `/api/v1/health` JSON 逐键相等且提交后复验 `head_sha` 随 HEAD 即时更新（`05cb9ff`）；深层路径浏览器侧复核与 `tests/test_spa_static.py` 12 例双覆盖（`/governance/metrics` 200 HTML、`/api/v1/definitely-not-a-route` 404 JSON）；0 个 OPTIONS、响应无 `access-control-*` 头（服务端无 CORS 中间件）。判据 9 由 vitest 承担（36 用例/6 文件全绿）。证据见 0018 注记 P1 批次第 2~5 节 | 0018 证伪条件：若判据 6~8 任一不可调和 → 决策 ② 同源假设被推翻，回退 dev proxy + prod 独立静态服务器 + 显式 CORS |
| P2 | `make ui-check` + 手工走查 6 治理子页 + 角色切换 | 0018 10 + 本页 §3.3 六条渲染义务 + §3.5 三条硬约束 | 值域跳过组默认展开且显示 `skip_reason` 原文；切角色即换 `session_id`；token 刷新页面后消失（内存态验证）。**三条均已验通（2026-09-16 收口）**：Browser 走查 12 步全过——值域 covered/skipped 两组默认展开且 `skip_reason` 原文可见（「distinct=2715 超过阈值 200」）；切角色后当前会话 id `0bd79a6c` → `5ebd97b2`（/sessions 同步列出）；刷新后顶栏变回「未认证 · 激活身份」、`/governance/models` 显示 401 引导，console 零报错。判据 10 由 vitest 承担（8 文件 60 用例全绿，build 1505 modules）。证据见 0018 注记 P2 批次第 3~5 节 | 会话时间线的跨重启历史**无数据源**（§2 面板 3）→ 允许只展示当前运行的会话，但必须在面板上写明该限制（§7 KL #34——实测取号：原草案 #33，因 P1 已占用顺延，见 §7 编号更新） |
| P3 | `make ui-check` + 手工走查图表与 2 个降级子页 | 0018 10 + **0025 判据 1~8** | 图表 spec 的 `type/x/y` 全部来自后端，前端零图表类型决策（TS 侧必须是 discriminated union，0018 纠错框 `:229`：`bar`/`line` 有 `data` 无 `columns`/`rows`，`table` 反之）；时间列由 `Compiler.emitted_time_column` 单一出口声明（0025 判据 1~3）；`_turn_payload` 键数按 0025 判据 7 的公式断言（19+1=20，0019 落地后 21+1=22）；36 份非主报告按"原始 JSON + 模式标签"降级。**已验通（2026-09-16 收口）**：判据 1/3/5 由 pytest 承担——10 组合逐条断言（`plain`/`rank` 双 `None`）、10 条黄金字面量零漂移、`git diff tests/test_chart.py` 删行 = 0（16 既有逐字未改，现 22 = 16 + 6）；判据 2/4/7/8 与走查见 dev-plan §2.8 回执（判据 4 真链：`emitted_time_column='d_year'` 与结果列逐字命中 = True、`chart.type='line'`，**未触发停批条件**）。浏览器走查实测：图表 line 渲染（两轴刻度完整）、reports **58 条（14 主 + 44 非主）**两级渲染 + 降级 Drawer、snapshots 19 条（最新徽标仅首行）——本行撰写时写「36 份非主报告」与实测不符（判定口径为 `_MAIN_REPORT_RE` 文件名正则，实测 44）。证据见 0018 注记 P3 批次第 1~4 节 | **0025 决策 ⑤：硬阻塞于 P-1**——P-1（0017 判据 1~4）未落地则同比/环比/累计过不了 Guard，图表只能拿到 plain/rank 两形态，先交付 UI 即构成对"支持时间序列图表"的隐性 N2 声称 |

---

## 6. 裁定摘要与未裁定项

§6.1 已由 **ADR-0025** 裁定（本节只留取证与摘要）；§6.3 已由 0018 落地注记
（「P1 批次」第 1 节，2026-09-16 经用户确认）解除；**§6.2 / §6.4 / §6.5
仍属未裁定项**，需新 ADR 或在对应 ADR 追加落地注记，**不得在本页拍板**。

### 6.1 图表接入与时间轴列事实源 → **已由 ADR-0025 裁定**（accepted，2026-09-14 用户确认）

**编号说明**：本页初稿建议的是 ADR-0024，但 `infra/adr/0024-cube-export-target-engine-gate.md`
（Cube 定位为导出目标，状态 **accepted**，2026-09-14 用户确认）已占用该编号，故本项顺延为
**ADR-0025**，已按 AGENTS.md §11 第 1 步用 `make adr` 建档并撰写完毕：

```bash
make adr TITLE="图表 spec 接入响应链与时间轴列事实源" SLUG="chart-spec-wiring"
ls infra/adr/0*.md | wc -l   # 建档后实测 25；0024/0025 于 2026-09-14 经用户确认转 accepted，故 25 篇全 accepted
```

本节以下**只保留缺口取证与裁定摘要**，不转述 ADR 正文（本页纪律 1）。

**缺口**（全部实测，2026-09-14；ADR-0025 背景 1~4 为完整取证）：

1. `render_chart`（`agent/tools/chart.py:127`）**未接进任何响应**：
   `grep -rn render_chart --include="*.py"` 只命中 `chart.py` 自身与
   `tests/test_chart.py`；`grep -n chart serving/api.py agent/graph.py agent/state.py`
   = 0 命中。即 spec 生成器存在、被 **16** 个用例覆盖（`grep -c "def test"`）、
   但**没有调用方**；
2. `_TIME_COLUMN_NAMES`（`chart.py:41-43`）是 6 项硬编码 frozenset
   （`CalendarYearID` `CalendarQtrID` `CalendarMonthID` `DateValue` `date` `time`），
   而零售域 `semantic/ossie/atlas_retail.ossie.yaml:83-90` 的 `time_dimension` 是
   `mode: composite` + `columns: {year: d_year, quarter: d_qoy, month: d_moy, date: d_date}`
   ——4 列全不在该 frozenset 内。**但这只是表层**：ADR-0025 背景 3 的双域 × 5 形态
   10 组合实测证明**金融域同样失效**，根因是 `compiler.py:991`（cumulative 为 `:1031`）
   的 `time_alias = time_col.lower()`——金融 `CalendarYearID` 被编译成结果列名
   `calendaryearid`，而 frozenset 是**精确大小写**成员判定（`chart.py:101`），永不命中；
3. 后果比"bar 而非 line"严重：comparison 形态下 y 轴变成**时间列本身**
   （如 `y=['d_year']`），真度量落进 `note` 文本——**画错量**而非画错图型，且静默；
4. `chart.py:39-40` 的注释自己已登记该债务：「零售结果集列名识别待实测扩展」；
   而 `chart.py:14-15` 的 docstring 声称「列名含 date/year/quarter/month 等时间词」，
   与实现的精确集合判定不符（ADR-0025 决策 ③ 第 2 点要求修正）。

**为什么不能在细化设计里拍板**：这不只是"补 4 个列名"。补硬编码会制造**第三套时间
列事实源**（ossie 的 `time_dimension.columns` 是权威源、planner 的粒度解析是第二处、
`chart.py` 的 frozenset 若继续扩就是第三处），与 0021 决策 ①「同一事实不得有两处
权威定义」和 0015 的双权威源禁令同源冲突。

**本页初稿 5 点骨架的裁定结果**（完整理由、代价与推翻条件见 ADR-0025 决策 ①~⑥）：

| # | 待裁定 | 本页初稿倾向 | **ADR-0025 裁定** |
|---|---|---|---|
| 1 | chart spec 挂在哪 | 独立 `chart` 键 | ✅ **采纳独立 `chart` 键**（决策 ②），理由与本页倾向同源：`explanation` 的 13 键被 0022 判据 6 断言，不得动。`_turn_payload` **+1 键**（19→20；0019 落地后→22）；渲染点定在 `turn_from_state`（`graph.py:502`）使 CLI 与 HTTP 共用单一调用点；spec **不进** `TurnState` |
| 2 | 时间轴列的识别源 | 读 ossie | ❌ **倾向被推翻**（决策 ①）。读 ossie 会失败：`time_dimension.columns` 给的是**物理列名**，而结果集列名是编译期 `.lower()` 后的**别名**，该规则只活在 `compiler.py:991`；消费者要么复制它（第四套副本）要么让生产者交出结果。裁定为**生产者携带**：`Compiler.emitted_time_column(plan) -> str \| None`，`.lower()` 收敛到单一调用点（`:991`/`:1031` 改为调用它），`node_execute` 返回字典加 `"time_column"`，`render_chart(execution, *, time_columns=())`。frozenset 降级为**兜底**（决策 ③，服务 `--llm` 候选链与 ad-hoc 执行），**不得加子串匹配**（会让 `update_date`/`years_held` 误判），且兜底命中时必须在 `note` 里声明 |
| 3 | composite 与 single 是否要不同的轴推断 | 未探查 | ✅ **已探查：不需要**（决策 ①）。两域差异在**别名生成**而非轴推断；`emitted_time_column` 对两种 `mode` 返回同一形态的 `str \| None`，`chart.py` 无需知道 `mode` |
| 4 | `MAX_TABLE_ROWS = 500` 是否同时约束图表点数 | 未裁定 | ✅ **无需新增上限**（决策 ④）。实测 `MAX_CATEGORIES = 200`（`chart.py:35`，判定点 `:170`）已使图表 `data` 恒 ≤ 200 点，超限直接降级为 `table`；`MAX_TABLE_ROWS` 只约束降级表格。本页初稿担心的"500 点折线"**不可达** |
| 5 | spec 是否进 checkpoint | 倾向不进 | ✅ **采纳不进**（决策 ②），且实测**无需动 `_CHECKPOINT_SERDE`**（`graph.py:100-107`）：该白名单只登记 4 个自定义类，纯 `dict` 无需注册；`TurnState.rows`（`state.py:55`）已含 `Decimal`，故 spec 可由 `rows` + `sql_sha256` 重算 |

**本页新增的两条前端级约束**（ADR 未展开，属本页职责）：

- **P-1 前不建 `panels/chart/` 目录、不写任何图表组件**（0025 决策 ⑤ 硬阻塞）。
  P-1 前只落后端契约测试（0025 判据 1~3、5、7）；
- **TS 侧 `ChartSpec` 必须是 discriminated union**（0018 纠错框 `:229`）：
  `bar`/`line` 有 `data` 无 `columns`/`rows`，`table` 反之；`x` 是 `string`
  （可能是 `"(行序)"` 字面量）而 `y` 是 `string[]`（已为列表形态但恒只 1 元，
  0025 决策 ⑥ 裁定**不扩多序列**，yoy/pop 只画当期）；`note` 在 `bar`/`line`
  分支是**条件键**（实测 `chart.py:241-242` 仅在 `note_parts` 非空时写入），
  TS 侧必须 `?:` 而非 `| null`；在 `table` 分支则是恒存键。

### 6.2 治理面分页与排序

0022 代价 ③ 已登记债务：治理面无分页（端点 2 返回 20 个指标全量、端点 7 返回
48 条索引全量），**一年后约 400 份报告**，届时需分页或按 `pattern` 过滤；0022
推翻条件也已写明触发点。本页**不提前设计分页**（AGENTS.md §10 第 5 条：不过度
设计），但登记前端侧的对应约束：`ReportsTable` 与 `MetricsTable` 的实现**不得**
内建客户端分页组件（AntD `Table` 的 `pagination` 默认开启），否则服务端加分页时
会出现"两套分页叠加"的口径混乱。默认关闭客户端分页，等 0022 债务到期时一并设计。

### 6.3 前端错误上报与 OTel 埋点 → **已解除**（0018 落地注记「P1 批次」第 1 节；2026-09-16 用户确认）

实测：`grep -n "OTel\|otel\|token_cost" infra/adr/0018-frontend-console.md
infra/adr/0022-http-contract-v2.md` = **0 命中**。而 AGENTS.md §11 第 6 步要求
新功能"加可观测：埋 OTel span，记录 token_cost"。即**前端批次的可观测口径无任何
裁定**：前端错误是否上报、上报到哪个端点（现有端点全为只读业务/治理面，无写入面）、
是否与 `observability/otel.py` 的既有 span 关联、治理页 8 请求的 trace 如何聚合
（0022 决策 ⑥ 的审计是每请求一行，8 行是否应属同一 trace）。

需新 ADR 或在 P1 开工前补 0018 落地注记。**在其裁定前，前端不得自建任何上报端点**
——那会在只读服务面上开出第一个写入面（N3 的邻接风险）。

> **解除记录（2026-09-16）**：裁定「前端零遥测」——不设上报端点（不在只读面上
> 开写入面）、不引入前端 OTel SDK、error 态如实渲染后端错误原文；治理页 8 请求
> 各独立、不聚合（与 0022 决策 ⑥ 审计行同构）。四条裁定的完整表述与推翻条件见
> `infra/adr/0018-frontend-console.md` 的「落地注记：P1 批次」第 1 节。

### 6.4 `make license-check` 的扫描域是否扩到 npm

见 §1.3 歧义 2。P0a 的自动检查只覆盖 Python 分发元数据；P0b 之后的 npm 依赖树
（实测 2026-09-16：直接 9 / 全树 186 包、lock 234 条目 / 115150 字节）只由 0018 判据 1 的人工回填覆盖。是否给
`infra/license_check.py` 增一个读 `frontend/package-lock.json` + `node_modules/*/LICENSE*`
的模式，属 0023 决策 ⑦ 的范围扩展，需在该 ADR 追加落地注记或新立 ADR，**不在本页决定**。

### 6.5 `row_count == limit` 的截断标志未进契约

§3.4 已实测：HTTP 响应不截断，行数上限完全由 `Plan.limit`（缺省 100）与
`CompileBody.limit`（`le=10_000`）决定，而 `_turn_payload` 的 19/21 键里
**无任何字段告知"结果是否被 limit 截断"**。后果：`row_count` 恰好等于 `limit` 时，
用户（与前端）无法区分"全量就是这么多"与"被截了"——这与 0022 决策 ⑦ 已裁定的
六类标志位是**同源的诚实性陷阱**，但未被覆盖。

前端可做的临时措施（不需求助后端）：`row_count === limit` 时展示**不确定语气**的
提示（「结果可能被 Plan 的 `limit` 截断」）而非断言。但这只是降级：真正修复应由
端点携带标志（例如 `explanation` 增 `limit_applied` 与 `row_count_at_limit`），属
**0022 契约变更**，需在该 ADR 追加落地注记或新立 ADR。本页**不拍板**，也不得在
前端把推测写成事实（N1/N2）。

> **P1 处置登记（2026-09-16 经用户确认）**：门禁 G3 以「接受降级 + 写 KL」解除——
> 前端按上段临时措施实现（`lib/honesty.ts` 的 `truncationState`：`row_count ===
> limit` 时显示不确定语气的 limit 提示；500 行渲染上限作为**确定事实**另行声明，
> 见 `renderNote`），降级记录进 README KL #33 第 ① 条。本节的**契约变更建议
> （`limit_applied` 等）仍未裁定**——本页不拍板的口径不变：确定标志落地前，前端
> 不得把「可能」改成「已截断」。

---

## 7. Known Limitations 增量草案

实测（2026-09-14 复测）：README `## 10. 已知限制`（`README.md:584`）当前最后一条是
**#31**（`:736`，黄金集计数口径纠正 + paraphrase 集无结构门槛，本批新增）。
故以下三条草案编号**顺延为 #32/#33/#34**（本页初稿写 #31/#32/#33，首条已被占用）。
以下为**草案**，随对应批次提交时写入；N4 要求**不得放宽或删除任何既有条目**，
故三条均为新增。

> **编号更新（2026-09-16，P1 收口时复测）**：`grep -nE "^[0-9]+\. \*\*" README.md
> | tail -1` 实测当前最大号 = **#32**（P0b 已写入，`README.md:887`）。P1 因门禁 G3
> 降级实际写入 **#33**（截断降级 + 零遥测等四条诚实边界，dev-plan §2.6 预授权路径，
> **不在本 §7 草案内**）；故本 §7 三条草案**再次顺延为 #33→#34（P2）、#34→#35
> （P3）**，下文标题已改。纪律同 dev-plan §2.6：**编号不得预留，一律按写入当日
> 实测最大号 +1**，本节编号仅是草案标识。

> **编号更新（2026-09-16，P2 收口时复测）**：实测当前最大号 = **#33**（P1 已写入）。
> P2 按纪律实测取号写入 **#34**——实写入正文**扩展为四条诚实边界**（会话时间线
> 仅当前运行 / 治理端点一律 Bearer 401 / dev 签发仅 `make ui-dev` / 6 子页挂载即发
> 6 条并发不得合并懒加载），**超出下方草案单条标题的范围**，正文以 README.md 为准；
> 下方「#34（P2 写入）」草案块仅存草案历史。§7 草案剩余一条顺延为 **#35（P3 写入）**。
> 另：dev-plan §2.7 卡内草案号写「#33」系撰写时（2026-09-14）旧号、未经 P1 顺延同步，
> 取号 #34 后已在该页 §2.7 回执偏差 ④ 登记归因。

> **编号更新（2026-09-16，P3 收口时复测）**：实测当前最大号 = **#34**（P2 已写入）。
> P3 按纪律实测取号写入 **#35**——本节最后一条草案即对应条目，实写入正文与草案
> 有两处时态/范围差异（见下「#35」块的 P3 实写入补记）。至此 §7 三条草案
> （#32/#34/#35）已全部实写入 README；#33 为 P1 计划外插入（上节），**本 §7 草案
> 清账**。

**#32（P0b 写入）前端不解除任何后端限制，且引入第二套工具链**
`workers=1` 约束仍在（0020 决策 ⑧ 收窄后的理由：会话/轮数/身份指纹已入
checkpoint，仍为进程内态的是限流桶、审计写与 SQLite 单写者）；前端只是消费面，
不改善可扩展性。同时仓库从 Python 单语言变为双工具链：`uv sync` 不再足以准备开发
环境，node 仅存于 nvm（`~/.nvm/versions/node/v24.16.0/bin`），**非交互 shell 不可见**
（实测 `command not found: node`），任何绕过 make 的调用都会失败。`make ui-*` 探测
不到 node 时 `exit 1`，不静默跳过。fresh clone 无 `frontend/dist`（被局部 `.gitignore`
忽略），此时 API 照常启动但浏览器无界面——这是预期行为，需先 `make ui-build`。

**#34（P2 写入）会话时间线只能展示当前运行的会话**
0022 未开 `/governance/sessions` 类端点，0020 的 SQLite checkpoint 是 Agent 内部
状态存储而非可查询的会话目录。故会话时间线面板的数据源只有"本浏览器标签内本次
运行累积的 `session_id`"；服务端重启后 `boot_id` 变化，历史会话虽在 SQLite 里
（0020 判据 11 已验证续接成功）但**前端列不出来**。要列出历史会话需新增只读端点
并裁定其权限口径（会话含问句原文，属敏感面），未裁定。

> **P2 实写入补记（2026-09-16）**：README 实际写入的 #34 为**四条**诚实边界
> （本条为第 ①；另含治理端点一律 Bearer 401、dev 签发仅 `make ui-dev`、
> 治理 6 子页挂载即发 6 条并发不得合并懒加载），正文以 README.md 为准。

**#35（P3 写入）图表能力取决于 ADR-0025 的落地，且落地后仍三项受限**
`render_chart` 当前无调用方（实测仅测试引用）。ADR-0025 已裁定接入方式
（决策 ①~⑥），但**裁定 ≠ 落地**：在 P3 批次交付前，任何响应里都没有 `chart` 键，
故两域时间序列**无图表**（不是"图表错误"，是根本不渲染）。落地后仍须如实写进
本条的三项剩余限制：(a) 时间轴列由 `Compiler.emitted_time_column` 声明携带，
`_TIME_COLUMN_NAMES` 只作非编译路径兜底，兜底命中时 spec 的 `note` 会显式标注
（决策 ③）；(b) `yoy`/`pop` 的图表**只画当期**，不画多序列对比（决策 ⑥）——
对比值只在数据表与 `note` 里；(c) 图表数据恒 ≤ 200 点（`MAX_CATEGORIES`），
超限降级为表格（决策 ④）。签名变更（`render_chart` 增关键字参数 `time_columns`，
带缺省值）受 0025 判据 5 约束：`tests/test_chart.py` 的 **16** 个既有用例必须
**逐字未改**仍全绿（`grep -c "def test"`，2026-09-14）。

> **P3 实写入补记（2026-09-16）**：README 实际写入的 #35 与草案有两处差异——
> ① 开头改为落地后时态（「图表接入已落地（P3，ADR-0025，2026-09-16），能力边界
> 仍有三条」；草案的「裁定 ≠ 落地」句系撰写时点事实，随交付失效）；② 末段补
> **「与 #21 的分工」**（#21 记渲染语义、#35 记接入状态与能力边界，两条不得合并
> 阅读）为草案所无。(a)(b)(c) 三项边界与草案一致；README.en.md digest #35 同步。
> 正文以 README.md 为准。

---

## 8. 本页能证伪什么

若 §5 任一批的"关键复验断言"失败，对应 ADR 的证伪条件即被触发，**回改 ADR 而不是
放宽断言**。三处最强的探针：

1. **P-2api 的 16 条 paths 集合断言**：它同时锁住 0022 决策 ①（前缀）、决策 ⑤
   （8+2 治理端点）与 0018 判据 7（fallback 不吞 API 404）。任一漂移都会在此暴露，
   不需要额外发明对比机制；
2. **P0a 的 `git diff eval/gold/` 为空**：全部锚定样本（分母按 0023 决策 ④ 的
   公式重测；撰写时实测 84，P-1 落地后预期 **97**——本页初稿误写 99，
   纠正过程见 §1.3 歧义 4）的 hash 逐条不变是驱动替换
   （GPLv2 → PyMySQL）零语义影响的唯一可机械验证证据；
3. **§3.3 六条渲染义务的 vitest 纯函数断言**：诚实性标志位一旦被前端"忘记"展示，
   `lib/honesty.ts` 的用例即红——把诚实性从"人工走查"变成"机器门槛"，这是本页
   相对 0018 判据 10 的实质收窄。
