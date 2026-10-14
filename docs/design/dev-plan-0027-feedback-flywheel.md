# 开发计划：ADR-0027 数据飞轮落地（反馈采集 → 候选生成 → 人工落权威源）

- 日期：2026-09-17
- 状态：**F0/T1/T2/T3/T5 DONE，T4 GATED（G1 未解除）**——起点实测见 §0
- 范围：**仅 ADR-0027**（数据飞轮的自动化边界）。阶段三其余项（自助式语义层、交互式引导、
  任务完成度评估）不在本页，见 §3 排除清单。
- 依据：ADR-0027 于 2026-09-17 经用户确认翻 `accepted`；批次唯一合法序取自 0027 决策 ①~⑥ 的
  依赖关系；实现细节以编号引用 0027，不复制正文。
- 本页**不新增任何裁定**。发现 ADR 缺口时只登记为门禁（§1.3 / §2 各卡停批条件），裁定须回 ADR。

## 本页纪律（与 `dev-plan-0017-0025.md` 同源）

1. **不转述**：判据一律以**编号引用**（如「0027 判据 3」「决策 ②」），不复制 ADR 正文；
2. **计数三要素**：凡跨批引用的计数写成「公式 + 撰写时实测值 + 重测时点」，实测值逐条验证归属；
3. **不含工期估算**：AGENTS.md N1 禁止未实测数字，工期无测量口径 → 本页只给**工作项数**（从
   0027 决策条目机械计数）与相对规模标注（S/M/L，**明示为主观量级、不得当承诺**）；
4. **不拍板未裁定项**：§1.3 门禁在裁定前**不得开工对应批次**；
5. **停批条件优先于进度**：命中即停该批回改 0027，不得"先开工后补"。

---

## 0. 起点实测（2026-09-17，本页撰写时逐条复测）

飞轮不是从零起建——采集与 SFT 侧已落地且诚实空转。落地前先钉住"哪些已有、哪些缺、哪些是坑"：

| 事实 | 实测证据 | 复测命令 |
|---|---|---|
| **反馈采集已落地**（一键纠错 5 kind，落 pending_review） | `agent/feedback.py` `submit_feedback` 写 `eval/failures/user_feedback/`，`status=pending_review` | `grep -n "pending_review" agent/feedback.py` |
| **SFT 飞轮五阶段已落地但空转** | `lora/flywheel.py` scan→review→export→build→train；`lora/data/flywheel-state.json` 记空转计数 | `grep -n "def scan\|def export_approved" lora/flywheel.py` |
| **澄清选择 / SQL 修改采集未实现** | 现状只有 `FeedbackKind` 5 值，无"选了哪个澄清候选""用户改了哪段 SQL"的结构化字段 | `grep -n "class FeedbackKind\|clarification\|edited_sql" agent/feedback.py` → 后两者 0 命中 |
| **⚠ 前置缺陷：两类反馈记录 schema 不一致（0027 代价 ①）** | `feedback.py` 写**顶层** `question/kind`；`flywheel.export_approved` 读 `payload["sample"]["question"]` + `["answer_plan"]` → user_feedback 即便人工 approve 也被 export 静默跳过 | `grep -n 'payload.get("sample"' lora/flywheel.py`；对照 `grep -n '"question"' agent/feedback.py` |
| **`_*` 候选区 lint 已天然隔离（0027 代价 ⑤ 落地注记）** | `check_semantic_authority` 豁免 `_*`（`lint.py:70`）；`VALUES_DIR.glob("*.json")` 非递归（`lint.py:197`）；`load_locale_synonyms` 只读固定文件名 | `grep -n 'startswith("_")\|glob("\*.json")' semantic/lint.py` |
| **值域别名"人工追加"入口已在（0027 决策 ② 的落源目标）** | `data/value_profile.py` 机器生成值本体，`semantic/values/*.json` 的 `aliases` 映射人工追加；`parse_profile` 强校验 | `grep -n "aliases" agent/value_domain.py` |
| **（相邻，非本页范围）ADR-0026 已落地** | commit `091ec7e`：`agent/analysis.py` + `/analyze` + `eval/analysis/`。飞轮不依赖它，仅登记避免误认为缺 | `git log --oneline -1 \| cat` |

