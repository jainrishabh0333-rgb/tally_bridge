"""
tally_bridge.portal — the distributor-facing API.

THE rule, stated once and enforced everywhere: the party is resolved from the
SESSION, through the DMS Portal Access grant, exactly once per request. It is
never accepted as a parameter, never read from a header, never inferred from
a payload. Every query below filters on the resolved party; there is no code
path that returns another party's rows.

Distributor logins hold no read permission on any mirror doctype — these
endpoints run raw queries under that identity and expose only what belongs to
the caller. Nothing here is allow_guest; the OTP login lives in
portal_auth.py, which is the only guest-reachable surface.

What a distributor must never see (the party-facing document rule): other
parties' names or balances, agent book totals, firm-wide receivables, exact
company stock. The catalogue buckets stock as in/low/out; the network shows
only ledgers grouped UNDER the caller's own ledger.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import add_days, flt, getdate, now_datetime

from tally_bridge.api import ORDER_TYPES, _POSTED_V, _limit

STATEMENT_CAP = 2000

# Stage names shown to the distributor, derived — never stored — from the
# queue row and the mirrored voucher.
QUEUE_STAGES = {
    "Pending": "Queued",
    "Importing": "In Tally",
    "Imported": "In Tally",     # refined by the mirror's own status below
    "Failed": "Failed",
    "Cancelled": "Cancelled",
}
MIRROR_STAGES = {
    "Open": "In Tally",
    "Partial": "Partial",
    "Delivered": "Delivered",
    "Billed": "Billed",
    "Cancelled": "Cancelled",
    "Pre-closed": "Billed",
}


# ---------------------------------------------------------------------------
# Party resolution
# ---------------------------------------------------------------------------

_PARTY_FIELDS = [
    "name", "ledger_name", "company", "parent_group", "primary_group",
    "agent", "closing_balance", "credit_limit", "credit_days",
    "credit_period", "mobile", "phone", "email", "last_synced",
]


def _party() -> dict:
    """
    The one gate — DELEGATED to snj_dms's grant resolver, per the MD's
    decision (2026-08-15): one security model, owned by snj_dms. The
    resolver enforces session -> enabled DMS Portal Access grant ->
    company-pinned ledger, with self-relinking across FY rollovers;
    tally_bridge only widens the already-resolved row with the extra mirror
    fields the portal needs.

    The dependency is enforced at runtime, not via required_apps — snj_dms
    already requires tally_bridge, and a circular required_apps pair cannot
    install. Absent snj_dms, every portal call refuses loudly.
    """
    user = frappe.session.user
    if not user or user == "Guest":
        # Checked here because the resolver answers a guest with a redirect
        # to /login — right for a www page, wrong for a JSON API.
        frappe.throw("Please log in.", frappe.AuthenticationError)

    try:
        from snj_dms.portal_utils import get_portal_party
    except ImportError:
        frappe.throw("The distributor portal needs the snj_dms app — it owns "
                     "the portal access grants.", frappe.PermissionError)

    led = get_portal_party()
    full = frappe.db.get_value("Tally Ledger", led.name, _PARTY_FIELDS,
                               as_dict=True)
    if not full:
        frappe.throw("Your account is being re-linked after a data refresh — "
                     "please check back shortly, or call the office.")
    return full


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def get_summary():
    """Outstanding, overdue, credit position, ageing buckets, next deliveries."""
    p = _party()

    bills = frappe.db.sql(
        """
        SELECT outstanding, overdue_days, due_date
        FROM `tabTally Bill`
        WHERE company = %(company)s AND party = %(party)s
          AND outstanding > 0 AND is_advance = 0
        """,
        {"company": p.company, "party": p.ledger_name}, as_dict=True)

    outstanding = round(sum(flt(b.outstanding) for b in bills), 2)
    overdue = [b for b in bills if (b.overdue_days or 0) > 0]
    buckets = {"not_due": 0.0, "1_15": 0.0, "16_30": 0.0, "30_plus": 0.0}
    for b in bills:
        d = int(b.overdue_days or 0)
        amt = flt(b.outstanding)
        if d <= 0:
            buckets["not_due"] += amt
        elif d <= 15:
            buckets["1_15"] += amt
        elif d <= 30:
            buckets["16_30"] += amt
        else:
            buckets["30_plus"] += amt
    buckets = {k: round(v, 2) for k, v in buckets.items()}

    limit = flt(p.credit_limit)
    credit = {
        "limit": limit or None,
        "used": outstanding,
        "available": round(limit - outstanding, 2) if limit else None,
        "note": None if limit else "No credit limit is set in Tally for this account.",
        "period": p.credit_period or None,
    }

    upcoming = frappe.db.sql(
        """
        SELECT p.voucher_number, l.item_name, l.size_batch, l.pending_qty,
               l.unit, l.due_date
        FROM `tabTally Sales Order Line` l
        INNER JOIN `tabTally Sales Order` p ON p.name = l.parent
        WHERE p.company = %(company)s AND p.party = %(party)s
          AND p.is_cancelled = 0 AND p.is_optional = 0
          AND l.pending_qty > 0 AND l.due_date IS NOT NULL
        ORDER BY l.due_date ASC
        LIMIT 10
        """,
        {"company": p.company, "party": p.ledger_name}, as_dict=True)
    for u in upcoming:
        u["due_date"] = str(u["due_date"] or "")

    return {
        "party": p.ledger_name,
        "outstanding": outstanding,
        "overdue_total": round(sum(flt(b.outstanding) for b in overdue), 2),
        "overdue_bills": len(overdue),
        "ageing": buckets,
        "credit": credit,
        "next_deliveries": upcoming,
        "as_of": str(p.last_synced or ""),
    }


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def _order_lines(parent_name: str) -> list:
    return frappe.get_all(
        "Tally Sales Order Line",
        filters={"parent": parent_name, "parenttype": "Tally Sales Order"},
        fields=["item_name", "size_batch", "qty", "unit", "delivered_qty",
                "pending_qty", "rate", "discount", "discount2", "amount",
                "due_date"],
        order_by="idx asc", limit_page_length=0)


def _queue_rows(p, order_key=None) -> list:
    filters = {"company": p.company, "party_ledger": p.ledger_name}
    if order_key:
        filters["order_key"] = order_key
    return frappe.get_all(
        "Tally Order Queue", filters=filters,
        fields=["order_key", "order_no", "order_date", "status", "queued_at",
                "tally_vch_number", "error", "source"],
        order_by="queued_at desc", limit_page_length=0)


@frappe.whitelist(methods=["GET"])
def get_orders(status=None):
    """
    The caller's orders: queued ones merged with mirrored Tally ones.

    A queue row that has become a Tally voucher appears ONCE, as the voucher,
    with its queue history attached. Stage flows Queued -> In Tally ->
    Partial -> Delivered -> Billed, with Failed/Cancelled as terminal side
    exits.
    """
    p = _party()

    mirrored = frappe.get_all(
        "Tally Sales Order",
        filters={"company": p.company, "party": p.ledger_name,
                 "is_optional": 0},
        fields=["name", "voucher_number", "voucher_date", "amount",
                "delivered_value", "order_status", "order_key", "reference"],
        order_by="voucher_date desc, creation desc",
        limit_page_length=200)
    mirrored_keys = {m.order_key for m in mirrored if m.order_key}

    out = []
    for m in mirrored:
        stage = MIRROR_STAGES.get(m.order_status, "In Tally")
        out.append({
            "stage": stage,
            "order_no": m.voucher_number,
            "order_date": str(m.voucher_date or ""),
            "amount": flt(m.amount),
            "delivered_value": flt(m.delivered_value),
            "order_key": m.order_key or None,
            "lines": _order_lines(m.name),
        })

    for q in _queue_rows(p):
        if q.order_key in mirrored_keys:
            continue                      # already shown as its voucher
        stage = QUEUE_STAGES.get(q.status, q.status)
        out.append({
            "stage": stage,
            "order_no": q.order_no or q.order_key,
            "order_date": str(q.order_date or ""),
            "amount": None,               # priced at import, not before
            "order_key": q.order_key,
            "queued_at": str(q.queued_at or ""),
            "error": q.error or None,
            "lines": frappe.get_all(
                "Tally Order Queue Line",
                filters={"parent": q.order_key,
                         "parenttype": "Tally Order Queue"},
                fields=["item_name", "size_batch", "qty", "unit", "rate",
                        "discount"],
                order_by="idx asc", limit_page_length=0),
        })

    if status:
        out = [o for o in out if o["stage"] == status]
    out.sort(key=lambda o: o.get("order_date") or "", reverse=True)
    return {"count": len(out), "orders": out}


@frappe.whitelist(methods=["GET"])
def get_order(order_key=None):
    """One order end to end: queue history, voucher, deliveries, invoices."""
    p = _party()
    if not order_key:
        frappe.throw("`order_key` is required")

    timeline = []
    queue = _queue_rows(p, order_key=order_key)
    order_no = None
    if queue:
        q = queue[0]
        order_no = q.order_no or None
        timeline.append({"event": "Queued", "at": str(q.queued_at or ""),
                         "detail": f"from {q.source}" if q.source else ""})
        if q.status == "Failed":
            timeline.append({"event": "Import failed", "at": "",
                             "detail": q.error or ""})
        if q.tally_vch_number:
            timeline.append({"event": "Imported into Tally", "at": "",
                             "detail": f"voucher {q.tally_vch_number}"})

    # The mirrored voucher: by order_key when the order came from the queue,
    # else by voucher number — BOTH always constrained to the caller's party.
    so_filters = {"company": p.company, "party": p.ledger_name}
    so = None
    for probe in ({"order_key": order_key},
                  {"voucher_number": order_key},
                  {"voucher_number": order_no} if order_no else None):
        if not probe:
            continue
        so = frappe.db.get_value(
            "Tally Sales Order", {**so_filters, **probe},
            ["name", "voucher_number", "voucher_date", "amount",
             "delivered_value", "order_status"], as_dict=True)
        if so:
            break

    if not queue and not so:
        frappe.throw("No such order on this account.", frappe.DoesNotExistError)

    result = {"order_key": order_key, "timeline": timeline}
    if so:
        result.update({
            "order_no": so.voucher_number,
            "order_date": str(so.voucher_date or ""),
            "amount": flt(so.amount),
            "delivered_value": flt(so.delivered_value),
            "stage": MIRROR_STAGES.get(so.order_status, "In Tally"),
            "lines": _order_lines(so.name),
        })
        timeline.append({"event": "Order in Tally",
                         "at": str(so.voucher_date or ""),
                         "detail": so.voucher_number})
        for table, parent, label, num_field in (
                ("Tally Delivery Note Line", "Tally Delivery Note",
                 "Delivered", "voucher_number"),
                ("Tally Invoice Line", "Tally Invoice", "Invoiced",
                 "invoice_no")):
            docs = frappe.db.sql(
                f"""
                SELECT DISTINCT p.{num_field} AS number, p.voucher_date
                FROM `tab{table}` l
                INNER JOIN `tab{parent}` p ON p.name = l.parent
                WHERE p.company = %(company)s AND p.party = %(party)s
                  AND l.order_no = %(order_no)s AND p.is_cancelled = 0
                ORDER BY p.voucher_date ASC
                """,
                {"company": p.company, "party": p.ledger_name,
                 "order_no": so.voucher_number}, as_dict=True)
            for d in docs:
                timeline.append({"event": label,
                                 "at": str(d.voucher_date or ""),
                                 "detail": d.number})
    else:
        q = queue[0]
        result.update({
            "order_no": q.order_no or order_key,
            "stage": QUEUE_STAGES.get(q.status, q.status),
        })
    return result


# ---------------------------------------------------------------------------
# Money: statement, bills, invoices, payments
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def get_statement(from_date=None, to_date=None):
    """
    Ledger statement with a running balance that reconciles.

    Runs off the mirrored voucher entries for the caller's own ledger.
    Order vouchers are excluded — they post nothing, and a statement that
    cannot reconcile to its closing balance is worse than none. The running
    balance is anchored on the ledger's opening balance plus everything
    before `from_date`.
    """
    p = _party()
    frm = getdate(from_date) if from_date else None
    to = getdate(to_date) if to_date else None

    opening = flt(frappe.db.get_value("Tally Ledger", p.name, "opening_balance"))
    anchor = opening
    if frm:
        moved = frappe.db.sql(
            f"""
            SELECT COALESCE(SUM(e.amount), 0)
            FROM `tabTally Voucher Entry` e
            INNER JOIN `tabTally Voucher` v ON v.name = e.parent
            WHERE e.ledger = %(party)s AND v.company = %(company)s
              AND {_POSTED_V} AND v.voucher_type NOT IN %(order_types)s
              AND v.voucher_date < %(frm)s
            """,
            {"party": p.ledger_name, "company": p.company,
             "order_types": ORDER_TYPES, "frm": frm})[0][0]
        anchor = opening + flt(moved)

    conds = ["e.ledger = %(party)s", "v.company = %(company)s",
             _POSTED_V, "v.voucher_type NOT IN %(order_types)s"]
    params = {"party": p.ledger_name, "company": p.company,
              "order_types": ORDER_TYPES, "cap": STATEMENT_CAP}
    if frm:
        conds.append("v.voucher_date >= %(frm)s")
        params["frm"] = frm
    if to:
        conds.append("v.voucher_date <= %(to)s")
        params["to"] = to

    rows = frappe.db.sql(
        f"""
        SELECT v.voucher_date, v.voucher_type, v.voucher_number,
               v.narration, e.amount
        FROM `tabTally Voucher Entry` e
        INNER JOIN `tabTally Voucher` v ON v.name = e.parent
        WHERE {' AND '.join(conds)}
        ORDER BY v.voucher_date ASC, v.voucher_number ASC
        LIMIT %(cap)s
        """,
        params, as_dict=True)

    running = anchor
    txns = []
    for r in rows:
        amt = flt(r.amount)
        running = round(running + amt, 2)
        txns.append({
            "date": str(r.voucher_date),
            "particulars": r.voucher_type,
            "vch": r.voucher_number,
            "narration": r.narration or "",
            "debit": amt if amt > 0 else 0.0,
            "credit": abs(amt) if amt < 0 else 0.0,
            "balance": running,
        })

    out = {
        "party": p.ledger_name,
        "period": {"from": str(from_date or ""), "to": str(to_date or "")},
        "opening": round(anchor, 2),
        "closing": round(running, 2),
        "count": len(txns),
        "rows": txns,
    }
    if len(txns) == STATEMENT_CAP:
        out["note"] = (f"Statement capped at {STATEMENT_CAP} rows — narrow "
                       f"the date range to see the rest.")
    return out


@frappe.whitelist(methods=["GET"])
def get_bills():
    """Open bill-wise outstandings, oldest due first."""
    p = _party()
    rows = frappe.db.sql(
        """
        SELECT bill_ref, bill_date, due_date, overdue_days, credit_period,
               opening_amount, outstanding
        FROM `tabTally Bill`
        WHERE company = %(company)s AND party = %(party)s
          AND outstanding > 0 AND is_advance = 0
        ORDER BY overdue_days DESC, due_date ASC
        LIMIT 500
        """,
        {"company": p.company, "party": p.ledger_name}, as_dict=True)
    for r in rows:
        r["bill_date"] = str(r["bill_date"] or "")
        r["due_date"] = str(r["due_date"] or "")
    return {
        "party": p.ledger_name,
        "count": len(rows),
        "total": round(sum(flt(r["outstanding"]) for r in rows), 2),
        "rows": rows,
    }


@frappe.whitelist(methods=["GET"])
def get_invoices(from_date=None, to_date=None, limit=100):
    """Invoice history, newest first."""
    p = _party()
    conds = ["company = %(company)s", "party = %(party)s", "is_cancelled = 0",
             "is_optional = 0"]
    params = {"company": p.company, "party": p.ledger_name,
              "limit": _limit(limit, 100)}
    if from_date:
        conds.append("voucher_date >= %(frm)s")
        params["frm"] = getdate(from_date)
    if to_date:
        conds.append("voucher_date <= %(to)s")
        params["to"] = getdate(to_date)
    rows = frappe.db.sql(
        f"""
        SELECT name, invoice_no, voucher_date, amount, taxable_value,
               cgst, sgst, igst, cess, round_off, reference, bill_refs
        FROM `tabTally Invoice`
        WHERE {' AND '.join(conds)}
        ORDER BY voucher_date DESC, creation DESC
        LIMIT %(limit)s
        """,
        params, as_dict=True)
    for r in rows:
        r["voucher_date"] = str(r["voucher_date"] or "")
        from urllib.parse import quote
        r["pdf"] = ("/api/method/tally_bridge.portal.download_invoice_pdf"
                    f"?invoice={quote(r['name'])}")
        del r["name"]
    return {"count": len(rows), "rows": rows}


@frappe.whitelist(methods=["GET"])
def download_invoice_pdf(invoice=None):
    """One invoice as PDF — only ever the caller's own."""
    p = _party()
    if not invoice:
        frappe.throw("`invoice` is required")
    doc = frappe.db.get_value(
        "Tally Invoice", invoice, ["name", "party", "company", "invoice_no"],
        as_dict=True)
    # Ownership FIRST, existence second: a wrong guess and a foreign invoice
    # answer identically, so a probing caller learns nothing either way.
    if not doc or doc.party != p.ledger_name or doc.company != p.company:
        frappe.throw("No such invoice on this account.", frappe.DoesNotExistError)

    html = frappe.get_print("Tally Invoice", doc.name, print_format=None,
                            no_letterhead=0)
    from frappe.utils.pdf import get_pdf
    frappe.local.response.filename = f"{(doc.invoice_no or doc.name).replace('/', '-')}.pdf"
    frappe.local.response.filecontent = get_pdf(html)
    frappe.local.response.type = "download"


