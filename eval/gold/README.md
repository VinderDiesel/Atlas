# 自建黄金集（Gold Set）

**这是 Atlas 的主评测集**，与 Spider / BIRD 等公开集分开报告，禁止混报或外推。

## 目录结构与编号约定（2026-09-05 目录化）

| 目录 | 编号段 | 场景 | 语义模型 | 状态 |
|---|---|---|---|---|
| `finance/` | gold-101 ~ 179（实测） | **金融分析（主场景）**：TPC-DI + FIBO 概念标注（ADR-0006/0007） | `atlas_finance_analytics` | **79 份**（中文 71 + 英文 8）；66 条已锚定、13 条待锚定（2026-09-14 实测，分类见下文「数量」节）。2026-09-05 双语批次时为 70 条全锚定（62 中文 + 8 英文，快照 1e5d35b）——历史陈述不改写 |
| `retail/` | gold-001 ~ 078（实测） | 零售分析（TPC-DS SF0.1，2026-09-04 转正为第二主评测域） | `atlas_retail_analytics` | **27 份**（中文 22 + 英文 5）；18 条已锚定、9 条待锚定（2026-09-14 实测）。2026-09-05 双语批次时为 19 条全锚定（14 中文含 1 歧义 + 5 英文，快照 1e5d35b）——历史陈述不改写 |
| `paraphrase/` | pp-*（实测 13 份，**不占 gold- 编号段**） | 同义改写鲁棒性（仅 Planner 层，不执行 SQL，`eval/paraphrase_eval.py`） | 固定 `atlas_finance`（`paraphrase_eval.py:43` 常量；`--domain` 只写入报告不参与选模型，见下文注） | **不属 gold schema**：实测 13/13 键集为 `base` `expected_dimensions` `expected_metric` `expected_time` `id` `intended_metric` `note` `question`，缺必填的 `expected_sql`/`result_hash`/`snapshot_sha`；**不计入 106**，不被 `validate_gold.py` 覆盖（见下文「校验」节） |

