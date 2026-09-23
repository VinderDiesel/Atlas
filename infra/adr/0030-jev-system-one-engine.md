# ADR-0030：Jev System One 决策引擎接入（可插拔判别后端 · 加性扩展 ADR-0029）

- 日期：2026-09-19
- 状态：**proposed（设计已定，实现未启动——落地前须跑「验证方式」全部判据；对外不得写「已实现/已上线」，N2）**
- 相关：
  - **ADR-0029**（LLM 引擎服务化）——本 ADR 是其**备选方案 ②「一等抽象」在具体引擎类型出现后的兑现**，但**收窄**：不引入通用 `LLMGateway`，只为「非 OpenAI 兼容线格式」补一个**引擎类型维度** + 一个判别客户端协议。
  - **ADR-0029 ①**（OpenAI 兼容可移植子集）——**本 ADR 是该条的第 124 行「推翻条件」触发**：Jev 的线格式（`POST /v1/systemone`，输出 Choice/Score/Noul）**不落在** `chat/completions` 可移植子集内。按 ADR-0029 明文，此时须「回到 ②/③ 重判后端形态」——本 ADR 即该重判结论。
  - ADR-0003 / 0011（只读 Guard、安全分层）——Jev **永不产 SQL、不新增执行通道**（N3）。
  - ADR-0029 ②③⑥⑦（敏感度划界 / fail-closed / RBAC / 可观测）——**全部原样继承，不放松**。
  - AGENTS.md **N1**（无脚本数字不写）、**N2**（设计不写成已完成）、**§5**（新依赖须 ADR）、**§10**（确定性优先）、**§11**（先写 ADR 再实现）。

---

## 背景

### 1. Jev 是什么（外部事实，非本项目实测）

TypeSafe AI（创始人 Diogo Almeida，前 OpenAI，RLHF/ChatGPT 核心作者）2026-09-15 发布的 **System One Model**。工程上须注意的口径边界：

| 项 | 公开口径 | 引用纪律 |
|---|---|---|
| 输出 | 强类型原语 `Choice`（选项 + 概率）/ `Score`（标定打分）/ `Noul`（陈述为真 0~1 概率），附**校准置信度** | 可采信为 API 形态 |
| 不做 | 自由文本生成、代码生成、精确算术/计数/日期排序 | 可采信为能力边界 |
| 端点 | `POST {base}/v1/systemone`，单请求含 `state` + `questions` | 可采信为接入坐标 |
| 延迟/成本 | 70–500ms、输入 $0.042/M token、输出免费；193.6×/444.6× | **仅为 TypeSafe 自有 workflow 数据，禁外推**（N1）；本 ADR 与实现**不引用任何这类数字** |
| 语言 | 英文最佳，CJK 建议自测 | 须自测，不得假定可用 |
| 部署 | early access，仅托管 API，未公开权重 | **数据须离开自有系统** —— 影响本 ADR 决策 ② |

### 2. 本项目当前状态（读码核实，commit 5570c26）

| 事实 | 证据 |
|---|---|
| 主链路 plan→retrieve→validate→execute→explain **零 LLM 调用** | `agent/graph.py`、`agent/planner.py` 全确定性 |
| 已有「薄策略层」决定用不用/用哪个后端，纯函数零 I/O | `agent/llm_policy.py:142` `resolve_llm_backend` |
| 后端选择被压缩为「选哪个 base_url/key/model_name」，**无引擎类型维度** | `Backend` 枚举仅 `CLOUD/SELF_HOSTED/NONE`（`llm_policy.py:30`）；`BackendEndpoint` 三字段（`:55`） |
| 两个客户端接缝均为 OpenAI 兼容 callable/protocol | `agent/generator.py:53` `ChatFn`；`agent/narrative.py:36` `ChatClient.complete` |
| 重排为确定性三信号词典序，**无置信度**，owner 信号当前恒常数 | `retrieval/rerank.py:83`（`OWNER_PRIORITY` 单 owner，`:11` 自陈无区分度） |
| 澄清判定为「同义词子串命中必须唯一」，**无置信度** | `agent/planner.py:26`、`agent/state.py`（TurnState 30+ 字段无置信度字段） |
| 失败归因为字符串子串 if-else，`understanding` 为垃圾桶类 | `eval/failure_collect.py:41-50` |
| 叙述已有数字硬闸 `verify_grounded` | `agent/narrative_guard.py:141` |

### 3. 为什么值得接（问题陈述，非收益承诺）

