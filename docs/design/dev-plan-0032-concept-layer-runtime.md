# ADR-0032 概念层接入运行时开发计划

> **面向执行者**：使用 `superpowers:subagent-driven-development`（推荐，逐卡派发 + 两阶段评审）或 `superpowers:executing-plans`（本会话批量执行 + 检查点）逐任务执行本计划。步骤用复选框（`- [ ]`）追踪。每张工作卡是**可独立验收的切片**：先红测 → 最小实现 → 绿测 → 回归 → 提交。

**目标：** 按 ADR-0032（accepted，2026-10-08）把已建好的 FIBO 概念资产（31 条映射 / 86 样本 162 条标注 / 双脚本 CI）以 AOT 编译产物形态接入运行时的三条缝——检索扩展（M2）、解析兜底（M3）、解释链（M4）——并兑现 ADR-0007 三个未兑现待办。**前后端一体交付（2026-10-14 用户指令扩展）**：解释缝在前端完成概念链体验（问数仪表舱 / 运行观测台），并按主流数据平台布局升级全局导航骨架（M5）。

**架构：** 后端：FIBO TBox 构建期一次性解析（rdflib 只出现在构建期）→ `semantic/ontology/atlas_finance.concept_index.json`（入库、绑 fibo_commit + 源 sha）→ 运行时纯 JSON 加载、确定性查表（零 rdflib、零推理、零 LLM）。**无索引 = 全缝旁路，行为与今日逐字一致**（注入式，`chooser=None` 同款纪律）。前端：延续星图设计系统（ProvenanceSpine / SnapshotSeal / DeterminismBadge，Phase 1 已提交）→ 新增 ConceptChain 组件消费 `concept_chain` 顶层键；全局布局从顶栏横向导航升级为「左侧导航 + 顶部工具栏」（参考 Databricks / Snowsight / Metabase / Superset / Quick BI 共性骨架）。

**技术栈：** Python 3.11；rdflib 7.6.0（仅构建期，`uv run --with` 注入，**不进 pyproject**）；pytest/unittest；前端 React 18 + TypeScript 5.4 + Vite 5 + AntD 5（星图设计系统 Phase 1 已就绪；**零新依赖**）。

**规格：** [ADR-0032](../../infra/adr/0032-concept-layer-runtime.md)；前端视觉与守线规格 [frontend-redesign.md](./frontend-redesign.md)（星图设计系统：配色 / 等宽 / 动效纪律 / 诚实性约束）。执行前通读 ADR-0032 决策 ①–⑦、代价与限制、推翻条件与验证方式；本计划不新增 ADR 级裁定——发现缺口只登记停批条件，回改 ADR（M5 布局升级为 2026-10-14 用户显式指令，属前端设计范畴，不触 ADR-0032 边界）。另阅 ADR-0007（L2 对齐层）、ADR-0015（locale 词典族）、ADR-0016（值域快照——本计划的同构先例）、ADR-0018（前端工程边界）、ADR-0019 决策 ④（不写死 sha 字面量纪律）、ADR-0022 判据 6（explanation 键集冻结）、ADR-0025 决策 ②（新输出挂顶层并列键的先例）。

---

## 0. 起点实测（2026-10-14 撰写时逐条复测）

| 事实 | 实测证据 | 复测命令 |
|---|---|---|
| ADR-0032 accepted，**实现未启动**；M0–M4 全部待做 | `infra/adr/0032-concept-layer-runtime.md` 状态行 | `grep -n "状态：" infra/adr/0032-concept-layer-runtime.md` |
| FIBO 资产停在展示层：运行时消费点 0 | 唯一消费方为治理 API（`serving/governance.py` `_metrics_index`）；planner/schema_linker 零 fibo 引用 | `grep -rn "fibo" agent/planner.py agent/tools/schema_linker.py` → 0 命中 |
| gold 标注 86 样本 / 162 条 `{term→concept}` 已就位，无脚本消费 | `eval/gold/schema.json` 已有 `fibo_concepts`；86 个样本文件已标注 | `grep -l '"fibo_concepts"' eval/gold/*/gold-*.json \| wc -l` → 86 ；`grep -o '"term"' eval/gold/*/gold-*.json \| wc -l` → 162 |
| paraphrase 集仅 13 条（以注册同义词内改写为主，测不出新词汇泛化） | `eval/gold/paraphrase/pp-*.json` 13 个 | `ls eval/gold/paraphrase/pp-*.json \| wc -l` |
| 四个评测入口**无当前 sha 基线产物** | `eval/reports/` 无 paraphrase-robustness*.json、无当前 HEAD 主评测/检索/schema-link 产物 | `ls eval/reports/ \| grep -i "paraphrase"` → 空 |
| planner 的 unmatched 出口单一且干净 | `plan_with_notices`：`metric_hits` 空 → `ClarificationRequest(kind="unmatched")` | `grep -n 'kind="unmatched"' agent/planner.py` |
| 解析缝需要新注入位：build_graph 内 Planner 硬编码 | `agent/graph.py`：`planner = Planner(model)`（linker 可注入，planner 不可） | `grep -n "planner = Planner(model)" agent/graph.py` |
| explanation 键集被契约冻结（13 键） | `tests/test_api_contract_v2.py` `EXPLANATION_KEYS`；chart 走顶层并列键的先例（ADR-0025 决策 ②） | `grep -n "EXPLANATION_KEYS" tests/test_api_contract_v2.py` |
| 索引文件目录不存在；`iri_registry.json`（29 条权威 IRI）与 31 条映射可作构建输入 | `data/fibo/iri_registry.json`；`data/fibo/validate_alignments.py` 已能提取 mappings | `ls semantic/ontology 2>/dev/null` → 不存在 |
| M1 依赖的本地闭包资产存在性**未验证**（fibo-src 为 gitignore） | `data/fibo/README.md`：需本地 sparse checkout；构建时缺失即响亮失败 | `ls data/fibo/fibo-src/FND/AllFND.rdf 2>/dev/null`（开工时现场确认） |
| 当前工作面：`main` = 8fae117；实际检出在 `feat/frontend-redesign-phase1`（前端重设计，未合并） | `git log --oneline -1 main`；`git branch --show-current` | `git rev-parse --short main` |
| 前端 Phase 1（星图设计系统）已提交未合并；运行观测台（Phase 2a）进行中 | 分支 HEAD `94793c0`（`feat(runs): redesign run catalog as observatory cards`）；`docs/design/frontend-redesign.md` 实施状态节 | `git log --oneline -1 feat/frontend-redesign-phase1` |
| 全局布局仍为顶栏横向 Menu（4 项）；前端对概念链零实现 | `frontend/src/App.tsx` `NAV_ITEMS` + `Layout.Header` 横向 `Menu`；`grep -rln "concept_chain" frontend/src` → 0 命中 | `grep -n "NAV_ITEMS" frontend/src/App.tsx` |

**起点结论**：本计划是把已有资产（映射/标注/CI 脚本/注入式基建）**接进运行时**，不是从零引入本体工程。M0 零功能代码，先冻基线；收益数字全部 `<待实测>`。

---

## 1. 全局约束与执行纪律

- **分支与工作区**：从 `main` 新建 `feat/adr0032-concept-layer`（或经 `superpowers:using-git-worktrees` 隔离）；**不得**携带 `feat/frontend-redesign-phase1` 的未提交变更；撰写时 main = `8fae117`，开工时以现场 `git rev-parse --short main` 为准，不写死。M5 开工前置见 H5（Phase 1 先合入 main 并 rebase 本分支——M5 依赖其设计系统组件）。
- **本计划不新增裁定**：批次序与实现细节引用 ADR-0032 编号（决策 ①–⑦ / 判据见「验证方式」）；发现缺口 → 停该卡回改 ADR，不得"先开工后补"。
- **零功能代码期（M0）**：M0 只产评测/文档/契约变更；M1 起才动运行时。
- **rdflib 红线**：只出现在构建期（`uv run --with rdflib==7.6.0`）；**不进 pyproject、不进运行时**。运行时为纯 JSON 加载。
- **旁路不变量**：无索引 / `ATLAS_CONCEPT_LAYER=off` / 未注入三种情形，planner 与 schema_linker 输出与基线**逐字一致**（测试固化，非声称）。
- **不改**：`semantic/ossie/`（本计划零 fibo_alignment 变更）、`airflow/dags/generated/`、`docs/outreach-series/`；不执行 `make seed`；不删 Known Limitations。
- **前端纪律（M5）**：一切视觉改动服从 `docs/design/frontend-redesign.md` 的诚实性约束与测试守线（`honesty.test.ts` / `order.test.ts` / `no-persist.test.ts` / `governance-readonly.test.ts` / `sha.test.ts` / `app.test.tsx` 全绿）；**不动 `lib/order.ts` 的 `ANSWER_SECTIONS` 渲染序**；**零新前端依赖**（AntD/Vite 既有栈内完成）；科技感 = 仪器感（发光活跃指示、150–300ms 微动效、等宽读数），禁 bounce/spring，`prefers-reduced-motion` 全局关闭；每张前端卡以 `make ui-check`（tsc + vitest + build）全绿收口；M5 开工前置 H5。
- **N1/N2 纪律**：一切收益数字（一致率、Recall@K、drop 率）出自脚本产物，报告里一律先写 `<待实测>`；本文档与任何文档不得把 M1–M5 写成"已实现/已上线"。
- **人工作业不可代理**（ADR-0032 ⑦）：OOV 样本标注、holdout 划分、首批 S3 别名、里程碑验收评审、新增命中抽检——Agent 只出候选与校验，不代签。
- **样本数不写死**：gold / paraphrase 的条数随人工标注演进，一律以 `make lint` 与评测脚本当日输出为准。
- **状态与证据**：状态只用 `NOT_STARTED / IN_PROGRESS / BLOCKED / DONE / CANCELLED`；每卡收口在 §7 证据账本追加一行（任务 ID、sha、命令、产物路径、关键数字、阻塞与下一动作）。文档完成 / fake 测试不等于真链完成。
- **环境前置**：`make eval` 需 Doris（`make up`）+ 已锁快照；`make schema-link` / `make paraphrase` / `make retrieve` 默认 bm25 不需 Doris；`make ontology-build` 需本地 `data/fibo/fibo-src`。缺前置即该卡 BLOCKED（如实登记），**不得**用编造数字或跳过判据的方式"做绿"。

### 1.1 可执行命令

已核对根 Makefile，以下为**现有入口**（实际执行结果落在 §7，不以入口存在代替验收）：

```bash
make lint                 # 语义层全部校验（当前 6 节；M1-T4 增第 7 节）
make test                 # unittest discover 全量
make eval                 # gold 主评测（需 Doris + 锁定快照；产出 eval/reports/<sha>.json）
make paraphrase           # 改写鲁棒性（planner-only；产出 eval/reports/paraphrase-robustness.json）
make retrieve             # 检索评测（默认 bm25；产出 retrieval-bm25-<sha>.json）
make schema-link          # schema linking（默认 bm25；产出 schema-link-bm25-<sha>.json）
make ui-check             # 前端门槛（tsc + vitest + build）
make report               # 生成 EVAL_REPORT.md
```

`make ontology-build`、`make concept-eval`、`schema-link TAG=...` 是**本计划新增入口**——对应任务完成前不可调用；不在计划外编造新 make target。

---

## 2. 模块与接口责任图

除注明"改"者均为新建；"改"文件只做最小注入/通道改动，不重写。

