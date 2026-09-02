# ⚠️ 本目录已废弃（Deprecated）

**原因**：ADR-0002 决定采用 **Apache Ossie** 作为语义层规范（见 `infra/adr/0002-ossie-as-semantic-spec.md`）。

| 文件 | 状态 |
|---|---|
| `metric.schema.json` | 已废弃，保留作设计演进对照 |
| `dimension.schema.json` | 已废弃，保留作设计演进对照 |

**新的语义层位置**：
- 规范层：`semantic/ossie/atlas_finance.ossie.yaml`（**主模型**，金融 TPC-DI + FIBO 对齐）
- 对照：`semantic/ossie/atlas_retail.ossie.yaml`（零售 TPC-DS，历史对照，不再扩展）
- 治理层：`semantic/governance/atlas_governance.schema.json`（Atlas 扩展，补 Ossie 缺口）

这两个旧文件**不再被 CI 校验**，保留仅供追溯设计演进过程。
