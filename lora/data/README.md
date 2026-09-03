# lora/data — 训练数据目录（数据源政策声明）

> 本目录是 LoRA/SFT 训练语料的唯一落盘位置。**任何进训练语料的样本必须能追溯到
> 人工确认记录**（AGENTS.md：人工确认后才可进 SFT）。

## 文件约定

| 文件 | 状态 | 说明 |
|---|---|---|
| `approved_pairs.jsonl` | 人工产出 | 人工复核确认的失败样本（`eval/failures/<category>/` 中 status 改为 `approved` 后导出；每行 `{"question", "answer", "source_sha", "reviewed_by"}`） |
| `pairs.jsonl` | 工具产出 | `lora/build_pairs.py` 过滤后的合规训练语料（勿手改，会被覆盖） |
| `pairs-<sha>.json` | 工具产出 | 每次构造的报告（计数 + 泄漏样本留证，绑定 git sha） |

## 红线（build_pairs.py 内置，测试锁定于 tests/test_build_pairs.py）

1. **gold 评测集逐字拒绝**：问句与 `eval/gold/` 任一 gold 问句相同 → 丢弃。
2. **模板级拒绝**：问句模板（指标同义词 → `<METRIC_n>`、数字 → `<NUM>` 归一）与
   任一 gold 模板相同 → 丢弃并记录（同模板衍生问句会在评测时泄漏答案形态）。
3. 去重 + 质量过滤（answer 形态按模式校验：mode=plan 默认——合法 Plan JSON，
   校验与 `agent/generator.validate_plan_json` 推理入口同口径；mode=sql 备用）。

## 当前状态（诚实声明）

- **语料为空是设计结论，不是缺陷**：当前唯一合规来源 = 人工确认的失败样本；
  gold 48 条与 ETL SQL 均不可作训练源（前者是评测集、后者非问答对）。
- 实测：`uv run python -m lora.build_pairs` → `[empty] 合规语料为空
  （approved 输入 0 条，0 通过过滤）`——空集由脚本断言，不是假设。
- 待 Day 31 LLM 实测产生失败样本 → `eval/failure_collect.py` 归类 → 人工确认 →
  导出 approved_pairs.jsonl → 本构造器出 pairs.jsonl → Day 37-38 训练。

## 反模式（禁止）

- ❌ 直接把 gold `expected_sql` 拷进训练语料（评测集泄漏）
- ❌ 手工编辑 `pairs.jsonl`（生成产物，会被覆盖）
- ❌ 把未经 `pending_review → approved` 流程的样本写入训练源
