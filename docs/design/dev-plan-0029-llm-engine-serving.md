# 开发计划：LLM 引擎服务化（候选路由 + 接地叙述 · 分级治理）

- 日期：2026-09-18
- 状态：**设计已定，实现未启动**（全部批次待执行；对外不得写「已实现/已上线」，N2）。
- 范围：把 ADR-0012 推翻条件第 3 条留白的「LLM 引擎服务化」连同 ADR-0026 未交付的「NL 叙述」一并落地——契约加性 `llm` 意图旗标 + 服务端分级路由（云 OpenAI 兼容 / 自托管 vLLM）+ 接地数值叙述校验器 + fail-closed 降级 + RBAC + 可观测 + 评测。**无新端点，HTTP 契约路径数维持 18**。
- 依据：ADR-0029 决策 ①–⑧（本计划的每条判据以编号引用，不复制正文）；产物契约 = ADR-0022（v2）/ADR-0026（`AnalysisPayload` 17 键）；执行模型 = ADR-0028 ④a（compute-then-stream 模型 A，STATE_SNAPSHOT 单源）；栈锁定 = AGENTS.md §5；降级哲学 = `lora/infer.py` 影子兜底。
- 发现契约缺口只登记为旁附注（§1.3），裁定须回 ADR-0029，不在本页拍板。

## 本页纪律（与 dev-plan-0027/0028 同源）

1. **不转述**：判据以编号引用（「0029 决策④」「0012 推翻第 3 条」「0028 模型 A」），不复制 ADR 正文；
2. **计数三要素**：HTTP 契约端点数写「公式 + 撰写时实测 + 重测时点」——本批**维持 18**（加性字段不改路径集），落 §5 判据；
3. **不含工期估算**（AGENTS.md N1）：只给工作项数与主观量级（S/M/L，非承诺）；
4. **不拍板未裁定项**：GPU 依赖项（真自托管 vLLM 评测）在未获硬件前保持 blocked，不臆测数字；
5. **停批条件优先于进度**：命中即停该批回改 ADR，不得「先开工后补」；
6. **确定性内核优先**：B0–B2（纯函数 + 校验器 + 降级）无网络无 GPU 即可全绿，安全价值最高，排最前。

---

## 0. 起点实测（2026-09-18，本页撰写时逐条核实）

服务化不是白地：生成器、OpenAI 兼容接入、降级先例、契约面均已就位。

| 事实 | 实测证据 | 复测命令 |
|---|---|---|
| Generator 双引擎 + 确定性关卡已存在 | `agent/generator.py:57`（`validate_plan_json`）、`_chat`（OpenAI 兼容） | `grep -n "def validate_plan_json\|chat/completions" agent/generator.py` |
| 自托管 vLLM 已按 OpenAI 兼容接入 + 影子兜底先例 | `lora/infer.py:80,101` | `sed -n '80,104p' lora/infer.py` |
| 候选链在图内、注册域内不可达（诚实口径） | `agent/graph.py:40`、`node_generate:347` | `grep -n "def node_generate\|allow_candidate" agent/graph.py` |
| HTTP 契约 18 路径、`engine` 不对外、无 `extra=forbid` | `serving/api.py:10,22,174`；`test_api_contract_v2.py` | `uv run pytest tests/test_api_contract_v2.py -q` |
| 响应 turn 已带 `usage/engine/latency_ms` | `serving/api.py:326-329` | `grep -n '"engine"\|"usage"' serving/api.py` |
| 角色注册表存在（加性 `llm` 能力的落点） | `serving/auth.py:71 ROLE_DIRECTORY`、`RoleSpec:63` | `grep -n "class RoleSpec\|ROLE_DIRECTORY" serving/auth.py` |

## 1. 唯一合法实施序与前置门

**B0 → B1 → B2 → B3 → B4 → B5 → B6**（测试先行；任一停批条件命中即停并回改 ADR-0029）。

- **B0 前置**：无（起点事实已就位）。
- **B1 前置**：B0 红测试定义决策矩阵与校验器契约。
- **B2 前置**：B1 决策函数可用（供叙述模块调用）。
- **B3 前置**：B1（路由后端解析）+ Generator 注入点设计定。
- **B4 前置**：B2/B3 后端能力绿（叙述与候选可返）。
- **B5 前置**：B4 接线通。
- **B6 前置**：B0–B5 全绿；真机评测**受端点/GPU 门约束**（GATE-LLM）。

### 1.3 旁附注（发现即登记，不擅自拍板）

- **N-α**：`narrative` 字段进 `openapi.json`/`types.ts` 若触发既有响应 schema 严格断言，按 0022 契约口径处置，不改既有 18 端点 request/response 形态。
- **N-β**：单例键 `(domain, engine, tier)` 扩展若与 ADR-0020 会话持久化（按域 thread）交互，先登记，须回 0029/0020 论证，不在本页改会话隔离语义。
- **N-γ**：GATE-LLM——B6 真自托管 vLLM 叙述评测需 GPU（§5 单卡 24G）；无硬件时该批以 **blocked 报告**入库，**不编数字**（N1）。

## 2. 批次卡（目标 / 前置 / 工作项 / 测试先行 / 停批条件）

