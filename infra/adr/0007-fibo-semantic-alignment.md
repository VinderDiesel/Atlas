# ADR-0007：引入 FIBO 本体（L2 概念对齐层）

- 日期：2026-09-02
- 状态：accepted
- 相关：ADR-0002（Ossie 语义规范）、ADR-0006（金融场景切换）、`docs/Apache技术栈改造说明.md`、`semantic/governance/`

---

## 背景

**FIBO（Financial Industry Business Ontology）** 是金融行业业务本体：
EDM Council 维护、**OMG 标准**，用 OWL/描述逻辑定义金融概念及其关系的无歧义、机器可读模型。
2020 年起开放社区开发，季度发布（2026 Q2 Production 约 196 个包），GitHub 公开。

**关键事实（2026-09 核实）**：

| 项 | 事实 |
|---|---|
| 定位 | "字典 + 语法 + 知识图谱"：Legal Entity / Party / Counterparty / Bond / Loan / Derivative / maturity date / exposure 等 |
| 结构 | 10 个域：FND（基础）/ BE（商业实体）/ FBC（金融业务与商业）/ SEC（证券）/ DER（衍生品）/ LOAN（贷款）/ MD（市场数据）/ IND（指数与指标）/ CAE（公司行为）/ BP（业务流程） |
| **许可证** | **GitHub 仓库 MIT License**（2020 EDM Council）——可商用、可修改、可再分发，仅需保留版权声明；FIBO 名称为 EDM Council 商标，需署名注明 |
| 官方 AI 叙事 | 用于 AI 数据发现与分类、BCBS 239 监管报告一致性、KYC/AML、跨系统数据标准化 |

**引入动机**：

1. **补 Ossie 的"信任"缺口**（ADR-0002 已识别）：Ossie 标准化"定义"不标准化"信任"——
   FIBO 概念映射恰好是"认证过的业务含义"的可审计载体
2. **金融术语歧义**（ADR-0006 已识别）：counterparty / client / customer 在不同系统语义不一，
   FIBO 提供共享锚点（如 Legal Entity / Party）
3. **金融场景锚点已就位**：ADR-0006 切换后，语义模型字段与 FIBO 概念天然对应
4. **Schema Linking 增强**：问句中的业务术语 → FIBO 概念 → 语义模型字段，
   比"同义词表 + 向量"多一层标准化的泛化路径

**边界（不可动摇）**：FIBO 是**概念本体**，不是指标计算规范、不携带数据。
指标表达仍归 Ossie，计算仍归自研 Compiler。FIBO 不进入查询路径。

---

## 备选方案

| 方案 | 内容 | 优势 | 约束 |
|---|---|---|---|
| **L2 概念对齐层**（选定） | Ossie 模型字段 ↔ FIBO 类/属性的可审计映射（挂 `custom_extensions`），CI 校验映射，评测新增"概念映射准确率" | 补信任缺口 + 标准化概念 + 可评测；价值最高 | 映射维护工作量持续存在 |
| L1 仅术语增强 | 用 FIBO 概念扩充同义词表 / 查询扩展 | 成本最低 | 无治理价值，映射不可审计，叙事单薄 |
| L3 全量知识图谱 | 加载 FIBO 子集 + OWL 推理引擎，Agent 沿图谱推理 | 推理能力最强 | FIBO 体量巨大、import 闭包复杂、学习成本陡增、评测难做，超出 MVP |
| 不引入 | 维持同义词表 + 向量检索 | 零成本 | 无法回答"金融概念凭什么标准"的追问 |

---

## 决策

**采用 L2 概念对齐层**，具体约定：

1. **子集化引入**：只取 FND（Foundations，必取，定义 LegalEntity/Party/Contract 等基础概念）
   与场景域（SEC / MD / FBC 的 import 闭包子集），不加载全量 196 包
2. **映射位置**：Ossie `custom_extensions`（`vendor_name: ATLAS`）新增
   `fibo_alignment` 节点，格式：`{ dataset_field: { fibo_class: <IRI>, match_type: exact_match|broad_match|related, confidence: <实测> } }`
3. **工具链**：`rdflib` 解析 FIBO OWL 文件（新增依赖，MIT 兼容，无许可证冲突）；
   MVP 不做推理，只做 TBox 查询（概念层级 + 属性）
4. **CI 校验**：映射完整性（每个金融指标必须至少对齐一个 FIBO 概念）、
   FIBO IRI 存在性校验（防止引用失效概念）
5. **评测**：EVAL_REPORT 新增"概念映射准确率"（问句业务术语 → FIBO 概念 IRI 的一致率），
   与 EX / Plan Acc 分开报告；黄金集问句人工标注 FIBO 概念
6. **可解释**：Agent 解释结果时引用 FIBO 概念定义（`fibo:definition`），
   形成"业务术语 → 标准概念 → 语义字段 → SQL"的可审计链条

