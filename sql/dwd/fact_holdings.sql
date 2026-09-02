-- =====================================================================
-- sql/dwd/fact_holdings.sql
-- 功能：持仓事实（每次成交后的持仓变更快照，241661 行）
--
-- 口径（实测依据，data/loader.py holding_history 注释可溯）：
--   - 源 holding_history 每行 = 一笔成交触发的持仓变更，hh_t_id 全 distinct
--     且 ⊆ trade.t_id；行数 241661 恰等于 CMPT 成交数（全为完成交易）。
--   - HoldingID = hh_t_id（触发交易 ID，唯一，可回溯源交易；语义层 PK）。
--   - 账户/证券/日期经 trade 关联：SK_DateID = 交易日（SK_CreateDateID 口径同
--     fact_trades，经 dim_date.DateValue）。
--   - CurrentQty = hh_after_qty（变更后持仓量）；CurrentPrice = 该笔成交价
--     t_trade_price；CurrentValue = qty × price。
--     ⚠ 快照口径 = "交易时点持仓"，非每日收盘重估（语义层 CurrentPrice 描述
--     为"当前市价"；如需收盘价口径需引入 daily_market，Known Limitations 备注）。
--   - hh_h_t_id（前序持仓 ID）暂不消费：MVP 无持仓链/SCD 需求，保留在源表。
--
-- 重跑：INSERT OVERWRITE 整表覆盖，幂等。
-- =====================================================================

CREATE TABLE IF NOT EXISTS atlas.dwd.fact_holdings (
    HoldingID INT,
    SK_AccountID INT,
    SK_SecurityID INT,
    SK_DateID INT,
    CurrentQty DECIMAL(18, 2),
    CurrentPrice DECIMAL(12, 2),
    CurrentValue DECIMAL(18, 2)
);

INSERT OVERWRITE TABLE atlas.dwd.fact_holdings
SELECT
    h.hh_t_id AS HoldingID,
    a.SK_AccountID,
    s.SK_SecurityID,
    d.SK_DateID,
    CAST(h.hh_after_qty AS DECIMAL(18, 2)) AS CurrentQty,
    t.t_trade_price AS CurrentPrice,
    CAST(h.hh_after_qty AS DECIMAL(18, 2)) * t.t_trade_price AS CurrentValue
FROM atlas.tpcdi.holding_history h
JOIN atlas.tpcdi.trade t
    ON t.t_id = h.hh_t_id
JOIN atlas.dwd.dim_date d
    ON d.DateValue = CAST(t.t_dts AS DATE)
JOIN atlas.dwd.dim_account a
    ON a.SK_AccountID = t.t_ca_id
JOIN atlas.dwd.dim_security s
    ON s.Symbol = t.t_s_symb;
