# Atlas

> **以 FIBO 本体为语义锚点的金融可信 AI 问数平台**｜统一语义层 + 受控 NL2SQL + 可解释 Data Agent
>
> Status: **under active development**
> 所有数字来自可复现脚本产物，不是营销断言。见 `EVAL_REPORT.md`。

---

## 0. 一句话定位

Atlas 是一个**个人主导建设的 AI 数据平台设计项目**：以**金融为主场景**（TPC-DI 零售经纪数据），
**FIBO 金融业务本体作为语义锚点**（L2 概念对齐层，ADR-0007），使用声明式语义层、指标编排、
受控 SQL 执行与 LangGraph Agent，打通**业务术语 → FIBO 概念 → 指标计划 → 安全查询 → 可解释结果 → 评测回流**的完整闭环。

**它刻意不做的事**：不训练基座模型、不追求 NL2SQL 榜单分数、不声称企业级生产能力。
**它刻意做好的事**：口径唯一、权限下推、SQL 只读、结果可解释、评测可复现。

### 0.1 技术栈：全栈 Apache

| 层 | 选型 | 说明 |
|---|---|---|
| 语义规范 | **Apache Ossie** (Incubating) | 语义元数据交换标准 |
| 治理扩展 | Atlas 自研 | 补 Ossie 缺的 owner / lineage / freshness |
| Catalog | **Apache Polaris** (Incubating) | Iceberg REST Catalog + RBAC |
| 表格式 | **Apache Iceberg** V2 | ACID、Time Travel、Hidden Partitioning |
| OLAP | **Apache Doris** 4.1 | 原生 Iceberg catalog，向量化执行 |
| 对象存储 | MinIO | S3 兼容 |
| 向量检索 | Milvus | 保留（已有工程经验） |
| 计算 / 编排 | Spark / Flink / Airflow | 已为 Apache |

见 `infra/adr/0004-apache-stack.md`。

**一个关键认知**：Apache Ossie **不是运行时**——它不执行查询、不解析指标、不在查询路径上。
社区类比是 Protocol Buffers：Ossie 定义"含义"，由 converter 编译为各平台语义层。
**所以 Compiler 仍需自己实现**，这正是本项目的核心工作量。

---

## 1. 为什么是"确定性优先"

绝大多数 NL2SQL 项目失败在企业不敢用，而不是模型不够准。Atlas 的核心纪律是：

```
已知指标  →  语义编译器直接生成 SQL（不经过 LLM，结果确定）
自由问句  →  RAG 检索 + LoRA/通用 LLM 生成候选
             ↓ 必须过：AST 只读检查 → 行级权限下推 → LIMIT/时间范围 → 成本预算
             ↓ 执行 → 结果校验 → 失败进修正日志 → 回流训练
```

**LLM 不是 SQL 的作者，是候选生成器。** 编译器和校验器才是权威。

---

## 2. 架构

```text
┌────────────────────── 用户与分析协作层 ──────────────────────┐
│ Web/Chat UI  │  Notebook  │  BI/API  │  Slack/OpenAPI         │
└───────────────────────┬──────────────────────────────────────┘
                        │ OAuth/JWT；部门/行级身份
┌───────────────────────▼──────────────────────────────────────┐
│ API Gateway + 审计 + 预算 + 只读 SQL 防火墙 + OTel SDK       │
└───────────────────────┬──────────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────────┐
│  Data Agent（LangGraph 状态机；确定性工具优先）              │
│  clarify → retrieve → plan → generate → validate → execute   │
│  → explain → visualize → reflect → handoff                   │
└──────┬──────────────┬───────────────┬───────────────┬────────┘
       │              │               │               │
┌──────▼──────┐ ┌─────▼─────┐ ┌──────▼──────┐ ┌──────▼──────┐
│ 语义检索     │ │ 指标注册   │ │ NL2SQL 服务  │ │ 结果解释     │
│ Milvus+HNSW │ │ YAML/Git   │ │ LoRA/通用LLM │ │ LLM Judge    │
│ +Graph 关系 │ │ OpenAPI    │ │ RAG/SC/修正  │ │ 图表/归因    │
└──────┬──────┘ └─────┬─────┘ └──────┬──────┘ └──────┬──────┘
       │              │               │               │
┌──────▼──────────────▼───────────────▼───────────────▼───────┐
│  Semantic Compiler（自研）：FIBO 概念/指标/维度/时间/筛选 → SQL│
├──────────────────────────────────────────────────────────────┤
│  指标编排（YAML + Airflow）；血缘、版本、质量                 │
├──────────────────────────────────────────────────────────────┤
│  Apache Doris 4.1（OLAP，原生 Iceberg catalog）               │
│  Apache Iceberg V2 on MinIO（湖仓表格式，Time Travel）        │
│  Apache Polaris（REST Catalog + RBAC，行级权限下推）          │
│  PostgreSQL（元数据/策略）+ Milvus（向量）+ NetworkX（图）    │
├──────────────────────────────────────────────────────────────┤
│  Spark / Trino；对象存储；Docker Compose；CI/CD               │
└──────────────────────────────────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────────┐
│  评测/反馈闭环：黄金集、Execution EX、Judge、修正日志、SFT     │
│  Prometheus + Grafana + OTel + 模型/提示词 Git 版本            │
└──────────────────────────────────────────────────────────────┘
```