| 路径 | 责任与导出合同 | 落卡 |
|---|---|---|
| `data/fibo/build_concept_index.py` | AOT 构建器。纯函数层 `assemble_index(registry, mapped, hierarchy, aliases) -> dict`（无 rdflib 依赖，可测）+ 薄提取层（延迟 import `check_iris`，仅构建期）；`main(argv)` 支持 `--check` 漂移比对（忽略 `generated_at`）；未见 registry 外 IRI **响亮失败**并输出待验证清单 | M1-T1 |
| `data/fibo/__init__.py` | 空包声明（使 tests 可 `from data.fibo.build_concept_index import ...`） | M1-T1 |
| `semantic/ontology/atlas_finance.concept_index.json` | AOT 产物（入库）。头部绑 `fibo_commit` / `commons_release` / `source_semantic_sha`；`concepts.<短名>` = {iri, label, definition, terms[S1/S2/S3+provenance], parents/children(registry 归纳子图，跨级边 `transitive:true`), objects[反向索引]}；全量排序保证重建确定性 | M1-T1 |
| `semantic/ontology/README.md` | 来源/复现/FIBO 版本升级流程（bump 锁 → check_iris → 重建 → 漂移 diff 评审 → 记录新 commit）；诚实边界（闭包派生部分 CI 不可查） | M1-T1 |
| `semantic/synonyms/concepts_zh_cn.yml` | S3 概念级别名（locale 词典族，ADR-0015 延伸）：`concepts: {<registry 短名>: [术语]}`；同 locale 同术语不得映射多概念 | M1-T2 |
| `agent/concept_index.py` | 运行时加载与匹配（镜像 `agent/value_domain.py`）：`ConceptIndex` dataclass；`parse_concept_index(path)` 强校验（畸形 → ValueError）；`load_concept_index(model_name)` 读 `ATLAS_CONCEPT_LAYER`（默认 on；off/0/false → None）与文件存在性（缺 → None）；`match_terms(text, locale)` / `expand(...)` / `concept_terms(...)` 纯函数；`detect_locale(text)`（与 planner 同规则，测试锁一致） | M1-T3 |
| `eval/concept_eval.py` | 概念一致率评测。M0 预实现版：只输出 86 样本/162 条标注统计；M1 真版：matcher 跑 162 条 `{term→concept}` 比对 IRI，产 `eval/reports/concept-<sha>.json`（一致率 + 覆盖/歧义/未命中三分） | M0-T2 / M1-T5 |
| `semantic/lint.py`（改） | 目录清单加 `ontology/`（与 AGENTS §7.3 同源）；新增第 7 节 `check_concept_layer()`：别名文件 + 索引结构 + 与 ossie 双向一致 + `source_semantic_sha` 比对 + S3 术语 = 别名合并结果 | M0-T5 / M1-T4 |
| `agent/tools/schema_linker.py`（改） | 构造参数 `concept_index: ConceptIndex \| None = None`（`chooser` 同款）；`link()` 检索缝：只做术语扩展（S1+S2+S3；自身+后代+祖先 1 层）追加查询词，**不注入对象**；None → 逐字不变 | M2-T1 |
| `agent/planner.py`（改） | 新增 `ConceptNotice`（frozen dataclass：term/concept/concept_iri/label/definition/object/match_type/confidence/note/source）；构造参数 `concept_index=None`；解析缝：**仅 `kind="unmatched"` 触发**，术语源 **S1+S3**，层级自身+后代（不外推祖先），分层唯一性（exact→broad→related 最高层恰 1 个）+ confidence=high 才解析；命中产完整 Plan + notice（并入 `plan_with_notices` 返回元组） | M3-T2 |
| `agent/graph.py`（改） | `build_graph` 增 `planner` 注入位（默认 None → 今日行为）；`DataAgent.__init__` 透传；plan 节点改用 `plan_with_notices` 并抽 `ConceptNotice` → state `concept_chain`（**纯 dict**，serde 零改动）；explain 节点原样透出 | M3-T3 / M4-T1 |
| `agent/state.py`（改） | `TurnState` 增 `concept_chain` 通道（plan 冲刷）；`TurnResult` 增顶层字段 `concept_chain: tuple[dict,...] \| None = None`（与 `chart` 并列先例） | M3-T3 / M4-T1 |
| `agent/factory.py` / `agent/cli.py` / `serving/api.py`（改） | 运行时装配点显式加载并注入（env 尊重）：`create_live_agent`（ask/HTTP/分析共用）、cli `plan`/`query`、api `/plan`；`/ask` payload 增顶层 `concept_chain` 键 | M3-T3 / M4-T1 |
| `agent/node_io.py`（改） | `NODE_IO_SPEC`：`plan.output`、`explain.output` 增 `concept_chain`（运行面板通道） | M4-T1 |
| `eval/runner.py` / `eval/schema_link_eval.py` / `eval/paraphrase_eval.py`（改） | 评测侧同源注入（env 尊重）；`--tag` 产物后缀（A/B 不互相覆盖）；paraphrase 支持 `oov`/`holdout` 标记与分节报告 | M2-T2 / M3-T1 / M3-T3 |
| `eval/report.py`（改） | EVAL_REPORT 新增「概念层一致率」独立节（与 EX/Plan Acc 分开），读 `concept-<sha>.json` | M1-T5 |
| `frontend/src/App.tsx` / `frontend/src/styles/global.css`（改） | 全局布局骨架：顶栏横向 Menu → 左侧导航（4 面板）+ 顶部工具栏；`.atlas-nav-item--active` 活跃指示（仪器感）；路由表零改动 | M5-T1 |
| `frontend/src/components/ConceptChain.tsx`（新建）+ `panels/workbench/ExplanationBlock.tsx` / `AskWorkbench.tsx` / `api/types.ts`（改）+ vitest | 概念链四要素展示（term→概念 IRI/label/definition→object→note）；挂进解释块，**不动 `ANSWER_SECTIONS` 渲染序**；chain 为 null → 零渲染（与今日逐字一致）；工作台历史区底部区块右栏化（宽屏≥1200px，窄屏回落底部） | M5-T2 |
| `frontend/src/panels/runs/node-io-view.ts` + `NodeInspector.tsx`（改）+ vitest | 运行面板概念链 formatter 与四要素详情（复用既有 NodeInspector，不新造治理 UI；ADR-0032 ③） | M5-T3 |
| `AGENTS.md` / `infra/adr/0007…` / `infra/adr/0015…`（改） | contract 三处（§3/§4/§7.3）+ 末端注记（不改正文） | M0-T4 / M0-T5 |

---

## 3. 顺序与门禁

### 3.1 唯一合法序

```
M0（基线与规格，零功能代码）
 T4 ADR 注记 ─▶ T5 AGENTS contract ─▶ T3 paraphrase 格式定版 ─▶ T1 基线冻结 ─▶ T2 concept_eval 预实现版
   │
   ▼
M1（概念索引与 matcher）T1 构建器 ─▶ T2 首批别名（人工）─▶ T3 loader/matcher ─▶ T4 lint 第 7 节 ─▶ T5 concept_eval 真版
   │
   ▼
M2（检索缝）T1 术语扩展 ─▶ T2 A/B 对照 ─▶ T3 降级证明
   │
   ▼
M3（解析缝，最高风险）T1 paraphrase 扩样（人工门禁）─▶ T2 Planner 兜底 ─▶ T3 通道与装配 ─▶ T4 A/B + 抽检 + 结论
   │
   ▼
M4（解释缝，后端通道）T1 explain 通道 ─▶ T2 四要素链测试
   │
   ▼
M5（前端，前后端一体）T1 布局骨架 ─▶ T2 问数舱概念链 ─▶ T3 运行观测台概念链 ─▶ T4 前端验证 + 文档收口
```

### 3.2 每条边的依据（换序的具体后果）

| 边 | 依据 | 若换序的后果 |
|---|---|---|
| M0 一切在前 | ADR-0032 ⑥（先冻结基线再动代码，M0 硬门禁） | 基线被新行为污染，"零回归/逐字一致"失去对照物 |
| M0-T4/T5 先于 M0-T1 | ADR ⑤（注记与 contract 属规格；冻结 sha 应含规格提交） | 冻结 sha 与规格文档分离，审计链断裂 |
| M1-T1 先于 T3 | loader 的强校验契约就是构建器产物 schema | loader 对着不存在的形态写死，接口二次返工 |
| M1-T2 先于 T3 | S2 术语派生与 S3 别名合并都进 loader 的术语表 | loader 先落无法覆盖 S3 词汇；concept_eval 无新词汇可测 |
| M2 先于 M3 | ADR ⑦（M3 最高风险，吃 M2 经验） | 解析缝（产出权威 Plan，错误下游兜不住）先于低风险缝上线 = 风险顺序倒置 |
| M3-T1 先于 T4 | ADR ⑥（paraphrase 是概念层主要考卷；holdout 先划分后 curation） | 样本后补 → holdout 与 curation 互相污染，泛化结论不可信 |
| M4 先于 M5 | ADR ⑦（解释缝依赖 M3 的 notice 通道） | 解释链无素材（chain 的来源是命中 notice） |
| M4-T1 先于 M5-T2/T3 | ADR ③（解释缝通道：/ask 顶层键 + node_io 通道） | 前端无数据可消费，表单先行 = 假 UI |
| M5-T1 先于 M5-T2/T3 | 布局骨架决定各面板挂接位（避免二次重排） | 面板先挂、骨架后改 = 全部返工 |
| M5-T4 最后 | 前端是解释缝的最终展示面；收口覆盖全量交付 | 收口文档遗漏前端证据（README/KL 三态不全） |

### 3.3 人工作业门禁（裁定前不开工对应卡）

| # | 门禁 | 阻塞 | 解除方式 |
|---|---|---|---|
| **H1** | 首批 S3 别名需人工 curation（Agent 只出候选） | M1-T2 的"首批"、M1-T5 真版才有新词汇可测 | 人工从 dev 侧证据（failures/paraphrase dev 集）确认词→概念后落盘 |
| **H2** | OOV dev/holdout 样本需人工标注与划分 | M3-T4 的 A/B 与泛化结论 | 人工确认 Agent 起草的候选样本（含 expected_*）与划分；**holdout 不参与 curation** |
| **H3** | M2/M3 新增命中需人工抽检 | M3-T4 收口（回退判据） | 人工逐条判定"解析正确/误解析"并记录错误率 |
| **H4** | 各里程碑验收评审 | 该里程碑 DONE | 人工评审证据账本与产物 |
| **H5** | frontend-redesign Phase 1（星图设计系统）合并到 `main`（含分支收尾：未提交的 docs 删除与计划文件的裁定） | M5 全部（ProvenanceSpine / SnapshotSeal / 设计令牌是 M5 既有依赖） | 人工完成 Phase 1 分支收尾与合并；M5 开工前 rebase 本分支 |

---

## 4. 工作卡

卡内五段：**依据 / 开工前置 / 可改文件 / 步骤 / 收口判据·提交拆分·停批条件**。规模标注（S/M/L）为主观量级，不得当工期承诺（N1）。

### 4.1 M0：基线与规格（零功能代码）

#### M0-T1 基线冻结（四个评测入口）

- **依据**：ADR-0032 ⑥（M0 硬门禁）、⑦
- **开工前置**：M0-T4/T5/T3 收口；`make up` 且 Doris 可达；`data/snapshots/` 有可绑定快照（`make eval` 走 ADR-0019 三级解析）；`git status` 无影响评测输入的未提交变更
- **可改文件**：`eval/reports/`（新增 4 个 JSON）；§7 证据账本
- **步骤**：

- [x] 记录冻结 sha：`git rev-parse --short HEAD`（分支 `feat/adr0032-concept-layer` 上的本次提交）；写入证据账本
- [x] `make paraphrase` → 复制为 `eval/reports/paraphrase-robustness-<sha>.json`（脚本产物为固定名，冻结副本带 sha；脚本本身 M0 不改）
- [x] `make retrieve` → 产出 `eval/reports/retrieval-bm25-<sha>.json`（默认 bm25）
- [x] `make schema-link` → 产出 `eval/reports/schema-link-bm25-<sha>.json`
- [x] `make eval` → 产出 `eval/reports/<sha>.json`（依赖锁定快照；报告的 `snapshot_sha` 记入账本）
- [x] 逐份核对产物非空且域分节正确（finance/retail 不混报）；账本记录关键数字（drop_rate / Recall@K / Plan Acc / EX——只记录，不评价）
- [x] 提交：`eval(baseline): ADR-0032 M0 基线冻结——四入口 <sha> 产物入库`

- **收口判据**：4 份产物在库且与账本一致；此后的全部对照实验引用它们
- **停批条件**：任一入口因环境缺失跑不成 → 该入口记 BLOCKED 并停后续对照类收口（不得以旧 sha 产物冒充）

#### M0-T2 concept_eval 预实现版（只输出标注统计）

- **依据**：ADR-0032 ⑦ M0 行；§11 先写评测
- **开工前置**：M0-T1 收口
- **可改文件**：`eval/concept_eval.py`（新建）、`tests/test_concept_eval.py`（新建）、`Makefile`（新增 `concept-eval`）
- **步骤**：

