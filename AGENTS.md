# AGENTS.md — Atlas 项目协作契约

> 本文件是给 **AI 编码助手（Cursor / Claude Code / Copilot / Codex / Qoder/ Kiro）** 与**人类协作者**共同遵守的项目契约。
> **优先级最高**：本文件的"禁止事项"高于一切代码风格、便利性与个人习惯。
> 修改本文件需要显式说明理由，并在 commit message 中标注 `[contract]`。

---

## 0. 三句话理解这个项目

1. Atlas 是**以 FIBO 本体为语义锚点的金融可信 AI 问数平台**：业务术语 → FIBO 概念 → 指标计划 → 安全查询 → 可解释结果 → 评测回流。
2. **确定性优先**：已知指标由语义编译器生成 SQL（不经过 LLM）；LLM 只是候选生成器，必须过校验。
3. **诚实是不可协商的红线**：任何没有计算脚本的数字都不许写进 README / 报告。

---

## 1. 绝对禁止（NEVER DO）

违反以下任一条，代码一律不合并：

| # | 禁止事项 | 原因 |
|---|---|---|
| N1 | **生成或补全任何未经实测的数字**（准确率、Recall、QPS、成本下降、性能提升） | 这是项目的生命线。数字只能来自脚本产物 |
| N2 | **把设计写成已完成**（使用"已实现/上线/达成"描述未验证功能） | 时态错误 = 造假风险 |
| N3 | **绕过只读 SQL 网关**（任何 INSERT/UPDATE/DELETE/DROP/ALTER/GRANT/COPY） | 安全红线，纵深防御 |
| N4 | **删除或美化 `Known Limitations` 章节** | 诚实性的核心证据 |
| N5 | **手工编辑 `airflow/dags/generated/`** | 该文件由 YAML 生成，会被覆盖 |
| N6 | **把 `data/snapshots/` 外的数据库当作评测基准** | 评测必须绑定固定快照 sha |
| N7 | **引入雇主真实数据/代码/指标定义** | 合规风险，项目声明明确排除 |
| N8 | **在语义层创建同名 active 指标** | 违反"同一业务词只有一个权威定义" |
| N9 | **硬编码密钥、连接串、token** | 一律走环境变量 / `.env`（`.env` 已 gitignore） |
| N10 | **把 Spider/BIRD 分数当作企业场景分数混报** | 口径不同，必须分开报告 |

---

## 2. 项目元信息

| 项 | 值 |
|---|---|
| 项目名 | Atlas |
| 定位 | 以 FIBO 为语义锚点的金融可信 AI 问数平台（统一语义层 + 受控 NL2SQL + 可解释 Data Agent） |
| 状态 | under active development（8 周计划） |
| 主语言 | Python 3.11（辅助：SQL、YAML、少量 Java/Scala 用于 Spark） |
| 数据 | TPC-DI 零售经纪（公开基准，ADR-0006）、FIBO 本体 FND+FBC+BE 域（L2 概念对齐锚点，ADR-0007）、BIRD finance（公开对照）、自建黄金集（人工标注） |
| 预算约束 | 个人可承受：单机 + 按量云资源；单卡 24G 起（仅 LoRA 阶段需要） |

---

## 3. 术语表（必须统一使用，禁止自创同义词）

| 术语 | 含义 | 禁止混用的词 |
|---|---|---|
| **Metric** | 已注册的指标定义（YAML 一等对象） | "字段"、"别名"、"标签" |
| **Measure** | 基础度量，可被聚合的物理列 | "指标"（易与 Metric 混淆） |
| **SemanticModel** | 指标与维度绑定的逻辑模型，指向物理表 | "宽表"、"模型"（后者留给 ML 模型） |
| **Dimension** | 可分组/筛选的业务维度 | "字段" |
| **Plan**（指标计划） | 问句解析后的结构化意图（metric/dimension/time/filter） | "Query"（后者指 SQL） |
| **Compiler** | Plan → SQL 的确定性转换器 | "生成器"（后者指 LLM 生成） |
| **Generator** | LLM/LoRA 候选 SQL 生成器 | "编译器" |
| **Guard** | 只读 SQL 安全网关 | "防火墙"（保留给网络层） |
| **EX** | Execution Accuracy，执行结果与快照一致率 | "准确率"（不精确） |
| **Plan Acc** | 逻辑计划与标注一致率 | — |
| **Gold set** | 自建黄金评测集（50 例） | "测试集"（含糊） |
| **Snapshot** | 固定数据快照，绑定 git sha | "数据库"（当前状态会变） |
| **RowPolicy** | 行级权限策略，可编译为 SQL 谓词 | "权限表" |
| **Agent** | LangGraph 状态机编排 | "Bot"、"助手" |
| **FIBO** | Financial Industry Business Ontology，金融业务本体，项目的语义锚点（ADR-0007） | 泛指时的"本体" |
| **fibo_alignment** | 语义对象 → FIBO 概念 IRI 的映射（L2 概念对齐层，挂 custom_extensions） | "映射"（含糊）、"标签" |

