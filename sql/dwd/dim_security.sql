-- =====================================================================
-- sql/dwd/dim_security.sql
-- 功能：证券维度（FINWIRE SEC 行按 symbol 取最近发布版本）
--
-- 口径（实测依据，data/loader.py finwire_security 注释可溯）：
--   - 源 203 个季度文件 SEC 行全量 1600，distinct symbol 1198
--     ⊇ Trade 引用 1008（CMPT）/1011（全量）
--   - 每 symbol 取 pts 最新一行（FINWIRE 发布流，旧版本被新版本取代）
--   - Status（ACTV 1399 / INAC 201）暂不入表：语义层未声明该列；
--     INAC 证券一并保留（历史交易仍引用），后续需要可加列区分。
--   - SK_SecurityID = ROW_NUMBER() OVER (ORDER BY symbol)：FINWIRE 无
--     自然数字 ID；符号集不变则 SK 稳定（重跑同源结果一致）。
--     事实表经 Symbol 关联到本表的 SK_SecurityID。
--
-- 重跑：INSERT OVERWRITE 整表覆盖，幂等。
-- =====================================================================

CREATE TABLE IF NOT EXISTS atlas.dwd.dim_security (
    SK_SecurityID INT,
    Symbol VARCHAR(16),
    Issue VARCHAR(16),
    ExchangeID VARCHAR(8),
    Name VARCHAR(80),
    Dividend DECIMAL(12, 2)
);

INSERT OVERWRITE TABLE atlas.dwd.dim_security
SELECT
    ROW_NUMBER() OVER (ORDER BY symbol) AS SK_SecurityID,
    symbol AS Symbol,
    issue AS Issue,
    exchange_id AS ExchangeID,
    name AS Name,
    dividend AS Dividend
FROM (
    SELECT
        symbol,
        issue,
        exchange_id,
        name,
        dividend,
        ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY pts DESC) AS rn
    FROM atlas.tpcdi.finwire_security
) latest
WHERE rn = 1;
