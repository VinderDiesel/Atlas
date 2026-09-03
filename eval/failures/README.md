# eval/failures/ —— 失败样本归集（Day 35）

**规则（AGENTS.md）：失败样本先归集、人工确认后才可进 SFT。任何失败样本不得
在未人工确认时直接进入训练数据或用于演示性结论。**

## 目录结构

```text
eval/failures/
├── categories.json      # 错误分类 schema（理解/生成/安全/权限/超时/执行/结果异常）
├── <category>/          # 自动归类输出（failure_collect.py），status=pending_review
│   └── <sha>-<sample-id>.json
├── _rejected/           # 人工确认不可用的样本归档（保留审计轨迹）
└── README.md
```

## 流程

1. `uv run python -m eval.failure_collect --report eval/reports/<sha>.json`
   读评测报告，把 ex=fail / error / refused（非歧义）样本归类到分类目录。
2. 人工复核每个样本：确认 category 归因，需要修复则修改语义层/提示词/实现。
3. 确认可作为训练对（question → 正确 Plan JSON，形态与 generator 推理
   同口径，ADR-0008）→ 追加 `lora/data/approved_pairs.jsonl`。
4. 确认不可用 → 移入 `_rejected/`（不删除，留审计）。

## 当前状态（2026-09-03，HEAD 7d48dcb）

**空集**：compiler-only 主评测（`eval/reports/7d48dcb.json`）48 条金融样本
0 失败（EX 44/44、歧义反问 4/4、0 执行错误、0 拒绝）——失败样本尚未产生，
归集机制已就绪；LLM 策略实测（Day 31，端点就绪后）预期产生首批失败样本，
届时本目录开始累积并驱动 Day 41 数据飞轮。