---

## 4. 目录职责与写入权限

```text
atlas-data-platform/
├── semantic/            # 语义层定义（Git 唯一事实源）
│   ├── schema/          # JSON Schema，CI 强制校验
│   ├── models/          # SemanticModel：base_table / measures / joins
│   ├── metrics/         # Metric 定义（核心资产）
│   ├── dimensions/      # Dimension 定义
│   ├── synonyms/        # 同义词表（业务术语 → Metric/Dimension）
│   ├── policies/        # RowPolicy 行级权限
│   └── migrations/      # 语义层变更记录（版本演进）
├── sql/
│   ├── tpcds_ddl/       # TPC-DS 原始 DDL（零售历史，只读，勿改）
│   ├── dwd/             # 明细层
│   ├── dws/             # 汇总层
│   └── views/           # 消费视图
├── spark/
│   ├── extractors/      # DDL/ETL 注释 → 语义对象候选
│   └── metadata_parser.py
├── airflow/
│   ├── yaml_jobs/       # ✅ 可编辑：YAML 任务编排源
│   └── dags/generated/  # ❌ 禁止手改：由 yaml_jobs 生成
├── agent/
│   ├── state.py         # LangGraph 状态定义
│   ├── graph.py         # 状态机：clarify→retrieve→plan→generate→validate→execute→explain
│   ├── planner.py       # 问句 → Plan
│   ├── compiler.py      # Plan → SQL（确定性）
│   ├── security.py      # 只读 Guard
│   ├── tools/           # Agent 可调用工具（确定性优先）
│   └── prompts/         # 提示词（Git 版本管理，禁止内联在代码里）
├── retrieval/           # bm25 / milvus_client / graph_store
├── serving/             # api / auth / gateway
├── observability/       # otel 埋点 / grafana dashboards
├── eval/
│   ├── gold/            # 自建黄金集（主评测）
│   ├── spider/ bird/    # 公开集对照（仅参照，不混报）
│   ├── runner.py        # 评测执行器
│   ├── failures/        # 失败样本（人工确认后进 SFT）
│   └── reports/         # 每次 commit 产物，文件名 = commit sha
├── lora/                # SQL 适配器训练 + 数据飞轮
├── infra/
│   ├── docker/          # 镜像与 compose 片段
│   ├── ci/              # GitLab CI / GitHub Actions
│   └── adr/             # ✅ 架构决策记录（重要决策必须写 ADR）
├── docs/                # 设计文档、逐日任务清单、术语表
└── data/snapshots/      # 固定数据快照，记录 sha
```

---

## 5. 技术栈锁定（Apache 全栈，变更需 ADR）

| 层 | 选型 | 版本 | 替代方案 | 状态 |
|---|---|---|---|---|
| **语义规范** | **Apache Ossie** | 0.2.0.dev0 (DRAFT) | dbt MetricFlow / Cube | 锁定（ADR-0002） |
| **治理扩展** | Atlas 自研（挂 Ossie custom_extensions） | — | — | 锁定 |
| **Catalog** | **Apache Polaris** (Incubating) | latest | Iceberg hadoop catalog（降级路径） | 锁定（ADR-0004） |
| **表格式** | **Apache Iceberg** | V2（V3 待验证） | Hudi（已有经验） | 锁定（ADR-0004） |
| **OLAP** | **Apache Doris** | 4.1 | ClickHouse（降级路径） | 锁定（ADR-0004） |
| **对象存储** | MinIO（S3 兼容） | RELEASE.2024-05-28 | — | 锁定 |
| **SQL 解析** | sqlglot | ≥ 25 | — | 锁定（ADR-0005：Calcite 列 Phase 2） |
| **向量检索** | Milvus | 2.4 | Doris 原生向量检索（可选对比） | 锁定 |
| 语言 | Python | 3.11 | — | 锁定 |
| 依赖管理 | uv | latest | pip | 锁定 |
| 关系库 | PostgreSQL | 16 | — | 锁定（Polaris/元数据） |
| 编排 | Airflow | 2.9 | Dagster | 锁定 |
| Agent | LangGraph | ≥ 0.2 | 自研状态机 | 锁定 |
| 计算 | Spark / Flink | 3.5 / 1.18 | — | 锁定 |
| 可观测 | OpenTelemetry | ≥ 1.24 | — | 锁定 |
| 图存储 | NetworkX（MVP） | — | Neo4j | MVP 够用 |
| LLM 推理 | vLLM | ≥ 0.6 | — | 锁定 |
| 容器 | Docker Compose | ≥ 2.20 | K8s | MVP 用 Compose |