@frappe.whitelist(methods=["GET"])
def get_payments(limit=100):
    """Receipts recorded against the account, plus pending intimations."""
    p = _party()
    receipts = frappe.get_all(
        "Tally Receipt",
        filters={"company": p.company, "party": p.ledger_name,
                 "is_cancelled": 0},
        fields=["name", "voucher_date", "amount", "mode", "instrument_no",
                "instrument_date"],
        order_by="voucher_date desc", limit_page_length=_limit(limit, 100))
    for r in receipts:
        r["voucher_date"] = str(r["voucher_date"] or "")
        r["instrument_date"] = str(r["instrument_date"] or "")
        r["bills_settled"] = frappe.get_all(
            "Tally Receipt Allocation",
            filters={"parent": r.name, "parenttype": "Tally Receipt"},
            fields=["bill_ref", "bill_type", "amount"],
            order_by="idx asc", limit_page_length=0)
        del r["name"]

    intimations = frappe.get_all(
        "Payment Intimation",
        filters={"party_ledger": p.name},
        fields=["name", "amount", "mode", "utr_ref", "paid_on", "status",
                "submitted_at", "matched_receipt"],
        order_by="creation desc", limit_page_length=50)
    for i in intimations:
        i["paid_on"] = str(i["paid_on"] or "")
        i["submitted_at"] = str(i["submitted_at"] or "")

    return {"receipts": receipts, "intimations": intimations}