目录即域声明：runner 按目录装载对应语义模型，报告分域不混报（AGENTS.md N10）；
样本 id 全局唯一（金融 gold-1xx、零售 gold-0xx），git mv 迁移保留 id，历史报告
（eval/reports/*.json）按 id 可追溯。

> **2026-09-05 裁定推翻记录**：早期目录表零售行文「gold-001 ~ 050 … 历史对照，
> 不再新增——转正裁定待数据装载批次」基于无数据前提；2026-09-04 TPC-DS SF0.1
> 装载完成（`make seed-retail`，4 表入 dwd，全库 29 表双源快照）后原裁定理由消失，
> 零售段转正为第二主评测域（行文随 92033c9 替换为“第二主评测域”，本注记作审计锚——
> 沿 ADR 推翻条件记录流程，ADR 均含推翻条件，见 docs/release-notes-v0.1.md）。
> 推翻后当批零售段样本已全量锚定（该批次 19 条，双语分节），终验报告 `eval/reports/9749fc5.json`；
> 当前计数以下文「数量」节为准（本注记为 2026-09-05 历史审计锚，不改写）。

语言为样本属性（tags 含 `lang_en` → 英文，缺省中文），不是目录维度：
报告按域再按语言分节（per-domain by_lang，zh/en 计数分开不混报）——
双语是同一域的两种问法，不构成第三域（N10 口径：分域是语义模型边界）。

金融段问句时间使用**绝对年份**（2012-07-07~2017-07-07，TPC-DI 官方生成器 PDGF 默认 Batch1 数据段，已实测），
不使用相对时间（“上月/最近”）——相对时间在固定快照评测下会漂移。

> 2026-09-02 修正：原描述 2004-07~2006-07 为计划口径，实测数据段为 2012-07-07 起（gold-101~103 时间已同步改为 2013 口径）。

零售段问句时间同为**绝对年份/季/月**（1998-01-02~2003-01-02 销售窗口，TPC-DS SF0.1
dsgen 官方数据，已实测——2003 年仅 37 订单，样本时间全部落在 1998~2002）。
中文问句中的英文州/城市码（如 “CA 州”）是预期形态：TPC-DS 值为英文缩写，无值翻译层（诚实边界）。

## 设计原则

1. **人工标注**，不是自动生成
2. **结果可复现**：每条样本绑定 `snapshot_sha`，`result_hash` 在固定快照下稳定
3. **覆盖四类能力**：聚合、维度筛选、时间范围、排序/对比
   （2026-09-03 起 filter 专项样本补维度值/排除/度量阈值三形态，见 149-155）
4. **包含故意设计的歧义问句**（`ambiguous: true`），用于测试 Agent 的澄清机制
   —— 正确行为是**反问**，不是猜一个答案
5. **FIBO 概念标注**（ADR-0007）：`fibo_concepts` 人工标注问句术语 → 概念 IRI，
   是“业务术语 → FIBO 概念 → 语义字段 → SQL”可解释链的评测载体

## 数量

- 原目标：50 条（含约 5 条歧义问句），已于 2026-09-02 达成；此后按评测需要持续扩张，
  **不再设固定条数目标**（对外条数一律由脚本实测得出，AGENTS.md N1）。主域为金融段
  （实测编号段 gold-101~179），零售为第二主评测域（gold-001~078，2026-09-04 转正，
  见上文裁定推翻记录）
- 当前（**2026-09-14 逐文件实测**，判据脚本与五类定义见 ADR-0023 决策 ④）：**106 条**
  （金融 79 = 中文 71 + 英文 8；零售 27 = 中文 22 + 英文 5），歧义 9 条（金融
  gold-104/121/122/148/155/179，零售 gold-047/068/078）。按锚定状态分三类：
  ① **已锚定 84**（金融 66 + 零售 18，具备真值 `result_hash` + 实测 `snapshot_sha`）；
  ② **非歧义待锚定 13**（金融 gold-172~178、零售 gold-072~077）——P-1 批次可 EX 锚定
  （ADR-0017 判据 4）；③ **歧义样本 9**——走澄清、无结果行，设计上永不产生
  `result_hash`（其中 7 条已按既有约定写 `result_hash: null` + 实测 sha，2 条
  gold-179/gold-078 仍是双占位符且偏离该约定，待修，见 ADR-0023 代价 ④）。
  另：`eval/gold/paraphrase/pp-*.json` 共 13 条 paraphrase 改写样本**不属 gold schema**
  （缺 `expected_sql`/`result_hash`/`snapshot_sha`），不计入 106，亦不被
  `validate_gold.py` 的 glob 覆盖（结构必需，非缺陷），见 README KL #31
- 历史（不改写）：2026-09-05 P7 收口时为 89 条（零售 19 = 中文 14 + 英文 5、金融 70 = 中文 62 + 英文 8；歧义 6 条：零售 gold-047，金融 gold-104/121/122/148/155）——50 条目标已达成后按评测需要扩张（2026-09-03 filter 能力批次、2026-09-04 派生指标补洞批次、2026-09-04 零售段转正批次、2026-09-05 双语样本批次、2026-09-05 P7 收口批次）；2026-09-05 起样本按域分目录存储（本 README「目录结构与编号约定」节）
- 2026-09-02：金融段 101~148 已全部经 Doris 实测锚定（快照 `b47a6c1`，Plan Acc 44/44、EX 44/44、反问 4/4，见 `eval/reports/b47a6c1.json`）；两批新增共 41 条普通样本覆盖全部 15 个指标与年/季/月/日四粒度（含 month 粒度回归用例 gold-115、数据段尾边界 gold-126/132、快照日+维度分组 gold-142/143、双维度分组 gold-146），歧义 3 条（相对时间 gold-121、双指标 gold-122/148）
- 2026-09-03：filter 解析能力批次（ADR-0014 ①，评测先行）新增 gold-149~155 共 7 条——6 条可解析（维度等值 149、排除 150、度量阈值上界 151/152、维度等值+分组 153、阈值下界 154）+ 1 条歧义（155「核心分支」过滤值模糊 → 反问验证）；样本 `result_hash` 以 null 提交，随 filter 实现批次实测锚定回填；实现前 dry 基线 = Plan Acc 44/50、反问 4/5（旧样本零回归，记录于实现批次 commit）
- 2026-09-04：派生指标补洞批次（KL #16/#17，评测先行）新增 gold-156~162 共 7 条——覆盖 Day 27 五个派生指标（平均每笔成交金额 156/157、平均每笔佣金 158、佣金率 159/160、户均持仓市值 161——fact_holdings 域首个黄金样本、户均现金余额 162——负值口径锚定），含分支分组 + TopN 组合 2 条（157/160）；**评测先行发现真实缺陷**：派生指标同义词含基础指标词（“平均每笔成交金额” ⊃ “成交金额”、“佣金率” ⊃ “佣金”）导致 Planner 对全部 7 条系统性误报口径歧义（实现前 dry 基线 = Plan Acc 50/57），按**最长命中**消歧修复（互不为子串的双指标问句 gold-122/148 仍反问）后 dry = Plan Acc 57/57、反问 5/5；样本 `result_hash` 已实测锚定回填（Plan Acc 57/57、EX 57/57、0 执行错误，快照 `30b8344`，报告 `eval/reports/30b8344.json`）
- 2026-09-04：**零售段转正批次（TPC-DS SF0.1 第二域）**：gold-001 大修（原为金融问句误拷贝——region/last_month/gmv 全错，改为“总销售额是多少？” total_sales_price 无时间口径）保留；gold-047 保留原问句（“最近卖得怎么样？”相对时间+口语歧义 → 反问，跨域形态保留）并修正 clarification 文案为零售口径（销售额/销量/净利润三口径 + 1998~2002 绝对年份 + 品类/门店维度）；新增 gold-051~062 共 12 条中文样本（覆盖 5 指标 × 年粒度、品类/门店城市分组、TopN 3、城市等值 filter（值 Midway 来自实测值域）、品类阈值 HAVING filter（900 万 = 实测单品类年最高量级机械转述）、1999Q1 quarter 复合时间、2000-05 month 复合时间）；样本 `result_hash` 以 null、`snapshot_sha` 占位提交（随 P5 实测锚定回填：绑定快照 `92033c9`——同数据多锁，指纹与 P2b 锁的 `dc4f350` 全一致，见 data/snapshots/README.md 演进）；实现前 dry 基线 = Plan Acc 13/13、反问 1/1（**0 红点**——样本标注与实现无洞，如实记录，与历史“评测先行发现缺陷”批次不同）；契约测试补零售问句解析 11 例（tests/test_planner.py TestRetailGoldPlanner）；注：州（s_state）维度因 SF0.1 全库单州（TN）无区分度，分组/过滤样本降级为门店城市（s_city），探查见 `retail/data-profile.md`
- 2026-09-05：**双语样本批次（P6 planner 英文全形态 locale 化）**：新增英文样本 13 条（金融 gold-163~170 共 8 条 + 零售 gold-063~067 共 5 条），全部为既有中文锚定样本的机械直译（同 Plan → 同 SQL → 同 result_hash 可预期），值域机械转述既有样本；覆盖英文全形态：年份介词 in/for（163/168）、quarter 语序两向 Q2 2013 / 2013 Q2（164/165）、月份名 May 2014（166）、ISO date（167）、by 分组 + top N 后短语提维度（168/065）、only for X 过滤（169/067）、over 阈值 + million 量级 + TopN 组合（170）、类别/城市过滤零售侧（064/067）；语言 = 样本属性（tags `lang_en`，缺省中文），runner 按 tags 显式传 locale（评测确定性优先，不依赖自动检测），报告 by_lang 分节；契约测试 TestEnglishPlanner 24 例 + 样本对照 TestEnglishGoldPlanner 13 例（tests/test_planner.py）；实现前 dry 基线 = finance zh 62/en 8、retail zh 14/en 5 全绿（**0 红点**——契约测试先行充分，样本标注无洞，如实记录）；实测锚定绑定快照 `1e5d35b`（同数据多锁，指纹与 92033c9 全一致，见 data/snapshots/README.md 演进），报告 `eval/reports/1e5d35b.json`（finance：zh 57/57 + 反问 5/5、en 8/8；retail：zh 13/13 + 反问 1/1、en 5/5；EX 全比对通过、0 执行错误）；注：零售 gold-067 原计划「only CA」（州过滤）因 SF0.1 实测全库单州（TN）无 CA 州，降级为城市过滤 only for city Midway（与 P4 gold-059 州降级城市同因同值，探查见 `retail/data-profile.md`）
- 2026-09-05：**P7 收口批次（API 多模型路由 + demo + 终验）**：serving /plan /compile /ask 请求体 `model` 字段（finance|retail，缺省 finance 向后兼容，serving/api.py DOMAIN_MODEL_PATHS 与 runner 同路径）；demo 集成测试 14 例（tests/test_demo_e2e.py = README 快速开始的可执行版：中英问句 12 条全链路双域各一 DataAgent + 带身份 2 条同 rls-verify 机制）；终验复验轮零回归（报告 `eval/reports/9749fc5.json`：finance zh 57/57 + 反问 5/5、en 8/8；retail zh 13/13 + 反问 1/1、en 5/5；EX 复验 65/65 + 18/18 全比对、0 执行错误——89 条全绿，同数据多锁 9749fc5，指纹与 1e5d35b 全一致，见 data/snapshots/README.md 演进；EVAL_REPORT.md 自本批起 per-domain 分节绑定此 sha）

## 校验

```bash
# schema 结构 + FIBO 概念标注 IRI 存在性（标注不得引用失效概念）
.venv/bin/python eval/gold/validate_gold.py
```

注（2026-09-14 实测）：`validate_gold.py` 的 glob 为 `<domain>/gold-*.json`，**13 份
`pp-*.json` 改写样本不在其覆盖范围内**——这是结构必需而非缺陷：实测 13/13 均不符合
`schema.json`（`expected_sql` 为必填），并入则 `make lint` 当场 13 红。但该脚本 docstring
称「验证 `eval/gold/<domain>/*.json` 全部符合 schema」与 glob 不一致，属待修文档债务；
paraphrase 侧真正的限制（无结构门槛、`.get()` 静默降级）见 README KL #31。

## 防泄漏要求

**测试集样本不得进入训练集。** 切分方式按问句模板维度切分，
而非随机切分（避免同一模板的不同问句同时出现在训练和测试集，造成虚高）。

切分方式必须能在评审中讲清楚。
