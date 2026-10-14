# ADR-0032：概念层接入运行时（本体驱动的检索/解析/解释增强）

- 日期：2026-10-08
- 状态：**accepted（2026-10-08 文档评审通过——决策经逐项确认 + 引用核实 18 项 / 精度修正 2 处）；实现未启动**（M0 冻结基线零功能代码；落地前须跑「验证方式」全部判据；对外不得写「已实现/已上线」，N2）
- 相关：ADR-0007（FIBO L2 概念对齐层；本 ADR 修订其"FIBO 不进入查询路径"边界）、ADR-0015（locale 词典族；本 ADR 扩展 `concepts_*.yml` 子族）、ADR-0016（"机器生成 + 人工别名 + 绑定 sha"模式的镜像对象）、ADR-0027（失败样本回流）、ADR-0030 ⑤（注入式设计哲学）；代码：`agent/planner.py` / `agent/tools/schema_linker.py` / `agent/graph.py` / `retrieval/graph_store.py` / `semantic/ossie/atlas_finance.ossie.yaml` / `data/fibo/` / `eval/paraphrase_eval.py` / `eval/schema_link_eval.py` / `eval/gold/schema.json`

---

## 背景

**现状（2026-10-08 代码核实）：**

1. **FIBO 资产停在展示层**。`atlas_finance.ossie.yaml` 有 31 条 `fibo_alignment`
   映射（键形如 `dim_account` / `dim_broker.Branch` / `dim_account->dim_customer` /
   `metric:total_trade_value`，覆盖审计见 `data/fibo/README.md`），经
   `data/fibo/validate_alignments.py` + `iri_registry.json`（29 条权威 IRI）CI 校验；
   但**运行时消费点为 0**——唯一消费方是治理 API（`serving/governance.py`）与前端
   治理展示。ADR-0007 的三个验证项（概念映射准确率入报告 / ≥3 条解释链 /
   检索 Recall@K 对照）**全部未兑现**。
2. **泛化缺口有测量工具但集太薄**。Planner（`agent/planner.py`）为同义词子串匹配；
   OOV 措辞的下场是候选链或反问（`retrieve` 节点仅在兜底路径执行，`agent/graph.py`）。
   `eval/paraphrase_eval.py` 自述"这是项目当前最大的未知风险"，但当前集仅 13 条、
   以注册同义词内改写为主——**测不出"新词汇泛化"**。
3. **评测载体早已铺好但无人消费**。`eval/gold/schema.json` 已有 `fibo_concepts`
   字段（`{term, concept, role, note}`，注释自述"ADR-0007 可解释链的评测载体"），
   **86 个 gold 样本已标注（共 162 条）**，没有任何评测脚本消费它（`eval/gold/validate_gold.py` 仅做 IRI 存在性校验，不作对照）。
4. **边界冲突必须先处理**。ADR-0007 明文"FIBO 不进入查询路径"；本次目标要求概念层
   参与解析与候选——按项目惯例（不改正文、末端注记）修订边界，而不是绕行。
5. **约束（AGENTS.md）**：N1 数字必须来自脚本产物、N2 设计不写成已完成、决策优先级
   诚实 > 安全 > 可复现 > 确定性优先 > 简洁；新增能力须先 ADR（§11）。

**目标三分与三缝映射**：泛化（OOV 措辞的确定性解析）→ 解析缝；准确性（候选召回与
解析正确）→ 检索缝；可解释性（术语→概念→字段→SQL 审计链）→ 解释缝。

## 备选方案

| 方案 | 优势 | 劣势 |
|---|---|---|
| **概念层接入运行时（选定）** | 兑现 ADR-0007 全部待办；三目标同源（共用概念索引）；复用既有评测与注入式基建 | AOT 构建纪律 + 别名 curation 持续人工投入；解析缝收益受映射粒度限制（待实测） |
| 维持现状（只做 L2 展示层） | 零成本零风险 | 泛化掉落不改善；ADR-0007 待办永远悬空；"概念层提升准确性"叙事无载体 |
| 运行时 rdflib 动态加载 | 免构建、随本地本体永远新鲜 | `fibo-src` 为 gitignore 资产（"仅本地使用，不入库"）→ 新克隆不可复现；rdflib 进运行时依赖树；启动解析成本 |
| 只重排对齐映射（不含 TBox） | 极简、无 FIBO 依赖 | 丢层级（家族泛化）、丢 label/definition（英文泛化与解释链）——只剩查表 |
| 全量 FIBO 加载 | 概念覆盖最大 | 196 包 import 闭包复杂度超 MVP；ADR-0007 已定子集引入 |
| LLM 生成别名（进一期） | 覆盖快 | 错误别名 = 自信地答错；违背确定性优先——留作后续飞轮（ADR-0027 模式） |
| validate 节点概念类型校验 | "更严谨" | 与图可达性（`retrieval/graph_store.py`）职责重复，为炫技加复杂度 |
| 闭包概念批量入 registry | 免"未见 IRI"处理 | 29 → 上千条，稀释权威清单的人工审核意义（ADR-0007 纪律） |