### 5.1 关键边界（改动前必须重读）

| 项目 | 边界 | 后果 |
|---|---|---|
| **Ossie** | 不是运行时，不执行查询、不在查询路径上 | **Compiler 必须自己写**，Ossie 不替代它 |
| **Ossie** | expression-carrying，非 expression-defining | 需实现方言选择；只写 ANSI_SQL 则其他方言不可用 |
| **Ossie** | 不含 lineage / ownership / freshness | 由 Atlas 治理扩展（custom_extensions）补齐 |
| **Polaris** | 孵化器项目 | 有不确定性；降级路径为 Iceberg hadoop catalog |
| **Doris** | FE+BE 双进程，内存压力大 | 单机跑不动则降级 ClickHouse（保留 Iceberg+MinIO） |
| **Calcite** | Java-only，需 JVM | MVP 不引入（ADR-0005），Phase 2 评估 |

**引入任何新依赖必须**：① 说明理由 ② 写 ADR ③ 评估许可证 ④ 确认个人预算可承受。

---

## 6. 常用命令（Agent 执行前先确认）

```bash
make install     # 安装依赖
make up          # 启动 PostgreSQL / ClickHouse / Milvus / Grafana
make down        # 停止
make seed        # 生成 TPC-DI 数据并建仓
make lint        # 语义层校验（Schema + 唯一性 + 血缘 + 权限）
make plan    Q="2013 年第二季度总交易额"   # 问句 → Plan（不执行 SQL）
make compile                    # Plan → 只读 SQL
make eval                       # 跑评测，产出 eval/reports/<sha>.json
make train                      # 用确认后的失败样本训练 LoRA
make report                     # 生成 EVAL_REPORT.md
make test                       # 单元 + 契约测试
make adr TITLE="xxx"            # 新建 ADR 模板
```

**Agent 注意**：
- 不要凭记忆编造命令，先读 `Makefile` 确认目标存在
- 不要执行 `make seed` 除非用户明确要求（耗时且会重置数据）
- 不要执行任何 `docker compose down -v`（会删数据卷）

---

## 7. 代码规范

### 7.1 Python

- 格式化：`ruff format`（不是 black）
- Lint：`ruff check`，必须零 error
- 类型：新增函数**必须**有类型注解；`mypy` 严格模式对 `agent/`、`semantic/`、`eval/` 生效
- 命名：模块/函数 `snake_case`，类 `PascalCase`，常量 `UPPER_CASE`
- 文档字符串：公共函数必须有，说明 **做什么 / 参数 / 返回 / 抛什么异常**
- 注释：解释 **为什么**，不解释 **做了什么**（代码本身已说明）

### 7.2 SQL

- 关键字大写（`SELECT` / `FROM` / `WHERE`）
- 生成的 SQL 必须可被 `sqlglot` 往返解析
- 所有生成的 SQL **必须**带 `LIMIT` 与时间范围约束
- 禁止字符串拼接构造 SQL，一律走 AST 操作

### 7.3 YAML（语义层）

- 2 空格缩进，禁止 Tab
- 每个文件顶部注释说明 owner 与最后修改原因
- 必须通过 `semantic/schema/*.schema.json` 校验
- 日期用 ISO 8601，时区显式声明（`+08:00`）

### 7.4 提示词

- **禁止内联在 Python 代码里**，一律放 `agent/prompts/*.yaml`
- 每个提示词文件必须包含：`version` / `owner` / `description` / `changelog`
- 提示词变更必须触发 CI 回归评测（PromptOps）

---

## 8. 提交规范

```
<type>(<scope>): <subject>

[可选正文]

[可选脚注]
```

**type 取值**：

| type | 用途 |
|---|---|
| `feat` | 新功能 |
| `fix` | 缺陷修复 |
| `semantic` | 语义层定义变更（新增/修改指标、维度） |
| `eval` | 评测集或评测逻辑变更 |
| `sec` | 安全相关（Guard、权限、只读） |
| `docs` | 文档 |
| `refactor` | 重构（无行为变化） |
| `chore` | 依赖、CI、工具 |
| `contract` | 修改 AGENTS.md 本身 |

