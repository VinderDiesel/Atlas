-- =====================================================================
-- sql/dwd/fact_cash_balances.sql
-- 功能：现金余额事实（账户每日余额 = 现金交易累计）
--
-- 口径（实测依据，data/loader.py cash_transaction 注释可溯）：
--   - 源 cash_transaction 241335 行、ct_amt 正入负出（±1e6 量级）；
--     ct_name 为随机流水名（无"开户入金"标记），余额起点 = 0 累计。
--   - 每日净额 = 按 (账户, 交易日) 汇总（同账户同日多笔：18672/219214 组日）；
--     余额 = 账户内按日净额窗口累计（参考 TPC-DI 参考实现 FactCashBalances）。
--   - 行数 = 219214（有现金交易的 (账户, 日) 组合），PK (SK_AccountID,
--     SK_DateID) 唯一。
--   - SK_CustomerID 经 dim_account（账户持有人，与 fact_trades 同口径）。
--
-- 重跑：INSERT OVERWRITE 整表覆盖，幂等。
-- =====================================================================

CREATE TABLE IF NOT EXISTS atlas.dwd.fact_cash_balances (
    SK_AccountID INT,
    SK_CustomerID INT,
    SK_DateID INT,
    Cash DECIMAL(18, 2)
);

INSERT OVERWRITE TABLE atlas.dwd.fact_cash_balances
SELECT
    a.SK_AccountID,
    a.SK_CustomerID,
    d.SK_DateID,
    x.Cash
FROM (
    SELECT
        ct_ca_id,
        ct_day,
        SUM(daily_net) OVER (
            PARTITION BY ct_ca_id
            ORDER BY ct_day
        ) AS Cash
    FROM (
        SELECT
            ct_ca_id,
            CAST(ct_dts AS DATE) AS ct_day,
            SUM(ct_amt) AS daily_net
        FROM atlas.tpcdi.cash_transaction
        GROUP BY ct_ca_id, CAST(ct_dts AS DATE)
    ) daily
) x
JOIN atlas.dwd.dim_account a
    ON a.SK_AccountID = x.ct_ca_id
JOIN atlas.dwd.dim_date d
    ON d.DateValue = x.ct_day;