## 决策

**总原则**：机制与本体来源解耦（FIBO 是首个实例，非唯一）；**无索引 = 全缝旁路，
行为与今日逐字一致**（沿 ADR-0030 ⑤ 注入哲学：默认不注入时逐字不变）。

```text
FIBO TBox（构建期，rdflib 一次性解析锁定的闭包）
   ↓ make ontology-build（AOT 编译）
概念索引（semantic/ontology/*.json，入库提交，绑定 fibo_commit + 源文件 sha）
   ↓ 注入（agent/concept_index.py；不注入 = 行为逐字不变）
三缝：检索扩展（schema_linker） / 解析兜底（planner） / 解释链（explain）
```

### ① 目标与范围

三缝三目标：**检索缝**（准确性）→ **解析缝**（泛化）→ **解释缝**（可解释性）；
分期落地（见 ⑦），共用同一核心"概念索引"。

范围外登记（不丢失、本期不做）：

- **隐式值过滤**："股票成交额"（无"只看/仅统计"结构词但意图为维度值过滤）——
  属 Planner 值域/过滤语义的独立缺口（ADR-0016 域），概念层不解决；
- **维度槽概念兜底**："按 X 统计"中 X 同义词 miss——二期候选，需先核实今日行为；
- **validate 概念类型校验**——本 ADR 明确不做。

### ② 概念索引内容模型与术语来源

| 内容 | 来源 | 性质 |
|---|---|---|
| 层级 parents/children | FIBO TBox（`rdfs:subClassOf`） | 自动（构建期） |
| 英文术语 | `rdfs:label`（29/29）+ `cmns-av:synonym`（7/29）+ `cmns-av:abbreviation`（0/29）+ `skos:prefLabel`（3/29） | 自动入索引（S1） |
| 中文术语·继承 | 对齐对象的 `ai_context.synonyms` 并集（带 `via` provenance） | 机器派生（S2） |
| 中文术语·别名 | **概念级人工别名**（如 股票→EquityInstrument） | 人工（S3） |
| 英文定义 | `skos:definition` | 自动（S4，解释引用专用，不参与匹配） |
| 反向索引 objects | 自 `fibo_alignment` 重排（metrics/fields/relationships） | 自动 |

- **S3 是唯一"新词汇"来源，也是泛化杠杆的本体**：对象级同义词需逐指标维护，
  概念级别名按概念挂——一次维护、全家族受益。**实测修正（2026-10-08 探针，起草时
  推翻访谈期判断；已固化为 `data/fibo/probe_annotations.py`，产物
  `annotation_stats.json`）**：FIBO 已弃用 `skos:altLabel`（fibo-src 明文改荐
  `cmns-av:synonym` / `cmns-av:abbreviation`；锁定闭包内 altLabel 仅 3 个类仍在
  用）；锁定闭包 1100 个类中 `cmns-av:synonym` 有 114 个类（200 条值）、
  `cmns-av:abbreviation` 47 个类（57 条值），但落在 29 条 registry 概念上仅 9 条
  概念有术语增量（synonym 7 + prefLabel 3，OrganizationalSubUnit 两者皆有）——
  **英文术语有真实存量、S1 必须采集；中文 OOV 仍只能靠 S3**。
- S3 存放：`semantic/synonyms/concepts_<locale>.yml`（locale 词典层，与 `en_us.yml`
  同族）。**不塞进 governance 扩展**——别名是解析器形态层的措辞数据，不是指标口径
  定义，不违反"权威源唯一"（ADR-0015 同一逻辑的延伸）。
- 每条术语带 **provenance**（来源 S1/S2/S3 + via 对象），供 ③ 的差异化激活消费。

### ③ 运行时激活规则（三缝差异化）

