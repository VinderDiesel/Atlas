# ADR-0004：全栈采用 Apache 生态

- 日期：2026-09-01
- 状态：accepted
- 相关：ADR-0001（TPC-DS）、ADR-0002（Ossie）、ADR-0005（Calcite 取舍）

---

## 背景

原方案混用 ClickHouse（非 Apache）、自研语义 DSL、Hudi。
现要求尽可能采用 Apache 成熟项目，理由是：

1. **面试价值**：Apache 项目有公开治理、可查源码、社区背书，讲得清
2. **可验证性**：任何人都能复现，不依赖闭源或 SaaS
3. **组合完整性**：Ossie + Polaris + Iceberg + Doris 已形成事实上的开放湖仓栈

---

## 技术栈全景（变更后）

| 层 | 原方案 | 新方案 | 状态 | 变更理由 |
|---|---|---|---|---|
| **语义规范** | 自研 YAML | **Apache Ossie** 0.2.0.dev0 | 🆕 需学 | 行业标准，AI-ready，详见 ADR-0002 |
| **治理扩展** | 自研 | 自研（挂 Ossie custom_extensions） | 🆕 需建 | 补 Ossie 缺的 owner/lineage |
| **Catalog** | 无 | **Apache Polaris** (Incubating) | 🆕 需学 | Iceberg REST Catalog，RBAC，多引擎互操作 |
| **表格式** | Hudi | **Apache Iceberg** V2（V3 待验证） | 🔄 补强 | 开放标准；Doris/Spark/Trino 全支持；time travel |
| **OLAP** | ClickHouse | **Apache Doris** 4.1 | 🔄 替换 | 原生 Iceberg 支持；TPC-DS 1TB 比 Trino 快约 3 倍 |
| **SQL 解析** | sqlglot | sqlglot（Python 侧） | ✅ 保留 | Python 原生，MVP 够用，见 ADR-0005 |
| **SQL 校验/优化** | 无 | **Apache Calcite**（可选，Phase 2） | 🟡 可选 | 见 ADR-0005 的诚实取舍 |
| **计算** | Spark / Flink | Spark / Flink | ✅ 不变 | 已是 Apache |
| **编排** | Airflow | Airflow | ✅ 不变 | 已是 Apache |
| **对象存储** | 本地文件 | **MinIO**（S3 兼容） | 🆕 需配 | Iceberg 需要对象存储语义 |
| **向量检索** | Milvus | Milvus | ✅ 保留 | 已有工程经验，是简历资产 |
| **可观测** | OTel + Prometheus | OTel + Prometheus | ✅ 不变 | 已是 CNCF/Apache 生态 |

---

## 各组件的关键事实（2026-09 核实）

### Apache Polaris（Incubating）

- Iceberg REST Catalog 的参考实现，Snowflake 于 2024 年中捐赠给 ASF
- 对象模型：`CATALOG / NAMESPACE / TABLE / PRINCIPAL / ROLE / GRANT`
- RBAC 在 namespace 与 table 级强制；每次 loadTable 签发**短期存储凭证**（引擎永不见根凭证）
- 支持 Spark / Trino / Flink / Dremio / **Doris** 通过标准 REST 协议接入
- 自托管为 Java 服务 + Postgres 存元数据；单节点与 HA 拓扑均有文档

→ **对 Atlas 的意义**：行级权限可从"应用层自研"升级为"catalog 层强制"，
   这比自研谓词注入更可信（纵深防御多一层）。

### Apache Iceberg

- 三层结构：Data（Parquet）→ Metadata（manifest list / manifest）→ Catalog（元数据指针）
- 关键能力：ACID（乐观并发 + 快照隔离）、Schema Evolution、**Hidden Partitioning**、
  **Partition Evolution**、Time Travel、Branch/Tag
- V1 append-only / V2 merge-on-read（position + equality delete）/ V3 deletion vectors + row lineage

→ **对 Atlas 的意义**：
   - **Time Travel 直接服务于评测可复现**——快照 sha 可绑定到 Iceberg snapshot id，
     比"导出 CSV + 算 hash"更严谨，这是评测可信度的实质升级
   - **Branch/Tag** 可用于隔离评测数据与脏数据

