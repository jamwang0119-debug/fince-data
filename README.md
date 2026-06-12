# fince-recon · 资金对账工具

银行流水与财务凭证的对账核销工具：**余额勾稽 + 分层匹配核销 + 差异工单闭环 + 审计全留痕**。
CLI + SQLite 形态，核心零第三方依赖（Python ≥ 3.11 标准库即可运行）。

完整方案设计见 [docs/资金对账系统方案设计.md](docs/资金对账系统方案设计.md)。

## 核心能力

- **余额对账层**：导入即做期初+发生=期末勾稽（发现流水缺漏/重复），对账后自动产出**银行存款余额调节表**（未达账项四分类 + 平衡总闸门校验）
- **四层匹配**：对账码分组轧差（1:1 / 1:N / M:N / 容差 / 组内部分核销）→ 多字段精确匹配（可救回对账码录错）→ subset-sum 凑数（建议核销）→ 户名相似度模糊匹配（AI 兜底，建议核销）
- **核销生命周期**：建议核销需人工 confirm；支持反核销（留痕不删除）；期间关账冻结写操作；同期间重跑增量安全，人工成果不被冲掉
- **差异工单**：自动开单带全上下文 + AI 一句话归因，按原因码路由责任方（配置化），状态机流转留痕，跨月结转后次月自动复对、自动闭环
- **AI 可插拔**：配置 `ANTHROPIC_API_KEY` 时模糊匹配判断用 Haiku、差异归因用 Sonnet 真实调用；未配置自动降级规则引擎，任何环境可跑

## 快速开始

```bash
# 一键端到端演示（生成两个月示例数据，跑完整流程）
bash samples/demo.sh

# 或手工流程
export PYTHONPATH=src           # 或 pip install -e .
python3 -m fince_recon init
python3 -m fince_recon accounts import templates/accounts.csv
python3 -m fince_recon period open 2026-06
python3 -m fince_recon import bank 银行流水.csv --period 2026-06
python3 -m fince_recon import vouchers 凭证.csv --period 2026-06
python3 -m fince_recon import balances 余额.csv --period 2026-06
python3 -m fince_recon check --period 2026-06      # 余额勾稽
python3 -m fince_recon run --period 2026-06        # 分层匹配
python3 -m fince_recon report --period 2026-06     # 调节表 + HTML 报告
```

## 常用命令

| 命令 | 说明 |
|------|------|
| `period open/close/reopen/list` | 期间管理（关账冻结写操作） |
| `import bank/vouchers/balances <file> --period P` | 导入（防重、作废剔除、红冲标记） |
| `check --period P` | 余额勾稽 |
| `run --period P` | 分层匹配（重跑安全） |
| `matches list/show` `confirm/reject <id> --by 工号` | 建议核销的确认/退回 |
| `unmatch <id> --by 工号 --reason 原因` | 反核销 |
| `match --bank 1,2 --voucher 3 --by 工号` | 人工核销 |
| `tickets list/show/board` `tickets move <id> --to 状态 --by 工号` | 工单流转与看板 |
| `carryover --from P1 --to P2` | 未达账项跨月结转 |
| `report --period P --out dir` | 调节表/核销明细/差异清单/工单 CSV + HTML 报告 |

数据模板见 `templates/`（CSV 表头中文，UTF-8，亦支持 .xlsx 需安装 openpyxl）；
参数与原因码路由配置见 `config/recon.toml`。

## 测试

```bash
pip install pytest && python3 -m pytest tests/ -q
```
