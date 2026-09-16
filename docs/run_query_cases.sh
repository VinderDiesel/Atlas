#!/usr/bin/env bash
# Atlas query 真实可用测试用例 · 一键复跑（配套 docs/atlas_query_test_cases.md）
#
# 逐条跑过文档用例并比对退出码（不比对具体值——值随快照变，见文档 §12.6）。
# 前置：Doris 在线 + data/snapshots/*.meta.json 存在（否则 query 退出码 1）。
# RLS 用例（F-19/F-20）需 .env 的 ATLAS_JWT_SECRET，未配置时自动跳过。
#
# 用法：bash docs/run_query_cases.sh
set -u

ATLAS=".venv/bin/python -m agent.cli"
pass=0; fail=0; skip=0

run() {  # run <预期退出码> <问句> [额外参数...]
  local expect="$1"; shift
  local q="$1"; shift
  $ATLAS query "$q" "$@" --format json >/dev/null 2>&1
  local rc=$?
  if [ "$rc" = "$expect" ]; then
    echo "  ok   (exit=$rc) $q"; pass=$((pass+1))
  else
    echo "  FAIL (expect=$expect got=$rc) $q"; fail=$((fail+1))
  fi
}

run_rls() {  # 需 ATLAS_JWT_SECRET，缺省跳过
  if ! grep -q ATLAS_JWT_SECRET .env 2>/dev/null; then
    echo "  skip (无 ATLAS_JWT_SECRET) $2"; skip=$((skip+1)); return
  fi
  run "$@"
}

echo "== 金融·基础聚合（年/季/月/日）=="
run 0 "2015 年总交易额"
run 0 "2015 年佣金收入"
run 0 "2015 年第二季度交易额"
run 0 "2015 年 5 月交易笔数"
run 0 "2015 年 6 月 30 日持仓市值"
run 0 "2015 年交易笔数"
run 0 "2015 年活跃客户数"
run 0 "2015 年总成交量"
run 0 "2015 年平均成交价"

echo "== 金融·维度分组与 TopN =="
run 0 "按客户等级统计 2015 年交易额，列出前 5 名"
run 0 "按证券类型统计 2015 年成交量，列出前 3 名"
run 0 "按分支统计 2015 年佣金收入，列出前 5 名"

echo "== 金融·过滤 =="
run 0 "只看交易所 NASDAQ 的 2015 年交易额是多少？"
run 0 "只看客户等级 3 的客户，2015 年交易额是多少？"

echo "== 金融·派生指标 =="
run 0 "2015 年佣金率是多少？"
run 0 "2015 年户均持仓市值"
run 0 "2015 年平均每笔成交金额是多少？"
run 0 "2015 年平均每笔佣金是多少？"

echo "== 金融·行级策略（RLS）=="
run_rls 0 "2015 年总交易额" --role hq_admin
run_rls 0 "2015 年总交易额" --role branch_manager --role-ctx branch=BR_A1

echo "== 金融·英文问句 =="
run 0 "What was the total trade value in 2015?"
run 0 "Show commission revenue by branch in 2015 and list the top 5 branches"
run 0 "What was the commission revenue only for exchange NASDAQ in 2015?"

echo "== 零售域 =="
run 0 "2001 年销售额" --domain retail
run 0 "1999 年第一季度销售额" --domain retail
run 0 "2000 年 5 月的销售额" --domain retail
run 0 "2000 年净利润" --domain retail
run 0 "按品类统计 2001 年销售额，列出前 3 名" --domain retail
run 0 "只看城市 Midway 的 2001 年销售额是多少？" --domain retail
run 0 "What were total sales in 1999?" --domain retail
run 0 "2000 年客单价" --domain retail
run 0 "2000 年订单量" --domain retail

echo "== 边界与澄清 =="
run 2 "2023 年客户满意度评分"
run 2 "2015 年总交易额和佣金收入分别是多少？"
run 2 "最近交易情况怎么样？"
run 0 "2001 年销售额同比" --domain retail   # CTE 豁免后（工作项 11）：一步问数直出

echo ""
echo "通过 $pass / 失败 $fail / 跳过 $skip"
[ "$fail" = 0 ] || exit 1
