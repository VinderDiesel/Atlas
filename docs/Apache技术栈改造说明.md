# Atlas 技术栈改造说明：全栈 Apache

> 目标：把 Atlas 从"自研 DSL + ClickHouse + Hudi"升级为 Apache 生态全栈。
> 本文件是改造的**导读**，决策细节见 `infra/adr/0002`、`0004`、`0005`。

---

## 一、改造前后对照

| 层 | 改造前 | 改造后 | 变化 |
|---|---|---|---|
| 语义规范 | 自研 YAML DSL | **Apache Ossie** 0.2.0.dev0 | 🆕 换标准 |
| 治理扩展 | （混在自研 DSL 里） | Atlas 自研，挂 Ossie `custom_extensions` | 🔄 解耦 |
| Catalog | 无 | **Apache Polaris** (Incubating) | 🆕 新增 |
| 表格式 | Hudi | **Apache Iceberg** V2 | 🔄 替换 |
| OLAP | ClickHouse | **Apache Doris** 4.1 | 🔄 替换 |
| 对象存储 | 本地文件 | **MinIO**（S3 兼容） | 🆕 新增 |
| SQL 解析 | sqlglot | sqlglot（Calcite 列 Phase 2） | ✅ 保留 |
| 向量检索 | Milvus | Milvus | ✅ 保留 |
| 计算/编排 | Spark / Flink / Airflow | 不变 | ✅ 已是 Apache |

---

## 二、三条最重要的认知（面试必须讲清）

### 1. Apache Ossie 不是运行时

Ossie **不执行查询、不解析指标、不在查询路径上**。
社区的官方类比是 **Protocol Buffers**：
`.proto` 定义 schema 编译成各语言代码，Ossie 定义"含义"编译成各平台语义层。

**→ 所以 Compiler 仍然必须自己写。Ossie 不替代它。**
这是最主要的工作量，也是本项目真正的技术含量所在。

### 2. Ossie 是 expression-carrying，不是 expression-defining

指标公式以命名方言存储：
`ANSI_SQL` / `SNOWFLAKE` / `DATABRICKS` / `BIGQUERY` / `MDX` / `TABLEAU` / `MAQL`。

同一个指标可以携带多个方言版本，但 **Ossie 不定义自己的可移植表达式语言**。

**→ 后果**：只写了 Snowflake SQL 的指标，不会自动被不懂 Snowflake 的引擎使用，
converter 必须翻译或拒绝。**Atlas 必须实现方言选择逻辑**——这是真实工程量，不是配置。

### 3. Ossie 标准化"定义"，不标准化"信任"

核心规范**没有** lineage、freshness、confidence、ownership、provenance。

"这是认证过的定义吗？谁负责？上次校验是什么时候？"——这些都不在规范里。
多位评论者指出这是 Ossie 最大的企业落地缺口。

**→ 这正是 Atlas 治理扩展要补的部分，也是本项目的技术价值。**

---

## 三、Atlas 的分层策略（核心设计）

```
┌─────────────────────────────────────────────────┐
│  Atlas 治理层（自研 = 项目核心价值）              │
│  governance: owner / version / supersedes        │
│  lineage: 来源表 / 依赖指标 / 下游消费方          │
│  freshness: 调度 / SLA / 实测延迟                │
│  policy: 行级策略 / 审批人                        │
│  quality: 黄金集用例 / 快照 sha                   │
│            ↓ 挂载于 custom_extensions            │
│              (vendor_name: "ATLAS")              │
└────────────────────┬────────────────────────────┘
                     │ 100% 符合 Ossie schema
┌────────────────────▼────────────────────────────┐
│  Apache Ossie Core Spec 0.2.0.dev0               │
│  semantic_model                                  │
│    ├── datasets（含 fields）                     │
│    ├── relationships                             │
│    ├── metrics（多方言 expression）               │
│    └── ai_context（instructions + synonyms）      │
└────────────────────┬────────────────────────────┘
                     │ converter
┌────────────────────▼────────────────────────────┐
│  dbt MetricFlow / Cube / Doris / BI              │
└─────────────────────────────────────────────────┘
```

