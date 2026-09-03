# eval/spider/ —— 目录状态声明（2026-09-03，Day 32）

**判定：Spider 不再新增接入。**

依据（README 5.2 目录注释，已在本轮重定位时更新）：
`spider/  # 历史对照（通用领域，与金融场景不匹配，不再新增）`

- Spider 是通用领域 text2sql 基准（Wikipedia/学术等 20 库），与 Atlas 的
  金融 FIBO 语义域不匹配；对照分数对企业场景无外推意义（N10 禁止混报）。
- Day 32 清单项「接入 Spider dev」为 TPC-DS 时代遗留任务，与上述既定决策矛盾，
  本次执行中消解：不下载数据、不建 harness、不产生对照分数。
- 自建 gold 集（eval/gold/，50 条人工标注）仍是主评测，不受影响。

公开集对照取向（如需要）：BIRD finance 段（见 ../bird/README.md）。
