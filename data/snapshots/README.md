# 数据快照

评测的**唯一可信基准**。所有 EX / result_hash 必须绑定某个具体 sha。

## 元数据格式

每个快照对应一个 `<sha>.meta.json`：

```json
{
  "sha": "<git rev-parse --short HEAD>",
  "created_at": "<ISO 8601, +08:00>",
  "source": "TPC-DI",
  "data_range": "2004-07-01~2006-07-01",
  "raw_size_bytes": "<待填写>",
  "row_counts": {
    "dwd.fact_trades": "<待填写>",
    "dwd.dim_account": "<待填写>"
  },
  "generation_seconds": "<待填写>",
  "notes": "<生成环境、随机种子等>"
}
```

## 规则

1. **大文件不入库**（已 gitignore），只提交 meta.json
2. 换快照 = 所有历史评测数字失效，必须重跑并更新 EVAL_REPORT
3. 面试时如果被问"数字怎么来的"，第一答案是"绑定到快照 sha xxx"