# ---------------------------------------------------------------------------
# Catalogue and network
# ---------------------------------------------------------------------------

# Stock is bucketed, never disclosed: "in" / "low" / "out". The low threshold
# is in the party's ordering unit where known.
LOW_STOCK_THRESHOLD = 20.0


def _bucket_qty(qty) -> str:
    q = flt(qty)
    if q <= 0:
        return "out"
    if q < LOW_STOCK_THRESHOLD:
        return "low"
    return "in"


@frappe.whitelist(methods=["GET"])
def get_catalogue(query=None, group=None, limit=200):
    """
    Items with their sizes, availability buckets and the caller's rate.

    Rates come from Tally Item Rate — harvested from real voucher lines,
    party-specific where the party is actually charged differently. Exact
    stock never leaves this function: sizes carry in/low/out only.
    """
    p = _party()
    conds = ["company = %(company)s"]
    params = {"company": p.company, "limit": _limit(limit, 200)}
    if query:
        conds.append("(item_name LIKE %(q)s OR part_no LIKE %(q)s)")
        params["q"] = f"%{query}%"
    if group:
        conds.append("stock_group = %(group)s")
        params["group"] = group

    items = frappe.db.sql(
        f"""
        SELECT item_name, stock_group, base_units, closing_qty
        FROM `tabTally Stock Item`
        WHERE {' AND '.join(conds)}
        ORDER BY ABS(closing_qty) DESC, item_name
        LIMIT %(limit)s
        """,
        params, as_dict=True)
    if not items:
        return {"count": 0, "items": []}

    names = tuple(i.item_name for i in items)
    sizes = frappe.db.sql(
        """
        SELECT item_name, batch_name, closing_qty, as_of
        FROM `tabTally Stock Batch`
        WHERE company = %(company)s AND item_name IN %(names)s
        ORDER BY item_name, batch_name
        """,
        {"company": p.company, "names": names}, as_dict=True)
    sizes_by_item: dict = {}
    for s in sizes:
        sizes_by_item.setdefault(s.item_name, []).append({
            "size": s.batch_name,
            "availability": _bucket_qty(s.closing_qty),
            "as_of": str(s.as_of or ""),
        })

    rates = frappe.db.sql(
        """
        SELECT item_name, party, rate, unit, discount, discount2, net_rate
        FROM `tabTally Item Rate`
        WHERE company = %(company)s AND item_name IN %(names)s
          AND party IN ('', %(party)s)
        """,
        {"company": p.company, "names": names, "party": p.ledger_name},
        as_dict=True)
    rate_by_item: dict = {}
    for r in rates:
        cur = rate_by_item.get(r.item_name)
        if cur is None or r.party:        # the party-specific row wins
            rate_by_item[r.item_name] = r

    out = []
    for i in items:
        r = rate_by_item.get(i.item_name)
        out.append({
            "item_name": i.item_name,
            "group": i.stock_group,
            "unit": i.base_units,
            "availability": _bucket_qty(i.closing_qty),
            "sizes": sizes_by_item.get(i.item_name, []),
            "rate": ({"rate": flt(r.rate), "unit": r.unit,
                      "discount": flt(r.discount), "discount2": flt(r.discount2),
                      "net_rate": flt(r.net_rate)} if r else None),
        })
    return {"count": len(out), "items": out}


