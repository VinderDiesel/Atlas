# 同义词补充：「手续费」/「手续费率」成对注册（2026-09-08）

变更载体：`semantic/ossie/atlas_finance.ossie.yaml`（metrics 数量不变，仅 ai_context.synonyms）
发现来源：`eval/gold/paraphrase/pp-comm-2.json`（同义改写鲁棒性评测批次 e3f6bfc 引入的样本）
绑定提交：与本记录同批入 commit

---

## 1. 变更内容

| 指标 | 同义词（变更前） | 变更后 |
|---|---|---|
| `commission_revenue` | 佣金收入 / 佣金 / 手续费收入 | + **手续费** |
| `commission_rate` | 佣金率 / 佣金费率 | + **手续费率** |

## 2. 触发事实

`pp-comm-2` 问句「2013 年第二季度的手续费多少？」样本 note 假定「手续费」为已注册同义词，
实测 Planner 返回 `kind=unmatched`（未命中任何指标同义词）——即 `commission_revenue`
实际注册的是 `手续费收入`，不含裸 `手续费`。**是注册缺口，不是消歧错误。**

## 3. 为什么必须成对注册（影响面核心）

Planner 的口径消歧采用**最长命中**（既有规则，见 gold-122/148 互不为子串仍反问的裁定）。
若只补裸「手续费」：

- 问句「手续费率是多少」中，`手续费率` 未注册给任何指标 → 裸 `手续费` 命中
  `commission_revenue` → **把比率口径静默解析为金额口径**（错且不报错）。

因此同批给 `commission_rate` 补 `手续费率`，使两者在最长命中下互斥正确：

| 问句片段 | 命中 | 依据 |
|---|---|---|
| 「手续费收入」 | commission_revenue | 最长命中（4 字 > 3 字），与变更前一致 |
| 「手续费」 | commission_revenue | 本次新增 |
| 「手续费率」 | commission_rate | 本次新增（否则被裸「手续费」抢占） |
| 「佣金率」「平均每笔佣金」 | commission_rate / average_commission_per_trade | 不变 |

## 4. 影响面

- **黄金集 89 条**：`make eval --dry` 逐域复验与快照 `b933e20` 口径完全一致
  （finance Plan Acc 65/65、反问 5/5；retail 18/18、反问 1/1）——零回归。
- **同义改写集 15 条**：pp-comm-2 由 `unmatched` 转为解析成功（该样本预期本为 STABLE）。
- **报表/指标口径本身不变**：表达式、血缘、行级策略、FIBO 映射均未触碰；
  仅新增业务话术到既有权威定义的映射，不产生第二个权威定义（AGENTS.md N8 合规）。
- **下游检索**：`retrieval` 语料含指标同义词，词表扩张会影响 bm25/向量命中，
  已随 `make lint` 与既有 retrieval 契约测试验证无失败。

## 5. 复现

```bash
make lint                      # 语义层五类校验（含 governance 双向对齐）
make test                      # 契约测试（含 test_paraphrase 双语改写用例）
.venv/bin/python -m eval.runner --dry   # 双域 Plan Acc 与 b933e20 口径比对
```
