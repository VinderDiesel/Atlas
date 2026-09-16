# ADR-0024：Cube 定位为导出目标（接口兼容），运行时引擎化设门禁

- 日期：2026-09-14
- 状态：accepted（决策经用户确认，2026-09-14；本 ADR 只裁**定位**，不含落地代码，落地批次见「验证方式」。本线与前端批次（P-1~P3）**无顺序依赖**，故在 `docs/design/dev-plan-0017-0025.md` 中列为独立工作线）
- 相关：
  - ADR-0002（Ossie 语义规范——Cube 曾作为被拒的**规范**备选；本 ADR 只裁 Cube 的**运行时引擎**定位，不改「Ossie 为主规范」）
  - ADR-0004（Apache 全栈——单 OLAP = Doris；ASF 治理偏好）、ADR-0005（Calcite 取舍——「引擎类组件延后 + 门禁」的先例范式）
  - ADR-0013（计算执行面 YAGNI + profile 门禁先例）、ADR-0014 ⑤（行级维持 Guard 谓词层为**架构终局**）
  - ADR-0003（只读网关）、ADR-0011（安全分层）、ADR-0017（时间智能：yoy/pop/cumulative/rank 的窗口/CTE 语义）
  - ADR-0018（前端控制台消费 `/api/v1`，明确拒绝绕过 Guard 的数据源）、ADR-0021（row-policy 二维来源）、ADR-0022（HTTP 契约 v2：`/api/v1` 多 API + 8 治理端点）
  - README §4.4（Cube = 接口兼容目标）、README 铁律 2（查询引擎是可替换执行面，语义层不绑定 Doris/ClickHouse/Trino）
  - 既有资产：`agent/tools/mcp_server.py`（MCP 风格工具服务）、`serving/api.py`（HTTP 服务面）、`agent/compiler.py`（Plan→SQL 确定性编译器）、`agent/security/sql_guard.py`（只读 Guard）、`semantic/export_dbt.py`（Ossie→dbt 导出器，本 ADR 新增导出器的类比样板）
  - 事实基线：HEAD = `bdcb6c0`（2026-09-14 核实）

---

## 背景

诉求：引入 cube.dev（`github.com/cube-js/cube`）配比 Apache Ossie，「增强语义层的引擎能力」。
经完整设计树盘问（角色 → 归属 → 事实源 → Guard → 预聚合 → 可复现 → 部署 → 许可证 →
converter 保真 → 缺口 → 重走架构 → Guard 等价 → 终局）后，核实到三条决定本 ADR 的事实：

### 1. Cube 与 Ossie 不在同一层，「配比」需先辨析层次

Ossie 是**定义/交换规范**，明确「不是运行时、不执行查询、不在查询路径上」（ADR-0002 边界 1）。
Cube 是**运行时合体**：语义模型 + SQL 生成 + 编排 + 缓存 + 多 API（REST/GraphQL/SQL-API/MCP）。
故 Cube **不是** README 铁律 2 所指的可替换「查询引擎」（那是 Doris/ClickHouse/Trino 的 SQL 执行层），
而是压在 **Ossie/compiler 层**——引入它会与 Atlas 刻意分离的 `定义(Ossie) / 生成(compiler) / 执行(Doris)`
三层解耦相冲突，而非补进铁律 2 的可替换槽位。

### 2. 许可证核实（2026-09 检索）：Apache-2.0 *许可* ≠ ASF *治理*

- Cube **Backend = Apache-2.0、Client = MIT**（许可证与本项目 Apache-2.0 兼容，ADR-0023）。
- 但 **Cube 非 ASF 项目**，由 Cube Dev Inc. 单厂商治理；部分能力企业 / Cube Cloud 门控；
  2026-05 存在「Future of Cube Core」公开不确定性讨论。
- ADR-0004「全栈 Apache」看重的是 **ASF 治理**（Ossie / Polaris / Iceberg / Doris 均 ASF）；
  Cube 属「Apache-2.0 许可但厂商治理」，与该叙事有实质区别，须诚实辨析。

### 3. 缺口核实：所称「多 API/MCP 能力缺口」已大部分自建覆盖，且无具名消费者

- `agent/tools/mcp_server.py` 已提供 MCP 风格 `list_tools/call_tool`（transport 层设计上预留「挂 SDK 即可」）；
  `serving/api.py`（ADR-0012）已提供 HTTP 服务面。
- ADR-0022 正在自建 `/api/v1` 多 API + 8 治理端点；ADR-0018 前端消费该 REST 面，
  并**明确拒绝**任何绕过 Guard 的数据源（Grafana SQL 数据源、Streamlit 因 N3 被拒）。
- 本 ADR 探查范围内（代码库 + 全部 ADR 0001~0023）**未见具名 BI 工具 / GraphQL 消费者**；
  唯一在飞消费者（ADR-0018 前端）走 REST，用不到 Cube 的 Postgres-wire SQL-API 与 GraphQL。