**两条铁律：**

1. **Git 是唯一事实源**。语义定义、提示词、评测集、模型适配器版本全部入库，任何线上行为可回溯到 commit。
2. **查询引擎是可替换执行面**。语义层不绑定 ClickHouse/Doris/Trino 中的任何一个。

---

## 3. 30 分钟快速开始

> ⚠️ 以下步骤处于**待验证**状态。首次跑通后请回填真实耗时与遇到的问题，并更新本节。

### 3.1 前置条件

| 依赖 | 版本 | 说明 |
|---|---|---|
| Docker / Docker Compose | ≥ 24 / ≥ 2.20 | 起 Polaris、Doris(FE+BE)、Iceberg/MinIO、Milvus、Grafana |
| Python | 3.11 | 主开发语言 |
| uv 或 pip | 最新 | 依赖管理（推荐 uv） |
| Git | ≥ 2.40 | 语义层版本控制 |
| 内存 | **建议 ≥ 16GB** | Doris FE+BE 双进程内存压力较大（ADR-0004 有降级路径） |
| 显卡（可选） | 单卡 24G 起 | 仅 LoRA 微调阶段需要；CPU 可跑通 P0–P1 |

> ⚠️ 若单机跑不动 Doris，按 ADR-0004 降级：保留 Iceberg + MinIO，查询引擎换回 ClickHouse。

### 3.2 启动

```bash
git clone <your-repo-url> atlas && cd atlas

# 1) 安装依赖
make install

# 2) 启动 Apache 全栈（Polaris / Doris / Iceberg+MinIO / Milvus / Grafana）
make up

# 3) TPC-DI Batch1（零售经纪，2012-07-07~2017-07-07 实测数据段）→ 装载 17 张 ODS → 锁定快照
#    （load 耗时分钟级；快照 meta 见 data/snapshots/<sha>.meta.json）
make seed

# 3b) DWD 加工：在 Doris 内执行 sql/dwd 的 8 张幂等 SQL（依赖 Doris 已建 atlas catalog）
make dwd

# 4) 校验语义层（Ossie 规范 + Atlas 治理扩展）
make lint-ossie
make lint-governance

# 5) 跑一次完整链路："2013 年第二季度总交易额是多少？"
make plan   Q="2013 年第二季度总交易额是多少？"
make compile
make eval

# 6) 生成评测报告
make report && cat EVAL_REPORT.md

# 7) 导出为 dbt MetricFlow YAML（证明语义层非封闭）
make export
```

### 3.3 验证成功

