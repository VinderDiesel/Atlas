# 同义词候选区（非权威）

本目录存放飞轮归纳器产出的同义词提议（ADR-0027 决策 ②）。

**本目录内容不是权威源**——`load_locale_synonyms` 只读 `semantic/synonyms/<locale>.yml`，
不扫描本子目录。候选须经人工确认后追加进对应 `locale` 文件才生效。

## 人工落源流程

1. 审阅本目录下的 `syn_*.json` 候选文件（每条含 `term`、`suggest_for_metric`、`source`、`question`）
2. 判断该措辞是否确实应作为指定 metric 的同义词
3. 若通过：把 `term` 追加进 `semantic/synonyms/<locale>.yml` 对应 section
4. 运行 `make lint` 确认结构校验通过
5. 运行 `make eval` 确认评测回归无破坏
6. 提交 Git（`semantic(synonyms): 追加同义词 <term> → <metric>`）

## 派生索引重建（判据 5）

同义词 Git 变更后，`retrieval/` 的向量索引需重跑既有 embedding 流程重建。
`retrieval/` 是**派生索引**（ADR-0015），永远从 Git 权威源重建，不存在运行时直写接口。
结构不变量：`load_locale_synonyms` 只读固定文件名（`_LOCALE_FILES` 注册表），
不扫描 `_candidates/`——候选不经过人工落源就无法被解析器命中。
