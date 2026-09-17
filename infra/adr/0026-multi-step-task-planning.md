# ADR-0026：多步任务规划（Text-to-Insight 阶段二入口——有界 Plan 序列与确定性变化贡献）

- 日期：2026-09-16
- 状态：proposed（决策待用户确认；本文完成的是分析与开发任务拆解，**不是功能实现回执**）
- 范围：一个 Metric、一个 Dimension、两个绝对期间的确定性贡献分解；复用单轮查询链，不推断业务因果。
- 相关：ADR-0003（Guard）、0009（无环图）、0010（评测）、0014（过滤/追问/绝对时间）、
  0015（形态词典）、0016（值域）、0017（时间智能）、0019（快照）、0020（会话）、
  0021（权限事实源）、0022（HTTP 契约）、0025（图表）；愿景 `docs/text2insight.md` §3.2。
- 阅读顺序：背景证据 → 决策 ①～⑥ → 开发任务 T01～T11 → 验收映射。

## 背景与现状证据

### 1. 增量是跨结果集的变化贡献，不是重新实现同比，也不是因果推断

现有 `Planner.plan` 产出单个 `Plan | ClarificationRequest`，`DataAgent.ask/run_plan` 每次执行一个回合。
`ComparisonSpec` 已定义 yoy/pop/cumulative/rank；ADR-0017 已裁定时间智能，ADR-0025 已裁定图表接入，
因此不能声称“阶段二零覆盖、四项从未裁定”。已有比较编译分支也不等于所有方言、粒度均已通过真库验收。

本 ADR 的增量是：按固定模板取得两期整体与分组结果，计算各组对整体变化的贡献，并展示计算证据。
“某分支的变化贡献”不等于“缺货导致下降”；后者需要额外业务证据或因果方法，本 ADR 不提供。
单条 SQL 在表达能力上可以实现贡献计算，缺的是**当前 Plan 的表达与跨结果综合契约**，不是 SQL 理论限制。

### 2. 核查结论（2026-09-16；源码位置以符号为主，行号为本次定位）

| 证据 | 当前事实 | 对任务拆解的约束 |
|---|---|---|
| `agent/graph.py:219` `node_plan`；`:432` `node_explain` | 每次图调用推进 `turns`，成功后写 `last_plan` | 不能在父 thread 连调四次后再“修回轮数” |
| `agent/graph.py:682` `_invoke_turn`；`:758` `run_plan` | 缺省 session_id 只是随机生成 ID，仍调用带 checkpointer 的图 | “一次性 ID = 不落 checkpoint”不成立；需要真正无状态的子执行模式 |
| `agent/graph.py:160` `build_graph` | `checkpointer=None` 创建 MemorySaver，不表示关闭存储 | 保持该默认契约，另设显式无状态构建选项 |
| `agent/graph.py:371` `node_execute` | 编译、逐 SQL 权限解析、Guard、执行、结果形态检查在同一实现中 | 复用该实现，不在 HTTP/分析模块另写 SQL 执行链 |
| `agent/planner.py:617` `_parse_time` / `_parse_time_en` | 命中首个时间形态即返回 | 双时间及其基线/当期角色需要新增解析，不能把整句直接交给现有解析器 |
| `agent/compiler.py:91` `Plan`；`:714` / `:775` / `:793` | 默认 LIMIT；度量过滤走 HAVING；排序需显式指定 | 分组完整性、统一 WHERE、禁止分析前 TopN 必须另行约束 |
| `agent/compiler.py:125` `Relationship` | 对象含连接列，不含可证明互斥穷尽的基数契约 | 不能仅凭存在 JOIN 路径认定指标可加 |
| `semantic/ossie/atlas_finance.ossie.yaml:1211` / `:1682` / `:1713` | 佣金收入是 SUM，笔均佣金和佣金率是比值 | 不能对所有 Metric 使用同一种贡献公式 |
| `agent/tools/execution_validator.py:54` | 空结果、全 NULL、多行等只产生 issues；图仍可返回 answer | answer 不等于可综合，必须检查 rows 与 issues 的具体含义 |
| `serving/api.py:279` `_turn_payload` | HEAD 提交为 21 键；复核当日工作区已有未提交 chart 接线（观测 22 键，含 chart），analysis 未出现 | 按落地当日实际字段集合扩展并断言集合，不沿用草稿的旧键数，也不回退他人 chart 变更 |
| `tests/test_api_contract_v2.py:108` `EXPLANATION_KEYS` | 带身份时含 policy_effect；无身份时无此条件键 | 13 个固定解释键与身份下的 14 键不可混淆 |
| `eval/gold/schema.json:6`；`eval/runner.py:110` `plan_acc` | 当前 gold/runner 面向单 Plan、单结果 hash | 添加分析字段不会自动获得分析评测；须独立 schema 与 runner |
| `Makefile:204` / `:211`；`eval/e2e_acceptance.py:319` | e2e 写固定报告路径；已有 S1～S7；eval 有首次回填 hash 行为 | 不预写通过数量、不用被测分析器自动生成期望、不以进程成功代替报告验收 |

本次无 DB 内存探针确认：`run_plan` 不传 session_id 后，checkpoint 仍含 `turns=1` 和 `last_plan: Plan`；
`_turn_payload` 在 HEAD 为 **21 键**（复核当日工作区未提交 chart 变更后观测 **22 键**），无身份 explanation 为 **13 键**。
另用 `update_state(..., as_node="explain")` 写入原始状态，观察到 `next=()` 且执行器未被调用；
这仅验证候选状态写入机制，**不证明多步会话已实现**，SQLite、并发和中断语义仍须 T06 验证。
键数可用下列只读命令复算，测试应断言集合而不只是数量：

```bash
.venv/bin/python - <<'PY'
from agent.state import TurnResult
from serving.api import _turn_payload
keys = set(_turn_payload(TurnResult(kind="clarify", session_id="probe", question="probe")))
print(len(keys), sorted(keys))
PY
```

核查期间 `agent/compiler.py` 与 `tests/test_compiler.py` 有其它未提交工作。
本 ADR 不修改或回退它们，也不把在途修复当成本能力的已验证前提。

## 备选方案

### 分解策略

| 方案 | 收益 | 代价与裁定 |
|---|---|---|
| 确定性模板（选定） | 有界、可复现，每步仍为标准 Plan | 仅支持明确注册的形态和指标/维度组合 |
| LLM 自由规划 | 表达灵活 | 不符合本功能的确定性默认、成本与验收边界；不采用。LLM 本身并非被项目全面禁止 |
| LLM 候选 AnalysisPlan + 校验 | 与既有 candidate 思路兼容 | 另立 ADR 后评估；本批不加无实现的 allow_candidate 参数 |

### 编排落点