- **检索缝（宽松，只管召回）**：术语源 S1+S2+S3（按问句 locale 取语种）；
  层级 = 自身 + 后代（全部）+ 祖先（限 1 层，防泛概念噪声）；match_type 不过滤
  （噪声由 BM25 排序 + 既有重排消化）；**只做术语扩展（追加检索查询词），不做对象
  注入**（注入绕过 BM25 证据、难测难查）；无唯一性要求。
- **解析缝（保守，产出权威 Plan）**：术语源 **S1+S3**（S2 不进——继承同义词在对象层
  已存在，解析无增量、噪声大）；**触发点仅 `kind="unmatched"`**（指标槽无命中），
  既有匹配行为逐字不变；层级 = 自身 + 后代（**不外推祖先**）；**分层唯一性**：在
  "有锚定对象的最高层级"（exact_match → broad_match → related）上恰 1 个才解析，
  ≥2 即 no-op（维持今日行为：候选链/反问）；confidence 需 high（medium 仅允许进
  检索扩展）；命中产完整 Plan + provenance notice（复用 `plan_with_notices` 机制），
  与同义词解析同权进确定性链；**不部分解析、不猜测**。
- **解释缝（只读）**：引用 `{命中词 → 概念 IRI + label + definition → 对象 → 映射
  note}` 完整链；入 explain 输出、run events 与前端运行面板 node_io 通道展示
  （复用既有 NodeInspector，不新造治理 UI）。

### ④ 产物形态与位置

- **AOT 编译**：构建期 rdflib 解析 FIBO 闭包 + 读对齐映射 + 读别名文件 → 静态 JSON；
  运行时纯 JSON 加载、**零 rdflib**——比 ADR-0007 原文"仅解析层使用"更严：仅构建期使用。
- 产物：`semantic/ontology/atlas_finance.concept_index.json` + `README.md`（来源/复现/
  锁版本）；**retail 无映射 → 不生成文件**，天然走降级路径。索引头部绑定
  `fibo_commit` / `commons_release` / `source_semantic_sha`（审计链）。
- 构建：`make ontology-build`（新增；`--check` 模式做漂移比对，忽略 `generated_at`）；
  构建器 `data/fibo/build_concept_index.py`（置闭包工具旁，复用 smoke/check_iris
  的加载逻辑）。
- 加载：新模块 `agent/concept_index.py`（镜像 `agent/value_domain.py` 的位置逻辑）；
  注入式（`chooser=None` 同款）；加载即强校验（镜像 `parse_profile`），失败响亮报错、
  **不静默降级半层**。
- **未见 IRI 规则**：build 遇 registry 外 IRI **响亮失败**并输出待验证清单（先跑
  `check_iris.py` 入 registry，再重建）；层级只物化 registry 概念集合内的归纳子图，
  跨级边打 `transitive: true`，**不物化中间未注册类**。

### ⑤ 治理与维护闭环

- **ADR-0007 末端注记（不改正文）**："FIBO 不进入查询路径"修订为"**本体不在请求
  路径被解析或推理；概念索引以构建期编译产物形态、以确定性查表参与解析辅助与候选
  生成；不参与 SQL 编译、执行与数值计算**"；rdflib 边界由"仅解析层"收紧为"仅构建期"；
  三个未兑现待办标注承接（→ 本 ADR）。
- **ADR-0015 末端注记**：locale 词典族扩展 `concepts_<locale>.yml` 子族（键 = 概念
  短名/IRI，校验自 `iri_registry.json`）。
- **AGENTS.md 三处 contract 变更**（单独 commit，标 `[contract]`）：§3 术语表新增
  "Concept Index（概念索引）"；§4 目录加 `semantic/ontology/`；§7.3 允许目录清单加
  `ontology/`（顺序：本 ADR 先落，再改清单）。
- **`make lint` 新增第 7 节**（概念索引 + 别名文件校验）：
  - 别名文件：概念 IRI ∈ registry；**同 locale 同一术语不得映射多个概念**（歧义 =
    curation bug）；locale 隔离（zh 表术语不并入 en 匹配，沿用既有铁律）；术语非空去重；
  - 索引：结构合法；概念 IRI ∈ registry；对象 ∈ 语义模型（指标/字段/关系存在）；
    与 ossie `fibo_alignment` 双向一致（无孤儿/无漏配）；**`source_semantic_sha` =
    当前 ossie 文件 sha**；**索引内 S3 术语 = 当前别名文件合并结果**——改源不重建 =
    lint 红。
- **两级漂移防线**：repo 文件派生的全部内容 CI 严格可查（上条）；闭包派生部分
  （label/definition/层级）本地 `--check` 全量比对（诚实声明：`fibo-src` 不入库，
  CI 查不了这两类）。