- [x] `make lint-ossie` 通过（Ossie 官方 schema 校验）
- [x] `make lint-governance` 通过（owner / lineage / policy 齐全）
- [x] `make plan` 输出 Plan 含 `metric='total_trade_value'`、`value='2013Q2'`（对应 gold-101，15/15 契约测试通过）
- [x] `make compile` 产出带 `LIMIT` 与时间范围约束的只读 SQL（gold-101~103 实测）
- [x] TPC-DI Batch1 → tpcdi 17 张 ODS 装载完成，行数与源文件逐行核验一致（data/loader.py QA 口径）
- [x] DWD 8 张加工完成（INSERT OVERWRITE 幂等，Doris 侧 COUNT 与 pyiceberg 侧双验一致），行数与 snapshot id 锁定于 `data/snapshots/b47a6c1.meta.json`
- [x] 语义层 source 命名与 Doris 实际路径统一（`atlas.dwd.*`）；gold-101~103 参考 SQL 已在 Doris 实跑返回（2013Q2 交易额 344129059.35 等，仅供链路验证，非评测数字）
- [x] `make eval` 已跑通且幂等：金融段 48 例（44 可解析 + 4 歧义）Plan Acc 44/44、歧义反问 4/4、EX 44/44，报告 `eval/reports/b47a6c1.json`（gold 共 50 条含零售 2，50 条目标达成，见 5.3）
- [x] `make retrieve` 指标检索双路实测：语料 15 指标文档（与 gold 同源）、查询 44 条可解析问句；BM25 Recall@1 44/44、Milvus 稀疏向量 Recall@1 42/44（两路 @5 均 44/44），报告 `eval/reports/retrieval-bm25-b47a6c1.json` / `retrieval-milvus-b47a6c1.json`
- [x] `make retrieve ENGINE=fuse` RRF 融合实测：BM25 + Milvus 双路各 top-20 融合后 Recall@1 = 44/44、@5 = 44/44（修复向量路 2 条 top-1 失手），报告 `eval/reports/retrieval-fuse-b47a6c1.json`；SemanticGraph 图约束过滤跨实体错配（现金域 × 证券维度），13 组 gold 维度样本契约测试全保留（tests/test_graph_store.py）
- [ ] Grafana 面板能看到 TTFT / p95 / token_cost

---

## 4. 语义层：Apache Ossie + Atlas 治理扩展

指标是 **Git 中的一等对象**，不是 LLM 提示词里的字段别名。

### 4.0 为什么是 Ossie，以及它不是什么

**Apache Ossie (Incubating)** 是语义元数据的交换规范。
前身是 Snowflake 于 2025-09 发起的 Open Semantic Interchange (OSI)，
**2026-07-10 进入 Apache 孵化器**并改名（避免与 OSI 缩写冲突）。
50+ 组织参与：Snowflake、Databricks、Salesforce、dbt Labs、Dremio、RelationalAI 等。

三条决定性边界，**面试必须讲清**：

| 边界 | 含义 | 对 Atlas 的影响 |
|---|---|---|
| **不是运行时** | 不执行查询、不解析指标、不在查询路径上 | **Compiler 必须自己写**，Ossie 不替代它 |
| **expression-carrying** | 指标按命名方言存储（ANSI_SQL / SNOWFLAKE / BIGQUERY / DATABRICKS / MDX / TABLEAU / MAQL），不定义可移植表达式语言 | 需实现方言选择；只写 ANSI_SQL 则其他方言需翻译或拒绝 |
| **不标准化"信任"** | 核心规范**无** lineage / ownership / freshness / provenance | **这正是 Atlas 治理扩展要补的缺口** |

社区类比：**Protocol Buffers** —— `.proto` 定义 schema 编译成各语言代码，
Ossie 定义"含义"编译成各平台语义层。

### 4.1 分层结构

```
Atlas 治理层（owner / version / supersedes / lineage / row_policy / freshness）
        ↓ 通过 custom_extensions（vendor_name: ATLAS）挂载
Apache Ossie Core Spec（semantic_model / datasets / fields / relationships / metrics / ai_context）
        ↓ converter
下游：dbt MetricFlow / Cube / Doris / BI
```

### 4.2 语义模型文件

主模型：`semantic/ossie/atlas_finance.ossie.yaml`（8 datasets / 12 relationships / 6 metrics，
挂载 17 条 FIBO 概念映射，见 `data/fibo/README.md`）；
`atlas_retail.ossie.yaml` 保留为演进对照（TPC-DS 零售，不再扩展）