**示例**：

```
semantic(metrics): 新增 gmv 指标的月粒度聚合定义

- 补充 time_granularity: month
- 关联 gold/gmv_by_region_month.yml 作为准确性测试用例
- 行级策略沿用 rp_dept_visible

Refs: #12
```

**规则**：
- 每个 PR 自动生成：评测摘要、性能摘要、安全摘要、破坏性变更摘要
- `semantic/` 目录变更必须说明影响面（哪些指标/报表受影响）
- 禁止一个 PR 混合 `refactor` + `semantic`（语义变更需要单独评审）

---

## 9. 数字与声明的规范（最重要的一节）

### 9.1 允许的写法

```
✅ 自建 50 例黄金集上 EX = <实测值>，计算方式为执行结果与固定快照一致
✅ 数据快照 sha = abc1234，评测脚本 eval/runner.py，报告 eval/reports/abc1234.json
✅ TPC-DI 15 表子集上 Table Recall@K = <实测值>
✅ Spider dev EM = <实测值>（公开集，仅作参照）
```

### 9.2 禁止的写法

```
❌ 准确率达到 95%（无计算脚本）
❌ 显著提升 / 大幅优化 / 大幅降低成本（无量化口径）
❌ 支持 1000 张表的 NL2SQL（未验证）
❌ 企业级 / 生产级 / PB 级（MVP 不满足）
❌ 基于 XX 模型，效果优于 YY（无对照实验）
```

### 9.3 占位符约定

文档与模板中未完成数字一律写成 `<待填写>` 或 `<实测后填写>`，
**Agent 不得自作主张填入看似合理的数字**。

---

## 10. Agent 工作时的决策优先级

遇到冲突时，按此顺序取舍：

1. **诚实**（不编造、不夸大）— 最高优先级
2. **安全**（只读、权限下推、纵深防御）
3. **可复现**（固定快照、脚本化、commit 可追溯）
4. **确定性优先**（能用编译器就不用 LLM）
5. **简洁**（不过度设计，不为炫技引入复杂度）
6. **性能**（最后考虑，且必须基于 profile 而非直觉）

---

## 11. 新建功能时的标准流程

Agent 被要求实现新能力时，按此顺序执行：

1. **确认是否已有 ADR**：无则先写 ADR 模板（`make adr`）
2. **确认能力来源**：是复用已有经验，还是需新建？（更新 README 第 11 节登记表）
3. **先写评测**：在 `eval/gold/` 或 `eval/` 下加测试用例，**再写实现**
4. **最小实现**：先跑通 happy path，不提前优化
5. **加安全约束**：如果涉及 SQL 执行，必须过 Guard
6. **加可观测**：埋 OTel span，记录 token_cost
7. **更新文档**：README 对应章节 + Known Limitations（如有新增限制）
8. **跑全量**：`make lint && make test && make eval`

---

## 12. 常见问题速答（Agent 自查）

| 问题 | 正确做法 |
|---|---|
| 用户要求"优化性能" | 先要 profile 数据，禁止凭直觉改 |
| 用户要求填一个数字 | 回答"需要实测"，用 `<待填写>` 占位 |
| 想用 LLM 直接生成 SQL | 必须先经过 Guard + 执行校验，不得直接返回 |
| 想加一个新指标 | 先检查同名 active 指标；写 YAML + 测试用例 + 关联黄金集 |
| 提示词要改 | 改 `agent/prompts/*.yaml`，触发 CI 回归，禁止内联 |
| 评测失败 | 失败样本进 `eval/failures/`，人工确认后才可进 SFT |
| 不确定某个技术选型 | 写 ADR，记录备选方案与理由，不擅自决定 |
| 生成的文件放在哪 | 严格按第 4 节目录职责；不确定的放 `docs/` 并询问 |

---

## 13. 环境与密钥

- 所有密钥、连接串、API Key 走环境变量，从 `.env` 读取（`.env` 已 gitignore）
- 提供 `.env.example` 列出所有必需变量名（不含真实值）
- Agent **不得**输出真实密钥到日志、注释、commit

---

## 14. 本文件的更新

修改本文件需要：

1. commit message 标注 `contract`
2. 在正文中说明理由
3. 如果放宽了某条禁止事项，必须同时说明风险与补偿措施

**本文件不是建议，是契约。**
