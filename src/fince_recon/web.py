"""内置 Web 界面：零第三方依赖（仅标准库 http.server），复用对账引擎。

启动：fince-recon serve --db recon.db --port 8000
然后浏览器打开 http://127.0.0.1:8000

提供概览/调节表、核销记录（确认/退回/反核销）、差异工单（流转/看板）、
数据导入（支持映射）、一键加载演示数据等操作，便于演示与日常操作。
"""

from __future__ import annotations

import json
import tempfile
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fince_recon import importer
from fince_recon.ai import get_provider
from fince_recon.config import Config, load_config
from fince_recon.db import audit, connect, now, period_status
from fince_recon.engine import MatchEngine
from fince_recon.money import fmt
from fince_recon.quality import check_balances
from fince_recon.report import METHOD_LABELS, STATUS_LABELS, export, gather
from fince_recon.statement import build_statements
from fince_recon.tickets import carryover, move_ticket
from fince_recon.writeoff import confirm_match, manual_match, reject_match, unmatch

STATIC = Path(__file__).parent / "static"


def _json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class App:
    def __init__(self, db_path: str, cfg: Config):
        self.db_path = db_path
        self.cfg = cfg

    def conn(self):
        return connect(self.db_path)

    # ----------------------------------------------------------- GET 数据

    def state(self):
        c = self.conn()
        periods = [dict(r) for r in c.execute(
            "SELECT period, status FROM periods ORDER BY period DESC")]
        mappings = []
        try:
            from fince_recon import adapter
            mappings = sorted(adapter.load_mappings())
        except SystemExit:
            pass
        return {"periods": periods, "mappings": mappings, "db": self.db_path}

    def summary(self, period: str):
        c = self.conn()
        d = gather(c, period)
        rate_b = d["matched_bank"] / d["total_bank"] * 100 if d["total_bank"] else 0
        rate_f = d["matched_fin"] / d["total_fin"] * 100 if d["total_fin"] else 0
        statements = []
        for s in build_statements(c, period):
            statements.append({
                "account_no": s.account_no,
                "stmt_close": fmt(s.stmt_close), "ledger_close": fmt(s.ledger_close),
                "bank_in_unbooked": fmt(s.bank_in_unbooked),
                "bank_out_unbooked": fmt(s.bank_out_unbooked),
                "fin_in_pending": fmt(s.fin_in_pending),
                "fin_out_pending": fmt(s.fin_out_pending),
                "adjusted_stmt": fmt(s.adjusted_stmt),
                "adjusted_ledger": fmt(s.adjusted_ledger),
                "tolerance_total": fmt(s.tolerance_total),
                "residual": fmt(s.residual),
                "balanced": s.balanced, "pending": s.pending_suggestions,
                "has_balance": s.stmt_close is not None,
            })
        checks = []
        for r in check_balances(c, period):
            checks.append({"account_no": r.account_no, "side": r.side, "ok": r.ok,
                           "expected": fmt(r.expected_close), "actual": fmt(r.actual_close)})
        return {
            "period": period,
            "total_bank": d["total_bank"], "matched_bank": d["matched_bank"], "rate_bank": round(rate_b, 1),
            "total_fin": d["total_fin"], "matched_fin": d["matched_fin"], "rate_fin": round(rate_f, 1),
            "pending": len([m for m in d["matches"] if m["status"] == "pending"]),
            "auto_confirmed": len([m for m in d["matches"] if m["status"] in ("auto", "confirmed")]),
            "unmatched_bank": len(d["unmatched_bank"]), "unmatched_fin": len(d["unmatched_fin"]),
            "open_tickets": len([t for t in d["tickets"] if t["status"] != "已解决"]),
            "total_tickets": len(d["tickets"]),
            "statements": statements, "balance_checks": checks,
        }

    def matches(self, period: str, status: str | None):
        c = self.conn()
        q = "SELECT * FROM matches WHERE period=?"
        p = [period]
        if status:
            q += " AND status=?"; p.append(status)
        out = []
        for m in c.execute(q + " ORDER BY id DESC", p):
            out.append({
                "id": m["id"], "method": METHOD_LABELS.get(m["method"], m["method"]),
                "confidence": m["confidence"], "status": m["status"],
                "status_label": STATUS_LABELS.get(m["status"], m["status"]),
                "recon_code": m["recon_code"] or "", "bank_total": fmt(m["bank_total"]),
                "fin_total": fmt(m["fin_total"]), "diff": fmt(m["diff"]),
                "explanation": (m["explanation"] or m["tolerance_note"] or "")[:120],
                "created_by": m["created_by"],
            })
        return out

    def tickets(self, period: str, status: str | None):
        c = self.conn()
        q = "SELECT * FROM tickets WHERE (period=? OR carried_to=?)"
        p = [period, period]
        if status:
            q += " AND status=?"; p.append(status)
        out = []
        for t in c.execute(q + " ORDER BY id", p):
            out.append({
                "id": t["id"], "status": t["status"], "reason_label": t["reason_label"],
                "reason_code": t["reason_code"], "assignee": t["assignee"],
                "action": t["suggested_action"], "diff": fmt(t["diff_amount"]),
                "recon_code": t["recon_code"] or "", "ai_note": t["ai_note"] or "",
                "carried_to": t["carried_to"] or "", "resolved": t["resolved_note"] or "",
                "period": t["period"],
            })
        return out

    def board(self, period: str):
        c = self.conn()
        rows = c.execute(
            "SELECT assignee, status, COUNT(*) c, SUM(ABS(diff_amount)) s FROM tickets"
            " WHERE (period=? OR carried_to=?) AND status!='已解决'"
            " GROUP BY assignee, status ORDER BY assignee, status", (period, period))
        return [{"assignee": r["assignee"] or "-", "status": r["status"],
                 "count": r["c"], "amount": fmt(r["s"] or 0)} for r in rows]

    def audit_log(self, limit: int = 40):
        c = self.conn()
        rows = c.execute(
            "SELECT at, actor, action, object_type, object_id, detail FROM audit_log"
            " ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- POST 操作

    def do(self, action: str, body: dict):
        c = self.conn()
        by = (body.get("by") or "演示员").strip()
        if action == "period_open":
            p = body["period"].strip()
            if period_status(c, p) is not None:
                raise ValueError(f"期间 {p} 已存在")
            c.execute("INSERT INTO periods(period, status, opened_at) VALUES(?, 'open', ?)", (p, now()))
            audit(c, by, "period_open", "period", p); c.commit()
            return {"msg": f"期间 {p} 已开启"}
        if action == "period_close":
            p = body["period"].strip()
            c.execute("UPDATE periods SET status='closed', closed_at=? WHERE period=?", (now(), p))
            audit(c, by, "period_close", "period", p); c.commit()
            return {"msg": f"期间 {p} 已关账"}
        if action == "period_reopen":
            p = body["period"].strip()
            c.execute("UPDATE periods SET status='open', closed_at=NULL WHERE period=?", (p,))
            audit(c, by, "period_reopen", "period", p); c.commit()
            return {"msg": f"期间 {p} 已重开"}
        if action == "run":
            provider = get_provider(self.cfg)
            stats = MatchEngine(c, self.cfg, provider).run(body["period"].strip())
            return {"msg": f"对账完成（AI: {provider.name}）", "stats": stats}
        if action == "check":
            res = check_balances(c, body["period"].strip())
            if not res:
                return {"msg": "未导入余额表，无法勾稽"}
            bad = [r for r in res if not r.ok]
            return {"msg": ("全部勾稽通过 ✓" if not bad else f"{len(bad)} 项勾稽不平，疑似流水缺漏/重复"),
                    "ok": not bad}
        if action == "confirm":
            confirm_match(c, int(body["id"]), by)
            return {"msg": f"核销 {body['id']} 已确认"}
        if action == "reject":
            reject_match(c, int(body["id"]), by, body.get("reason", ""))
            return {"msg": f"核销 {body['id']} 已退回"}
        if action == "unmatch":
            unmatch(c, int(body["id"]), by, body.get("reason", ""))
            return {"msg": f"核销 {body['id']} 已反核销"}
        if action == "ticket_move":
            move_ticket(c, int(body["id"]), body["to"], by, body.get("note", ""))
            return {"msg": f"工单 {body['id']} → {body['to']}"}
        if action == "carryover":
            ids = carryover(c, body["from"].strip(), body["to"].strip(), by)
            return {"msg": f"已结转 {len(ids)} 张工单到 {body['to']}"}
        if action == "report":
            files = export(c, body["period"].strip(), body.get("out", f"out/{body['period'].strip()}"))
            return {"msg": "报告已导出", "files": files}
        if action == "seed_demo":
            return self.seed_demo()
        raise ValueError(f"未知操作: {action}")

    def upload(self, kind: str, period: str, mapping: str | None, data: bytes, filename: str):
        c = self.conn()
        suffix = Path(filename).suffix or ".csv"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
            tf.write(data)
            tmp = tf.name
        if mapping:
            from fince_recon import adapter
            m = adapter.load_mappings()[mapping]
            mkind, rows, skipped = adapter.adapt(tmp, m)
            if kind == "accounts" or mkind != kind:
                pass
            if mkind == "bank":
                rep = importer.import_bank(c, filename, period, self.cfg, rows=rows)
            else:
                rep = importer.import_vouchers(c, filename, period, self.cfg, rows=rows)
            return {"msg": rep.summary(), "skipped": skipped}
        fn = {
            "accounts": lambda: importer.import_accounts(c, tmp),
            "bank": lambda: importer.import_bank(c, tmp, period, self.cfg),
            "vouchers": lambda: importer.import_vouchers(c, tmp, period, self.cfg),
            "balances": lambda: importer.import_balances(c, tmp, period),
            "aliases": lambda: importer.import_aliases(c, tmp),
        }[kind]
        return {"msg": fn().summary()}

    def scan_accounts(self, mapping: str, data: bytes, filename: str):
        from fince_recon import adapter
        c = self.conn()
        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(filename).suffix or ".csv") as tf:
            tf.write(data); tmp = tf.name
        rows = adapter.distinct_accounts(tmp, adapter.load_mappings()[mapping])
        return {"msg": importer.import_accounts(c, filename, rows=rows).summary()}

    def seed_demo(self):
        """一键加载内置两个月演示数据（需在仓库根目录运行，含 samples/）。"""
        import importlib.util
        gen = Path.cwd() / "samples" / "generate_demo.py"
        if not gen.is_file():
            raise ValueError("未找到 samples/generate_demo.py，请在仓库根目录启动 serve")
        spec = importlib.util.spec_from_file_location("generate_demo", gen)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sp = Path.cwd() / "samples"
        c = self.conn()
        importer.import_accounts(c, str(sp / "accounts.csv"))
        importer.import_aliases(c, str(sp / "aliases.csv"))
        for period in ("2026-05", "2026-06"):
            if period_status(c, period) is None:
                c.execute("INSERT INTO periods(period, status, opened_at) VALUES(?, 'open', ?)",
                          (period, now()))
        c.commit()
        for period in ("2026-05", "2026-06"):
            importer.import_bank(c, str(sp / f"bank_{period}.csv"), period, self.cfg)
            importer.import_vouchers(c, str(sp / f"vouchers_{period}.csv"), period, self.cfg)
            importer.import_balances(c, str(sp / f"balances_{period}.csv"), period, )
            MatchEngine(c, self.cfg, get_provider(self.cfg)).run(period)
        return {"msg": "演示数据已加载（2026-05 / 2026-06），并已执行对账"}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静默默认访问日志
            pass

        def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _err(self, e):
            self._send(400, _json({"error": str(e)}))

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path in ("/", "/index.html"):
                    html = (STATIC / "index.html").read_bytes()
                    return self._send(200, html, "text/html; charset=utf-8")
                if u.path == "/api/state":
                    return self._send(200, _json(app.state()))
                period = q.get("period", [""])[0]
                if u.path == "/api/summary":
                    return self._send(200, _json(app.summary(period)))
                if u.path == "/api/matches":
                    return self._send(200, _json(app.matches(period, q.get("status", [None])[0])))
                if u.path == "/api/tickets":
                    return self._send(200, _json(app.tickets(period, q.get("status", [None])[0])))
                if u.path == "/api/board":
                    return self._send(200, _json(app.board(period)))
                if u.path == "/api/audit":
                    return self._send(200, _json(app.audit_log(int(q.get("limit", ["40"])[0]))))
                return self._send(404, _json({"error": "not found"}))
            except (Exception, SystemExit) as e:  # 引擎用 SystemExit 表达用户级错误
                traceback.print_exc()
                return self._err(e)

        def do_POST(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            length = int(self.headers.get("Content-Length", "0"))
            data = self.rfile.read(length) if length else b""
            try:
                if u.path == "/api/upload":
                    kind = q.get("kind", ["bank"])[0]
                    period = q.get("period", [""])[0]
                    mapping = q.get("mapping", [""])[0] or None
                    fname = q.get("filename", ["upload.csv"])[0]
                    return self._send(200, _json(app.upload(kind, period, mapping, data, fname)))
                if u.path == "/api/scan_accounts":
                    mapping = q.get("mapping", [""])[0]
                    fname = q.get("filename", ["upload.csv"])[0]
                    return self._send(200, _json(app.scan_accounts(mapping, data, fname)))
                body = json.loads(data.decode("utf-8")) if data else {}
                action = u.path.removeprefix("/api/")
                return self._send(200, _json(app.do(action, body)))
            except (Exception, SystemExit) as e:  # 引擎用 SystemExit 表达用户级错误
                traceback.print_exc()
                return self._err(e)

    return Handler


def serve(db_path: str, cfg: Config, host: str = "127.0.0.1", port: int = 8000):
    app = App(db_path, cfg)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"对账 Web 界面已启动：http://{host}:{port}   （数据库 {db_path}，Ctrl+C 退出）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
