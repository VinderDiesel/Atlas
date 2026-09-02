# ADR-0002：语义层采用 Apache Ossie 规范（取代早期自研 DSL）

- 日期：2026-09-01
- 状态：**accepted（取代原 ADR-0002「自研 DSL vs dbt/Cube」）**
- 相关：README 第 4 节、AGENTS.md 术语表、`semantic/ossie/`

---

## 背景

原方案自研 YAML DSL。调研中发现 **Apache Ossie (Incubating)** —— 语义元数据的行业标准，
且官方仓库 `apache/ossie/examples/tpcds_semantic_model.yaml`（631 行）与 Atlas 使用的
TPC-DS 数据集完全吻合，可直接参考。需要重新决策。

**Ossie 关键事实**（2026-09 核实）：

| 项 | 事实 |
|---|---|
| 前身 | Open Semantic Interchange (OSI)，Snowflake 2025-09 发起 |
| 进入 Apache 孵化器 | **2026-07-10**，因 OSI 缩写冲突改名 |
| 版本 | 0.1.1 已发布；**0.2.0.dev0 为 DRAFT** |
| 参与方 | 50+ 组织（Snowflake、Databricks、Salesforce、dbt Labs、Dremio、RelationalAI 等） |
| 许可证 | Apache 2.0 |

---

## 三条决定性边界（这是决策的核心依据）

### 1. Ossie 不是运行时

它不执行查询、不解析指标、不在查询路径上。社区类比是 **Protocol Buffers**：
`.proto` 定义 schema 编译成各语言代码；Ossie 定义"含义"，由 converter 编译成各平台语义层。

→ **结论：Ossie 不替代 Atlas 的 compiler。我们仍然必须自己实现 `metric → SQL`。**

### 2. expression-carrying，而非 expression-defining

指标公式以命名方言存储：`ANSI_SQL` / `SNOWFLAKE` / `DATABRICKS` / `BIGQUERY` / `MDX` / `TABLEAU` / `MAQL`。
同一指标可携带多方言版本，但 **Ossie 不定义自己的可移植表达式语言**。

→ 后果：只写了 Snowflake SQL 的指标不会自动被其他引擎使用，converter 必须翻译或拒绝。
→ **Atlas 必须实现方言选择逻辑**，这是真实工程工作量。

### 3. 标准化"定义"，不标准化"信任"

核心规范**没有** lineage、freshness、confidence、ownership、provenance。
"这是认证过的定义吗？谁负责？上次校验是什么时候？"——都不在规范内。
这被多位评论者指出是 Ossie 最大的企业落地缺口。

→ **这正是 Atlas 治理扩展要补的部分，也是本项目的技术价值。**

---

## 备选方案

| 方案 | 优势 | 约束 |
|---|---|---|
| 早期自研 YAML DSL | 完全可控 | 封闭，无法与行业工具交换；重复造轮子 |
| dbt MetricFlow | 生态成熟 | 依赖 dbt/Jinja；定制成本高 |
| Cube | 集中管理 + 多 API 暴露 | SaaS/多组件运维 |
| **Apache Ossie + Atlas 治理扩展** | 标准合规 + 补齐缺口 + 可导出 | 0.2.0 仍是 draft；孵化器项目有不确定性 |

---

## 决策

**采用 Apache Ossie 0.2.0.dev0 作为语义层的交换规范，
Atlas 自研治理扩展通过 `custom_extensions`（`vendor_name: ATLAS`）承载。**

分层：

```
Atlas 治理层（owner / version / supersedes / lineage / row_policy / freshness）
        ↓ 通过 custom_extensions 挂载
Apache Ossie Core Spec（semantic_model / datasets / fields / relationships / metrics / ai_context）
        ↓ converter
下游：dbt MetricFlow / Cube / Doris / BI
```

自研部分**只**负责：compiler（指标 → SQL）、权限下推、评测绑定、治理元数据。

---

## 理由

1. **标准 > 自研**：语义层的本质是跨工具共享"含义"，封闭自研违背其目的
2. **补齐缺口 = 项目价值**：Ossie 不管 ownership/lineage，而这恰是企业不敢用 NL2SQL 的原因
3. **TPC-DS 示例直接可用**：官方 631 行示例与 Atlas 数据集一致，大幅降低建模成本
4. **技术敏感度加分**：能讲清"2026 年 7 月刚进孵化器的标准"及其边界，本身就是架构判断力的证明

---

## 代价与限制（必须公开承认）

| 风险 | 说明 | 缓解 |
|---|---|---|
| **0.2.0 仍是 DRAFT** | schema 明确标注可能变化 | 锁定 `apache/ossie` 具体 commit sha 并写入 README |
| **孵化器项目** | 存在无法毕业的可能 | 治理层与规范层解耦；Ossie 失败时迁移到 dbt YAML 只需换 converter |
| **不含治理字段** | 需自己实现 owner/version/lineage | 通过 custom_extensions 承载，单独 JSON Schema 校验 |
| **方言翻译未完成** | DATABRICKS 等方言为占位 | 明确标注"待实测"，禁止编造 |
| **converter 生态早期** | 官方仅有 dbt/Polaris/Snowflake 三个 | 优先实现 Cube JSON 导出以证明非封闭 |

---

## 什么情况下应该推翻这个决策

- **Ossie 从孵化器毕业失败** → 迁移到 dbt MetricFlow YAML（治理层不变，只换 converter）
- **企业已有 dbt / Cube 资产** → 优先复用，Ossie 作为交换中间格式
- **0.2.0 正式版 schema 与 draft 差异过大** → 重新评估迁移成本

**面试时必须主动说明这三点。** 自研治理层 + 标准规范层的组合，
是为了展示架构判断；不是对所有场景的通用建议。

---

## 验证方式

- [ ] `semantic/ossie/atlas_retail.ossie.yaml` 通过 Ossie 官方 `ossie-schema.json` 校验
- [ ] 治理扩展通过 `semantic/governance/atlas_governance.schema.json` 校验
- [ ] 能导出为 dbt MetricFlow YAML（证明非封闭）
- [ ] 4 个 dataset、3 个 relationship、5 个 metric 可被 compiler 正确消费
