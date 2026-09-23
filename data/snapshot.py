#!/usr/bin/env python3
"""数据快照锁定：把当前 Iceberg 仓库状态固化为 data/snapshots/<sha>.meta.json。

评测的唯一可信基准（AGENTS.md 术语表：Snapshot = 固定数据快照，绑定 git sha；
N6：禁止把 data/snapshots/ 外的数据库当作评测基准）。meta.json 字段约定见
data/snapshots/README.md 模板，本脚本在其基础上补充每表 Iceberg snapshot id
（git sha 只锁代码/清单版本，snapshot id 锁数据文件版本：任何重跑 loader 或
DWD 加工都会改变它）。

防呆：目标文件已存在时先复核"数据指纹"（行数/snapshot id/数据范围/raw 大小），
一致则提示沿用并退出 0；不一致则报错要求先 git commit（产生新 sha）再锁，
防止旧评测引用的快照被悄悄覆盖。

用法（从仓库根执行）：
    uv run python -m data.snapshot          # 锁当前状态
    uv run python -m data.snapshot --check  # 只复核已锁快照，不写文件
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from data.identity import SNAPSHOT_DIR, git_short_sha
from data.loader import build_catalog, open_table

REPO_ROOT = Path(__file__).resolve().parent.parent
# data_range 口径：主事实表交易时间戳的实际覆盖范围（实测，不取模板示例）
RANGE_TABLE = ("tpcdi", "trade")
RANGE_COLUMN = "t_dts"
# TPC-DS SF0.1 零售装载落位 dwd 的表（data/tpcds_loader.py）；dwd.dim_date 已被
# TPC-DI 金融占用，零售日期表用 TPC-DS 原生名 date_dim（物理表名由 dataset
# source 决定，dataset 名仅是 SQL 别名）。dwd 出现这些表即全库为双源。
RETAIL_DWD_TABLES = ("store_sales", "date_dim", "dim_item", "dim_store")
# raw_size_bytes 口径：TPC-DI 源数据目录（Batch1 已加载；Batch2 存在未加载，Batch3 空）
RAW_DIR = REPO_ROOT / "data" / "raw" / "tpcdi"

TZ = timezone(timedelta(hours=8))  # 契约要求：时间戳显式 +08:00


def measure_row_counts() -> dict[str, dict[str, int]]:
    """枚举 Polaris 下全部 namespace/表，返回 {ns: {table: row_count}}。

    行数口径 = pyiceberg 全表 scan().count()（读 manifest 汇总，不读数据文件），
    与 Doris 侧 COUNT(*) 实测一致（dwd 表已抽验对齐）。
    """
    catalog = build_catalog()
    result: dict[str, dict[str, int]] = {}
    for (ns,) in sorted(catalog.list_namespaces()):
        counts: dict[str, int] = {}
        # list_tables 返回完整 identifier（(namespace, table) 二元组），取表名
        for _, table_name in sorted(catalog.list_tables(ns)):
            table = open_table(catalog, ns, table_name)
            counts[table_name] = table.scan().count()
        result[ns] = counts
    return result


def measure_snapshot_ids() -> dict[str, dict[str, int]]:
    """枚举各表 current snapshot id（数据文件版本指纹，重跑即变）。"""
    catalog = build_catalog()
    result: dict[str, dict[str, int]] = {}
    for (ns,) in sorted(catalog.list_namespaces()):
        ids: dict[str, int] = {}
        # list_tables 返回完整 identifier（(namespace, table) 二元组），取表名
        for _, table_name in sorted(catalog.list_tables(ns)):
            table = open_table(catalog, ns, table_name)
            ids[table_name] = table.metadata.current_snapshot_id
        result[ns] = ids
    return result


def measure_data_range() -> str:
    """返回主事实表交易时间覆盖范围 "min~max"（ISO，本地无时区语义）。"""
    import pyarrow.compute as pc

    catalog = build_catalog()
    table = open_table(catalog, *RANGE_TABLE)
    column = table.scan(selected_fields=(RANGE_COLUMN,)).to_arrow()[RANGE_COLUMN]
    return f"{pc.min(column).as_py().isoformat()}~{pc.max(column).as_py().isoformat()}"


def raw_size_bytes(raw_dir: Path | None = None) -> int:
    """返回 TPC-DI 源目录递归字节数；显式目录不存在时抛 FileNotFoundError。

    raw_dir 为空时沿用 RAW_DIR（含 Batch1~3），显式目录用于隔离工作区复用源数据。
    """
    directory = RAW_DIR if raw_dir is None else raw_dir
    if raw_dir is not None and not directory.is_dir():
        raise FileNotFoundError(f"原始数据目录不存在或不是目录：{directory}")
    return sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())


def build_meta(notes: str | None = None, *, raw_dir: Path | None = None) -> dict:
    """现场测量并返回 meta；raw_dir 覆盖原始数据目录，测量/IO 异常向上传播。"""
    row_counts = measure_row_counts()
    has_retail = set(row_counts.get("dwd", {})) & set(RETAIL_DWD_TABLES)
    return {
        "sha": git_short_sha(),
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
        # 全库多源时如实声明（零售 4 表随装载进 dwd，见 RETAIL_DWD_TABLES）
        "source": "TPC-DI + TPC-DS SF0.1" if has_retail else "TPC-DI",
        "data_range": measure_data_range(),
        "raw_size_bytes": raw_size_bytes() if raw_dir is None else raw_size_bytes(raw_dir),
        "row_counts": row_counts,
        "snapshot_ids": measure_snapshot_ids(),
        "generation_seconds": None,  # 数据加载耗时未单独计时（历史会话完成）
        "notes": notes
        or (
            "全库锁定：TPC-DI Batch1 + DWD 加工（金融 8 表）"
            "+ TPC-DS SF0.1 零售装载（data/tpcds_loader.py，dwd 4 表）"
            if has_retail
            else "全库锁定：TPC-DI Batch1 + DWD 加工（金融 8 表）"
        )
        + "；row_counts 口径 pyiceberg scan count；snapshot_ids 为各表 current "
        "snapshot id；data_range 口径 tpcdi.trade.t_dts（金融主事实表；零售销售窗口见 "
        "eval/gold/retail/data-profile.md）；raw_size_bytes 口径 data/raw/tpcdi 递归字节。",
    }


def fingerprint(meta: dict) -> dict:
    """提取用于防呆复核的数据指纹（排除 created_at/notes 等描述性字段）。"""
    return {
        "sha": meta["sha"],
        "data_range": meta["data_range"],
        "raw_size_bytes": meta["raw_size_bytes"],
        "row_counts": meta["row_counts"],
        "snapshot_ids": meta["snapshot_ids"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只复核已锁快照，不写文件")
    args = parser.parse_args()

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    sha = git_short_sha()
    target = SNAPSHOT_DIR / f"{sha}.meta.json"

    meta = build_meta()
    if target.exists() or args.check:
        if not target.exists():
            print(f"[error] 无已锁快照 {target.name}，请去掉 --check 执行锁定。", file=sys.stderr)
            return 2
        locked = json.loads(target.read_text(encoding="utf-8"))
        if fingerprint(locked) == fingerprint(meta):
            print(f"[noop] 数据指纹与 {target.name} 一致，快照仍有效。")
            return 0
        # 指纹不一致：--check 报告漂移；锁定模式拒绝覆盖旧快照（防止旧评测引用被悄悄改写）
        reason = f"数据指纹已变（如重跑 loader/DWD 加工）但 git sha 未变（{sha}）"
        print(f"[error] {reason}，旧评测引用的是 {target.name}。", file=sys.stderr)
        if not args.check:
            print("       请先 git commit 产生新 sha 再重新锁快照。", file=sys.stderr)
        return 2

    target.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[done] 快照已锁定: {target.relative_to(REPO_ROOT)}")
    for ns in sorted(meta["row_counts"]):
        for table_name in sorted(meta["row_counts"][ns]):
            count = meta["row_counts"][ns][table_name]
            snapshot_id = meta["snapshot_ids"][ns][table_name]
            print(f"  {ns}.{table_name}: rows={count} snapshot={snapshot_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
