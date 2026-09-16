# ADR-0018：前端控制台（React + Vite + Ant Design）——同源部署、工程边界与执行点

- 日期：2026-09-14
- 状态：accepted（决策经用户逐条确认，Q1~Q32 盘问；落地批次 P0b~P3）
- 相关：
  - ADR-0012（HTTP 服务面 v1，本 ADR 的消费方）、ADR-0022（HTTP 契约 v2，
    前端依赖的 `/api/v1` 前缀与治理端点）、ADR-0019（运行时快照口径，
    前端要展示绑定 sha）、ADR-0020（会话持久化）、ADR-0023（许可证基准）、
    ADR-0011（安全分层——前端**不新增**任何执行面）
  - 既有资产：`serving/rls_verify.py:319-341`（自包含 HTML 报告，仓库唯一 UI 先例）、
    `observability/dashboards/`（Grafana）、`agent/cli.py`（四态输出文案）、
    `agent/tools/chart.py`（spec 级确定性渲染，KL #21）
  - 既有 React 环境：`docs/outreach-video/remotion/`（`package.json` private:true、
    react ^18.3.1、typescript ^5.4、`tsconfig.json` strict:true、
    `package-lock.json` 142KB **已入库**、`docs/outreach-video/.gitignore` 局部忽略）
  - 工程边界：`Makefile`（36 目标，短横线簇命名）、`.pre-commit-config.yaml`、
    `.github/workflows/{lint,eval,integration,tag}.yml`、`.dockerignore`、
    `infra/docker/api/Dockerfile`、`docker-compose.yml:205-240`

---

## 背景

Atlas 当前**零前端工程**：日常使用只有 CLI（`atlas plan/compile/ask/query`）与
4 个 HTTP 端点，唯一图形产物是 `rls_verify.py` 拼字符串生成的自包含 HTML 报告
（验证工具，非交互界面）与 Grafana 指标看板（不含问数能力）。用户诉求是
"方便管理和日常使用"。

但探查证明前端**不是独立增量**——它压在既存缺陷上，任一项不修则对应面板不可用：

| 前端能力 | 被什么卡住 |
|---|---|
| 问数工作台 | `/ask` 在当前 HEAD 直接 503（`factory.py:40` 要求 `{HEAD}.meta.json`，实测 `/health` 返回 `snapshot_sha: null`）→ ADR-0019 |
| 多轮续接 | `MemorySaver` 进程内，重启静默失忆且不报错 → ADR-0020 |
| 图表 | `render_chart` 未接进任何响应（`_turn_payload` 无 chart 字段，实测 `grep -n chart serving/api.py agent/graph.py agent/state.py agent/cli.py` = 0 命中）。**缺陷比初稿记的严重**（ADR-0025 背景 3 实测）：`compiler.py:690-691` 拒绝 `is_time` 字段作维度，故无 comparison 时 SELECT 里**根本没有时间列**；有 comparison 时时间列被 `:991` 的 `time_col.lower()` 别名成小写，而 `chart.py:101` 是精确大小写集合判定 → **金融域同样失效**（`CalendarYearID` → `calendaryearid`），不只是 retail 的 `d_year/d_moy/d_qoy/d_date` 不识别。后果不是「误判为 bar」而是**画错量**：y 轴变成年份编号本身（`y=['d_year']`），真度量只落进 `note` 的「未渲染」清单，且不抛异常不红测试 → ADR-0025 |
| 角色切换器 | `row_policy.yml` 有 `broker` 但 `ROLE_DIRECTORY` 未注册 → 签发即 AuthError；`hq_admin` 在零售域误报 `rp_branch_visible` → ADR-0021 |
| 治理面板 | 无任何只读结构化端点（现有 4 端点全是业务面）→ ADR-0022 |
| 任何请求 | per-token 共享桶 60/min（`ratelimit.py:23-24`），治理面板一次加载 8 端点即撞穿 → ADR-0022 |

约束：

- **§5 技术栈锁定表无前端条目**，且标题为「Apache 全栈」——引入前端必须扩表并
  辨析措辞（见代价段 ①）。
- **N9**：token 与密钥不进前端代码/构建产物；dev 签发必须走仓库内 `sign_token`。
- **N3**：前端**不得**新增任何执行面或绕过 Guard；所有数据经既有 `/api/v1` 端点。
- **§7.3/§4**：`semantic/` 下不得新增 YAML 权威目录；`frontend/` 需在 §4 登记。
- 许可证基准缺失（LICENSE=MIT vs pyproject/README=Apache-2.0）→ 由 ADR-0023 先解。

---

## 备选方案

| 方案 | 优势 | 劣势 |
|---|---|---|
| **React 18 + TS + Vite + AntD + Recharts（选定）** | 仓库已有 React 环境与 TS strict 惯例（remotion）；AntD 覆盖治理面板全部组件（表格/树/描述列表/筛选/分页），工量最小；TS 给 HTTP 契约提供编译期类型 | 新增 6 个依赖 + node 工具链进入 Python 单语言仓库；§4/§5 需扩；`make ui-*` 需处理 nvm 的 PATH 问题（代价段 ③） |
| 零构建（StaticFiles + 原生 ES modules + 自绘 SVG） | 零新依赖、无 node 工具链、沿 `rls_verify.py` 既有风格 | 治理面板的表格/树/虚拟滚动/筛选全部手写，工量反而最大；无类型系统，HTTP 契约漂移无法编译期发现 |
| Vue 3 + Vite | 能力对等，中文生态成熟 | 仓库零 Vue 痕迹，无既有经验可复用（README §11 能力登记表全是 React 侧） |
| Streamlit / Gradio | 零前端代码 | 无法定制信息优先级（决策 ⑦）；把 Python 进程变成 UI 服务器，与 `serving/api.py` 职责重叠；引入不可控运行时依赖，且其数据访问惯例易绕过 Guard |
| 只扩 Grafana | 已在栈内、已部署 | Grafana 是指标看板，无法承载问数工作台与语义层浏览；其 SQL 数据源会**直连 Doris 绕过 Guard**（N3 风险），必须拒绝 |

---

## 决策

### ① 技术栈与许可证登记

| 包 | 版本约束 | 许可证 | 实测状态 |
|---|---|---|---|
| `react` / `react-dom` | ^18.3.1 | MIT | ✅ **已实测**：`docs/outreach-video/remotion/node_modules/react/LICENSE` 首行 `MIT License`，`package.json` version 18.3.1 |
| `typescript` | ^5.4 | Apache-2.0 | ✅ **已实测（P0b，2026-09-16）**：`frontend/node_modules/typescript/LICENSE.txt` 首行 `Apache License Version 2.0`；实装 5.9.3（约束 ^5.4 内） |
| `vite` | ^5 | MIT | ✅ **已实测（P0b）**：`frontend/node_modules/vite/LICENSE.md`（`Vite is released under the MIT license`）；实装 5.4.21 |
| `antd` | ^5 | MIT | ✅ **已实测（P0b）**：`frontend/node_modules/antd/LICENSE` 首行 `MIT LICENSE`；实装 5.29.3 |
| `recharts` | ^2 | MIT | ✅ **已实测（P0b）**：`frontend/node_modules/recharts/LICENSE`（`The MIT License (MIT)`）；实装 2.15.4 |
| `vitest` | ^1 | MIT | ✅ **已实测（P0b）**：`frontend/node_modules/vitest/LICENSE.md`（`Vitest is released under the MIT license`）；实装 1.6.1 |

