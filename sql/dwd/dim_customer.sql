-- =====================================================================
-- sql/dwd/dim_customer.sql
-- 功能：客户维度（CustomerMgmt NEW 底稿 + UPDCUST 字段级最新覆盖）
--
-- 口径（实测依据，data/loader.py customer_action 注释可溯）：
--   - 客户全集 = NEW 3056（每 c_id 恰 1 条，全字段画像）；
--     UPDCUST 691 客户最多 5 次、字段级补丁（tier/city/state/ctry/email，
--     不携带 gender/tax_id/dob/姓名）；INACT 434 仅标记，语义层无状态列故忽略。
--   - Tier = NEW 值被后续 UPDCUST 中"最新非空"覆盖。取法：ROW_NUMBER 窗口
--     按 action_ts DESC 取每 c_id 最新一条（实测 Doris 4.1 Nereids 不支持
--     相关子查询内 LIMIT：报 limit is not supported in correlated subquery）
--   - Gender = 仅 NEW 携带（1485/3056 非空；源数据大小写混合 M/F/m/f，
--     保真存储，查询侧需大小写不敏感处理）
--   - NetWorth / CreditRating：CustomerMgmt.xml 无此属性，取自 Prospect 名单，
--     按 (姓, 名, 城市, 州, 国家) 五键精确匹配（两侧 TRIM；实测无歧义：
--     客户侧 0 多对一、prospect 侧 0 自重复）。709/3056 命中，其余 NULL。
--     该口径为语义层扩展字段的最小实现，非 TPC-DI 官方规定（见 Known
--     Limitations 备注，data04 复核匹配率后固化）。
--
-- 重跑：INSERT OVERWRITE 整表覆盖，幂等。
-- =====================================================================

CREATE TABLE IF NOT EXISTS atlas.dwd.dim_customer (
    SK_CustomerID INT,
    Tier INT,
    Gender VARCHAR(8),
    NetWorth INT,
    CreditRating INT
);

INSERT OVERWRITE TABLE atlas.dwd.dim_customer
SELECT
    n.c_id AS SK_CustomerID,
    COALESCE(u.tier, n.tier) AS Tier,
    n.gender AS Gender,
    p.net_worth AS NetWorth,
    p.credit_rating AS CreditRating
FROM (
    SELECT c_id, tier, gender, l_name, f_name, city, state_prov, ctry
    FROM atlas.tpcdi.customer_action
    WHERE action_type = 'NEW'
) n
LEFT JOIN (
    SELECT c_id, tier
    FROM (
        SELECT
            c_id,
            tier,
            ROW_NUMBER() OVER (PARTITION BY c_id ORDER BY action_ts DESC) AS rn
        FROM atlas.tpcdi.customer_action
        WHERE action_type = 'UPDCUST' AND tier IS NOT NULL
    ) latest
    WHERE rn = 1
) u
    ON u.c_id = n.c_id
LEFT JOIN (
    SELECT
        TRIM(last_name)  AS last_name,
        TRIM(first_name) AS first_name,
        TRIM(city)       AS city,
        TRIM(state)      AS state,
        TRIM(country)    AS country,
        net_worth,
        credit_rating
    FROM atlas.tpcdi.prospect
    WHERE last_name IS NOT NULL AND first_name IS NOT NULL
) p
    ON p.last_name = TRIM(n.l_name)
   AND p.first_name = TRIM(n.f_name)
   AND p.city = TRIM(n.city)
   AND p.state = TRIM(n.state_prov)
   AND p.country = TRIM(n.ctry);
