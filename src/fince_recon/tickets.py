"""差异工单：开单带全上下文、原因码自动派单、状态机流转、跨月结转、自动闭环。"""

from __future__ import annotations

import sqlite3

from fince_recon.ai import AIProvider, bank_desc, voucher_desc
from fince_recon.config import Config
from fince_recon.db import audit, now
from fince_recon.money import fmt

OPEN_STATUSES = ("待派单", "待处理", "处理中", "待复核", "已结转")

# 状态机：from → 允许的 to
TRANSITIONS: dict[str, set[str]] = {
    "待派单": {"待处理"},
    "待处理": {"处理中", "已结转"},
    "处理中": {"待复核", "已结转"},
    "待复核": {"已解决", "处理中"},  # 待复核→处理中 即退回，必填原因
    "已结转": {"已解决"},
    "已解决": set(),
}

# 可跨月结转的原因码（未达账项类）
CARRYOVER_REASONS = ("FIN_ONLY", "BANK_ONLY", "FEE_INTEREST")


def _event(conn, ticket_id: int, frm: str | None, to: str, actor: str, note: str = ""):
    conn.execute(
        "INSERT INTO ticket_events(ticket_id, from_status, to_status, actor, note, at)"
        " VALUES(?,?,?,?,?,?)",
        (ticket_id, frm, to, actor, note, now()),
    )


def create_ticket(
    conn: sqlite3.Connection,
    cfg: Config,
    provider: AIProvider,
    period: str,
    batch_id: int | None,
    reason_code: str,
    recon_code: str | None,
    diff_amount: int,
    bank_rows: list,
    fin_rows: list,
    actor: str = "system",
) -> int:
    rule = cfg.routing_for(reason_code)
    ctx_parts = [f"差异金额 {fmt(diff_amount)} 元"]
    if recon_code:
        ctx_parts.append(f"对账码 {recon_code}")
    for r in bank_rows[:5]:
        ctx_parts.append("银行:" + bank_desc(r))
    for r in fin_rows[:5]:
        ctx_parts.append("凭证:" + voucher_desc(r))
    ai_note = provider.attribute_diff("；".join(ctx_parts), rule.label)
    cur = conn.execute(
        "INSERT INTO tickets(period, batch_id, recon_code, diff_amount, reason_code,"
        " reason_label, assignee, suggested_action, ai_note, status, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            period, batch_id, recon_code, diff_amount, reason_code,
            rule.label, rule.assignee, rule.action, ai_note,
            "待处理", now(), now(),
        ),
    )
    tid = cur.lastrowid
    for r in bank_rows:
        conn.execute(
            "INSERT INTO ticket_links(ticket_id, side, record_id) VALUES(?,?,?)",
            (tid, "bank", r["id"]),
        )
    for r in fin_rows:
        conn.execute(
            "INSERT INTO ticket_links(ticket_id, side, record_id) VALUES(?,?,?)",
            (tid, "voucher", r["id"]),
        )
    _event(conn, tid, None, "待派单", actor, "差异自动开单")
    _event(conn, tid, "待派单", "待处理", actor, f"按原因码 {reason_code} 自动派单 → {rule.assignee}")
    audit(conn, actor, "ticket_create", "ticket", tid,
          {"reason": reason_code, "assignee": rule.assignee, "diff": diff_amount})
    return tid


def move_ticket(
    conn: sqlite3.Connection, ticket_id: int, to_status: str, actor: str, note: str = ""
) -> None:
    t = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if not t:
        raise SystemExit(f"工单 {ticket_id} 不存在")
    frm = t["status"]
    if to_status not in TRANSITIONS.get(frm, set()):
        allowed = "、".join(sorted(TRANSITIONS.get(frm, set()))) or "（终态）"
        raise SystemExit(f"工单 {ticket_id} 不允许 {frm} → {to_status}，可选: {allowed}")
    if frm == "待复核" and to_status == "处理中" and not note:
        raise SystemExit("退回（待复核→处理中）必须填写原因 --note")
    conn.execute(
        "UPDATE tickets SET status=?, updated_at=? WHERE id=?",
        (to_status, now(), ticket_id),
    )
    _event(conn, ticket_id, frm, to_status, actor, note)
    audit(conn, actor, "ticket_move", "ticket", ticket_id, {"from": frm, "to": to_status})
    conn.commit()


def carryover(
    conn: sqlite3.Connection, from_period: str, to_period: str, actor: str
) -> list[int]:
    """将本期未解决的未达账项工单结转到下期，关联记录带入下期对账范围。"""
    carried: list[int] = []
    rows = conn.execute(
        "SELECT * FROM tickets WHERE status IN ('待处理','处理中','待复核')"
        " AND reason_code IN (%s)"
        " AND (period=? OR carried_to=?)" % ",".join("?" * len(CARRYOVER_REASONS)),
        (*CARRYOVER_REASONS, from_period, from_period),
    ).fetchall()
    for t in rows:
        frm = t["status"]
        conn.execute(
            "UPDATE tickets SET status='已结转', carried_from=COALESCE(carried_from, period),"
            " carried_to=?, updated_at=? WHERE id=?",
            (to_period, now(), t["id"]),
        )
        for link in conn.execute(
            "SELECT side, record_id FROM ticket_links WHERE ticket_id=?", (t["id"],)
        ).fetchall():
            table = "bank_txns" if link["side"] == "bank" else "vouchers"
            conn.execute(
                f"UPDATE {table} SET carried_to=? WHERE id=? AND status='unmatched'",
                (to_period, link["record_id"]),
            )
        _event(conn, t["id"], frm, "已结转", actor, f"结转至 {to_period}，下期优先复对")
        audit(conn, actor, "ticket_carryover", "ticket", t["id"], {"to": to_period})
        carried.append(t["id"])
    conn.commit()
    return carried


def auto_close_resolved(
    conn: sqlite3.Connection, resolved_in: str, actor: str = "system"
) -> list[int]:
    """关联记录已全部核销的开放工单自动闭环，回溯标注「X月差异，Y月解决」。"""
    closed: list[int] = []
    for t in conn.execute(
        "SELECT * FROM tickets WHERE status IN (%s)"
        % ",".join("?" * len(OPEN_STATUSES)),
        OPEN_STATUSES,
    ).fetchall():
        links = conn.execute(
            "SELECT side, record_id FROM ticket_links WHERE ticket_id=?", (t["id"],)
        ).fetchall()
        if not links:
            continue
        all_matched = True
        for link in links:
            table = "bank_txns" if link["side"] == "bank" else "vouchers"
            row = conn.execute(
                f"SELECT status FROM {table} WHERE id=?", (link["record_id"],)
            ).fetchone()
            if not row or row["status"] != "matched":
                all_matched = False
                break
        if not all_matched:
            continue
        note = f"{t['period']} 差异，{resolved_in} 解决（关联记录已全部核销，自动闭环）"
        conn.execute(
            "UPDATE tickets SET status='已解决', resolved_note=?, updated_at=? WHERE id=?",
            (note, now(), t["id"]),
        )
        _event(conn, t["id"], t["status"], "已解决", actor, note)
        audit(conn, actor, "ticket_autoclose", "ticket", t["id"], note)
        closed.append(t["id"])
    return closed


def open_ticket_record_ids(conn: sqlite3.Connection) -> set[tuple[str, int]]:
    """已挂在未解决工单上的记录集合，避免重复开单。"""
    return {
        (r["side"], r["record_id"])
        for r in conn.execute(
            "SELECT l.side, l.record_id FROM ticket_links l"
            " JOIN tickets t ON t.id = l.ticket_id WHERE t.status != '已解决'"
        )
    }
