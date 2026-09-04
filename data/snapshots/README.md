# 数据快照

评测的**唯一可信基准**。所有 EX / result_hash 必须绑定某个具体 sha。

## 元数据格式

每个快照对应一个 `<sha>.meta.json`：

```json
{
  "sha": "<git rev-parse --short HEAD>",
  "created_at": "<ISO 8601, +08:00>",
  "source": "TPC-DI",  # 全库含 TPC-DS SF0.1 零售表时自动为 "TPC-DI + TPC-DS SF0.1"
  "data_range": "<tpcdi.trade.t_dts min~max, 实测>",
  "raw_size_bytes": "<data/raw/tpcdi 递归字节, 实测>",
  "row_counts": {
    "dwd.fact_trades": "<待填写>",
    "dwd.dim_account": "<待填写>"
  },
  "snapshot_ids": {
    "dwd.fact_trades": "<current snapshot id, 重跑即变>"
  },
  "generation_seconds": "<待填写>",
  "notes": "<生成环境、随机种子等>"
}
```

## 锁定脚本

`data/snapshot.py`（从仓库根 `uv run python -m data.snapshot`）：

- 枚举 Polaris 下全部表（pyiceberg scan count）并写入 `row_counts` 与 `snapshot_ids`
  （2026-09-04 起全库 29 表 = tpcdi 17 + dwd 12：金融 8 + TPC-DS 零售 4；`source`
  字段随 dwd 是否含零售表自动声明多源）
- git sha 只锁代码/清单版本；**snapshot id 锁数据文件版本**：任何重跑 loader 或
  DWD 加工都会改变它，必须重新锁快照
- 防呆：目标文件已存在时复核数据指纹，一致则 noop；不一致则拒绝覆盖
  （要求先 git commit 产生新 sha），防止旧评测引用被悄悄改写

## 规则

1. **大文件不入库**（已 gitignore），只提交 meta.json
2. 换快照 = 所有历史评测数字失效，必须重跑并更新 EVAL_REPORT
3. 被问“数字怎么来的”时，第一答案是“绑定到快照 sha xxx”

## 已锁快照与演进（2026-09-04 起记）

- `7d48dcb` / `b7e9ce7`：TPC-DI 单源 25 表（agent 批次与 filter 样本批次）
- `a207284` / `30b8344`：TPC-DI 单源同数据多锁（后续批次 HEAD 前进后重锁，
  指纹与 b7e9ce7 全一致，见对应 eval 批次 commit）
- `dc4f350`：TPC-DI + TPC-DS SF0.1 双源 29 表（P2b 零售装载后首次全库锁定）
- `92033c9`：双源 29 表同数据多锁（P5 零售锚定前重锁，指纹与 dc4f350
  全一致——中间 9cf70c7/b2a2e5c/92033c9 语义与样本批次未动数据）；零售 13 条
  gold 样本锚定绑定此 sha（eval/reports/92033c9.json）
- `1e5d35b`：双源 29 表同数据多锁（P6 双语样本批次：planner locale 化 + 英文
  样本 13 条，未动数据，指纹与 92033c9 全一致）；英文样本 13 条锚定绑定此 sha
  （eval/reports/1e5d35b.json，per-domain by_lang 分节）

换快照纪律：数据未变时 HEAD 前进仅重锁新 sha（同数据多锁先例），meta.json
逐个保留可追溯，README 记演进即可，不需重锚历史样本。