**为什么这样分层是加分的：**

| 论点 | 说明 |
|---|---|
| 符合标准 | 不是封闭自研，能导出到行业工具（`make export`） |
| 补上缺口 | 解决 Ossie 没解决的企业落地问题（ownership/lineage） |
| 诚实边界 | 明确知道标准管什么、不管什么 |

---

## 四、Apache 各组件的关键事实（2026-09 核实）

### Apache Ossie（Incubating）

| 项 | 事实 |
|---|---|
| 前身 | Open Semantic Interchange (OSI)，Snowflake 2025-09 发起 |
| 进入孵化器 | **2026-07-10**（因 OSI 缩写冲突改名） |
| 版本 | 0.1.1 已发布；**0.2.0.dev0 为 DRAFT** |
| 参与方 | 50+ 组织：Snowflake、Databricks、Salesforce、dbt Labs、Dremio、RelationalAI、GoodData、Honeydew 等 |
| 官方示例 | `examples/tpcds_semantic_model.yaml`（631 行 TPC-DS，零售对照建模用） |
| 吉祥物 | 一只把语义元数据装在育儿袋里、在数据栈间跳跃的袋鼠 |

> 🎯 **金融主场景（TPC-DI + FIBO）说明**：官方示例基于 TPC-DS，仅作为零售历史语义模型
> （`atlas_retail`）的对照参考；主语义模型已切换为 `atlas_finance`（ADR-0006/0007）。

### Apache Polaris（Incubating）

- Iceberg REST Catalog 的参考实现，Snowflake 2024 年中捐赠给 ASF
- 对象模型：`CATALOG / NAMESPACE / TABLE / PRINCIPAL / ROLE / GRANT`
- RBAC 在 namespace 与 table 级强制；每次 loadTable 签发**短期存储凭证**（引擎永不见根凭证）
- 自托管为 Java 服务 + Postgres；单节点与 HA 拓扑均有文档

**→ 对 Atlas 的意义**：行级权限从"应用层自研"升级为"catalog 层强制"，纵深防御多一层。

### Apache Iceberg

- 三层：Data（Parquet）→ Metadata（manifest list / manifest）→ Catalog（元数据指针）
- ACID（乐观并发 + 快照隔离）、Schema Evolution、**Hidden Partitioning**、Time Travel、Branch/Tag

**→ 对 Atlas 的意义（重要）**：
**Time Travel 直接提升评测可信度**——快照可绑定到 Iceberg snapshot id，
比"导出 CSV + 算 hash"严谨得多，能直接 time travel 复现。

### Apache Doris 4.1

- 原生 Iceberg catalog（基于 Iceberg Java library，走同一套 catalog API）
- 支持 7 种 metastore：rest / hms / glue / s3tables / dlf / jdbc / hadoop
- 读 V1–V3，默认写 V2；V3 写需 format version 4.1+
- **TPC-DS 1TB on Iceberg：99 查询全部完成，耗时约为 Trino 的 1/3**（velodb 基准 2026）
- 4.1 新增 **per-user identity mode**：真实用户身份透传给 Polaris，而非共用服务账号
- 4.1 原生向量检索（IVF/IVF_ON_DISK），VectorDBBench 900 QPS @ 97% recall

**→ 对 Atlas 的意义**：
- 替换 ClickHouse 的最大收益是 **Doris ↔ Iceberg ↔ Polaris 原生组合**，而非孤立选 OLAP
- per-user identity mode 正好解决行级权限下推

---

## 五、Apache Calcite：为什么 MVP 不引入（ADR-0005）

Calcite 是 SQL 解析/校验/CBO 领域最成熟的 Apache 项目
（Hive、Flink、Kylin、Drill、MaxCompute、Dremio 的底座），但：

