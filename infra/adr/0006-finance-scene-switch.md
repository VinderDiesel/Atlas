# ADR-0006：场景切换至金融（TPC-DS → TPC-DI + BIRD finance）

- 日期：2026-09-02
- 状态：accepted
- 相关：ADR-0001（为什么用 TPC-DS 而非真实数据）、ADR-0007（FIBO 引入）、README 第 0/7 节、`semantic/ossie/`、`eval/gold/`

---

## 背景

原场景为零售分析（TPC-DS SF1 重建），语义模型 `atlas_retail.ossie.yaml` 以
GMV / 销量 / 客单价 / 门店 / 品类为核心。切换动机：

1. **目标城市聚焦**：求职目标含上海，金融数据平台岗位密度高于零售；
   金融场景叙事对目标岗位（AI 数据平台架构师 / Data Agent 方向负责人）匹配度更高
2. **FIBO 引入的前提**（见 ADR-0007）：FIBO 是金融业务本体，
   零售场景下无对应锚点，引入意义不大；切换到金融后 FIBO 才有用武之地
3. **叙事收窄**：从"通用零售分析"收窄为"金融数据治理 + 语义层 + AI"，差异化更清晰

**约束（继承 ADR-0001，不可动摇）**：
- 数据必须来自公开基准，可自行生成、可复现，不与任何雇主真实数据挂钩
- 评测绑定固定快照 sha / Iceberg snapshot id
- 黄金集人工标注，禁止自动生成充数

---

## 备选方案

| 方案 | 优势 | 约束 |
|---|---|---|
| **TPC-DI**（零售经纪） | TPC 官方基准，与 TPC-DS 同体系、叙事无缝延续；schema 自带金融概念（账户/证券/交易/持仓/现金）；含异构源 ETL 与合规审计场景；工具免费（注册下载，与 TPC-DS 相同模式） | 数据集成基准，schema 规模小于 TPC-DS（7 维 + 4 事实）；工具需注册同意许可条款 |
| BIRD finance 子集 | 公开 NL2SQL 基准，可直接做对照 | schema 过于简单（个位数表），撑不起"湖仓 + 语义层 + 治理"叙事；无标准 ETL 流程 |
| 自造数据（按 FIBO 概念虚构） | 概念与数据完全可控 | 评测可信度弱于公开基准，违背"可复现"原则；面试难自证 |
| 维持零售（不切换） | 零成本 | 无法承载 FIBO 引入；目标城市叙事不聚焦 |

---

## 决策

**主数据源切换为 TPC-DI（retail brokerage 场景），BIRD finance 作为公开集对照**
（替换 Spider；Spider 为通用领域，与金融场景不匹配，不再作为对照集）。
Spider / BIRD 混报禁令不变，对照口径分开报告。

数据流：

```
TPC-DI 工具生成（公开基准，注册下载）
    ↓ Spark ETL（异构源：OLTP / CSV / 金融参考数据）
Apache Iceberg 表（MinIO）—— DimBroker/DimAccount/DimCustomer/DimSecurity/DimDate/DimTrade/DimProspect
    + FactTrades/FactHoldings/FactCashBalances/FactWatches
    ↓ Polaris (REST Catalog + RBAC) + Doris 4.1
Atlas 金融语义模型（Ossie + ATLAS 治理扩展）
    ↓ Compiler → 只读 Guard → Doris
```

**影响面（诚实清单）**：

| 资产 | 处理 |
|---|---|
| `semantic/ossie/atlas_retail.ossie.yaml` | 重写为 `atlas_finance.ossie.yaml`；零售版保留为演进对照（README 标注"已废弃"） |
| `eval/gold/*.json`（50 例目标） | 全部重写为金融问句（交易/持仓/现金/客户分层/佣金） |
| `semantic/policies/row_policy.yml` | 行级策略从"region/品类"→"分支行/客户分层/机构" |
| 文档（README / 改造说明 / EVAL_REPORT 口径 / 逐日任务清单 / 术语表） | 同步更新；术语表新增金融术语（金融场景下"同义词混用"风险更高） |
| `agent/security/sql_guard.py` | **复用**（场景无关）；默认 dialect 由 clickhouse 改为 doris |
| Makefile / docker-compose / CI / ADR 框架 | **复用** |
| `semantic/schema/`、`models/`、`metrics/`、`dimensions/`、`synonyms/` | 已废弃自研 DSL，不投入重写，仅作演进对照 |

---

## 理由

1. **公开基准优先原则不变**：TPC-DI 与 TPC-DS 同属 TPC 体系，续用"公开基准重建场景"的诚实叙事，不引入任何真实金融数据
2. **schema 自带金融语义**：证券（DimSecurity）、交易（FactTrades）、持仓（FactHoldings）、现金余额（FactCashBalances）、账户与客户分层（DimAccount/DimCustomer）天然匹配金融指标与行级权限设计
3. **合规/审计场景**：TPC-DI 数据源含合规审计、客户分析场景，与 FIBO 的监管叙事（BCBS 239 / KYC / AML）对齐
4. **FIBO 锚点**：金融语义模型是 ADR-0007 概念对齐层的载体，两者互为放大器
5. **对照集聚焦**：BIRD finance 比 Spider 更贴合"金融 NL2SQL 能力"的对照目标

---

## 代价与限制

| 风险 | 说明 | 缓解 |
|---|---|---|
| 存量零售资产作废 | 语义模型与黄金集重写（预估第 2 周存量工作重做） | 零售版保留为演进对照，不删除（诚实留痕） |
| TPC-DI 工具需注册 | 免费但需同意许可条款、邮箱下载 | 与 TPC-DS 相同模式，ADR-0001 合规路径延续；注册信息不写入仓库 |
| schema 规模小于 TPC-DS | 表数量少，"湖仓"叙事撑分量有限 | 用"异构源 ETL + 增量/CDC + 语义层治理"补叙事，不夸大表规模 |
| 金融术语歧义风险 | counterparty/client/customer 等词在金融行业语义不一 | 术语表强化；语义模型 ai_context.synonyms 收敛；歧义问句必须反问（黄金集含 ambiguous 用例） |
| 求职叙事波动 | 面试官可能追问"为什么放弃零售" | 理由已固化在本 ADR：目标城市聚焦 + FIBO 锚点 |

---

## 什么情况下应该推翻

- **TPC-DI 工具不可用或数据生成过于复杂** → 主数据源退回 BIRD finance 子集（FIBO 锚点改挂 BIRD schema）
- **FIBO 引入失败（ADR-0007 被推翻）** → 金融场景仍可独立成立，保留切换（无需回退）
- **目标岗位转向非金融方向** → 重新评估场景选择，保留本 ADR 作为决策对照

---

## 验证方式

- [x] `semantic/ossie/atlas_finance.ossie.yaml` 通过治理扩展校验（`make lint` 的 ossie_validate 模块待落地，
      2026-09-02 用 `data/fibo/validate_alignments.py` 完成等价校验：governance schema + 17 条 FIBO 映射 IRI 存在性）
- [ ] 黄金集 50 例金融问句全部有 `result_hash` 与 `snapshot_sha`（绑定 Iceberg snapshot id）
- [ ] `make eval` 可跑通，报告按"自建黄金集 / BIRD finance 对照"分开输出
- [ ] 行级策略按分支行/客户分层下推的 SQL 谓词可解释、可审计
- [ ] sql_guard 默认 dialect 为 doris，安全测试集（恶意 SQL 10 例）全部拦截