**起点结论**：真正缺的是**「反馈 → 候选（同义词/值域别名/指标 proposal）」这条沉淀链**，
且必须先修一个会**静默丢样本**的 schema 不一致（前置批次 F0）。权威事实源、门禁、人工闸口**均已存在**，
本计划是把它们接起来并锁死"候选 ≠ 权威源"的不变量，不新造执行面。

---

## 1. 顺序与门禁

### 1.1 唯一合法序

```
F0（前置修复：schema 统一）
   │
   ▼
T1（候选/提案非权威区 + lint 不变量断言）
   │
   ├─▶ T2（同义词 / 值域别名候选生成）  ─┐
   └─▶ T3（指标变更 proposal 生成）      ─┼─▶ T5（运营状态诚实登记 + 文档）
                                          │
   T4（反馈采集扩展：澄清/SQL修改）◀── GATED G1（0027 未裁定字段形态，默认不排期）
```

T2 与 T3 无相互依赖（一个写同义词/值域候选、一个写指标 proposal），可并行；都依赖 T1 的目录与
lint 断言。T5 必须在 T2/T3 之后（要按"实际交付了什么"写能力登记，否则违反 N2）。

### 1.2 每条边的依据（换序的具体后果）

| 边 | 依据 | 若换序的后果 |
|---|---|---|
| F0 最先 | 0027 代价 ①、判据 2 | user_feedback 被 `export_approved` 静默跳过 = 反馈进得来、出不去；后续任何"approved 样本可被导出/归纳"的判据在修好前不可能绿 |
| F0 → T1 | 候选文件也走统一 pending 形态（决策 ④「一个飞轮链路」） | 候选归纳器读到两套 schema，归纳逻辑分裂成两份（违反"同一事实单一实现"） |
| T1 → T2/T3 | 决策 ②③ 的候选落点 + 判据 1/3 的"不写权威源"断言依赖目录先存在 | 沉淀器无处可写，或为图省事直写 `semantic/synonyms/*.yml` → 正面违反决策 ①② 铁律 |
| T2/T3 → T5 | 决策 ⑥（运营状态如实登记三态） | 文档写了"沉淀路径已交付"而实际未交付 = N2；反之漏写 = ADR-0025 代价 ⑦「有限制无能力」同款缺口 |
| T4 挂起 | 0027 决策 ① 提及但**未定义字段形态**（§1.3 G1） | 无裁定的 schema 拍脑袋定 = 本页越权裁定，破坏"ADR 答选什么、计划答怎么做"的分工 |

### 1.3 门禁（开工前须解除；本页不拍板）

| # | 门禁 | 阻塞 | 解除方式 |
|---|---|---|---|
| **G1** | 「澄清选择 / SQL 修改」采集的字段形态未在 0027 决策正文裁定（背景表列其为"未实现"，决策 ① 一句"新增…采集"无 schema） | **T4** | 二选一：① 回 0027 追加决策定义字段（`selected_candidate` / `sql_diff` 等）；② 裁为 **MVP 外**（推荐——既有 5 kind 已足以驱动 T2 候选归纳，扩采集面属增量而非地基）。**裁定前 T4 不开工** |
| **G2** | 候选归纳的触发判据（什么 pending 记录算"可归纳的未命中→已确认归属"）0027 只给意图未给规则 | **T2 内部** | 属实现细节：按 §11 步骤 ③ **先写评测**（gold/合成 pending → 期望候选）钉住规则再实现，不上升为 ADR 门禁。**不阻塞开工**，但 T2 收口判据以该先行评测为准 |

---

## 2. 批次工作卡

每卡五段：**开工前置 / 工作项 / 收口判据 / 提交拆分 / 停批条件**。工作项只写"做什么 + 依据哪个
决策/判据编号"，不写"怎么做"。