| 方案 | 收益 | 代价与裁定 |
|---|---|---|
| 单轮图加环 | 一张图承载迭代 | 改变 ADR-0009 拓扑及状态语义；本需求没有结果驱动迭代的必要性，不选 |
| 图外编排、复用同一图定义的无状态实例（选定） | 保持节点与边集合，复用唯一执行实现 | 必须新增父回合状态写入、子调用隔离、聚合与观测契约；**不是零代码改动** |
| 第二套分析专用图 | 可独立演进 | 图本身不违反 N3，但若复制 execute 就会产生安全漂移；本批无收益，不选 |
| 扩展单 Plan 为贡献 SQL | 减少往返 | 要扩展查询抽象并验证 JOIN/方言/Guard；当前以复用既有 Plan 为优先，保留重审条件 |

## 决策

### ① AnalysisPlan：完整解析后生成固定四步，先校验再执行

`AnalysisPlanner.plan(question) -> AnalysisPlan | ClarificationRequest | None`：

- `None` 仅表示**没有分析意图**，交回普通 ask；命中“为什么/归因/变化贡献/why/contribution”等
  分析意图后，缺槽或不支持必须澄清，绝不能回落为只答一个数。
- Metric、Dimension、WHERE Filter 复用现有语义模型、同义词与值域机制；形态词、连接词放
  `semantic/synonyms/patterns_{zh_cn,en_us}.yml`。不在 Python 内硬编码业务措辞。
- 要求一个指标、一个明确分组维度、两个绝对时间，显式确定 baseline/current；同粒度、完整期间，
  baseline 早于 current。支持 year/quarter/month/date 的两个完整期间，不支持任意范围或省略年份。
  相对时间报 `relative_time`；缺指标/维度/期间、多个指标/维度或时间角色不清报 `ambiguous`。
- 分析比较使用 `comparison=None` 的四个普通 Plan，不依赖 yoy/pop 的窗口实现。
  两期非时间过滤完全相同；只接受可解析的 WHERE 过滤，拒绝 HAVING、TopN、排名与不同期不同过滤。
  未消费的业务条件不能静默丢弃。

固定角色顺序：`baseline_total, current_total, current_by_dimension, baseline_by_dimension`。
两个总量 Plan 不分组、`limit=1`；两个分组 Plan 按 D 升序，`limit=L`，
`L=min(10000, budget.max_rows)`；全部带绝对时间与相同过滤。
`MAX_SUB_PLANS=4` 是该模板的设计上限，不是容量实测值，不预留无用途的第五、六步。

示例输入（不预言实际涨跌）：
“分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献”；
“Analyze commission revenue in 2013Q4 compared with 2013Q3 by branch”。
若问“为什么下降”，还须在综合时核实整体确实下降。

### ② 可加资格、结果完整性与一致性是综合前置条件

资格只来自指标自己的 Ossie ATLAS 扩展，拟新增：

```json
{"analysis": {"attribution": {"dimensions": ["Branch"]}}}
```

这是**允许分析的指标/维度对**，不是指标表达式的第二份定义。
`semantic/governance/atlas_governance.schema.json` 增此闭合结构，校验维度存在、非时间字段、无重复；
`SemanticModel` 加载为 `attribution_dimensions: dict[str, tuple[str, ...]]`，缺省为空。
初始登记 `commission_revenue`、`total_trade_value` 对 `Branch`，不声称全指标覆盖；
新增组合必须通过同一资格验证，不得靠“分组结果刚好对上”自动扩大白名单。

资格验证要求：指标是期间内可加的事件型 SUM（可含行内乘法）或非 DISTINCT COUNT；
不是比率、均值、DISTINCT、期末余额、窗口结果或跨事实表表达式。
DISTINCT 在某些分区可以可加，但本批不证明该条件，明确拒绝。
额外分组 JOIN 相对总量不得丢事实行或放大行数，须在锁定快照验证连接键唯一性及引用覆盖，
并覆盖时间、过滤和权限追加的 JOIN 路径。不能仅看 Relationship 的连接列。

验证证据写入拟新增的 `data/snapshots/<snapshot_sha>.analysis.json`，绑定模型名、语义文件 SHA256、
指标/维度对、实际 JOIN 路径及各检查结果；失败不产生“合格”证书。运行时只消费匹配证据；
缺失、过期或不合格 → 分析不可用，不影响普通 ask。该文件是机器验证产物，不是新权威语义定义。

四步使用同一模型实例、同一快照绑定、同一身份及同一预算，不在步骤间重新解析最新快照。
**相同 snapshot_sha 不证明数据库读隔离**：MVP 只支持验证后保持不变的锁定数据；部署期间禁止重载，
评测前后复核指纹。可变数据上的一致读需另行设计事务/版本读取，不能宣称本批支持。

结果检查与可加资格是两层独立门禁：

- 按 columns 定位指标与分组键，不依赖列位置猜测；总量必须恰好一行有效数值。
- 分组行数触及有效 LIMIT（包括刚好等于 L）→ `possible_truncation`，保守拒绝综合。
  不分页、不自动提高预算；值域快照不能证明本次分组完整。
- 两期分组键分别唯一，取并集对齐；NULL 维度保留独立桶，不与字符串“未知”合并。
- 在完整性通过后，某个组单期不存在才按零处理；NULL 度量、全空期间、列不符、非有限数值均不能补零。
- 分别核对两期分组和等于各自总量；不能只核对 delta，不能用正负抵消掩盖遗漏。

### ③ 图外有界执行，父回合与子步骤隔离

新增 `DataAgent.analyze(question, *, session_id=None, identity=None) -> AnalysisResult`。
无分析意图时只调用一次既有 ask 并包装其结果；匹配时按四步顺序调用私有 `_run_analysis_step`，
不调用公开 `run_plan`，也不重新生成子问句。

`build_graph` 增 `persist: bool = True`；默认保持现状，`persist=False` 显式编译无 checkpointer 的实例，
同时传入 saver 与 persist=False 属配置错误。该实例与父图由**同一个 build_graph 定义**构造，
依赖、node_execute、Guard 与 RLS 均同源，不是第二份执行逻辑。
每步带 `plan_override` 与同一已验证 identity；子步局部 turns/last_plan 随调用丢弃，
无独立 checkpoint、无用户轮计数、无 atlas.turn.count 增量。

任一步 blocked/error 立即整体停止，后续执行器调用为零；意外 clarify/handoff 视为内部契约错误。
不重试、不自动换维度、不启用 candidate、不发布部分成功为完整答案。
父请求身份指纹只校验一次，但**每个子 SQL 仍须 resolve_claims + Guard**，不能把这两类校验混为一谈。

