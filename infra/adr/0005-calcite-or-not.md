# ADR-0005：Apache Calcite 的取舍（MVP 不用，但必须讲清为什么）

- 日期：2026-09-01
- 状态：**accepted（MVP 阶段不引入；Phase 2 评估）**
- 相关：ADR-0004、AGENTS.md 第 5 节

---

## 背景

要求"使用更多成熟技术"，Calcite 是 SQL 解析/校验/优化领域最成熟的 Apache 项目。
需要判断 Atlas 是否应该用它替代 sqlglot。

**Calcite 关键事实**（2026-09 核实）：

- 定位：**动态数据管理框架**，不是数据库。提供标准 SQL parser、validator、JDBC driver
- 核心能力：关系代数表示、基于代价的优化器（CBO）、可插拔规则、异构数据源适配器
- **不管理存储**，无内置分布式执行
- 使用者（部分）：Apache Hive（CBO）、Apache Flink、Apache Kylin、Apache Drill、
  Apache Phoenix、Apache Samza、Alibaba MaxCompute、Dremio、Qubole Quark
- **Java-only**：需要 JVM，无原生 Python 绑定

---

## 备選方案

| 方案 | 优势 | 约束 |
|---|---|---|
| **sqlglot（Python）** | Python 原生；纯解析/转译，无 JVM 依赖；方言覆盖广 | 无 CBO；不校验语义正确性 |
| **Apache Calcite（Java）** | 工业级 validator + CBO；Flink/Hive/Kylin 底座 | Java-only；需独立 JVM 服务；学习曲线陡 |
| 两者并用 | 各取所长 | 架构复杂度显著上升 |
| **DataFusion（Rust/Python）** | 有 Python 绑定；性能强 | 非 Apache 顶级项目（ASF 但有独立治理）；生态弱于 Calcite |

---

## 决策

**MVP 阶段（P0–P2）只使用 sqlglot；Calcite 列入 Phase 2 评估项，不进入 MVP 关键路径。**

理由：

1. **Atlas 的核心难点不在优化器**。项目要证明的是"语义层 → 安全 → 评测 → 可解释"
   这个闭环，Calcite 的 CBO 对这条链路不是必需项
2. **引入 JVM 会让 Python 项目变成双语言栈**，8 周业余时间内风险过高
3. **sqlglot 已足够做只读校验**：AST 层禁 DDL/DML、函数黑名单、LIMIT 注入、
   策略谓词注入——这些都不需要 CBO
4. **Calcite 真正值钱的地方（CBO、物化视图改写、跨源联邦）在 MVP 用不上**

---

## 但必须在面试中讲清：什么时候该用 Calcite

诚实答案是这几条，能讲出来比"我用了 Calcite"更有说服力：

1. **需要跨数据源联邦查询**时（Iceberg + MySQL + API 混查），Calcite 的 adapter 机制是标准解法
2. **需要物化视图自动改写**时（指标预聚合加速），Calcite 的 `MaterializedViewRule` 远优于手写路由
3. **需要完整的 SQL 语义校验**时（列是否存在、类型是否匹配），
   Calcite validator 比 sqlglot 的纯语法解析严格得多
4. **需要对接企业现有 Hive/Flink 栈**时，用 Calcite 可以保证方言与语义一致
5. **查询性能成为瓶颈**时，CBO 是正解——但前提是**先有 profile 数据证明瓶颈在优化器**

**如果 Atlas 演进到"自动生成物化视图 + 自动路由"阶段，我会引入 Calcite。**
当前 MVP 没有这个需求，提前引入是过度设计。

---

## 代价与限制

- sqlglot 不做语义校验：列不存在、类型不匹配不会被发现
  → 缓解：执行前用 Doris 的 `EXPLAIN` 做一次 dry-run 校验（成本远低于引入 Calcite）
- 方言覆盖：Ossie 的 `DATABRICKS` / `BIGQUERY` 等方言需在 Phase 2 补
  → 缓解：MVP 只用 `ANSI_SQL` + Doris 方言，其余标注"未验证"
- 未来迁移 Calcite 需要重写 compiler 的 AST 层
  → 缓解：compiler 输出层已隔离（Plan → AST → SQL），替换 AST 后端影响可控

---

## 验证方式

- [ ] MVP 结束时能明确列出 sqlglot 的 3 个具体不足（有实例，不是空谈）
- [ ] 能画出"如果引入 Calcite，架构会怎么改"的图
- [ ] 能说清引入 Calcite 后需要额外投入多少工作量

**能回答这三点，就证明这个决策是权衡的结果，而不是不懂 Calcite。**

### 已实测的 sqlglot 不足（截至 2026-09，1/3 例）

1. `Select.set("from", exp.From(...))` 不生效：30.17 中 `set()` 写入的 from 在 `sql()` 输出时丢失，
   必须改在 `exp.Select(expressions=..., from_=...)` 构造时传入（`agent/compiler.py` 有注释与契约测试覆盖）。
   实例：`tests/test_compiler.py::test_gold_101_quarter_total_trade_value` 首次运行输出
   `SELECT ... INNER JOIN ...`（无 FROM），修复后输出完整 `FROM ... AS fact_trades`。

其余不足项待 MVP 过程中继续实测积累，不预填。
