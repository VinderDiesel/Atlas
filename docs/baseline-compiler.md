# Compiler-only 基线分析（Day 30）

> 结论一句话：**在自建 48 条金融 gold 覆盖的注册语义域内，确定性链零 LLM 全覆盖（48/48），
> 其中 44 条解析命中 + 4 条歧义诚实反问**。这条基线证明：先别上 LLM，先把语义层与确定性
> 编译器做扎实，注册域内的问题根本不需要生成式 SQL。
>
> 绑定：HEAD `7d48dcb`，快照 `data/snapshots/7d48dcb.meta.json`，主评测
> `eval/reports/7d48dcb.json`，分析产物 `eval/reports/baseline-compiler-7d48dcb.json`。

## 1. 口径与样本盘存

| 项 | 值 | 说明 |
|---|---|---|
| gold 总数 | 50 | 人工标注黄金集（不含 schema.json） |
| 金融段（gold-1xx） | 48 | 主评测对象：44 可解析 + 4 歧义 |
| 零售段（gold-0xx） | 2 | gold-001/047 历史对照（gmv 等指标未注册于金融语义层），runner 跳过计数，不混报 |
| 链路 | Planner → Compiler → Guard → Doris | 全确定性，零 LLM 参与（`eval/runner.py`） |
| EX 基准 | 固定快照锚定 result_hash | 执行结果 sha256 与 gold 锚定值一致（数据指纹跨 sha 复核未漂移） |

## 2. 实测结果（2026-09-03，HEAD 7d48dcb）

| 指标 | 结果 | 说明 |
|---|---|---|
| Plan Acc | **44/44** | metric/dimensions/time 与人工标注一致 |
| 歧义反问 | **4/4** | ambiguous 样本返回 ClarificationRequest，不猜 |
| EX | **44/44** | 全部与锚定 result_hash 一致（本轮 0 锚定、0 失败、0 执行错误） |
| 确定性链覆盖 | **48/48** | 解析命中 44 + 歧义反问 4 |

数字来源：`eval/reports/7d48dcb.json`（`make eval`）+ `eval/reports/baseline-compiler-7d48dcb.json`
（`make baseline`），均为脚本产物，无手工数字。

## 3. "有多少问题根本不需要 LLM"——分解

48 条金融样本按确定性链的处置方式分类：

```text
48 = 44 注册域内问句（语义层有权威定义，Planner/Compiler 确定性解析命中）
   + 4  标注歧义问句（返回澄清反问——这里本来就不该让 LLM 猜，
        用户澄清后仍走同一确定性链）
   + 0  需要"生成式猜测"才能答出的样本
```

- **44/44 解析命中**意味着：问句措辞与语义层同义词对齐时，图谱路由 + 确定性编译器
  的答案是零方差、可解释、可审计的——LLM 生成器在此域内没有增量价值，反而引入
  不确定性与越权风险。
- **4/4 诚实反问**是设计行为：歧义时不猜。LLM 在此处最多能辅助"把反问组织得更自然"，
  但"要不要反问"的决策仍由确定性策略层做出。
- 结论的诚实边界（不夸大）：48 条 gold 与语义层同源，覆盖的是**已注册语义域**。
  **未见问句 / 未注册指标 / 跨指标复合分析 / 新措辞**不在基线内——那是 Day 31-34
  RAG+LLM 候选生成器的对照实验对象（gold 样本将用于验证 LLM 路径能否通过
  **同一 Guard + 执行校验**后补齐注册域外问题，并以同一评测口径对比）。

## 4. 与后续策略实验的关系（Day 31-34）

compiler-only 基线是四策略对比（compiler-only / RAG+LLM / LoRA / LoRA+SC）的**锚点**：

- 任何 LLM 策略若在 48 条 gold 上达不到 48/48，说明生成器在破坏确定性链已解决的问题
  （回归测试意义）。
- LLM 策略的**唯一正当增量**应来自注册域外测试（Day 31 将显式构造 out-of-registry
  问句子集并分开报告），域内域外混报视为无效实验。
- 所有策略必须过同一安全网关（Guard），无一例外（任务清单 Day 34 红线）。

## 5. 复现

```bash
make eval       # 主评测 → eval/reports/<sha>.json（先确认 data/snapshots/<sha>.meta.json 存在）
make baseline   # 基线分析 → eval/reports/baseline-compiler-<sha>.json
```