@frappe.whitelist(methods=["GET"])
def get_network():
    """
    The caller's own slice of the network: their agent, and any ledgers
    grouped UNDER their own ledger (semi-distributors / direct parties).

    Group-path matching, because the network is modelled as Tally groups. The
    caller's own outstandings aside, no figure from any other branch of the
    tree is reachable from here.
    """
    p = _party()
    sub = frappe.db.sql(
        """
        SELECT ledger_name, parent_group, closing_balance, mobile, phone
        FROM `tabTally Ledger`
        WHERE company = %(company)s
          AND (parent_group = %(me)s
               OR group_path LIKE %(path_mid)s
               OR group_path LIKE %(path_end)s)
          AND ledger_name != %(me)s
        ORDER BY ABS(closing_balance) DESC
        LIMIT 200
        """,
        {"company": p.company, "me": p.ledger_name,
         "path_mid": f"%> {p.ledger_name} >%",
         "path_end": f"%> {p.ledger_name}"},
        as_dict=True)
    parties = [{
        "name": s.ledger_name,
        "group": s.parent_group,
        "outstanding": abs(flt(s.closing_balance)),
        "direction": "owes" if flt(s.closing_balance) > 0 else
                     ("advance" if flt(s.closing_balance) < 0 else "settled"),
        "mobile": s.mobile or s.phone or "",
    } for s in sub]

    return {
        "party": p.ledger_name,
        "agent": p.agent or p.parent_group or None,
        "sub_parties": parties,
        "note": (None if parties else
                 "No ledgers are grouped under this account in Tally."),
    }


