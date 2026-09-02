-- =====================================================================
-- sql/dwd/fact_trades.sql
-- 功能：证券交易事实（只收已完成 CMPT 交易，241661 行）
--
-- 口径（实测依据，data/loader.py trade 注释可溯）：
--   - Trade.txt 全量 261261 = CMPT 241661 + CNCL 15658 + PNDG 3941 + SBMT 1；
--     t_trade_price 空值 19600 = 非 CMPT 行数（CNCL+PNDG+SBMT），且
--     holding_history 行数 241661 恰等于 CMPT —— 非成交无价格/佣金，
--     "成交记录"语义 = CMPT（与语义层 description 一致）。
--   - 字段映射（TPC-DI spec + 参考实现 DimTrade）：
--     Commission = t_comm（佣金；t_chrg 是 fee 不入表，loader 注释可溯）
--     Quantity = t_qty（整数，cast Decimal 对齐语义层声明）
--     CashFlag = (t_is_cash = '1')；实测值域 '0'/'1'
--     Status/Type = status_type/trade_type 可读名（Completed/Market Buy）
--   - 各 FK 完整性已核：CMPT 行 t_ca_id/t_exec_name/t_s_symb 无空值；
--     ca_id ⊆ dim_account 6094、exec_name 与 dim_broker(314) 双向相等 2865、
--     symbol ⊆ dim_security 1198、交易日 ⊆ dim_date 全历。
--   - SK_CreateDateID 经 dim_date.DateValue 关联（交易日），保证 FK 一致。
--
-- 重跑：INSERT OVERWRITE 整表覆盖，幂等。
-- =====================================================================

CREATE TABLE IF NOT EXISTS atlas.dwd.fact_trades (
    TradeID INT,
    SK_CreateDateID INT,
    SK_AccountID INT,
    SK_CustomerID INT,
    SK_BrokerID INT,
    SK_SecurityID INT,
    Quantity DECIMAL(18, 2),
    TradePrice DECIMAL(12, 2),
    Commission DECIMAL(12, 2),
    Tax DECIMAL(12, 2),
    CashFlag BOOLEAN,
    Status VARCHAR(32),
    Type VARCHAR(32)
);

INSERT OVERWRITE TABLE atlas.dwd.fact_trades
SELECT
    t.t_id AS TradeID,
    d.SK_DateID AS SK_CreateDateID,
    t.t_ca_id AS SK_AccountID,
    a.SK_CustomerID,
    b.SK_BrokerID,
    s.SK_SecurityID,
    CAST(t.t_qty AS DECIMAL(18, 2)) AS Quantity,
    t.t_trade_price AS TradePrice,
    t.t_comm AS Commission,
    t.t_tax AS Tax,
    (t.t_is_cash = '1') AS CashFlag,
    st.st_name AS Status,
    tt.tt_name AS Type
FROM atlas.tpcdi.trade t
JOIN atlas.tpcdi.status_type st
    ON st.st_id = t.t_st_id
JOIN atlas.tpcdi.trade_type tt
    ON tt.tt_id = t.t_tt_id
JOIN atlas.dwd.dim_date d
    ON d.DateValue = CAST(t.t_dts AS DATE)
JOIN atlas.dwd.dim_account a
    ON a.SK_AccountID = t.t_ca_id
JOIN atlas.dwd.dim_broker b
    ON b.BrokerID = CAST(t.t_exec_name AS INT)
JOIN atlas.dwd.dim_security s
    ON s.Symbol = t.t_s_symb
WHERE t.t_st_id = 'CMPT';
