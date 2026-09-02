-- =====================================================================
-- sql/dwd/dim_broker.sql
-- 功能：经纪人维度（HR 员工中 job_code='314' 的子集）
--
-- 口径（实测依据，data/loader.py hr_employee 注释可溯）：
--   - broker 判据 = HR.job_code = '314'，恰 2865 人，三重吻合：
--     (a) Trade.t_exec_name distinct = 2865 且与 HR emp_id 双向相等
--     (b) HR_audit HR_BROKERS = 2865
--     (c) XML CA_B_ID 引用 2663（真子集，账户只挂部分 broker）
--   - Batch1 无 SCD（HR 每员工一行、无历史），SK_BrokerID = 自然键 emp_id；
--     语义层两列同值，保留双列以对齐 TPC-DI 表结构、可审计。
--   - 语义层未声明姓名列，此处只投影 4 个声明字段（最小表原则）。
--
-- 重跑：INSERT OVERWRITE 整表覆盖，幂等。
-- =====================================================================

CREATE TABLE IF NOT EXISTS atlas.dwd.dim_broker (
    SK_BrokerID INT,
    BrokerID INT,
    Branch VARCHAR(64),
    Office VARCHAR(32)
);

INSERT OVERWRITE TABLE atlas.dwd.dim_broker
SELECT
    emp_id AS SK_BrokerID,
    emp_id AS BrokerID,
    branch AS Branch,
    office AS Office
FROM atlas.tpcdi.hr_employee
WHERE job_code = '314';
