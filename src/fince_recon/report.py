"""对账报告：终端摘要、HTML 报告、CSV 明细导出（调节表/差异清单/核销明细/工单）。"""

from __future__ import annotations

import csv
import html
import sqlite3
from pathlib import Path

from fince_recon.money import fmt
from fince_recon.statement import ReconStatement, build_statements

METHOD_LABELS = {
    "code_1_1": "对账码1:1",
    "code_group": "对账码分组轧差",
    "code_tolerance": "对账码容差核销",
    "code_partial": "组内部分核销",
    "exact": "多字段精确",
    "subset_sum": "凑数匹配",
    "fuzzy": "模糊匹配",
    "manual": "人工核销",
}
STATUS_LABELS = {
    "auto": "自动核销",
    "pending": "建议核销(待确认)",
    "confirmed": "已确认核销",
    "cancelled": "已撤销",
}


def gather(conn: sqlite3.Connection, period: str) -> dict:
    matches = conn.execute(
        "SELECT * FROM matches WHERE period=? ORDER BY id", (period,)
    ).fetchall()
    unmatched_bank = conn.execute(
        "SELECT * FROM bank_txns WHERE status='unmatched' AND (period=? OR carried_to=?)"
        " ORDER BY account_no, txn_date",
        (period, period),
    ).fetchall()
    unmatched_fin = conn.execute(
        "SELECT * FROM vouchers WHERE status='unmatched' AND (period=? OR carried_to=?)"
        " ORDER BY account_no, voucher_date",
        (period, period),
    ).fetchall()
    tickets = conn.execute(
        "SELECT * FROM tickets WHERE period=? OR carried_to=? ORDER BY id",
        (period, period),
    ).fetchall()
    total_bank = conn.execute(
        "SELECT COUNT(*) c FROM bank_txns WHERE period=? OR carried_to=?",
        (period, period),
    ).fetchone()["c"]
    total_fin = conn.execute(
        "SELECT COUNT(*) c FROM vouchers WHERE period=? OR carried_to=?",
        (period, period),
    ).fetchone()["c"]
    matched_bank = conn.execute(
        "SELECT COUNT(*) c FROM bank_txns WHERE status='matched'"
        " AND (period=? OR carried_to=?)",
        (period, period),
    ).fetchone()["c"]
    matched_fin = conn.execute(
        "SELECT COUNT(*) c FROM vouchers WHERE status='matched'"
        " AND (period=? OR carried_to=?)",
        (period, period),
    ).fetchone()["c"]
    return {
        "matches": matches,
        "unmatched_bank": unmatched_bank,
        "unmatched_fin": unmatched_fin,
        "tickets": tickets,
        "total_bank": total_bank,
        "total_fin": total_fin,
        "matched_bank": matched_bank,
        "matched_fin": matched_fin,
        "statements": build_statements(conn, period),
    }


def console_summary(conn: sqlite3.Connection, period: str) -> str:
    d = gather(conn, period)
    rate_b = d["matched_bank"] / d["total_bank"] * 100 if d["total_bank"] else 0
    rate_f = d["matched_fin"] / d["total_fin"] * 100 if d["total_fin"] else 0
    lines = [
        f"== 对账摘要 {period} ==",
        f"银行流水: {d['total_bank']} 笔，已核销 {d['matched_bank']} 笔（{rate_b:.1f}%）",
        f"财务凭证: {d['total_fin']} 行，已核销 {d['matched_fin']} 行（{rate_f:.1f}%）",
        f"核销记录: {len([m for m in d['matches'] if m['status'] in ('auto','confirmed')])} 笔生效，"
        f"{len([m for m in d['matches'] if m['status'] == 'pending'])} 笔建议待确认",
        f"未匹配: 银行 {len(d['unmatched_bank'])} 笔 / 凭证 {len(d['unmatched_fin'])} 行",
        f"差异工单: {len(d['tickets'])} 张（未解决 "
        f"{len([t for t in d['tickets'] if t['status'] != '已解决'])} 张）",
        "",
        "-- 银行存款余额调节表 --",
    ]
    for s in d["statements"]:
        lines.append(_stmt_text(s))
    return "\n".join(lines)


def _stmt_text(s: ReconStatement) -> str:
    if s.stmt_close is None:
        return f"  账户 {s.account_no}: 未导入余额表，跳过调节表"
    mark = "✓ 平衡" if s.balanced else f"✗ 存在未解释差异 {fmt(s.residual)} 元"
    note = f"（含 {s.pending_suggestions} 笔建议核销按未达列示）" if s.pending_suggestions else ""
    return (
        f"  账户 {s.account_no}: 对账单期末 {fmt(s.stmt_close)}"
        f" + 企业已收银行未收 {fmt(s.fin_in_pending)}"
        f" − 企业已付银行未付 {fmt(s.fin_out_pending)} = {fmt(s.adjusted_stmt)}\n"
        f"      账面期末 {fmt(s.ledger_close)}"
        f" + 银行已收企业未收 {fmt(s.bank_in_unbooked)}"
        f" − 银行已付企业未付 {fmt(s.bank_out_unbooked)} = {fmt(s.adjusted_ledger)}\n"
        f"      容差核销累计差额 {fmt(s.tolerance_total)} → {mark} {note}"
    )


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fobj:
        w = csv.writer(fobj)
        w.writerow(header)
        w.writerows(rows)