- **curation loop**：`make paraphrase`（dev 集）失败 → `eval/failures/`（ADR-0027 既有
  流程）→ 人工提出别名 → 写 `concepts_*.yml` → `make ontology-build` → lint → 复跑；
  **holdout 一律不碰、只在最终报告**；LLM 提别名 = 后续飞轮，不进本期。
- **运行开关**：env `ATLAS_CONCEPT_LAYER`（默认 on；off = 全缝旁路；命中 `ATLAS_*`
  惯例）+ 注入式零变更；细粒度回退（如只关解析缝）= 代码常量 + ADR 注记的治理动作，
  **不造运行时多开关矩阵**。
- FIBO 版本升级流程写入 `semantic/ontology/README.md`：bump 锁 → `check_iris.py` →
  重建 → 漂移 diff 评审 → 记录新 commit。

### ⑥ 评测与验收

**先冻结基线再动代码（M0 硬门禁）**：`make paraphrase` / `make schema-link` /
`make retrieve` / `make eval` 当前 sha 的 JSON 入库，作为全部对照的基线。

| 缝 | 载体 | 对照 | 产物 | 门禁 |
|---|---|---|---|---|
| 概念一致率（ADR-0007 待办 1） | 新 `eval/concept_eval.py`：以 matcher 纯函数跑 **162 条已标注** `{term→concept}`（86 个样本），比对 IRI | 无层时 = 0 命中，数字为本体（一致率 + 覆盖/歧义/未命中三分） | `eval/reports/concept-<sha>.json`；EVAL_REPORT 独立节（与 EX/Plan Acc 分开） | 阈值 `<待实测>` |
| 检索缝（待办 3） | `make schema-link`（bm25 / fuse） | 扩展开/关 A/B | `schema-link-<engine>-<sha>.json` | Recall@K 不降 + 提升如实报告 |
| 解析缝 | `make paraphrase` + gold `make eval` | 兜底开/关 A/B | drop 率对照 | drop 不升；零回归；新增命中**人工抽检**；holdout 数字 |
| 解释缝（待办 2） | ≥3 条已标注 gold，**plan+compile 级**（不依赖 Doris）断言四要素链 | — | `tests/` 固化 + 运行面板展示 | 链条完整（term→概念 IRI/definition→对象→映射 note） |

- **paraphrase 集扩充**（概念层的主要考卷）：补**刻意 OOV 改写**（用未注册词汇表达
  已注册意图），人工标注、条数不写死（以 `make lint` 输出为准），格式加 `oov` /
  `holdout` 标记；划分 dev/holdout，别名 curation 只碰 dev。
- **降级证明**：retail（无索引）与"删除索引文件"两种情形下，planner/schema_linker
  输出与基线逐字一致（显式测试固化）。
- 全部收益数字 `<待实测>`，由脚本产物回填（AGENTS.md N1）；若对照显示无提升，
  如实报告并触发回退（见推翻条件）。

### ⑦ 里程碑与人工作业

| 里程碑 | 交付 | 关闭 |
|---|---|---|
| **M0 基线与规格**（零功能代码） | 基线冻结；`concept_eval` 预实现版（只输出 86 样本/162 条标注统计）；paraphrase 扩样格式定版；本 ADR + ADR-0007/0015 注记 + AGENTS contract + `docs/design/dev-plan-0032-*.md` | — |
| **M1 概念索引与 matcher** | 构建器 + `make ontology-build`（含 `--check`）；`semantic/ontology/` 产物 + README；`concepts_*.yml` 首批别名；loader；lint 第 7 节；`concept_eval` 真版 | 待办 1 |
| **M2 检索缝** | schema_linker 术语扩展（注入式） | 待办 3 |
| **M3 解析缝**（最高风险，吃 M2 经验） | planner 兜底（unmatched + 分层唯一性 + provenance） | —（泛化最终结论） |
| **M4 解释缝与收口** | 四要素链 + tests + 运行面板；文档全量（README/KL/EVAL_REPORT/ontology README）；全量回归 | 待办 2 |

统一门禁：`make lint && make test` 全绿 + 漂移检查 + 对照实验 + 零回归证明。

**人工投入（不可代理，瓶颈资源）**：OOV 样本标注、holdout 划分、首批 S3 别名、
各里程碑验收评审——Agent 只出候选与校验，不代签（N1/N2）。

## 理由