### B0 · 契约与策略测试（红先行，无实现）
- **目标**：把「决策矩阵 + 接地校验 + `off` 逐字兼容 + 路径仍 18」锁成可机读断言。
- **工作项（S）**：新增 `tests/test_llm_policy.py`（矩阵逐格：narrative→拒云、不可用→FALLBACK、无权→DENY）、`tests/test_narrative_guard.py`（新数字→拒、格式变体→过、payload 外年份→拒）、契约加性回归（`llm=off` 对既有快照逐字一致 + `EXPECTED_PATHS` 仍 18）。
- **测试先行**：B0 **即**测试，无实现 → 预期红。
- **停批**：若决策矩阵任一格与 0029 ②/③ 无法对应 → 停，回 ADR 补裁定。

### B1 · `resolve_llm_backend` 纯函数 + RBAC 能力位（绿）
- **目标**：0029 ②③⑥ 的策略内核，零网络。
- **工作项（M）**：`agent/llm_policy.py`（intent→data_class、敏感度选后端、cloud-for-narrative 排除、探活缓存封装、fail-closed→FALLBACK_TEMPLATE）；`serving/auth.py` `RoleSpec` 加 `llm` 能力 + `rbac_verify` 覆盖。
- **测试先行**：B0 矩阵测试转绿。
- **停批**：出现「需读结果数据才能判后端」的耦合（破坏纯函数性）→ 停。

### B2 · 接地叙述校验器 + 叙述模块 + fail-closed（绿，无网络）
- **目标**：0029 ③④——安全内核。
- **工作项（L）**：`agent/narrative_guard.py`（`ALLOW` 构造：遍历 payload 数值 + 格式变体归一；文本抽数校验）；`agent/narrative.py`（受限 prompt → 注入式 OpenAI 兼容 client（测试用 fake）→ 校验 → `grounded`/回落模板）。
- **测试先行**：B0 校验器测试转绿 + 降级接线（不可用→模板、永不碰云）。
- **停批**：若 `grounded=false` 仍能经某分支发货 LLM 文本 → 停（N1 红线）。

### B3 · Generator 注入 client-config + 候选路由接线（绿）
- **目标**：0029 ①——按能力选 base_url/key/model，可移植子集。
- **工作项（M）**：`agent/generator.py` 加可选注入（缺省读 `.env`，向后兼容）；候选路由用 B1 解析的后端；诚实口径不变（注册域内仍不可达，仅 unmatched 触发）。
- **测试先行**：注入点单测（无网络，fake client）+ 候选仍过 `validate_plan_json`、SQL 仍 Compiler 产（N3）。
- **停批**：若为兼容子集外的字段（`response_format` 等）→ 停（违 ①）。

### B4 · serving 接线 + 契约加性 + 前端消费（绿）
- **目标**：0029 ⑤ + 单例键触动（代价 5）。
- **工作项（L）**：`serving/api.py` AskBody/AnalyzeBody `+ llm`（默认 off）、`_agent()` 键扩 `(domain,engine,tier)`、响应 `+ narrative` 块、无权 403；`frontend/src/api/types.ts` + `fixtures/analysis.ts` + 组件按 `grounded/fallback` 呈现；三处契约（`api.py`/`test_api_contract_v2.py`/`endpoints.ts`）**仍 18**。
- **测试先行**：`off` 逐字回归 + narrative 字段规则 + `grounded=false` 不发货 + ui-check。
- **停批**：若 `off` 路径产生任何行为 diff → 停（破坏向后兼容）。

### B5 · 可观测与预算（绿）
- **目标**：0029 ⑦。
- **工作项（S）**：OTel span（model/tier/intent/grounded/tokens/refusal·fallback），敏感 prompt 只记计数/哈希；LLM 成本与 Guard `Budget` 分离；密钥不入日志/query（N9）。
- **测试先行**：`tests/test_otel.py` 家族加断言（span 属性齐 + 无密钥/无结果数值落日志）。
- **停批**：若发现 prompt 原文（含数值）入 span/日志 → 停。

### B6 · 评测 + 文档回写（受 GATE-LLM 约束）
- **目标**：0029 ⑧ + 诚实登记（N2/N4）。
- **工作项（M，部分 blocked）**：真 candidate 评测（`make compare RAG_ENGINE=openai` 绑快照 sha）；叙述机械评测（校验器通过率，黄金叙述集）；自托管 vLLM 叙述评测**无 GPU → blocked 报告**；`docs/GLOSSARY.md` 补 `narrative`；README/KL 按已落地批次据实从「设计已定」更新（**仅更已实现部分**，未跑的不写已完成）。
- **测试先行**：报告脚本产物入库 `eval/reports/<sha>.json`，数字只引自脚本。
- **停批**：若需在无端点/GPU 下产出一个"看起来合理"的叙述质量数字 → 停（N1 绝对红线）。

## 5. 收口判据（全批完成后逐条实测，任一不满足不算 delivered）

1. `make lint && make test && make ui-check` 全绿；契约路径**仍 18**（三处相等）。
2. `llm=off` 对既有黄金集/契约/前端快照**逐字一致**（回归 0 diff）。
3. `resolve_llm_backend` 矩阵**全绿**，含「narrative 强制自托管、cloud 排除」格。
4. 接地校验器：**新数字→`grounded=false`→发模板**；`grounded=false` 文本无任何发货路径。
5. 降级：自托管不可用/失败/非 JSON → 回落模板 + `fallback=true`，代码路径无「叙述发云」分支（grep 辅助 + 测试权威）。
6. N3：全程 LLM 不产 SQL、SQL 仍 Compiler→Guard；被拒不出网。
7. N9：span/日志/query 无密钥、无含结果数值的 prompt 原文。
8. N2/N1：README/KL 只登记**已实测**批次；真机数字缺失处 `<待填写>` 或 blocked 报告，无脚本不编数。
