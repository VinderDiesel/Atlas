-- =====================================================================
-- sql/dwd/dim_date.sql
-- 功能：日期维度（ODS dim_date 全量直通投影，列名转 PascalCase）
--
-- 口径（实测依据）：
--   - 源 atlas.tpcdi.dim_date = 全量日历 1950-01-01 ~ 2020-12-31（25933 行）
--   - SK_DateID = yyyymmdd 整数（19500101），与 DateValue 一一对应
--   - 交易/现金日期均落在此日历内（已核：CMPT 交易日 0 缺失）
--
-- 执行前提（data04 验证）：
--   1. Doris 已建 Iceberg catalog 'atlas' 指向 Polaris（源表 atlas.tpcdi.*）
--   2. namespace dwd 由下方 CREATE DATABASE 确保存在；目标表可建在
--      Iceberg catalog（Doris 4.1 支持外部 DDL；若失败，改用
--      data/loader.py --emit-ddl 产物经 pyiceberg 建同构空表）
--
-- 重跑：INSERT OVERWRITE 整表覆盖，幂等。
-- =====================================================================

CREATE DATABASE IF NOT EXISTS atlas.dwd;

CREATE TABLE IF NOT EXISTS atlas.dwd.dim_date (
    SK_DateID INT,
    DateValue DATE,
    DateDesc VARCHAR(64),
    CalendarYearID INT,
    CalendarYearDesc VARCHAR(64),
    CalendarQtrID INT,
    CalendarQtrDesc VARCHAR(64),
    CalendarMonthID INT,
    CalendarMonthDesc VARCHAR(64),
    CalendarWeekID INT,
    CalendarWeekDesc VARCHAR(64),
    DayOfWeekNumeric INT,
    DayOfWeekDesc VARCHAR(64),
    FiscalYearID INT,
    FiscalYearDesc VARCHAR(64),
    FiscalQtrID INT,
    FiscalQtrDesc VARCHAR(64),
    HolidayFlag BOOLEAN
);

INSERT OVERWRITE TABLE atlas.dwd.dim_date
SELECT
    sk_date_id AS SK_DateID,
    date_value AS DateValue,
    date_desc AS DateDesc,
    calendar_year_id AS CalendarYearID,
    calendar_year_desc AS CalendarYearDesc,
    calendar_qtr_id AS CalendarQtrID,
    calendar_qtr_desc AS CalendarQtrDesc,
    calendar_month_id AS CalendarMonthID,
    calendar_month_desc AS CalendarMonthDesc,
    calendar_week_id AS CalendarWeekID,
    calendar_week_desc AS CalendarWeekDesc,
    day_of_week_numeric AS DayOfWeekNumeric,
    day_of_week_desc AS DayOfWeekDesc,
    fiscal_year_id AS FiscalYearID,
    fiscal_year_desc AS FiscalYearDesc,
    fiscal_qtr_id AS FiscalQtrID,
    fiscal_qtr_desc AS FiscalQtrDesc,
    holiday_flag AS HolidayFlag
FROM atlas.tpcdi.dim_date;