```yaml
version: "0.2.0.dev0"
semantic_model:
  - name: atlas_finance_analytics
    description: Atlas 金融语义模型，基于 TPC-DI 零售经纪基准重建，FIBO 概念对齐（ADR-0007）
    ai_context:
      instructions: >
        使用本模型进行金融场景分析（账户/交易/持仓/佣金）。
        涉及"交易额/成交额"时优先使用 total_trade_value……
        当用户问句存在歧义时，必须反问澄清，不要猜测。
    datasets:
      - name: fact_trades
        source: iceberg_rest.atlas.dwd_fact_trades
        primary_key: [TradeID]
        fields:
          - name: TradePrice
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: TradePrice
            datatype: Decimal
            ai_context:
              synonyms: ["成交价", "成交单价"]
    relationships:
      - name: trades_to_date
        from: fact_trades
        to: dim_date
        from_columns: [SK_CreateDateID]
        to_columns: [SK_DateID]
    metrics:
      - name: total_trade_value
        description: 总交易额（成交数量 × 成交单价）
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(fact_trades.Quantity * fact_trades.TradePrice)
        datatype: Decimal
    custom_extensions:
      - vendor_name: ATLAS
        data: |
          {
            "governance": { "owner": "finance@atlas.local", "version": 1, "status": "active" },
            "lineage": { "source_columns": ["fact_trades.Quantity", "fact_trades.TradePrice"], "dependencies": [] },
            "fibo_alignment": {
              "layer": "L2",
              "fibo_commit": "119fa8c091aa4beece7d22aefa6fe138021a4355",
              "mappings": {
                "fact_trades": {
                  "concept": "https://spec.edmcouncil.org/fibo/ontology/FBC/FinancialInstruments/FinancialInstruments/SecuritiesTransaction",
                  "match_type": "exact_match"
                }
              }
            }
          }
```

**对照参考**：`apache/ossie/examples/tpcds_semantic_model.yaml`（631 行 TPC-DS 示例）
—— 零售历史语义模型（`atlas_retail`）的建模参考；金融主模型的数据集为 TPC-DI（ADR-0006）。

### 4.3 第一约束：同一业务词只有一个权威定义

创建 `xxx_v2` **必须**填写 `supersedes`、`reason`、`migration_window`。
CI 禁止同名 active 指标并存，并对生产引用发出 Breaking Change 告警。
（这套治理元数据挂在 custom_extensions 的 `governance` 节点下。）

### 4.4 与开源方案的关系

| 方案 | 定位 | Atlas 的关系 |
|---|---|---|
| **Apache Ossie** | 语义元数据交换规范 | **主规范**（ADR-0002） |
| dbt MetricFlow | 指标即代码，生态成熟 | 通过 `make export` 导出兼容，证明非封闭 |
| Cube | 集中指标 + 访问规则 + 多 API 暴露 | 接口兼容目标 |
| Apache Kylin | 预计算亚秒 OLAP | 不做主方案（固定报表场景更合适） |
| Apache Calcite | SQL 解析/校验/CBO 框架 | **MVP 不引入**，见 ADR-0005 的诚实取舍 |

### 4.5 校验

```bash
make lint-ossie        # Ossie 官方 ossie-schema.json 校验
make lint-governance   # Atlas 治理扩展 JSON Schema 校验
make export            # 导出 dbt MetricFlow YAML
```

> ⚠️ **Ossie 0.2.0.dev0 是 DRAFT**，schema 可能变化。
> 锁定时必须在 README 记录 `apache/ossie` 的具体 commit sha。
> 若 Ossie 未能从孵化器毕业，迁移路径见 ADR-0002「什么情况下应该推翻」。

---

## 5. NL2SQL：如何评测

### 5.1 两个指标分开报，不许混

| 指标 | 定义 | 用途 |
|---|---|---|
| **EX**（Execution Accuracy） | 执行结果与固定快照一致的占比 | 主指标，反映真实可用度 |
| **Plan Acc** | 逻辑计划（指标/维度/筛选/时间）与标注一致 | 用于定位错误发生在"理解"还是"生成" |

禁止用 Spider 分数代替企业分数，禁止用训练集准确率代替测试集准确率。

### 5.2 评测集