### Apache Doris 4.1

- 原生 Iceberg catalog，基于 Iceberg Java library，走同一套 catalog API
- 支持 7 种 metastore：rest / hms / glue / s3tables / dlf / jdbc / hadoop
- 读 V1–V3，默认写 V2；V3 写需 format version 4.1+
- 支持 `ALTER TABLE ... CREATE BRANCH`、`expire_snapshots`、time travel
- **TPC-DS 1TB on Iceberg：99 个查询全部完成，耗时约为 Trino 的 1/3**（velodb 基准，2026）
- 谓词下推 + 动态分区裁剪，高选择率场景快 30–40%
- Doris 4.1（2026-05）新增 **per-user identity mode**：把真实用户身份透传给 Polaris，
  而非所有查询共用一个服务账号
- Doris 4.1 原生向量检索（IVF / IVF_ON_DISK），VectorDBBench 900 QPS @ 97% recall

→ **对 Atlas 的意义**：
   - 替换 ClickHouse 的最大收益是 **Doris ↔ Iceberg ↔ Polaris 三者的原生组合**，
     而不是孤立选一个 OLAP
   - **per-user identity mode 正好解决行级权限下推**（2026 新特性）
   - 向量检索可作为 Milvus 的对比项，但 **MVP 仍用 Milvus**（已有经验，是简历资产）

---

## 关键数据流

```
TPC-DS 原始数据
    ↓ Spark 写入
Apache Iceberg 表（MinIO 对象存储）
    ↓ Polaris (REST Catalog) 管理元数据指针 + RBAC
Apache Doris 4.1（原生 Iceberg catalog，向量化执行）
    ↑
Atlas Semantic Compiler（读 Ossie 模型 → 生成 SQL）
    ↑
Data Agent (LangGraph) → 只读 Guard → Doris
```

---

## 决策

1. **语义层**：Apache Ossie + Atlas 治理扩展（ADR-0002）
2. **存储层**：Apache Iceberg on MinIO
3. **Catalog**：Apache Polaris
4. **查询引擎**：Apache Doris 4.1
5. **向量检索**：Milvus 保留（对比 Doris 原生向量检索作为可选 ADR）
6. **SQL 校验**：MVP 用 sqlglot；Phase 2 评估 Calcite（ADR-0005）

---

## 代价与限制（诚实清单）

| 限制 | 说明 |
|---|---|
| **Polaris 是孵化器项目** | 与 Ossie 一样有不确定性；缓解：Iceberg REST 是协议标准，可换实现 |
| **Doris 资源占用高于 ClickHouse** | FE + BE 双进程，个人单机压力大；需实测内存占用后决定是否降级 |
| **Iceberg 引入运维复杂度** | Catalog + 对象存储 + 元数据文件；MVP 用 MinIO 单机简化 |
| **技术栈学习量显著增加** | Ossie / Polaris / Iceberg / Doris 四个新组件；已同步调整 8 周计划 |
| **Doris 向量检索不主用** | 避免技术栈过度分散；Milvus 是已有资产 |
| **Calcite 需 JVM** | 见 ADR-0005 的取舍 |

---

## 什么情况下应该推翻

- 单机跑不动 Doris（内存不足）→ 退回 ClickHouse，但保留 Iceberg + Polaris
- Polaris 部署复杂度过高 → 先用 Iceberg 的 `hadoop` catalog（文件系统），Phase 2 再换 Polaris
- 学习量超出 8 周可承受范围 → **优先保住 Ossie + Iceberg，Doris/Polaris 可降级为 Phase 2**

---

## 验证方式

| 组件 | 验证标准 |
|---|---|
| Ossie | 语义模型通过官方 schema 校验 |
| Iceberg | 能 time travel 到指定 snapshot，评测结果可复现 |
| Polaris | 三角色 RBAC 生效，不同 principal 看到不同数据 |
| Doris | 通过 Iceberg catalog 查询，TPC-DS 查询全部可执行 |
| MinIO | Iceberg 元数据与数据文件正确落盘 |