### 2.1 F0：反馈记录 schema 统一（前置缺陷修复）

- **依据**：0027 决策 ④、代价 ①、判据 2
- **开工前置**：无（全链入口）
- **入口命令**：`make test`

| # | 工作项 | 依据 | §11 | 规模 |
|---|---|---|---|---|
| F0.1 | 先写回归测试：构造一条 user_feedback（现有顶层形态）+ 一条 failure_collect 样本（`sample` 包裹形态），人工置 `status=approved` 并补合法 `answer_plan`，断言 `export_approved` 对两者**一致导出**（当前 user_feedback 被跳过 = 红）。锁死代价 ① 缺陷 | 判据 2 | ③ 先写测试 | S |
| F0.2 | 统一 pending 形态：二选一并留注释说明取舍——(a) `feedback.py` 改写为 `sample` 包裹形态（对齐 failure_collect），或 (b) `export_approved` 兼容两形态。**取 (a)**（单一形态、下游只读一套），迁移历史 user_feedback 文件的读取兼容 | 决策 ④ | ④ | S |

- **收口判据**：判据 2 绿（approved 的 user_feedback 可被导出）；`make test` 全绿
- **提交拆分**：`fix(feedback): 统一 pending 记录形态，修复 user_feedback 被 export 静默跳过` + `test`
- **停批条件**：若 (a) 令既有 user_feedback 审计文件全部失效且无低成本兼容路径 → 回退 (b)，不硬改（保留审计轨迹，N4 邻接）

### 2.2 T1：非权威候选区 + lint 不变量断言

- **依据**：0027 决策 ①②③、代价 ⑤（落地注记已收窄）、判据 1、判据 3
- **开工前置**：F0 收口
- **入口命令**：`make lint && make test`

| # | 工作项 | 依据 | §11 | 规模 |
|---|---|---|---|---|
| T1.1 | 建目录：`semantic/synonyms/_candidates/`、`semantic/values/_candidates/`、`eval/failures/_proposals/`（proposal 落点在此页拍板，理由见停批下方注记）。放 `.gitkeep` + README 说明"非权威、人工 approve 才落源" | 决策 ②③ | ④ | S |
| T1.2 | 新增 lint/单测断言锁死三处不变量（**不改 lint 逻辑**，见代价 ⑤ 注记）：候选区文件不被 `load_locale_synonyms` / `VALUES_DIR.glob`（非递归）/ `check_value_profiles` 消费；给定候选放入 `_candidates/` 前后 `make lint` 输出**逐字一致** | 判据 1、判据 3 | ③ 先写测试 → ④ | S |

> **proposal 落点在此页定（属"怎么实现"）**：0027 决策 ③ 给了 `eval/failures/_proposals/` 或
> `semantic/migrations/` 旁两个选项。取前者——`semantic/migrations/` 是**已生效**变更的叙述记录，
> 混入**未生效** proposal 语义错位；`eval/failures/` 与反馈同源、天然在 semantic 权威树外，最小化被扫描面。

- **收口判据**：判据 1（飞轮产物只落 `_*` 区/`eval/failures/**`）、判据 3（候选生成前后 `make lint` 逐字一致）
- **提交拆分**：`chore(semantic): 新增飞轮候选/提案非权威目录` + `test(lint): 锁死候选区不被权威加载器消费`
- **停批条件**：若发现某加载器其实会递归扫进 `_*`（与起点实测矛盾）→ 停，回 0027 代价 ⑤ 重开真实修复项

### 2.3 T2：同义词 / 值域别名候选生成

- **依据**：0027 决策 ②、判据 5、G2（先写评测钉规则）
- **开工前置**：T1 收口
- **入口命令**：`make test`

