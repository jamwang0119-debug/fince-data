"""fince-recon 命令行入口。"""

from __future__ import annotations

import argparse
import sys

from fince_recon import __version__
from fince_recon.ai import get_provider
from fince_recon.config import load_config
from fince_recon.db import audit, connect, now, period_status
from fince_recon.engine import MatchEngine
from fince_recon.money import fmt
from fince_recon.quality import check_balances
from fince_recon.report import METHOD_LABELS, STATUS_LABELS, console_summary, export
from fince_recon.tickets import carryover, move_ticket
from fince_recon.writeoff import confirm_match, manual_match, reject_match, unmatch
from fince_recon import importer


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fince-recon",
        description="资金对账工具：余额勾稽 + 分层匹配核销 + 差异工单闭环",
    )
    p.add_argument("--db", help="SQLite 数据库路径（默认取配置或 recon.db）")
    p.add_argument("--config", help="配置文件路径（默认 config/recon.toml）")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化数据库")

    sp = sub.add_parser("accounts", help="账户主数据")
    sp.add_argument("action", choices=["import", "list", "scan"])
    sp.add_argument("file", nargs="?")
    sp.add_argument("--mapping", help="scan：真实文件的映射名")
    sp.add_argument("--mapping-file", help="映射配置路径")

    sp = sub.add_parser("aliases", help="别名表")
    sp.add_argument("action", choices=["import"])
    sp.add_argument("file")

    sp = sub.add_parser("period", help="期间管理")
    sp.add_argument("action", choices=["open", "close", "reopen", "list"])
    sp.add_argument("period", nargs="?")
    sp.add_argument("--by", default="system")

    sp = sub.add_parser("import", help="导入银行流水/凭证/余额表")
    sp.add_argument("kind", choices=["bank", "vouchers", "balances"])
    sp.add_argument("file")
    sp.add_argument("--period", required=True)
    sp.add_argument("--mapping", help="真实导出文件的字段映射名（见 config/mappings.toml）")
    sp.add_argument("--mapping-file", help="映射配置路径（默认 config/mappings.toml）")

    sp = sub.add_parser("check", help="余额勾稽与数据质量校验")
    sp.add_argument("--period", required=True)

    sp = sub.add_parser("run", help="执行分层匹配（增量重跑安全）")
    sp.add_argument("--period", required=True)
    sp.add_argument("--note", default="")

    sp = sub.add_parser("matches", help="核销记录查询")
    sp.add_argument("action", choices=["list", "show"])
    sp.add_argument("id", nargs="?", type=int)
    sp.add_argument("--period")
    sp.add_argument("--status", choices=["auto", "pending", "confirmed", "cancelled"])

    sp = sub.add_parser("confirm", help="确认建议核销")
    sp.add_argument("match_id", type=int)
    sp.add_argument("--by", required=True, help="操作人工号")

    sp = sub.add_parser("reject", help="退回建议核销")
    sp.add_argument("match_id", type=int)
    sp.add_argument("--by", required=True)
    sp.add_argument("--reason", default="")

    sp = sub.add_parser("unmatch", help="反核销（撤销已生效核销）")
    sp.add_argument("match_id", type=int)
    sp.add_argument("--by", required=True)
    sp.add_argument("--reason", required=True)

    sp = sub.add_parser("match", help="人工核销")
    sp.add_argument("--period", required=True)
    sp.add_argument("--bank", default="", help="银行流水ID，逗号分隔")
    sp.add_argument("--voucher", default="", help="凭证行ID，逗号分隔")
    sp.add_argument("--by", required=True)
    sp.add_argument("--note", default="")
    sp.add_argument("--allow-diff", action="store_true")

    sp = sub.add_parser("tickets", help="差异工单")
    sp.add_argument("action", choices=["list", "show", "move", "board"])
    sp.add_argument("id", nargs="?", type=int)
    sp.add_argument("--period")
    sp.add_argument("--status")
    sp.add_argument("--assignee")
    sp.add_argument("--to", help="move 的目标状态")
    sp.add_argument("--by", default="system")
    sp.add_argument("--note", default="")

    sp = sub.add_parser("carryover", help="跨月结转未达账项工单")
    sp.add_argument("--from", dest="from_period", required=True)
    sp.add_argument("--to", dest="to_period", required=True)
    sp.add_argument("--by", default="system")

    sp = sub.add_parser("report", help="生成调节表 + HTML 报告 + CSV 导出")
    sp.add_argument("--period", required=True)
    sp.add_argument("--out", default="out")

    sp = sub.add_parser("status", help="期间对账状态摘要")
    sp.add_argument("--period", required=True)

    sp = sub.add_parser("serve", help="启动 Web 界面（浏览器操作/演示）")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    db_path = args.db or cfg.db_path
    conn = connect(db_path)

    if args.cmd == "init":
        print(f"数据库已初始化: {db_path}")
        return 0

    if args.cmd == "accounts":
        if args.action == "import":
            if not args.file:
                raise SystemExit("用法: fince-recon accounts import accounts.csv")
            print(importer.import_accounts(conn, args.file).summary())
        elif args.action == "scan":
            if not args.file or not args.mapping:
                raise SystemExit("用法: fince-recon accounts scan <真实文件> --mapping <名字>")
            from fince_recon import adapter

            mappings = adapter.load_mappings(args.mapping_file)
            if args.mapping not in mappings:
                raise SystemExit(f"映射 {args.mapping!r} 不存在，可选: {sorted(mappings)}")
            rows = adapter.distinct_accounts(args.file, mappings[args.mapping])
            print(importer.import_accounts(conn, args.file, rows=rows).summary())
            print("  注：科目编码取默认值，请按需用 accounts import 覆盖修正")
        else:
            for r in conn.execute("SELECT * FROM accounts ORDER BY account_no"):
                print(f"  {r['account_no']}  {r['account_name'] or '-'}  "
                      f"{r['bank_name'] or '-'}  科目 {r['ledger_account'] or '-'}  {r['currency']}")
        return 0

    if args.cmd == "aliases":
        print(importer.import_aliases(conn, args.file).summary())
        return 0

    if args.cmd == "period":
        if args.action == "list":
            for r in conn.execute("SELECT * FROM periods ORDER BY period"):
                print(f"  {r['period']}  {r['status']}  开:{r['opened_at'] or '-'}  关:{r['closed_at'] or '-'}")
            return 0
        if not args.period:
            raise SystemExit("请指定期间，如 2026-06")
        st = period_status(conn, args.period)
        if args.action == "open":
            if st is not None:
                raise SystemExit(f"期间 {args.period} 已存在（{st}）")
            conn.execute(
                "INSERT INTO periods(period, status, opened_at) VALUES(?, 'open', ?)",
                (args.period, now()),
            )
            audit(conn, args.by, "period_open", "period", args.period)
            print(f"期间 {args.period} 已开启")
        elif args.action == "close":
            if st != "open":
                raise SystemExit(f"期间 {args.period} 当前状态 {st}，无法关账")
            conn.execute(
                "UPDATE periods SET status='closed', closed_at=? WHERE period=?",
                (now(), args.period),
            )
            audit(conn, args.by, "period_close", "period", args.period)
            print(f"期间 {args.period} 已关账，所有写操作冻结")
        elif args.action == "reopen":
            if st != "closed":
                raise SystemExit(f"期间 {args.period} 当前状态 {st}，无法重开")
            conn.execute(
                "UPDATE periods SET status='open', closed_at=NULL WHERE period=?",
                (args.period,),
            )
            audit(conn, args.by, "period_reopen", "period", args.period)
            print(f"期间 {args.period} 已重开（留痕）")
        conn.commit()
        return 0

    if args.cmd == "import":
        if args.mapping:
            from fince_recon import adapter

            mappings = adapter.load_mappings(args.mapping_file)
            if args.mapping not in mappings:
                raise SystemExit(
                    f"映射 {args.mapping!r} 不存在，可选: {sorted(mappings)}"
                )
            m = mappings[args.mapping]
            kind, rows, skipped = adapter.adapt(args.file, m)
            if kind != args.kind:
                raise SystemExit(
                    f"映射 {args.mapping} 的 side={m.side}，应配合 import {kind}，而非 {args.kind}"
                )
            if skipped:
                print(f"（适配 {args.mapping}：跳过 {skipped} 行无金额/无效行）")
            if kind == "bank":
                rep = importer.import_bank(conn, args.file, args.period, cfg, rows=rows)
            else:
                rep = importer.import_vouchers(conn, args.file, args.period, cfg, rows=rows)
            print(rep.summary())
            return 0
        fn = {
            "bank": lambda: importer.import_bank(conn, args.file, args.period, cfg),
            "vouchers": lambda: importer.import_vouchers(conn, args.file, args.period, cfg),
            "balances": lambda: importer.import_balances(conn, args.file, args.period),
        }[args.kind]
        print(fn().summary())
        return 0

    if args.cmd == "check":
        results = check_balances(conn, args.period)
        if not results:
            print(f"期间 {args.period} 未导入余额表，无法勾稽（先 import balances）")
            return 1
        print(f"== 余额勾稽 {args.period} ==")
        bad = 0
        for r in results:
            print(r.describe())
            bad += 0 if r.ok else 1
        if bad:
            print(f"⚠ {bad} 项勾稽不平，疑似流水缺漏/重复，建议先修复数据再执行 run")
            return 1
        print("全部勾稽通过 ✓")
        return 0

    if args.cmd == "run":
        provider = get_provider(cfg)
        engine = MatchEngine(conn, cfg, provider)
        stats = engine.run(args.period, args.note)
        print(f"== 对账批次完成（AI Provider: {provider.name}）==")
        for k in sorted(stats):
            print(f"  {k}: {stats[k]}")
        print()
        print(console_summary(conn, args.period))
        return 0

    if args.cmd == "matches":
        if args.action == "show":
            if not args.id:
                raise SystemExit("用法: fince-recon matches show <id>")
            m = conn.execute("SELECT * FROM matches WHERE id=?", (args.id,)).fetchone()
            if not m:
                raise SystemExit(f"核销 {args.id} 不存在")
            print(f"核销 #{m['id']}  {METHOD_LABELS.get(m['method'], m['method'])}"
                  f"  {m['confidence']}  {STATUS_LABELS.get(m['status'], m['status'])}")
            print(f"  期间 {m['period']}  对账码 {m['recon_code'] or '-'}")
            print(f"  银行合计 {fmt(m['bank_total'])}  凭证合计 {fmt(m['fin_total'])}"
                  f"  差额 {fmt(m['diff'])}")
            if m["explanation"]:
                print(f"  说明: {m['explanation']}")
            if m["tolerance_note"]:
                print(f"  容差: {m['tolerance_note']}")
            if m["cancel_reason"]:
                print(f"  撤销: {m['cancelled_by']} @ {m['cancelled_at']}  原因: {m['cancel_reason']}")
            for link in conn.execute(
                "SELECT side, record_id FROM match_links WHERE match_id=?", (args.id,)
            ):
                if link["side"] == "bank":
                    r = conn.execute("SELECT * FROM bank_txns WHERE id=?",
                                     (link["record_id"],)).fetchone()
                    print(f"  [银行 {r['id']}] {r['txn_date']} {fmt(r['amount'])} {r['direction']}"
                          f" {r['counterparty_name'] or '-'} 流水号{r['bank_txn_no']}")
                else:
                    r = conn.execute("SELECT * FROM vouchers WHERE id=?",
                                     (link["record_id"],)).fetchone()
                    print(f"  [凭证 {r['id']}] {r['voucher_date']} {fmt(r['amount'])} {r['direction']}"
                          f" {r['counterparty'] or '-'} {r['voucher_no']}-{r['line_no']}")
            return 0
        q = "SELECT * FROM matches WHERE 1=1"
        params: list = []
        if args.period:
            q += " AND period=?"
            params.append(args.period)
        if args.status:
            q += " AND status=?"
            params.append(args.status)
        for m in conn.execute(q + " ORDER BY id", params):
            print(f"  #{m['id']}  {m['period']}  {METHOD_LABELS.get(m['method'], m['method'])}"
                  f"  {m['confidence']}  {STATUS_LABELS.get(m['status'], m['status'])}"
                  f"  码:{m['recon_code'] or '-'}  差额 {fmt(m['diff'])}")
        return 0

    if args.cmd == "confirm":
        confirm_match(conn, args.match_id, args.by)
        print(f"核销 {args.match_id} 已确认生效（操作人 {args.by}）")
        return 0

    if args.cmd == "reject":
        reject_match(conn, args.match_id, args.by, args.reason)
        print(f"核销 {args.match_id} 已退回，记录释放回未匹配")
        return 0

    if args.cmd == "unmatch":
        unmatch(conn, args.match_id, args.by, args.reason)
        print(f"核销 {args.match_id} 已撤销（反核销），记录释放回未匹配")
        return 0

    if args.cmd == "match":
        bank_ids = [int(x) for x in args.bank.split(",") if x.strip()]
        voucher_ids = [int(x) for x in args.voucher.split(",") if x.strip()]
        mid = manual_match(conn, cfg, args.period, bank_ids, voucher_ids,
                           args.by, args.note, args.allow_diff)
        print(f"人工核销完成，核销ID {mid}")
        return 0

    if args.cmd == "tickets":
        if args.action == "move":
            if not args.id or not args.to:
                raise SystemExit("用法: fince-recon tickets move <id> --to 处理中 --by 工号")
            move_ticket(conn, args.id, args.to, args.by, args.note)
            print(f"工单 {args.id} → {args.to}")
            return 0
        if args.action == "show":
            t = conn.execute("SELECT * FROM tickets WHERE id=?", (args.id,)).fetchone()
            if not t:
                raise SystemExit(f"工单 {args.id} 不存在")
            print(f"工单 #{t['id']}  [{t['status']}]  {t['reason_label']}"
                  f"  责任方:{t['assignee']}  建议:{t['suggested_action']}")
            print(f"  期间 {t['period']}  对账码 {t['recon_code'] or '-'}"
                  f"  差异金额 {fmt(t['diff_amount'])}")
            print(f"  AI 归因: {t['ai_note'] or '-'}")
            if t["carried_to"]:
                print(f"  结转: {t['carried_from'] or t['period']} → {t['carried_to']}")
            if t["resolved_note"]:
                print(f"  解决: {t['resolved_note']}")
            for link in conn.execute(
                "SELECT side, record_id FROM ticket_links WHERE ticket_id=?", (args.id,)
            ):
                table = "bank_txns" if link["side"] == "bank" else "vouchers"
                r = conn.execute(f"SELECT * FROM {table} WHERE id=?",
                                 (link["record_id"],)).fetchone()
                if link["side"] == "bank":
                    print(f"  [银行 {r['id']}] {r['txn_date']} {fmt(r['amount'])}"
                          f" {r['direction']} {r['summary'] or '-'} [{r['status']}]")
                else:
                    print(f"  [凭证 {r['id']}] {r['voucher_date']} {fmt(r['amount'])}"
                          f" {r['direction']} {r['summary'] or '-'} [{r['status']}]")
            print("  流转历史:")
            for e in conn.execute(
                "SELECT * FROM ticket_events WHERE ticket_id=? ORDER BY id", (args.id,)
            ):
                print(f"    {e['at']}  {e['from_status'] or '·'} → {e['to_status']}"
                      f"  by {e['actor']}  {e['note'] or ''}")
            return 0
        if args.action == "board":
            q = "SELECT assignee, status, COUNT(*) c, SUM(ABS(diff_amount)) s FROM tickets"
            params = []
            if args.period:
                q += " WHERE period=? OR carried_to=?"
                params = [args.period, args.period]
            q += " GROUP BY assignee, status ORDER BY assignee, status"
            print("== 工单看板（责任方 × 状态）==")
            for r in conn.execute(q, params):
                print(f"  {r['assignee'] or '-'}  {r['status']}  {r['c']} 张"
                      f"  差异合计 {fmt(r['s'] or 0)}")
            return 0
        q = "SELECT * FROM tickets WHERE 1=1"
        params = []
        if args.period:
            q += " AND (period=? OR carried_to=?)"
            params += [args.period, args.period]
        if args.status:
            q += " AND status=?"
            params.append(args.status)
        if args.assignee:
            q += " AND assignee=?"
            params.append(args.assignee)
        for t in conn.execute(q + " ORDER BY id", params):
            print(f"  #{t['id']}  [{t['status']}]  {t['reason_label']}"
                  f"  {t['assignee']}  差异 {fmt(t['diff_amount'])}"
                  f"  码:{t['recon_code'] or '-'}  {t['period']}")
        return 0

    if args.cmd == "carryover":
        ids = carryover(conn, args.from_period, args.to_period, args.by)
        print(f"已结转 {len(ids)} 张工单到 {args.to_period}: {ids}")
        return 0

    if args.cmd == "report":
        files = export(conn, args.period, args.out)
        print(console_summary(conn, args.period))
        print("\n已导出:")
        for f in files:
            print(f"  {f}")
        return 0

    if args.cmd == "status":
        print(console_summary(conn, args.period))
        return 0

    if args.cmd == "serve":
        from fince_recon.web import serve

        conn.close()
        serve(db_path, cfg, args.host, args.port)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
