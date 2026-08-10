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

Companies
---------
Tally books are commonly split into one company file per financial year, so
the same business appears as several "companies" ("ACME 2023-24", "ACME
2024-25", ...). Every record therefore carries a `company` field naming the
file it came from, and ledgers are keyed on Tally's GUID rather than on the
ledger name — the same party exists once per year, and collapsing those into
one row would overwrite each year with the next.

`ledger_name` is the join key ACROSS companies: it is how the same party is
recognised from one year to the next. See `compare_ledger()`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import flt, getdate, now_datetime, add_days

# Groups treated as receivable / payable, matched against a ledger's RESOLVED
# root group rather than its immediate parent.
#
# This matters: real charts of accounts nest. In this book, customers sit under
# groups like "AGENT RK" and "Sundry Debtors Online" which are children of
# "Sundry Debtors". Matching on the immediate parent found only 8% of
# receivables. The sync agent walks the group tree and stores `primary_group`,
# which is what these are compared against.
RECEIVABLE_GROUPS = ("Sundry Debtors",)
PAYABLE_GROUPS = ("Sundry Creditors",)

MAX_ROWS = 2000  # hard cap so a bad query can never dump the whole ledger set


def _ledger_docname(company: str, ledger_name: str, guid: str = "") -> str:
    """
    Stable, collision-free primary key for a ledger.

    Tally's GUID is unique per company file, so the same party in two financial
    years gets two rows — which is what makes year-on-year comparison possible.
    Falls back to company::name when a GUID is missing, hashing the tail if the
    pair would exceed Frappe's 140-character name limit.
    """
    guid = (guid or "").strip()
    if guid:
        return guid
    key = f"{company}::{ledger_name}"
    if len(key) <= 140:
        return key
    digest = hashlib.md5(ledger_name.encode("utf-8")).hexdigest()[:16]
    return f"{company[:100]}::{digest}"


def _company_clause(company, conds: list, params: dict, col: str = "company") -> None:
    """Append an optional company filter. Accepts a single name or a list."""
    if not company:
        return
    if isinstance(company, str):
        try:
            parsed = json.loads(company)
            company = parsed if isinstance(parsed, list) else company
        except ValueError:
            pass
    if isinstance(company, (list, tuple)) and company:
        conds.append(f"{col} IN %(company)s")
        params["company"] = tuple(company)
    else:
        conds.append(f"{col} = %(company)s")
        params["company"] = company


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
        company = (row.get("company") or "").strip()
        docname = _ledger_docname(company, name, row.get("guid"))

        values = {
            "ledger_name": name,
            "company": company,
            "parent_group": row.get("parent") or row.get("parent_group") or "",
            "primary_group": row.get("primary_group") or row.get("parent") or "",
            "group_path": row.get("group_path") or "",
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

        existing_alter = frappe.db.get_value("Tally Ledger", docname, "alter_id")
        if existing_alter is not None:
            # Unchanged in Tally? Just touch the sync stamp — much cheaper.
            if existing_alter and existing_alter == values["alter_id"]:
                frappe.db.set_value("Tally Ledger", docname, "last_synced", stamp,
                                    update_modified=False)
                skipped += 1
                continue
            frappe.db.set_value("Tally Ledger", docname, values, update_modified=False)
            updated += 1
        else:
            doc = frappe.get_doc({"doctype": "Tally Ledger", **values})
            doc.name = docname
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
            "company": (row.get("company") or "").strip(),
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
        "company": detail.get("company") or "",
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
def get_sync_state(company=None):
    """
    Tell the agent where to resume from.

    Each company file keeps its own high-water mark, so a newly added year
    starts from scratch instead of inheriting another year's progress.
    """
    conds, params = [], {}
    _company_clause(company, conds, params)
    where = f"WHERE {' AND '.join(conds)}" if conds else ""

    last_voucher_date = frappe.db.sql(
        f"SELECT MAX(voucher_date) FROM `tabTally Voucher` {where}", params
    )[0][0]

    log_filter = {"status": "Success"}
    count_filter = {}
    if company and isinstance(company, str):
        log_filter["company"] = company
        count_filter["company"] = company
    last_success = frappe.db.get_value(
        "Tally Sync Log", log_filter, "sync_time", order_by="sync_time desc",
    )
    return {
        "company": company,
        "last_voucher_date": str(last_voucher_date) if last_voucher_date else None,
        "last_successful_sync": str(last_success) if last_success else None,
        "voucher_count": frappe.db.count("Tally Voucher", count_filter),
        "ledger_count": frappe.db.count("Tally Ledger", count_filter),
    }


@frappe.whitelist(methods=["GET"])
def companies():
    """
    Every company file mirrored, with its actual date span.

    Claude should call this before any year-on-year question so it knows which
    company names exist and which period each one covers.
    """
    rows = frappe.db.sql(
        """
        SELECT company,
               COUNT(*) AS voucher_count,
               MIN(voucher_date) AS first_voucher,
               MAX(voucher_date) AS last_voucher,
               SUM(amount) AS total_value
        FROM `tabTally Voucher`
        WHERE is_cancelled = 0 AND company != ''
        GROUP BY company
        ORDER BY MIN(voucher_date) ASC
        """,
        as_dict=True,
    )
    ledger_counts = dict(frappe.db.sql(
        "SELECT company, COUNT(*) FROM `tabTally Ledger` GROUP BY company"
    ))
    for r in rows:
        r["first_voucher"] = str(r["first_voucher"])
        r["last_voucher"] = str(r["last_voucher"])
        r["ledger_count"] = ledger_counts.get(r["company"], 0)
    return {"count": len(rows), "rows": rows}


@frappe.whitelist(methods=["GET"])
def compare_ledger(ledger_name=None, limit=50):
    """
    One party's position in every company file, oldest year first.

    This is the cross-year view Tally itself cannot easily give you: the same
    ledger name is matched across company files, so you can see how a party's
    balance and activity moved from one financial year to the next.
    """
    if not ledger_name:
        frappe.throw("`ledger_name` is required")

    rows = frappe.db.sql(
        """
        SELECT l.company, l.ledger_name, l.parent_group, l.primary_group,
               l.opening_balance, l.closing_balance, l.gstin
        FROM `tabTally Ledger` l
        WHERE l.ledger_name = %(n)s
        ORDER BY l.company ASC
        LIMIT %(limit)s
        """,
        {"n": ledger_name, "limit": _limit(limit, 50)}, as_dict=True,
    )
    if not rows:
        near = frappe.db.sql(
            "SELECT DISTINCT ledger_name FROM `tabTally Ledger` WHERE ledger_name LIKE %(q)s LIMIT 10",
            {"q": f"%{ledger_name}%"}, as_dict=True,
        )
        return {
            "error": f"No ledger named '{ledger_name}' in any company.",
            "suggestions": [n.ledger_name for n in near],
        }

    # Activity per company, so a year with no movement is visibly distinct
    # from a year that simply carried a balance forward.
    activity = dict(frappe.db.sql(
        """
        SELECT v.company, COUNT(*) FROM `tabTally Voucher Entry` e
        INNER JOIN `tabTally Voucher` v ON v.name = e.parent
        WHERE e.ledger = %(n)s AND v.is_cancelled = 0
        GROUP BY v.company
        """,
        {"n": ledger_name},
    ))
    spans = dict(frappe.db.sql(
        "SELECT company, MIN(voucher_date) FROM `tabTally Voucher` GROUP BY company"
    ))

    out = []
    for r in rows:
        bal = flt(r.closing_balance)
        out.append({
            "company": r.company,
            "period_starts": str(spans.get(r.company) or ""),
            "group": r.parent_group,
            "opening_balance": flt(r.opening_balance),
            "closing_balance": bal,
            "outstanding": abs(bal),
            "direction": "owes_us" if bal > 0 else ("we_owe" if bal < 0 else "settled"),
            "transaction_count": activity.get(r.company, 0),
            "gstin": r.gstin,
        })
    out.sort(key=lambda x: (x["period_starts"] or "", x["company"]))

    for i in range(1, len(out)):
        prev, cur = out[i - 1]["closing_balance"], out[i]["closing_balance"]
        out[i]["change_vs_previous"] = round(cur - prev, 2)

    return {
        "ledger_name": ledger_name,
        "appears_in_companies": len(out),
        "note": "Matched by ledger name across company files. Oldest period first.",
        "rows": out,
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
def outstanding(party_type="receivable", limit=100, min_amount=0, company=None):
    """
    Outstanding balances by party.

    party_type: "receivable" (Sundry Debtors) or "payable" (Sundry Creditors).
    Returns positive `outstanding` amounts with an explicit `direction`.
    """
    groups = RECEIVABLE_GROUPS if party_type == "receivable" else PAYABLE_GROUPS
    conds = ["COALESCE(NULLIF(primary_group, ''), parent_group) IN %(groups)s",
             "ABS(closing_balance) > %(min_amount)s"]
    params = {"groups": groups, "min_amount": flt(min_amount), "limit": _limit(limit)}
    _company_clause(company, conds, params)
    rows = frappe.db.sql(
        f"""
        SELECT company, ledger_name, parent_group, primary_group, group_path,
               closing_balance, gstin, email, phone
        FROM `tabTally Ledger`
        WHERE {' AND '.join(conds)}
        ORDER BY ABS(closing_balance) DESC
        LIMIT %(limit)s
        """,
        params,
        as_dict=True,
    )
    out = []
    for r in rows:
        bal = flt(r.closing_balance)
        out.append({
            "party": r.ledger_name,
            "company": r.company,
            "group": r.parent_group,
            "primary_group": r.primary_group,
            "outstanding": abs(bal),
            "direction": "owes_us" if bal > 0 else "we_owe",
            "gstin": r.gstin,
            "email": r.email,
            "phone": r.phone,
        })
    total = sum(r["outstanding"] for r in out)
    return {
        "party_type": party_type,
        "company_filter": company or "all companies",
        "count": len(out),
        "total": total,
        "note": ("Spans multiple company files; the same party may appear once per "
                 "financial year. Pass `company` to scope to one."
                 if not company else None),
        "rows": out,
    }


@frappe.whitelist(methods=["GET"])
def ledger_statement(ledger=None, from_date=None, to_date=None, limit=500, company=None):
    """Every transaction hitting one ledger, with a running balance."""
    if not ledger:
        frappe.throw("`ledger` is required")

    # `ledger` is a ledger NAME, which repeats once per company file.
    mfilter = {"ledger_name": ledger}
    if company and isinstance(company, str):
        mfilter["company"] = company
    matches = frappe.get_all(
        "Tally Ledger", filters=mfilter,
        fields=["name", "company", "ledger_name", "parent_group", "primary_group",
                "group_path", "opening_balance", "closing_balance"],
        limit=20,
    )
    if len(matches) > 1:
        return {
            "needs_company": True,
            "ledger": ledger,
            "note": ("This ledger exists in several company files. Re-run with "
                     "`company` set to one of these, or use compare_ledger() to "
                     "see all years at once."),
            "found_in": [
                {"company": m.company, "closing_balance": flt(m.closing_balance)}
                for m in matches
            ],
        }
    master = matches[0] if matches else None
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
    _company_clause(master.company, conds, params, col="v.company")
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
        "company": master.company,
        "group": master.parent_group,
        "primary_group": master.primary_group,
        "group_path": master.group_path,
        "opening_balance": flt(master.opening_balance),
        "closing_balance": flt(master.closing_balance),
        "period": {"from": str(from_date or ""), "to": str(to_date or "")},
        "transaction_count": len(txns),
        "period_movement": round(running, 2),
        "transactions": txns,
    }


@frappe.whitelist(methods=["GET"])
def day_book(from_date=None, to_date=None, voucher_type=None, party=None, limit=200, company=None):
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
    _company_clause(company, conds, params)

    rows = frappe.db.sql(
        f"""
        SELECT company, voucher_date, voucher_type, voucher_number, party, amount, narration
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
def trial_balance(group=None, company=None):
    """Closing balances rolled up by ledger group."""
    conds = []
    params: dict[str, Any] = {}
    if group:
        conds.append("COALESCE(NULLIF(primary_group, ''), parent_group) = %(group)s")
        params["group"] = group
    _company_clause(company, conds, params)
    where = f"WHERE {' AND '.join(conds)}" if conds else ""

    rows = frappe.db.sql(
        f"""
        SELECT company,
               COALESCE(NULLIF(primary_group, ''), parent_group) AS `group`,
               COUNT(*) AS ledger_count,
               SUM(CASE WHEN closing_balance > 0 THEN closing_balance ELSE 0 END) AS debit,
               SUM(CASE WHEN closing_balance < 0 THEN -closing_balance ELSE 0 END) AS credit
        FROM `tabTally Ledger`
        {where}
        GROUP BY company, COALESCE(NULLIF(primary_group, ''), parent_group)
        HAVING debit <> 0 OR credit <> 0
        ORDER BY (debit + credit) DESC
        """,
        params, as_dict=True,
    )
    total_debit = sum(flt(r.debit) for r in rows)
    total_credit = sum(flt(r.credit) for r in rows)
    return {
        "company_filter": company or "all companies",
        "note": ("Grouped by company. A trial balance is only meaningful within "
                 "ONE company file — do not sum across years."),
        "rows": rows,
        "total_debit": round(total_debit, 2),
        "total_credit": round(total_credit, 2),
        "difference": round(total_debit - total_credit, 2),
    }


@frappe.whitelist(methods=["GET"])
def summary_by_voucher_type(from_date=None, to_date=None, company=None):
    """Volume and value per voucher type — the shape of the period."""
    conds = ["is_cancelled = 0"]
    params: dict[str, Any] = {}
    if from_date:
        conds.append("voucher_date >= %(from_date)s")
        params["from_date"] = getdate(from_date)
    if to_date:
        conds.append("voucher_date <= %(to_date)s")
        params["to_date"] = getdate(to_date)
    _company_clause(company, conds, params)

    rows = frappe.db.sql(
        f"""
        SELECT company, voucher_type, COUNT(*) AS count, SUM(amount) AS total,
               MIN(voucher_date) AS first_date, MAX(voucher_date) AS last_date
        FROM `tabTally Voucher`
        WHERE {' AND '.join(conds)}
        GROUP BY company, voucher_type
        ORDER BY total DESC
        """,
        params, as_dict=True,
    )
    for r in rows:
        r["first_date"] = str(r["first_date"])
        r["last_date"] = str(r["last_date"])
    return {"rows": rows, "grand_total": round(sum(flt(r["total"]) for r in rows), 2)}


@frappe.whitelist(methods=["GET"])
def group_summary(company=None, root=None, limit=200):
    """
    Ledger groups with their totals, mirroring Tally's Group Summary.

    Rolls up by the RESOLVED root group, so sub-groups such as "AGENT RK" are
    counted inside "Sundry Debtors" exactly as Tally reports them. Use this to
    reconcile against Tally's own Group Summary screen.
    """
    conds = []
    params = {"limit": _limit(limit, 200)}
    _company_clause(company, conds, params)
    if root:
        conds.append("COALESCE(NULLIF(primary_group, ''), parent_group) = %(root)s")
        params["root"] = root
    where = f"WHERE {' AND '.join(conds)}" if conds else ""

    rows = frappe.db.sql(
        f"""
        SELECT company,
               COALESCE(NULLIF(primary_group, ''), parent_group) AS root_group,
               parent_group AS sub_group,
               COUNT(*) AS ledger_count,
               SUM(CASE WHEN closing_balance > 0 THEN closing_balance ELSE 0 END) AS debit,
               SUM(CASE WHEN closing_balance < 0 THEN -closing_balance ELSE 0 END) AS credit
        FROM `tabTally Ledger`
        {where}
        GROUP BY company, root_group, sub_group
        HAVING debit <> 0 OR credit <> 0
        ORDER BY root_group ASC, (debit + credit) DESC
        LIMIT %(limit)s
        """,
        params, as_dict=True,
    )
    return {
        "count": len(rows),
        "total_debit": round(sum(flt(r.debit) for r in rows), 2),
        "total_credit": round(sum(flt(r.credit) for r in rows), 2),
        "note": ("Debit is positive, matching Tally's Group Summary. Compare "
                 "these figures directly against that screen in Tally."),
        "rows": rows,
    }


@frappe.whitelist(methods=["GET"])
def search_ledgers(query=None, limit=25, company=None):
    """Fuzzy ledger lookup — lets Claude resolve 'Acme' to the real name."""
    if not query:
        frappe.throw("`query` is required")
    conds = ["ledger_name LIKE %(q)s"]
    params = {"q": f"%{query}%", "limit": _limit(limit, 25)}
    _company_clause(company, conds, params)
    rows = frappe.db.sql(
        f"""
        SELECT company, ledger_name, parent_group, primary_group, closing_balance, gstin
        FROM `tabTally Ledger`
        WHERE {' AND '.join(conds)}
        ORDER BY ABS(closing_balance) DESC
        LIMIT %(limit)s
        """,
        params,
        as_dict=True,
    )
    distinct = sorted({r["ledger_name"] for r in rows})
    return {"count": len(rows), "distinct_names": distinct, "rows": rows}


@frappe.whitelist(methods=["GET"])
def unbalanced_vouchers(from_date=None, to_date=None, tolerance=0.01, limit=100, company=None):
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
    _company_clause(company, conds, params, col="v.company")

    rows = frappe.db.sql(
        f"""
        SELECT v.company, v.name AS guid, v.voucher_date, v.voucher_type, v.voucher_number,
               v.party, v.amount, SUM(e.amount) AS entry_net, COUNT(e.name) AS entry_count
        FROM `tabTally Voucher` v
        LEFT JOIN `tabTally Voucher Entry` e ON e.parent = v.name
        WHERE {' AND '.join(conds)}
        GROUP BY v.company, v.name, v.voucher_date, v.voucher_type, v.voucher_number, v.party, v.amount
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
