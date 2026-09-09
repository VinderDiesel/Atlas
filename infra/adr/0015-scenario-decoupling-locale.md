# ADR-0015：场景适配解耦——locale 词典落 `semantic/synonyms/` 与幽灵定义处置

- 日期：2026-09-08
- 状态：accepted（决策经用户确认，随 P0 解耦批次 B2/B3 落地）
- 相关：ADR-0002（Ossie 为语义规范）、ADR-0014 ①（filter 解析）、ADR-0010（评测先行）、
  `semantic/synonyms/en_us.yml`、`semantic/_legacy/README.md`、`agent/compiler.py::load_locale_synonyms`

---

## 背景

目标（推广与落地时反复被问到的那句话）：**换一个业务场景，应该改配置，而不是改内核代码。**

实测耦合度地图（HEAD=305a771 核实）给出的答案是"一半一半"：

| 场景相关内容 | 当前位置 | 换场景要不要改代码 |
|---|---|---|
| 中文指标/维度同义词 | `semantic/ossie/*.ossie.yaml` 的 `ai_context.synonyms` | 否（改 YAML） |
| **英文同义词** | `agent/planner.py` 代码常量（17 指标 + 15 维度字段） | **是** |
| **形态触发词**（分组结构词、filter 触发词、量级词、TopN、指代链接词、相对时间拒答词） | `agent/planner.py` 字面量正则 | **是** |
| 行级权限/脱敏 | `semantic/policies/row_policy.yml`（Jinja 模板） | 否 |

同一批实测还发现第二类问题：ADR-0002 之前的自研 DSL 定义并未真正退场——
`semantic/metrics/gmv.yml` 仍标 `status: active`，引用的 `dwd.fact_order_line` /
`dwd.dim_order` / `dwd.dim_region` 在任何已锁快照里都不存在（当前锁定基准
`b933e20`：29 表 = dwd 12 + tpcdi 17；零售装载前的历史快照 `7d48dcb`：25 表），
`gold/gmv_by_region_month.yml` 也不存在；`semantic/lint.py` 不覆盖这些目录，
全仓零代码引用。幽灵定义不会报错，但会让读者与新工具误以为口径仍由它定义。

约束：
1. 确定性优先（AGENTS.md N1）——外置只是把规则从代码搬到数据，不得引入猜测；
2. 权威源唯一（ADR-0002）——同一口径不允许有两个可写来源；
3. 零行为变化（B2/B3 是纯搬运批，任何"顺手优化解析"都必须另批评审）。

## 备选方案（英文同义词的落点，三选一改后否掉两个）

**A. 写进 ossie 的 `ai_context.synonyms`（与中文注记混排）** — 否。
实测解析器按 locale **分轨**使用同义词：`zh` 轨 = 模型注记（全中文，唯一例外
`GMV`/`AOV` 中英同形）；`en` 轨 = 模型注记 ∪ 英文表。zh 轨从不并入英文表——
中文问句里出现的英文片段（分支代码 `IEMJHuQgCPDHCwwJkgQQeaqGvzMcVD`、证券代码等）
会参与指标/维度匹配，混排直接破 `locale=zh` 的防误命中隔离（tests/test_planner.py
的双语隔离用例即为此而存在）。此外口径注记与解析器形态层混在一处，也违反
"模型是业务资产、措辞形态是解析器实现"的分层。

**B. 写进治理扩展 `custom_extensions`（ATLAS vendor data）** — 否。
`semantic/governance/atlas_governance.schema.json` 顶层 `additionalProperties: false`
且只登记 7 个扩展键（governance / lineage / freshness / policy / quality /
fibo_alignment / time_dimension）。同义词不是治理属性，为塞它而扩 schema = 污染
治理语义；改 schema 又要牵动 `governance_validate.py` 与全部治理契约测试，代价与
收益完全不对称。

**C. 搬到 `agent/prompts/en_synonyms.yaml` 或新建 `agent/lexicon/*.py`** — 否。
仍是代码目录，"改配置"的目标没达成，只是把字面量从 A 文件挪到 B 文件。

**D.（选定）`semantic/synonyms/<locale>.yml`** — AGENTS.md §4 早已把 `synonyms/`
声明为"同义词表"职责（该目录当时唯一的 `business_terms.yml` 是 ADR-0002 前的幽灵
定义、全仓零引用，随幽灵处置一并归档），落此目录零新增约定。

## 裁定

### ① locale 词典目录形态与加载契约

- `semantic/synonyms/en_us.yml`：`metric_synonyms: {<指标名>: [措辞, ...]}` +
  `dimension_synonyms: {<字段名>: [措辞, ...]}`，两节均**可缺省**（视为空表）；
- `semantic/synonyms/zh_cn.yml`：**当前为空占位**。中文同义词是口径定义的一部分，
  权威源是模型 `ai_context.synonyms`，抄进词典即制造双权威源（违反 ADR-0002）。
  中文的**形态触发词**不属口径定义，它们落在独立的 `patterns_zh_cn.yml`（§②）——
  措辞层与形态层分文件，前者可被模型注记覆盖、后者只能被词典定义；
- 加载入口 `agent/compiler.py::load_locale_synonyms(locale)`（与模型注记同一入口）：
  locale 为**封闭注册表**（未注册名直接报错，不拼接文件名）；顶层键白名单；值必须
  是"名称 → 字符串列表"；空措辞/重复措辞报错。**已注册但文件缺失 = 报错，不静默
  返回空表**——静默降级会让解析器在无人察觉时丢措辞、出口径；