# ---------------------------------------------------------------------------
# Writes: order, payment intimation, suggestion
# ---------------------------------------------------------------------------

def _parse_dict(payload, key="payload") -> dict:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        frappe.throw(f"`{key}` must be a JSON object")
    return payload


@frappe.whitelist(methods=["POST"])
def place_order(payload=None):
    """
    Queue a Sales Order for import into Tally.

    The party comes from the SESSION — a party field in the payload is
    ignored outright. Items and sizes are validated against the mirror
    exactly as queue_sales_order does it (Tally auto-creates unknown masters
    on import; exact-match validation here is what prevents that). Rates come
    from Tally Item Rate, never from the caller: a distributor does not price
    their own order. Replaying the same order_key returns the existing row.
    """
    p = _party()
    o = _parse_dict(payload, "payload")

    order_key = (o.get("order_key") or "").strip()
    lines = o.get("lines") or []
    if not order_key:
        frappe.throw("`order_key` is required — it is the idempotency key.")
    if not lines:
        frappe.throw("The order has no lines.")

    existing = frappe.db.get_value("Tally Order Queue", order_key,
                                   ["status", "party_ledger"], as_dict=True)
    if existing:
        # Idempotent replay — but only of one's own order. A key that
        # belongs to another party is treated as taken.
        if existing.party_ledger != p.ledger_name:
            frappe.throw("This order key is already in use.")
        return {"queued": False, "order_key": order_key,
                "status": existing.status}

    doc_lines = []
    for i, line in enumerate(lines, start=1):
        item = (line.get("item_name") or "").strip()
        size = str(line.get("size_batch") or "").strip()
        qty = flt(line.get("qty"))
        if not item:
            frappe.throw(f"Line {i}: `item_name` is required.")
        if qty <= 0:
            frappe.throw(f"Line {i} ({item}): qty must be greater than zero.")
        if not frappe.db.exists("Tally Stock Item",
                                {"company": p.company, "item_name": item}):
            frappe.throw(f"Line {i}: '{item}' is not in the catalogue.")
        if size and frappe.db.exists("Tally Stock Batch",
                                     {"company": p.company, "item_name": item}) \
                and not frappe.db.exists(
                    "Tally Stock Batch",
                    {"company": p.company, "item_name": item,
                     "batch_name": size}):
            frappe.throw(f"Line {i}: '{item}' does not come in size '{size}'.")

        rate_row = (frappe.db.get_value(
            "Tally Item Rate",
            {"company": p.company, "item_name": item, "party": p.ledger_name},
            ["rate", "unit", "discount"], as_dict=True)
            or frappe.db.get_value(
            "Tally Item Rate",
            {"company": p.company, "item_name": item, "party": ""},
            ["rate", "unit", "discount"], as_dict=True))
        if not rate_row or flt(rate_row.rate) <= 0:
            frappe.throw(
                f"Line {i}: '{item}' has no rate on record yet — please ask "
                f"the office to price it, then place the order again.")
        doc_lines.append({
            "item_name": item,
            "size_batch": size,
            "qty": qty,
            "unit": (line.get("unit") or rate_row.unit or "Doz").strip(),
            "rate": flt(rate_row.rate),
            "discount": flt(rate_row.discount) if rate_row.discount is not None else 50,
            "due_days": int(line.get("due_days") or 0),
        })

    doc = frappe.get_doc({
        "doctype": "Tally Order Queue",
        "order_key": order_key,
        "company": p.company,
        "party_ledger": p.ledger_name,
        "order_no": (o.get("order_no") or "").strip(),
        "order_date": getdate(o.get("order_date")) if o.get("order_date") else getdate(),
        "status": "Pending",
        "source": "distributor-portal",
        "queued_at": now_datetime(),
    })
    for line in doc_lines:
        doc.append("lines", line)
    doc.name = order_key
    try:
        doc.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
        return {"queued": False, "order_key": order_key,
                "status": frappe.db.get_value("Tally Order Queue", order_key,
                                              "status")}
    frappe.db.commit()
    return {"queued": True, "order_key": order_key, "lines": len(doc.lines)}


