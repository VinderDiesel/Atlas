# ADR-0029：LLM 引擎服务化（候选路由 + 接地叙述 · 分级治理）

- 日期：2026-09-18
- 状态：**accepted（决策经用户逐条确认，2026-09-18：brainstorming 5 问定范围/后端/降级/叙述边界/契约 + 逐节确认 §1–§8）；实现未启动**（Phase 2 设计闭合，代码属未来批次，见「验证方式」与 `docs/design/dev-plan-0029-llm-engine-serving.md`；对外不得写「已实现/已上线」，N2）
- 相关：
  - **ADR-0012 推翻条件第 3 条**（「LLM 引擎需要服务化 → engine 参数开放（v2，本 ADR 不设计）」）——**本 ADR 即该条的兑现**；但裁定**不**全局开放 `engine`（见 ⑤），是对 0012 预期的**收窄修正**。
  - **ADR-0022**（HTTP 契约 v2「不开放 engine」）——本 ADR 以**加性可选字段**扩展 AskBody，不改既有 request/response 逐字形态，**不 bump 主版本**。
  - **ADR-0008**（LoRA 基座模型 / vLLM）、技术栈 §5（vLLM ≥0.6）——自托管后端来源。
  - **ADR-0026**（多步任务规划：固定四步模板已 delivered，**LLM 叙述明确列为未交付**）——本 ADR 决策 ④ 承接其叙述缺口。
  - **ADR-0028**（④a compute-then-stream 执行模型 A、STATE_SNAPSHOT 单一事实源）——叙述附在既有 payload，不新开流式通道。
  - **ADR-0003/0011**（只读 Guard、安全分层）——⑤ 硬约束 LLM 永不产 SQL、不新增执行通道（N3）。
  - 被改动的既有代码触点：`agent/generator.py`（可选注入 client-config）、`serving/api.py`（AskBody/AnalyzeBody + `_turn_payload` + 单例键）、`serving/auth.py`（`RoleSpec` 加性 `llm` 能力）、`frontend/src/api/types.ts`、`docs/GLOSSARY.md`（新术语 `narrative`）。

---

## 背景

Atlas 服务面（`/ask`·`/analyze`·`/analyze/stream`）当前**确定性默认 `engine=stub`**：Planner → Compiler → Guard → Doris，LLM 不参与线上查询路径（`serving/api.py:22`、ADR-0012/0022 明文）。LLM 仅在**评测期**经 [`Generator(engine="openai")`](../../agent/generator.py) 跑候选路由（`eval/compare_4way.py` 的 `rag-llm`），真实端点/未测项 README 已诚实标注。

「LLM 引擎服务化」正是 ADR-0012 显式**留白待裁**的 Phase 2 项。用户要求把它连同**自然语言叙述/洞察服务化**（0026 未交付块）一并闭合为一份可执行决策。

代码级事实（本 ADR 立论依据，均读码核实）：

| 事实 | 证据 |
|---|---|
| 生成器已支持 OpenAI 兼容 + stub 双引擎，输出 **Plan 候选**（非 SQL） | `agent/generator.py` `_chat`/`_generate_llm`（打 `{OPENAI_BASE_URL}/chat/completions`，`model/messages/temperature/max_tokens`） |
| 候选必过确定性关卡 `validate_plan_json`，不过即 refuse（不猜） | `agent/generator.py:57` |
| 自托管 vLLM **已按 OpenAI 兼容**接入并有降级先例 | `lora/infer.py:80`（`{endpoint}/v1/chat/completions`）；端点失败→**影子兜底回落确定性编译器**（`lora/infer.py:101`） |
| 候选链在图内但**注册域内不可达**（Planner 44/44 命中，`generate/validate` 仅 unmatched 时触发） | `agent/graph.py:40`、`node_generate:347` |
| HTTP 契约现 18 路径、`engine` 不对外、无 `extra=forbid` | ADR-0022/0028；`serving/api.py:10,22`；`AskBody:174` |

约束（引 AGENTS.md，不重述）：**N1**（无脚本数字不写）、**N2**（设计不写成已完成；不宣称对外协议兼容）、**N3**（只读 Guard 单一执行通道，被拒 SQL 不出网）、**N6**（评测绑定锁定快照 sha）、**N9**（密钥/token 走 env，不入 query/日志）、**§5**（新依赖须 ADR + 许可证 + 预算可承受）、**§10**（确定性优先，能用编译器就不用 LLM）。