分层关系：

```
FIBO（概念本体层：金融业务"是什么"，OMG 标准，OWL）
   ↓ 概念对齐映射（fibo_alignment，custom_extensions 承载）
Apache Ossie（语义规范层：指标/字段"怎么表达"，Apache 孵化）
   ↓ 自研 Compiler
Iceberg + Polaris + Doris（物理层）
```

---

## 理由

1. **补 Ossie 信任缺口 = 项目核心价值**：ADR-0002 已论证"Ossie 不标准化信任"是企业落地最大缺口，
   FIBO 映射给出标准化的答案，且可审计、可评测
2. **双标准叙事升级**：Ossie（Apache 孵化，语义表达）+ FIBO（OMG 标准，金融概念）——
   从"全栈 Apache"升级为"行业标准双支柱"，面试叙事更完整（FIBO 非 Apache 项目，
   但 ADR-0004 口径是"尽可能"采用 Apache，且 FIBO 是 OMG 标准，属于行业标准范畴）
3. **许可证无风险**：MIT 许可比 CC 系列更宽松，个人项目可放心使用；商标署名要求成本极低
4. **与官方 AI 趋势对齐**：EDM Council 官方将 FIBO 定位为 AI 数据发现/分类的基础，
   与 Atlas 的 Schema Linking / 检索增强方向一致
5. **克制引入**：L2 不引入推理引擎、不进入查询路径、不做全量加载——
   避免过度设计，符合 AGENTS.md 决策优先级（简洁 > 性能）

---

## 代价与限制

| 风险 | 说明 | 缓解 |
|---|---|---|
| 子集化复杂 | FIBO import 依赖链深，需计算 import 闭包 | MVP 只取 FND + 2-3 个场景包，逐包验证可加载 |
| 映射维护工作量 | 每个新指标都要做概念对齐 | CI 强制校验；映射文件与指标同 PR 提交（禁止"先指标后映射"） |
| 概念映射准确率未实测 | 可能低于预期，影响叙事 | 数字必须实测后填写；若实测显著低于可接受阈值 → 降级 L1 |
| FIBO 季度更新 | 概念 IRI 可能变化 | 锁定 FIBO release（记录具体 commit/tag sha），随语义层版本演进 |
| 新增依赖 | `rdflib` 进入依赖树 | MIT 兼容，无冲突；仅解析层使用，不进入查询路径 |
| 商标与署名 | FIBO 是 EDM Council 商标 | README 与 NOTICE 注明"FIBO is a trademark of EDM Council, Inc." |
| 面试追问"为何不全量用 FIBO" | — | 标准答案：FIBO 无数据、无指标计算语义；Ossie 管表达、FIBO 管概念，分层职责不同 |

---

## 什么情况下应该推翻

- **概念映射准确率实测显著低于阈值**（`<待实测后填写>`）且定位不到修复路径 → 降级为 L1（仅术语增强）
- **FIBO 许可证变更**或商标条款收紧 → 立即评估替代（如自建金融概念表，但会失去标准背书）
- **映射维护成本挤压主线进度**（8 周计划）→ 冻结映射，只保留已完成的子集
- **Ossie 毕业失败（ADR-0002 被推翻）** → FIBO 映射迁移到 dbt MetricFlow 语义模型，概念层不受影响

---

## 验证方式

- [x] FIBO 子集（FND + 场景包）可被 `rdflib` 加载，TBox 查询返回正确概念层级
      （2026-09-02 实测：55 文件 / 22,730 三元组 / 0.3s；Party→Person/LegalPerson/Organization、
      Organization→Formal/Informal/SubUnit、Contract→Verbal/Written；复现见 `data/fibo/README.md`）
      **重要发现：FIBO 2.0 基础概念（Party 等）已下沉至 OMG Commons，映射设计需优先查 Commons**
- [x] `atlas_finance.ossie.yaml` 每个指标均有 `fibo_alignment`，通过 CI 校验（IRI 存在性）
      （2026-09-02 实测：17 条映射 / 26 条权威候选 IRI 全部存在；校验脚本
      `data/fibo/validate_alignments.py`，概念来源扩展种子见 `data/fibo/check_iris.py`）
      **发现：AllFND import 闭包不含 TransactionsExt/FBC 域，交易/账户/证券概念
      需显式加入扩展种子；类名前缀以本体实际声明为准（fibo-fnd-txn-mkt）**
- [ ] 概念映射准确率实测值进入 EVAL_REPORT（与 EX / Plan Acc 分开报告）
- [ ] 至少 3 条黄金集问句展示"业务术语 → FIBO 概念 → 语义字段 → SQL"的完整可解释链
- [ ] 检索链路中，FIBO 概念扩展确实提升 Schema Linking 的 Recall@K（对照实验，`<待实测>`）
