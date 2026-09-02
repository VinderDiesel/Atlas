-- =====================================================================
-- sql/dwd/dim_account.sql
-- 功能：账户维度（账户底稿 NEW/ADDACCT + 全 action 流最新非空覆盖）
--
-- 口径（实测依据，data/loader.py account_action 注释可溯）：
--   - 账户全集 = 底稿（NEW+ADDACCT 的 Account 子元素）6094 个 ca_id，
--     每 ca_id 恰 1 条；UPDACCT 1341 账户最多 6 次补丁、CLOSEACCT 868 关闭标记，
--     两者 ca_id 均 ⊆ 底稿（已核）。
--   - SK_CustomerID = 底稿持有人 c_id（实测同一 ca_id 的 c_id 恒不变，
--     Account 元素无 CA_C_ID 属性，持有人即外层 Customer）。
--   - SK_BrokerID = 该 ca_id 全 action 流中最新非空 b_id：底稿全量携带
--     （ADDACCT/NEW null b_id = 0），UPDACCT 354/1736 携带（换 broker）。
--   - TaxStatus = 同上最新非空 tax_st（底稿全量携带，UPDACCT 349 携带更新）。
--     取法同 SK_BrokerID：ROW_NUMBER 窗口按 action_ts DESC 取最新
--     （实测 Doris 4.1 Nereids 不支持相关子查询内 LIMIT）。
--   - Status = 存在 CLOSEACCT 则 'closed'，否则 'active'（语义层可读值）。
--
-- 重跑：INSERT OVERWRITE 整表覆盖，幂等。
-- =====================================================================

CREATE TABLE IF NOT EXISTS atlas.dwd.dim_account (
    SK_AccountID INT,
    SK_BrokerID INT,
    SK_CustomerID INT,
    Status VARCHAR(16),
    TaxStatus INT
);

INSERT OVERWRITE TABLE atlas.dwd.dim_account
SELECT
    a.ca_id AS SK_AccountID,
    b.b_id AS SK_BrokerID,
    a.c_id AS SK_CustomerID,
    CASE
        WHEN cl.ca_id IS NOT NULL THEN 'closed'
        ELSE 'active'
    END AS Status,
    COALESCE(x.tax_st, a.tax_st) AS TaxStatus
FROM (
    SELECT ca_id, c_id, b_id, tax_st
    FROM atlas.tpcdi.account_action
    WHERE action_type IN ('NEW', 'ADDACCT')
) a
LEFT JOIN (
    SELECT ca_id, b_id
    FROM (
        SELECT
            ca_id,
            b_id,
            ROW_NUMBER() OVER (PARTITION BY ca_id ORDER BY action_ts DESC) AS rn
        FROM atlas.tpcdi.account_action
        WHERE b_id IS NOT NULL
    ) latest
    WHERE rn = 1
) b
    ON b.ca_id = a.ca_id
LEFT JOIN (
    SELECT DISTINCT ca_id
    FROM atlas.tpcdi.account_action
    WHERE action_type = 'CLOSEACCT'
) cl
    ON cl.ca_id = a.ca_id
LEFT JOIN (
    SELECT ca_id, tax_st
    FROM (
        SELECT
            ca_id,
            tax_st,
            ROW_NUMBER() OVER (PARTITION BY ca_id ORDER BY action_ts DESC) AS rn
        FROM atlas.tpcdi.account_action
        WHERE tax_st IS NOT NULL
    ) latest
    WHERE rn = 1
) x
    ON x.ca_id = a.ca_id;