四次是分析 SQL 调用上限，不包括离线资格验证；它只证明次数有限，不证明查询能在有限墙钟时间内返回。
现有执行器只有连接超时（`eval/runner.py:159`），没有完整查询取消契约；本批不承诺硬任务超时。
沿用每 SQL 的 Budget，不扩大表白名单、时间窗或行数限制；工作量放大由四步上限和业务限流约束。

### ④ 一个用户逻辑轮：checkpoint 单一事实源与明确中断语义

保留 `run_plan` 的公开签名和既有行为；不增加含糊的 record_last_plan 开关。
新增的仅是分析内部生命周期，普通调用仍由 node_plan 记账。

1. 在 `DataAgent` 的实例级可重入锁内完成读取状态、身份比较、父轮开始、子步与父轮结束。
   ask/run_plan/analyze 共用该锁；一模型一 Agent 的现有部署下，避免长分析与普通请求交错覆写。
   这是串行化代价，不声称支持多个进程/多个实例同时写同一模型会话。
2. 身份冲突在写状态及执行前抛出，轮数不变。认证、请求格式或限流拒绝不产生用户回合。
   分析入口的身份转移规则显式化：已绑定身份的会话收到匿名 analyze 调用按冲突拒绝（零写入零 SQL）；
   匿名会话带身份调用沿用现有图的首次绑定语义；running 恢复仅接受与记录一致的身份
   （先前匿名仅可匿名接续）。普通入口不因本 ADR 改变转移规则。
3. 已接纳分析在父 thread 写 `turns=旧值+1`、原问句、身份指纹及 `analysis_record.status=running`，
   清理旧 plan/sql/rows/clarification 等单轮残留；不 invoke 父查询图。子步只更新本轮证据，不再增 turns。
4. 正常结束、澄清、执行失败均写明确终态；无总量假值、不拿任一子 SQL 冒充整轮 SQL。
   状态写入使用 LangGraph `update_state`，显式指定终端 writer `as_node="explain"`；
   该参数只指定状态写入归属，不调用解释节点。必须验证每次写入后 `next=()`，无额外 SQL。
5. 中途进程崩溃保留 running 事实；下次持同一身份进入时先将其标记 interrupted（不增加旧轮计数），
   再接受新一轮。无自动续跑、无 exactly-once 承诺，客户端重试是新请求。
6. 子步骤不改父 `last_plan`。同时新增 `analysis_followup_blocked` 状态：分析后不把残句追问
   偷接到更早的普通 Plan；明确提示完整重述。直到一次明确的普通 Plan 执行成功才解除，
   未含该新字段的旧 checkpoint 仍按原追问逻辑读取。

`analysis_record` 只保存有界原始事实：版本、状态、问句、期间与 Plan 投影、成功步骤的 Guard 出口 SQL、
columns/rows、耗时、失败步骤编号与安全原因码、快照/语义绑定。rows 标量按**带类型编码**保存
（Decimal/字符串/整数/NULL 可区分，类型与值均往返不变）；不保存 claims 本体。
贡献率与叙事属于派生值，不存 checkpoint；`AnalysisResult`/`Attribution` 不进 msgpack 白名单。
若未来改为直接持久化 dataclass，必须另补 serde 往返测试，不能假设嵌套类型自动注册。

### ⑤ 综合是精确算术；不可用与执行失败分开

令整体基线/当期为 B/C，各组为 b_i/c_i：

```text
delta_total = C - B
delta_i = c_i - b_i
contribution_pct_i = 100 * delta_i / delta_total
```

本批数值输入仅接受 int/有限 Decimal（排除 bool/float/字符串等未声明类型）。
总量、分组加减及对账使用精确十进制或等价有理数；不得经 float。
比例计算用有理数分子/分母，最终百分数统一 ROUND_HALF_EVEN 保留六位小数并输出字符串；
同时保留 delta_i 与 delta_total 作为复算依据。六位是展示契约，不是统计精度声明。

- 负贡献及超过 100% 的贡献原样保留：分别表示抵消或被其它组抵消，不裁剪、不重新归一。
- 整体净零 → 保留已验证 delta，所有 contribution_pct 为 null，状态 unavailable、原因 zero_total_delta。
- 整体未下降而问下降（上升同理）→ direction_mismatch；报告实际方向，不顺着错误前提编结论。
- 完整性/数值/资格/快照失败 → unavailable + 原因；不产生贡献项或结论。
- NULL 分组维度不是 NULL 度量：前者是合法桶，不能被 all_null_column 的笼统注记误杀。
- items 按 abs(delta) 降序、同值按带类型的规范化维度键排序；先完整计算再展示，MVP 不裁 TopN。
- 文本仅引用结构化字段，固定说明“这是变化贡献分解，不代表业务因果”；不生成缺货、营销等未观察原因。

执行成功但综合不可用仍是 `turn.kind=answer`、`analysis.status=unavailable`，不扩展 TurnKind。
被 Guard 拒绝/执行故障则 `turn.kind=blocked/error`，分析无 totals/items/text，
对外仅安全步骤摘要，不泄露被拒 SQL或底层异常中的连接信息。

### ⑥ API、观测、评测与明确非目标

- 新增 `POST /api/v1/analyze`，请求复用 AskBody，复用 Bearer、模型选择、业务桶、身份冲突 422 与快照 503。
  `/ask`、`/plan`、`/compile`、`/plan/execute` 不自动改成多步。无分析意图的 analyze 请求按③回落 ask。
- `AnalysisResult` 包装 `TurnResult` 与分析证据；`_turn_payload` 增独立 `analysis` 键。
  非分析请求恒为 null，原 explanation 不变。HEAD 观测 21 键，复核当日工作区未提交 chart 接线后
  观测 22 键；无论 chart 是否先行合入，analysis 都在落地当日真实集合上再增一键，
  测试断言键集合而非固定数量，不回退他人 chart 变更，也不把未来 chart 写成现状。
- 分析父轮的 sql/explanation 为 null，columns/rows 为空、row_count=0，不冒充单 SQL 结果；
  metric 为目标 Metric，latency_ms 为实际执行的子 SQL 耗时之和；总耗时另放 analysis.elapsed_ms。
- analysis 固定投影：`schema_version, intent, status, metric, dimension, baseline, current, filters,
  snapshot_sha, semantic_sha256, recipe_version, totals, items, steps, reason_code, text, elapsed_ms`。
  不适用字段用 null 或空数组；步骤携 role/kind/已执行 SQL/columns/rows/latency_ms，失败时裁为安全摘要。
- 一次 HTTP 请求一条业务审计记录；父轮一次 atlas.turn 计数，子步骤独立 span 关联
  `(model, session_id, turns, step_role)`，不能调用 record_turn 四次抬高 QPS。
  综合无 LLM token，不虚构模型调用、token 成本或时延倍数结论。
