# paraphrase 改写鲁棒性样本集（格式定版）

- owner：agent（评测侧；人工标注 + Agent 起草候选，人工裁定）
- 定版：2026-10-14（ADR-0032 M0-T3）；本文件是该目录格式的**唯一事实源**
- 纪律：条数不写死（以 `ls eval/gold/paraphrase/pp-*.json | wc -l` 当日输出为准）；
  一切数字由 `make paraphrase` 脚本产物回填（AGENTS N1）

## 定位

主评测（`eval/gold/` 主集）只覆盖逐字标注问句；本集用「同一业务意图的多种自然语言
改写」喂给确定性 Planner，测量两件事：

1. 注册同义词范围内的稳定性（改写后仍正确解析）；
2. **超出口吻（OOV）时的真实掉落率**——这是 ADR-0032 概念层的主要考卷。

样本随人工标注演进；文件名 `<id>.json`，加载 glob `pp-*.json`（与
`eval/paraphrase_eval.py` 同一约定）。

## 字段表（定版）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | ✓ | 唯一标识；与文件名一致（`<id>.json`） |
| `base` | string | ✓ | 所属 base 样本；同一 base 的多条改写共享同一人类意图，报告按 base 分节 |
| `intended_metric` | string | ✓ | 人类意图的指标名（语义模型注册名；用于比对与 drop 归因） |
| `question` | string | ✓ | 自然语言问句（评测输入，逐字喂给 Planner） |
| `expected_metric` | string | ✓ | 期望 Planner 输出的指标 |
| `expected_dimensions` | string[] | ✓ | 期望维度；无维度写空数组 `[]` |
| `expected_time` | string \| null | ✓ | 期望时间（`TimeSpec.value` 口径，如 `"2013Q2"`）；无时间约束写 `null` |
| `note` | string | ✓ | 改写说明与标注依据（谁改自哪条、用了什么词汇） |
| `oov` | bool | 可选 | **新增（ADR-0032 ⑥）**：`true` = 表达意图的词未注册（OOV 改写）；缺省 `false` |
| `holdout` | bool | 可选 | **新增（ADR-0032 ⑥）**：`true` = 参与最终报告、**不参与 curation**；缺省 `false` |

前 8 个字段为既有字段（**语义不变**）；`oov` / `holdout` 为可选新字段，缺省 `false`，
对既有样本（定版时 13 条；条数以当日输出为准，下同）**零行为变化**（兼容性由缺省值
保证，不改写既有文件）。

## 合法样例（OOV 样本完整 JSON）

```json
{
  "id": "pp-oov-1",
  "base": "gold-101",
  "intended_metric": "total_trade_value",
  "question": "2013 年第二季度盘口金额累计多少？",
  "expected_metric": "total_trade_value",
  "expected_dimensions": [],
  "expected_time": "2013Q2",
  "oov": true,
  "holdout": false,
  "note": "「盘口金额」未见于注册同义词与概念别名（OOV 改写）；期望 ADR-0032 解析缝经概念层兜底解析为 total_trade_value"
}
```

（该样例为格式演示；实际样本须经人工标注后落入本目录，不得以此演示条充数。）

## curation 纪律（三条，硬约束）

1. **curation（S3 别名）只碰 dev**：`holdout != true` 的样本才可作为"新增概念别名"
   的证据来源（哪条失败样本支持哪个词→概念，写进提交说明）；`holdout == true`
   的样本**不看、不碰、不改写**。
2. **holdout 划分人工裁定**：OOV 样本的 dev/holdout 划分由人工裁定（ADR-0032 H2
   门禁）；Agent 只起草候选与划分建议，不代签；holdout 不参与 curation。
3. **最终报告 dev/holdout 分节、不合并汇报**：两部分的 drop 率分别报告；
   **holdout 数字 = 泛化最终结论**（ADR-0032 M3-T4），合并汇报会把"练过的词"
   与"没练过的词"搅在一起，失去结论效力。

## 与评测实现的关系

- `eval/paraphrase_eval.py` 当前只消费 `id / base / question / intended_metric /
  expected_metric / expected_dimensions / expected_time`（`note` 为人类阅读字段）；
  `oov` / `holdout` 的解析、分节报告与 `--tag` 产物后缀由 **ADR-0032 M3-T1**
  实现——格式行为以该卡测试为准对照本文件。
- 本文件先于实现定版（ADR ⑥"先冻结基线再动代码"、AGENTS §11"先写评测"）：
  实现与本文件的差异必须先改本文件并说明理由，不得在代码里暗改口径。