Atlas 主链路不调 LLM，所以 Jev 的价值**不在替代生成**，而在**替换那批「用硬规则硬扛、扛不住就反问」的判断点**：

- `retrieval/rerank.py` 三信号同层后只能退回「原召回序」——**没有语义裁决能力**；且 docstring 记录了加权求和被全局信号污染的真实教训（Recall@1 34/44）。
- `planner.py` 的歧义判定只有「命中唯一/多命中」二分，无法表达「我很确信」与「我勉强猜的」之差。
- `eval/failure_collect.classify` 的 `understanding` 桶混装了路由失败、同义词缺失、时间解析失败三种根因，**归因错 → 飞轮数据错**。

这些位置的共同特征是：**答案范围已知、需要的是判断而非生成、且当前没有校准过的置信度**——正是 System One 型模型的适用域。

---

## 备选方案

| 方案 | 优势 | 劣势 |
|---|---|---|
| **① 加性「引擎类型」维度 + 独立判别协议（选定）** | 不动 ADR-0029 的敏感度划界/fail-closed/RBAC；引擎可插拔且**默认 off 时逐字不变**；Jev 线格式不被硬塞进 `chat/completions` | `LlmConfig`/`BackendEndpoint` 加字段，须同步 env 装配与契约面 |
| ② 通用 `LLMGateway` 抽象（cloud/self/jev 三实现） | 概念最干净 | **ADR-0029 备选 ② 已否决**（YAGNI）；本次只多**一类**引擎，重构成本高于收益 |
| ③ 把 Jev 包装成 OpenAI 兼容假端点（适配器进程） | 零改 Atlas 代码 | 假端点仍需一套契约与部署；且 Jev 输出本就不是 chat 语义，包装层会丢失概率/置信度结构（**核心价值**） |
| ④ 不做，等产品真需求 | 零成本 | 无法量化评估 Jev 是否真能改善判断点——项目正处于「先写评测再写实现」的批次纪律下 |

---

## 决策

> 每项自成可独立推翻的粒度（沿 ADR-0016/0029 惯例）。实现状态：**设计已定，代码未启动**（N2）。

### ① 新增正交的「引擎类型」维度，不引入通用 Gateway

在 `agent/llm_policy.py` 现有 `Backend`（物理形态：云/自托管/none）之上，**加一层正交的 `EngineKind`**：

```
EngineKind = CHAT_COMPLETIONS | SYSTEM_ONE
```

- `Backend` 回答「数据发去哪」（出境红线，ADR-0029 ② 不变）；
- `EngineKind` 回答「用什么线格式说话」。
- **二者正交**：Jev 既可能自托管也可能云托管，因此引擎类型**不参与**敏感度划界，**不放松**任何既有红线。
- 不建 `LLMGateway`、不建 `engines/` 包——只加枚举 + 一个协议 + 一个实现，符合 §10 简洁。

### ② 数据出境：Jev 视为「第三方云托管」，受 ADR-0029 ② 同等约束

**这是本 ADR 最重要的安全决策。**

Jev 目前**仅有托管 API，无本地部署、未公开权重**，即使用户自建代理，**prompt 数据仍离开自有系统**。因此：

- Jev 后端**按 `Backend.CLOUD` 对待**（新增 `Backend.JEV` 但继承云的全部出境约束），**不得**被当作自托管豁免。
- `narrative` 意图（result-bearing，含业务结果数字）**无条件禁止**路由到 Jev —— 与 ADR-0029「narrative 绝不选云」同一条红线，**不因 Jev 成本低而开口子**。
- Jev **只允许承载 `schema-only` 类判断**（候选重排、意图归类、失败归因分类——prompt 内无业务数值）。
- 若将来 Jev 提供可自托管权重，须**新 ADR** 重判，不在本 ADR 授权范围内。

### ③ 新增 `SystemOneClient` 协议 + `JevEngine` 实现（可插拔接缝）

新模块 `agent/jev_engine.py`，定义：

```python
class SystemOneClient(Protocol):
    def decide(self, state: str, questions: dict[str, Any], model: str) -> tuple[dict[str, Any], dict[str, int]]:
        """返回 (decisions, usage)。失败/超时须抛异常，由上层回落（不静默返回空）。"""

class JevEngine:
    """将 Jev 的 Choice/Score/Noul 收敛为 Atlas 可消费的判别结果（含校准置信度）。"""
```