| # | 工作项 | 依据 | §11 | 规模 |
|---|---|---|---|---|
| T2.1 | **先写评测（G2 解除）**：合成 pending（含"未命中措辞 + 经澄清/纠错确认归属"）→ 期望候选（同义词提议 / 值域别名提议，各含 `source` 反馈路径、措辞原文、建议归属对象）。规则以测试为准，不内联猜测 | 决策 ②、G2 | ③ 先写评测 | M |
| T2.2 | 实现归纳器（新模块，**离线、纯函数、无 I/O/LLM**）：读 pending → 产候选 → 落 `_candidates/`；**绝不写** `synonyms/*.yml`、值域 `aliases`、向量库 | 决策 ②① 铁律 | ④ | M |
| T2.3 | 人工落源 + 派生索引重建验证：文档化"人工把候选追加进 Git → 重跑既有 embedding 流程"，断言 `retrieval/` 命中来自 Git 内容（无运行时直写接口 = 结构不变量） | 判据 5 | ⑦ 文档 | S |

- **收口判据**：判据 3（候选不自动生效：生成前后权威源逐字一致）、判据 5（索引派生自 Git）
- **提交拆分**：`feat(flywheel): 同义词/值域别名候选归纳（非权威、人工落源）`；若测试期发现需动 `agent/value_domain` 只读接口，单列
- **停批条件**：若归纳规则无法在不用 LLM 的前提下确定性表达（G2 先行评测跑不出稳定期望）→ 停，登记为候选生成需 LLM 参与、回 0027 重开"候选生成是否算自动段"（决策 ① 现假定确定性归纳）

### 2.4 T3：指标 / 语义层变更 proposal 生成

- **依据**：0027 决策 ③、判据 4
- **开工前置**：T1 收口（与 T2 并行）
- **入口命令**：`make test`

| # | 工作项 | 依据 | §11 | 规模 |
|---|---|---|---|---|
| T3.1 | 先写评测：给定 `wrong_metric`/`wrong_value` pending → 期望 proposal（受影响 metric、变更类型、证据反馈路径列表、疑似正确定义草案） | 决策 ③ | ③ 先写评测 | M |
| T3.2 | 实现 proposal 生成器（离线纯函数）：产结构化 proposal 落 `_proposals/`，**不提交、不改写** `semantic/ossie/*.yaml` | 决策 ③ | ④ | M |
| T3.3 | 只读验证（判据 4）：飞轮运行前后 `semantic/ossie/` 的 `git diff` 为空（用 tmp 副本仓或 monkeypatch 断言写入被拒/未发生） | 判据 4 | ⑤ 安全（守权威源） | S |

- **收口判据**：判据 4（proposal 只读：`git diff semantic/ossie` 空）
- **提交拆分**：`feat(flywheel): 指标变更 proposal 生成（只读，生效走 lint+eval 门禁）`
- **停批条件**：若 proposal 生成不可避免地需要写 `semantic/migrations/`（与本页拍板冲突）→ 停，回 0027 决策 ③ 重定落点

### 2.5 T4：反馈采集面扩展（澄清选择 / SQL 修改）—— **GATED，默认不排期**

- **依据**：0027 决策 ①、门禁 G1
- **开工前置**：G1 解除（回 0027 补决策定义字段形态，或裁为 MVP 外）
- **工作项**：留空（不得在本页拍板未裁定的 schema）。解除后按 §11「先写评测 → 采集实现」补卡。
- **停批条件**：G1 未解除 → 本批不排期（AGENTS §10 优先级 1 诚实 > 进度）

### 2.6 T5：运营状态诚实登记 + 文档同步

- **依据**：0027 决策 ⑥、判据 6、N2/N4
- **开工前置**：T2/T3 收口（按实际交付写登记）
- **入口命令**：`grep` 自查 + `make lint`

| # | 工作项 | 依据 | §11 | 规模 |
|---|---|---|---|---|
| T5.1 | README 能力登记表新增"数据飞轮"行：SFT 侧机制**已落地/空转**、语义沉淀路径（T2/T3）**本次交付/待实现**按落地日实况写；Known Limitations 追加"沉淀需人工落源、非实时自进化；任务完成度/缓存/LLM 改提示词未实现"，**不删既有 KL** | 决策 ⑥、判据 6、N4 | ⑦ 文档 | S |
| T5.2 | `grep` 自查文档无未溯源效果数字（无"飞轮提升准确率 X%"字样），效果数字只能指向 `eval/reports/<sha>.json` | 判据 6、N1 | ⑦ 文档 | S |