- [x] 写失败测试（纯函数先行）：`tests/test_concept_eval.py`——tmp 目录构造 2 个含 `fibo_concepts` 的合成 gold 样本 + 1 个不含的，断言统计函数输出 `samples_annotated=2`、`terms_total=<N>`、`terms_by_role` 计数正确、`registry_coverage` 能标出不在 `data/fibo/iri_registry.json` 的 IRI
- [x] 跑测试确认红（模块不存在）
- [x] 实现 `eval/concept_eval.py`：扫描 `eval/gold/*/gold-*.json`（与 lint 同 glob），统计并写 `eval/reports/concept-<sha>.json`，字段：
  ```json
  {"eval": "concept-alignment", "stage": "stats-only", "created_at": "...", "sha": "<git short sha>",
   "stats": {"samples_total": 0, "samples_annotated": 0, "terms_total": 0,
             "terms_by_role": {}, "concepts_distinct": 0,
             "registry_coverage": {"in_registry": 0, "not_in_registry": 0, "missing": []}},
   "notes": "matcher 未实现（M1-T5）；本产物仅标注统计"}
  ```
- [x] Makefile：新增 `concept-eval` 目标（`$(PYTHON) -m eval.concept_eval`；M0 阶段无 A/B 需求，不加 `--tag`）+ `help` 节一行 + 顶部 `.PHONY` 追加
- [x] 跑 `make concept-eval` 实测：核对与 §0 复测口径一致（撰写时 86/162；以当日输出为准）；账本记录实测数与 sha
- [x] 跑测试确认绿；`make test` 回归
- [x] 提交：`eval(concept): ADR-0032 M0 concept_eval 预实现版（仅标注统计，无 matcher）`

- **收口判据**：`make concept-eval` 产出 `concept-<sha>.json` 且无任何"一致率"字段（matcher 不存在，不得预填）
- **停批条件**：若 gold 标注出现 `not_in_registry` 非零 → 停，登记清单回 M0（属 ADR-0007 已定校验面，不自行修 IRI）

#### M0-T3 paraphrase 扩样格式定版（零代码）

- **依据**：ADR-0032 ⑥（OOV + dev/holdout 标记）、⑦
- **开工前置**：无
- **可改文件**：`eval/gold/paraphrase/README.md`（新建）
- **步骤**：

- [x] 撰写格式文档（定版）：字段 `id / base / intended_metric / question / expected_metric / expected_dimensions / expected_time / note` 不变，**新增可选 `oov: bool`（表达意图的词未注册）与 `holdout: bool`（参与最终报告、不参与 curation）**，缺省 false（兼容既有 13 条）；样例给出一条 OOV 样本的完整 JSON
- [x] 写明纪律：curation（S3 别名）只碰 dev（`holdout != true`）；holdout 划分人工裁定；最终报告 dev/holdout 分节，不合并汇报
- [x] 提交：`docs(eval): paraphrase 扩样格式定版（oov/holdout 标记，ADR-0032 M0）`

- **收口判据**：文档含完整字段表 + 一条合法样例 + curation 纪律三条；与 M3-T1 实现的解析行为一致（由该卡测试对照）

#### M0-T4 ADR-0007 / ADR-0015 末端注记

- **依据**：ADR-0032 ⑤（不改正文，末端注记）
- **开工前置**：无
- **可改文件**：`infra/adr/0007-fibo-semantic-alignment.md`、`infra/adr/0015-scenario-decoupling-locale.md`（**只追加文件末尾**）
- **步骤**：

- [x] ADR-0007 末尾追加「末端注记（落注记当日，ADR-0032）」：正文"FIBO 不进入查询路径"修订为——**本体不在请求路径被解析或推理；概念索引以构建期编译产物形态、以确定性查表参与解析辅助与候选生成；不参与 SQL 编译、执行与数值计算**；rdflib 边界收紧为仅构建期；验证方式中三个未兑现项标注"由 ADR-0032 承接，关闭判据见其 M1–M5"
- [x] ADR-0015 末尾追加：「末端注记（落注记当日，ADR-0032）」——locale 词典族扩展 `concepts_<locale>.yml` 子族（概念级人工别名；键=概念短名，校验自 `data/fibo/iri_registry.json`；locale 隔离铁律不变）
- [x] 自查：正文（含"FIBO 不进入查询路径"原句）一字未改
- [x] 本计划文件随本卡入库（ADR-0032 ⑦ M0 交付物清单含 `docs/design/dev-plan-0032-*.md`）：`git add docs/design/dev-plan-0032-concept-layer-runtime.md`
- [x] 提交：`docs(adr): ADR-0007/0015 末端注记与 ADR-0032 开发计划入库（不改正文）`

- **收口判据**：两条注记在库；`git diff` 显示仅文件末尾追加
- **停批条件**：无

#### M0-T5 AGENTS.md contract 三处 + lint 目录清单同步

- **依据**：ADR-0032 ⑤（§3 术语 / §4 目录 / §7.3 清单；「本 ADR 先落，再改清单」——ADR-0032 已入库 ✓）；AGENTS §14（`[contract]`）
- **开工前置**：M0-T4 收口
- **可改文件**：`AGENTS.md`、`semantic/lint.py`（仅 `_AUTHORITATIVE_DIRS` 一行 + 注释）
- **步骤**：

- [x] AGENTS §3 术语表新增行：`| **Concept Index**（概念索引） | 构建期 AOT 编译的静态概念索引（semantic/ontology/*.json）：FIBO 层级 / 术语 / 英文定义 / 反向索引；运行时只做确定性查表，不解析、不推理、不参与 SQL | "本体缓存"、"FIBO 库" |`
- [x] AGENTS §4 目录树 `semantic/` 节新增：`│   └── ontology/        # 概念索引 AOT 产物（ADR-0032）：构建期编译、入库提交，运行时确定性查表`
- [x] AGENTS §7.3 允许目录清单：`ossie/`、`synonyms/`、`policies/`、`ontology/`、`_*` 归档区
- [x] `semantic/lint.py`：`_AUTHORITATIVE_DIRS` 增 `"ontology"`（注释引用 ADR-0032：产物目录，清单与 AGENTS §7.3 同源维护）
- [x] `make lint && make test` 全绿（目录清单变化不得影响既有校验输出——`check_semantic_authority` 对现存目录的报错集不变；如有既有测试锁定该 frozenset，一并更新并在提交说明登记）
- [x] 提交：`contract(agents): 新增概念索引术语/目录/清单登记（ADR-0032 ⑤）`（commit message 标 `[contract]`）

- **收口判据**：AGENTS 三处 + lint 清单一致；`make lint` 输出与改前对既有目录逐字一致（ontology/ 不存在时）
- **停批条件**：若发现需改动 `check_semantic_authority` 的判定逻辑（非清单数据）→ 停，属越出 ADR ⑤ 授权范围，回改 ADR

### 4.2 M1：概念索引与 matcher（关闭 ADR-0007 待办 1）

#### M1-T1 构建器 + `make ontology-build` + 产物 + README

- **依据**：ADR-0032 ② ④（内容模型 / AOT / 未见 IRI 响亮失败 / 归纳子图）
- **开工前置**：M0 收口；本地 `data/fibo/fibo-src` 与 `vendor` 就位（`uv run --with rdflib==7.6.0 python data/fibo/smoke_fibo.py` 冒烟通过）
- **可改文件**：`data/fibo/build_concept_index.py`（新建）、`data/fibo/__init__.py`（新建）、`Makefile`（`ontology-build`）、`semantic/ontology/atlas_finance.concept_index.json`（产物）、`semantic/ontology/README.md`（新建）、`tests/test_concept_build.py`（新建）
- **接口**：
  - 构建器纯函数层（无 rdflib，测试对象）：`assemble_index(registry: dict[str, str], mapped: list[MappingEntry], hierarchy: dict[str, HierarchyFacts], aliases: dict[str, list[str]]) -> dict`；`MappingEntry = {object: str, concept_iri: str, match_type: str, confidence: str, note: str}`（object 键形如 `metric:total_trade_value` / `dim_account` / `dim_account->dim_customer` / `dim_broker.Branch`）；`HierarchyFacts = {parents: list[tuple[str, bool]], children: list[tuple[str, bool]]}`（短名 + `transitive` 布尔；仅 registry 内归纳子图）
  - 产物 schema（v1）：
    ```json
    {"schema_version": 1, "generated_at": "<ISO8601+08:00>",
     "fibo_commit": "119fa8c091aa4beece7d22aefa6fe138021a4355", "commons_release": "20250801",
     "source_semantic_sha": "<atlas_finance.ossie.yaml sha256>",
     "concepts": {"<registry 短名>": {"iri": "...", "label": "...", "definition": "|null",
       "terms": [{"text": "...", "lang": "en|zh", "source": "S1|S2|S3", "via": "<对象键|null>"}],
       "parents": [{"concept": "<短名>", "transitive": false}],
       "children": [{"concept": "<短名>", "transitive": false}],
       "objects": [{"object": "metric:total_trade_value|dim_account|dim_account->dim_customer|dim_broker.Branch",
                    "match_type": "exact_match|broad_match|related", "confidence": "high|medium", "note": "..."}]}}}
    ```
- **步骤**：

- [x] 写失败测试 `tests/test_concept_build.py`（纯函数，合成输入）：
  - [x] 层级归纳：合成 A→B→C（B 未注册）→ registry {A,C} ⇒ C.parents=[{A, transitive:true}]，B 不出现
  - [x] 术语三源合并与 provenance：S1 取 label/synonym/prefLabel/abbreviation；S2 取对齐对象 synonyms（`via`=对象键）；S3 取别名文件
  - [x] 反向索引重排：mapping `metric:X → C`（broad_match, high）进 `concepts.C.objects`
  - [x] registry 外 IRI → `assemble_index` 抛 `ValueError`（含 IRI 清单）
  - [x] 确定性：同一输入两次组装 `json.dumps(..., sort_keys)` 逐字节相等
- [x] 跑测试确认红（模块不存在）
- [x] 实现构建器：提取层（延迟 import `check_iris.load_graph`，复用其 SEEDS 闭包；S1 术语自 `rdfs:label` / `cmns-av:synonym` / `cmns-av:abbreviation` / `skos:prefLabel`，定义自 `skos:definition`）→ 纯函数层组装 → 写产物（`ensure_ascii=False`、`sort_keys=True`、结尾换行）
- [x] 实现 `--check`：重建到内存并与入库产物比对（剔除 `generated_at`），差异打印字段路径 + exit 1
- [x] Makefile：`ontology-build: uv run --with rdflib==7.6.0 python data/fibo/build_concept_index.py $(if $(CHECK),--check)` + `help` + `.PHONY`
- [x] 实测生成产物：`make ontology-build`；核对 31 条映射全部落位（`jq '[.concepts[].objects[]] | length'`）；`make ontology-build CHECK=1` 复跑 diff 为空（重建确定性）
- [x] 撰写 `semantic/ontology/README.md`：来源（fibo_commit / commons_release / source_semantic_sha 三元绑定）、复现命令、FIBO 升级流程（bump 锁 → `check_iris.py` → 重建 → `--check` 漂移 diff 评审 → 记录新 commit）、如实声明"闭包派生部分 CI 不可查、本地 `--check` 可查"
- [x] `make test` 回归 + 提交流：`feat(ontology): 概念索引 AOT 构建器与产物（ADR-0032 M1-T1）`；产物与 README 同一提交（审计链）

- **收口判据**：`make ontology-build CHECK=1` 零 diff；产物 31 条映射全落位；registry 外 IRI 响亮失败有测试；README 三元绑定与升级流程齐备
- **停批条件**：闭包加载发现 registry 内 IRI 缺失（与 `check_iris.py` 实测矛盾）→ 停，回数据层排查（禁改 registry 掩盖）

#### M1-T2 `concepts_zh_cn.yml` 骨架与首批别名（含 H1 人工门禁）

- **依据**：ADR-0032 ②（S3 唯一新词汇入口）、⑤（别名校验）
- **开工前置**：M0 收口；H1 人工 curation（Agent 可先出候选清单：从 `eval/failures/**` 与 paraphrase dev 集提取"反复未命中的措辞"→建议概念，人工逐条确认）
- **可改文件**：`semantic/synonyms/concepts_zh_cn.yml`（新建）
- **步骤**：

- [x] 落地骨架（Agent 起草，结构定死）：
  ```yaml
  # 概念级人工别名（locale=zh_cn；ADR-0032 ② S3）：唯一的"新词汇"来源
  # owner: agent（解析器形态层；非业务口径定义——不进 governance 扩展）
  # 校验：semantic/lint.py 第 7 节（键=iri_registry.json 概念短名；术语非空去重；
  #       同 locale 同一术语不得映射多个概念）
  # 纪律：新增术语 → make ontology-build 重建 → make lint；curation 只碰 dev 集
  concepts: {}
  ```