---

## 备选方案

| 方案 | 优势 | 劣势 |
|---|---|---|
| **① Bolt-on 复用：薄策略层 + 复用 Generator + 新叙述模块 + 加性契约旗标（选定）** | 不动确定性内核；候选路由几乎=接线+策略；叙述是唯一真新增；两半各单一职责可独立测；契合「确定性优先/简洁/YAGNI」 | 后端选择/分级的抽象是内聚函数而非独立层，将来多引擎类型扩展需再抽缝 |
| ② 一等 `LLMGateway` 抽象（cloud/self 两实现 + 路由） | 接口更干净、演进性好 | **为尚不存在的第三类引擎预付复杂度**；重构已绿且带契约测试的 Generator 调用面，YAGNI |
| ③ 独立 LLM 代理 sidecar（进程级数据出境隔离） | 隔离最强 | 与「单机 + Compose MVP」预算冲突，运维面陡增，MVP 过重 |
| 全量开放 `engine` 参数给客户端自选后端 | 灵活 | **否决**：客户端可选后端直接破坏数据出境分级（可把含结果数值的叙述硬塞云 API），违反本 ADR 划界轴 |

---

## 决策

> 每项自成可独立推翻的粒度（沿 ADR-0016 惯例）。实现状态：全部**设计已定、代码未启动**（N2）。

### ① 后端接入统一「OpenAI 兼容 `chat/completions` 可移植子集」

云 API 与自托管 vLLM **收敛到同一线格式**（代码已两处独立如此用，见背景表）。后端差异退化为「选哪个 `base_url/key/model_name`」。

- **硬约束·可移植子集**：请求只用交集 `model / messages / temperature / max_tokens`；响应只读 `choices[0].message.content` + `usage.{prompt,completion}_tokens`。**禁用** `response_format`/结构化输出/function-calling/流式增量——云与 vLLM 在这些上行为不一致（否则「都兼容」是幻觉）。
- 统一 `base_url` 规范化写法（消除现状 `generator` 含 `/v1`、`lora` 不含的小不对称）。
- **按能力选模型，非全局一模型**：路由可用自托管 LoRA `atlas-sql-v1` 或云通用模型；**叙述**指向 vLLM 部署的通用 instruct 模型（另一 `model_name`）。

### ② 分级治理按「数据敏感度」划界（非操作类型）

由 intent 派生敏感度：
- `candidate-fallback → schema-only`：prompt 仅含问句 + 注册指标/维度名 + RAG 候选，**无结果数值** → **云或自托管皆合法**（选哪个由配置 `ATLAS_LLM_ROUTE_BACKEND`）。
- `narrative → result-bearing`：prompt 必含 TurnPayload 结果数字（业务数据出境）→ **只允许自托管**，云被**无条件排除**（无视配置）。

### ③ fail-closed 降级：叙述缺自托管 → 回落确定性模板，绝不静默升级云

`self_hosted_available = env 配置 ∩ 对 /v1/models 短超时探活（结果短时缓存，避免惊群）`。不可用（resolve 期）或调用中途失败/超时/非 JSON → 回落 0026 确定性模板叙述（带 `fallback` 与 `reason_code`），**不整请求报错、绝不改道云**。复用 `lora/infer.py` 影子兜底心智。

### ④ 叙述「接地数值校验器」：数字硬闸 + 因果软约束

- `ALLOW` **只从已算完的确定性 TurnPayload 生成**：遍历数值叶，登记规范化十进制 + 其格式化变体（千分位/定点/百分/万亿/货币取整），匹配判据 = **规范化值相等（容显示级四舍五入）**。
- 从叙述文本抽全部数字 token（正则含千分位/小数/负/百分/中文万·亿倍数 → 归一）；任一 ∉ `ALLOW` → `grounded=false` → **丢弃 LLM 文本发模板**（`violations` 进审计，不回塞用户文本）。
- 结构性非事实数（年份、ISO 时间戳、`snapshot_sha`/`semantic_sha` hex、`gpt-4o`/`v1` 内数字）**豁免事实校验，但须本身出现在 payload 元数据**。
- **推论（特性）**：想让某比率/占比被叙述，它**必须先是确定性 payload 字段**，否则被判越界回落——把「造数」压力挡回 Compiler/模板，N1 因此干净。
- **诚实边界**：校验器只兜数字与新数字；**定性/因果词无法客观验真**，靠 prompt 约束 + 响应 `grounded/model/tier` 标签让调用方知情，**不假装能验因果**。