- 新增独立 analysis gold/schema/runner，分析计划匹配、各步结果匹配、贡献计算匹配、拒答契约分别报告，
  **不混入既有 EX/Plan Acc 分母**。无期望、快照不匹配、必测 skip、任一断言失败均非零退出。
- 不纳入：自由 LLM 规划、结果驱动动态下钻、跨指标/跨模型/多维交叉归因、异常检测、自动修复、
  任务完成度评价、LLM 叙事、分析专用图表、SSE、异步恢复、CLI 新命令与分析 UI。
  API/SDK 是本批使用入口；任务完成度属于愿景 §3.3，不是 §3.2，补充工程评测不等于取代 ADR-0010。

## 理由、代价与对既有 ADR 的影响

1. 保留 ADR-0009 的**无环拓扑与无自愈**，不承诺节点源码逐字不变。拓扑不变须查节点/边，
   不能靠“测试文件只增不改”证明；新增状态冲刷和追问保护是有意变更。
2. ADR-0020 的 checkpoint 单一事实源不变，但“只有 node_plan 推进轮数”需增加分析父轮写入例外；
   一实例串行处理会降低并发度，worker 数与跨进程写限制不放开。
3. ADR-0022 增一个业务端点和一个顶层字段，须同步 OpenAPI、前端路径集合/类型和测试 fixture。
   其“一次性会话不落盘”的旧文案与当前实现不符，落地注记需纠正，而非据其设计子步骤。
4. 资格证明、原始证据 checkpoint、独立评测不是可删的收尾；缺任一项会出现“数学正确但业务错误”。
   多步有额外 DB 往返和保存成本，无实测不写具体性能指标，也不写“时延=单轮×步数”。
5. `docs/text2insight.md` 的相对时间与因果百分比示例必须标为愿景，并补绝对时间、单维贡献的本批边界。
   不能把“分析完成”或本文 proposed 状态改成“功能已交付”。
6. 无新增第三方依赖；复用 Python 标准库、LangGraph、sqlglot、JSON Schema 及现有测试工具。
   若实现需要新增依赖、放宽 Guard 或改变权限，先重新裁定，不在任务内顺手引入。

## 开发实施计划

> 执行者先读本 ADR 与 AGENTS.md；使用 `superpowers:subagent-driven-development` 或
> `superpowers:executing-plans` 逐任务实施。以下复选框**全部未执行**；proposed 经确认后才进入编码。
> 计划直接维护在本文件，不另建一份会漂移的任务清单。下面命令是实施时的验证命令，不是本次执行回执。

**Goal**：交付可复现、可拒绝、受权限约束的单维变化贡献分析，且普通问数不回归。

**Architecture**：确定性解析 → 四个普通 Plan → 同源无状态子图 → 精确综合；父回合独占会话生命周期。

**Tech Stack**：Python 3.11、现有 LangGraph/checkpointer、sqlglot、JSON Schema、unittest；真链为锁定 Doris 快照。

**Spec**：`infra/adr/0026-multi-step-task-planning.md` 决策①～⑥。

### 全局约束与实施顺序

- N1/N2/N3/N4/N6/N8/N9 不放宽；不得 make seed、重锁覆盖基准、输出凭据或绕开 Guard。
- 每任务先写可失败断言并记录红灯原因，再写最小实现，再跑该任务与相关既有回归。
- 合成 rows 只证明算法；真库期望必须来自人工审阅的独立参考计算，不能调用被测 synthesizer 回填。
- 下文“新增”文件目前不存在；实际编码时才创建。临时测试数据走 TemporaryDirectory，不污染仓库。
- 仅在用户明确要求提交时创建 commit；语义登记与重构不得混为一个提交。
- 依赖：`T01 → T02 → T03`；`T01 → T04`；`T01 → T05 → T06`；
  `T02/T03/T04/T05/T06 → T07 → T08`；`T02/T07 → T09`；`T08/T09 → T10`；`T10 → T11`。
  T04 与 T05 可并行；同一 graph.py 的 T05/T06/T07 必须串行。

### T01：锁定数据契约与先行分析样本

**文件**：新增 `agent/analysis.py`（类型/纯函数）、`eval/analysis/schema.json`、
`eval/analysis/finance/attribution-001.json`、`eval/analysis/finance/clarify-001.json`、
`tests/test_analysis_contract.py`；修改 `semantic/lint.py` 的 schema 调用。

**接口**：在 analysis.py 定义以下 frozen dataclass；不放编排、数据库或 HTTP 逻辑：

```python
AnalysisPlan(intent, metric, dimension, baseline, current, filters, direction,
             sub_plans, synthesizer="additive_delta_v1", recipe_version=1)
AttributionItem(value, baseline, current, delta, contribution_pct)
Attribution(status, baseline, current, delta, items, reason_code, text)
AnalysisResult(turn, plan, steps, attribution, reason_code, elapsed_ms,
               snapshot_sha, semantic_sha256)
```

`baseline/current` 在 AnalysisPlan 中为 TimeSpec；在 Attribution/AttributionItem 中为
`Decimal | None`（unavailable 路径无有效数值时为 null，成功路径非空）；
`filters: tuple[Filter, ...]`，`sub_plans: tuple[Plan, ...]`，`steps: tuple[TurnResult, ...]`；
`direction ∈ change/decrease/increase`，`turn: TurnResult`；未解析时 plan 为 None，未综合时 attribution 为 None。
`AnalysisResult` 携带快照/语义绑定字段，供综合校验、HTTP 序列化与评测取证，不依赖从 steps 反推。
status/原因码在本任务以**闭合矩阵**定义：枚举全部终态与全部原因码，声明各终态允许的字段组合，
以及 `turn.reason_code`（blocked/error 终态）与 `attribution.reason_code` 的优先关系——
turn 终态优先，attribution 仅在 answer 下有意义。不使用任意对象代替未定义类型。

- [ ] schema 采用 `additionalProperties:false`，用判别字段区分 expected answer/clarify/blocked/error/unavailable。
      非澄清样本逐一声明四角色的完整 Plan、快照 SHA、语义 SHA、参考 SQL/结果、人工审阅信息；
      澄清样本声明 reasons/clarification kind 与 expected_sql_calls=0。
- [ ] 首先写 schema 负测：缺第二期、未知字段、重复角色、错步序、answer 缺期望结果必须失败；
      `make lint` 必须发现 analysis 目录里的非法文件，而不是只扫 gold-*.json。
- [ ] 样本带 maturity 判别（draft/ready）：draft 只须通过结构 lint（可缺参考结果字段），
      禁止进入真链评测；ready 必须具备完整 oracle。T09 完成独立计算后才提升 ready，
      不能从被测输出自动锚定。blocked/error 样本不要求伪造未执行步骤的结果。
- [ ] 定义 canonical Plan 投影包含 metric/dimensions/time/filters/order_by/limit/comparison，固定四角色常量；
      对实例长度、角色顺序和全 Plan 类型做契约断言。