- [x] 人工确认首批术语（≥ 教学样例：`股票 / 个股 → fibo-fbc-fi-fi:EquityInstrument` 等；规模由人工带宽决定，条数不写死），逐条写入并在提交说明记录来源（哪条失败样本/哪条 dev 改写支持该别名）
- [x] 提交：`semantic(synonyms): 新增概念级别名 concepts_zh_cn.yml（首批人工 curation，ADR-0032 S3）`——提交说明按 AGENTS §8 记录影响面（影响 planner 解析兜底与检索扩展的词匹配，不改变任何指标口径）

- **收口判据**：文件在库且每条术语可溯源（提交说明）；结构通过（lint 第 7 节在 M1-T4 落，此卡先由 M1-T3 loader 的强校验代跑）
- **停批条件**：人工给不出可溯源的术语 → 允许首批为空（空表合法），但**不得**用 LLM 生成别名凑数（ADR 已裁：LLM 提别名 = 后续飞轮，不进本期）

#### M1-T3 `agent/concept_index.py`（loader + matcher + 激活开关）

- **依据**：ADR-0032 ③（三缝激活规则的数据面）、④（加载即强校验，镜像 `parse_profile`；不静默降级半层）、⑤（`ATLAS_CONCEPT_LAYER`）
- **开工前置**：M1-T1 产物在库；M1-T2 文件在库
- **可改文件**：`agent/concept_index.py`（新建）、`tests/test_concept_index.py`（新建）
- **接口**：
  - `load_concept_index(model_name: str = "atlas_finance", ontology_dir: Path = ONTOLOGY_DIR) -> ConceptIndex | None`
  - `parse_concept_index(path: Path) -> ConceptIndex`（畸形/缺键/类型错 → ValueError，消息含路径）
  - `match_terms(index, text, locale) -> tuple[ConceptHit, ...]`；`ConceptHit = {term, concept, concept_iri, source, via}`（frozen dataclass；`source ∈ {S1,S2,S3}`，`via` 为 S2 的对象 provenance，其余 null）（en：大小写不敏感；zh：子串；最长命中优先，同长按 index 序；不去重多命中——由消费缝的规则裁决）
  - `expand(index, concept_ids, *, ancestors: int) -> tuple[str, ...]`（自身+后代；`ancestors=1` 时并入最近一层祖先——检索缝用；`ancestors=0` 解析缝用）
  - `concept_terms(index, concept_ids, locale, *, ancestors: int) -> tuple[str, ...]`（扩展后的术语列表，按概念序 + 文件序去重）
  - `detect_locale(text: str) -> str`（"zh" if 含任意 CJK else "en"；与 `agent/planner._resolve_locale` 同规则——**planner 实现不动**，测试锁一致性）
- **步骤**：

- [x] 写失败测试：
  - [x] `load_concept_index`：文件缺失 → None；`ATLAS_CONCEPT_LAYER=off/0/false`（大小写空格容忍）→ None；畸形 JSON / 缺 `schema_version` → ValueError 且消息含路径
  - [x] `match_terms`：en 大小写不敏感；zh 子串命中；"总交易额" 命中 "交易额" 类最长匹配优先；返回带 `term / concept / source`
  - [x] `expand`：后代全量 + `ancestors=1` 一层祖先 / `ancestors=0` 不含祖先；`transitive` 边照常连通；输出确定序
  - [x] `detect_locale` 与 `Planner._resolve_locale` 在样例集（纯中文 / 纯英文 / 中英混排 / 覆盖参数）上逐例一致
- [x] 跑测试确认红
- [x] 实现模块（镜像 `agent/value_domain.py`：进程缓存 + `clear_cache()`；模块 docstring 写清"无索引 = 全缝旁路"契约）
- [x] `make test` 回归
- [x] 提交：`feat(concept): 概念索引 loader 与 matcher（注入式，无索引旁路，ADR-0032 M1-T3）`

- **收口判据**：测试全绿；模块零 rdflib 依赖（`grep -n rdflib agent/concept_index.py` → 0）；加载失败响亮不静默
- **停批条件**：发现产物 schema 无法支撑某消费缝（如检索缝需要而索引没存）→ 停，回 M1-T1 扩 schema（按 ADR ② 内容模型判，不即兴加字段）

#### M1-T4 `make lint` 第 7 节（概念索引 + 别名校验）

- **依据**：ADR-0032 ⑤（两级漂移防线：repo 文件派生部分 CI 严格可查）
- **开工前置**：M1-T1/T2/T3 收口
- **可改文件**：`semantic/lint.py`（新增 `check_concept_layer()` + `main()` 第 7 节）、`tests/test_concept_lint.py`（新建）
- **步骤**：

- [x] 写失败测试（参数化临时目录，镜像 `check_value_profiles` 测试模式）：
  - [x] 别名文件：非 registry 短名 → 错；空术语/重复 → 错；同 locale 同术语映射两个概念 → 错
  - [x] 索引：结构非法 → 错；概念 IRI ∉ registry → 错；`objects[].object` 不在语义模型（指标/字段/关系）→ 错；与 ossie `fibo_alignment` 不平（有孤儿/有漏配）→ 错
  - [x] `source_semantic_sha` ≠ 当前 `atlas_finance.ossie.yaml` sha256 → 错（改源不重建 = 红）
  - [x] 索引内 S3 术语 ≠ 当前别名文件合并结果 → 错
  - [x] 合法 fixture → 零错；目录不存在/为空 → 跳过不报错
- [x] 跑测试确认红
- [x] 实现 `check_concept_layer(ontology_dir=None, synonyms_dir=None)`；`main()` 增第 7 节（❌/✅ 打印与既有节同形；`✅ [concept] 概念索引与别名校验通过（<n> 概念 / <m> 条别名）` 或目录空时跳过提示）
- [x] `make lint` 实测全绿；`make test` 回归
- [x] 提交：`feat(lint): 概念索引与别名校验（第 7 节，ADR-0032 M1-T4）`

- **收口判据**：五类错误各有测试；`make lint` 输出含第 7 节
- **停批条件**：若"双向一致"在实现中发现需要改 ossie 模型文件才能成立 → 停（本计划零 ossie 变更），回 ADR 登记

#### M1-T5 concept_eval 真版 + EVAL_REPORT 新节（关闭 ADR-0007 待办 1）

- **依据**：ADR-0032 ⑥（载体/对照/产物/门禁行 1）、⑦ M1 关闭项
- **开工前置**：M1-T3/T4 收口
- **可改文件**：`eval/concept_eval.py`（真版）、`eval/report.py`（新节）、`tests/test_concept_eval.py`、`tests/test_report.py`（扩）
- **步骤**：

- [x] 写失败测试：固定合成索引 + 合成样本 → `match` 结果三分（hit / mismatch / miss）计数正确；分母 = 标注总数（162 口径由运行期实测，不写死）
- [x] 实现真版：对每个标注样本的问句跑 `match_terms`（locale 按 `detect_locale`），按标注 `{term→concept IRI}` 逐条比对（IRI→短名经索引 `iri` 反查）；产物 `eval/reports/concept-<sha>.json` 增 `"stage": "matcher"`、`"consistency": {"hit": n, "mismatch": n, "miss": n, "rate": "<n/m>"}`（**不在脚本里折算百分比口径以外的任何加权**；无层时基线 = 0 命中，数字为本体对照）
- [x] `eval/report.py`：新增独立节「概念层一致率（ADR-0032；与 EX / Plan Acc 分开）」读 `concept-<sha>.json`；`make report` 重生成 `EVAL_REPORT.md`
- [x] 实测：`make concept-eval` → 账本记录一致率与三分构成（<待实测> 由本次产物回填；**不预设阈值**）
- [x] `make test` 回归 + `make lint`
- [x] 提交：`eval(concept): 概念一致率评测真版 + EVAL_REPORT 独立节（ADR-0007 待办 1 关闭证据）`

- **收口判据**：`eval/reports/concept-<sha>.json` 在库；EVAL_REPORT 含独立节且数字 = 产物原文；ADR-0007 待办 1 可在账本标关闭
- **停批条件**：一致率异常低且集中于 registry 覆盖缺口（而非 matcher 缺陷）→ 如实报告（这正是 ADR「代价与限制」预期），**不停批也不粉饰**；若 matcher 对 86 样本全 miss → 停，排查术语采集（S1 是否真采到 label）

### 4.3 M2：检索缝（准确性；关闭 ADR-0007 待办 3）

#### M2-T1 SchemaLinker 术语扩展（注入式）

- **依据**：ADR-0032 ③（检索缝：S1+S2+S3 / 自身+后代+祖先 1 层 / match_type 不过滤 / 只扩展查询词不注入对象 / 无唯一性要求）
- **开工前置**：M1 收口
- **可改文件**：`agent/tools/schema_linker.py`、`tests/test_schema_linker_concept.py`（新建）
- **步骤**：

- [x] 写失败测试（合成小索引 fixture + 真实模型）：
  - [x] 注入 fixture 索引（含一个能桥接到 `total_trade_value` 文档的术语）→ 目标指标进 top-K（或在打分序中升位）；不注入 → 与现基线逐字一致（`SchemaLinkResult` 全字段比对）
  - [x] `concept_index=None` 与"索引文件不存在（loader 返回 None）"两情形，`link()` 输出与基线逐字一致
  - [x] 扩展只发生在查询词：`kept / dims` 阶段 1 输出不受影响（比对字段）
- [x] 跑测试确认红
- [x] 实现：`__init__(..., concept_index=None)`；`link()` 中构造 `query = question`，注入时 `query = _augment(question)`（`match_terms` → `concept_terms(..., ancestors=1)` → 追加未出现过的术语，确定序）；阶段 2 用 `query` 打分，阶段 1 仍用原问句
- [x] `make test` 回归（既有 `tests/test_schema_linker.py` 全绿 = 零回归）
- [x] 提交：`feat(linker): 检索缝概念术语扩展（注入式，ADR-0032 M2-T1）`

- **收口判据**：不注入/无索引逐字一致有测试；注入路径有确定性测试
- **停批条件**：若实现不得不改动阶段 1/3（图粗筛或重排）→ 停（越出 ADR ③ "只做术语扩展"授权）

#### M2-T2 `schema-link` A/B 对照（`--tag` 产物隔离）

- **依据**：ADR-0032 ⑥ 行 2（扩展开/关 A/B；Recall@K 不降 + 提升如实报告）
- **开工前置**：M2-T1 收口；M0 基线在库
- **可改文件**：`eval/schema_link_eval.py`（注入 + `--tag`）、`Makefile`（`TAG` 变量）
- **步骤**：

- [x] 改 `eval/schema_link_eval.py`：`SchemaLinker(model, popularity=hot, concept_index=load_concept_index(model.name))`（env 尊重）；报告增 `"concept_layer": "on"|"off"`；`--tag` 追加文件名后缀 `schema-link-<engine>-<sha>[-<tag>].json`（默认名不变 → `eval/report.py` §5 不受影响）
- [x] Makefile：`schema-link` 增 `$(if $(TAG),--tag $(TAG))`
- [x] **关臂**：`ATLAS_CONCEPT_LAYER=off make schema-link` → 与 M0 冻结基线**逐字段比对**（sha/时间戳/新增 `concept_layer` 字段除外，差异数必须为 0；比对脚本或 `python -c` 内联，命令记入账本）
- [x] **开臂**：`make schema-link TAG=concept` → 比对两臂 Recall@1/3/5 与 rank 分布（ADR-0032 ⑥ 载体含 bm25/fuse 两引擎；fuse 需向量环境，可用时同法 A/B，否则账本注明"仅 bm25 对照"）
- [x] 账本如实记录：不降 = 底线；提升 = 如实报告；**无提升或下降 → 按 ADR 推翻条件登记回退建议（移扩展或 `ATLAS_CONCEPT_LAYER` 默认 off 的 ADR 级决策）**，不修饰
- [x] 提交：`eval(linker): schema-link 概念层 A/B（off 臂零差异 + on 臂实测，ADR-0032 M2-T2）`

- **收口判据**：off 臂与基线零差异（降级证明的评测级证据）；on 臂数字入账本与报告
- **停批条件**：off 臂出现任何非元数据差异 → 停，回 M2-T1 查"旁路不变量"破在哪

