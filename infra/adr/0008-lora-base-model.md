# ADR-0008：LoRA 训练目标与底座模型选型（sql_v1 adapter）

- 日期：2026-09-03
- 状态：accepted
- 相关：ADR-0005（SQL 解析确定性优先）、Day 31（Generator = Plan 级候选架构）、
  `agent/generator.py`、`lora/`、pyproject `[project.optional-dependencies].ml`

---

## 背景

Day 37-38 需要落地「SQL Adapter」训练（四策略对比实验中的 LoRA 行）。训练前有
两个必须先定的架构问题：

1. **训练目标形态**：任务清单早期表述为「SFT pair（question → SQL）」，但 Day 31
   已落地架构是 **LLM/LoRA 只输出 Plan 候选 JSON，SQL 一律由确定性 Compiler 生成**
   （最小攻击面：Guard 只兜底 Compiler 产物）。若 LoRA 直接产 SQL，等于架起
   一条绕过 Compiler/Guard 的旁路，违反 Day 34 验收「所有策略 SQL 一律由
   确定性 Compiler 生成后过同一 Guard，无旁路通道」。
2. **底座模型与量化路径**：预算约束为「单卡 24G 起（仅 LoRA 阶段需要）」，
   需选 Apache 许可友好、24G 可微调的开放权重模型，并确定量化与训练依赖。

---

## 备选方案

| 方案 | 优势 | 约束 |
|---|---|---|
| **LoRA 训练目标 = Plan JSON**（与 Generator 推理同口径） | 与 Day 31 架构无缝一致；训练语料校验复用 `generator.validate_plan_json`（推理入口同一道门）；评测直接挂 rag_eval 链路 | 训练样本的 answer 需为结构化 JSON（标注成本略高于自然 SQL） |
| LoRA 训练目标 = SQL（任务清单早期字面） | 与 Spider 式生成器一致 | 违反「SQL 只由确定性 Compiler 生成」红线；需另建 SQL 校验/Guard 旁路；无法与 compiler-only 基线同口径对比 |
| 底座 Qwen2.5-7B-Instruct + QLoRA 4bit | 7B 规模在 24G 单卡可 QLoRA（nf4）微调（公共经验）；Qwen2.5 Apache-2.0 许可；中文/金融措辞能力均衡 | 需 bitsandbytes（新增依赖）；云上按量费用需记录 |
| 底座 Qwen2.5-3B-Instruct（全参/LoRA） | 更小更快、按量费用低 | 路由与格式跟随能力弱于 7B，作为 24G 不够用时的降级选项 |
| 底座 Qwen2.5-14B（QLoRA） | 容量更大 | 24G 单卡 QLoRA 紧张（公共经验上限附近），预算超约束；降级路径 0004 同型 |

---

## 决策

1. **训练目标 = 合法 Plan JSON**（metric/dimensions/time/top_n），校验与
   Generator 推理入口共用 `agent/generator.validate_plan_json`（Day 37 已提取为
   模块级函数）。语料形态见 `lora/data/README.md`（mode=plan）。
2. **底座 = Qwen/Qwen2.5-7B-Instruct**（Apache-2.0），QLoRA 4bit（bitsandbytes
   nf4）微调，adapter 名 `sql_v1`。24G 不足时降级 Qwen2.5-3B-Instruct
   （config 切换，不改变本 ADR 其余内容）。
3. **权重不入库**：adapter 权重/检查点落 `lora/weights/`（.gitignore 已有
   `lora/weights/` 与 `*.safetensors`）；入库的是训练配置（`lora/configs/`）、
   语料构造报告与训练报告（train-report.json 含 rank/alpha/样本数/耗时/GPU）。
4. **训练与评测闭环**：`make train`（语料过滤 → QLoRA 训练）→ vLLM 加载
   adapter 以 OpenAI 兼容端点 serve → `make rag-eval ENGINE=openai` /
   `make compare RAG_ENGINE=openai` 补全四策略 LoRA 行——与 RAG+LLM 同一条
   评测链路，无旁路。
5. **新增依赖**：pyproject ml extra 增补 `bitsandbytes`（QLoRA 量化必需；
   torch/transformers/peft/vllm 原已在 ml extra，技术栈表已锁 vLLM）。许可
   MIT（bitsandbytes），无商用限制。安装命令 `uv sync --extra ml`（仅在训练机
   执行，不装进日常开发环境）。

---

## 理由

1. **攻击面不变**：Plan 级训练使 LoRA 只是「路由候选的更强实现」，SQL 生成与
   Guard 路径与 compiler-only 完全一致——四策略可同口径对比（Day 34 验收）。
2. **语料与推理同门校验**：validate_plan_json 复用后，「训练语料合法但推理
   永远拒」的错位在构造期就被拦截（build_pairs mode=plan 测试锁定）。
3. **预算内最大容量**：24G 单卡 QLoRA 7B 是个人预算内的合理上限（3B 降级路径
   保留）；Apache-2.0 许可避免权重复用合规风险。
4. **可复现**：训练配置入库、语料报告绑定 sha、权重 gitignore——任何训练结果
   数字可追溯到配置 + 语料 sha + 训练报告，不满足即视为不可复现（AGENTS.md 9）。

---

## 代价与限制

| 风险 | 说明 | 缓解 |
|---|---|---|
| 云 GPU 按量费用 | QLoRA 7B 训练需数小时级 GPU 时长 | 按量计费，使用前记录预算；min_samples 门槛防止「几条例样本空跑」烧钱 |
| 未实测的训练路径 | 本机无 GPU（mac），train.py 训练分支无法本地验证 | 训练分支按最小实现写并标注「未实测」；前置检查（依赖/GPU/语料）失败即 exit 2，不装样子；实测在 GPU 就绪后执行 |
| 7B 单卡 24G 边缘 | 激活/序列长度可能吃紧 | max_len/微批次配置保守；config 降级到 3B 只改 base_model |
| Plan JSON 标注成本 | 人工确认失败样本需产出正确 Plan | answer 字段有 JSON schema 校验（validate_plan_json），标注错误在构造期拦截 |

---

## 什么情况下应该推翻

- **24G 实测 QLoRA 7B 无法稳定训练** → 切 Qwen2.5-3B-Instruct（config 级降级）
- **Plan 级训练在评测中表现不如预期（对比表 LoRA 行劣于 compiler-only 或无增益）**
  → 保留 Plan 架构但放弃 LoRA 路线，对比表如实记录（AGENTS.md N1 禁止美化）
- **架构回退到「LLM 直接产 SQL」**（违反本 ADR 前提）→ 本 ADR 随之失效并需新 ADR

---

## 验证方式

- [ ] `make train` 前置检查在无 GPU/无语料时 exit 2 且输出明确恢复指引（本机可验）
- [ ] 有 GPU + 合规语料时训练产出 adapter 与 train-report.json（含 rank/alpha/
      样本数/耗时/GPU 型号，无手写数字）
- [ ] vLLM serve adapter 后 `make rag-eval ENGINE=openai` 走通，报告标注
      adapter sql_v1（LoRA 行进入四策略对比表）
- [ ] 训练集与评测集零重叠（build_pairs 泄漏检测测试持续锁定）