```text
eval/
├── gold/        # 自建黄金集：50 例目标，金融段为主（FIBO 概念标注），人工标注
├── spider/      # 历史对照（通用领域，与金融场景不匹配，不再新增）
├── bird/        # 公开集对照（BIRD finance 段，仅作参照，不与 gold 混报）
├── runner.py    # 固定快照 + expected result hash + CI
└── reports/     # 每次 commit 自动生成
```

每条黄金集样本结构：

```json
{
  "id": "gold-101",
  "question": "2013 年第二季度总交易额是多少？",
  "expected_metric": "total_trade_value",
  "expected_dimensions": [],
  "expected_time": "2013Q2",
  "expected_sql": "<人工标注 SQL>",
  "result_hash": "<固定数据快照下的执行结果哈希>",
  "snapshot_sha": "<git rev-parse HEAD of data snapshot>",
  "tags": ["aggregation", "time_range", "fibo_alignment"],
  "fibo_concepts": [
    { "term": "交易额", "concept": "…/FND/Accounting/CurrencyAmount/MonetaryAmount", "role": "measure" }
  ]
}
```

### 5.3 评测执行（eval/runner.py）

链路：`gold 问句 → Planner → Plan（记 Plan Acc）→ Compiler → Guard（只读 + 表白名单=快照内表）→ Doris 执行 → 结果 sha256`，与标注 `result_hash` 一致即 EX pass。

- 评测只认当前 HEAD：启动时复核 `data/snapshots/<sha>.meta.json` 数据指纹，漂移即拒绝出报告
- 首轮执行自动锚定：gold JSON 的 `result_hash` 占位符回填为实测 sha256（`snapshot_sha` 同步绑定），此后比对即 EX
- 歧义样本（`ambiguous: true`）要求返回澄清反问；反问命中 = pass，不猜
- 产出：`eval/reports/<git sha>.json`（当前 `b47a6c1`：金融段 48 例，Plan Acc 44/44、反问 4/4、EX 44/44）

### 5.4 准确率提升手段（按优先级）

1. **Schema Linking 两阶段**：图域约束粗筛 → BM25 + 向量召回 → 列级 rerank → JOIN 可达性验证
2. **自洽投票（self-consistency）**：多次采样，按执行结果聚类取众数
3. **执行结果校验**：空结果 / 全 NULL / 行数异常 → 触发修正或澄清
4. **错误反馈修正**：失败样本进 `eval/failures/`，人工确认后进 SFT 飞轮
5. **多轮澄清**：问句歧义时主动反问，而不是猜

---

## 6. 安全红线

**只读网关是硬约束，不允许任何绕过路径。**

```python
# agent/security/sql_guard.py（设计示意，实现见源码）
READONLY_AST_RULES = [
    Insert, Update, Delete, Drop, AlterTable, Grant, Copy,  # 全部禁止
]
```

强制执行链（顺序不可调换）：

```
parse(AST) → 禁 DDL/DML → 函数黑名单 → apply LIMIT → apply 时间范围
          → 注入行级策略谓词 → 成本估算 → 超预算拒绝 → 执行
```

必须覆盖的边界：CTE、子查询、视图展开、动态 SQL、函数黑名单、权限表达式注入。

**PoC 阶段不开放任何写操作。** 数据库账号本身也必须是只读账号（纵深防御，不依赖单一层）。

---

## 7. 数据来源与诚实边界

### 7.1 本项目使用的数据

| 数据 | 用途 | 来源 |
|---|---|---|
| TPC-DI（零售经纪，2012-07-07~2017-07-07 实测数据段；已装载 17 张 ODS + 8 张 DWD，快照 `b47a6c1`） | 主场景数据：重建金融库表关系、指标场景 | 公开基准，注册下载 |
| FIBO 本体（FND+FBC+BE 域）+ OMG Commons/LCC | 语义锚点：概念 IRI 注册表与对齐映射 | EDM Council（MIT）/ OMG 规范（研究用途） |
| BIRD finance | 公开集能力对照（仅参照，不混报） | 公开学术基准 |
| 自建黄金集（50 例目标） | 主评测集 | 本人基于 TPC-DI 人工标注 |