#### M2-T3 降级证明测试（固化为测试，不是一次性命令）

- **依据**：ADR-0032 ⑥（降级证明行）、总原则（无索引 = 逐字一致）
- **开工前置**：M2-T1 收口
- **可改文件**：`tests/test_concept_degradation.py`（新建）
- **步骤**：

- [x] 写测试：
  - [x] retail（无索引）：`load_concept_index("atlas_retail")` → None；retail planner 与 schema_linker 对固定问句集输出与"未注入"基准逐字一致
  - [x] 删除索引情形：`ontology_dir=tmp（空）` → loader None → 同上的逐字一致
  - [x] `ATLAS_CONCEPT_LAYER=off` → loader None → 同上
  - [x] 基准定义方式：测试内先以"绝不注入"路径生成输出作为期望（确定性），再在"注入但旁路成立"路径比对——两路都必须绿
- [x] `make test` 回归
- [x] 提交：`test(concept): 无索引/off/未注入三情形逐字一致（ADR-0032 降级证明固化）`

- **收口判据**：三情形测试绿且断言是逐字比对（非抽样）
- **停批条件**：无（本卡即防线本身）

### 4.4 M3：解析缝（泛化；最高风险）

#### M3-T1 paraphrase 扩样（OOV / dev / holdout；H2 人工门禁）

- **依据**：ADR-0032 ⑥（paraphrase 扩充行）、⑦（人工投入）
- **开工前置**：M2 收口；**H2 解除**：人工完成 OOV 标注与 dev/holdout 划分（Agent 只出候选）
- **可改文件**：`eval/paraphrase_eval.py`（标记与分节）、`eval/gold/paraphrase/pp-*.json`（人工样本）、`tests/test_paraphrase_eval.py`（扩）
- **步骤**：

- [ ] 改 `eval/paraphrase_eval.py`：读取 `oov`/`holdout`（缺省 false）；报告分节 `dev` / `holdout`（互不合并）；增 `--tag`：无 tag 时产物名不变（`paraphrase-robustness.json`，兼容 M0 流程），`--tag T` 时写 `paraphrase-robustness-<sha>-T.json`（注释说明兼容规则）；增 `"concept_layer": "on"|"off"`；注入 `Planner(model, concept_index=load_concept_index(model.name))`（env 尊重）
- [ ] 扩 `tests/test_paraphrase_eval.py`：合成 OOV/holdout 样本 → 分节统计正确；无标记旧样本落 dev
- [ ] Agent 起草 OOV 候选样本（问句用未注册词汇表达已注册意图，附 expected_* 建议）→ **人工逐条确认/修正后才入库**（N1：标注不代签）；`holdout: true` 的样本只由人工裁定并此后禁改
- [ ] 跑 `ATLAS_CONCEPT_LAYER=off make paraphrase`：与新扩样本集上"关臂"数字入账（旧 13 条子集应与 M0 基线一致——逐条比对）
- [ ] `make test` 回归；提交：`eval(paraphrase): OOV/dev/holdout 扩样与分节报告（人工标注，ADR-0032 M3-T1）`

- **收口判据**：样本标记合法（字段过 gold schema 邻接校验/lint 不炸）；dev/holdout 分节产物在库
- **停批条件**：H2 未解除 → 本卡 BLOCKED（**禁止**用未标注样本来跑 M3-T4 出数字）

#### M3-T2 Planner 兜底（unmatched + 分层唯一性 + provenance）

- **依据**：ADR-0032 ③（解析缝全规则）
- **开工前置**：M3-T1 收口；M1-T3 loader 可用
- **可改文件**：`agent/planner.py`、`tests/test_planner_concept.py`（新建）
- **步骤**：

- [ ] 写失败测试（合成索引 fixture）：
  - [ ] 唯一解析：问句含 S3 术语（无指标同义词命中）+ 合法时间 → 完整 Plan（metric 来自概念锚定）+ `ConceptNotice`（term/concept/concept_iri/label/definition/object/match_type/confidence/note/source 齐备——与 §2 字段表逐一对应）
  - [ ] 分层唯一性：最高层（exact→broad→related）上 ≥2 个指标 → **no-op**：返回与"未注入索引"逐字相同的 `ClarificationRequest(kind="unmatched")`（kind/reasons/candidates 全比对）
  - [ ] confidence：medium 锚点不解析（no-op）；high 才解析
  - [ ] 术语源：S2 术语不触发解析（S2 只进检索缝）；S1（英文问句）可解析
  - [ ] 不外推祖先：祖先概念上的锚点不参与解析
  - [ ] 既有命中路径零变化：同义词命中时概念层从未被调用（monkeypatch spy 断言）
  - [ ] `concept_index=None` → 与今日行为逐字一致（既有测试全绿 = 证据）
- [ ] 跑测试确认红
- [ ] 实现：`Planner.__init__(..., concept_index=None)`；`plan_with_notices` 的 `metric_hits` 空分支：`fallback = self._resolve_metric_by_concept(question, locale)`；`None` → 原 `ClarificationRequest(kind="unmatched")` **原文案原样返回**；命中 → 以 resolved metric 继续既有下游解析（时间/维度/过滤），notice 并入返回元组（类型 `tuple[ValueNotice | ConceptNotice, ...]`，顺序：值域 notice 在前、概念 notice 在后——文档化）; `_resolve_metric_by_concept` 实现分层唯一性：`match_terms(sources={S1,S3})` → `expand(ancestors=0)` → 收集 `metric:*` 且 confidence=high 锚点 → 逐层 exact/broad/related 取最高非空层，恰 1 个指标才返回（≥2 → None）
- [ ] `make test` 回归；提交：`feat(planner): 解析缝概念兜底（unmatched 触发，分层唯一性，ADR-0032 M3-T2）`

- **收口判据**：上列测试全绿；"≥2 即 no-op"与"今日 ClarificationRequest 逐字相同"是硬断言
- **停批条件**：既有 planner 测试出现任一断言变化（除新增）→ 停，"既有匹配行为逐字不变"被破，回实现排查

#### M3-T3 通道与装配（graph / state / factory / cli / api / runner）

- **依据**：ADR-0032 ③（命中进确定性链）、④（注入式）、⑤（env 开关）
- **开工前置**：M3-T2 收口
- **可改文件**：`agent/graph.py`、`agent/state.py`、`agent/factory.py`、`agent/cli.py`、`serving/api.py`、`eval/runner.py`、`tests/test_concept_channel.py`（新建）
- **步骤**：

- [ ] 写失败测试：注入带 fixture 索引的 `Planner` 构造 DataAgent（fake executor）→ unmatched 问句跑出确定性 answer，`TurnResult` 级可拿到 `concept_chain`（本卡只保 state 通道：断言 `get_state` 里 `concept_chain` 内容正确）；未注入构造点行为与今日逐字一致
- [ ] `agent/graph.py`：`build_graph(..., planner: Planner | None = None)`；`planner = planner or Planner(model)`；`DataAgent.__init__(..., planner=None)` 透传 `_graph_kwargs`；plan 节点：`result, notices = planner.plan_with_notices(question)`（**核心路径改为 notices 入口，result 语义不变**）；从 notices 抽 `ConceptNotice` → `out["concept_chain"] = tuple(dict)`（按 notice 字段展平为纯 dict）；冲刷集合加 `"concept_chain": None`
- [ ] `agent/state.py`：`TurnState` 增 `concept_chain: tuple[dict[str, Any], ...] | None`（注释：ADR-0032 解释缝素材；纯 dict 保 serde 安全，**无需动 `_CHECKPOINT_SERDE` 白名单**）
- [ ] 装配点显式注入（env 尊重，`load_concept_index(model.name)`）：`agent/factory.py::create_live_agent`（Planner + SchemaLinker 两处）、`agent/cli.py` plan/query、`serving/api.py` `/plan`、`eval/runner.py`（gold 主评测同源——A/B 的评测载体）
- [ ] 写"checkpoint 保真"测试：跨轮（MemorySaver serialize 往返）`concept_chain` 类型与内容不退化（对照 `TestCheckpointStateFidelity` 模式）
- [ ] `make test` 全量回归（27+ 处 DataAgent 构造点零改动 = 默认逐字一致的证据）
- [ ] 提交：`feat(agent): 概念层注入位与 concept_chain 通道（graph/state/装配点，ADR-0032 M3-T3）`

- **收口判据**：全量测试绿；`grep -n "_CHECKPOINT_SERDE" agent/graph.py` 白名单**未增项**（纯 dict 设计的证据）
- **停批条件**：如必须引入新 dataclass 进 state → 停，评审是否扩大 serde 面（能纯 dict 就纯 dict）

#### M3-T4 A/B 对照 + 人工抽检 + 泛化结论

- **依据**：ADR-0032 ⑥ 行 3（drop 率对照 / drop 不升 / 零回归 / 新增命中人工抽检 / holdout = 最终结论）、推翻条件 1
- **开工前置**：M3-T3 收口；H2/H3 就位（人工可评审抽检）
- **可改文件**：`eval/reports/`（A/B 产物）、§7 账本
- **步骤**：

- [ ] 关臂：`ATLAS_CONCEPT_LAYER=off make paraphrase TAG=off` → `paraphrase-robustness-<sha>-off.json`；（旧 13 条子集与 M0 冻结基线逐条一致）
- [ ] 开臂：`make paraphrase TAG=concept` → drop 率对照（dev 分节；holdout 只记录不出结论）
- [ ] gold 零回归：`make eval`（开臂、真 Doris + 锁定快照）→ Plan Acc/EX 与 M0 基线比对：**零回归**（不降；如升 = 如实报告新命中样本清单）
- [ ] 人工抽检（H3）：从 paraphrase/gold 的新增命中中抽样，逐条判"解析正确/误解析"，错误率入账本；**误解析率超阈值 → 触发 ADR 推翻条件 1（关闭解析缝的评定）**——阈值由人工评审裁定并回写 ADR（本计划不预设数字）
- [ ] 产出泛化结论：holdout 数字 = 最终结论；"有提升/持平/无提升"三态如实写（无提升时按 ADR 登记降级叙事）
- [ ] 提交：`eval(planner): 解析缝 A/B 与 holdout 结论（ADR-0032 M3-T4，数字见 reports）`

- **收口判据**：off 臂旧子集与基线一致 + gold 零回归（有硬证据）；抽检记录在库；holdout 数字入账本
- **停批条件**：抽检发现误解析 → 立即停止扩大使用；误解析率评审超阈值 → 按 ADR 推翻条件关闭解析缝（保留检索+解释），**不得**先上新版本再补评审

### 4.5 M4：解释缝（后端通道）

#### M4-T1 explain 通道与 `/ask` 键（chart 先例）

- **依据**：ADR-0032 ③ 解释缝（只读；入 explain 输出、run events 与运行面板）、ADR-0025 决策 ②（新输出挂顶层并列键，explanation 键集不漂移）、ADR-0022 判据 6
- **开工前置**：M3 收口
- **可改文件**：`agent/state.py`（`TurnResult`）、`agent/graph.py`（`turn_from_state`/explain 节点）、`serving/api.py`、`agent/node_io.py`、`tests/test_api_contract_v2.py`、`tests/test_concept_explain.py`（新建）
- **步骤**：

- [ ] 写失败测试：带 `concept_chain` state 的回合 → `TurnResult.concept_chain` 为元组 dict；`/ask` payload 顶层含 `"concept_chain"`（无链时为 null）；`explanation` 键集**仍为 13 键**（`EXPLANATION_KEYS` 不动）
- [ ] `TurnResult` 增 `concept_chain: tuple[dict[str, Any], ...] | None = None`；`turn_from_state` 映射（与 `chart` 并列注释先例）
- [ ] `serving/api.py` `/ask` payload 增 `"concept_chain": result.concept_chain`（注释引 chart 先例与 ADR-0032 ③）
- [ ] `agent/node_io.py` `NODE_IO_SPEC`：`plan.output`、`explain.output` 各增 `"concept_chain"`
- [ ] `tests/test_api_contract_v2.py`：按现有 /ask 键断言形态同步（新增键若在断言集内则加入；**不动** `EXPLANATION_KEYS`）
- [ ] `make test` + `make api-verify`（可运行时）回归
- [ ] 提交：`feat(explain): concept_chain 顶层键与 node_io 通道（ADR-0032 M4-T1）`