### ⑤ 契约加性可选字段 + 服务端定后端（客户端不选 backend）

- AskBody/AnalyzeBody `+ llm: Literal["off","candidate-fallback","narrative"] = "off"`。**默认 `off` → 现有全部调用方/用例逐字不变**（黄金集、契约快照零 diff）。
- 后端/分级/是否允许全由服务端 `resolve_llm_backend` + RBAC 裁定；**客户端不能选 backend**。
- 响应 TurnPayload `+ 可选 narrative: {text, model, tier: "cloud"|"self_hosted"|"none", grounded: true, fallback: bool, reason_code?}`；`off`/未请求 → 字段缺席；`grounded=false` 文本**永不发货**（发模板）。
- **无新端点 → 契约 18 路不变**，`EXPECTED_PATHS`/`endpoints.ts` 不动。流式：叙述**不逐 token 流**（不破坏 compute-then-stream 模型 A），只附在 `STATE_SNAPSHOT` 完整 TurnPayload 内，reader/reducer 零改。

### ⑥ RBAC 承载于 `RoleSpec` 加性 `llm` 能力字段

给 `serving/auth.py` 的 `RoleSpec` 增 `llm` 能力位（如 `{route, narrate}` 子集，角色→能力映射是配置）。角色无该 intent 权限 → **显式 403**，不静默降级、不假装成功。

### ⑦ 可观测与预算：token 成本入 OTel + audit，敏感 prompt 不落日志

每次 LLM 调用一条 OTel span（复用 `observability/otel.py`）：`model/backend_host（不含 key）/tier/intent/grounded/violations 计数/tokens/latency/refusal·fallback reason`。**span 不落含结果数值的 prompt 原文**（只记计数与哈希）；密钥永不进日志/query（N9）。**LLM 成本记账与 Guard `Budget`（查询成本）明确分离**；速率仍走 `serving/ratelimit.py`，`max_tokens` 已由 Generator 限 800。

### ⑧ 评测纪律：确定性内核先测，真 LLM/叙述评测须实测才出数

- **无网络/无 GPU 可测层（安全内核，先红后绿）**：`llm_policy` 决策矩阵逐格（含「narrative 绝不选云」格）、接地校验器（新数字→拒、允许值格式变体→过、payload 外年份→拒）、降级接线（不可用→模板、永不碰云）、契约加性（`off` 逐字快照一致、narrative 字段规则、403 形状）。
- **真 LLM 层（活端点/GPU 才有数，绑定快照 sha）**：candidate 复用 `compare_4way --rag-engine openai`；叙述无「标准答案散文」→ 评 = 校验器通过率（机械）+ 定性人工抽检并标注，**绝不把散文准确率当数字瞎报**，无脚本即 `<待填写>`。自托管 vLLM 叙述评测无 GPU → **如实 blocked**（照 lora 行先例）。
- README/Known Limitations：实现未启动前标「设计已定，实现未启动」（N2）；追加限制「因果未硬验、真机评测待端点/GPU」（守 N4，不藏）。

---

## 理由

- **为什么 Bolt-on 而非 Gateway 抽象**：OpenAI 兼容客户端概念已在两处独立存在，「接口」无需新建；真正新的只有叙述模块与分级策略函数——各一职责即可，抽公共 Gateway 是为不存在的第三方引擎预付成本（§10 简洁/YAGNI）。
- **为什么按敏感度而非操作类型划界**：数据出境风险来自「prompt 里有没有业务数字」，与是路由还是叙述无关；路由输入天然低敏、叙述输入必然含值，敏感度轴让分级判定客观可验。
- **为什么不开放 `engine` 给客户端**：客户端选后端 = 客户端可决定把结果数据发去哪，直接架空 ② 的数据出境红线。意图旗标（表达"要不要"）+ 服务端定"用哪个后端"是安全与可用性的分界。
- **为什么数字硬闸 + 因果软约束**：数字可客观判真伪（N1 命门），因果/定性无法机器验真；与其造一个假装有能力的因果校验器（违反诚实），不如把能硬保证的（数字）做到位、不能的（因果）透明标注给调用方。
- **为什么 fail-closed 到模板**：Atlas 是「可信」平台；一个叙述拿不到就退回确定性措辞，远好于静默把数据发去云或整链失败（对齐 lora 影子兜底既有哲学）。