1. **兑现而非新造**：ADR-0007 的 3 个待办、86 样本的 162 条 `fibo_concepts` 标注、31 条映射、
   评测基建全部已在——本次是把已建好的资产接进运行时，不是从零引入本体工程。
2. **确定性优先的落法**：概念匹配是确定性查表（无推理、无 LLM、无运行时解析）；
   解析缝的"分层唯一性 + 不部分解析"保证行为只做"把反问变成正确解析"的加法——
   "能用编译器就不用 LLM"（AGENTS §0）在泛化问题上的延伸。
3. **泛化杠杆在 S3**：机器派生（S1/S2）只能覆盖本体已有措辞与对齐对象同义词；
   `cmns-av:synonym` 存量有限（29 条 registry 概念中仅 7 条有），**中文新词汇仍无解**；
   概念级人工别名是唯一"新词汇"入口，且按概念挂 = 一次维护全家族受益。
4. **形态选择服从可复现**：`fibo-src` 不入库的既有事实决定运行时加载不可复现；
   AOT + 入库产物 + sha 绑定与 `semantic/values/`（ADR-0016）的成熟模式同构，有先例。
5. **失败可回退**：三缝可独立关（检索/解析/解释），无索引全旁路，误解析有唯一性
   门槛——最坏情况回到今日行为，不产生新风险面。

## 代价与限制

1. **解析缝命中率设计性保守**（可验证事实）：现存 31 条映射的 metric 级
   match_type 多为 broad/related，分层唯一性规则下可唯一解析出指标的概念很少；
   量化与收益由 M1 `concept_eval` 与 M3 对照实测填写，不预设数字。
2. **别名 curation 是持续人工成本**：每个新概念/新场景的 OOV 词汇都要人工维护；
   扩张速度受人工带宽限制（与 ADR-0007"映射维护工作量"同源风险）。
3. **闭包派生部分 CI 不可查**（诚实声明）：`fibo-src` 不入库，label/definition/
   层级的漂移只能本地 `--check`；CI 只保 repo 文件派生部分。
4. **覆盖边界不变**：概念层仅覆盖已对齐概念/对象；`total_trade_tax` 无映射等
   ADR-0007 既有缺口不因本决策解决。
5. **范围外三项**（①登记）：隐式值过滤、维度槽兜底、validate 类型校验——用户侧
   对这些场景的期待需另行立项。
6. **中文泛化增量取决于 S3 质量**：若 M3 实测 holdout drop 率无改善，泛化叙事
   应如实降级（见推翻条件）。

## 什么情况下应该推翻

- **解析缝误解析率超阈值**：新增命中人工抽检错误率 > `<待实测阈值>` → 关闭解析缝
  （保留检索+解释），回退到"OOV 进候选链/反问"的今日行为；
- **Recall@K 对照无提升或下降** → 检索缝回退（`ATLAS_CONCEPT_LAYER=off` 或移除扩展）；
- **curation 挤压主线** → 冻结索引与别名（只保留既有内容，停止新增）；
- **FIBO 许可证/商标条款变化** → 触发 ADR-0007 已定替代评估链；
- **契约级上游变化**（ADR-0002 被推翻 / Ossie 语义层迁出）→ 索引 schema 随
  `semantic/ontology/` 迁移，概念层本体不受影响。

## 验证方式

- [ ] M0：基线产物入库（4 个评测入口当前 sha 的 JSON）；paraphrase 扩样
  （OOV + dev/holdout，人工标注）
- [ ] M1：`make ontology-build && make lint` 全绿；重建确定性（`--check` diff 空）；
  概念一致率实测入 `eval/reports/concept-<sha>.json` 与 EVAL_REPORT 新节
  （与 EX / Plan Acc 分开；ADR-0007 待办 1 关闭）
- [ ] M2：`make schema-link` 对照（扩展开/关）Recall@K 数字与结论
  （ADR-0007 待办 3 关闭）
- [ ] M3：`make paraphrase` 对照（兜底开/关）+ `make eval` gold Plan Acc 零回归；
  新增命中人工抽检记录；holdout 数字 = 泛化最终结论
- [ ] M4：≥3 条已标注 gold 的"术语→概念→字段→SQL"链（plan+compile 级测试固化 +
  运行面板展示；ADR-0007 待办 2 关闭）
- [ ] 降级证明：retail（无索引）与删索引情形下输出与基线逐字一致（测试固化）
- 全部收益数字 `<待实测>`，由脚本产物回填（AGENTS.md N1）。