- 生产实现 `HttpSystemOneClient`：`POST {base_url}/v1/systemone`，仅用 `state` + `questions` + `model` 三字段（**可移植子集原则的延续**，ADR-0029 ① 精神不因换线格式而放弃）。
- 输出收敛为 `JevDecision`（不可变 dataclass）：`choice` / `confidence` / `scores` / `probs`，**置信度原样透传，不二次加工**（校准是 Jev 的卖点，Atlas 不得破坏它）。
- **fail-closed 继承 ADR-0029 ③**：端点失败/超时/非 JSON/未知原语 → 抛异常由上层回落**当前确定性逻辑**（不是回落模板文本，见 ④）。

### ④ 「可切换」的语义：确定性逻辑是唯一无条件可用路径

用户要求「随时切换原逻辑和 Jev 构建的逻辑」。落地口径：

- 开关：环境变量 `ATLAS_ENGINE_KIND`（`chat_completions` 默认 / `system_one`），经 `LlmConfig.from_env` 装配，**不经客户端**（承 ADR-0029 ⑤：客户端不选后端）。
- **默认值必须保持 `chat_completions`**，且 `llm=off` 时**任何引擎都不触网** → 既有黄金集/契约快照逐字不变（ADR-0029 验证方式首条）。
- **Jev 只做「建议 + 置信度」，不做「决策」**：判别结果的消费方式由调用点决定，且**每个调用点必须保留原有确定性分支作为 fallback**。即：Jev 不可用 → 完全退回当前行为，**零行为回归**。
- 由 ② 得：`narrative` 路径**即使开关设为 `system_one` 也必须继续走 `chat_completions`**（因 Jev 禁用于 result-bearing）。此约束由 `resolve_llm_backend` 硬保证，不由调用点自觉。

### ⑤ 首批只接一个调用点（最小可用，避免过度设计）

按收益/风险比，首批**只接 `retrieval/rerank.py` 的候选重排**：

- 输入 `schema-only`（问句 + 候选指标的 name/synonyms/description），**无业务数值**，合规。
- 现有三信号词典序**保留为 fallback**；Jev 仅在有有效置信度时覆盖前 K 名。
- 消费 `business_terms.yml` 中**设计了却未被消费**的 `confidence` 字段（`semantic/synonyms/*.yml`）——该字段本就是为重排设计的，Jev 让它第一次有真实语义。
- `planner` 澄清、`failure_collect` 归因**列为后续批次**，本 ADR 不授权（先证明单点有效，§10 简洁）。

### ⑥ 置信度进入既有结构，不新造流通管道

- `ClarificationRequest.candidates`（`agent/state.py:213`）已是既有字段：后续批次的澄清置信度**填在这里**，不改 HTTP 契约形状。
- 首批重排的置信度**只进内部返回值 + OTel span**，**不进 TurnPayload**（避免在未验证前扩契约面）。
- **绝不把置信度当准确率报**（ADR-0029 ⑦ 同理）：置信度是「模型有多确定」，不是「答案正确概率」。

### ⑦ 可观测与成本：复用 ADR-0029 ⑦ 管道，成本与 Guard Budget 分离

- 每次 Jev 调用一条 OTel span：`engine_kind=system_one` / `backend_host`（不含 key）/ `model` / `primitive`（choice|score|noul）/ `confidence` 分桶 / `tokens` / `latency_ms` / `fallback_reason`。
- **复用 `gen_ai.token_cost`**（`observability/otel.py`），**不新建成本指标**；与 Guard 查询 `Budget` 保持分离（ADR-0029 ⑦）。
- span **不落 prompt 原文**（即使 schema-only），密钥永不入日志/query（N9）。

### ⑧ 评测纪律：先补评判用例，真机数字须实测

- **无网络可测层（先红后绿）**：决策矩阵补 Jev 格（narrative+system_one → 仍走 chat_completions，**绝不放行 Jev**）；`SystemOneClient` fake 注入测收敛与置信度透传；端点失败 → 回落确定性逻辑断言。
- **真机层（须 Jev 端点）**：重排效果须用 `eval/retrieval_eval` 同口径重跑并绑定快照 sha，产出 `eval/reports/retrieval-rerank-<sha>-jev.json`；**无端点则以 blocked 形态入库，无数字不编**（N1，照 lora 行先例）。
- **CJK 须自测**：Jev 官方称英文最佳，中文重排效果**不得假定**，须在评测中单独分片报告。

---

## 理由