诚实口径：本机 `npm view` 无输出（registry 仅在 `~/.npmrc` 配 npmmirror，且 node
不在非交互 PATH），**除 react 外均无法在安装前实测**。上表 ⬜ 项须在 P0b 批次
`npm install` 后逐包读 `node_modules/<pkg>/LICENSE*` 回填本表（列入验证方式判据 1），
不得沿用"官方声明"作为最终登记（AGENTS.md §5 ③ 要求评估许可证）。
**回填已完成（P0b，2026-09-16）**：5 个 ⬜ 全部转 ✅ 实测——全部 permissive
（Apache-2.0 ×1 + MIT ×4），**推翻条件第 4 条（非 permissive → 替换）未触发**；
实装版本均在约束内，无偏差项。实测口径与依赖规模见文末「落地注记：P0b 批次」。

TypeScript 的 Apache-2.0 与项目的 Apache-2.0（ADR-0023）同族；MIT/BSD 依赖被
Apache-2.0 项目包含是既有事实（`pyproject.toml` 注释已记 fastapi/uvicorn 为
"MIT/BSD 许可"），本决策不改变该兼容性判断。

**禁止再引 `@ant-design/charts`**：与 Recharts 功能重叠，双图表库同时进 bundle
且增加许可证面（决策已选 Recharts，理由见 ⑦）。

### ② 同源部署，永久零 CORS

- dev：vite dev server + `server.proxy` 转发到后端；
- prod：`FastAPI.mount` + `StaticFiles` 挂 `frontend/dist`，与 API 同源；
- `serving/api.py` 当前**零 CORS 配置**（同源假设），本决策**保持该假设**，不引入
  `CORSMiddleware`——一旦引入即承认跨源部署，随之而来的是凭据/预检/源白名单治理，
  与"自用为主"（Q1）的定位不匹配；
- SPA 路由：多级 `BrowserRouter` 路径（如 `/governance/metrics`）刷新会 404，因为
  `StaticFiles(html=True)` **只对目录请求**回 `index.html`。故用显式 catch-all
  fallback 路由返回 `index.html`，且该 fallback **必须注册在 `/api/v1` 路由之后**，
  否则会吞掉 API 的 404（把接口错误变成 HTML 200，前端静默失败）。

### ③ dev 后端目标双档可切换，且禁止 `localhost`

`ATLAS_API_TARGET` 与 `ATLAS_API_BASE` **成对**切换（单一变量会导致前缀与目标不匹配）：

| 档 | TARGET | BASE | 用途 |
|---|---|---|---|
| 默认 | `http://127.0.0.1:8000` | `/api/v1` | `make serve-dev`（宿主机新代码） |
| 对照 | `http://127.0.0.1:8010` | ``（空前缀） | atlas-api 容器（旧代码 `7d48dcb`），仅旧契约对照 |

**必须硬写 `127.0.0.1`，禁止 `localhost`**。实测（2026-09-14）：

```
curl http://localhost:8000/health    → {"detail":"Not Found"}   # ccdp-ontology-api（他人项目，IPv6 ::1）
curl http://127.0.0.1:8000/health    → {"status":"ok","head_sha":"bdcb6c0","snapshot_sha":null}
curl http://127.0.0.1:8010/health    → {"status":"ok","head_sha":"7d48dcb","snapshot_sha":"7d48dcb"}
```

`localhost` 经 DNS 解析优先命中 IPv6 `::1`，而 docker 占的是 IPv6 `*:8000`、
uvicorn 绑的是 IPv4 `127.0.0.1:8000`——同一端口两个不同服务。vite proxy 若写
`localhost`，会代理进**另一个项目**，症状是满屏 404 且极难归因。该结论必须写进
`vite.config.ts` 注释，防止后续"顺手改成 localhost"。

### ④ 工程边界

- `frontend/.gitignore` **局部**忽略 `node_modules`/`dist`/`coverage`，不改根
  `.gitignore`（沿 `docs/outreach-video/.gitignore` 全文一行 `remotion/node_modules`
  的既有惯例）；根 `.gitignore` 仅新增 `serving/state/`（ADR-0020 的落盘目录，
  与既有 `serving/audit/` 同处 L46）。
- 包管理器 **npm**，`frontend/package-lock.json` **入库**（沿 remotion 惯例）。
- `.dockerignore` **必须**补 `frontend/node_modules` 与 `frontend/dist`：docker
  不读 `.gitignore`，也不读嵌套的 `frontend/.gitignore`，只读根 `.dockerignore`；
  而 Dockerfile 是 `COPY . .`——本地 `npm install` 后一次构建会把数百 MB 依赖
  打进镜像层。
- `tsconfig.json` 沿 remotion 既有严格度：`strict: true`、`target: ES2020`、
  `module: ESNext`、`moduleResolution: Bundler`、`jsx: react-jsx`、
  `skipLibCheck: true`；`package.json` 设 `private: true`（不发布，避免 npm 侧
  许可证/版本声明与 ADR-0023 打架）。
- `[tool.setuptools] packages = {find = {where = ["."]}}` 只发现带 `__init__.py`
  的顶层包，故 `frontend/` 不会被打进 Python 包；但需在 AGENTS.md §4 登记目录职责。

### ⑤ prod 镜像多阶段构建

- Dockerfile 增 `node:20-alpine` 阶段跑 `npm ci && npm run build`，再
  `COPY --from=<stage> /app/frontend/dist ./frontend/dist` 进 `python:3.11-slim`；
  多阶段最终层不含 node，镜像体积不涨。
- `StaticFiles` 挂载做成**条件挂载**：`dist` 不存在则跳过并打 warning，容器照常起
  （否则 fresh clone 未构建前端时 API 直接启动失败，把 UI 缺失升级为服务不可用）。
- 统一 `GIT_SHA` 的三处默认值并修掉撒谎注释（行号复测于 2026-09-14）：
  `docker-compose.yml:211` 生效值为 `7d48dcb`，而 `:198-199` 的注释自称"默认最新
  29 表全量数据版本 b933e20（data/snapshots/ 最新锁定 meta）"；
  `Dockerfile:33` `ARG GIT_SHA=b933e20`，`:11` 的注释同样自称"GIT_SHA 默认 =
  最新 29 表全量数据版本"；而 `data/snapshots/` 实测 17 份 meta 中按 `created_at`
  最新是 `a11d779`，字典序最大是 `dc4f350`（ADR-0019 实测，两者不同）——
  **三处默认值互不相同，且无一等于当前 HEAD `bdcb6c0`，注释声称的"最新"无一成立**。
  compose `args` 覆盖 Dockerfile `ARG`，故实际生效 `7d48dcb`。
- `docker-compose.yml:231` healthcheck 硬编码 `/health`，ADR-0022 迁 `/api/v1` 后
  **必须同步**——漏改则容器永久 unhealthy，而 `:215/:217` 有两个服务用
  `condition: service_healthy` 依赖它，下游全起不来；且该失败在 CI 不暴露
  （`lint.yml` 不起 compose，只有手动触发的 `integration.yml` 才起）。

### ⑥ 执行点：本地 make 是唯一真实门槛

实测（2026-09-14）：**4 个 GitHub Actions workflow 从未执行过一次**。

```
git remote -v                → 唯一 origin git@gitee.com:hyperions/Atlas.git
ls -d .workflow .gitee       → 不存在（Gitee Go 读 .workflow/，非 .github/workflows/）
git ls-remote --tags origin  → exit=0，stdout 全空（3 个本地 tag 从未推送）
```

