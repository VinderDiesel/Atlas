"""TPC-DS dsdgen .dat -> Iceberg dwd 全量加载器（零售域 SF0.1，P2）。

数据链路（对应 AGENTS.md 目录职责 data/raw -> Iceberg DWD）：
    data/raw/tpcds/sf01/*.dat（dsdgen 生成，scripts/setup_tpcds.sh）
        --解析--> Iceberg V2 表（Polaris catalog 'atlas'，namespace 'dwd'）

装载 4 张表（表名与金融 dwd 无冲突——dwd.dim_date 已被 TPC-DI 金融占用，零售日期表
用 TPC-DS 原生名 date_dim；列名/列序保留 TPC-DS 官方 DDL 原生）：
    store_sales.dat   -> dwd.store_sales（23 列事实表）
    date_dim.dat      -> dwd.date_dim（28 列日期维度；语义模型内 dataset 名
                         date_dim/dim_date 只是别名，SQL FROM 用 source 物理名）
    item.dat          -> dwd.dim_item（22 列商品维度）
    store.dat         -> dwd.dim_store（29 列门店维度；官方列名 s_tax_precentage
                         拼写错误保留，与 tpcds.sql 一致）

.dat 解码规则（实测确认，勿改）：
- 物理行恒为 N+1 个 token（N 列 + 行尾终止 '|'）；NULL = 空 token（TPC-DS
  nulls.c 行级随机位图，SF0.1 实测 ~9% 行含随机列 NULL，ss_item_sk/
  ss_ticket_number/ss_net_profit 0% 空）。不得用 rstrip('|') 预处理——会剥掉
  行尾连续 '|' 造成列错位假象。
- 日期文本 ISO 'YYYY-MM-DD'；金额 decimal；空 token -> NULL。

SF0.1 口径（与 scripts/setup_tpcds.sh 头注释一致）：社区扩展档，实测行数
date_dim 73,049 / item 18,000 / store 12 / store_sales 240,485
（≈ SF1 2,880,404 的 1/12，dsdgen 离散化）。维表保持 SF1 基数（品类/州值域
完整，但门店 12 家全 TN 州 2 城——低 SF 地理集中的如实边界）。

行为契约（同 data/loader.py）：
- 幂等重建：默认逐表 drop-if-exists -> create -> append，任何一步失败即停。
- 复用 data/loader.py 基建（build_catalog/open_table/_iceberg_schema 等），
  不执行 TPC-DI 的 specs；本文件为装载的权威列定义（--emit-ddl 可对照）。

用法：
    uv run python -m data.tpcds_loader        # 装载 4 张表 + 数据探查
    uv run python -m data.tpcds_loader --table store_sales
    uv run python -m data.tpcds_loader --probe  # 只做探查（不装载）

"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pyarrow as pa
from pyiceberg.io import load_file_io

from data.loader import (
    D2,
    DATE,
    I32,
    STR,
    TableSpec,
    _col,
    _iceberg_schema,
    _pa_schema,
    build_catalog,
    open_table,
)

# ---------------------------------------------------------------------------
# dwd 表定义：列序 = .dat 物理列序（与 TPC-DS 官方 DDL/tpcds.sql 逐列核对）
# ---------------------------------------------------------------------------


def _specs() -> list[TableSpec]:
    """4 张零售 dwd 表（TPC-DS SF0.1）。required 列 = 官方 DDL not null + 实测 0% 空。"""
    return [
        # ---- 事实表（最大表）-----------------------------------------------
        TableSpec(
            name="store_sales",
            file="store_sales.dat",
            columns=[
                _col("ss_sold_date_sk", I32, False, "销售日期外键（julian，指向 dim_date）"),
                _col("ss_sold_time_sk", I32, False, "销售时间外键"),
                _col("ss_item_sk", I32, True, "商品外键（指向 dim_item）"),
                _col("ss_customer_sk", I32, False, "客户外键（customer 未装载，保留 NULL）"),
                _col("ss_cdemo_sk", I32, False, "客户人口特征外键"),
                _col("ss_hdemo_sk", I32, False, "家庭人口特征外键"),
                _col("ss_addr_sk", I32, False, "地址外键"),
                _col("ss_store_sk", I32, False, "门店外键（指向 dim_store）"),
                _col("ss_promo_sk", I32, False, "促销外键"),
                _col("ss_ticket_number", I32, True, "票号（一次结账交易标识，订单去重键）"),
                _col("ss_quantity", I32, False, "销售件数"),
                _col("ss_wholesale_cost", D2(7), False, "批发成本单价"),
                _col("ss_list_price", D2(7), False, "牌价单价"),
                _col("ss_sales_price", D2(7), False, "成交单价"),
                _col("ss_ext_discount_amt", D2(7), False, "明细折扣额"),
                _col("ss_ext_sales_price", D2(7), False, "明细销售额（量×价）"),
                _col("ss_ext_wholesale_cost", D2(7), False, "明细批发成本"),
                _col("ss_ext_list_price", D2(7), False, "明细牌价额"),
                _col("ss_ext_tax", D2(7), False, "明细税额"),
                _col("ss_coupon_amt", D2(7), False, "优惠券抵扣额"),
                _col("ss_net_paid", D2(7), False, "实付净额"),
                _col("ss_net_paid_inc_tax", D2(7), False, "含税实付净额"),
                _col("ss_net_profit", D2(7), False, "净利润（可负，已扣成本）"),
            ],
        ),
        # ---- 维度表（小表，SF0.1 保持 SF1 基数）-----------------------------
        TableSpec(
            name="date_dim",
            file="date_dim.dat",
            columns=[
                _col("d_date_sk", I32, True, "日期代理键（julian 天数）"),
                _col("d_date_id", STR, True, "日期 ID（16 位词典编码）"),
                _col("d_date", DATE, False, "实际日期"),
                _col("d_month_seq", I32, False, "月序列号"),
                _col("d_week_seq", I32, False, "周序列号"),
                _col("d_quarter_seq", I32, False, "季序列号"),
                _col("d_year", I32, False, "年"),
                _col("d_dow", I32, False, "星期几（0=周日）"),
                _col("d_moy", I32, False, "月（1-12）"),
                _col("d_dom", I32, False, "日（1-31）"),
                _col("d_qoy", I32, False, "季（1-4）"),
                _col("d_fy_year", I32, False, "财年"),
                _col("d_fy_quarter_seq", I32, False, "财季序列号"),
                _col("d_fy_week_seq", I32, False, "财周序列号"),
                _col("d_day_name", STR, False, "星期名（Monday..Sunday）"),
                _col("d_quarter_name", STR, False, "季度名（1998Q1）"),
                _col("d_holiday", STR, False, "是否假日（Y/N）"),
                _col("d_weekend", STR, False, "是否周末（Y/N）"),
                _col("d_following_holiday", STR, False, "次日是否假日（Y/N）"),
                _col("d_first_dom", I32, False, "当月首日 sk"),
                _col("d_last_dom", I32, False, "当月末日 sk"),
                _col("d_same_day_ly", I32, False, "去年同日 sk"),
                _col("d_same_day_lq", I32, False, "上季同日 sk"),
                _col("d_current_day", STR, False, "是否当前日（Y/N）"),
                _col("d_current_week", STR, False, "是否当前周（Y/N）"),
                _col("d_current_month", STR, False, "是否当前月（Y/N）"),
                _col("d_current_quarter", STR, False, "是否当前季（Y/N）"),
                _col("d_current_year", STR, False, "是否当前年（Y/N）"),
            ],
        ),
        TableSpec(
            name="dim_item",
            file="item.dat",
            columns=[
                _col("i_item_sk", I32, True, "商品代理键"),
                _col("i_item_id", STR, True, "商品 ID（16 位词典编码）"),
                _col("i_rec_start_date", DATE, False, "有效起始日（Type-2）"),
                _col("i_rec_end_date", DATE, False, "有效截止日（空 = 当前版本）"),
                _col("i_item_desc", STR, False, "商品描述（词典文本）"),
                _col("i_current_price", D2(7), False, "现价"),
                _col("i_wholesale_cost", D2(7), False, "批发成本"),
                _col("i_brand_id", I32, False, "品牌 ID"),
                _col("i_brand", STR, False, "品牌名"),
                _col("i_class_id", I32, False, "类别 ID"),
                _col("i_class", STR, False, "类别名"),
                _col("i_category_id", I32, False, "大类 ID"),
                _col("i_category", STR, False, "商品大类（10 类，43 行空——生成器词典边界）"),
                _col("i_manufact_id", I32, False, "厂商 ID"),
                _col("i_manufact", STR, False, "厂商名"),
                _col("i_size", STR, False, "尺码"),
                _col("i_formulation", STR, False, "配方/规格"),
                _col("i_color", STR, False, "颜色"),
                _col("i_units", STR, False, "单位"),
                _col("i_container", STR, False, "包装"),
                _col("i_manager_id", I32, False, "商品经理 ID"),
                _col("i_product_name", STR, False, "商品名（词典文本）"),
            ],
        ),
        TableSpec(
            name="dim_store",
            file="store.dat",
            columns=[
                _col("s_store_sk", I32, True, "门店代理键"),
                _col("s_store_id", STR, True, "门店 ID（16 位词典编码）"),
                _col("s_rec_start_date", DATE, False, "有效起始日（Type-2）"),
                _col("s_rec_end_date", DATE, False, "有效截止日（空 = 当前版本）"),
                _col("s_closed_date_sk", I32, False, "关店日 sk（未关为未来日）"),
                _col("s_store_name", STR, False, "门店名"),
                _col("s_number_employees", I32, False, "员工数"),
                _col("s_floor_space", I32, False, "营业面积"),
                _col("s_hours", STR, False, "营业时间"),
                _col("s_manager", STR, False, "店长"),
                _col("s_market_id", I32, False, "商圈 ID"),
                _col("s_geography_class", STR, False, "地理分类"),
                _col("s_market_desc", STR, False, "商圈描述"),
                _col("s_market_manager", STR, False, "商圈经理"),
                _col("s_division_id", I32, False, "事业部 ID"),
                _col("s_division_name", STR, False, "事业部名"),
                _col("s_company_id", I32, False, "公司 ID"),
                _col("s_company_name", STR, False, "公司名"),
                _col("s_street_number", STR, False, "门牌号"),
                _col("s_street_name", STR, False, "街道名"),
                _col("s_street_type", STR, False, "街道类型"),
                _col("s_suite_number", STR, False, "房间号"),
                _col("s_city", STR, False, "城市"),
                _col("s_county", STR, False, "县"),
                _col("s_state", STR, False, "州（SF0.1 实测 12 店全 TN）"),
                _col("s_zip", STR, False, "邮编"),
                _col("s_country", STR, False, "国家"),
                _col("s_gmt_offset", D2(5), False, "GMT 偏移"),
                _col("s_tax_precentage", D2(5), False, "税率（官方拼写保留）"),
            ],
        ),
    ]


# ---------------------------------------------------------------------------
# .dat 解析（TPC-DS 管道文本，NULL = 空 token）
# ---------------------------------------------------------------------------


def _read_dat(data_dir: Path, spec: TableSpec) -> pa.Table:
    """按 spec 列定义读取 dsdgen .dat（显式类型，空 token -> NULL）。"""
    n_cols = len(spec.columns)
    raw: list[list[str | None]] = []
    with (data_dir / spec.file).open() as fh:
        for lineno, line in enumerate(fh, 1):
            parts = line.rstrip("\n").split("|")
            if len(parts) not in (n_cols, n_cols + 1):
                raise ValueError(
                    f"{spec.file}:{lineno} token 数 {len(parts)} != {n_cols}（.dat 物理行恒 N+1）"
                )
            raw.append([p if p else None for p in parts[:n_cols]])
    arrays = []
    for i, c in enumerate(spec.columns):
        # 先建 string 数组再 cast：pa.array 的 sequence 构造不做 str 解析
        # （空 token 已在上层转 None -> cast 后为 NULL）
        arrays.append(pa.array([r[i] for r in raw], pa.string()).cast(c.pa_type))
    return (
        pa.Table.from_arrays(arrays, names=[c.name for c in spec.columns]).cast(_pa_schema(spec))
    )


def _load_table(catalog: object, namespace: str, spec: TableSpec, data_dir: Path) -> pa.Table:
    """加载单张表：drop-if-exists -> create -> append，返回已写入的 DataFrame。

    create_table 响应可能带 Polaris 下发的容器视角 s3.endpoint
    （host.docker.internal:9000，Doris 容器内可达），append 前须改回宿主机视角
    —— 与 loader.open_table 同一修正（见其 docstring 背景，实测 2026-09-02）。
    """
    identifier = (namespace, spec.name)
    df = _read_dat(data_dir, spec)
    if catalog.table_exists(identifier):
        catalog.drop_table(identifier)
    table = catalog.create_table(identifier, schema=_iceberg_schema(spec))
    host_endpoint = os.environ.get("MINIO_ENDPOINT", "http://127.0.0.1:9000")
    if table.io.properties.get("s3.endpoint") != host_endpoint:
        props = dict(table.io.properties)
        props["s3.endpoint"] = host_endpoint
        table.io = load_file_io(props, table.metadata_location)
    table.append(df)
    return df


# ---------------------------------------------------------------------------
# 数据探查（样本设计输入；数字全部来自装载后实测，N1 机械转述）
# ---------------------------------------------------------------------------


def _scan(catalog: object, namespace: str, name: str) -> pa.Table:
    return open_table(catalog, namespace, name).scan().to_arrow()


def probe(catalog: object, namespace: str = "dwd") -> dict:
    """打印 4 表行数/窗口/值域/量级，返回关键数字 dict（供留档）。"""
    import pyarrow.compute as pc

    out: dict = {}
    n_rows = {}
    for spec in _specs():
        df = _scan(catalog, namespace, spec.name)
        n_rows[spec.name] = df.num_rows
        print(f"[probe] {spec.name}: {df.num_rows:,} 行")
    out["row_counts"] = n_rows

    date_dim = _scan(catalog, namespace, "date_dim")
    dates = pc.min_max(date_dim["d_date"]).as_py()
    years_list = date_dim["d_year"].to_pylist()
    out["date_dim"] = {
        "d_date_min": str(dates["min"]),
        "d_date_max": str(dates["max"]),
        "d_year_min": min(years_list),
        "d_year_max": max(years_list),
    }
    print(
        f"[probe] date_dim 窗口: {dates['min']} ~ {dates['max']}"
        f"（d_year {min(years_list)}~{max(years_list)}）"
    )

    item = _scan(catalog, namespace, "dim_item")
    vc = pc.value_counts(item["i_category"]).to_pylist()
    cat_counts = {r["values"]: r["counts"] for r in vc}
    out["item_categories"] = cat_counts
    print("[probe] i_category 值域:", cat_counts)

    store = _scan(catalog, namespace, "dim_store")
    state_counts = (
        store.group_by("s_state")
        .aggregate([("s_store_sk", "count")])
        .sort_by([("s_store_sk_count", "descending")])
    )
    city_counts = store.group_by("s_city").aggregate([("s_store_sk", "count")])
    out["store_state"] = {r["s_state"]: r["s_store_sk_count"] for r in state_counts.to_pylist()}
    out["store_city"] = {r["s_city"]: r["s_store_sk_count"] for r in city_counts.to_pylist()}
    print("[probe] store s_state:", out["store_state"])
    print("[probe] store s_city:", out["store_city"])

    ss = _scan(catalog, namespace, "store_sales")
    out["store_sales"] = {
        "null_sold_date_sk": pc.sum(pc.is_null(ss["ss_sold_date_sk"])).as_py(),
        "tickets": len(pc.unique(ss["ss_ticket_number"])),
        "qty_sum": pc.sum(ss["ss_quantity"]).as_py(),
        "sales_sum": pc.sum(ss["ss_ext_sales_price"]).as_py(),
        "profit_sum": pc.sum(ss["ss_net_profit"]).as_py(),
    }
    print("[probe] store_sales 总览:", out["store_sales"])

    # 按年分组量级（join dim_date 取 d_year/d_date；日期键可能为 NULL——TPC-DS
    # 查询模板惯例由 join 自然排除，探查同口径标注）
    sk2year = dict(
        zip(date_dim["d_date_sk"].to_pylist(), date_dim["d_year"].to_pylist(), strict=True)
    )
    sk2date = dict(
        zip(date_dim["d_date_sk"].to_pylist(), date_dim["d_date"].to_pylist(), strict=True)
    )
    years = [sk2year.get(sk) for sk in ss["ss_sold_date_sk"].to_pylist()]
    n_join = sum(1 for y in years if y is not None)
    print(f"[probe] store_sales 日期键 join dim_date 命中率: {n_join}/{ss.num_rows}")
    ss2 = ss.append_column("_d_year", pa.array(years, pa.int32()))
    agg = ss2.group_by("_d_year").aggregate(
        [
            ("ss_ext_sales_price", "sum"),
            ("ss_quantity", "sum"),
            ("ss_ticket_number", "count_distinct"),
            ("ss_item_sk", "count"),
        ]
    )
    by_year = {
        r["_d_year"]: {
            "sales": r["ss_ext_sales_price_sum"],
            "qty": r["ss_quantity_sum"],
            "orders": r["ss_ticket_number_count_distinct"],
            "rows": r["ss_item_sk_count"],
        }
        for r in agg.sort_by([("_d_year", "ascending")]).to_pylist()
        if r["_d_year"] is not None
    }
    out["sales_by_year"] = by_year
    for y, v in by_year.items():
        print(f"[probe] {y}: 销售额 {v['sales']:,} / 件数 {v['qty']:,} / 订单 {v['orders']:,}")

    # 品类 × 年（top 类目单年销售额量级）；i_category 空行（43）join 落空自然排除
    sk2cat = dict(zip(item["i_item_sk"].to_pylist(), item["i_category"].to_pylist(), strict=True))
    cats = [sk2cat.get(sk) for sk in ss["ss_item_sk"].to_pylist()]
    ss3 = ss2.append_column("_i_category", pa.array(cats, pa.string()))
    agg2 = ss3.group_by(["_d_year", "_i_category"]).aggregate([("ss_ext_sales_price", "sum")])
    by_cat_year = {
        (r["_d_year"], r["_i_category"]): r["ss_ext_sales_price_sum"]
        for r in agg2.to_pylist()
    }
    out["top_cat_year_sales"] = sorted(
        [{"year": y, "category": c, "sales": s} for (y, c), s in by_cat_year.items() if c],
        key=lambda r: r["sales"],
        reverse=True,
    )[:10]
    for r in out["top_cat_year_sales"]:
        print(
            f"[probe] 品类×年 top: {r['year']} {r['category']}: 销售额 {r['sales']:,.0f}"
        )

    # 门店 × 年
    agg3 = ss3.group_by(["_d_year", "ss_store_sk"]).aggregate([("ss_ext_sales_price", "sum")])
    out["top_store_year_sales"] = sorted(
        agg3.to_pylist(), key=lambda r: r["ss_ext_sales_price_sum"], reverse=True
    )[:5]
    for r in out["top_store_year_sales"]:
        print(
            f"[probe] 门店×年 top: {r['_d_year']} store_sk={r['ss_store_sk']}: "
            f"销售额 {r['ss_ext_sales_price_sum']:,.0f}"
        )

    # 日期窗口（首/末有销售日期）
    dated = [sk2date[sk] for sk in ss["ss_sold_date_sk"].to_pylist() if sk in sk2date]
    if dated:
        out["sale_window"] = {"min": str(min(dated)), "max": str(max(dated))}
        print(
            "[probe] store_sales 销售日期窗口: "
            f"{out['sale_window']['min']} ~ {out['sale_window']['max']}"
        )
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", help="只加载指定表（默认全部 4 张）")
    parser.add_argument("--namespace", default="dwd", help="Iceberg namespace（默认 dwd）")
    parser.add_argument(
        "--data-dir",
        default=str(Path(__file__).resolve().parent / "raw" / "tpcds" / "sf01"),
        help="TPC-DS SF0.1 数据目录（scripts/setup_tpcds.sh 产物）",
    )
    parser.add_argument(
        "--probe", action="store_true", help="只做数据探查打印（不装载）"
    )
    args = parser.parse_args()

    specs = _specs()
    if args.table:
        specs = [s for s in specs if s.name == args.table]
        if not specs:
            opts = ", ".join(s.name for s in _specs())
            raise SystemExit(f"未知表名: {args.table}（可选: {opts}）")

    data_dir = Path(args.data_dir)
    catalog = build_catalog()
    if args.probe:
        probe(catalog, args.namespace)
        return

    missing = [s.file for s in specs if not (data_dir / s.file).exists()]
    if missing:
        raise SystemExit(
            f"缺少源文件: {missing}（目录: {data_dir}；先跑 scripts/setup_tpcds.sh）"
        )

    print(f"catalog ok, namespaces: {catalog.list_namespaces()}")
    for spec in specs:
        df = _load_table(catalog, args.namespace, spec, data_dir)
        csv_rows = df.num_rows
        written = open_table(catalog, args.namespace, spec.name).scan().to_arrow().num_rows
        status = "OK" if written == csv_rows else "MISMATCH"
        print(f"[{status}] {spec.name}: dat={csv_rows} written={written}")
        if status == "MISMATCH":
            raise SystemExit(f"{spec.name} 行数不一致，中止后续加载")
    print("all tables loaded")
    print("==> 数据探查（N1：数字来自装载后实测）")
    probe(catalog, args.namespace)


if __name__ == "__main__":
    main()
