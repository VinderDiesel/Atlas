# 语义层：Apache Ossie + Atlas 治理扩展

## Ossie 是什么（先搞清楚边界，非常重要）

**Apache Ossie (Incubating)** 是语义元数据的**交换规范**。

| 属性 | 说明 |
|---|---|
| 前身 | Open Semantic Interchange (OSI)，Snowflake 于 2025 年 9 月发起 |
| 进入 Apache 孵化器 | **2026 年 7 月 10 日**，因 OSI 缩写冲突改名 Ossie |
| 当前版本 | 0.1.1 已发布；**0.2.0.dev0 为 DRAFT，schema 可能变化** |
| 参与方 | 50+ 组织：Snowflake、Salesforce、Databricks、dbt Labs、Dremio、RelationalAI、GoodData、Honeydew 等 |
| 许可证 | Apache 2.0 |
| 吉祥物 | 一只把语义元数据装在育儿袋里、在数据栈之间跳跃的袋鼠 |

### 三条决定性边界（对外介绍必须讲清）

**1. Ossie 不是运行时。**
它不执行查询、不解析指标、不在查询路径上。
社区自己的类比是 **Protocol Buffers**：`.proto` 定义 schema 编译成各语言代码，
Ossie 文件定义"含义"，由 converter 编译成各平台的语义层。
→ **所以 Atlas 仍然需要自己写 compiler。Ossie 不替代它。**

**2. Ossie 是 expression-carrying，不是 expression-defining。**
指标公式以"命名方言"存储：`ANSI_SQL` / `SNOWFLAKE` / `DATABRICKS` / `BIGQUERY` / `MDX` / `TABLEAU` / `MAQL`。
同一个指标可以携带多个方言版本，但 Ossie **不定义自己的可移植表达式语言**。
→ 后果：只写了 Snowflake SQL 的指标，不会自动被不懂 Snowflake 的引擎使用，
   converter 必须翻译或拒绝。**Atlas 必须处理方言选择逻辑。**

**3. Ossie 标准化的是"定义"，不是"信任"。**
核心规范**没有** lineage、freshness、confidence、ownership、provenance 字段。
"这是认证过的定义吗？谁负责？上次校验是什么时候？"——这些都不在规范里。
→ **这正是 Atlas 治理扩展要补的缺口，也是本项目的技术价值所在。**

## Atlas 的分层策略

```
┌─────────────────────────────────────────────────┐
│  Atlas 治理层（自研，本项目核心价值）            │
│  owner / version / supersedes / lineage         │
│  row_policy / freshness / confidence            │
│  → 通过 Ossie 的 custom_extensions 承载          │
│  → vendor_name: "ATLAS"                          │
└────────────────────┬────────────────────────────┘
                     │ 100% 符合 Ossie schema
┌────────────────────▼────────────────────────────┐
│  Apache Ossie Core Spec 0.2.0.dev0               │
│  semantic_model / datasets / fields              │
│  relationships / metrics / ai_context            │
└────────────────────┬────────────────────────────┘
                     │ converter
┌────────────────────▼────────────────────────────┐
│  下游：dbt MetricFlow / Cube / Doris / BI        │
└─────────────────────────────────────────────────┘
```

**为什么这样分层是加分的：**

- 符合标准 → 不是封闭自研，能导出到行业工具
- 补上缺口 → 解决 Ossie 没解决的企业落地问题（ownership/lineage）
- 诚实边界 → 明确知道标准管什么、不管什么

## 文件

| 文件 | 说明 |
|---|---|
| `atlas_finance.ossie.yaml` | **主语义模型**（金融 TPC-DI + 17 条 FIBO 概念映射，ADR-0007） |
| `atlas_retail.ossie.yaml` | 历史对照模型（零售 TPC-DS，不再扩展） |
| `../governance/atlas_governance.schema.json` | Atlas 治理扩展的 JSON Schema（挂在 custom_extensions 下） |
| `../synonyms/en_us.yml` | 英文同义词表（代码外配置，ADR-0015；与模型 `ai_context.synonyms` 并集） |
| `../policies/row_policy.yml` | 行级权限与脱敏策略声明 |
| `../_legacy/` | **已归档**：ADR-0002 之前的自研 DSL 定义与 Schema，仅作演进对照，零代码引用 |

## 权威源唯一（机械锁定）

本目录是语义定义的**唯一权威源**。`semantic/lint.py` 的 `[authority]` 检查保证
`semantic/` 下除 `ossie/`、`synonyms/`、`policies/`（与 `_*` 归档区）外不存在任何
`*.yaml/*.yml` 语义定义文件——曾经长期挂着的 `status: active` 幽灵定义（引用不存
在的表、lint 不覆盖）已集中到 `../_legacy/`，理由与清单见该目录 README。

## 校验

```bash
# 1) Ossie 官方 schema 校验（从 apache/ossie 取 core-spec/ossie-schema.json）
make lint-ossie

# 2) Ossie 官方 validate.py（另做业务规则校验）
python -m semantic.ossie_validate semantic/ossie/*.ossie.yaml

# 3) Atlas 治理扩展校验
make lint-governance
```

## 参考

- 规范与示例：https://github.com/apache/ossie
- 官网：https://ossie.apache.org/
- 官方 TPC-DS 示例（631 行）：`apache/ossie/examples/tpcds_semantic_model.yaml`
  → **零售历史模型（`atlas_retail`）的建模参考；金融主模型为 TPC-DI（ADR-0006）**
