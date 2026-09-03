# eval/bird/ —— 目录状态声明（判定记录，2026-09-03 更新）

**判定：BIRD finance 段不再新增接入（与 Spider 同判，ADR-0014 记录）。**

本判定仲裁一处自相矛盾：v0.1 release-notes（docs/release-notes-v0.1.md §4 行 3）
状态列写「判定历史对照，不新增」，而本文件旧版正文登记了「待 text2sql 生成器
可用后执行」的完整计划——两处冲突以本判定记录为准（README 是唯一事实源，
release-notes-v0.1.md 是 v0.1 定格历史文档，不修改）。

## 判定依据（2026-09-03）

1. **无 text2sql 生成器且架构不对齐**：Atlas 的 Generator（agent/generator.py）
   只产出 Plan 候选（问句 → Plan），SQL 一律由确定性 Compiler 生成（ADR-0012 同
   口径）；BIRD 对照需要问句 → SQL 直出能力的引擎，与确定性优先架构无契合点。
2. **数据与工程成本无收益**：BIRD dev finance 数据体积大、需下载建库；在无
   生成器前提下投入不产生可解释收益。
3. **外推无意义**：公开集分数对企业场景无外推意义（AGENTS.md N10 禁止混报）；
   自建 gold 集（eval/gold/，主评测）不受影响。

## 恢复路径（推翻本判定需新 ADR）

1. 出现真实 text2sql 生成器并过同一 Guard（只读红线，无一例外）——如
   LoRA adapter 训练完成（GPU 依赖，KL #19）或引入直出 SQL 的 LLM 端点。
2. 下载 BIRD dev finance 数据到本目录（数据体积大，入 .gitignore）。
3. 复用 `eval/rag_eval.py` 同口径评测链路 + Guard，产出
   `eval/reports/bird-<engine>-<sha>.json`；EX 与 gold 完全分开报告。

## 历史注记

- 2026-09-03（Day 32）旧版正文曾登记执行计划（数据集 / 前置条件 / 评测口径），
  前置条件①「text2sql 生成器可用」当时侦察证据：.env OPENAI_API_KEY 为空、
  LOCAL_VLLM_ENDPOINT(8000) 被无关服务占用、ollama/vLLM 未运行。
- 2026-09-03 稍后 openai Plan 端点就绪并实测（`rag-llm-openai-7d48dcb.json`，
  44/44 Plan Acc），但该端点是 **Plan 生成器而非 text2sql**，BIRD 阻塞判定不变。