@frappe.whitelist(methods=["POST"])
def submit_payment_intimation(payload=None):
    """
    'I have paid you' — recorded, then auto-confirmed against a mirrored
    receipt on a later sync. Never an accounting entry.
    """
    p = _party()
    o = _parse_dict(payload, "payload")

    amount = flt(o.get("amount"))
    if amount <= 0:
        frappe.throw("`amount` must be greater than zero.")
    mode = (o.get("mode") or "").strip()
    if mode not in ("UPI", "NEFT", "RTGS", "IMPS", "Cheque", "Cash", "Other"):
        frappe.throw("`mode` must be one of UPI, NEFT, RTGS, IMPS, Cheque, "
                     "Cash, Other.")

    key = (o.get("idempotency_key") or "").strip()
    if key:
        existing = frappe.db.get_value(
            "Payment Intimation", {"idempotency_key": key},
            ["name", "party_ledger", "status"], as_dict=True)
        if existing:
            if existing.party_ledger != p.name:
                frappe.throw("This idempotency key is already in use.")
            return {"created": False, "name": existing.name,
                    "status": existing.status}

    bills = []
    for ref in o.get("bills") or []:
        ref = (ref if isinstance(ref, str) else ref.get("bill_ref") or "").strip()
        if not ref:
            continue
        bill = frappe.db.get_value(
            "Tally Bill", {"company": p.company, "party": p.ledger_name,
                           "bill_ref": ref},
            ["bill_ref", "outstanding"], as_dict=True)
        if not bill:
            frappe.throw(f"Bill '{ref}' is not open on this account.")
        bills.append(bill)

    doc = frappe.get_doc({
        "doctype": "Payment Intimation",
        "party_ledger": p.name,
        "party_name": p.ledger_name,
        "company": p.company,
        "amount": amount,
        "mode": mode,
        "utr_ref": (o.get("utr_ref") or "").strip(),
        "paid_on": getdate(o.get("paid_on")) if o.get("paid_on") else getdate(),
        "note": (o.get("note") or "").strip()[:500],
        "status": "Pending Confirmation",
        "submitted_by": frappe.session.user,
        "submitted_at": now_datetime(),
        "idempotency_key": key or None,
    })
    for b in bills:
        doc.append("bills", {"bill_ref": b.bill_ref,
                             "amount": flt(b.outstanding)})
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"created": True, "name": doc.name, "status": doc.status}