**验证**：`.venv/bin/python -m unittest tests.test_analysis_contract -v`；`make lint`。
**完成门槛**：schema 负例能击穿、合法结构通过；分析执行测试仍红时明确留待 T07，不把 schema 绿当功能绿。

### T02：登记可加组合并建立快照资格证据

**文件**：修改 `semantic/governance/atlas_governance.schema.json`、`semantic/governance_validate.py`、
`semantic/ossie/atlas_finance.ossie.yaml`、`agent/compiler.py` 的 SemanticModel 加载部分、`agent/factory.py`；
新增 `eval/analysis_eligibility.py`、`tests/test_analysis_eligibility.py`。
产物为 `data/snapshots/<snapshot_sha>.analysis.json`，不手写合格检查结果。

**接口**：`SemanticModel.attribution_dimensions`；
`verify_eligibility(model, snapshot_meta, executor, budget) -> dict`；
`load_eligibility(model, snapshot_meta) -> dict`（只读绑定证据，缺失/过期返回不可用事实，不阻断普通 Agent 构造）。

- [ ] 先测未声明组合、比率/AVG/DISTINCT、余额与未知字段均拒绝；用最小临时语义模型测试 SUM/COUNT 正例。
- [ ] 为两个初始组合补声明，复用原 Metric 表达式；模型文件 SHA 变更使旧资格证据失效。
- [ ] 用 sqlglot 解析指标 AST，解析实际 JOIN 路径；验证重复目标键、孤儿引用、行放大、漏行、
      跨事实表达式及权限额外 JOIN。检查 SQL 由 AST 构造且全部为标量/计数型
      （COUNT、存在性判别，不倾倒行集），带显式绝对时间谓词覆盖锁定快照全期——
      不依赖 Guard 默认时间窗回退；逐条 enforce 后执行，报告记录每条检查 SQL、
      实际检查范围（如用分片则含分片规则）与结果。覆盖不全不得签发 eligible。
      不使用值域注册表代替连接质量证明。
- [ ] factory 读取匹配的资格产物传给分析入口；普通 ask 在证据缺失时仍可构造。
      资格 CLI 接受显式 `--snapshot-sha`，先复核锁定数据，失败非零，不写入合格状态。

**断言核心**：`self.assertFalse(report["eligible"])` 分别对应重复键/孤儿引用 fixture；
语义 hash 不符时 `load_eligibility` 不得返回 eligible=True。
**验证**：`.venv/bin/python -m unittest tests.test_analysis_eligibility -v`；`make lint`。
**完成门槛**：合成坏数据能被拒；真快照资格检查由 T09 执行，在此之前不宣称初始组合已可上线。

### T03：双语分析槽位解析与固定 Plan 生成

**文件**：修改 `agent/analysis.py`、`agent/planner.py`、
`semantic/synonyms/patterns_zh_cn.yml`、`semantic/synonyms/patterns_en_us.yml`；
新增 `tests/test_analysis_planner.py`。

**接口**：`AnalysisPlanner(model, *, group_limit: int)` 与 `.plan(question)`；
底层复用 `load_locale_patterns/load_locale_synonyms`，必要的槽位解析入口从 Planner 提取，禁止复制同义词表。

- [ ] 写中英矩阵：无分析意图返回 None；完整四槽返回 AnalysisPlan；“为什么/帮我分析”缺槽不回落普通问数。
- [ ] 拆出两段时间后分别调用已有单期间解析，严格匹配连接词决定角色；消除年月/季度内部重叠命中。
      两种词序、跨年、同期间、混粒度、三个期间、省略年份、相对时间全部锁定。
- [ ] 检查一个 Metric/Dimension、资格声明、同 WHERE、无 HAVING/TopN；未识别过滤片段必须澄清。
- [ ] 构建四个 comparison=None 的 Plan；总量 limit=1，分组 limit=group_limit 且 OrderSpec(D)。
      编译预检四个 Plan，但不执行 SQL；不以重新拼写的子问句再走 Planner。

```python
plan = planner.plan("分析 2013Q4 相对 2013Q3 的佣金收入按分支的变化贡献")
self.assertEqual([p.time.value for p in plan.sub_plans], ["2013Q3", "2013Q4", "2013Q4", "2013Q3"])
self.assertEqual([p.dimensions for p in plan.sub_plans], [(), (), ("Branch",), ("Branch",)])
self.assertTrue(all(p.comparison is None for p in plan.sub_plans))
```

**验证**：`.venv/bin/python -m unittest tests.test_analysis_planner tests.test_planner -v`。
**完成门槛**：同输入输出相等、角色不颠倒、未知条件不丢弃；既有单期间 Planner 行为不变。

### T04：完整性检查与纯函数贡献计算

**文件**：修改 `agent/analysis.py`；新增 `tests/test_analysis_synthesis.py`。
**接口**：`synthesize(plan: AnalysisPlan, steps: tuple[TurnResult, ...]) -> Attribution`；
只消费执行事实，不导入执行器、图、API 或 LLM。

- [ ] 先写合成 rows 测试：B=100、C=80；A 组 60→30、B 组 40→50；断言两项 delta 为 -30/+10，
      百分数为 150/-50。这是测试输入，不是实测业务数字。
- [ ] 按列名取值、两期键并集、NULL 维度独立桶；重复键、NULL 度量、空总量、多行总量、非有限值拒绝。
- [ ] 两期分别对账；构造遗漏正负抵消仍对账成功但行数触及 LIMIT 的反例，必须 possible_truncation。
- [ ] 实现精确加减与百分数舍入，覆盖大 Decimal、负值、净零、零基线、方向不符、同贡献稳定排序。
      测试修改全局 Decimal context 不改变结果，避免依赖进程默认精度。
- [ ] 固定措辞仅从 Attribution 字段渲染，零总变化保留 delta/空比例，失败不输出原因臆测或百分数。

```python
self.assertEqual(by_value["A"].delta, Decimal("-30"))
self.assertEqual(by_value["A"].contribution_pct, Decimal("150.000000"))
self.assertEqual(by_value["B"].contribution_pct, Decimal("-50.000000"))
```

**验证**：`.venv/bin/python -m unittest tests.test_analysis_synthesis -v`。
**完成门槛**：完整性门禁与算术分别可被反例击穿；删除任一门禁不能仍全绿。

### T05：同源图的无状态子执行通道

**文件**：修改 `agent/graph.py` 的 build_graph/DataAgent 私有构造；新增 `tests/test_analysis_execution.py`。
**接口**：`build_graph(..., persist: bool = True)`；
`DataAgent._run_analysis_step(plan: Plan, *, identity: dict[str, object] | None) -> TurnResult`。