- **收口判据**：explanation 键集不变有断言；concept_chain 在 /ask 与事件通道可见
- **停批条件**：若实现倾向改为塞进 explanation → 停（违 ADR-0025 先例与 0022 键集纪律），回 ADR 登记再定

#### M4-T2 四要素链测试（≥3 条已标注 gold，plan+compile 级）

- **依据**：ADR-0032 ⑥ 行 4（关闭待办 2 的证据形态）
- **开工前置**：M4-T1 收口
- **可改文件**：`tests/test_concept_explain.py`
- **步骤**：

- [ ] 写测试（不依赖 Doris）：
  - [ ] 选 **≥3 条**含 `fibo_concepts` 标注的 gold（各角色覆盖：measure/entity/dimension 类），断言：每个标注 concept ∈ 索引；至少一个标注概念锚定 `plan.metric`（`objects` 含 `metric:<plan.metric>`）；组装链 = `{term→concept_iri/label/definition→object→note}` 四要素齐备；`Compiler(model).compile(plan)` 成功（plan+compile 级完整链）
  - [ ] 运行期路径：fixture 索引 + OOV 问句经 DataAgent（fake executor）→ `TurnResult.concept_chain` 四要素与 notice 一致
- [ ] `make test` 回归
- [ ] 提交：`test(explain): ≥3 条 gold 四要素链固化（ADR-0007 待办 2 链级证据）`

- **收口判据**：≥3 条 gold 链 + 1 条运行期链全绿；账本记链级证据（待办 2 最终关闭 = 本卡 + M5-T3 运行面板展示）
- **停批条件**：找不到 3 条"标注概念锚定 plan.metric"的 gold → 先核查是否映射缺口（ADR-0007 既有缺口不硬补），换样本并在账本说明选择口径

### 4.6 M5：前端布局与概念链体验（前后端一体交付；2026-10-14 用户指令）

> **总体要求（用户指令口径）**：参考市面主流系统的前端布局进行调整，注重用户交互体验，布局丰富细腻，交互富含科技感。落地口径 = 主流数据平台导航骨架 + 星图设计系统延续（仪器感而非玩具感）+ 零新依赖。

#### M5-T1 全局布局骨架升级（左导航 + 顶部工具栏）

- **依据**：用户指令（2026-10-14）；`docs/design/frontend-redesign.md`（星图设计系统）；ADR-0018（前端工程边界）
- **参考布局（共性骨架，落地为 Atlas 语义）**：左侧垂直导航（图标 + 文字、可折叠、活跃指示）——Databricks / Snowflake Snowsight / Metabase / Superset / Quick BI 的共同骨架；顶部工具栏承载全局状态（快照封印、主题、身份）；内容区归各面板自组织（本卡不动面板内部）。Atlas 签名保留：溯源脊、快照封印、星图网格、等宽读数。
- **开工前置**：H5 解除（Phase 1 合入 main 且本分支已 rebase）；`make ui-check` 基线绿
- **可改文件**：`frontend/src/App.tsx`、`frontend/src/styles/global.css`、`frontend/src/__tests__/app.test.tsx`（扩）
- **步骤**：

- [ ] 写失败测试（扩 `app.test.tsx`）：渲染 `/ask` → 侧栏导航含 4 项（工作台 / 运行 / 数据 / 治理）；活跃项带 `.atlas-nav-item--active`；保留既有 "Atlas 控制台" 文本断言（标题移入侧栏顶部后仍须命中）
- [ ] 跑 `cd frontend && npm test -- app.test.tsx` 确认红
- [ ] 实现 `App.tsx`：`Layout` 改「`Layout.Sider`（`collapsible`、`breakpoint="lg"`、surface 底）+ 内嵌 `Layout`（Header 精简为工具栏 + Content）」；`Menu` 由 `mode="horizontal"` 改 `mode="inline"`；`selectedKey` 映射逻辑原样保留（`/governance* → /governance`）；**路由表零改动**
- [ ] CSS（`global.css`）：`.atlas-sider`、`.atlas-nav-item--active` 左缘 2px 青色指示 + 发光（`box-shadow`），过渡 150ms；`prefers-reduced-motion` 关闭；窄屏折叠态可展开
- [ ] 跑 `make ui-check` 全绿；`npm run dev` 浏览器走查（明暗主题、窄屏折叠、导航活跃态），截图入 `docs/screenshots/`
- [ ] 提交：`feat(ui): 全局布局升级为左侧导航 + 顶部工具栏（主流数据平台骨架）`

- **收口判据**：4 导航项在侧栏且活跃高亮；窄屏自动折叠；既有守线测试全绿；走查截图在库
- **停批条件**：若需改路由结构或动各面板内部布局 → 停（本卡只动骨架；面板内部归 frontend-redesign Phase 2 系列）
- **提交拆分**：单提交（App.tsx + global.css + 测试）

#### M5-T2 问数仪表舱：概念链体验 + 历史区右栏化

- **依据**：ADR-0032 ③ 解释缝（四要素链入 explain 输出与运行面板）；M4-T1 的 `/ask` 顶层键；用户指令（布局丰富细腻）
- **开工前置**：M4-T1 收口（`/ask` payload 有 `concept_chain` 键）；M5-T1 收口
- **可改文件**：`frontend/src/components/ConceptChain.tsx`（新建）、`frontend/src/api/types.ts`、`frontend/src/panels/workbench/ExplanationBlock.tsx`、`frontend/src/panels/workbench/AskWorkbench.tsx`、`frontend/src/__tests__/concept-chain.test.tsx`（新建）
- **接口**：
  - `ConceptChainItem`（`types.ts`，与后端 §2 `ConceptNotice` 10 字段逐一对齐）：`{term, concept, concept_iri, label, definition, object, match_type, confidence, note, source}`
  - `TurnPayload` 增可选键 `concept_chain?: ConceptChainItem[] | null`（向后兼容：旧后端无键 = undefined = 不渲染）
  - `ConceptChain({ items }: { items: ConceptChainItem[] | null })`
- **步骤**：

- [ ] 写失败测试（`concept-chain.test.tsx`）：2 项 chain 渲染 2 个 `.atlas-chain__item`（含 staggered 延迟），各含 term、概念 label 与 IRI（`.atlas-mono`）、object、note；`items=null/[]` → 容器零渲染（`querySelector(".atlas-chain")` 为 null）；缺字段不编造（渲染 "（无）"）
- [ ] 跑测试确认红
- [ ] `types.ts` 增 `ConceptChainItem` 与 `TurnPayload.concept_chain`
- [ ] 实现 `ConceptChain`：垂直链（复用 `.atlas-spine__node` 视觉语言），逐项两行——主行 `term → label`（match_type/confidence 用 Tag：high=青、medium=琥珀）+ 副行 object/note；definition 用 Tooltip；全部读数 mono
- [ ] `ExplanationBlock` 增 `conceptChain` prop：null/空 → 渲染零变化（不加占位）；非空 → Descriptions 之后追加 `<ConceptChain>` 子块；`AskWorkbench` 透传 `payload.concept_chain`
- [ ] 历史区右栏化：`AskWorkbench` 主体改两栏（`grid-template-columns: minmax(0,1fr) 320px`，≥1200px 生效；<1200px 回落单列历史区垫底）；**只重排区块位置，不动各区块内部渲染与 `ANSWER_SECTIONS`**
- [ ] `make ui-check` 全绿（`order.test.ts` / `honesty.test.ts` 零改动即证据）
- [ ] 提交：`feat(workbench): 概念链四要素展示与历史区右栏化（ADR-0032 解释缝前端）`

- **收口判据**：四要素齐备有测试；null 零渲染有测试；`order.test.ts` / `honesty.test.ts` 未改且绿；宽/窄屏走查截图在库
- **停批条件**：若发现需把链塞进 `explanation`（键集冻结）或动渲染序 → 停，回 M4-T1 通道设计
- **提交拆分**：ConceptChain（含 types/ExplanationBlock/测试）与历史区右栏化可同提交（同文件族）——执行者按可审性自裁二拆

#### M5-T3 运行观测台：概念链接入 node_io 通道

- **依据**：ADR-0032 ③（解释缝"入 run events 与前端运行面板 node_io 通道展示（复用既有 NodeInspector，不新造治理 UI）"）
- **开工前置**：M4-T1 收口（`node_io` 增 `concept_chain`）；frontend-redesign Phase 2a 已合并（否则停批并在账本登记，避免与观测台布局重排冲突）
- **可改文件**：`frontend/src/panels/runs/node-io-view.ts`、`frontend/src/panels/runs/NodeInspector.tsx`、`frontend/src/__tests__/node-io-view.test.ts`（新建）
- **步骤**：

- [ ] 写失败测试（`node-io-view.test.ts`，新建——该文件现无专测）：`formatNodeIO({concept_chain: [2 项]})` → 条目 display 形如 `2 条 · 交易额→TotalTradeValue`、expandable=true；`concept_chain: []` → `0 项`（沿用既有数组分支语义）；既有键（plan/sql/clarification）行为逐字不变（逐字断言）
- [ ] 跑测试确认红
- [ ] `node-io-view.ts`：`formatValue` 增 `concept_chain` 分支（置于数组通用分支之前）：逐项 `term→concept` 摘要（最多前 3 项 + "等 N 条"）；`formatNodeIO` 其余逻辑不动
- [ ] `NodeInspector.tsx`：concept_chain 条目展开视图——逐项四要素（term / concept + IRI（mono）/ label / definition / object / note / match_type+confidence Tag），复用既有「展开」面板机制，不改 NodeInspector 主体结构
- [ ] `make ui-check` 全绿
- [ ] 提交：`feat(runs): 概念链 node_io 展示（NodeInspector 四要素，ADR-0032 解释缝）`

- **收口判据**：formatter 专测绿且既有键逐字不变；ui-check 绿
- **停批条件**：若 NodeInspector 需大改（而非增一类键处理）→ 停，先与 Phase 2a 的界面重排合并评估
- **提交拆分**：单提交

#### M5-T4 前端全量验证与文档收口（覆盖全量交付）

- **依据**：ADR-0032 ⑦ M4 行（文档全量 + 全量回归）；AGENTS N2/N4；§11 步骤 ⑦⑧；用户指令（前端三态诚实登记）
- **开工前置**：M0–M5 各卡按实况收口（只写"实际交付了什么"）
- **可改文件**：`README.md`、`EVAL_REPORT.md`（重生成）、`docs/design/frontend-redesign.md`（实施状态注记）、`docs/screenshots/`（走查截图）、本文件 §7 账本
- **步骤**：

- [ ] README 能力登记 + Known Limitations：按实测三态写（概念索引构建/检索缝/解析缝/解释缝/前端布局与概念链各自"已交付 or 待实现 or 受限"）；KL 追加（闭包派生部分 CI 不可查、curation 人工带宽、覆盖边界=`total_trade_tax` 等既有缺口、解析缝保守命中率、S3 是唯一中文新词汇来源、**M5 只动全局骨架与概念链展示、未动各面板内部**、**概念链仅在命中时展示、无链 = 旧界面逐字**）——**不删既有 KL**
- [ ] `grep -n "提升\|达到\|已实现" README.md`（针对概念层与前端段落）→ 每处皆可指向 `eval/reports/` 或截图证据；无未溯源数字
- [ ] 浏览器走查（演示模式全链路：登录 → 问数 → 概念链 → 运行面板 → 治理只读 → 明暗主题 → 窄屏折叠 → `prefers-reduced-motion`）；截图入 `docs/screenshots/` 并在账本登记文件名
- [ ] `docs/design/frontend-redesign.md`「实施状态」追加注记（本次：全局布局骨架升级 + 概念链体验 + 历史区右栏化；不改设计原则与诚实性约束）
- [ ] `make report` 重生成 `EVAL_REPORT.md`（含概念节与 A/B 数字）
- [ ] 全量门禁：`make lint && make test && make ui-check`；`make ontology-build CHECK=1`（漂移零 diff）；对照实验产物齐（§7 表）；（可运行时）`make eval`
- [ ] 账本收口：ADR-0032 验证方式 7 项 + 前端门禁（布局骨架 / 概念链双端可见 / ui-check）逐条勾状态；未达成项如实标 BLOCKED/部分
- [ ] 提交：`docs(concept): 能力登记与 KL 同步（前后端三态诚实，ADR-0032 M5 收口）`

- **收口判据**：§7 总表全绿或显式标注未达成及理由；无 N1/N2 违规；`make ui-check` 绿
- **停批条件**：无（纯收口；若发现与实测不符即回改对应卡，不粉饰）