故 `tag.yml` 的 `workflow_run` 事件链（`conclusion==success` 才打 tag）在 gitee 上
永不触发，README:191「lint 通过后自动打 tag，不再依赖手工记忆」不成立（3 个 tag
均手工打且未推）。README:191 自身已诚实登记「部署目标 GitHub Actions」「真实执行
待 push」，故不构成 N2 违规；不符的是 `AGENTS.md §4`（把 workflows 写成既成事实）
与 `README:179`/`:316`（「CI 经 `make lint` 自动执行」）。

决策：

- `ui.yml` **照写**（`paths: ['frontend/**']` + `setup-node@v4` + npm 缓存 +
  `npm ci` → `tsc --noEmit` → `vitest run` → `npm run build`），作为未来接 GitHub
  镜像时的现成配置；但**不作为本批次的验收依据**。
- 真实门槛落在本地：`make ui-check`（tsc + vitest + build）并入 `.pre-commit-config.yaml`。
- `lint.yml` 不加前端步骤（它无 `paths` 过滤，任何 push 都会跑；塞进去会让改一个
  错别字也付 npm ci 的代价，反而诱使人少跑语义层 lint 这个真正核心的门槛）。
- 同步修正 `AGENTS.md §4` 与 `README:179/316` 的表述为「当前远程为 gitee，
  GitHub Actions 不执行；执行点为本地 make + pre-commit」。

**Makefile 必须显式探测 node**。实测：node v24.16.0 / npm 11.13.0 装在 nvm
（`~/.nvm/versions/node/v24.16.0/bin`），`zsh -ic` 可见而**非交互 shell 不可见**
（`command not found: node`）；Make recipe 用 `/bin/sh` 执行 → `make ui-*` 必然失败。
处理：`NODE_BIN ?= $(shell command -v node 2>/dev/null || ls -d $$HOME/.nvm/versions/node/*/bin 2>/dev/null | tail -1)`
+ `export PATH := $(NODE_BIN):$(PATH)`；探测不到则打印明确指引后 `exit 1`（**不静默
跳过**——静默跳过会让"门槛通过"变成假信号）。README 记 node ≥ 20。

`.pre-commit-config.yaml` 的 `check-added-large-files --maxkb=1024` 需加
`exclude: ^frontend/package-lock\.json$`（remotion 的 lock 142KB/152 包，
React+AntD+Recharts+Vite+vitest 的依赖树量级更大，很可能逼近阈值）。**只 exclude
该文件，不全局抬高 maxkb**——抬高会削弱对二进制误提交的防线。

### ⑦ 工作台信息优先级：继承既有诚实性设计

既有两处展示物料的信息优先级是**一致**的：

```python
# serving/rls_verify.py:319-341  自包含 HTML 报告
f"<h1>Atlas 行级权限验证 <code>{report['sha']}</code></h1>"       # ① sha 进标题
"<p>链路：Planner → Compiler → Guard（谓词别名对齐 + 二次只读校验）→ Doris 执行</p>"  # ② 链路显式
f"<h3>{role}（{policy_name}）</h3><p>谓词：<code>{condition}</code></p>"            # ③ 谓词与注入后 SQL 可见
```

CLI 侧同构：`[answer] 指标=X | 行数=Y | 执行 Zms` → `SQL: ...` → 表格 →
`…（仅显示前 500 / 共 N 行；完整数据用 --format json）`。

故前端工作台的渲染顺序**锁定**为：**指标口径 → 行数/耗时 → 出口 SQL → 数据 →
截断声明**，并额外要求：绑定快照 sha 常驻可见（Q2）、四态（answer/clarify/blocked/
error）各有显式视觉分支（沿 CLI 文案）、图表置于数据之后而非之前。

图表选 Recharts 而非 `@ant-design/charts`：前端只做 **spec → 组件的确定性映射**，
不参与任何图表类型决策——决策在 `chart.py`。Recharts 是 React 原生组件库，映射代码
即 JSX，无需再学一套 AntD charts 的配置 DSL；且 AntD 已用于治理面板组件，图表再引
AntD 生态会让"AntD 版本升级"同时影响两类不相关能力。