- **声明顺序即语义**：合并语义逐字保持原实现（`en = 模型注记 + 表内顺序`追加；
  `zh = 模型注记`）。词典顺序影响最长命中集合，YAML 头注释已写明"调整顺序需连同评测复验"。

### ② 形态触发词词典（B3a 中文已落地 / B3b 英文待落）

planner 内的形态字面量外置为 `semantic/synonyms/patterns_<locale>.yml`，按**能力
分节**，每节是正则模式串或词表；解析顺序 = 声明顺序（结构即顺序，消除“变量名
暗示顺序”的隐性依赖）。

**实际落的节名与本节初稿的差异（B3a 实现时定形，已回写本 ADR）**：初稿列的是八
个平级节（含 `relative_time_reject`），实现时改为**七个平级节 + `time` 内两子节**：

| 节 | 内容 | 为什么这样分 |
|---|---|---|
| `time.relative_reject.words` | 相对时间词表（命中即反问） | 拒答与解析是**同一个解析阶段**的两面（`_parse_time`），拆成平级节会让一能力两节 |
| `time.patterns[]` | 有序 `[{kind, pattern}]` | kind 显式化才能把“顺序”与“构造口径”分开锁定（见下） |
| `grouping` / `topn` / `filter_include` / `filter_exclude` | 单 `pattern` | 与初稿一致 |
| `threshold.greater` / `threshold.less` | 各含单 `pattern` | 上下界同节，避免两个平级节可缺其一 |
| `magnitude.cn_units` / `cn_numerals` | 词→整数倍率 映射 | 与初稿一致（量级不参与匹配顺序，只做换算） |
| `followup.prefixes` / `dim_pattern` | 词表 + 单 `pattern` | 与初稿一致 |

加载入口 `agent/compiler.py::load_locale_patterns(locale)`：locale 封闭注册表；
**缺节/多节/正则不可编译/词表含空词或重复/倍率非整数 → 一律报错**（静默降级等于
某类问句在无人察觉时不再被解析）。kind 一致性在 planner 侧锁：
`{词典 kind} == set(_TIME_DISPATCH)`，不等即导入失败——新形态必须“词典 + 分发表”
同时落，不允许单边。

约与初稿不变的两条硬约束：（a）**纯搬运**——模式串整串复制，不在代码里用词表
重组正则；（b）planner 常量的**名与类型不变、值来自词典**，使使用点零改动，
等价性可用对象同一性断言证明（`tests/test_locale_patterns.py`）。正则等价性
对照表见 `docs/design/adr-0015-pattern-lexicon-zh.md`。

### ③ 幽灵语义定义：归档不删除，并由 lint 锁死

`semantic/_legacy/`（`models/ metrics/ dimensions/ synonyms/ schema/`）集中存放
ADR-0002 前的自研 DSL 定义与其 JSON Schema，README 记录"为什么归档而不是删除"的
实测依据。`semantic/lint.py` 新增第 4 项 `[authority]` 检查：`semantic/` 下除
`ossie/`、`synonyms/`、`policies/`（及 `_*` 前缀归档区）外不得存在 `*.yaml/*.yml`。
新增权威目录必须先补 ADR——把"目录职责"从口头约定变成 CI 断言。

## 理由

关键权衡是**"配置可改"与"权威源唯一"的冲突怎么解**：把英文措辞塞进模型 YAML 能
少一个加载入口，但会破 locale 隔离并让口径资产混入实现细节；留在代码里则第三域
接入必然要改 planner。落到 `semantic/synonyms/` 是唯一同时满足两条约束的位置，
代价是多一个 YAML→tuple 的加载器与一组防漂移契约测试。

幽灵定义选择归档而非删除：删除会丢掉"我们为什么走 Ossie"的实物对照（AGENTS.md
§变更流程要求语义层变更留痕），而归档 + lint 锁定既终止其"看起来还活着"的误导，
又保留演进证据。

## 代价与限制

1. 词典与模型的一致性靠**契约测试**而非 schema：`tests/test_planner.py` 锁
   条数基线（17 指标 / 15 维度）、键必须在金融 ∪ 零售模型已注册名字集内、
   措辞不得与模型注记重叠（唯一例外中英同形的 `AOV`）。跨域词典（单文件服务两域）
   因此无法用单模型 schema 校验；
2. 顺序敏感是真实负担：词典是纯 YAML 列表，无法像 Python 元组那样在评审里直观
   看出"谁先匹配"，靠注释与评测复验兜底；
3. `zh_cn.yml` 目前是空文件，读者可能误以为中文同义词配置失效——README 式头注释
   已写明原因，但这是"结构先行于内容"的固有代价；
4. 重新评估触发条件：若出现**同一措辞在 zh/en 需要不同口径**的真实场景（当前不
   存在），分轨假设即被证伪，需回到 ADR 重开方案 A。

## 验证方式

- `make lint`（含 `[authority]`）全绿；
- `make test` 全绿（B2 后 481 例，含词典加载器与防漂移契约测试；B3a 后 502 例，
  新增 `tests/test_locale_patterns.py`，**既有中/英 planner 用例断言一字未改**）；
- `python -m eval.runner --dry` 双域 summary 与 b933e20 口径一致（**实测**：
  finance `plan_acc 65/65`、`clarify 5/5`（zh 57/57 + 5/5，en 8/8）、
  retail `18/18`、`1/1`（zh 13/13 + 1/1，en 5/5）、`exec_errors 0`；非 EX 维度
  逐域逐语言与 `eval/reports/b933e20.json` **完全相等**，EX 列 dry 下不执行故不比对）；
- 能证伪本决策的数据：任一同义词用例断言变化，或 dry summary 与 b933e20 出现非 EX
  维度差异——都说明搬运不是等价的，本批必须回退重做。
