# 值域别名候选区（非权威）

本目录存放飞轮归纳器产出的值域别名提议（ADR-0027 决策 ②）。

**本目录内容不是权威源**——`VALUES_DIR.glob("*.json")` 非递归，不扫描本子目录。
候选须经人工确认后追加进对应 `semantic/values/<model>.<field>.json` 的 `aliases` 映射才生效。

## 人工落源流程

1. 审阅本目录下的 `val_*.json` 候选文件（每条含 `term`、`source`、`question`）
2. 确定该措辞应归属哪个维度值域（结合 question 上下文推断 field）
3. 若通过：把 `term` 追加进对应 `semantic/values/<model>.<field>.json` 的 `aliases` 映射
   （ADR-0016：机器生成值本体 + 人工追加别名，不改值本体）
4. 运行 `make lint` 确认值域校验通过
5. 提交 Git（`semantic(values): 追加值域别名 <term> → <field>`）

## 派生索引重建

值域 Git 变更后，`retrieval/` 的向量索引需重跑既有 embedding 流程重建。
`retrieval/` 是派生索引（ADR-0015），永远从 Git 权威源重建。
结构不变量：`check_value_profiles` 使用非递归 glob，不扫描 `_candidates/`。
