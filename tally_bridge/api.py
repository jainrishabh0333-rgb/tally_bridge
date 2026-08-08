"""
tally_bridge.api
-----------------
Whitelisted endpoints for the Tally Bridge.

Two groups:

1. Ingestion  — called by the LAN-side sync agent. Requires System Manager.
2. Analytics  — called by the MCP server on Claude's behalf. Read-only.

Sign convention
---------------
Balances and entry amounts are stored exactly as TallyPrime exports them:
positive = Debit, negative = Credit. Reporting helpers below convert to the
"outstanding" framing (always positive) and label the direction explicitly, so
a caller never has to guess. Verify against Tally once after first sync using
`sync_health()` — it returns control totals you can eyeball.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import flt, getdate, now_datetime, add_days

# Groups treated as receivable / payable. Tally's default group names; extend
# here if your chart of accounts uses custom groups.
RECEIVABLE_GROUPS = ("Sundry Debtors",)
PAYABLE_GROUPS = ("Sundry Creditors",)

MAX_ROWS = 2000  # hard cap so a bad query can never dump the whole ledger set


# ===========================================================================
# Ingestion (sync agent -> Frappe)
# ===========================================================================

def _require_writer():
    """Ingestion is privileged: only System Manager may write mirrored data."""
    if "System Manager" not in frappe.get_roles():
        frappe.throw("Not permitted: sync user needs the System Manager role.", frappe.PermissionError)


def _parse_payload(value, key: str) -> list[dict]:
    """Accept either a JSON string (form-encoded) or a real list (JSON body)."""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        frappe.throw(f"`{key}` must be a list")
    return value


@frappe.whitelist(methods=["POST"])
def upsert_ledgers(ledgers=None):
    """Insert or update ledger masters. Idempotent — safe to re-run."""
    _require_writer()
    rows = _parse_payload(ledgers, "ledgers")
    stamp = now_datetime()
    created = updated = skipped = 0

    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            continue

        values = {
            "parent_group": row.get("parent") or row.get("parent_group") or "",
            "opening_balance": flt(row.get("opening_balance")),
            "closing_balance": flt(row.get("closing_balance")),
            "gstin": row.get("gstin") or "",
            "email": row.get("email") or "",
            "phone": row.get("phone") or "",
            "bill_by_bill": 1 if row.get("bill_by_bill") else 0,
            "guid": row.get("guid") or "",
            "master_id": row.get("master_id") or "",
            "alter_id": row.get("alter_id") or "",
            "last_synced": stamp,
        }

        existing_alter = frappe.db.get_value("Tally Ledger", name, "alter_id")
        if existing_alter is not None:
            # Unchanged in Tally? Just touch the sync stamp — much cheaper.
            if existing_alter and existing_alter == values["alter_id"]:
                frappe.db.set_value("Tally Ledger", name, "last_synced", stamp,
                                    update_modified=False)
                skipped += 1
                continue
            frappe.db.set_value("Tally Ledger", name, values, update_modified=False)
            updated += 1
        else:
            doc = frappe.get_doc({
                "doctype": "Tally Ledger",
                "ledger_name": name,
                **values,
            })
            doc.insert(ignore_permissions=True)
            created += 1

    frappe.db.commit()
    return {"created": created, "updated": updated, "unchanged": skipped}


@frappe.whitelist(methods=["POST"])
def upsert_vouchers(vouchers=None):
    """
    Insert or update vouchers with their ledger entries.

    Keyed on Tally's GUID, so re-syncing an overlapping date range updates in
    place instead of duplicating. Vouchers whose AlterID is unchanged are
    skipped without touching child rows.
    """
    _require_writer()
    rows = _parse_payload(vouchers, "vouchers")
    stamp = now_datetime()
    created = updated = skipped = 0

    for row in rows:
        guid = (row.get("guid") or "").strip()
        if not guid:
            continue

        alter_id = row.get("alter_id") or ""
        existing = frappe.db.get_value(
            "Tally Voucher", {"guid": guid}, ["name", "alter_id"], as_dict=True
        )

        if existing and alter_id and existing.alter_id == alter_id:
            skipped += 1
            continue

        fields = {
            "voucher_type": row.get("voucher_type") or "",
            "voucher_number": row.get("voucher_number") or "",
            "voucher_date": row.get("date") or row.get("voucher_date"),
            "party": row.get("party") or "",
            "narration": row.get("narration") or "",
            "amount": flt(row.get("amount")),
            "is_cancelled": 1 if row.get("is_cancelled") else 0,
            "alter_id": alter_id,
            "last_synced": stamp,
        }
        entries = row.get("entries") or []

        if existing:
            doc = frappe.get_doc("Tally Voucher", existing.name)
            doc.update(fields)
            doc.set("entries", [])
            updated += 1
        else:
            doc = frappe.get_doc({"doctype": "Tally Voucher", "guid": guid, **fields})
            created += 1

        for e in entries:
            doc.append("entries", {
                "ledger": e.get("ledger") or "",
                "amount": flt(e.get("amount")),
                "is_debit": 1 if e.get("is_debit") else 0,
            })

        doc.flags.ignore_permissions = True
        if existing:
            doc.save(ignore_permissions=True)
        else:
            doc.insert(ignore_permissions=True)

    frappe.db.commit()
    return {"created": created, "updated": updated, "unchanged": skipped}


@frappe.whitelist(methods=["POST"])
def log_sync(status=None, detail=None):
    """Record a sync run so failures are visible without SSHing anywhere."""
    _require_writer()
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            detail = {"raw": detail}
    detail = detail or {}

    doc = frappe.get_doc({
        "doctype": "Tally Sync Log",
        "sync_time": now_datetime(),
        "status": status if status in ("Success", "Failed", "Partial") else "Partial",
        "ledgers_synced": int(detail.get("ledgers") or 0),
        "vouchers_synced": int(detail.get("vouchers") or 0),
        "date_range": detail.get("range") or "",
        "duration_seconds": flt(detail.get("seconds")),
        "detail": json.dumps(detail, indent=2, default=str),
    })
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "name": doc.name}


@frappe.whitelist(methods=["GET"])
def get_sync_state():
    """Tell the agent where to resume from."""
    last_voucher_date = frappe.db.sql(
        "SELECT MAX(voucher_date) FROM `tabTally Voucher`"
    )[0][0]
    last_success = frappe.db.get_value(
        "Tally Sync Log", {"status": "Success"}, "sync_time",
        order_by="sync_time desc",
    )
    return {
        "last_voucher_date": str(last_voucher_date) if last_voucher_date else None,
        "last_successful_sync": str(last_success) if last_success else None,
        "voucher_count": frappe.db.count("Tally Voucher"),
        "ledger_count": frappe.db.count("Tally Ledger"),
    }


# ===========================================================================
# Analytics (Claude -> MCP -> Frappe).  Read-only.
# ===========================================================================

def _limit(n, default=100) -> int:
    try:
        n = int(n) if n else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_ROWS))


@frappe.whitelist(methods=["GET"])
def outstanding(party_type="receivable", limit=100, min_amount=0):
    """
    Outstanding balances by party.

    party_type: "receivable" (Sundry Debtors) or "payable" (Sundry Creditors).
    Returns positive `outstanding` amounts with an explicit `direction`.
    """
    groups = RECEIVABLE_GROUPS if party_type == "receivable" else PAYABLE_GROUPS
    rows = frappe.db.sql(
        """
        SELECT ledger_name, parent_group, closing_balance, gstin, email, phone
        FROM `tabTally Ledger`
        WHERE parent_group IN %(groups)s
          AND ABS(closing_balance) > %(min_amount)s
        ORDER BY ABS(closing_balance) DESC
        LIMIT %(limit)s
        """,
        {
            "groups": groups,
            "min_amount": flt(min_amount),
            "limit": _limit(limit),
        },
        as_dict=True,
    )
    out = []
    for r in rows:
        bal = flt(r.closing_balance)
        out.append({
            "party": r.ledger_name,
            "group": r.parent_group,
            "outstanding": abs(bal),
            "direction": "owes_us" if bal > 0 else "we_owe",
            "gstin": r.gstin,
            "email": r.email,
            "phone": r.phone,
        })
    total = sum(r["outstanding"] for r in out)
    return {"party_type": party_type, "count": len(out), "total": total, "rows": out}


@frappe.whitelist(methods=["GET"])
def ledger_statement(ledger=None, from_date=None, to_date=None, limit=500):
    """Every transaction hitting one ledger, with a running balance."""
    if not ledger:
        frappe.throw("`ledger` is required")

    master = frappe.db.get_value(
        "Tally Ledger", ledger,
        ["ledger_name", "parent_group", "opening_balance", "closing_balance"],
        as_dict=True,
    )
    if not master:
        # Be helpful rather than blank: suggest near matches.
        suggestions = frappe.db.sql(
            "SELECT ledger_name FROM `tabTally Ledger` WHERE ledger_name LIKE %(q)s LIMIT 10",
            {"q": f"%{ledger}%"}, as_dict=True,
        )
        return {
            "error": f"No ledger named '{ledger}'.",
            "suggestions": [s.ledger_name for s in suggestions],
        }

    conds = ["e.ledger = %(ledger)s", "v.is_cancelled = 0"]
    params: dict[str, Any] = {"ledger": ledger, "limit": _limit(limit, 500)}
    if from_date:
        conds.append("v.voucher_date >= %(from_date)s")
        params["from_date"] = getdate(from_date)
    if to_date:
        conds.append("v.voucher_date <= %(to_date)s")
        params["to_date"] = getdate(to_date)

    rows = frappe.db.sql(
        f"""
        SELECT v.voucher_date, v.voucher_type, v.voucher_number, v.party,
               v.narration, e.amount, e.is_debit
        FROM `tabTally Voucher Entry` e
        INNER JOIN `tabTally Voucher` v ON v.name = e.parent
        WHERE {' AND '.join(conds)}
        ORDER BY v.voucher_date ASC, v.voucher_number ASC
        LIMIT %(limit)s
        """,
        params, as_dict=True,
    )

    running = 0.0
    txns = []
    for r in rows:
        amt = flt(r.amount)
        running += amt
        txns.append({
            "date": str(r.voucher_date),
            "voucher_type": r.voucher_type,
            "voucher_number": r.voucher_number,
            "party": r.party,
            "narration": r.narration,
            "debit": amt if amt > 0 else 0.0,
            "credit": abs(amt) if amt < 0 else 0.0,
            "running_total": round(running, 2),
        })

    return {
        "ledger": master.ledger_name,
        "group": master.parent_group,
        "opening_balance": flt(master.opening_balance),
        "closing_balance": flt(master.closing_balance),
        "period": {"from": str(from_date or ""), "to": str(to_date or "")},
        "transaction_count": len(txns),
        "period_movement": round(running, 2),
        "transactions": txns,
    }


@frappe.whitelist(methods=["GET"])
def day_book(from_date=None, to_date=None, voucher_type=None, party=None, limit=200):
    """Vouchers in a date range — the 'what happened' query."""
    conds = ["is_cancelled = 0"]
    params: dict[str, Any] = {"limit": _limit(limit, 200)}
    if from_date:
        conds.append("voucher_date >= %(from_date)s")
        params["from_date"] = getdate(from_date)
    if to_date:
        conds.append("voucher_date <= %(to_date)s")
        params["to_date"] = getdate(to_date)
    if voucher_type:
        conds.append("voucher_type = %(voucher_type)s")
        params["voucher_type"] = voucher_type
    if party:
        conds.append("party LIKE %(party)s")
        params["party"] = f"%{party}%"

    rows = frappe.db.sql(
        f"""
        SELECT voucher_date, voucher_type, voucher_number, party, amount, narration
        FROM `tabTally Voucher`
        WHERE {' AND '.join(conds)}
        ORDER BY voucher_date DESC, voucher_number DESC
        LIMIT %(limit)s
        """,
        params, as_dict=True,
    )
    return {
        "count": len(rows),
        "total_value": round(sum(flt(r.amount) for r in rows), 2),
        "rows": [{**r, "voucher_date": str(r.voucher_date)} for r in rows],
    }


@frappe.whitelist(methods=["GET"])
def trial_balance(group=None):
    """Closing balances rolled up by ledger group."""
    conds = []
    params: dict[str, Any] = {}
    if group:
        conds.append("parent_group = %(group)s")
        params["group"] = group
    where = f"WHERE {' AND '.join(conds)}" if conds else ""

    rows = frappe.db.sql(
        f"""
        SELECT parent_group AS `group`,
               COUNT(*) AS ledger_count,
               SUM(CASE WHEN closing_balance > 0 THEN closing_balance ELSE 0 END) AS debit,
               SUM(CASE WHEN closing_balance < 0 THEN -closing_balance ELSE 0 END) AS credit
        FROM `tabTally Ledger`
        {where}
        GROUP BY parent_group
        HAVING debit <> 0 OR credit <> 0
        ORDER BY (debit + credit) DESC
        """,
        params, as_dict=True,
    )
    total_debit = sum(flt(r.debit) for r in rows)
    total_credit = sum(flt(r.credit) for r in rows)
    return {
        "rows": rows,
        "total_debit": round(total_debit, 2),
        "total_credit": round(total_credit, 2),
        "difference": round(total_debit - total_credit, 2),
    }


@frappe.whitelist(methods=["GET"])
def summary_by_voucher_type(from_date=None, to_date=None):
    """Volume and value per voucher type — the shape of the period."""
    conds = ["is_cancelled = 0"]
    params: dict[str, Any] = {}
    if from_date:
        conds.append("voucher_date >= %(from_date)s")
        params["from_date"] = getdate(from_date)
    if to_date:
        conds.append("voucher_date <= %(to_date)s")
        params["to_date"] = getdate(to_date)

    rows = frappe.db.sql(
        f"""
        SELECT voucher_type, COUNT(*) AS count, SUM(amount) AS total,
               MIN(voucher_date) AS first_date, MAX(voucher_date) AS last_date
        FROM `tabTally Voucher`
        WHERE {' AND '.join(conds)}
        GROUP BY voucher_type
        ORDER BY total DESC
        """,
        params, as_dict=True,
    )
    for r in rows:
        r["first_date"] = str(r["first_date"])
        r["last_date"] = str(r["last_date"])
    return {"rows": rows, "grand_total": round(sum(flt(r["total"]) for r in rows), 2)}


@frappe.whitelist(methods=["GET"])
def search_ledgers(query=None, limit=25):
    """Fuzzy ledger lookup — lets Claude resolve 'Acme' to the real name."""
    if not query:
        frappe.throw("`query` is required")
    rows = frappe.db.sql(
        """
        SELECT ledger_name, parent_group, closing_balance, gstin
        FROM `tabTally Ledger`
        WHERE ledger_name LIKE %(q)s
        ORDER BY ABS(closing_balance) DESC
        LIMIT %(limit)s
        """,
        {"q": f"%{query}%", "limit": _limit(limit, 25)},
        as_dict=True,
    )
    return {"count": len(rows), "rows": rows}


@frappe.whitelist(methods=["GET"])
def unbalanced_vouchers(from_date=None, to_date=None, tolerance=0.01, limit=100):
    """
    Reconciliation check: vouchers whose ledger entries don't net to zero.

    A healthy double-entry book has none. Hits here usually mean a partial
    sync or an XML export that dropped entries — worth investigating before
    trusting any downstream report.
    """
    conds = ["v.is_cancelled = 0"]
    params: dict[str, Any] = {"tol": flt(tolerance), "limit": _limit(limit, 100)}
    if from_date:
        conds.append("v.voucher_date >= %(from_date)s")
        params["from_date"] = getdate(from_date)
    if to_date:
        conds.append("v.voucher_date <= %(to_date)s")
        params["to_date"] = getdate(to_date)

    rows = frappe.db.sql(
        f"""
        SELECT v.name AS guid, v.voucher_date, v.voucher_type, v.voucher_number,
               v.party, v.amount, SUM(e.amount) AS entry_net, COUNT(e.name) AS entry_count
        FROM `tabTally Voucher` v
        LEFT JOIN `tabTally Voucher Entry` e ON e.parent = v.name
        WHERE {' AND '.join(conds)}
        GROUP BY v.name, v.voucher_date, v.voucher_type, v.voucher_number, v.party, v.amount
        HAVING ABS(COALESCE(SUM(e.amount), 0)) > %(tol)s OR COUNT(e.name) = 0
        ORDER BY ABS(COALESCE(SUM(e.amount), 0)) DESC
        LIMIT %(limit)s
        """,
        params, as_dict=True,
    )
    for r in rows:
        r["voucher_date"] = str(r["voucher_date"])
    return {
        "count": len(rows),
        "healthy": len(rows) == 0,
        "note": "Entries should net to zero. Non-zero rows indicate a sync or export problem.",
        "rows": rows,
    }


@frappe.whitelist(methods=["GET"])
def sync_health():
    """Is the mirror fresh and complete? Claude should check this first."""
    state = get_sync_state()
    last_log = frappe.db.get_value(
        "Tally Sync Log", {}, ["name", "status", "sync_time", "vouchers_synced", "detail"],
        order_by="creation desc", as_dict=True,
    )
    stale_hours = None
    if last_log and last_log.get("sync_time"):
        delta = now_datetime() - last_log["sync_time"]
        stale_hours = round(delta.total_seconds() / 3600, 1)

    recent_failures = frappe.db.count(
        "Tally Sync Log",
        {"status": "Failed", "sync_time": [">", add_days(now_datetime(), -1)]},
    )

    return {
        **state,
        "last_sync_status": last_log.get("status") if last_log else None,
        "last_sync_time": str(last_log.get("sync_time")) if last_log else None,
        "hours_since_last_sync": stale_hours,
        "failures_last_24h": recent_failures,
        "is_fresh": stale_hours is not None and stale_hours < 24,
    }


def check_sync_freshness():
    """Hourly scheduled job — logs a warning if the agent has gone quiet."""
    health = sync_health()
    if not health.get("is_fresh"):
        frappe.log_error(
            title="Tally sync is stale",
            message=json.dumps(health, indent=2, default=str),
        )