1. **Java-only**，需 JVM，会让 Python 项目变成双语言栈
2. Atlas 核心难点不在优化器——要证明的是"语义层→安全→评测→可解释"闭环
3. sqlglot 已足够做只读校验（AST 禁 DDL/DML、函数黑名单、LIMIT 注入、谓词注入）

**但必须在面试中讲清什么时候该用 Calcite**（能讲出来比"我用了"更有说服力）：

| 场景 | 为什么需要 Calcite |
|---|---|
| 跨数据源联邦查询 | adapter 机制是标准解法 |
| 物化视图自动改写 | MaterializedViewRule 远优于手写路由 |
| 完整 SQL 语义校验 | validator 比 sqlglot 的纯语法解析严格得多 |
| 对接企业 Hive/Flink 栈 | 保证方言与语义一致 |
| 优化器成为瓶颈时 | CBO 是正解（**前提是先用 profile 证明**） |

---

## 六、风险与降级路径（写死，避免临场慌乱）

| 风险 | 说明 | 降级 |
|---|---|---|
| **Ossie 0.2.0 是 DRAFT** | schema 可能变化 | 锁定 `apache/ossie` commit sha 并写入 README |
| **Ossie 孵化器风险** | 存在无法毕业的可能 | 治理层与规范层解耦，迁移 dbt YAML 只需换 converter |
| **Doris 内存压力** | FE+BE 双进程 | 换回 ClickHouse，**但保留 Iceberg + MinIO** |
| **Polaris 部署复杂** | 孵化器项目 | 用 Iceberg `hadoop` catalog（文件系统） |
| **Milvus 资源** | 内存占用 | 内存检索（仅 MVP 演示） |

**降级顺序**：
```
Doris → ClickHouse
  ＞ Polaris → Iceberg hadoop catalog
  ＞ Milvus → 内存检索
```

**但两条绝不降级**：
- **Apache Ossie 语义层** —— 项目的技术身份
- **Apache Iceberg 表格式** —— 评测可复现的基础

---

## 七、8 周计划的调整

| 周次 | 变化 |
|---|---|
| 第 1 周 | Day 3 改为 Apache 全栈 compose；Day 4-5 数据写入 **Iceberg** |
| 第 2 周 | Day 8-9 改为 **Ossie 模型**；Day 10 新增 **Atlas 治理扩展**；Day 11 compiler 读 Ossie → Doris SQL |
| 第 4 周 | Day 20/25 新增 **Polaris RBAC**（纵深防御从 2 层变 3 层） |
| 第 8 周 | Day 53 新增 `make export`（导出 dbt YAML 证明非封闭） |

**新增学习量**已分摊进第 1–2 周。若进度落后，**优先保住 Ossie + Iceberg**。

---

## 八、做完之后，简历可以这样写

| ❌ 危险写法 | ✅ 安全写法 |
|---|---|
| 基于 Apache Ossie 构建企业级语义层 | 采用 Apache Ossie 0.2.0.dev0 作为语义交换规范，自研 compiler 将指标编译为 Doris SQL；治理元数据通过 custom_extensions 扩展 |
| 使用 Iceberg 实现数据湖 | 基于 Apache Iceberg V2 承载 DWD/DWS，利用 Time Travel 将评测结果绑定到 snapshot id（`<实测复现方式>`） |
| 引入 Polaris 实现权限 | 通过 Apache Polaris 配置 PRINCIPAL/ROLE/GRANT，实现 catalog 层 RBAC；配合应用层谓词注入与只读账号形成三层防御 |
| 使用 Doris 提升查询性能 | Apache Doris 4.1 通过原生 Iceberg catalog 查询；**性能数字待实测后填写** |

**核心卖点一句话**：
> 用行业标准（Ossie）定义语义，用自研扩展补齐标准缺失的治理能力，
> 用 Apache 全栈（Iceberg + Polaris + Doris）承载可复现、可审计的交付闭环。