- **为什么是「引擎类型维度」而不是 Gateway**：ADR-0029 否决 Gateway 的理由是「为尚不存在的第三类引擎预付复杂度」。现在第三类**已经存在**（Jev 是真金白银要接的引擎），但只需**一个枚举 + 一个协议 + 一个实现**即可容纳——上 Gateway 仍是过度设计。
- **为什么必须触发 ADR-0029 的推翻条件而非绕开**：Jev 线格式客观不落在 `chat/completions` 可移植子集内。若强行用 ③ 适配器包装，会丢失 `Choice` 的概率分布与校准置信度——**那恰恰是接 Jev 的唯一理由**。诚实的做法是按 ADR-0029 明文重判，而不是假装兼容。
- **为什么 Jev 按云对待**：项目合规声明明确排除雇主真实数据（N7），而 Jev 托管 API 意味着数据出境。成本低、延迟低都**不能**成为放松出境红线的理由——安全优先级（§10 #2）高于性能（#6）。
- **为什么首批只接重排**：这是唯一同时满足「schema-only 合规」「已有确定性 fallback」「有现成评测口径（`retrieval_eval`）」三个条件的调用点。先在最能证伪的地方证伪。
- **为什么置信度不进契约**：ADR-0029 的教训是契约面扩展代价高（须同步 `EXPECTED_PATHS`/前端/契约测试）。未经实测验证的信号不应先污染契约。

---

## 代价与限制

1. **Jev 能力边界即 Atlas 的边界**：Jev 不做精确算术/计数/日期排序（官方口径），故它**不能**用于任何涉及确定性计算的判断（Guard 成本估算、时间解析**不得**交给它）。
2. **语言风险**：英文最佳，中文效果未验证；Atlas 主战场是中文问句，**这是最大不确定性**，须在评测中单独分片。
3. **early access 依赖**：模型版本（`jev-1.13.0`）、API 形态、价格均可能变；本实现须把模型名与端点全走 env（N9），不硬编码版本假设。
4. **新增一类出网依赖**：即便 schema-only，仍是新增数据出境面与运维面；须在 README 登记表（§11）与 Known Limitations 显式登记。
5. **不做 gateway 的代价**：将来第四类引擎出现时仍需再次加枚举——这是刻意的 YAGNI 取舍，记为技术债。
6. **`LlmConfig` 加字段**：属契约面扩动，须同步 env 装配、测试与文档，否则开关形同虚设。
7. **本 ADR 不含代码**：未跑「验证方式」前不得声称能力上线（N2）。

---

## 什么情况下应该推翻

- **②**：若 Jev 提供可自托管权重且经合规确认 → 可解锁 result-bearing 承载，回到 ② 重判；否则红线维持。
- **①**：若出现第四类非 OpenAI 兼容引擎 → 本维度设计不足以容纳，届时评估升 Gateway。
- **⑤**：若重排实测（绑 sha）证明 Jev 未优于现有词典序 → **首批调用点撤销**，本 ADR 退化为「协议保留、无调用点」。
- **通用**：若任何 Jev 路径被发现构成绕过 Guard 的执行通道（N3），或 Jev 被用于产出任何未实测数字（N1），本 ADR 立即否决。

---

## 验证方式

- **不破坏既有契约（每批必跑）**：`make lint && make test` 全绿；
  - `llm=off` 与 `ATLAS_ENGINE_KIND=chat_completions`（默认）下，既有黄金集/契约快照**逐字一致**；
  - `test_api_contract_v2.py` 路径计数**仍为当前值**（无新端点）。
- **决策矩阵（新格）**：`tests/test_llm_policy.py` 增断言：`narrative` + `ATLAS_ENGINE_KIND=system_one` → 仍解析为 `chat_completions` 自托管，**绝不出现 Jev 端点**。
- **Jev 客户端（无网络）**：`tests/test_jev_engine.py`（新）：fake `SystemOneClient` 注入 → 断言 Choice 概率透传、置信度不被改写、未知原语显式失败、端点异常 → 抛出让上层回落。
- **可切换性（核心用户诉求）**：断言开关切换后**默认路径行为逐字不变**；Jev 不可用 → 完全退回原确定性逻辑（与 lora 影子兜底同构）。
- **真机（须端点）**：`eval/retrieval_eval` 同口径重跑，绑快照 sha 出 `eval/reports/`；CJK 分片单独报告；无端点以 blocked 形态入库（N1）。
- **诚实登记**：实现未启动前 README/KL 标「设计已定，实现未启动」；追加限制「Jev 仅托管、中文未验证、因果不硬验」（守 N4，不藏）。