约束：AGENTS.md §5（新依赖四步：理由 / ADR / 许可证 / 预算）、§10（性能最后且需 profile；
简洁不过度设计）、§12（优化性能先要 profile）、N1/N2/N3/N6/N8；ADR-0013 已确立的
「引擎 / 抽象层 YAGNI + profile 门禁」先例。

## 备选方案

| 方案 | 优势 | 劣势 |
|---|---|---|
| **① Cube = 离线导出目标 / 接口兼容目标（选定）** | 与 ADR-0002、README §4.4 已登记姿态一致；零红线代价（不动 compiler / Guard / 只读 / 快照）；兑现「非封闭」与未来接入 Cube 生态的扩展性；上游风险被隔离在离线路径 | 不带来运行时收益（不加速、不提供在线多 API——那是 ADR-0022 REST 的职责）；导出器是新增工程量 |
| ② Cube = 运行时引擎（替代 compiler + 退役 Guard + queryRewrite 重建安全）——盘问走出的完整路径 | 一站式拿到 SQL-API/GraphQL/MCP + 预聚合加速 | 重写 ADR-0003/0011、supersede ADR-0014 ⑤、与 ADR-0021/0022/0018 在飞批次正面对撞；N3 只读 chokepoint 需按 §14 修订 + 补偿控制；从零无损 converter 成新核心且无官方实现；Node/Rust 多语运行时；6 条承重可行性假设未验证 |
| ③ Cube Cloud（托管 SaaS）免运维 | 零运维 | 直接撞 ADR-0004 原则 2「不依赖闭源或 SaaS、任何人可复现」；数据离开单机（合规风险） |
| ④ 现在自建 SQL-API / GraphQL shim 补「多 API」 | 完全自控、守 Guard | 无具名消费者，ADR-0013 门禁未触发即立项 = YAGNI 违规；与 ADR-0022 REST 面重复 |

## 决策

### ① 定位：Cube 维持「离线导出目标 / 接口兼容目标」，不进查询路径

Cube 保持在 ADR-0002 与 README §4.4 早已登记的位置。落地形态 = 新增 `Ossie → Cube model`
**导出器**（类比既有 `semantic/export_dbt.py` 的 dbt 导出），产物为 **generated artifact**
（禁手改，沿 `airflow/dags/generated/` 的 N5 纪律），用途是**证明语义层非封闭**（可被 Cube
生态消费），**不接入 Agent 查询路径**。

### ② 查询路径逐字不变

Agent 仍走 `Planner → Compiler（agent/compiler.py）→ Guard（sql_guard.py）→ Doris`
（ADR-0004 铁律 2 的可替换执行面）。**compiler 不退役、Guard 不退役、N3 只读红线不动、
ADR-0014 ⑤ 行级 Guard 谓词层终局不动、ADR-0021 row-policy 来源不动。**

### ③ 运行时引擎化：延后 + 设门禁（非否决）

「Cube 作为运行时引擎」列为**门禁触发后才评估**的 Phase 2+ 项，触发条件见「什么情况下应该推翻」。
门禁范式对齐 ADR-0005（Calcite「什么时候该用」清单）与 ADR-0013（计算执行面立项触发条件）。

### ④ 许可证与叙事登记

Cube 为 **Apache-2.0 许可但非 ASF、单厂商治理**。本 ADR 只登记导出目标定位，不改 README §4.4。
若未来引擎化立项，须按 §5 ③ 逐项验证 SQL API / queryRewrite / MCP 是否在 Apache-2.0 核心
（非企业门控），并把「全栈 Apache」叙事诚实调整为「ASF 核心 + 一个厂商治理的 Apache-2.0 组件」。

## 理由

1. **必要性当前不成立**：缺口（多 API/MCP）已被 `mcp_server.py` + `api.py` + ADR-0022 覆盖，
   且探查范围内无具名 BI/GraphQL 消费者（背景 §3）。给已解决问题引入第二套运行时 = 过度设计（§10 优先级 5）。
2. **与在飞批次撞车**：ADR-0018/0021/0022 均 accepted 但**未落地**；引擎化会重写 ADR-0003/0011、
   supersede ADR-0014 ⑤、与 ADR-0021/0022/0018 对撞。单人 8 周 + 一大批 ADR 未落地时再叠加，范围与风险失控。
3. **违反既有门禁纪律**：ADR-0013 对「加引擎/抽象层」已确立 YAGNI + profile 门禁；当前两个触发条件
   都不满足（无 profile 显示 Doris 瓶颈；无第二个引擎消费者）。现在引擎化 = 破自己立的纪律。
4. **分层解耦是资产**：铁律 2 的精神是语义层不绑定引擎。Cube 把 定义/生成/执行 揉成一层，
   反而降低未来换执行引擎（Doris→Trino）或换规范（Ossie→dbt）的扩展性。
