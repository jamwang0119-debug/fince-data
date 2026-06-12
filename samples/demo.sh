#!/usr/bin/env bash
# 一键端到端演示：两个月对账全流程（含建议核销确认、跨月结转、自动闭环、关账）
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
DB=${DB:-demo_recon.db}
RECON="python3 -m fince_recon --db $DB"
rm -f "$DB"

step() { echo; echo "=============== $* ==============="; }

step "0. 生成演示数据"
python3 samples/generate_demo.py

step "1. 初始化与主数据"
$RECON init
$RECON accounts import samples/accounts.csv
$RECON aliases import samples/aliases.csv

step "2. 开启期间 2026-05 并导入三类数据"
$RECON period open 2026-05
$RECON import bank samples/bank_2026-05.csv --period 2026-05
$RECON import vouchers samples/vouchers_2026-05.csv --period 2026-05
$RECON import balances samples/balances_2026-05.csv --period 2026-05

step "3. 余额勾稽（对账前置总闸门）"
$RECON check --period 2026-05

step "4. 执行分层匹配"
$RECON run --period 2026-05

step "5. 建议核销待确认清单（subset-sum / 模糊匹配）"
$RECON matches list --period 2026-05 --status pending

step "6. 人工确认全部建议核销"
for id in $(python3 - "$DB" <<'EOF'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
for (i,) in conn.execute("SELECT id FROM matches WHERE status='pending'"):
    print(i)
EOF
); do
  $RECON confirm "$id" --by 张三
done

step "7. 差异工单与看板"
$RECON tickets list --period 2026-05
$RECON tickets board --period 2026-05

step "8. 5 月对账报告（调节表应平衡）"
$RECON report --period 2026-05 --out out/2026-05

step "9. 跨月结转未达账项到 2026-06"
$RECON carryover --from 2026-05 --to 2026-06

step "10. 6 月对账：在途款到账自动复对、工单自动闭环"
$RECON period open 2026-06
$RECON import bank samples/bank_2026-06.csv --period 2026-06
$RECON import vouchers samples/vouchers_2026-06.csv --period 2026-06
$RECON import balances samples/balances_2026-06.csv --period 2026-06
$RECON check --period 2026-06
$RECON run --period 2026-06
$RECON tickets list --period 2026-06

step "11. 6 月对账报告"
$RECON report --period 2026-06 --out out/2026-06

step "12. 5 月关账（之后写操作被冻结）"
$RECON period close 2026-05

echo
echo "演示完成。报告见 out/2026-05 与 out/2026-06，数据库 $DB"