def export(conn: sqlite3.Connection, period: str, out_dir: str) -> list[str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    d = gather(conn, period)
    files: list[str] = []

    p = out / f"调节表_{period}.csv"
    _write_csv(
        p,
        ["账号", "对账单期末余额", "企业已收银行未收", "企业已付银行未付", "调节后对账单余额",
         "账面期末余额", "银行已收企业未收", "银行已付企业未付", "调节后账面余额",
         "容差核销累计差额", "未解释差异", "是否平衡", "建议核销待确认笔数"],
        [
            [s.account_no, fmt(s.stmt_close), fmt(s.fin_in_pending), fmt(s.fin_out_pending),
             fmt(s.adjusted_stmt), fmt(s.ledger_close), fmt(s.bank_in_unbooked),
             fmt(s.bank_out_unbooked), fmt(s.adjusted_ledger), fmt(s.tolerance_total),
             fmt(s.residual), "" if s.balanced is None else ("是" if s.balanced else "否"),
             s.pending_suggestions]
            for s in d["statements"]
        ],
    )
    files.append(str(p))

    p = out / f"核销明细_{period}.csv"
    rows = []
    for m in d["matches"]:
        links = conn.execute(
            "SELECT side, record_id FROM match_links WHERE match_id=?", (m["id"],)
        ).fetchall()
        bank_ids = ",".join(str(x["record_id"]) for x in links if x["side"] == "bank")
        fin_ids = ",".join(str(x["record_id"]) for x in links if x["side"] == "voucher")
        rows.append([
            m["id"], METHOD_LABELS.get(m["method"], m["method"]), m["confidence"],
            STATUS_LABELS.get(m["status"], m["status"]), m["recon_code"] or "",
            fmt(m["bank_total"]), fmt(m["fin_total"]), fmt(m["diff"]),
            bank_ids, fin_ids, m["created_by"], m["created_at"],
            m["tolerance_note"] or "", m["explanation"] or "",
        ])
    _write_csv(
        p,
        ["核销ID", "匹配方式", "置信度", "状态", "对账码", "银行合计", "凭证合计", "差额",
         "银行流水ID", "凭证行ID", "操作人", "时间", "容差说明", "说明"],
        rows,
    )
    files.append(str(p))

    p = out / f"差异清单_{period}.csv"
    rows = [
        ["银行", r["id"], r["account_no"], r["txn_date"], fmt(r["amount"]), r["direction"],
         r["counterparty_name"] or "", r["summary"] or "", r["bank_txn_no"],
         r["recon_code"] or "",
         "银行已收企业未收" if r["direction"] == "IN" else "银行已付企业未付"]
        for r in d["unmatched_bank"]
    ] + [
        ["凭证", r["id"], r["account_no"], r["voucher_date"], fmt(r["amount"]), r["direction"],
         r["counterparty"] or "", r["summary"] or "", f"{r['voucher_no']}-{r['line_no']}",
         r["recon_code"] or "",
         "企业已收银行未收" if r["direction"] == "IN" else "企业已付银行未付"]
        for r in d["unmatched_fin"]
    ]
    _write_csv(
        p,
        ["侧", "记录ID", "账号", "日期", "金额", "方向", "对手方", "摘要", "单据号",
         "对账码", "未达账项分类"],
        rows,
    )
    files.append(str(p))

    p = out / f"差异工单_{period}.csv"
    _write_csv(
        p,
        ["工单ID", "期间", "对账码", "差异金额", "原因码", "原因", "责任方", "建议动作",
         "状态", "AI归因", "结转自", "结转至", "解决说明", "创建时间", "更新时间"],
        [
            [t["id"], t["period"], t["recon_code"] or "", fmt(t["diff_amount"]),
             t["reason_code"], t["reason_label"], t["assignee"], t["suggested_action"],
             t["status"], t["ai_note"] or "", t["carried_from"] or "", t["carried_to"] or "",
             t["resolved_note"] or "", t["created_at"], t["updated_at"]]
            for t in d["tickets"]
        ],
    )
    files.append(str(p))

    p = out / f"对账报告_{period}.html"
    p.write_text(_render_html(conn, period, d), encoding="utf-8")
    files.append(str(p))
    return files


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _table(headers: list[str], rows: list[list]) -> str:
    th = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    trs = "".join(
        "<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def _render_html(conn, period: str, d: dict) -> str:
    rate_b = d["matched_bank"] / d["total_bank"] * 100 if d["total_bank"] else 0
    rate_f = d["matched_fin"] / d["total_fin"] * 100 if d["total_fin"] else 0
    stmt_rows = [
        [s.account_no, fmt(s.stmt_close), fmt(s.fin_in_pending), fmt(s.fin_out_pending),
         fmt(s.adjusted_stmt), fmt(s.ledger_close), fmt(s.bank_in_unbooked),
         fmt(s.bank_out_unbooked), fmt(s.adjusted_ledger), fmt(s.tolerance_total),
         fmt(s.residual),
         "—" if s.balanced is None else ("✓ 平衡" if s.balanced else "✗ 未解释差异")]
        for s in d["statements"]
    ]
    match_rows = [
        [m["id"], METHOD_LABELS.get(m["method"], m["method"]), m["confidence"],
         STATUS_LABELS.get(m["status"], m["status"]), m["recon_code"] or "",
         fmt(m["bank_total"]), fmt(m["fin_total"]), fmt(m["diff"]),
         m["created_by"], (m["explanation"] or m["tolerance_note"] or "")[:80]]
        for m in d["matches"]
    ]
    ticket_rows = [
        [t["id"], t["reason_label"], t["assignee"], t["suggested_action"], t["status"],
         fmt(t["diff_amount"]), (t["ai_note"] or "")[:80], t["resolved_note"] or ""]
        for t in d["tickets"]
    ]
    # 看板：责任方 × 状态
    board: dict[tuple[str, str], int] = {}
    for t in d["tickets"]:
        if t["status"] != "已解决":
            board[(t["assignee"], t["status"])] = board.get((t["assignee"], t["status"]), 0) + 1
    board_rows = [[a, s, c] for (a, s), c in sorted(board.items())]
    diff_rows = [
        ["银行", r["account_no"], r["txn_date"], fmt(r["amount"]), r["direction"],
         r["counterparty_name"] or "", r["summary"] or "",
         "银行已收企业未收" if r["direction"] == "IN" else "银行已付企业未付"]
        for r in d["unmatched_bank"]
    ] + [
        ["凭证", r["account_no"], r["voucher_date"], fmt(r["amount"]), r["direction"],
         r["counterparty"] or "", r["summary"] or "",
         "企业已收银行未收" if r["direction"] == "IN" else "企业已付银行未付"]
        for r in d["unmatched_fin"]
    ]
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>资金对账报告 {_esc(period)}</title>
<style>
body{{font-family:"Microsoft YaHei",sans-serif;margin:2em;color:#222}}
h1{{border-bottom:3px solid #2c6e9e;padding-bottom:.3em}}
h2{{color:#2c6e9e;margin-top:1.6em}}
table{{border-collapse:collapse;width:100%;margin:.8em 0;font-size:14px}}
th,td{{border:1px solid #ccc;padding:6px 10px;text-align:left}}
th{{background:#eef4f8}}
tr:nth-child(even){{background:#fafafa}}
.kpi{{display:inline-block;background:#eef4f8;border-radius:8px;padding:12px 20px;margin-right:12px}}
.kpi b{{font-size:22px;display:block}}
</style></head><body>
<h1>资金对账报告 · {_esc(period)}</h1>
<div>
<span class="kpi"><b>{rate_b:.1f}%</b>银行流水核销率（{d["matched_bank"]}/{d["total_bank"]}）</span>
<span class="kpi"><b>{rate_f:.1f}%</b>凭证核销率（{d["matched_fin"]}/{d["total_fin"]}）</span>
<span class="kpi"><b>{len([m for m in d["matches"] if m["status"] == "pending"])}</b>建议核销待确认</span>
<span class="kpi"><b>{len([t for t in d["tickets"] if t["status"] != "已解决"])}</b>未解决工单</span>
</div>
<h2>银行存款余额调节表</h2>
{_table(["账号", "对账单期末", "企业已收银行未收", "企业已付银行未付", "调节后对账单余额",
         "账面期末", "银行已收企业未收", "银行已付企业未付", "调节后账面余额",
         "容差核销差额", "未解释差异", "平衡校验"], stmt_rows)}
<h2>核销明细（含已撤销留痕）</h2>
{_table(["ID", "匹配方式", "置信度", "状态", "对账码", "银行合计", "凭证合计", "差额",
         "操作人", "说明"], match_rows)}
<h2>差异工单</h2>
{_table(["ID", "原因", "责任方", "建议动作", "状态", "差异金额", "AI 归因", "解决说明"], ticket_rows)}
<h2>工单看板（责任方 × 状态，未解决）</h2>
{_table(["责任方", "状态", "数量"], board_rows)}
<h2>未达账项清单</h2>
{_table(["侧", "账号", "日期", "金额", "方向", "对手方", "摘要", "未达分类"], diff_rows)}
</body></html>"""