- [ ] 先证明当前随机 run_plan 仍写 saver；为真正 stateless 写失败测试（saver spy 的 get/put/put_writes 零调用）。
- [ ] 增 persist=False 构建分支，共用节点定义/依赖；不要修改 checkpointer=None 的 MemorySaver 默认语义。
- [ ] 每步以全新输入 state + plan_override 调无状态图，传同一 identity；禁止 `_invoke_turn` 和 record_turn。
- [ ] 断言 stateless/stateful 节点和边集合相等；同 Plan 的 Guard 出口 SQL/rows/拒绝结果相等。
      分别在步骤中的非法表、权限解析失败、编译失败、执行器异常处断言安全结果形态。
- [ ] 失败步骤的执行事实由本任务采集：error 终态同样记录 latency_ms 与已执行证据，
      并区分“未执行被拒”与“执行后失败”；T07 的耗时汇总只计实际执行的子 SQL。

**验证**：`.venv/bin/python -m unittest tests.test_analysis_execution tests.test_graph tests.test_session_persistence -v`。
**完成门槛**：execute 实现只有一份；子图不持久化、不走候选、不写用户轮指标，默认图回归通过。

### T06：父会话状态、身份与中断恢复记账

**文件**：修改 `agent/state.py`、`agent/graph.py`；扩展 `tests/test_session_persistence.py`。
**接口**：TurnState 新增 `analysis_record: dict | None`、`analysis_followup_blocked: bool`；
DataAgent 私有 `_begin_analysis(sid, question, identity) -> dict`、
`_write_analysis_state(sid, updates: dict) -> None`，后者不推进 turns。

- [ ] MemorySaver/SQLite 都测试开始 + 子步记录 + 结束只推进一个用户轮，且 next 始终为空、无父 SQL。
- [ ] 把 ask/run_plan/analyze 包在同一实例 RLock 内；用线程屏障证明同模型的普通请求不能插入分析四步中间。
      测试身份冲突在任何状态写入前发生；分析新会话、跨模型同 sid、重启续接分别覆盖。
      补匿名→实名、实名→匿名与 running 恢复的身份转移断言（按决策④规则）。
- [ ] 开始时清除所有单轮结果残留，保存分析事实而不写贡献率/叙事/claims。
      原 last_plan 保留，但挂 analysis_followup_blocked；明确新 Plan 成功后才解除。
- [ ] 注入中断：开始后或任一步后重新构造 Agent，旧 running 变 interrupted、不自动发 SQL；
      新用户轮只再增加一次 turns。持久化失败不得返回伪成功。
- [ ] 原始 rows 标量（Decimal/字符串/整数/NULL）按带类型编码往返后类型与值均保持，可供复算；
      检查 checkpoint 无 AnalysisResult dataclass，
      并验证一轮分析之后再普通问数不会携带旧 analysis_record 的成功状态。

**验证**：`.venv/bin/python -m unittest tests.test_session_persistence tests.test_graph -v`。
**完成门槛**：只认持久化状态，不新增轮数副本；失败与重启不污染 last_plan，也不静默承接旧指标。

### T07：DataAgent.analyze 编排与父子观测

**文件**：修改 `agent/graph.py`、`observability/otel.py`；新增 `tests/test_analysis_agent.py`。
**接口**：`DataAgent.analyze(...) -> AnalysisResult`（决策③签名）；
观测模块新增 `record_analysis_step(turn, *, model, session_id, turns, role) -> None`，只写步骤 span。

- [ ] 用 recording executor 写完整四步测试及每个失败索引的测试；断言最多四次、固定顺序、失败后零调用。
- [ ] 按 T06 开始父轮，解析/资格检查失败不执行 SQL；通过后依次运行 T05 并记录已执行事实；
      四步全成功再调用 T04，最终封装父 TurnResult 和 AnalysisResult。
- [ ] 无分析意图仅委托 ask 一次，analysis 为 null；有意图的澄清计一父轮且 SQL/LLM 调用均为零。
- [ ] 统一模型/快照/身份/预算，禁止每步构建 live agent。blocked/error 裁掉部分结果与被拒 SQL，
      reason_code 为稳定安全代码，保留失败步骤角色。
- [ ] 记录父 atlas.turn 一次、各已执行步 span 一次，失败步也有终态；测 no-op 与观测异常隔离。
      既有 latency_ms 仍只表示执行耗时之和，elapsed_ms 用单调时钟测量全流程。

**验证**：`.venv/bin/python -m unittest tests.test_analysis_agent tests.test_analysis_execution tests.test_graph -v`。
**完成门槛**：真实 graph + fake DB 可完成全流程与所有终态；没有“只测综合函数”的假端到端通过。

### T08：HTTP 入口、响应与前端契约镜像

**文件**：修改 `serving/api.py`、`tests/test_api_contract_v2.py`、
`frontend/src/api/endpoints.ts`、`frontend/src/api/types.ts`；新增 `tests/test_analysis_api.py`；
同步编译报出的 TurnPayload fixture（只补字段，不增加分析 UI）。

**接口**：POST `/api/v1/analyze` 使用 AskBody；
`_analysis_payload(result: AnalysisResult) -> dict | None`；`_turn_payload` 增可选 analysis 参数并恒输出该键。

- [ ] 先写无令牌/非法体/未知域/身份冲突/限流/缺快照、普通 fallback、成功/澄清/blocked/error/unavailable 测试。
- [ ] 新端点复用已有认证、业务桶与工厂；一请求一条审计，治理桶不挤占；不得客户端提供 identity。
- [ ] 按决策⑥序列化完整键集，Decimal 字符串、NULL 原样；父级空 SQL/rows 的语义与子步数据层分开。
      对拒绝路径搜索响应、审计、span，不能含被拒 SQL、claims 条件值或底层连接异常文本。
- [ ] 同步 OpenAPI 路径集合、TS API.analyze、TurnPayload.analysis、AnalysisPayload 类型与 fixture；
      不修改已有 kind 顺序，不把 analysis 塞进 Explanation，不为未来 chart 预填实现。
- [ ] 原 /ask 与 /plan/execute 逐字段回归，新增键以外行为不变；analysis=null 的前端页面照常渲染。

**验证**：`.venv/bin/python -m unittest tests.test_analysis_api tests.test_api_contract_v2 -v`；
`make ui-build`。实施时若修改了前端文件，按项目要求启动 `make ui-dev` 并在浏览器验证普通问数、
角色切换、会话时间线和异常响应；记录 console/network，不能只拿类型检查当 UI 验收。
**完成门槛**：路径与响应双向集合一致；保留原安全断言，不为多一个字段盲改计数常量。

### T09：独立参考值、分析评测器与严格退出码

