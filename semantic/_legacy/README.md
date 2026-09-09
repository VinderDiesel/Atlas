# ❌ 本目录是已废弃的自研 DSL 定义（Archived，勿引用、勿扩展）

**原因**：ADR-0002 决定采用 **Apache Ossie** 作为语义层规范
（`infra/adr/0002-ossie-as-semantic-spec.md`）。本目录内容是 ADR-0002 之前的
自研 DSL 遗留物，**权威语义源唯一 = `semantic/ossie/`**。

## 为什么归档而不是删除

这些文件在仓库里长期处于"看起来还活着"的状态，比死代码更贵：

| 事实 | 后果 |
|---|---|
| `metrics/gmv.yml` 仍标 `status: active` | 读者/新工具会以为 GMV 口径仍由它定义 |
| 引用 `dwd.fact_order_line` / `dwd.dim_order` / `dwd.dim_region` | 锁定快照 `7d48dcb` 的 25 张表里**一张都不存在** |
| `metrics/gmv.yml` 引用 `gold/gmv_by_region_month.yml` | 黄金集里没有该文件（悬空引用） |
| `semantic/lint.py` 不覆盖本目录 | 漂移无人报警 |
| 全仓零代码引用（`grep` 核实） | 删不掉只是因为"留着当对照"，不是因为有消费者 |

保留价值仅为**设计演进对照**（ADR-0006 §"已废弃自研 DSL，不投入重写"）。
2026-09 起从各原目录集中到此处，由 `semantic/lint.py` 的权威源唯一性检查锁定：
`semantic/` 下除 `ossie/`、`synonyms/`、`policies/`、`_legacy/` 外不得存在语义定义文件。

## 内容清单

| 文件 | 原目录 | 说明 |
|---|---|---|
| `schema/metric.schema.json` | `semantic/schema/` | 自研 DSL 的 JSON Schema（原 README 已标废弃） |
| `schema/dimension.schema.json` | `semantic/schema/` | 同上 |
| `models/orders.yml` | `semantic/models/` | SemanticModel 早期形态（base_table/measures/joins） |
| `metrics/gmv.yml` | `semantic/metrics/` | GMV 指标早期定义（`status: active` 已失效） |
| `dimensions/region.yml` | `semantic/dimensions/` | 地区维度（valid_values 手写值域） |
| `dimensions/order_date.yml` | `semantic/dimensions/` | 订单时间维度 |
| `synonyms/business_terms.yml` | `semantic/synonyms/` | 术语→gmv 映射（指向已废弃指标名） |

## 现在这些职责在哪里

| 职责 | 现位置 |
|---|---|
| 指标/维度/关系定义 | `semantic/ossie/atlas_finance.ossie.yaml`、`atlas_retail.ossie.yaml` |
| 治理扩展（owner/lineage/row_policy 等） | `semantic/governance/atlas_governance.schema.json` + 模型内 `custom_extensions` |
| 行级权限声明 | `semantic/policies/row_policy.yml` |
| 英文同义词表 | `semantic/synonyms/en_us.yml`（ADR-0015） |
| 结构校验 | `make lint`（ossie_validate + governance_validate + gold schema + 权威源唯一性） |