- **收口判据**：判据 6（三态表述准确、无造假数字）
- **提交拆分**：`docs(flywheel): README 能力登记 + KL 同步（三态诚实，ADR-0027 判据 6）`
- **停批条件**：无（纯文档，但若发现与实测不符即回改对应批次，不在文档里粉饰）

---

## 3. 明确排除（不在本计划，越界即违 0027 决策 ⑤）

| 项 | 归属 | 排除理由 |
|---|---|---|
| 任务完成度评估体系（text2insight §3.3.4） | **独立 ADR** | 与 ADR-0010 的 EX/Plan Acc 二元口径直接冲突（"废弃单一 SQL 准确率"须先仲裁评测地基），不搭飞轮便车偷偷改口径 |
| 高频查询自动优化缓存 | 设计性不做 | 当前无缓存层、无 profile 证据（0027 决策 ⑤）；触发条件见 0027"什么情况下应该推翻" |
| LLM 自动改写提示词/口径 | 设计性不做 | 提示词走 `agent/prompts/*.yaml` + CI 回归（PromptOps，AGENTS 7.4），至多产 proposal |
| 自助式语义层 / 交互式引导（§3.3.1/3.3.2） | 各自 ADR | 自助语义层触及 ADR-0002 Git 唯一事实源与 N8，需独立裁定，非飞轮范围 |

---

## 4. 提交拆分总则（AGENTS.md §8）

- `fix(feedback)`（F0）/ `chore(semantic)`（T1.1）/ `test(lint)`（T1.2）/ `feat(flywheel)`（T2/T3）/
  `docs(flywheel)`（T5）各独立提交；
- **不混 `refactor` + `semantic`**：若候选生成/落源触碰 `semantic/` 权威文件，属**人工**动作、
  单独 `semantic` 提交并说明影响面（哪些指标/维度受影响），不得由飞轮代码自动产生；
- `semantic/` 变更须说明影响面（AGENTS §8）；本页批次**不自动改语义层**（决策 ③），故正常不出现
  由代码触发的 `semantic` 提交——出现即说明越界，回停批条件。

---

## 5. 收口总判据（映射 0027 验证方式，逐批复验）

| 0027 判据 | 落在哪批 | 复验命令（示意，取可机读式而非行号） |
|---|---|---|
| 1 单一事实源不破坏 | T1.2 | `grep -rn "write" agent/flywheel* lora/flywheel.py` 无写权威源路径 + 单测断言产物仅在 `_*`/`eval/failures/**` |
| 2 schema 统一 | F0.1 | `pytest tests/test_flywheel.py -k user_feedback_export` 绿 |
| 3 候选不自动生效 | T1.2 / T2 | 候选生成前后 `make lint` 输出 diff 为空 |
| 4 指标 proposal 只读 | T3.3 | `git diff semantic/ossie/` 在飞轮运行后为空 |
| 5 派生索引重建 | T2.3 | Git 同义词变更后重跑 embedding，`retrieval/` 命中来自 Git |
| 6 运营状态诚实 | T5.1/T5.2 | `grep -n "已闭环\|提升准确率\|已自进化" README.md` → 0 命中 |
| 7 全量回归 | 每批收口 | `make lint && make test && make eval`；SFT 轮转按 ADR-0008 在有样本/GPU 时补 |

**总证伪条件**（同 0027）：判据 1/3/5 任一显示飞轮改了权威源或向量库被直写 → 飞轮退化为
"仅产报告、不产任何可生效产物"；判据 2 证实两形态无法在不破坏 pending 契约下统一 → 拆两条导出通道（增实现成本，不改红线）。
