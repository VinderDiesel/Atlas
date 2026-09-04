# Compiler-only 基线分析（Day 30，2026-09-05 双域分节版）

> 结论一句话：**注册语义域（目录即域声明）内，确定性链零 LLM 全覆盖——金融域 70/70
> （65 条解析命中 + 5 条歧义诚实反问）、零售域 19/19（18 条解析命中 + 1 条歧义反问）**。
> 这条基线证明：先别上 LLM，先把语义层与确定性编译器做扎实，注册域内的问题
> 根本不需要生成式 SQL。
>
> 绑定：HEAD `b933e20`，快照 `data/snapshots/b933e20.meta.json`，主评测
> `eval/reports/b933e20.json`，分析产物 `eval/reports/baseline-compiler-b933e20.json`
> （单域历史版见 git 历史 `baseline-compiler-7d48dcb.json`，gold 50 时代）。

## 1. 口径与样本盘存

| 项 | 值 | 说明 |
|---|---|---|
| gold 总数 | 89 | 人工标注黄金集，按域分目录（`schema.json`/`data-profile.md` 不计） |
| 金融域（gold-1xx） | 70 | `eval/gold/finance/`：65 可解析 + 5 歧义 |
| 零售域（gold-0xx） | 19 | `eval/gold/retail/`：18 可解析 + 1 歧义（TPC-DS SF0.1，2026-09-04 转正） |
| 链路 | Planner → Compiler → Guard → Doris | 全确定性，零 LLM 参与（`eval/runner.py`） |
| EX 基准 | 固定快照锚定 result_hash | 执行结果 sha256 与 gold 锚定值一致（快照 b933e20） |
| 域归属 | 报告 samples 自带 `domain` 键 | 2026-09-05 起 runner 注入；更早报告按 id 前缀回退兼容 |

两域口径完全并列（AGENTS.md N10：不混报）。确定性链覆盖 = 解析命中 + 歧义反问
（反问是确定性系统的诚实行为：不猜、不在样本外引入 LLM 猜测，澄清后再走同一
确定性链）。

## 2. 实测结果（HEAD b933e20，脚本产物）

### finance 域

| 指标 | 结果 | 说明 |
|---|---|---|
| Plan Acc | **65/65** | metric/dimensions/time（含 filters，ADR-0014）与人工标注一致 |
| 歧义反问 | **5/5** | ambiguous 样本返回 ClarificationRequest，不猜 |
| EX | **65/65** | 全部与锚定 result_hash 一致（0 失败、0 执行错误） |
| 确定性链覆盖 | **70/70** | 解析命中 65 + 歧义反问 5 |

### retail 域

| 指标 | 结果 | 说明 |
|---|---|---|
| Plan Acc | **18/18** | metric/dimensions/time 与人工标注一致 |
| 歧义反问 | **1/1** | ambiguous 样本返回 ClarificationRequest，不猜 |
| EX | **18/18** | 全部与锚定 result_hash 一致（0 失败、0 执行错误） |
| 确定性链覆盖 | **19/19** | 解析命中 18 + 歧义反问 1 |

数字来源：`eval/reports/b933e20.json`（`make eval`）+ `eval/reports/baseline-compiler-b933e20.json`
（`uv run python -m eval.baseline_compiler --sha b933e20`），均为脚本产物，无手工数字。

## 3. 结论与边界

- 双域各 0 样本需要生成式猜测——注册语义域内的确定性链先于 LLM 成立。
- **不推断域外泛化**：未见指标/复合分析/新措辞问句不在基线内，是 Day 31-34
  RAG+LLM 候选生成器的对照实验对象（gold-50 时代实测 44/44 持平，
  `rag-llm-openai-7d48dcb.json`，历史对照，不与当前轮混报）；LoRA 策略 blocked
  状态见 compare-4way 报告（adapter 训练完成前不编造数字）。
- 双语（zh/en）分节计数与样本明细见 `eval/gold/README.md`；分域 summary 口径
  见 `eval/runner.py` docstring；基线脚本 `eval/baseline_compiler.py` 对 summary
  与仓库 gold 盘存逐域校验，不一致即拒绝出报告。

## 4. 与后续策略实验的关系（Day 31-34）与复现

compiler-only 基线是四策略对比（compiler-only / RAG+LLM / LoRA / LoRA+SC）的锚点：
任何 LLM 策略若在注册域内达不到全量覆盖（当前 = 金融 70/70 + 零售 19/19），说明
生成器在破坏确定性链已解决的问题（回归测试意义）；LLM 策略的唯一正当增量应来自
注册域外测试并分开报告（域内域外混报视为无效实验）；所有策略必须过同一安全网关
（Guard）。

```bash
make eval        # 主评测 → eval/reports/<sha>.json（先确认 data/snapshots/<sha>.meta.json 存在）
make baseline    # 基线分析 → eval/reports/baseline-compiler-<sha>.json（默认当前 HEAD）
uv run python -m eval.baseline_compiler --sha b933e20   # 或指定历史报告 sha
```