**文件**：新增 `eval/analysis_eval.py`、`tests/test_analysis_eval.py`；完善 T01 样本；
修改 `Makefile` 增 `analysis-eval` 目标（此前不存在，命令在本任务落地后才可用）。
复用 T02 的 eligibility CLI，不改旧 EX/Plan Acc 算法或自动回填策略。

**接口**：`evaluate_analysis(agent, samples) -> dict`；CLI 输出
`eval/reports/analysis-<code_sha>.json`，单列 code_sha、snapshot_sha、semantic_sha256、dirty 状态和逐项断言。

- [ ] 用故意错误的 AnalysisPlan/rows/贡献率/拒答 kind 写评测器反例，确保每种失败导致非零退出；
      缺期望、draft、空样本集、必测 skip 和快照不符同样失败，禁止空跑绿灯。
- [ ] 在人工选定的锁定快照执行 T02 资格检查；独立参考 SQL 经 Guard 后执行，审阅两期与完整分组。
      独立参考算术不调用 AnalysisPlanner/synthesize，以防同错同对；复核后保存期望并置 ready。
- [ ] 用故意未实现或错误的分析器记录失败基线，再运行实现；不把 first-run 输出自动回填为答案。
- [ ] 分别比对完整有序 Plan 序列、每步列/结果（规范化行序）、整体/各组 delta/百分数及 unavailable 原因；
      一项失败不得被其它项平均掩盖。报告按检查列给计数，不造综合“准确率”。
- [ ] 注册 `analysis-eval`；提供 `--snapshot-sha` 与 `--dry`：真链运行必须显式给出快照 sha
      （缺失即拒绝出报告，不默认 HEAD），dry 只校验结构/计划，执行项标 not_run、不能用于放行。
      运行前后复核指定快照的数据指纹与对应 meta（现有 `data/snapshot.py --check` 只认 HEAD
      锁定快照，不能直接复用），源数据变化即报告无效。Makefile 目标透传
      `ANALYSIS_SNAPSHOT_SHA` 变量给该参数。

**验证**：`.venv/bin/python -m unittest tests.test_analysis_eval -v`；
落地后运行 `make analysis-eval`，检查文件内容与退出码两者。
**完成门槛**：错误实现能被评测器发现，真链有独立 oracle；旧 gold 与新分析报告不混分母。

### T10：真实 Doris 与 HTTP 跨层验收

**文件**：修改 `eval/e2e_acceptance.py`、`eval/api_acceptance.py`；
扩展 `tests/test_analysis_eval.py` 的验收报告门禁断言。

- [ ] 保留现有 S1～S7，增加一个不预设涨跌的绝对期间贡献场景，再加真实受限角色与相对时间拒答场景。
      分析场景的样本、预算、资格证据与前后指纹绑定同一快照（与 T09 的 `--snapshot-sha` 一致）；
      该快照缺资格证据时场景失败而非静默跳过。通过数从实际 scenarios 生成，不把设计新增数量写成已通过数量。
- [ ] HTTP 验收覆盖 analyze → 四步 Guard/RLS → 贡献响应 → 同会话普通问数；
      每条执行 SQL 含对应角色谓词与时间/LIMIT，跨身份仍 422，不可拿 hq_admin 冒充受限角色验证。
- [ ] 强制中途 Guard 拒绝与综合不可用，核对后续不执行、部分结果不作为最终解释、轮数和统计不多算。
- [ ] 保留 `make e2e` 既有固定输出约定，报告内容增 code_sha/snapshot_sha；
      归档时用已有 `--report` 参数明确写入 `eval/reports/e2e-acceptance-<code_sha>.json`。
      API 报告沿用 api-acceptance-<sha>.json；归档前检查真实 SHA，不手造报告。

**验证**：`make e2e`、`make api-verify`（均为现有目标）。
**完成门槛**：真库不可用或关键场景 skip 就不能声称交付；绝不通过 make seed 或换基准来消除失败。

### T11：文档同步、全量回归与交付审计

**文件**：修改 `README.md`、`README.en.md`、`docs/text2insight.md` 与本 ADR；
在 `infra/adr/0020-session-persistence-sqlite-checkpoint.md`、`infra/adr/0022-http-contract-v2.md`
追加落地注记，不重写其历史正文。

- [ ] README 能力表与端点表同时写支持范围、初始组合、绝对时间、确定性措辞、API/SDK 入口；
      Known Limitations 追加不可加/截断/可变数据/无硬超时/无分析 UI 等限制，不删除美化旧条目。
- [ ] 愿景示例补绝对期间与明确维度，未测数字改为符号化说明；动态下钻、业务因果、异常检测、
      LLM 叙事和阶段三任务完成度保留为未交付边界。
- [ ] ADR-0020 注记父轮记账例外/串行与中断边界；ADR-0022 注记新端点/字段，并纠正随机 ID 不等于无存储。
      本 ADR 只有取得用户决策确认才改 accepted，只有全部验收证据齐全才加实现回执。
- [ ] 运行 `make lint && make test && make eval`、`make analysis-eval`、`make e2e`、`make api-verify`。
      检查旧 eval 的逐样本 `plan_ok`、`ex`（pass/fail/anchored）、`clarify_ok`、Guard 拒绝和执行错误，
      不以 exit 0 代替通过；`anchored` 是首次回填，不算独立比对通过。预检回填行为，
      未审阅的空期望不允许自动变成发布基准。
- [ ] 用实际报告核对下表所有条目；保存代码版本、数据版本、脏工作区标记、命令、测试结论、
      失败/skip 理由与报告路径。未完成项继续保持未勾选，不写通过率或时延承诺。

**完成门槛**：新功能验收与旧问数回归都具直接证据，才报告实现完成；本次文档拆解不勾选上述执行任务。

## 验收映射与证伪条件

| 要求/原判据 | 开发任务 | 必须检查的证据 |
|---|---|---|
| ① AnalysisPlan、绝对双时间、模板上限；原判据2 | T01/T03 | 完整 Plan 序列断言，双语/两种词序/缺槽/未知过滤/相对时间零 SQL |
| ② 图外同源、无环；原判据1 | T05/T07 | 节点/边集合、默认图回归、无状态 saver spy，非“测试文件没改” |
| ③ 一父轮与 last_plan；原判据5 | T06/T07 | Memory/SQLite、身份冲突、普通/分析交错、重启、中断、追问保护 |
| ④ 贡献与溯源；原判据3 | T02/T04/T09 | 可加资格、JOIN 质量、完整性、两期对账、精度/NULL/负贡献/净零/方向/独立 oracle |
| N3 与逐步权限；原判据4 | T05/T07/T08/T10 | 每 SQL Guard/RLS、失败即停、拒绝信息裁剪、受限角色真链 |
| API 键集；原判据6 | T08 | OpenAPI/TS 集合、analysis null、Decimal、所有终态、旧 explanation 不变 |
| 主评测/分析评测/真链；原判据7 | T01/T09/T10/T11 | 严格 schema、错误实现反例、独立期望、SHA 绑定、报告逐项与退出码 |
| 文档诚实；原判据8 | T11 | 中英 README 能力与 KL、愿景示例、相关 ADR 注记、实现/决策状态分开 |
| MVP 范围与不实现 LLM 候选接口；决策⑤⑥原意 | T03/T07/T11 | 未支持形态澄清、零 LLM 调用、无空参数/新候选路径、非目标文档 |
| 成本与生命周期 | T06/T07/T08 | 父轮一次计数、步骤 span、限流隔离、执行耗时/总耗时分开、无伪造恢复保证 |

