# eval/bird/ —— 目录状态声明（2026-09-03，Day 32）

**定位：BIRD finance 段为公开集能力对照（仅参照，不与 gold 混报）。**

依据（README 5.2 / 第 11 节能力登记表）：
`bird/  # 公开集对照（BIRD finance 段，仅作参照，不与 gold 混报）`

## 计划（待生成器可用后执行）

- 数据集：BIRD dev finance 段（数据库 + question + gold SQL）。
- 前置条件（当前未满足，登记为阻塞）：
  1. text2sql 生成器可用——本机当前无任何 LLM 端点（侦察证据 2026-09-03：
     .env OPENAI_API_KEY 为空、LOCAL_VLLM_ENDPOINT(8000) 被无关服务占用、
     ollama/vLLM 未运行）；或 LoRA adapter 训练完成（Day 37-38，GPU 依赖）。
  2. 生成 SQL 必须过同一 Guard（只读红线），无一例外（Day 34 红线）。
- 评测口径：EX 与 gold 完全分开报告；分数仅作公开集参照，禁止外推为企业分数（N10）。

## 恢复方式

1. 在 `.env` 配置可用 LLM 端点（OPENAI_API_KEY / OPENAI_BASE_URL，或
   LOCAL_VLLM_ENDPOINT + LOCAL_MODEL_NAME），或完成 LoRA adapter。
2. 下载 BIRD dev finance 数据到本目录（数据体积大，入 .gitignore）。
3. 复用 `eval/rag_eval.py` 同口径评测链路 + Guard，产出
   `eval/reports/bird-<engine>-<sha>.json`。