### 7.2 明确声明

- ❌ 本项目**不使用、不包含**任何前雇主/现雇主的真实数据、代码、指标定义或业务指标
- ❌ 不使用真实公司名构造场景；实体关系由 TPC-DI 重建
- ❌ 公开集分数**不外推**为企业场景分数
- ✅ 已有工程经验（SQL LoRA、GraphRAG 检索、OTel 可观测、YAML 编排）作为**设计输入**，在本项目中以公开数据重新实现

---

## 8. 目录结构

```text
atlas-data-platform/
├── semantic/
│   ├── ossie/           # ⭐ Apache Ossie 语义模型（主规范）
│   ├── governance/      # ⭐ Atlas 治理扩展 Schema（补 Ossie 缺口）
│   ├── policies/        # 行级权限策略
│   └── schema/          # ⚠️ 已废弃：早期自研 DSL，仅作演进对照
├── sql/                 # tpcds_ddl（零售历史，只读）/ dwd / dws / views（金融表待建）
├── spark/               # 元数据抽取器（SQL/DDL/ETL 注释解析）
├── airflow/             # yaml_jobs（源）+ dags/generated（自动生成，勿手改）
├── agent/               # state / graph / tools / planner / compiler / security
├── retrieval/           # bm25 / milvus_client / graph_store
├── serving/             # api / auth / gateway
├── observability/       # otel / dashboards
├── eval/                # gold / spider / bird / runner / reports
├── lora/                # SQL 适配器训练与数据飞轮
├── infra/               # docker / ci / adr
├── docs/                # 设计文档、逐日任务清单、术语表
└── data/snapshots/      # 固定数据快照（记录 sha，保证评测可复现）
```

**约定**：`airflow/dags/generated/` 下所有文件由 YAML 生成，**禁止手工编辑**。

---

## 9. 命令速查

| 命令 | 作用 |
|---|---|
| `make install` | 安装依赖 |
| `make up` | 启动基础设施 |
| `make seed` | 装载 TPC-DI Batch1 → Iceberg → 锁定快照 |
| `make lint` | 校验语义层定义（JSON Schema + 唯一性 + 血缘） |
| `make plan Q="..."` | 问句 → 指标计划（不执行） |
| `make compile` | 计划 → 只读 SQL |
| `make eval` | 跑评测集，产出 report JSON |
| `make retrieve` | 指标检索评测（BM25；`ENGINE=milvus` 走 Milvus 稀疏向量；`ENGINE=fuse` 走 RRF 双路融合） |
| `make train` | 用确认后的失败样本训练 SQL LoRA |
| `make report` | 生成 EVAL_REPORT.md |
| `make test` | 全量单元 + 契约测试 |

---

## 10. 已知限制（Known Limitations）

> 这一节是**诚实性的核心**，禁止删除或美化。

1. **数据规模有限**：TPC-DI 为基准默认规模（实测数据段 2012-07-07~2017-07-07，294 万行），与真实金融机构 PB 级、上千张表的复杂度不可比
2. **Schema Linking 未大规模验证**：当前仅在 15 张表子集上验证；1000 表场景属于**待验证假设**
3. **权限模型简化**：行级策略为自研简化实现，未经过真实 IAM/审计/合规检验
4. **并发与容灾未验证**：MVP 为单机部署，无高可用、无限流压测
5. **NL2SQL 准确率数字待填**：所有 EX / Recall 数值必须实测后填写，不得预估
6. **Apache Ossie 0.2.0.dev0 是 DRAFT**：schema 可能变化；且 Ossie 是孵化器项目，
   存在无法毕业的可能（迁移路径见 ADR-0002）
7. **Ossie 不含治理字段**：owner / lineage / freshness 由 Atlas 扩展补齐，
   该扩展是本项目自研，未经过行业标准检验
8. **Polaris 与 Doris 为新增组件**：学习成本与运维复杂度高于原方案；
   若单机资源不足，按 ADR-0004 降级