@frappe.whitelist(methods=["POST"])
def submit_suggestion(text=None):
    """Free-text feedback; the office is notified."""
    p = _party()
    text = (text or "").strip()
    if not text:
        frappe.throw("Nothing to submit.")
    if len(text) > 2000:
        frappe.throw("Please keep suggestions under 2000 characters.")

    doc = frappe.get_doc({
        "doctype": "Distributor Suggestion",
        "party_ledger": p.name,
        "party_name": p.ledger_name,
        "suggestion": text,
        "status": "New",
        "submitted_by": frappe.session.user,
        "submitted_at": now_datetime(),
    })
    doc.insert(ignore_permissions=True)

    recipients = frappe.get_all(
        "Has Role",
        filters={"role": ["in", ["DMS Manager", "System Manager"]],
                 "parenttype": "User"},
        pluck="parent", distinct=True)
    for user in recipients:
        if user in ("Administrator", "Guest"):
            continue
        try:
            frappe.get_doc({
                "doctype": "Notification Log",
                "for_user": user,
                "type": "Alert",
                "document_type": "Distributor Suggestion",
                "document_name": doc.name,
                "subject": f"Suggestion from {p.ledger_name}",
                "email_content": text[:500],
            }).insert(ignore_permissions=True)
        except Exception:
            pass          # a notification failure must not lose the suggestion
    frappe.db.commit()
    return {"created": True, "name": doc.name}