---

## 代价与限制

1. **只硬保证数字、不保证因果**：④ 校验器挡不住「数字都对但归因荒谬」的定性句；靠标签与人工抽检兜底——这是有意的诚实边界，非疏漏。
2. **无比率则不能叙述比率**：④ 推论要求所有可叙述数值先进 payload，等于把「让 LLM 算数」的诉求全部推回确定性层；换来 N1 干净，代价是模板/Compiler 需先把差值/占比算全。
3. **含 GPU 依赖**：自托管 vLLM 叙述评测与真机上线**受 GPU 阻塞**（个人预算单卡 24G，§2/元信息），无 GPU 阶段只能跑确定性可测层，真机数字 `<待填写>`。
4. **`RoleSpec` 加字段**：属 `serving/auth.py` 契约面扩动，须同步 `rbac_verify` 与 JWT 校验口径，否则新字段形同虚设。
5. **单例键扩展（承 ⑤ 服务端定后端）**：`serving/api.py` 的 `_agent()` 从按域单例改为按 `(domain, engine, tier)` 懒建多变体，会话状态按变体隔离——是这版对现有「按域单例」唯一的结构性触动，须测证明默认确定性变体行为逐字不变。
6. **可移植子集束缚**：① 禁用的 `response_format`/工具调用等，若某天路由候选需要约束解码降 refusal 率，本 ADR 的「统一兼容子集」前提需重估（见推翻条件）。
7. **本 ADR 不含代码**：与 ADR-0024 同类——只裁定位与边界，落地批次在 dev-plan，未跑「验证方式」前不得声称能力上线（N2）。

---

## 什么情况下应该推翻

- **①**：出现「可移植子集不足以把 refusal 率降到可接受」且经实测证实 → 引入约束解码/工具调用，届时统一兼容假设破裂，回到 ②/③ 重判后端形态（可能升 ② Gateway 或 ③ sidecar）。
- **②/③**：若合规评审明确「结果数值可发经认证的云 API」→ 敏感度划界轴松动，云可承载叙述；否则红线维持。
- **④**：若产品接受「叙述只做机械模板拼接」（放弃 LLM 表达力），④ 整块撤销，D 降级为模板增强——即回到 0026 现状。
- **⑤**：若确有多后端逐请求切换的**合规**需求（且分级由服务端仍强制），方考虑把意图旗标升级为受策略约束的 `engine` 提示；客户端自由选 backend 永久拒绝。
- **通用**：若任何 LLM 路径被发现构成绕过 Guard 的执行通道（N3），本 ADR 立即否决。

---

## 验证方式

> 实现批次与工作项见 `docs/design/dev-plan-0029-llm-engine-serving.md`。本 ADR 正确性的可执行判据：

- **不破坏既有契约（每批必跑）**：`make lint && make test`、`make ui-check` 全绿；
  - `llm=off` 路径对既有黄金集/契约快照**逐字一致**（回归断言）；
  - `test_api_contract_v2.py` 路径计数**仍 18**（无新端点）。
- **决策矩阵（②/③）**：`tests/test_llm_policy.py`（新）逐格断言 `resolve_llm_backend`：narrative+result-bearing→拒云；self-hosted 不可用→FALLBACK_TEMPLATE；无权角色→DENY。纯函数零网络。
- **接地校验器（④·安全内核）**：`tests/test_narrative_guard.py`（新）：模板 payload + 注入串，断言 新数字→`grounded=false`、格式化变体（千分位/万/百分）→过、payload 外年份/sha 外数字→拒；`grounded=false` 永不发货。
- **降级接线（③/⑧）**：stub 自托管不可用 → 返模板 + `fallback=true`；断言代码路径**无任何向云发叙述 prompt 的分支**（`grep -nE 'narrat.*OPENAI|result.*cloud' agent/narrative.py` 为空，N3/出境辅助判据，权威以测试为准）。
- **RBAC（⑥）**：`serving/rbac_verify.py` 覆盖 `llm` 能力位；无权置 `llm≠off` → 403。
- **真 LLM（⑧，须端点/GPU）**：candidate `make compare --rag-engine openai` 绑快照 sha 出 `eval/reports/`；叙述评测无 GPU 时以 **blocked 报告**形态入库，无数字不编（N1）。
- **诚实登记**：实现未启动前，README/KL 标「设计已定，实现未启动」；启动后按批次据实更新（N2/N4）。