5. **导出目标定位零红线代价、且正确兑现扩展性**：保留未来接入 Cube 生态的能力（证明非封闭），
   而不现在就把运行时引擎、只读 Guard、compiler 差异化全押上——这正是 README §4.4 与 ADR-0002 的姿态。

## 代价与限制

- **导出器是新增工程量，且映射非机械**：Ossie 是 expression-carrying（metric = 每方言一整条 SQL），
  Cube 要 measure/dimension/join 分解；ADR-0017 时间智能（窗口/CTE 语义）在 Cube 未必 identical 可表达。
  作为**导出目标**（不进查询路径）可 fail-closed：不可忠实映射的 metric **跳过导出并记录**，不影响 Agent 服务。
  （对比引擎化路径：同样的 fail-closed 会导致 Cube 覆盖 <100% gold 且直接影响服务——这是导出目标定位的关键降级收益。）
- **导出目标不带来运行时收益**：不加速查询、不提供在线多 API。若把「导出成功」读成「Cube 引擎能力已具备」
  即 N2 时态错误——本 ADR 落地物是离线导出器，不是运行时引擎。
- **Cube 上游不确定性**（非 ASF、单厂商、2026-05 core 未来公开讨论）：导出目标定位下该风险被隔离——
  产物是离线的、非关键路径，Cube 变动不影响 Agent 服务与评测可复现。
- **门禁可能被误读为「永不引擎化」**：本 ADR 是「延后 + 设门禁」，非否决；触发条件满足即应重评（见下节）。

## 什么情况下应该推翻

> 满足 ① **或** ②，**且** ③ 成立时，推翻本 ADR「仅导出目标」定位，立项评估 Cube 运行时引擎化：

① 出现 REST（ADR-0022 `/api/v1`）满足不了的**具名** BI / GraphQL / 数据应用消费者
（须写进新 ADR，具体到工具名 + 端点，非声称）——例如 Tableau / Superset 需 Postgres-wire SQL-API 直连语义层；
② **profile 数据**证明 Doris 直查在真实负载下成瓶颈、需预聚合亚秒加速（§10/§12：先 profile，禁凭直觉）；
③ 且届时**优先评估「Cube 作为额外出口」**（Ossie→Cube 导出 + Cube 服务该具名消费者，Agent 路径保留
compiler + Guard），**而非「Cube 替代 compiler + 退役 Guard」**——以最小化对确定性优先与 N3 只读红线的冲击。

**引擎化若立项，落地前必须先跑可行性 spike（不得声称已通过，N1）**：
(a) Cube 有可用的**只读 Doris 驱动**（MySQL 协议）？
(b) SQL API / queryRewrite / MCP 是否都在 **Apache-2.0 核心**（非企业门控）？
(c) Cube 是否暴露**输出 SQL**（`/sql` dry-run 或日志）以供薄只读断言（纵深防御残留层）？
(d) Ossie→Cube 能否分解 expression-carrying 指标 + 编译 row_policy→queryRewrite（含 ADR-0014 C7 跨表）+ 保真 ADR-0017 时间智能？
(e) 若 Guard 退役，`rls_verify.py`/`rbac_verify.py` 能否重指 Cube API 并**证明** queryRewrite ≥ Guard（N3 §14 补偿控制）？

④ **反向推翻**：若 Cube 上游许可证转为非 permissive，或导出器维护成本持续高于「证明非封闭」的收益
→ 移除 Cube 导出目标，回退到仅 dbt MetricFlow 导出（`semantic/export_dbt.py` 既有）。

## 验证方式

- **导出器落地批次**：新增 `make export-cube`（类比 `make export` → dbt），产物落 `exports/cube/`；
  契约测试 `tests/test_export_cube.py` 断言：可映射 metric 数、fail-closed 跳过清单（含 ADR-0017 比较类若不可映射）、
  governance（owner / version / lineage）随 Cube `meta` 无损、不可映射项被**跳过而非近似**（对齐 N8 口径唯一）。
- **非封闭证明**：导出产物能被 Cube 加载并列出 metric（离线校验，不接 Agent 查询路径）。
- **门禁未被绕过（本 ADR 的核心可证伪点）**：`make test` 全绿中，Agent 查询路径的 compiler / Guard 契约测试
  （`tests/test_compiler.py`、`tests/test_sql_guard.py`、`make rls-verify`、`make rbac-verify`）**逐字未改**——
  以 `git diff` 断言，证明本 ADR 未动查询路径与只读红线（N3）。
- **证伪条件**：若发现代码库 / ADR 中已存在具名 BI / GraphQL 消费者（本 ADR 声称未见），
  或已有 profile 证明 Doris 直查瓶颈，则「当前不必要」结论被证伪，须按「什么情况下应该推翻」重评引擎化。
