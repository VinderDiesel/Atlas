#!/usr/bin/env python3
"""TPC-DI Batch1 CSV -> Iceberg ODS 全量加载器。

数据链路（对应 AGENTS.md 目录职责 data/raw -> Iceberg ODS）：
    data/raw/tpcdi/Batch1/*.{txt,csv,xml,FINWIRE*} --解析-->  Iceberg V2 表
    （Polaris catalog 'atlas'，namespace 'tpcdi'）

源文件族与解析器（kind）：
- pipe CSV（默认）：Date/Time/Industry/StatusType/TaxRate/TradeType/Trade/TradeHistory/
  CashTransaction/WatchHistory/HoldingHistory/DailyMarket
- comma CSV（kind="csv", delimiter=","）：HR.csv（员工全量，job_code='314' 为 broker）、
  Prospect.csv
- CustomerMgmt.xml（kind="xml_customer"/"xml_account"）：Action 流拆客户/账户两表
- FINWIRE*（kind="finwire_sec"）：季度文件中的 SEC 行（dim_security 源，PDGF 定宽）

行为契约：
- 幂等重建：默认逐表 drop-if-exists -> create -> append，任何一步失败即停（部分成功可整体重跑）。
- 列类型为 ODS 保真设计：数值文本（t_exec_name/t_is_cash/TimeDesc 系列/job_code）保留 string，
  金额/税率用 decimal，时间戳统一 timestamp(us)（无时区，源数据无时区语义）。
- 与 Polaris 1.7 的对接要点（已在 smoke 链路验证，勿随意改动）：
  1. catalog 需预配 table-default.s3.* 静态凭证（服务端建表写初始 metadata 用），见 infra 脚本；
  2. 请求必须覆盖 pyiceberg 默认的 vended-credentials 头为 Polaris 忽略值，
     否则 Polaris 尝试为无签发能力的 catalog 出账凭证（infra/0004 有说明）。

用法：
    python data/loader.py                  # 加载全部 17 张表
    python data/loader.py --table trade    # 只加载一张
    python data/loader.py --emit-ddl       # 把 Iceberg DDL 打印到 stdout（sql/dwd/ 引用）
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from datetime import date as _py_date
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pcsv
from pyiceberg.catalog import load_catalog
from pyiceberg.io import load_file_io
from pyiceberg.schema import Schema
from pyiceberg.types import (
    BooleanType,
    DateType,
    DecimalType,
    IcebergType,
    IntegerType,
    LongType,
    NestedField,
    StringType,
    TimestampType,
    TimeType,
)

# ---------------------------------------------------------------------------
# ODS 表定义：列序 = Batch1 源文件列序（实测），列名/精度 = TPC-DI spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    name: str
    pa_type: pa.DataType
    required: bool = False
    doc: str = ""


@dataclass(frozen=True)
class TableSpec:
    name: str
    file: str
    columns: list[Column] = field(default_factory=list)
    kind: str = "csv"  # csv（delimiter 可配）/ xml_customer / xml_account / finwire_sec
    delimiter: str = "|"


def _col(name: str, pa_type: pa.DataType, required: bool = False, doc: str = "") -> Column:
    return Column(name, pa_type, required, doc)


# pyarrow 类型别名（保持声明紧凑）
STR = pa.string()
I32 = pa.int32()
I64 = pa.int64()
BOOL = pa.bool_()
DATE = pa.date32()
TIME = pa.time64("us")
TS = pa.timestamp("us")
D2 = lambda p: pa.decimal128(p, 2)  # noqa: E731 金额（2 位小数）
D5 = lambda p: pa.decimal128(p, 5)  # noqa: E731 税率（5 位小数）


def _specs() -> list[TableSpec]:
    """17 张 ODS/stage 表（Batch1 无 cdc 列，列数已对源文件逐行核验）。"""
    return [
        # ---- 维表（小表）--------------------------------------------------
        TableSpec(
            name="dim_date",
            file="Date.txt",
            columns=[
                _col("sk_date_id", I32, True, "日期代理键 yyyymmdd"),
                _col("date_value", DATE, True, "日期"),
                _col("date_desc", STR, True, "英文日期描述"),
                _col("calendar_year_id", I32, True),
                _col("calendar_year_desc", STR, True),
                _col("calendar_qtr_id", I32, True),
                _col("calendar_qtr_desc", STR, True),
                _col("calendar_month_id", I32, True),
                _col("calendar_month_desc", STR, True),
                _col("calendar_week_id", I32, True),
                _col("calendar_week_desc", STR, True),
                _col("day_of_week_numeric", I32, True, "1=周一 ... 7=周日"),
                _col("day_of_week_desc", STR, True),
                _col("fiscal_year_id", I32, True),
                _col("fiscal_year_desc", STR, True),
                _col("fiscal_qtr_id", I32, True),
                _col("fiscal_qtr_desc", STR, True),
                _col("holiday_flag", BOOL, False, "是否节假日"),
            ],
        ),
        TableSpec(
            name="dim_time",
            file="Time.txt",
            columns=[
                _col("sk_time_id", I32, True, "HHMMSS 数值键"),
                _col("time_value", TIME, True, "当日时间"),
                _col("hour_id", I32, True),
                _col("hour_desc", STR, True, "00..23（文本保真）"),
                _col("minute_id", I32, True),
                _col("minute_desc", STR, True, "HH:MM（文本保真）"),
                _col("second_id", I32, True),
                _col("second_desc", STR, True, "HH:MM:SS（文本保真）"),
                _col("market_hours_flag", BOOL, True),
                _col("office_hours_flag", BOOL, True),
            ],
        ),
        TableSpec(
            name="industry",
            file="Industry.txt",
            columns=[
                _col("in_id", STR, True, "行业代码"),
                _col("in_name", STR, True),
                _col("in_sc_id", STR, True, "所属行业分类代码"),
            ],
        ),
        TableSpec(
            name="status_type",
            file="StatusType.txt",
            columns=[
                _col("st_id", STR, True, "状态代码"),
                _col("st_name", STR, True),
            ],
        ),
        TableSpec(
            name="tax_rate",
            file="TaxRate.txt",
            columns=[
                _col("tx_id", STR, True, "税率代码"),
                _col("tx_name", STR, True),
                _col("tx_rate", D5(8), False, "税率（0~1 区间）"),
            ],
        ),
        TableSpec(
            name="trade_type",
            file="TradeType.txt",
            columns=[
                _col("tt_id", STR, True, "交易类型代码"),
                _col("tt_name", STR, True),
                _col("tt_is_sell", I32, True, "1=卖出"),
                _col("tt_is_mrkt", I32, True, "1=市价单"),
            ],
        ),
        # ---- 事实表（大表）-------------------------------------------------
        TableSpec(
            name="trade",
            file="Trade.txt",
            columns=[
                _col("t_id", I64, True, "交易 ID"),
                _col("t_dts", TS, True, "交易时间"),
                _col("t_st_id", STR, True, "状态（关联 status_type）"),
                _col("t_tt_id", STR, True, "交易类型（关联 trade_type）"),
                _col("t_is_cash", STR, False, "0/1 文本（spec CHAR(3)，保真）"),
                _col("t_s_symb", STR, True, "证券代码"),
                _col("t_qty", I64, True, "交易数量"),
                _col("t_bid_price", D2(12), False, "下单时买价"),
                _col("t_ca_id", I64, False, "客户账户 ID"),
                _col("t_exec_name", STR, False, "执行人 ID（数字文本，保真）"),
                _col("t_trade_price", D2(12), False, "成交价（未成交为空）"),
                _col("t_chrg", D2(12), False, "手续费 fee（TPC-DI 口径，区别于 t_comm）"),
                _col("t_comm", D2(12), False, "佣金 commission（语义层 Commission 的源）"),
                _col("t_tax", D2(12), False, "税费"),
            ],
        ),
        TableSpec(
            name="trade_history",
            file="TradeHistory.txt",
            columns=[
                _col("th_t_id", I64, True, "交易 ID"),
                _col("th_dts", TS, True, "状态变更时间"),
                _col("th_st_id", STR, True, "新状态（关联 status_type）"),
            ],
        ),
        TableSpec(
            name="cash_transaction",
            file="CashTransaction.txt",
            columns=[
                _col("ct_ca_id", I64, True, "客户账户 ID"),
                _col("ct_dts", TS, True, "交易时间"),
                _col("ct_amt", D2(12), True, "金额（正入负出）"),
                _col("ct_name", STR, True, "交易描述"),
            ],
        ),
        TableSpec(
            name="watch_history",
            file="WatchHistory.txt",
            columns=[
                _col("w_c_id", I64, True, "客户 ID"),
                _col("w_s_symb", STR, True, "证券代码"),
                _col("w_dts", TS, True, "操作时间"),
                _col("w_action", STR, True, "ACTV=加入关注 / CNCL=取消"),
            ],
        ),
        TableSpec(
            name="holding_history",
            file="HoldingHistory.txt",
            columns=[
                _col("hh_h_t_id", I64, True, "被更新的历史持仓 ID（0=新建）"),
                _col("hh_t_id", I64, True, "触发变更的交易 ID"),
                _col("hh_before_qty", I64, True, "变更前持仓量"),
                _col("hh_after_qty", I64, True, "变更后持仓量"),
            ],
        ),
        TableSpec(
            name="daily_market",
            file="DailyMarket.txt",
            columns=[
                _col("dm_date", DATE, True, "交易日"),
                _col("dm_s_symb", STR, True, "证券代码"),
                _col("dm_close", D2(12), True, "收盘价"),
                _col("dm_high", D2(12), True, "最高价"),
                _col("dm_low", D2(12), True, "最低价"),
                _col("dm_vol", I64, True, "成交量"),
            ],
        ),
        # ---- 扩展源（非 pipe 文件，DWD 加工输入）--------------------------
        # HR.csv：员工全量 10000 行；broker 子集 = job_code='314'（实测 2865 人，
        # 与 Trade.t_exec_name 值域及 HR_audit HR_BROKERS=2865 双重吻合）。
        # 列序/列名按 PDGF 输出（Family-Names.dict→emp_first_name 为反直觉映射，保真）。
        TableSpec(
            name="hr_employee",
            file="HR.csv",
            kind="csv",
            delimiter=",",
            columns=[
                _col("emp_id", I32, True, "员工 ID（自然键，=dim_broker.BrokerID）"),
                _col("mgr_id", I32, True, "直属经理 ID（值域 1..1000）"),
                _col("emp_first_name", STR, True, "名（PDGF 词典映射，保真）"),
                _col("emp_last_name", STR, True, "姓"),
                _col("emp_mi", STR, False, "中间名首字母（63% 为空）"),
                _col("job_code", STR, False, "岗位码文本保真；314=broker"),
                _col("branch", STR, False, "分支编码（20-30 位随机串）"),
                _col("office", STR, False, "办公室，如 OFFICE7152"),
                _col("phone", STR, False, "电话 (xxx) xxx-xxxx"),
            ],
        ),
        # CustomerMgmt.xml：Action 流（10000 个 Action：NEW 3056 / UPDCUST 868 /
        # INACT 434 / ADDACCT 3038 / UPDACCT 1736 / CLOSEACCT 868，已与 audit 核对）。
        # customer_action：每 Action 一行共 10000 行——NEW 携带全字段画像、
        # UPDACCT 字段级补丁（tier/city/state_prov/ctry/prim_email 等）、
        # 其余类型仅带 c_id（实测，Customer 子元素恒存在但属性不全）。
        # account_action：每 Action 的 Account 子元素一行（见下方注释）。
        TableSpec(
            name="customer_action",
            file="CustomerMgmt.xml",
            kind="xml_customer",
            columns=[
                _col("action_type", STR, True, "NEW/UPDCUST/INACT"),
                _col("action_ts", TS, True, "ActionTS（XML ISO，无时区）"),
                _col("c_id", I64, True, "客户自然键"),
                _col("tax_id", STR, False, "税务 ID"),
                _col("gender", STR, False, "性别"),
                _col("tier", I32, False, "客户分层 1-3"),
                _col("dob", DATE, False, "出生日期"),
                _col("l_name", STR, False, "姓"),
                _col("f_name", STR, False, "名"),
                _col("m_name", STR, False, "中间名"),
                _col("city", STR, False, "城市"),
                _col("state_prov", STR, False, "省/州"),
                _col("ctry", STR, False, "国家"),
                _col("prim_email", STR, False, "主邮箱"),
            ],
        ),
        # account_action：每 ca_id 恰好 1 条 NEW/ADDACCT 底稿（全量携带账户属性），
        # UPDACCT 最多 6 次补丁（部分携带 b_id/tax_st/ca_name），CLOSEACCT 1 次关闭标记；
        # 合计 8698 = NEW 3056 + ADDACCT 3038 + UPDACCT 1736 + CLOSEACCT 868。
        TableSpec(
            name="account_action",
            file="CustomerMgmt.xml",
            kind="xml_account",
            columns=[
                _col("action_type", STR, True, "NEW/ADDACCT/UPDACCT/CLOSEACCT"),
                _col("action_ts", TS, True, "ActionTS（XML ISO，无时区）"),
                _col("c_id", I64, True, "持有人客户自然键"),
                _col("ca_id", I64, True, "账户自然键"),
                _col(
                    "b_id",
                    I64,
                    False,
                    "托管 broker（ADDACCT/NEW 全量携带，UPDACCT 部分携带"
                    "（354/1736 为换 broker），DWD 按 ts 取最近非空）",
                ),
                _col(
                    "ca_name",
                    STR,
                    False,
                    "账户名（UPDACCT 1296/1736 携带改名，DWD 按 ts 取最近非空）",
                ),
                _col("tax_st", I32, False, "税务状态 0/1/2（attr CA_TAX_ST）"),
            ],
        ),
        # FINWIRE* 季度文件（1967Q1..2017Q2 共 203 个）：只取 SEC 行（行 16-18 位
        # 为类型标记；全量 SEC 行 1600，distinct symbol 1198 ⊇ Trade.t_s_symb 1011）。
        # 行尾被 PDGF rstrip：CIK 引用行实测 170 字符、公司名引用行 220 字符，
        # 故 148 字符后按剩余截取（co_name_or_cik 可能短/空）。
        TableSpec(
            name="finwire_security",
            file="FINWIRE*",
            kind="finwire_sec",
            columns=[
                _col("pts", TS, True, "yyyyMMdd-HHmmss 发布时间戳"),
                _col("symbol", STR, True, "证券代码（15 位 base-26，自然键）"),
                _col("issue", STR, False, "证券类型，如 COMMON/PREF_A"),
                _col("status", STR, True, "ACTV/INAC"),
                _col("name", STR, False, "证券名称（PDGF 随机串）"),
                _col("exchange_id", STR, False, "交易所代码，如 NYSE/AMEX/PCX"),
                _col("share_out", I64, False, "流通股数"),
                _col("first_trade", DATE, False, "首发日期"),
                _col("first_exchange_trade", DATE, False, "本所首交易日（含异常年份，保真）"),
                _col("dividend", D2(12), False, "每股年股息（12 宽右对齐，%.2f）"),
                _col("co_name_or_cik", STR, False, "公司名(60 宽)或 CIK(10 位数字)引用"),
            ],
        ),
        # Prospect.csv：潜在客户 9988 行（官方 audit P_RECORDS=9988/P_NEW=9988）。
        TableSpec(
            name="prospect",
            file="Prospect.csv",
            kind="csv",
            delimiter=",",
            columns=[
                _col("agency_id", STR, True, "机构代码"),
                _col("last_name", STR, True),
                _col("first_name", STR, True),
                _col("middle_initial", STR, False),
                _col("gender", STR, False),
                _col("address_line1", STR, False),
                _col("address_line2", STR, False),
                _col("postal_code", STR, False),
                _col("city", STR, False),
                _col("state", STR, False),
                _col("country", STR, False),
                _col("phone", STR, False),
                _col("income", I64, False, "年收入"),
                _col("number_cars", I32, False),
                _col("number_children", I32, False),
                _col("marital_status", STR, False, "U/M/W/D 等"),
                _col("age", I32, False),
                _col("credit_rating", I32, False),
                _col("own_or_rent", STR, False, "O/R"),
                _col("employer", STR, False),
                _col("number_credit_cards", I32, False),
                _col("net_worth", I64, False, "净资产（整数，spec DECIMAL(10,2) 实测无小数）"),
            ],
        ),
    ]


# ---------------------------------------------------------------------------
# Iceberg catalog / 写入
# ---------------------------------------------------------------------------


def build_catalog() -> object:
    """构造 Polaris REST catalog（宿主机视角，MinIO 走 127.0.0.1:9000）。

    环境变量可覆盖：POLARIS_URI / POLARIS_CLIENT_ID / POLARIS_CLIENT_SECRET /
    MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY。
    """
    endpoint = os.environ.get("MINIO_ENDPOINT", "http://127.0.0.1:9000")
    return load_catalog(
        "atlas",
        type="rest",
        uri=os.environ.get("POLARIS_URI", "http://127.0.0.1:8181/api/catalog"),
        credential=(
            f"{os.environ.get('POLARIS_CLIENT_ID', 'root')}:"
            f"{os.environ.get('POLARIS_CLIENT_SECRET', 'secret')}"
        ),
        scope="PRINCIPAL_ROLE:ALL",
        warehouse="atlas",
        **{
            # 客户端静态凭证：写数据文件用（Polaris 侧凭证走 catalog table-default.*）
            "s3.endpoint": endpoint,
            "s3.access-key-id": os.environ.get("MINIO_ACCESS_KEY", "admin"),
            "s3.secret-access-key": os.environ.get("MINIO_SECRET_KEY", "password"),
            "s3.path-style-access": "true",
            "s3.region": "us-east-1",
            # 覆盖 pyiceberg 默认 vended-credentials（Polaris 1.7 忽略该值 -> direct 模式）
            "header.X-Iceberg-Access-Delegation": "vendor-credentials",
        },
    )


def open_table(catalog: object, namespace: str, name: str) -> object:
    """load_table 后把 io 的存储 endpoint 修正为宿主机可达地址。

    背景（实测 2026-09-02）：Polaris 在 loadTable 响应的 config 段下发
    storageConfigInfo.endpoint（host.docker.internal:9000，Doris 容器内可达），
    pyiceberg 按 {表属性, config, storage-credentials} 顺序合并，config 覆盖
    客户端 catalog 属性 —— 宿主机脚本（loader/snapshot）直接 load_table 会
    拿到容器视角 endpoint，扫描时解析失败（Could not resolve host:
    host.docker.internal）。Polaris 侧不能改（Doris FE 依赖该值），故在此
    把 io 的 endpoint 显式改回宿主机视角（与 build_catalog 的 s3.endpoint 一致）。

    Returns:
        Iceberg Table，io 已指向宿主机可达的 MinIO endpoint。
    """
    table = catalog.load_table((namespace, name))
    host_endpoint = os.environ.get("MINIO_ENDPOINT", "http://127.0.0.1:9000")
    if table.io.properties.get("s3.endpoint") != host_endpoint:
        props = dict(table.io.properties)
        props["s3.endpoint"] = host_endpoint
        table.io = load_file_io(props, table.metadata_location)
    return table


def _iceberg_type(t: pa.DataType) -> IcebergType:
    """pyarrow 列类型 -> Iceberg 原生类型（loader 使用的类型子集）。"""
    mapping = {
        pa.string(): StringType(),
        pa.int32(): IntegerType(),
        pa.int64(): LongType(),
        pa.bool_(): BooleanType(),
        pa.date32(): DateType(),
        pa.time64("us"): TimeType(),
        pa.timestamp("us"): TimestampType(),
    }
    if t in mapping:
        return mapping[t]
    if isinstance(t, pa.Decimal128Type):
        return DecimalType(t.precision, t.scale)
    raise ValueError(f"unmapped pyarrow type: {t}")


def _pa_schema(spec: TableSpec) -> pa.Schema:
    return pa.schema([pa.field(c.name, c.pa_type, nullable=not c.required) for c in spec.columns])


def _iceberg_schema(spec: TableSpec) -> Schema:
    """由 TableSpec 构造 Iceberg schema（字段顺序 = 源文件列序）。"""
    return Schema(
        *[
            NestedField(i + 1, c.name, _iceberg_type(c.pa_type), required=c.required)
            for i, c in enumerate(spec.columns)
        ]
    )


def _read_csv(data_dir: Path, spec: TableSpec) -> pa.Table:
    """按 spec 列定义读取源 CSV（显式类型，空串 -> NULL）。

    pyarrow CSV 输出列恒为 optional，需 cast 到目标 nullable 约束
    （required 列若出现 NULL 会在此处报错，兼作数据完整性校验）。
    """
    column_types = {c.name: c.pa_type for c in spec.columns}
    df = pcsv.read_csv(
        data_dir / spec.file,
        read_options=pcsv.ReadOptions(column_names=[c.name for c in spec.columns]),
        parse_options=pcsv.ParseOptions(delimiter=spec.delimiter),
        convert_options=pcsv.ConvertOptions(column_types=column_types, strings_can_be_null=True),
    )
    return df.cast(_pa_schema(spec))


def _read_table(data_dir: Path, spec: TableSpec) -> pa.Table:
    """按 spec.kind 分派解析器，返回与 spec.columns 对齐的 pyarrow Table。"""
    if spec.kind == "csv":
        return _read_csv(data_dir, spec)
    if spec.kind in ("xml_customer", "xml_account"):
        return _read_xml_actions(data_dir, spec)
    if spec.kind == "finwire_sec":
        return _read_finwire_sec(data_dir, spec)
    raise ValueError(f"未知解析器 kind={spec.kind}（表 {spec.name}）")


def _read_xml_actions(data_dir: Path, spec: TableSpec) -> pa.Table:
    """CustomerMgmt.xml -> Action 流（iterparse 流式，不整树驻留内存）。

    Action 直接子元素为 Customer（Account 嵌套在 Customer 内，实测结构）；
    Account 仅在 NEW/ADDACCT/UPDACCT/CLOSEACCT action 中存在。缺省子元素 -> NULL。
    注意：仅 Actions/Action 带 TPCDI: 前缀，子元素无前缀（无默认命名空间），
    因此只有 Action 用 ns 定位，其余一律无前缀查找。
    列语义见 _specs() 中两个 stage 表的 Column.doc。
    """
    import xml.etree.ElementTree as ET

    ns = "{http://www.tpc.org/tpc-di}"
    want_account = spec.kind == "xml_account"

    def _t(el: ET.Element | None, tag: str) -> str | None:
        if el is None:
            return None
        node = el.find(tag)
        if node is None or node.text is None:
            return None
        v = node.text.strip()
        return v or None

    def _int_attr(el: ET.Element | None, attr: str) -> int | None:
        if el is None:
            return None
        v = el.get(attr)
        return int(v) if v else None

    def _d_attr(el: ET.Element | None, attr: str) -> _py_date | None:
        if el is None:
            return None
        v = el.get(attr)
        return _py_date.fromisoformat(v) if v else None

    rows: list[dict] = []
    for _, elem in ET.iterparse(data_dir / spec.file, events=("end",)):
        if elem.tag != ns + "Action":
            continue
        attrs = elem.attrib
        action_type = attrs.get("ActionType")
        action_ts = attrs.get("ActionTS")
        cust = elem.find("Customer")
        if want_account:
            if cust is None:
                continue
            acc = cust.find("Account")
            if acc is not None:
                b_id = _t(acc, "CA_B_ID")
                rows.append(
                    {
                        "action_type": action_type,
                        "action_ts": datetime.fromisoformat(action_ts),
                        "c_id": int(cust.get("C_ID")) if cust is not None else None,
                        "ca_id": int(acc.get("CA_ID")),
                        "b_id": int(b_id) if b_id else None,
                        "ca_name": _t(acc, "CA_NAME"),
                        "tax_st": _int_attr(acc, "CA_TAX_ST"),
                    }
                )
        else:
            if cust is None:
                continue
            name = cust.find("Name")
            addr = cust.find("Address")
            contact = cust.find("ContactInfo")
            rows.append(
                {
                    "action_type": action_type,
                    "action_ts": datetime.fromisoformat(action_ts),
                    "c_id": int(cust.get("C_ID")),
                    "tax_id": cust.get("C_TAX_ID"),
                    "gender": cust.get("C_GNDR"),
                    "tier": _int_attr(cust, "C_TIER"),
                    "dob": _d_attr(cust, "C_DOB"),
                    "l_name": _t(name, "C_L_NAME"),
                    "f_name": _t(name, "C_F_NAME"),
                    "m_name": _t(name, "C_M_NAME"),
                    "city": _t(addr, "C_CITY"),
                    "state_prov": _t(addr, "C_STATE_PROV"),
                    "ctry": _t(addr, "C_CTRY"),
                    "prim_email": _t(contact, "C_PRIM_EMAIL"),
                }
            )
        elem.clear()
    return _table_from_rows(rows, spec)


def _read_finwire_sec(data_dir: Path, spec: TableSpec) -> pa.Table:
    """FINWIRE* 季度文件 -> SEC 证券记录（PDGF 定宽 + 行尾 trim 容错）。

    整行布局（0-based）：[0:8 日期][8:15 时间][15:18 类型标记]
    [18:33 symbol][33:39 issue][39:43 status][43:113 name][113:119 exchange]
    [119:132 share_out][132:140 first_trade][140:148 first_exchange_trade]
    [148:160 dividend][160:220 co_name_or_cik]

    PDGF 输出时 rstrip 行尾：co_name_or_cik 为 10 位 CIK 时行实测 170 字符、
    为 60 位公司名时 220 字符 —— 148 之前不受影响，其后切片越界自动截断。
    """
    from decimal import Decimal

    def _frag(line: str, a: int, b: int) -> str | None:
        v = line[a:b].strip()
        return v or None

    def _int(line: str, a: int, b: int) -> int | None:
        v = _frag(line, a, b)
        return int(v) if v else None

    def _d8(line: str, a: int, b: int) -> _py_date | None:
        v = _frag(line, a, b)
        return _py_date.fromisoformat(v) if v else None

    rows: list[dict] = []
    files = sorted(p for p in data_dir.glob(spec.file) if "." not in p.name)
    if not files:
        raise FileNotFoundError(f"{data_dir} 下无 {spec.file} 匹配文件")
    for f in files:
        for line in f.open(encoding="utf-8", errors="replace"):
            line = line.rstrip("\n")
            if len(line) < 18 or line[15:18] != "SEC":
                continue

            rows.append(
                {
                    "pts": datetime.strptime(line[0:15], "%Y%m%d-%H%M%S"),
                    "symbol": _frag(line, 18, 33),
                    "issue": _frag(line, 33, 39),
                    "status": _frag(line, 39, 43),
                    "name": _frag(line, 43, 113),
                    "exchange_id": _frag(line, 113, 119),
                    "share_out": _int(line, 119, 132),
                    "first_trade": _d8(line, 132, 140),
                    "first_exchange_trade": _d8(line, 140, 148),
                    "dividend": Decimal(_frag(line, 148, 160)) if _frag(line, 148, 160) else None,
                    "co_name_or_cik": _frag(line, 160, 220),
                }
            )
    return _table_from_rows(rows, spec)


def _table_from_rows(rows: list[dict], spec: TableSpec) -> pa.Table:
    """dict 行集 -> 按 spec 列定义构建（显式类型，缺省 -> NULL）。"""
    arrays = []
    for c in spec.columns:
        col = [r.get(c.name) for r in rows]
        arrays.append(pa.array(col, type=c.pa_type))
    return pa.Table.from_arrays(arrays, names=[c.name for c in spec.columns]).cast(_pa_schema(spec))


def load_table(catalog: object, namespace: str, spec: TableSpec, data_dir: Path) -> pa.Table:
    """加载单张表：drop-if-exists -> create -> append，返回已写入的 DataFrame。"""
    identifier = (namespace, spec.name)
    df = _read_table(data_dir, spec)
    schema = _iceberg_schema(spec)

    if catalog.table_exists(identifier):
        catalog.drop_table(identifier)
    table = catalog.create_table(identifier, schema=schema)
    table.append(df)
    return df


def emit_ddl() -> str:
    """由 spec 生成 Iceberg DDL（Spark `USING iceberg` 方言），供 sql/dwd/ 参考。"""
    lines = [
        "-- 本文件由 `python data/loader.py --emit-ddl` 生成，勿手改。",
        "-- 权威定义：data/loader.py TableSpec（与 Iceberg 表 schema 强一致）。",
        "-- 执行环境：Spark 3.5 + iceberg-spark-runtime，catalog=atlas, namespace=tpcdi。",
        "",
    ]
    for spec in _specs():
        lines.append(f"-- {spec.file} 行数据 -> ODS 表（列序 = 源文件列序）")
        lines.append(f"CREATE TABLE atlas.tpcdi.{spec.name} (")
        col_lines = []
        for c in spec.columns:
            ice_type = _pyarrow_to_iceberg_ddl_type(c.pa_type)
            col_lines.append(f"  {c.name} {ice_type}{'' if c.required else ' NULL'}")
        lines.append(",\n".join(col_lines))
        lines.append(") USING iceberg\n  TBLPROPERTIES ('format-version' = '2');")
        lines.append("")
    return "\n".join(lines)


def _pyarrow_to_iceberg_ddl_type(t: pa.DataType) -> str:
    mapping = {
        "string": "STRING",
        "int32": "INT",
        "int64": "LONG",
        "bool": "BOOLEAN",
        "date32[day]": "DATE",
        "time64[us]": "TIME",
        "timestamp[us]": "TIMESTAMP",
    }
    if str(t) in mapping:
        return mapping[str(t)]
    if isinstance(t, pa.Decimal128Type):
        return f"DECIMAL({t.precision}, {t.scale})"
    raise ValueError(f"unmapped pyarrow type: {t}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", help="只加载指定表（默认全部 17 张）")
    parser.add_argument("--namespace", default="tpcdi", help="Iceberg namespace（默认 tpcdi）")
    parser.add_argument(
        "--data-dir",
        default=str(Path(__file__).resolve().parent / "raw" / "tpcdi" / "Batch1"),
        help="Batch1 数据目录",
    )
    parser.add_argument("--emit-ddl", action="store_true", help="输出 Iceberg DDL 到 stdout 后退出")
    args = parser.parse_args()

    specs = _specs()
    if args.table:
        specs = [s for s in specs if s.name == args.table]
        if not specs:
            opts = ", ".join(s.name for s in _specs())
            raise SystemExit(f"未知表名: {args.table}（可选: {opts}）")

    if args.emit_ddl:
        print(emit_ddl())
        return

    data_dir = Path(args.data_dir)
    missing = []
    for s in specs:
        if s.kind == "finwire_sec":
            if not list(data_dir.glob(s.file)):
                missing.append(s.file)
        elif not (data_dir / s.file).exists():
            missing.append(s.file)
    if missing:
        raise SystemExit(f"缺少源文件: {missing}（目录: {data_dir}）")

    catalog = build_catalog()
    print(f"catalog ok, namespaces: {catalog.list_namespaces()}")
    for spec in specs:
        df = load_table(catalog, args.namespace, spec, data_dir)
        csv_rows = df.num_rows
        written = open_table(catalog, args.namespace, spec.name).scan().to_arrow().num_rows
        status = "OK" if written == csv_rows else "MISMATCH"
        print(f"[{status}] {spec.name}: csv={csv_rows} written={written}")
        if status == "MISMATCH":
            raise SystemExit(f"{spec.name} 行数不一致，中止后续加载")
    print("all tables loaded")


if __name__ == "__main__":
    main()
