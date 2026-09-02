# 数据快照

评测的**唯一可信基准**。所有 EX / result_hash 必须绑定某个具体 sha。

## 元数据格式

每个快照对应一个 `<sha>.meta.json`：

```json
{
  "sha": "<git rev-parse --short HEAD>",
  "created_at": "<ISO 8601, +08:00>",
  "source": "TPC-DI",
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
- git sha 只锁代码/清单版本；**snapshot id 锁数据文件版本**：任何重跑 loader 或
  DWD 加工都会改变它，必须重新锁快照
- 防呆：目标文件已存在时复核数据指纹，一致则 noop；不一致则拒绝覆盖
  （要求先 git commit 产生新 sha），防止旧评测引用被悄悄改写

## 规则

1. **大文件不入库**（已 gitignore），只提交 meta.json
2. 换快照 = 所有历史评测数字失效，必须重跑并更新 EVAL_REPORT
3. 面试时如果被问"数字怎么来的"，第一答案是"绑定到快照 sha xxx"