**证伪处置**：若同源无状态图不能同时保持执行安全与父会话不变量，停止 T07，重审编排落点；
若资格/完整性无法证明，保持 unavailable，不凑平结果、不引入 LLM 猜测；
若独立参考值揭示四 Plan 的口径不一致，修正模板与语义登记，不能先改 golden 迎合实现。
这些是实施门禁，不是本次文档任务尚未执行的测试被表述为“已失败”。

## 什么情况下应该推翻

- 需要结果驱动动态下钻、第二种分析模板或自由候选规划：先补独立评测与终止/预算设计，再决定是否改图。
- 单 Plan 可以在已验证方言与 Guard 下直接表达相同贡献语义：比较一致性、代价和复杂度后考虑替换四步。
- 要支持比率、DISTINCT 或时点余额：先裁定其分解数学与可加范围，不能简单放开 AST 白名单。
- 要支持可变数据、硬任务时限、并行步骤或多进程共享会话：先补一致读、取消、锁与恢复协议。
- 相对时间有稳定评测锚点：联动 ADR-0014 修订；本 ADR 不单方面放宽。
- 要输出业务因果、任务完成度或 LLM 叙事：另立 ADR，增加相应证据和评测，不把算术贡献当因果结论。

## 实现回执（2026-09-16）

> 本节为实现回执，不改变上文任务清单的复选框状态（交付审计属控制器职权）；状态行仍为 `proposed`，待用户决策确认后再定 accepted。本节全部数字引自控制器 2026-09-16 实测（T11 brief「可引用实测事实」节），均有 `eval/reports/` 下报告产物背书。

**代码版本**：`b95a9e9`；工作树脏——当前全部改动未提交（用户约束：仅明确要求时才 commit）。

**数据版本（三快照锚定关系）**：TPC-DI 零售经纪固定快照（ADR-0006）。分析评测与资格证据唯一绑定 `7c966e9`（`data/snapshots/` 唯一含 `*.analysis.json`）；旧问数 API 验收锚定 `b933e20`（`api_acceptance.py:88`）；e2e 12 场景实跑全部绑定 `7c966e9`（Makefile 动态缺省；裸跑不带 `--snapshot-sha` 时 S1~S7 代码缺省 `7d48dcb`，`e2e_acceptance.py:69`）；旧 eval（`make eval`）设计只认当前 HEAD，实跑锚定 `b95a9e9`（R11-6 重锁）。数据未变：gold result_hash 印章跨历史 sha 逐位复现（b47a6c1 49 条、ccb4c8b 15、92033c9 14、1e5d35b 13、30b8344 7、b7e9ce7 6、5d1e22b 2、None 13——None 全为 runner 不处理的 clarify/paraphrase 类），97/97 EX pass 即直接证据。

**执行的命令清单**：`make lint`、`make test`、`make eval`、`make analysis-eval`（`ANALYSIS_SNAPSHOT_SHA=7c966e9`）、`make e2e`、`make api-verify`。

**测试结论**：

- `make lint`：全绿——黄金集结构校验 106 条样本；权威目录校验通过；值域快照绑定 21 个文件；analysis 样本 schema 校验 2 条样本。
- `make test`：`Ran 1183 tests in 18.881s` → `OK (skipped=14)`；skip 为既有跳过（环境依赖类），非本次引入。
- `make eval`：106 样本全绿，逐样本核实（非仅 exit 0）——97 执行类 EX 全 pass（finance 73/73 + retail 24/24，EX 口径 = 过 Guard 的编译 SQL 执行结果 sha256 与锚定 result_hash 一致）+ 9/9 clarify（ambiguous=True、无 SQL、无 ex 键）+ plan_acc 73/73 与 24/24；`ex_anchored=0`（无首次回填）、`exec_errors=0`、`guard_blocked=0`；gold 文件零改动。
- `make analysis-eval`：`make analysis-eval ANALYSIS_SNAPSHOT_SHA=7c966e9` 裸跑 exit 0——attribution-001 ok（终态 answer、四步各带 SQL、attribution_totals/attribution_items、reason_code、bindings 全 pass）+ clarify-001 ok；42 检查 33 pass / 0 fail / 9 skip（skip 均为终态 kind 的设计性跳过）；报告指纹 code_sha=b95a9e9、dirty=true、snapshot_sha=7c966e9、semantic_sha256=a3ba5e2b…、snapshot_verified before/after=True。
- `make e2e`：12/12 场景 pass（S1~S7 旧问数回归 + S8~S12 分析新增），文件名 sha 闸门通过；S8 实测 totals：baseline `906568.53` / current `872849.17` / delta `-33719.36`（与 `eval/analysis/finance/attribution-001.json` reference 逐位一致，快照 7c966e9）。
- `make api-verify`：15/15 pass（A1~A9 旧契约回归 + A10a~A10d 分析新增）。
- `/analyze` 真链行为：hq_admin 成功路径恰四步、每步带 SQL（LIMIT + 2013 时间窗 + 角色谓词 `1 = 1` 为 hq 渲染）；branch_manager 受限角色第一步即 Guard 拦截（`reason_code=guard_blocked`），零 SQL 达执行器，同会话后续普通问数正常（turns=2）。
- 过程如实记录（R11-6/R11-7）：analysis-eval 在 HEAD 锁快照后裸跑曾失败（`missing_eligibility`——live agent 经 resolve_runtime_snapshot 绑 HEAD b95a9e9 而资格证据只登记在 7c966e9，前置门按设计诚实拒答，非缺陷）；经 `ATLAS_SNAPSHOT_SHA=7c966e9` 显式覆盖（ADR-0019 决策①③文档化补救）跑绿，再删除本会话自建的 `data/snapshots/b95a9e9.meta.json` 后裸跑复核 exit 0（最终报告即裸跑产物）。

**报告路径清单**：`eval/reports/b95a9e9.json`（make eval）、`eval/reports/analysis-b95a9e9.json`（make analysis-eval）、`eval/reports/e2e-acceptance-b95a9e9.json`（make e2e）、`eval/reports/api-acceptance-b95a9e9.json`（make api-verify）。