---

## 5. 明确排除（越界即违 ADR-0032 ①）

| 项 | 归属 | 排除理由 |
|---|---|---|
| 隐式值过滤（"股票成交额"式意图过滤） | ADR-0016 域独立立项 | ADR-0032 ① 范围外登记：属 Planner 值域/过滤语义缺口，概念层不解决 |
| 维度槽概念兜底（"按 X 统计"X 同义词 miss） | 二期候选 | ADR-0032 ① 登记；需先核实今日行为 |
| validate 节点概念类型校验 | 设计性不做 | ADR-0032 ① 明确不做（与图可达性职责重复） |
| LLM 生成别名 | 后续飞轮（ADR-0027 模式） | ADR-0032 备选方案已否（错误别名=自信地答错） |
| 运行时 rdflib 动态加载 | 设计性不做 | ADR-0032 备选已否（`fibo-src` 不入库 → 不可复现） |
| 全量 FIBO 加载 / 闭包概念批量入 registry | 设计性不做 | ADR-0032 备选已否（子集引入 + 29 条人工审核清单纪律） |
| 本计划修改 `semantic/ossie/` 任何文件 | — | 零 fibo_alignment 变更；映射缺口（如 `total_trade_tax`）按 ADR-0007 既有待办处理 |
| frontend-redesign Phase 2b/2c/3（治理星图 / 数据接入港 / 优化引擎舱内部重设计） | 各自独立计划 | 本计划 M5 只做全局骨架与概念链体验，不吞并面板内部重设计 |
| 移动端原生适配 / 触屏手势 | 不属本期 | 响应式折叠（Sider `breakpoint`）即止 |
| 新前端依赖（图表库 / 动画库 / 组件库） | 技术栈锁定（ADR-0018） | M5 零新依赖：AntD 既有栈 + CSS 动效 |
| 治理面新增「概念索引」子页（第 9 子页） | ADR-0032 ③ 明确"复用既有 NodeInspector，不新造治理 UI"；8 子页有结构断言测试 | 概念层前端展示只落解释缝三处（问数 / 运行 / 通知），不动治理 8 子页 |

---

## 6. 提交拆分总则（AGENTS §8）

- M0：`eval(baseline)` / `eval(concept)` / `docs(eval)` / `docs(adr)` / `contract(agents) [contract]` 各自独立；
- M1：`feat(ontology)`（构建器+产物+README 同一提交——审计链）/ `semantic(synonyms)`（人工别名，说明影响面）/ `feat(concept)` / `feat(lint)` / `eval(concept)`；
- M2/M3/M4：`feat(linker)` / `feat(planner)` / `feat(agent)` / `feat(explain)` / `test(concept)` / `eval(...)`；
- M5：`feat(ui)`（骨架）/ `feat(workbench)`（概念链 + 历史区）/ `feat(runs)`（node_io 概念链）/ `docs(concept)`（收口）；前端提交必须附 `make ui-check` 绿证据；
- **不混 `refactor` + `semantic`**；`semantic/` 变更（仅 M1-T2 别名文件）必须写明影响面（哪些解析/检索行为受影响，哪些指标口径不受影响）；
- 每个 PR 附：评测摘要（指向 `eval/reports/` 具体文件）、安全摘要（本计划零 Guard 接触）、破坏性变更摘要（API 仅增 `concept_chain` 键）。

---

## 7. 收口总判据（映射 ADR-0032 验证方式 + 推进记录）

### 7.1 总表（执行时逐条勾）

| ADR-0032 验证方式 | 落卡 | 证据产物 / 命令 | 状态 |
|---|---|---|---|
| M0：4 入口基线产物入库 + paraphrase 扩样格式（OOV + dev/holdout） | M0-T1/T3 | `eval/reports/*ad99a2b*.json`（4 份）+ `eval/gold/paraphrase/README.md` | DONE（ad99a2b，2026-10-14） |
| M1：`make ontology-build && make lint` 全绿；重建确定性（`--check` diff 空）；概念一致率入报告（待办 1 关闭） | M1-T1/T4/T5 | 产物 + `concept-<sha>.json` + EVAL_REPORT 节 | NOT_STARTED |
| M2：`make schema-link` 开/关对照 Recall@K 数字与结论（待办 3 关闭） | M2-T2/T3 | `schema-link-bm25-<sha>[-concept].json` + 零差异比对 | NOT_STARTED |
| M3：`make paraphrase` 对照 + gold 零回归；抽检记录；holdout = 泛化结论 | M3-T1–T4 | A/B 产物 + 抽检记录 + holdout 数字 | NOT_STARTED |
| M4：≥3 条 gold 四要素链（ADR-0007 待办 2 链级证据） | M4-T2 | `tests/test_concept_explain.py` | NOT_STARTED |
| M5：布局骨架 + 概念链双端可见（问数/运行）+ ui-check 全绿（前后端一体；待办 2 展示面） | M5-T1–T4 | 走查截图 + `concept-chain.test.tsx` / `node-io-view.test.ts` + `make ui-check` | NOT_STARTED |
| 降级证明：retail 与删索引情形输出与基线逐字一致（测试固化） | M2-T3 | `tests/test_concept_degradation.py` | NOT_STARTED |
| 收益数字全部 `<待实测>`，由脚本产物回填（N1） | 全程 | §7.2 账本 | NOT_STARTED |

### 7.2 推进记录（执行时逐卡追加；一行一卡）