> 纠错（ADR-0025 撰写时实测，2026-09-14）：本段初稿称「后端 spec 已含
> type/x/y/**series** 与 `sql_sha256`」。实测 `grep -rn series agent/tools/chart.py
> tests/test_chart.py` = **0 命中**，spec **无 `series` 键**。真实键集是
> `bar`/`line`：`type` `x`（**string**，非列表）`y`（**list**）`data` `sql_sha256`
> `skipped`（恒 0）+ 条件键 `note`；`table`：`type` `columns` `rows` `sql_sha256`
> `note` `skipped`（`chart.py:171-179` / `:183-190` / `:233-242`）。
> 该差异对前端非 cosmetic：`x` 是 string 而 `y` 是 list，两分支键集不同，
> TS 侧必须是 discriminated union 而非单一 interface。
> **本段结论（选 Recharts、前端不做图型决策）不受该纠错影响**——它只依赖
> 「决策在后端 spec」这一点，而该点成立。spec 键集与时间轴事实源由 ADR-0025 裁定。

---

## 理由

1. **AntD 的决定性优势是治理面板**：5 个面板的主体是表格/树/描述列表/标签页/
   筛选器，AntD 全覆盖；自绘或零构建方案要手写虚拟滚动与树形展开，工量超过前端
   其余部分之和。
2. **同源部署消掉一整类问题**：CORS、凭据跨源、预检、cookie SameSite 全部不存在。
   项目是自用工具（Q1），不需要跨源能力，为一个用不到的能力付治理成本不划算。
3. **本地 make 为执行点是诚实选择而非妥协**：既然 GitHub Actions 在 gitee 上永不
   执行，把验收判据挂在它上面就是 N2（把设计写成已完成）。写成脚本化 make 目标
   至少有真实的执行者（你自己在提交前跑）。
4. **多阶段构建而非产物入库**：Vite 产物是 hash 文件名（`assets/index-a1b2c3.js`），
   每次构建全部改名 → 每次提交都是整目录增删的巨 diff；且入库产物与源码可能不同步
   而无法验证（诚实性问题）。`airflow/dags/generated/` 虽入库但那是可读 YAML，
   不是压缩 JS，类比不成立。

---

## 代价与限制

① **§5「Apache 全栈」标题下出现 5 个非 Apache 基金会项目**（Meta 的 React、
微软的 TypeScript、社区维护的 Vite/AntD/Recharts）。措辞需辨析为"数据与后端基础
设施 Apache 全栈，前端为 permissive 许可的社区栈"，否则 §5 标题本身变成不实声明。

② **Python 单语言仓库引入 node 工具链**：贡献者需同时具备两套环境；`uv sync`
不再足以准备开发环境，README 的安装依赖表必须扩（精确定位：**§3.1 前置条件**，
`grep -n "^### 3.1" README.md`；2026-09-14 实测该节为 6 行依赖表：Docker / Python /
uv 或 pip / Git / 内存 / 显卡，node 需作为第 7 行插入）。

③ **node 仅存于 nvm**：任何绕过 make 的调用（直接 `npm run dev`、CI 脚本、
其他 agent 的非交互 shell）都会 `command not found`。这是环境事实而非代码可修复项，
只能在 Makefile 探测 + README 显式声明。

④ **双档切换的 8010 档在 ADR-0022 落地后不保证可用**：容器跑 `7d48dcb` 旧代码，
无 `/api/v1` 前缀。故 `ATLAS_API_BASE` 必须成对切换，且该档需在 `.env.example`
与 README 标注"仅旧契约对照用"。

⑤ **fresh clone 无 UI**：`dist/` 被 `.gitignore:13` 忽略，不构建就没有前端；
条件挂载（决策 ⑤）保证此时 API 仍可用，但"打开浏览器什么都没有"是预期行为，
需在 README 写明先 `make ui-build`。

⑥ **治理面板的数据本身有诚实性陷阱**，面板必须显式区分，否则会误导：

- 值域 20 份中 **11 份是 `status: skipped` 空壳**（`values: []`，
  `skip_reason: "distinct=2715 超过阈值 200"`，ADR-0016 §①）。若面板平铺 20 条，
  会让人误以为值域覆盖充分。
- 评测报告 48 份中只有 **12 份结构统一**（主报告 `<sha>.json` 固定 7 键），
  另 36 份分属 18 种文件名模式且内部结构互不相同（rls-verify 有 roles/outcomes、
  api-acceptance 有 A1~A7、polaris-rbac 有 grants）。故报告浏览器只能对主报告做
  结构化渲染，其余按"原始 JSON + 模式标签"降级展示，**不得伪造统一表头**。
- 主报告的 `dry: bool` 区分 CI 无 DB 跑与真跑（含 EX）；面板必须显示该标志，
  否则 dry 报告的 `ex: "n/a"` 会被读成"EX 为 0"。

⑦ **前端不解除任何后端限制**：`workers=1` 约束仍在（收窄后的理由见 ADR-0020
决策 ⑧：会话/轮数/身份指纹已入 checkpoint，仍为进程内态的是限流桶、审计写与
SQLite 单写者）；前端只是消费面，不改善可扩展性。README KL 不得因"有了前端"而
放宽任何既有条目（N4）。

---

## 什么情况下应该推翻

- 出现跨源部署需求（前端与 API 分离部署、或嵌入其他系统）→ 决策 ② 的同源假设
  作废，需引入 `CORSMiddleware` + 源白名单 + 凭据策略，并重估限流与审计口径；
- 出现多用户/真实鉴权需求 → dev-only 的 vite middleware 签发（Q4）必须废除，
  改接 ADR-0011「gateway 认证先行」的 IdP 路径（`serving/auth.py` 已有 RS256/JWKS
  验签钩子与 `cryptography>=42` 依赖）；
- 治理面板需要写操作（编辑指标、改策略）→ 本 ADR 的"只读消费面"定位作废，
  需重新设计写路径的审批与审计，且必须评估 N3/N8 的冲突；
- AntD 或 Recharts 出现许可证变更（转为非 permissive）→ 决策 ① 需重评，
  优先替换图表库（Recharts 可替换面小），AntD 替换成本高需单独立项；
- node 工具链的维护成本持续高于收益（例如依赖漏洞修复占用显著时间）→ 回退备选
  方案第 2 行（零构建），保留后端 spec 契约不变。

---

## 验证方式

**P0b 批次（工程边界）判据**：

1. `npm install` 后逐包读 `node_modules/{typescript,vite,antd,recharts,vitest}/LICENSE*`，
   回填决策 ① 表格的 ⬜ 项为 ✅ 实测——**未回填不得声称许可证评估完成**；
2. `frontend/.gitignore` 存在且根 `.gitignore` 无 `node_modules`；
   `git check-ignore -v frontend/node_modules frontend/dist` 命中局部文件；
3. `.dockerignore` 含 `frontend/node_modules` 与 `frontend/dist`；
   `docker build` 上下文体积可测（构建日志的 context 大小）不含 node_modules；
4. `make ui-check` 在**非交互 shell** 下可跑通（`sh -c 'make ui-check'`），
   证明 node 探测生效；移除 node 后该目标打印指引并 `exit 1`，不静默跳过；
5. pre-commit 对 `frontend/package-lock.json` 不报 large-file，且对 >1MB 的
   二进制文件仍报（exclude 未过度放宽）。

**P1~P3 批次（能力）判据**：

6. `curl http://localhost:5173/...` 经 vite proxy 命中 `127.0.0.1:8000`（而非 ::1
   的他人服务）——用 `/api/v1/health` 的 `head_sha` 断言；
7. SPA 深层路径刷新（如 `/governance/metrics`）返回 200 HTML；同时
   `GET /api/v1/<不存在路径>` 返回 **404 JSON**（证明 fallback 未吞 API 路由）；
8. prod 模式（`make ui-build` + `make serve-dev`）同源访问 UI 与 API，
   浏览器 Network 面板零 CORS 预检请求；
9. 工作台渲染顺序符合决策 ⑦（指标口径先于 SQL 先于数据先于图表），
   由 vitest 对纯函数（spec→组件映射、四态分支判定、sha 展示格式）断言；
10. 治理面板对值域 `skipped` 空壳与评测报告非主报告模式**显式降级展示**，
    由 vitest 对分组/降级纯函数断言（不依赖渲染快照）。

**证伪条件**：若判据 6~8 中任一无法达成（例如同源挂载与 `/api/v1` 路由冲突无法
调和），则决策 ② 被推翻，回退到 dev 用 proxy、prod 用独立静态服务器 + 显式 CORS。

---

## 落地注记：上游 ADR 齐备后的收窄与依赖修正（2026-09-14，ADR-0022 / ADR-0023；不改裁定正文）

**本注记记录的是其他 ADR 对本 ADR 约束的收窄与排序修正，前端本体（`frontend/`）
仍未开工**——P0a/P0b/P1~P3 四个批次均为未落地状态，决策 ① 表格的 5 个 ⬜ 仍为 ⬜。

### 1）决策 ⑤ 第 4 条（compose healthcheck 必须同步）收窄为「**不需要同步**」

原文要求：`docker-compose.yml:231` 的 healthcheck 硬编码 `/health`，ADR-0022 迁
`/api/v1` 后必须同步，否则容器永久 unhealthy。ADR-0022 决策 ① 改为 **`/health`
双挂**（根路径保留 + 新增 `/api/v1/health`，同一 handler），故：

- `docker-compose.yml:230-231` 的 healthcheck 字符串**逐字未变**（ADR-0022 判据 2
  要求以 `git diff` 断言），本条描述的失败链不再可达；
- `:215/:217` 两个 `condition: service_healthy` 依赖方不受影响；
- 本条原文保留作为**历史风险记录**（若将来反向拆除根路径 `/health`，风险原样
  回来，包括「该失败在 CI 不暴露」那一段）。

### 2）代价 ⑥ 的三个诚实性陷阱已有**契约级载体**，不再靠面板自己推断

ADR-0022 决策 ⑦ 把判别信息放进响应体，面板只需渲染字段而不需内置启发式：
值域 `status:"skipped"` + `skip_reason`（实测 11 份）、报告 `structured:false` +
`pattern`（实测 36 份 / 18 种模式）、主报告 `dry` 透传。另额外覆盖本 ADR 未列举的
两项：同义词 `empty_placeholder:true`（zh_cn 实测 0 指标 / 0 维度，权威源在 ossie
`ai_context`）与快照 `is_latest_by_created_at`（实测字典序最大 `dc4f350` ≠
`created_at` 最新 `a11d779`）。判据 10 的「显式降级展示」因此可直接断言字段，
不必断言渲染快照。

### 3）批次顺序修正：**P0a（ADR-0023 许可证）必须先于 P0b**

本 ADR 把前端包许可证复核放在 P0b 判据 1，但项目自身的许可证当时三方矛盾
（`LICENSE` = MIT、`pyproject.toml:13` = Apache-2.0、`README.md` §12 代码行 = Apache-2.0；
该行初稿记为 `:756`，2026-09-14 复测已漂到 `:860`，以 `grep -n "^## 12" README.md` 定位），
在无项目基准的情况下回填第三方包许可证无意义（无法判定兼容性）。ADR-0023 将项目
许可证统一为 Apache-2.0 并清理 copyleft 依赖（GPLv2 的 `mysql-connector-python`、
LGPL-3.0 的零引用 `psycopg`），作为批次 **P0a**，插入在 P0b 之前。完整顺序：
P-1（ADR-0017/0019/0020）→ P-2sec（ADR-0021）→ P-2api（ADR-0022）→ **P0a**
（ADR-0023）→ P0b（本 ADR 工程边界）→ P1~P3（本 ADR 能力）。

### 4）背景表第 5/6 行（治理面板 / 限流撞穿）已有裁定，但**未落地**

ADR-0022 决策 ⑤（8 集合 + 2 钻取治理端点）与决策 ⑥（治理桶 240/min 与业务桶
60/min 独立）分别对应本 ADR 背景表的「无任何只读结构化端点」与「治理面板一次
加载 8 端点即撞穿 60/min」两行。两者均为**待实现裁定**（批次 P-2api）；在该批次
落地前，前端治理面板无可用数据源，不得开工。

### 5）P-2api 落地状态更新（2026-09-16，收口复测；不改上文裁定）

P-2api（ADR-0022）已落地：真链 `make api-verify` A1-A9 全绿（11 场景，报告
`eval/reports/api-acceptance-95cba68.json`）。本 ADR 受影响三处复测如下：

- **§1 的收窄预判兑现**：本批对 `docker-compose.yml` 仅新增注释 4 行，
  healthcheck `test:` 字符串逐字未变（`git diff` 可核）；「`/health` 双挂同
  body」由真链 A4（`eval/api_acceptance.py` 同打两个挂点断言同 body）与契约
  测试 `TestHealthDualMount`（`tests/test_api_contract_v2.py`）双向锁定。
- **§4 的「待实现裁定」已实现**：治理面 8 集合 + 2 钻取已在
  `/api/v1/governance/*` 提供（A8 真链全绿），治理面板的「无数据源」阻塞
  解除；但前端本体（`frontend/`）仍未开工——§3 的批次序（P0a → P0b →
  P1~P3）与决策 ① 表格的 5 个 ⬜ 不变，本节不构成开工许可。
- **§2 引用的实测数字时点更新，以本段为准**：报告索引 53 份 = 13 主报告 +
  40 份非主报告分属 18 种文件名模式（原记 48 份 = 12 + 36/18 种）；值域
  `skipped` 11 份、zh_cn `empty_placeholder:true`（0 指标 / 0 维度）与
  字典序最大 `dc4f350` 三项复测不变；`created_at` 最新快照更新为
  **`ccb4c8b`**（原记 a11d779，P-1 收口新锁）。

---

## 落地注记：P0b 批次（前端工程边界，2026-09-16；不含任何界面）

**落地范围**：`frontend/` 骨架（11 个入库文件 = `index.html` + `package.json` /
`tsconfig.json` / `vite.config.ts` / `.gitignore` 4 配置 + `src/` 下 3 源码
（`main.tsx` / `App.tsx` / `api/endpoints.ts`）+ 2 个 vitest 测试 +
`package-lock.json`）、`serving/api.py` 条件 SPA 挂载、仓库侧工程面（Makefile
四目标 / `.pre-commit-config.yaml` / `.dockerignore` / Dockerfile 多阶段 /
`ui.yml` / `.env.example`）。五个面板、图表与一切真实界面**均未开工**
（P1~P3 仍为未落地状态）；决策 ① 表格的 5 个 ⬜ 已在本批回填（见上文）。
以下逐条记录判据 1~5 的实测证据。

### 1）判据 1：5 包许可证逐包回填（决策 ① 表格 ⬜ → ✅）

- `typescript` 5.9.3 → Apache-2.0（`LICENSE.txt` 首行 `Apache License Version 2.0`）；
  `vite` 5.4.21 / `vitest` 1.6.1 → MIT（`LICENSE.md`）；`antd` 5.29.3 → MIT
  （`LICENSE` 首行 `MIT LICENSE`）；`recharts` 2.15.4 → MIT（`LICENSE`）。
  全部 permissive（Apache-2.0 ×1 + MIT ×4），**推翻条件第 4 条未触发**，
  实装版本均在约束内。
- 依赖规模实装口径（2026-09-16 实测）：直接依赖 **9 个**（dependencies 4 +
  devDependencies 5）= 决策 ① 表内 7 包 + 表外 `@types/react` 18.3.31 与
  `@types/react-dom` 18.3.7（均 MIT，`docs/outreach-video/remotion/package.json`
  已有 `@types/react` 先例）；`npm ls --all` 全树 **186 包**；
  `package-lock.json` **234 个包条目 / 115150 字节**。备选栏「新增 6 个依赖」
  为撰写时预估口径，以 `frontend/package.json` 实装为准。
- 未引入 `@vitejs/plugin-react`：vite 内置 esbuild 按 `tsconfig.json` 的
  `jsx: react-jsx` 完成 TSX 转换（最小壳 `npm run build` 实测通过）；
  Fast Refresh 属开发体验，P1 开工时复核。

### 2）判据 2：gitignore 局部化（实测）

- `frontend/.gitignore:3-4` = `node_modules/`、`dist/`；根 `.gitignore` 无
  `node_modules`（`grep` 零命中）。
- `git check-ignore -v frontend/node_modules frontend/dist` 命中局部文件：
  `frontend/.gitignore:3:node_modules/` 与 `:4:dist/`。

### 3）判据 3：.dockerignore 与构建上下文（实测）

- `.dockerignore` 两条已加（`frontend/node_modules`、`frontend/dist`）。
- 判据原文「构建日志的 context 大小不含 node_modules」在 `COPY . .` 场景下
  无法分辨（上下文总量本身很小），改用决定性实验：临时 Dockerfile
  `COPY frontend/ /check/` + `ls -A && du -sh` → `/check` 仅 7 项源码
  （`.gitignore` / `index.html` / `package-lock.json` / `package.json` / `src` /
  `tsconfig.json` / `vite.config.ts`）共 172K，**无 `node_modules` / `dist`**。
  临时镜像已 `rmi` 清理。
- `docker build --target frontend` 实测通过：`npm ci` 186 包 5s + `vite build`
  601ms，产物 hash 与宿主机一致（多阶段注入路径可用）。

### 4）判据 4：非交互 shell 与 node 探测（实测）

- `env -i HOME=$HOME PATH=/usr/bin:/bin sh -c 'make ui-check'` 全绿：默认 PATH
  无 node，`NODE_BIN` 探测命中 nvm 后注入，tsc + vitest（6 用例）+ build
  三连通过。
- 负向：`make ui-check NODE_BIN=`（显式置空覆盖探测）→ guard recipe 打印中文
  指引后 `exit 1`，make 以 exit 2 收总——**未静默跳过**；指引内 `$HOME` 正确展开。
- `make -n` dry-run 复核：canned recipe 为单逻辑行、`$$HOME` 转义正确、
  `.PHONY` 与真实目标 48/48 零差集。

### 5）判据 5：pre-commit（实测 + 网络受限降级）

- `check-added-large-files --maxkb=1024` 已加
  `exclude: ^frontend/package-lock\.json$`（仅该文件；maxkb 未抬，二进制误提交
  防线不削弱）。
- 本机 GitHub 不可达（2026-09-16 实测 `git fetch` 持续 `Connection reset by
  peer`），remote hook 环境从未完成初始化（`~/.cache/pre-commit` 为空）——
  **完整端到端 run 待网络可达时补验**。本批以等价组合验证：
  ① `pre-commit validate-config` exit 0；② exclude 正则 Python `re` 双向 4 用例
  （lock 命中排除 / >1MB 二进制不命中）；③ 直跑
  `check-added-large-files --maxkb=1024 --enforce-all`：1.1MB 探针 exit 1、
  lock（115KB）exit 0；④ mini-config（repo 源换 local）端到端：large-file
  Skipped、ui-check Passed、exit 0。

### 6）旁记：SPA 挂载与文面「StaticFiles 挂载」的偏差（不改裁定正文）

决策 ②⑤ 文面写 `StaticFiles` 挂载；实现为 **catch-all 路由 + `FileResponse`**：
`mount("/")` 是终止匹配、会把 fallback 连同 API 404 一起吞掉，而
`StaticFiles(html=True)` 只对目录请求回 `index.html`、多级路径刷新依旧 404
（理由全文在 `serving/api.py` 的 `_attach_spa` docstring）；文件直出仍走
`FileResponse`（StaticFiles 内部同款实现），无能力损失。该行为由
`tests/test_spa_static.py` 两态锁定（12 用例 = 有 dist 9 + 无 dist 3）：有 dist
时深层路径 200 HTML、`/api/v1/<不存在>` 仍 404 JSON、路径穿越不越出 dist；
无 dist 时 warning + 默认 404、API 不受影响。**判据 7 的 ASGI 层断言由此预锁定**
（浏览器侧复核与判据 6/8 归 P1，未验）。

---

## 落地注记：P1 批次（2026-09-16）

**注记结构**：第 1 节为**开工前**的跨批门禁 G1 解除裁定（须先于 P1 的 `feat`
提交——dev-plan §2.6 预授权的解除路径）；第 2 节起为落地与判据实测，收口时补写。

### 1）门禁 G1 解除：前端可观测口径（裁定；2026-09-16 经用户确认）

设计页 §6.3 实测：本 ADR 与 ADR-0022 对 `OTel` / `otel` / `token_cost` 零命中
——前端批次的可观测口径无任何裁定，而 AGENTS.md §11 第 6 步要求新功能
「加可观测：埋 OTel span，记录 token_cost」。按设计页 §6.3 的四个子问题逐条
裁定（总题为「前端零遥测」）：

1. **前端错误不上报**：不新增任何上报端点。现有服务面全部是只读业务/治理面，
   错误上报会成为该面上的**第一个写入面**（N3 邻接风险），与本 ADR「只读
   消费面」定位（决策 ②、代价 ⑦）冲突——安全先于可观测的完备性；
2. **不引入前端遥测 SDK**：`@opentelemetry/*` Web 包属新依赖，须走 AGENTS.md
   §5 流程（理由 / ADR / 许可证 / 预算）；对单机自用工具（Q1）收益不足——
   服务端 `observability/otel.py` 的 `atlas.turn` span（含 SQL / 行数 / 延迟
   属性）与 `serving/audit.py` 的逐请求审计行已覆盖服务端全部事实；
3. **与既有 span 的关联**：无——前端不产生 span、不消费 trace 上下文。前端侧
   「可观测」义务收敛为**渲染义务**：error 态如实展示后端错误原文（不美化、
   不摘要、不吞错），与决策 ⑦ 的诚实性优先级同源；
4. **治理页 8 请求不聚合**：与 0022 决策 ⑥ 的逐请求审计行同构（每请求一条、
   各自独立）；跨请求 trace 聚合无消费方，不在 P1~P3 范围。

**推翻条件**：出现「错误仅在用户浏览器可见、服务端无从归因」的实际排障阻塞，
或出现多用户/远程排障需求 → 重评本裁定（备选：OTLP 直发 `observability/`
的 collector，不经 serving 写入面）。

### 2）判据 6：vite proxy 命中 127.0.0.1:8000（双探针实测）

`/api/v1/health` 双探针（direct `http://127.0.0.1:8000` vs proxied
`http://localhost:5173`，vite proxy 转发）：两 JSON 逐键相等，且 `head_sha`
= 当前 git HEAD（先用 `6868f14` 时点验证；feat 提交后复验升至 `05cb9ff`）——
`head_sha` 随 HEAD 即时更新是「命中本仓 IPv4 后端而非 `::1` 他人服务」的断言
（若是他人服务，proxied 侧不可能与 direct 逐键相等且跟随本仓 HEAD）。
观察值：`snapshot_source=latest` + `bound_to_head=false`（该时点 HEAD 与锁定
快照不同源）→ 徽标须为橙色警示态，浏览器复核确认（`快照 7c966e9（未绑定
HEAD，取自最新已锁）`，`ant-tag-orange`）。

### 3）判据 7：SPA 深层路径与 API 404（浏览器侧复核）

- `curl -i /governance/metrics` → 200 `text/html; charset=utf-8`（SPA 兜底，
  多级路径可刷新）；`/` → 200 `text/html`；
- `curl -i /api/v1/definitely-not-a-route` → 404 `application/json` +
  `{"detail":"Not Found"}`（catch-all 的 `api/` 前缀分支不落 HTML）；
- 浏览器侧：导航 + 刷新 `/governance/metrics` 均渲染工作台（P1 无路由，
  面板 1 形态；路由属 P2），console 零消息；
- ASGI 层 `tests/test_spa_static.py` 12 例（P0b 预锁定）继续绿灯。

### 4）判据 8：prod 同源零 CORS 预检（浏览器 Network 实测）

浏览器直开 `http://127.0.0.1:8000/`（uvicorn 同源供 SPA 与 API）：Network
记录 5 条请求（`/`、`/assets/index-*.js`、`/assets/index-*.css`、
`/api/v1/health`、内联 data URI 图），全部 200；**0 个 OPTIONS 请求、0 个失败
请求、0 个 `access-control-*` 请求头**；console 零消息。结构保证：服务端全程
无 `CORSMiddleware`（`grep` 零命中）；OPTIONS 探针（`curl -X OPTIONS
/api/v1/ask`）落 catch-all 的 `api/` 前缀分支 → 404 JSON（P0b 三态分流第 1
条；同页已登记「已知 API 路径配错误方法 405→404 归并」副作用）。**决策 ② 的
同源假设未被推翻，停批条件未触发**。

### 5）判据 9：渲染顺序与四态分支（vitest 纯函数断言，不依赖渲染快照）

`make ui-check`（tsc + vitest + build）exit 0；vitest 6 文件 36 用例：

- `order.test.ts`（6）：`ANSWER_SECTIONS` 逐字锁定渲染次序「指标口径 → 行数/
  耗时 → 出口 SQL → 数据 → 截断声明」；`branchOf` 五 kind 字段集逐字 + 未知
  kind → null（不猜分支）；
- `sha.test.ts`（8）：徽标 4 态文案逐字 + degraded 不得含「加载中」 + 2 个
  契约外组合的防御态；
- `honesty.test.ts`（14）：`truncationState` 两提示的触发条件与文案逐字（含
  500 渲染上限、`row_count === limit` 的不确定语气）+ 治理标志位判定（判据
  10 的 P1 前置：纯函数已入库，完整断言随 P2 治理面板交付时补）；
- `session.test.ts`（2）：`crypto.randomUUID` 形态（36 字符 ≤ 服务端 64 上限）；
- `endpoints.test.ts`（5）/ `app.test.tsx`（1）：P0b 既有，未动。

### 6）工作项落地盘点与偏差（逐条登记）

**已落**：`lib/`（honesty/order/sha 三件）、`api/`（client/types + P0b 的
endpoints）、`state/session.ts`、`panels/workbench/` 六组件、
`components/SnapshotBadge.tsx`、App/main 重写（顶栏 + 工作台 + ConfigProvider
zh_CN）。

**偏差（与设计页 / 本 ADR 文面）**：

1. **`components/` 目录新增**（设计页 §4.1 树未预留）：顶栏徽标为跨面板共用件，
   避免 `panels/` 子目录间反向依赖；设计页已加「P1 落地注记」登记。
2. **设计页 §3.4 徽标表第 4 行纠错**：原写「不可用 → 服务 503」，实测 `/health`
   恒 200（degraded 走 200 + 5 键 null，键集与 ok 路径恒同）；503 属业务端点
   （`/ask` 构造 agent 绑定快照失败）。前端按状态事实渲染（error 色），设计页
   已就地纠错。
3. **`@vitejs/plugin-react` 复核结论**（P0b 注记第 1 节遗留项）：**不加**——
   真实组件树（1481 modules）下 esbuild 按 tsconfig `jsx: react-jsx` 转换，
   构建与 dev 走查均通过；Fast Refresh 属开发体验，维持最小依赖（新增须走
   AGENTS.md §5 流程）。
4. **react-router-dom 未引入**：P1 单面板无路由；若 P2 引入，按 AGENTS.md §5
   依赖流程执行（许可证 + 注记/ADR + 预算）。
5. **门禁 G3 降级**：`row_count == limit` 的确定截断标志未进 0022 契约（设计页
   §6.5 未裁定项），`TruncationNote` 用不确定语气且不改语气；降级记录进 README
   KL #33。

## 落地注记：P2 批次（2026-09-16）

覆盖范围：面板 2（`RoleSwitcher`）+ 面板 3（`SessionPanel`）+ 面板 4 前 6 子页
（models / metrics / dimensions / synonyms / values / policies）+ `ValuesDrawer`；
路由（`react-router-dom`）与全局单源状态（域 / 会话 / 身份）随之落地。

### 1）判据 10 完整断言（诚实性标志位全覆盖）

`make ui-check`（tsc + vitest + build）exit 0；vitest **8 文件 60 用例**：

- `honesty.test.ts`（15，P1 的 14 → 15）：补 §3.3 六条渲染义务的纯函数断言——
  `valueDomainGroup` / `valueSkipNote`（skip_reason 原文引用与缺失回落）、
  `partitionValueDomains`（status 驱动分组；空数组不得被读成「0 个值」）、
  `reportDegradeNote` / `dryNote` / `synonymsBanner` / `latestSnapshotSha`；
- `role.test.ts`（19，本批新增）：`decodeTokenPayload`（不验签；非三段 / 缺 role →
  null）、`identityKey`（keys 排序稳定）、`rolesForDomain`（按域过滤，矩阵不硬编码）、
  `claimFieldsOf`、`parseListClaimValue`（中英文逗号 / 空白）、`buildUserContext`
  （缺键如实列出、不补默认值）、`makeTokenCommand`（`$` 的 Make 转义 + 单引号跳出）、
  `formatClaimValue`、`ROLE_SWITCH_NOTICE` / `ROLE_UNREGISTERED_NOTICE` /
  `LIST_CLAIM_HELP` 三条文案逐字；
- `no-persist.test.ts`（1，本批新增）：`import.meta.glob` 读 `src/` 运行时代码原文，
  断言无 localStorage / sessionStorage / cookie / IndexedDB 的调用或赋值
  （§3.5 约束 3 的机械兜底；局限如实注明：不覆盖第三方包自写存储）；
- `session.test.ts`（5，P1 的 2 → 5）：`rotateSession`（切角色追加新会话）、
  `recordTurns`（轮数取 max、不缩小）；
- `endpoints.test.ts`（5，未动）/ `app.test.tsx`（1）：P2 更新——app 冒烟包
  `MemoryRouter`（App 起消费路由上下文），断言仍为「包含标题文本」，非渲染快照。

### 2）依赖引入：`react-router-dom` + `@types/node`（AGENTS.md §5 流程）

P1 注记第 4 节的遗留项，本批按 §5 四步（理由 / ADR 注记 / 许可证 / 预算）执行：

| 包 | 版本 | 许可证（node_modules 实测） | 理由 | 预算 |
|---|---|---|---|---|
| `react-router-dom` | 7.18.4 | MIT | 路由表（0018 判据 7 的 SPA 面 + 设计页 §2 的 5 路由） | 0（开源，无服务费） |
| `@types/node` | 24.13.5 | MIT | `vite.config.ts` 的 dev 签发中间件需要 `node:child_process` / `node:path` / `node:url` 类型（tsconfig `types` 增 `"node"`） | 0 |

两条均为 permissive 许可、无新增运行时服务面（前者进前端 bundle，后者仅类型期）。

### 3）dev 签发中间件：「前端不内置签发逻辑或密钥」的边界落地

P1 注记第 3 节与设计页 §3.5 尾段均写明「前端不得内置签发逻辑或密钥，dev 便利由
vite middleware 承担」。本批落地该 middleware（`vite.config.ts` 的 `devSignPlugin`，
`apply: "serve"` 仅 dev），边界：

- **vite 进程不读密钥**：中间件 `spawn("uv", ["run", "--env-file", <repo>/.env,
  "python", "-c", <sign_token 单行脚本>, role, context])`——与 `make token` 目标
  逐字同构；密钥只经 `.env` → `uv` → 子进程，不落 vite 内存、不写日志；
- **暴露面**：仅 `POST /__dev/sign`（role + context 白名单校验：string|number 标量
  或它们的数组，拒嵌套对象）；405（非 POST）/ 400（参数或签发失败，stderr 原文
  透传）/ 501（无法启动 uv）；**不属 0022 契约的 16 条**（判据 9 的正则不提取，
  `api/endpoints.ts` 的 `DEV_SIGN` 已登记该事实）；
- **不可用时降级**：`make serve-dev`（uvicorn 直服 dist）与容器无此端点 → 前端
  （`api/devsign.ts` 三态归一 ok / unavailable / error）降级为「复制 make token
  命令 + ②粘贴激活」，不伪造第二套签发逻辑；推翻条件第 2 条（出现多用户 / 真实
  鉴权需求时废除 middleware）不变；
- **实测**（2026-09-16，`make ui-dev`）：POST 合法 → 200 `{token}`（217 字符 JWT）；
  GET → 405；嵌套 context → 400；未注册角色 → 400（stderr 原文含
  `AuthError: 角色未注册：'nope'`）。

### 4）工作项落地盘点与偏差（逐条登记）

**已落**：`state/role.ts`（+`formatClaimValue`）、`state/session.ts`（5 函数）、
`api/devsign.ts`、`api/endpoints.ts`（+`DEV_SIGN`）、`vite.config.ts`（+中间件）、
`components/`（+`CopyBlock` / `ErrorNote`）、`panels/role/RoleSwitcher.tsx`、
`panels/sessions/SessionPanel.tsx`、`panels/governance/`（`GovernanceLayout` +
`GovernanceSection` 共用件 + 6 子页 + `ValuesDrawer`）、`App.tsx` / `main.tsx`
路由化、`AskWorkbench` 受控化（token / model / sessionId 上收 + `onTurnSeen` 回报）。

**偏差（与设计页 / 本 ADR 文面）**：

1. **401 实测使激活引导顺序收窄**（设计页 §3.5 第 409 行）：设计页代价句写「刷新
   页面后需重新点一次角色」——实测治理端点一律 Bearer（2026-09-16：无 token 请求
   `/api/v1/governance/policies` → 401，直连与经代理均同），**取不到角色矩阵就无法
   就地签发**；故刷新后的正确路径是「重新粘贴一次 `make token` 输出」，面板内签发
   仅在已认证状态可用。设计页该行已加落地注记；
2. **2 个 Drawer 消解为 1 + 1**：P2 落值域钻取（`ValuesDrawer`）；报告钻取 Drawer
   随 `reports` 子页归 P3（设计页 §2「2 个 Drawer」按此读）；
3. **治理面「挂载即发」P2 = 6 条**：`reports` / `snapshots` 两条随其子页属 P3
   （0022 决策 ⑥ 的 240/min 推导按终态 8 条；P2 阶段 6 条已写入 README KL #34 ④）；
4. **会话时间线的限制横幅是新渲染义务**（与 README KL #34 ① 同源）：
   `SessionPanel` 顶部常驻 Alert（未开端点 / 只列本次运行 / 列出历史需新端点且权限
   口径未裁定），**不得删除或弱化**。

### 5）收口实测证据（2026-09-16，HEAD `eecefb7`）

- `make ui-check`：tsc exit 0；vitest 8 文件 60 用例全绿；build 1505 modules；
- **三条收口判据（浏览器走查 12 步，console 零报错）**：① 值域子页两组
  （已覆盖 10 / 已跳过 11）**默认展开**，skipped 行显示
  「值域未采集：distinct=2715 超过阈值 200」原文
  （`docs/screenshots/acceptance-step9-values-expanded.png`）；② 激活 hq_admin →
  切 branch_manager（dev 签发 + 表单 claims），会话 id `0bd79a6c…` → `5ebd97b2…`，
  `/sessions` 三行且当前行角色 = branch_manager；③ 刷新后顶栏回「未认证 · 激活身份」，
  `/governance/models` 显示 401 引导（非表格）；
- 治理 6 子页数据实测：models 2 条 / metrics 20 条 / dimensions 49 条 / values
  21 条（10 覆盖 + 11 跳过）/ policies 2 策略 7 角色（全 `registered: true`）；
  synonyms zh_cn 空占位横幅引用 `authority_note` 原文；`reports` / `snapshots`
  未开放说明与未知子页说明按设计返回；
- 快照侧：走查时点 health 报 `snapshot_source=latest` + `bound_to_head=false`
  （橙色警示态，「未绑定 HEAD，取自最新已锁」）。

**提交**：`feat(frontend)` + `docs`（本注记、README 中英 KL #34、设计页 P2 注记、
dev-plan §2.7 回执、走查截图 3 张）。

## 落地注记：P3 批次（2026-09-16）

覆盖范围：面板 5 图表接入（`panels/chart/ChartBlock.tsx` + `lib/chart.ts`，ADR-0025
决策 ①③④⑥ 的前端侧）+ 面板 4 收尾两子页（`ReportsSection` / `SnapshotsSection` +
`ReportDrawer`——P2 注记偏差 2 的「2 个 Drawer 消解为 1 + 1」遗留项）+ `order.ts`
次序表补第 6 段 `chart`（决策 ⑦：… → 数据 → 截断声明 → 图表）。

### 1）判据 9/10 完整断言（治理面与工作台全覆盖）

`make ui-check`（tsc + vitest + build）exit 0；vitest **10 文件 82 用例**
（P2 的 8 文件 60 → 新增 2 文件 20 用例 + `honesty.test.ts` +2）：

- `chart.test.ts`（11，本批新增）：`chartSeries` 零推断映射（xKey/yKey 原样取自
  spec、行键恒为 `"x"`/`"y"`、保持响应行序、y 只取首个——0025 决策 ①⑥ 的前端侧
  机械兜底）、`chartNumber`（Decimal 字符串 → number；坏数 → null，不补 0）、
  `tableFallbackLines`（table 分支 note 恒渲染 + skipped 计数）；
- `report.test.ts`（9，本批新增）：`flattenKv`（任意嵌套平铺为点分路径行；空对象
  显式成行；顶层非对象不发明键名——非主报告降级渲染的机械兜底）、`sampleColumns`
  （首选键序 + 全键并集，不挑好看的键）；
- `honesty.test.ts`（15 → 17）：+2 = 快照徽标文案逐字（「最新（按 created_at）」
  防文件名排序陷阱）+ 报告清单分组由 `structured` 驱动保序（不重排响应）；
- `order.test.ts`（6，断言增强）：answer 段落次序补「数据 → 截断声明 → 图表」
  （决策 ⑦ 锁定；`ANSWER_SECTIONS` 现为 6 段：metric/counts/sql/data/truncation/chart）。

### 2）图表接入的 0018 侧义务（决策 ⑦ 次序 + 零推断）

工作台图表位置由 `order.ts` 的 `ANSWER_SECTIONS` 锁定：图表在**数据与截断声明
之后**（决策 ⑦「数据可查先于图形摘要」）。前端零图表决策：spec 的
`type`/`x`/`y`/`note` 原样消费，x/y 只作语义标签与行键映射，不推断时间列、不重算
行序——「时间轴列以编译声明为权威」的前端侧由此保证（运行时权威在
`Compiler.emitted_time_column` → `render_chart` 的 `time_columns`，见 ADR-0025）。

### 3）走查修复（2 个前端缺陷，随 feat 提交）

① `ChartBlock` 误将 spec 语义列名（如 `d_year`）用作 Recharts `dataKey`，而行键
恒为 `"x"`/`"y"` → 全部数据点取不到值（曲线无 `d` 路径、两轴刻度为空）；② `YAxis`
默认 60px 宽截断亿级刻度（`"100000000"` 显示为 `"0000000"`）→ `width={90}`。两
缺陷均属**契约消费错误**（不是「前端零图表决策」被破坏），修复含回归用例
（`chart.test.ts`「行键恒为「x」「y」」）。

### 4）收口实测证据（2026-09-16；截图 6 张随本批 docs 提交）

- `make ui-check`：tsc exit 0；vitest 10 文件 82 用例全绿；build 2300 modules；
- **浏览器走查**：工作台（激活 → retail →「2001 年销售额同比」）图表 line 渲染
  （曲线 `d` 路径、2 数据点、两轴刻度完整）+ note 与 footer
  （`x=d_year · y=total_sales_price · 出口 SQL 摘要`）+ 行级策略与 CTE SQL 在位
  （`docs/screenshots/p3-walkthrough-1-ask-chart.png`）；治理面 `reports` 58 条
  （14 主 + 44 非主）两级渲染 + 降级 drawer（Alert「降级展示（非统一结构报告）」+
  模式标签 + 原始 JSON 原文）与主报告 drawer（`p3-walkthrough-2~5-*.png`）；
  `snapshots` 19 条、「最新（按 created_at）」徽标仅首行
  （`p3-walkthrough-6-snapshots-badge.png`）；console 全流程零报错；
- 真链侧（0025 判据 4 + 移交项）见同批 docs 提交的 dev-plan §2.8 回执与 ADR-0025
  「移交完成」注。

**提交**：`69d6288` `refactor` + `398bce1` `feat(chart)` + `docs`（本注记、README
中英 KL #21 追加/#35、ADR-0017 ⑦ 补强、ADR-0025 移交完成与补登记、设计页 P3 注记、
dev-plan §2.8 回执、走查截图 6 张）。
