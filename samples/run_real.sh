#!/usr/bin/env bash
# 真实数据端到端跑通（不含任何数据，按你本地文件路径传参）。
#
# 用法:
#   bash samples/run_real.sh <银行流水文件> <资金付款文件> <资金收款文件> [期间]
# 例:
#   bash samples/run_real.sh 银行流水.xlsx 付款明细.xlsx 收款明细.xlsx 2026-06
#
# 三份文件分别对应 config/mappings.toml 里的 bank_c / fund_payment / fund_receipt。
# 若你的导出列名不同，改 config/mappings.toml 即可，无需改代码。
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src

BANK=${1:?第1个参数=银行流水文件}
PAY=${2:?第2个参数=资金付款文件}
RECV=${3:?第3个参数=资金收款文件}
PERIOD=${4:-2026-06}
DB=${DB:-real_recon.db}
MF=config/mappings.toml
RECON="python3 -m fince_recon --db $DB"
rm -f "$DB"

echo "=== 1. 初始化 + 从真实文件扫描账户主数据 ==="
$RECON init
$RECON accounts scan "$BANK" --mapping bank_c --mapping-file $MF
$RECON accounts scan "$PAY"  --mapping fund_payment --mapping-file $MF
$RECON accounts scan "$RECV" --mapping fund_receipt --mapping-file $MF

echo "=== 2. 开期间 + 导入三份数据（自动字段映射 + 对账码提取）==="
$RECON period open "$PERIOD"
$RECON import bank     "$BANK" --period "$PERIOD" --mapping bank_c       --mapping-file $MF
$RECON import vouchers "$PAY"  --period "$PERIOD" --mapping fund_payment  --mapping-file $MF
$RECON import vouchers "$RECV" --period "$PERIOD" --mapping fund_receipt  --mapping-file $MF

echo "=== 3. 执行分层匹配 ==="
$RECON run --period "$PERIOD"

echo "=== 4. 生成报告（调节表需另导入 balances；无余额表时仅出核销/差异/工单）==="
$RECON report --period "$PERIOD" --out "out/$PERIOD"

echo
echo "完成。核销明细/差异清单/工单/HTML 报告见 out/$PERIOD ，数据库 $DB"
echo "查看差异工单:   $RECON tickets list --period $PERIOD"
echo "查看某账户调节: 先 import balances 余额表.csv 再 report"