9. **Apache Calcite 未引入**：MVP 用 sqlglot，无 CBO 与语义校验（取舍见 ADR-0005）
10. **图表与归因能力为最小实现**：仅做确定性渲染，无自动洞察
11. **Planner 为确定性规则版**：维度解析要求显式分组结构词（“按X统计/分组”）
    且仅匹配 dim_* 维度表字段；相对时间（“上个月/最近”）不支持（返回澄清）；
    filter 解析未实现（详见 agent/planner.py 已知边界）
12. **检索语料与查询同源、MVP 向量为词法级**：Recall 评测的问句措辞来自语义层
    同义词（同源口径验证，非跨领域泛化数字）；Milvus 向量为确定性词法稀疏向量
    （tf-IP，无 idf），不编码语义相似（“佣金”与“手续费”不同 token），嵌入与
    rerank 待 Schema Linking 阶段评估（见 retrieval/bm25.py、milvus_client.py）
13. **图约束比 Compiler 保守**：SemanticGraph 禁止事实表间桥接（dim→fact 回跳）的
    跨实体召回，Compiler 目前技术上能编译这类多跳 SQL（缺维度域检查）；检索层先剔除，
    双方口径差异属已知边界（见 retrieval/graph_store.py）

**如果有真实企业数据，我会优先补做**：数据契约、IAM 集成、审计留痕、容灾、并发压测、模型红队测试、变更管理流程。

---

## 11. 能力来源登记（避免夸大）

| 能力 | 状态 | 本项目中的动作 |
|---|---|---|
| Multi-LoRA SQL 推理、SFT 飞轮 | 已有工程经验 | 用公开数据重建训练/评测流水线，**不声称是新发明** |
| 元数据治理、SQL/tokenizer 解析 | 已有工程经验 | 改造为 Atlas 的 DDL/ETL 注释抽取器 |
| 标签体系、Bitmap、OneID、销售/金融指标 | 已有业务经验 | 抽象为 Atlas 的指标/实体/分群/权限设计模式 |
| YAML 编排 + Airflow + GitLab CI | 已有工程经验 | 迁移为指标任务编排与 CI 契约测试 |
| GraphRAG + Milvus 混合过滤 | 已有工程经验 | 复用于指标、同义词、业务术语检索 |
| OpenTelemetry + PromptOps + LLM Judge | 已有工程经验 | 接入 SQL 调用链、成本、回归评测 |
| 声明式语义层、指标版本血缘 | **需新建** | 基于 Apache Ossie 规范实现编译器、治理扩展与测试 |
| Ossie / Polaris / Iceberg / Doris | **需新建** | 单机部署、基准测试、维护 ADR（0002/0004/0005） |
| Agent 安全执行与自动洞察 | **需新建** | 先做安全工具，再扩展规划与归因 |

---

## 12. 许可与声明

- 代码：`Apache-2.0`
- 数据：TPC-DI / BIRD / FIBO（MIT）/ OMG Commons（研究用途）遵循各自原始许可

---

## 附：我的诚实承诺

本项目 README 中的每一个数字，只能来自以下之一：

- Git commit / CI log
- 评测 JSON（`eval/reports/`）
- Prometheus / Grafana 导出
- 云账单
- 公开基准官方结果

**没有计算脚本的数字，不写。单次运行得出的性能结论，不写。**

---

## 关于作者与交流

**范德塞尔** · 江苏 苏州

> 数据平台 / 可信 AI 问数方向。Atlas 是我个人设计与实现的完整项目——
> 从 Apache 语义层（Ossie + 自研治理扩展）到确定性 NL2SQL 编译器、
> 再到可复现评测闭环（黄金集 + 快照锁定），全链路动手实现；
> 本 README 中的每一个数字都绑定 commit 或评测报告（见文末《我的诚实承诺》）。

如果你在做同类方向（语义层 / 指标平台 / NL2SQL / 数据 Agent），欢迎交流：
项目里踩过的坑（Ossie 不是运行时、固定快照评测、TPC-DI 时间编码等）
也许能帮你少走弯路，也欢迎对 Atlas 提 issue / PR。

<div align="center">

<img src="docs/contact-wechat.png" width="280" alt="微信二维码：扫一扫添加好友" />

**扫码添加微信，备注「Atlas」**（注明来意，方便我知道你是从仓库来的）

</div>