# ---------------------------------------------------------------------------
# Scheduled: confirm intimations against mirrored receipts
# ---------------------------------------------------------------------------

def match_payment_intimations():
    """
    Hourly: flip Pending intimations to Confirmed when a mirrored receipt
    matches. Match = same party, amount within one rupee, receipt dated on or
    after the claimed payment date (minus a day for clock slack); a UTR match
    on instrument_no wins outright. One receipt confirms at most one
    intimation — ever.
    """
    pending = frappe.get_all(
        "Payment Intimation",
        filters={"status": "Pending Confirmation"},
        fields=["name", "party_name", "company", "amount", "utr_ref",
                "paid_on"],
        order_by="creation asc", limit_page_length=200)
    if not pending:
        return

    used = set(frappe.get_all(
        "Payment Intimation",
        filters={"matched_receipt": ["!=", ""]},
        pluck="matched_receipt"))

    confirmed = 0
    for pi in pending:
        conds = ["company = %(company)s", "party = %(party)s",
                 "is_cancelled = 0",
                 "ABS(amount - %(amount)s) <= 1"]
        params = {"company": pi.company, "party": pi.party_name,
                  "amount": flt(pi.amount)}
        if pi.paid_on:
            conds.append("voucher_date >= %(paid_on)s")
            params["paid_on"] = add_days(pi.paid_on, -1)
        candidates = frappe.db.sql(
            f"""
            SELECT name, instrument_no, voucher_date
            FROM `tabTally Receipt`
            WHERE {' AND '.join(conds)}
            ORDER BY voucher_date ASC
            LIMIT 20
            """,
            params, as_dict=True)
        candidates = [c for c in candidates if c.name not in used]
        if not candidates:
            continue

        match = None
        note = ""
        if pi.utr_ref:
            for c in candidates:
                if c.instrument_no and pi.utr_ref.strip().lower() in \
                        c.instrument_no.strip().lower():
                    match, note = c, "matched by UTR and amount"
                    break
        if match is None:
            match, note = candidates[0], "matched by party, amount and date"

        frappe.db.set_value("Payment Intimation", pi.name, {
            "status": "Confirmed",
            "matched_receipt": match.name,
            "matched_at": now_datetime(),
            "match_note": f"{note} (receipt dated {match.voucher_date})",
        })
        used.add(match.name)
        confirmed += 1

    if confirmed:
        frappe.db.commit()