| 卡 | 状态 | sha | 命令 | 产物路径 | 关键数字（实测） | 阻塞/下一动作 |
|---|---|---|---|---|---|---|
| M0-T4 | COMPLETE | `243fa8f` | —（文档注记） | ADR-0007/0015 末端注记（正文一字未改）；本计划入库 | — | 下一卡 M0-T5 |
| M0-T5 | COMPLETE | `8396fe4`（契约修复 `3e7238d`） | `make lint && make test` | `AGENTS.md` §3/§4/§7.3；`semantic/lint.py`；`tests/test_api_contract_v2.py` | make test 1271 例 OK；修复 e71ff8d 漏登记的契约漂移 2 例（计数断言 44→45）；首跑曾现 1 例未捕获 flaky，复跑两次全绿 | 下一卡 M0-T3 |
| M0-T3 | COMPLETE | `88e09d4` | —（格式定版） | `eval/gold/paraphrase/README.md` | 字段 10 项（8 既有 + `oov`/`holdout` 缺省 false）；OOV 样例 pp-oov-1；curation 三纪律 | 下一卡 M0-T1 |
| M0-T1 | COMPLETE | `ad99a2b`（冻结点） | `make paraphrase; make retrieve; make schema-link; make eval; make test` | `eval/reports/ad99a2b.json`；`retrieval-bm25-ad99a2b.json`；`schema-link-bm25-ad99a2b.json`；`paraphrase-robustness-ad99a2b.json`；`data/snapshots/ad99a2b.meta.json` | paraphrase plan_acc 9/13（drop 30.77%）；retrieve Recall@1 69/73 @5 72/73；schema-link 指标 Recall@1 71/73（表真值子集 66/73，skip 7 显式登记）；make eval finance EX 73/73 / retail EX 24/24、clarify 6/6+3/3、exec_errors 0；快照重锁指纹与 e0e0422/1e2e557 全一致（`make eval` 走 `--check` noop）；make test 1271 例 OK | 基线修复两处（fix `ad99a2b`）：域目录 glob 漂移（retrieve n=0→73）+ 缺真阻断（7 条显式登记）；诚实性常量合法更新 3 处（STRUCTURED_REPORTS 17 / REPORT_PATTERNS 26 / FIRST_SNAPSHOT_SHA `ad99a2b`，测试注释登记）；flaky 1 例捕获名字（test_r2_clarify_flushes_stale_analysis_record，单跑 3 次与复跑全量全绿，测试间干扰待查——M0-T5 遗留）；下一卡 M0-T2 |
| M0-T2 | COMPLETE | `b96ee5f`（产物绑定 sha） | `make concept-eval; make test` | `eval/reports/concept-b96ee5f.json`；`eval/concept_eval.py`；`tests/test_concept_eval.py`；`Makefile`（concept-eval） | samples 106 / annotated 86 / terms 162（measure 62 / dimension 33 / event 30 / entity 20 / time 17）；concepts_distinct 11；registry_coverage 11/0，missing 空（停批条件不触发） | 产物无一致率字段（matcher 属 M1-T5，N1/N2）；REPORT_PATTERNS 定 26（提交态口径 = 24 基线 + paraphrase-robustness-<sha> + concept-<sha>；修正 M0-T1 含本机运行残留的首测偏差——固定名 paraphrase-robustness.json 残留已清理、不计不入库，口径注释已登记）；make test 1273 例 OK；M0 五卡全部收口——下一里程碑 M1（T1 构建器起步；人工门禁 H1 阻塞 M1-T2） |
| M1-T1 | COMPLETE | `6612761`（开工 sha） | `make ontology-build; make ontology-build CHECK=1; make test` | `semantic/ontology/atlas_finance.concept_index.json`；`semantic/ontology/README.md`；`data/fibo/build_concept_index.py`；`data/fibo/__init__.py`；`tests/test_concept_build.py`；`Makefile`（ontology-build） | concepts 29 / objects 31（31 条映射全落位，jq 核对）/ terms 118（S1=42 / S2=76 / S3=0——M1-T2 未建）；CHECK=1 零漂移（剔除 generated_at）；make test 1281 例 OK（1273+8） | 计划接口缺口 2 处按「测试与产物 schema 为准」补全并登记（`MappingEntry.synonyms` S2 入口 + `assemble_index` 第 5 参数 `fibo_facts` S1 入口；测试 docstring 与提交说明说明依据）；下一卡 M1-T2 被 H1 人工门禁阻塞（Agent 只出候选清单，不代签） |
| M1-T2 | COMPLETE | `b2396e4`（开工 sha） | `make ontology-build`；`make ontology-build CHECK=1`；`make test` | `semantic/synonyms/concepts_zh_cn.yml`（新建）；`semantic/ontology/atlas_finance.concept_index.json`（重建 terms 118→122）；`tests/test_locale_synonyms.py`（守护测试同步） | 首批 4 术语（H1 人工逐条裁定；数据源=paraphrase dev 集，holdout 均缺省 false）：经纪服务费 / 券商抽成 → Fee（pp-comm-3/4 drop 溯源）；成交总金额 → MonetaryAmount（人工将「买卖总金额」改词为「成交总金额」——非 pp-101-5 问句子串，该样本 M3 复跑如实观察）；个股 → EquityInstrument（「股票」裁定不注册：objects=[] 无桥接面）；「网点」排除（已注册于 ossie Branch synonyms，缺口属形态层）；结构自检：4 术语键全在 iri_registry、无重复；CHECK=1 零漂移；make test 1281 例 OK | 计划外登记 2 处：① 新增 concepts_ 子族触发 test_locale_synonyms 目录守护红 → 同步更新（排除 concepts_ 前缀 + docstring 说明第三族归属；依 M0-T5 先例「既有测试锁定则一并更新并登记」）② 该文件历史遗留 format 债务（42e5fa8 起、早于现行 format 基线）顺带修复（0.6.8 钩子版与 0.16.5 双版 format/check 全过）；发现基线遗留 1 处（未修，建议独立卡）：`make lint` [values] 21 红——ad99a2b 重锁后 values 未同步绑定（M0 冻结批次遗留，与 M1 改动无关；git status 实证未触碰 values/snapshots） |
| M1-T3 | COMPLETE | `cdfab9b`（开工 sha） | `pytest tests/test_concept_index.py`；`mypy agent/concept_index.py`；`make test`；ruff 双版本（0.6.8 钩子 + 0.16.5）；真实产物冒烟 | `agent/concept_index.py`（新建，376 行）；`tests/test_concept_index.py`（新建，441 行） | 测试 31 passed + 22 subtests；make test 1312 例 OK（1281+31，skipped=14）；收口判据全部满足：`grep rdflib agent/concept_index.py` → 0（docstring 以「零本体解析依赖」表述）、mypy 严格 Success、加载失败全部响亮 ValueError 含路径；真实产物冒烟（29 概念）：经纪服务费 / 券商抽成 → Fee(S3)、「买卖总金额」按 M1-T2 裁定不注册 → 空、Fee+ancestors=1 → (Fee, MonetaryAmount)、Fee zh 术语 11 条；零消费缝改动（planner / schema_linker / explain 未触碰）= 真旁路 | 裁定 2 处并登记：① terms 非空纳入强校验（镜像 value_domain「registered 非空」先例）② matcher 按问句 locale 过滤术语语种（ADR-0032 ③「按问句 locale 取语种」字面依据）；测试 1 处断言修正（fixture m:Amount 含后代 m:Fee）；下一卡 M1-T4（lint 第 7 节；开工前置 T1/T2/T3 已收口） |
| M1-T4 | COMPLETE | `1d65cc3`（开工 sha；前置修复 `206e617`） | `pytest tests/test_concept_lint.py`；`make lint`；`make test`；`uv run mypy`（基线归属实证）；ruff 双版本 | `semantic/lint.py`（+216 行：`check_concept_layer()` + `main()` 第 7 节）；`tests/test_concept_lint.py`（新建，269 行 / 20 例）；`data/fibo/build_concept_index.py`（2 行重命名修复） | 20 passed（先红后绿：ImportError → 全绿）；`make lint` 全绿含 `✅ [concept] 概念索引与别名校验通过（29 概念 / 4 条别名）`；`make test` 1332 例 OK（1312+20，skipped=14）；mypy 归属实证：基线 91 errors/17 files → 含变更 93/18（增量恰 2 条，全在 build_concept_index.py）→ 修复后 91/17 与基线逐字一致（diff 空）；收口判据满足（五类错误各有测试 + lint 含第 7 节）；停批条件不触发（双向一致无需改 ossie，本计划零 ossie 变更） | 前置修复 1 处（`206e617`）：M0-T1 重锁 ad99a2b 漏跑 profile-values 致 [values] 21 红，依 Makefile L240 纪律重跑（21 文件 ×3 处 sha/generated_at/note 文本；值本体与人工别名零变化）；超清单登记 1 处：build_concept_index.py 变量名修复（`parents` 循环变量复用致 2 个 mypy 推断错，经 lint.py 新 import 检查面暴露；零行为变化，test_concept_build 8 例护航；依 M1-T2 先例登记）；下一卡 M1-T5（concept_eval 真版 + EVAL_REPORT 新节；前置 T3/T4 已收口） |
| M1-T5 | COMPLETE | `213d5f8`（开工 sha） | `pytest tests/test_concept_eval.py tests/test_report.py`；`make concept-eval`；`make report`；`make test`；`make lint`；`uv run mypy`；ruff 双版本 | `eval/concept_eval.py`（真版：classify_annotation / summarize_consistency / build_report）；`eval/report.py`（§8 概念层一致率节 + `_snapshot_ref()` 头部快照引用 + §9 来源清单）；`tests/test_concept_eval.py`（+10 例）；`tests/test_report.py`（+7 例）；`Makefile`（concept-eval 文案 2 处）；`EVAL_REPORT.md`（重生成，绑 213d5f8）；`eval/reports/concept-213d5f8.json`（产物） | 实测（产物原文，不预设阈值）：hit 55 / mismatch 12 / miss 95，rate **55/162**（不折算加权）；samples 106 / annotated 86 / terms 162；concepts_distinct 11；registry_coverage 11/0 missing 空；layer_enabled=true；停批条件不触发（非全 miss，registry 无缺口）；EVAL_REPORT §8 数字 = 产物原文逐字；`make test` 1349 例 OK（1332+17，skipped=14）；mypy 91 errors/17 files 与基线一致（改动文件零错误）；`make lint` 全绿 | ADR-0007 待办 1 **关闭**：概念映射准确率实测值进入 EVAL_REPORT 且与 EX / Plan Acc 分开报告（ADR 正文一字未改——承接注记 2026-10-14 已入，本轮补产物证据面）；裁定 3 处并登记：① 新节为 §8、来源清单顺延 §9（尾注属性保留；README 仅 Day 39 历史条目提及旧编号，不追改）② 标注概念不在索引（反查失败）与未命中同归 miss（真实数据不触发：registry 全覆盖）③ 新增 layer_enabled 字段标记层可用性（无层 0 命中=本体对照，防误读）；计划外修复 3 处并登记（依 M1-T4 先例）：① report.py 头部快照假引用——曾硬编码 `data/snapshots/{HEAD}.meta.json`（213d5f8.meta.json 不存在，重生成即假引用），改 `_snapshot_ref()` 取 created_at 最新真实 meta、缺失占位 ② §9 幻影条目——主报告缺失不再无条件列出（文件级可审计面不声称不存在的文件）③ `SNAPSHOT_DIR` 改用 eval/runner re-export 权威常量——初版自建副本被 test_identity 判据 5(b)（快照目录构造唯一）守卫拦截，41 例回归全过；下一卡 M2-T1（检索缝术语扩展；开工前置 M1 收口） |
| M2-T1 | COMPLETE | `a5055ed`（开工 sha） | `pytest tests/test_schema_linker_concept.py`（先红后绿）；`make test`；`make lint`；`mypy`（基线 91/17 实证）；ruff format/check | `agent/tools/schema_linker.py`（注入式：`concept_index` 构造参数 + `_augment()` 仅阶段 2 查询词扩展）；`tests/test_schema_linker_concept.py`（新建，8 例） | 先红：6 failed（TypeError: unexpected keyword 'concept_index'）/ 2 passed（前置基线锁定）→ 全绿 15 passed（8 新 + 既有 test_schema_linker 7 零回归）；桥接实测（合成 fixture）：基线「2013 年销售款是多少？」零重叠 → scores/candidates 全空，注入后 `total_trade_value` 双问句 top-1（scores[0] + candidates[0]）；阶段 1 不变量：dims/kept 与基线逐字一致（含 Branch 维度问句）；旁路：None / loader-None（atlas_retail → None）与基线 SchemaLinkResult 全字段一致；`make test` 1357 例 OK（1349+8，skipped=14）；mypy 91 errors/17 files 与基线逐字一致（改动文件零错误）；`make lint` 全绿 | 裁定 1 处并登记：阶段 3 重排仍收**原问句**（rerank 调用语义零改动）——同义词置信度信号定义即「问句直接用了指标名」，机器追加术语不计入（防对象注入变体；ADR ③「只做术语扩展」+ 停批条件「不改阶段 1/3」字面执行）；收口判据满足（旁路逐字一致 + 注入确定性均有测试）；停批条件不触发（阶段 1/3 零改动）；M1 里程碑 H4 验收通过（用户确认）→ 开工前置解除；下一卡 M2-T2（A/B 对照，off 臂零差异 + on 臂实测） |
| M2-T2 | COMPLETE | `8f8e4b9`（开工 sha） | `ATLAS_CONCEPT_LAYER=off make schema-link`；`make schema-link TAG=concept`；逐字段比对脚本（递归 walk 两 JSON + 豁免集 /sha, /base_bm25_day24/report, /concept_layer）；`make test`；`make lint`；`mypy`；ruff format/check | `eval/schema_link_eval.py`（注入 + `concept_layer` 字段 + `--tag` 后缀）；`Makefile`（`$(if $(TAG),--tag $(TAG))` + 注释 44→73 校正）；`tests/test_api_contract_v2.py`（REPORT_PATTERNS 26→27 + 注释登记）；`eval/reports/schema-link-bm25-8f8e4b9.json`（off 臂）/ `schema-link-bm25-8f8e4b9-concept.json`（on 臂） | 关臂 vs M0 冻结基线（schema-link-bm25-ad99a2b.json）逐字段：差异路径 = ['/base_bm25_day24/report', '/concept_layer', '/sha'] 恰为豁免三处，**豁免外差异 0**；开臂 vs 关臂：metrics 逐字同（Recall@1 71/73、@3 71/73、@5 72/73、表覆盖 64/66、65/66、fail 1）；rank 分布两臂同 {0:1, 1:71, 4:1}、rank 变化 []；top5 集合变化 54/73（同 rank 后段构成序变、主指标零贡献）；**不降但无提升**——归因（可验证事实）：gold-166 英文问句 "How many trades were there in May 2014?" top5 全空（既有 BM25 整词 tokenize 缺口 trades≠trade；locale=en 只取英文术语、doc 为中文不桥接；trade_count 无映射），两臂一致；gold-167 rank 4 两臂一致；`make test` 1357 例 OK（REPORT_PATTERNS 27 修复后复跑全绿）；`make lint` 全绿；mypy 91/17 与基线逐字一致 | 裁定/登记 5 处：① 计划接口缺口——`load_concept_index(model.name)` 直传必返 None（model.name "atlas_finance_analytics" ≠ 产物固定名 "atlas_finance"，M1-T4 lint 锁定）→ 评测走 loader 默认参数（finance 域专用，注释登记；缺口留 M3 前对齐）② **按 ADR L179-180/L232 登记回退建议（不拍板，§8「解禁裁定」属 M3-T4）**：检索缝无提升 → 二选一（a）保留扩展待 M3 解析缝与 curation 复评（b）移除扩展或 `ATLAS_CONCEPT_LAYER` 默认 off——供人工裁定 ③ REPORT_PATTERNS 26→27 合法更新（`--tag` 隔离新增模式；off 臂为既有模式不增量；M0-T2 先例）④ Makefile 注释 44→73 条 gold 顺带修复（证据：产物 queries.n=73）⑤ 仅 bm25 对照（Milvus 未运行，fuse 臂未跑）；停批条件不触发（off 臂豁免外 0 差异）；下一卡 M2-T3（降级证明测试固化） |
| M2-T3 | COMPLETE | `08cc9a9`（开工 sha） | `pytest tests/test_concept_degradation.py`（9 例）；变异检验（临时脚本：真实索引注入比对）；`make test`；`make lint`；`mypy`；ruff check/format | `tests/test_concept_degradation.py`（新建，9 例：3 前置 + 三情形 × 2） | 9 passed；三情形 loader None 契约：retail 无映射 / 空 ontology_dir（等价删索引文件）/ `ATLAS_CONCEPT_LAYER=off`；逐字比对（SchemaLinkResult 全字段 ==）：3 问句 × 三情形全一致；前置防混淆：默认 arm 29 概念可加载、问句集 top1 锁定（total_trade_value / commission_revenue / cash_balance）、基线确定性；**变异检验：真实索引注入下 2/3 问句输出 DIFFERENT——逐字比对具捕获能力（非永真断言）**；`make test` 1366 例 OK（1357+9）；`make lint` 全绿；mypy 91/17 与基线一致（tests 不在 mypy files 面） | 裁定 1 处并登记（**planner 接口缺口**）：计划文本含 "retail planner 与 schema_linker"，但 planner 的 `concept_index` 注入口属 M3 交付（实证：`Planner.__init__` 无该参数、planner.py 全文 `grep concept` 零命中）→ 本卡收敛至 M2 已接入消费缝（schema_linker），planner 降级证明在同文件随 M3 注入落地时按同法补齐（测试 docstring 已声明覆盖范围）；**M2 三卡（T1/T2/T3）全部收口 → M2 里程碑完成，待 H4 人工验收**；M3-T1 被 H2 人工门禁阻塞（OOV 标注 + dev/holdout 划分——Agent 只出候选不代签） |

**总证伪条件**（同 ADR-0032 推翻条件）：解析缝误解析率超阈值 → 关解析缝；Recall@K 无提升 → 检索缝回退；curation 挤压主线 → 冻结索引与别名；off/未注入出现任何非元数据差异 → 立即停止推进并回查旁路不变量。

---

## 8. 人工作业清单（不可代理，瓶颈资源）

| 项 | 卡 | Agent 的边界 |
|---|---|---|
| 首批 S3 概念级别名（可溯源、只碰 dev 证据） | M1-T2（H1） | 只出候选清单与校验，不代签 |
| OOV 样本标注 + dev/holdout 划分 | M3-T1（H2） | 只起草候选，人工确认后入库；holdout 此后禁改 |
| 新增命中抽检（正确/误解析判定） | M3-T4（H3） | 只出抽样清单与比对表 |
| 里程碑验收评审（M1–M5 各一次） | 各卡（H4） | 只汇总证据账本 |
| 解禁裁定（误解析率阈值、回退决策） | M3-T4 | 只登记 ADR，不拍板 |
| frontend-redesign Phase 1 分支收尾与合并裁定（含未提交变更处置） | M5 前置（H5） | 只列变更清单与选项，不代签 |
| 前端布局与概念链视觉验收（截图确认） | M5（H4 覆盖） | 只产出截图清单与对比表 |
