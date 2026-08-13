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

# Order vouchers are commitments, not postings: in Tally a Sales/Purchase
# Order carries a party and an amount but writes NOTHING to any ledger.
# Mirrored verbatim, they still land in `tabTally Voucher` beside real
# transactions — 2,044 Sales Orders (10.66cr) were being counted into party
# statements and period totals, which is why a customer's statement could
# never reconcile to the closing balance printed beside it. Every endpoint
# that answers an ACCOUNTING question must exclude them; day_book keeps them
# visible because Tally's own Day Book shows orders too.
# Matched by type name: the reserved parent type is not fetched from Tally,
# and this book uses the stock names.
ORDER_TYPES = ("Sales Order", "Purchase Order")
_NOT_ORDER = "voucher_type NOT IN %(order_types)s"
_NOT_ORDER_V = "v.voucher_type NOT IN %(order_types)s"

# Ageing buckets, in days past due. Chosen to match how collections are
# actually chased rather than a textbook 30/60/90.
AGEING_BUCKETS = ((0, "not due"), (30, "1-30"), (60, "31-60"),
                  (90, "61-90"), (180, "91-180"), (10**6, "180+"))


def _bucket(days: int) -> str:
    for limit, label in AGEING_BUCKETS:
        if days <= limit:
            return label
    return "180+"


def _docname(company: str, natural_key: str, guid: str = "") -> str:
    """
    Collision-free primary key, ALWAYS scoped to the company file.

    Tally GUIDs are NOT unique across company files. When a year is carried
    forward, ledgers keep the GUID they had in the previous year — so keying
    on the GUID alone made the (25-26) sync overwrite 633 rows belonging to
    (26-27), replacing this year's balances with last year's under the wrong
    label. Silent, and invisible in any single query.

    The company prefix is therefore mandatory, never optional. The GUID is
    still preferred WITHIN a company because it survives a rename; the name is
    the fallback when Tally omits it.
    """
    tail = (guid or "").strip() or natural_key
    key = f"{company}::{tail}"
    if len(key) <= 140:
        return key
    # Hash only the tail so the company stays readable in the docname.
    digest = hashlib.md5(tail.encode("utf-8")).hexdigest()[:16]
    return f"{company[:100]}::{digest}"


# Kept as an alias: several call sites read better as "ledger docname".
_ledger_docname = _docname


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
    if isinstance(company, (list, tuple)):
        if company:
            conds.append(f"{col} IN %(company)s")
            params["company"] = tuple(company)
        return  # empty list means no filter, not 'equal to nothing'
    conds.append(f"{col} = %(company)s")
    params["company"] = company


# ===========================================================================
# Ingestion (sync agent -> Frappe)
# ===========================================================================

def _require_writer():
    """Ingestion is privileged: only System Manager may write mirrored data."""
    if "System Manager" not in frappe.get_roles():
        frappe.throw("Not permitted: sync user needs the System Manager role.", frappe.PermissionError)


def _require_reader():
    """
    Analytics run raw SQL, which bypasses DocType permissions entirely — so
    gate them on the same roles the DocTypes grant read to. Without this, any
    authenticated user (a future portal signup, say) could pull the entire
    receivables list with contact details.
    """
    roles = frappe.get_roles()
    if "System Manager" not in roles and "Accounts User" not in roles:
        frappe.throw("Not permitted: reading Tally data needs the Accounts User role.",
                     frappe.PermissionError)


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
    created = updated = skipped = pruned = 0
    errors: list = []

    for i, row in enumerate(rows):
      name = (row.get("name") or "").strip()
      if not name:
          continue
      # Real books contain data Frappe's validators dislike — an email field
      # holding a bare domain, a name with odd characters. One such row must
      # not take down the other 499 in the batch, so each is isolated and the
      # failures are reported back for the agent to log.
      savepoint = f"row_{i}"
      try:
        frappe.db.savepoint(savepoint)
        company = (row.get("company") or "").strip()
        # Strip ONCE and reuse for the docname, the stored value and the match
        # filter. _ledger_docname strips internally, so leaving it unstripped
        # here made the lookup and the primary key disagree by construction:
        # a whitespace-only GUID is truthy, so the filter became
        # {company, guid: " "} — and under MariaDB's PAD SPACE collation that
        # compares equal to guid = "", returning every GUID-less ledger in the
        # company for the prune below to delete.
        guid = (row.get("guid") or "").strip()
        docname = _ledger_docname(company, name, guid)

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
            "guid": guid,
            "master_id": row.get("master_id") or "",
            "alter_id": row.get("alter_id") or "",
            "last_synced": stamp,
        }

        # Resolve the existing row by FIELDS, never by docname. The docname
        # formula has already changed once (company scoping), and a lookup
        # keyed on it silently failed to see every row written under the old
        # formula — so all 2,476 ledgers were inserted a SECOND time beside
        # their originals and every balance in the mirror doubled. Parties
        # showed twice with different figures and there was no way to tell
        # which was current. upsert_vouchers has always matched on fields,
        # which is precisely why the same rename left 19,566 vouchers intact.
        match = ({"company": company, "guid": guid} if guid else
                 {"company": company, "ledger_name": name})
        found = frappe.get_all("Tally Ledger", filters=match,
                               fields=["name", "alter_id"],
                               order_by="creation desc")

        # More than one row for the same ledger IS the duplication above.
        # Keep the NEWEST and drop the rest, so an ordinary --ledgers-only run
        # repairs the mirror in place. Without this the stale generation is
        # unreachable forever: nothing else in this file ever deletes a
        # Tally Ledger row.
        #
        # Newest, not oldest, and the direction is load-bearing. The older row
        # is the pre-company-scoping generation whose docname is a bare GUID —
        # the very primary key that had to be abandoned because Tally reuses
        # GUIDs across financial-year files. Keeping it would delete every
        # correctly scoped row, reinstate the unsafe key for the whole table,
        # and leave no later run able to converge, since the field match would
        # keep finding the bare-GUID survivor and updating it in place forever.
        had_duplicates = len(found) > 1
        for extra in found[1:]:
            frappe.db.delete("Tally Ledger", {"name": extra.name})
            pruned += 1
        found = found[:1]

        if found:
            target = found[0].name
            existing_alter = found[0].alter_id
            # Unchanged in Tally? Just touch the sync stamp — much cheaper.
            # Skip that shortcut when this ledger had duplicates: the row we
            # kept may be the stale generation, so its figures must be
            # rewritten from Tally even if AlterID looks unchanged.
            if existing_alter and existing_alter == values["alter_id"] and not had_duplicates:
                frappe.db.set_value("Tally Ledger", target, "last_synced", stamp,
                                    update_modified=False)
                skipped += 1
                continue
            frappe.db.set_value("Tally Ledger", target, values, update_modified=False)
            updated += 1
        else:
            doc = frappe.get_doc({"doctype": "Tally Ledger", **values})
            doc.name = docname
            doc.insert(ignore_permissions=True)
            created += 1
        frappe.db.release_savepoint(savepoint)
      except Exception as exc:
        try:
            frappe.db.rollback(save_point=savepoint)
        except Exception:
            # A deadlock or dropped connection destroys every savepoint. Keep
            # what committed, report the rest as failed, stop cleanly.
            frappe.db.rollback()
            # The rollback just discarded every write of this request —
            # including any prunes. Counters that keep their pre-rollback
            # values would report deletions and updates that never happened,
            # and a prune count is exactly the number an operator reconciles
            # row totals against.
            created = updated = skipped = pruned = 0
            errors.append({"ledger": (row.get("name") or "")[:140],
                           "error": f"{type(exc).__name__}: {exc}"[:300]})
            errors.append({"ledger": "(batch stopped)",
                           "error": "transaction was rolled back by the database; "
                                    "no rows from this batch were saved"})
            break
        if len(errors) < 50:
            errors.append({
                "ledger": (row.get("name") or "")[:140],
                "company": (row.get("company") or "")[:140],
                "error": f"{type(exc).__name__}: {exc}"[:300],
            })

    frappe.db.commit()
    out = {"created": created, "updated": updated, "unchanged": skipped}
    if pruned:
        # Surfaced so the agent logs it and the operator watches the mirror
        # repair itself, rather than wondering where rows went.
        out["pruned_duplicates"] = pruned
    if errors:
        out["failed"] = len(errors)
        out["errors"] = errors
    return out


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
    errors: list = []

    for i, row in enumerate(rows):
      guid = (row.get("guid") or "").strip()
      if not guid:
          continue
      # Isolated per voucher for the same reason as ledgers: one malformed
      # record must not discard the rest of the batch.
      savepoint = f"vch_{i}"
      try:
        frappe.db.savepoint(savepoint)
        alter_id = row.get("alter_id") or ""
        company_name = (row.get("company") or "").strip()
        existing = frappe.db.get_value(
            "Tally Voucher", {"guid": guid, "company": company_name},
            ["name", "alter_id"], as_dict=True,
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
        else:
            doc = frappe.get_doc({"doctype": "Tally Voucher", "guid": guid, **fields})
            # Same company-scoping rule as ledgers: a voucher GUID can repeat
            # across company files when a year is carried forward.
            doc.name = _docname(fields["company"], guid, guid)

        for e in entries:
            doc.append("entries", {
                "ledger": e.get("ledger") or "",
                "amount": flt(e.get("amount")),
                "is_debit": 1 if e.get("is_debit") else 0,
            })

        doc.flags.ignore_permissions = True
        if existing:
            doc.save(ignore_permissions=True)
            updated += 1
        else:
            doc.insert(ignore_permissions=True)
            created += 1
        frappe.db.release_savepoint(savepoint)
      except Exception as exc:
        try:
            frappe.db.rollback(save_point=savepoint)
        except Exception:
            frappe.db.rollback()
            errors.append({"voucher": guid[:140],
                           "error": f"{type(exc).__name__}: {exc}"[:300]})
            errors.append({"voucher": "(batch stopped)",
                           "error": "transaction was rolled back by the database; "
                                    "remaining rows in this batch were not attempted"})
            break
        if len(errors) < 50:
            errors.append({
                "voucher": f"{row.get('voucher_type') or ''} {row.get('voucher_number') or ''}".strip()[:140],
                "date": str(row.get("date") or row.get("voucher_date") or "")[:20],
                "company": (row.get("company") or "")[:140],
                "error": f"{type(exc).__name__}: {exc}"[:300],
            })

    frappe.db.commit()
    out = {"created": created, "updated": updated, "unchanged": skipped}
    if errors:
        out["failed"] = len(errors)
        out["errors"] = errors
    return out


@frappe.whitelist(methods=["POST"])
def upsert_bills(bills=None, company=None, replace=1):
    """
    Replace the outstanding-bill snapshot for one company.

    Tally's Bills collection returns only what is still UNPAID, so this is a
    snapshot rather than a log: a bill that gets paid simply stops appearing.
    That means the old rows for the company must be cleared, or paid invoices
    would linger forever and overstate receivables.
    """
    _require_writer()
    rows = _parse_payload(bills, "bills")
    stamp = now_datetime()

    if int(replace or 0) and company:
        frappe.db.delete("Tally Bill", {"company": company})

    # Party group and GSTIN are denormalised so ageing can be sliced by agent
    # without a join per row.
    parties = {}
    if company:
        for l in frappe.get_all(
            "Tally Ledger", filters={"company": company},
            fields=["ledger_name", "primary_group", "gstin"], limit_page_length=0,
        ):
            parties[l.ledger_name] = (l.primary_group, l.gstin)

    created = 0
    errors: list = []
    for i, row in enumerate(rows):
        party = (row.get("party") or "").strip()
        ref = (row.get("name") or "").strip()
        if not party or not ref:
            continue
        savepoint = f"bill_{i}"
        try:
            frappe.db.savepoint(savepoint)
            grp, gstin = parties.get(party, ("", ""))
            doc = frappe.get_doc({
                "doctype": "Tally Bill",
                "party": party,
                "bill_ref": ref,
                "company": (row.get("company") or company or "").strip(),
                "bill_date": row.get("bill_date") or None,
                "due_date": row.get("due_date") or None,
                "overdue_days": int(row.get("overdue_days") or 0),
                "credit_period": row.get("credit_period") or "",
                "opening_amount": flt(row.get("opening")),
                "outstanding": flt(row.get("closing")),
                "is_advance": 1 if row.get("is_advance") else 0,
                "primary_group": grp or "",
                "gstin": gstin or "",
                "last_synced": stamp,
            })
            # Bill references repeat across parties, so the key is the triple.
            doc.name = _ledger_docname(
                f"{row.get('company') or company or ''}|{party}", ref)
            doc.insert(ignore_permissions=True)
            created += 1
            frappe.db.release_savepoint(savepoint)
        except Exception as exc:
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                frappe.db.rollback()
                break
            if len(errors) < 50:
                errors.append({"bill": f"{party} / {ref}"[:140],
                               "error": f"{type(exc).__name__}: {exc}"[:300]})

    frappe.db.commit()
    out = {"created": created}
    if errors:
        out["failed"] = len(errors)
        out["errors"] = errors
    return out


def _upsert_simple(doctype: str, rows: list, company: str, key_field: str,
                   mapping: dict, stamp) -> dict:
    """
    Replace a company's rows for a small master table.

    Masters are snapshots — an item deleted in Tally should disappear here —
    and these tables are small enough (units, godowns, groups, items) that a
    clean replace is simpler and safer than diffing.
    """
    if company:
        frappe.db.delete(doctype, {"company": company})
    created = 0
    errors: list = []
    for i, row in enumerate(rows):
        name = (row.get("name") or "").strip()
        if not name:
            continue
        savepoint = f"m_{i}"
        try:
            frappe.db.savepoint(savepoint)
            values = {"doctype": doctype, key_field: name,
                      "company": (row.get("company") or company or "").strip(),
                      "last_synced": stamp}
            for dest, src in mapping.items():
                v = row.get(src)
                values[dest] = v if v is not None else ""
            doc = frappe.get_doc(values)
            doc.name = _ledger_docname(values["company"], name, row.get("guid"))
            doc.insert(ignore_permissions=True)
            created += 1
            frappe.db.release_savepoint(savepoint)
        except Exception as exc:
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                frappe.db.rollback()
                break
            if len(errors) < 50:
                errors.append({"row": name[:140],
                               "error": f"{type(exc).__name__}: {exc}"[:300]})
    frappe.db.commit()
    out = {"created": created}
    if errors:
        out["failed"] = len(errors)
        out["errors"] = errors
    return out


@frappe.whitelist(methods=["POST"])
def upsert_inventory(units=None, godowns=None, stock_groups=None,
                     stock_items=None, company=None):
    """
    Replace the inventory master snapshot for one company.

    Units first: their conversion table is what makes every quantity in the
    domain interpretable, and the agent resolves compound quantities before
    sending, so this only stores the result plus the raw string for audit.
    """
    _require_writer()
    stamp = now_datetime()
    out = {}

    if units is not None:
        out["units"] = _upsert_simple(
            "Tally Unit", _parse_payload(units, "units"), company, "unit_name",
            {"formal_name": "formal_name", "base_units": "base_units",
             "conversion": "conversion", "guid": "guid"}, stamp)
    if godowns is not None:
        out["godowns"] = _upsert_simple(
            "Tally Godown", _parse_payload(godowns, "godowns"), company,
            "godown_name", {"parent_godown": "parent", "guid": "guid"}, stamp)
    if stock_groups is not None:
        out["stock_groups"] = _upsert_simple(
            "Tally Stock Group", _parse_payload(stock_groups, "stock_groups"),
            company, "group_name",
            {"parent_group": "parent", "primary_group": "primary_group",
             "guid": "guid"}, stamp)
    if stock_items is not None:
        out["stock_items"] = _upsert_simple(
            "Tally Stock Item", _parse_payload(stock_items, "stock_items"),
            company, "item_name",
            {"stock_group": "parent", "primary_group": "primary_group",
             "category": "category", "part_no": "part_no",
             "base_units": "base_units", "additional_units": "additional_units",
             "conversion": "conversion",
             "closing_qty": "closing_qty", "closing_qty_unit": "closing_qty_unit",
             "closing_qty_raw": "closing_qty_raw",
             "closing_rate": "closing_rate", "closing_rate_unit": "closing_rate_unit",
             "closing_value": "closing_value", "costing_method": "costing_method",
             "hsn_code": "hsn_code", "hsn_description": "hsn_description",
             "gst_rate": "gst_rate", "taxability": "taxability",
             "is_batchwise": "is_batchwise", "guid": "guid",
             "alter_id": "alter_id"}, stamp)
    return out


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
    _require_reader()
    # Normalise: a JSON-encoded single-element list means that one company.
    if isinstance(company, str) and company.startswith("["):
        try:
            parsed = json.loads(company)
            if isinstance(parsed, list):
                company = parsed[0] if len(parsed) == 1 else parsed
        except ValueError:
            pass

    conds, params = [], {}
    _company_clause(company, conds, params)
    where = f"WHERE {' AND '.join(conds)}" if conds else ""

    last_voucher_date = frappe.db.sql(
        f"SELECT MAX(voucher_date) FROM `tabTally Voucher` {where}", params
    )[0][0]

    log_filter = {"status": "Success"}
    count_filter = {}
    if isinstance(company, str) and company:
        log_filter["company"] = company
        count_filter["company"] = company
    elif isinstance(company, (list, tuple)) and company:
        log_filter["company"] = ["in", list(company)]
        count_filter["company"] = ["in", list(company)]
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
    _require_reader()
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
    _require_reader()
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
          AND v.voucher_type NOT IN %(order_types)s
        GROUP BY v.company
        """,
        {"n": ledger_name, "order_types": ORDER_TYPES},
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
    _require_reader()
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
    _require_reader()
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

    # Orders excluded: a statement's running total must reconcile to the
    # closing balance shown beside it, and orders post nothing in Tally.
    conds = ["e.ledger = %(ledger)s", "v.is_cancelled = 0", _NOT_ORDER_V]
    params: dict[str, Any] = {"ledger": ledger, "limit": _limit(limit, 500),
                              "order_types": ORDER_TYPES}
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
    _require_reader()
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
    _require_reader()
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
        HAVING SUM(CASE WHEN closing_balance > 0 THEN closing_balance ELSE 0 END) <> 0 OR SUM(CASE WHEN closing_balance < 0 THEN -closing_balance ELSE 0 END) <> 0
        -- Sort on the aggregates themselves, not on their aliases. MySQL
        -- permits a bare alias here but rejects one inside an expression when
        -- it names a group function: "Reference 'debit' not supported
        -- (reference to group function)", a 500 that made this endpoint fail
        -- on every call it has ever received.
        ORDER BY (SUM(CASE WHEN closing_balance > 0 THEN closing_balance ELSE 0 END)
                + SUM(CASE WHEN closing_balance < 0 THEN -closing_balance ELSE 0 END)) DESC
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
    _require_reader()
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
    # Orders stay VISIBLE as rows — "how much is on order" is a real question
    # — but they are commitments, not postings, so they must not inflate the
    # accounting total.
    return {
        "rows": rows,
        "grand_total": round(sum(flt(r["total"]) for r in rows
                                 if r["voucher_type"] not in ORDER_TYPES), 2),
        "on_order_total": round(sum(flt(r["total"]) for r in rows
                                    if r["voucher_type"] in ORDER_TYPES), 2),
        "note": ("grand_total excludes Sales/Purchase Orders (commitments, "
                 "not postings); their value is reported as on_order_total."),
    }


@frappe.whitelist(methods=["GET"])
def group_summary(company=None, root=None, limit=200):
    """
    Ledger groups with their totals, mirroring Tally's Group Summary.

    Rolls up by the RESOLVED root group, so sub-groups such as "AGENT RK" are
    counted inside "Sundry Debtors" exactly as Tally reports them. Use this to
    reconcile against Tally's own Group Summary screen.
    """
    _require_reader()
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
        HAVING SUM(CASE WHEN closing_balance > 0 THEN closing_balance ELSE 0 END) <> 0 OR SUM(CASE WHEN closing_balance < 0 THEN -closing_balance ELSE 0 END) <> 0
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
def search_items(query=None, company=None, group=None, limit=25):
    """
    Find products by partial name, part number or HSN code.

    Resolves an informal name ("the thermal vest", "402") to the exact item
    name before any item-level question.
    """
    _require_reader()
    if not query:
        frappe.throw("`query` is required")
    conds = ["(item_name LIKE %(q)s OR part_no LIKE %(q)s OR hsn_code LIKE %(q)s)"]
    params = {"q": f"%{query}%", "limit": _limit(limit, 25)}
    _company_clause(company, conds, params)
    if group:
        conds.append("stock_group = %(group)s")
        params["group"] = group
    rows = frappe.db.sql(
        f"""
        SELECT company, item_name, stock_group, part_no, base_units,
               closing_qty, closing_qty_unit, closing_qty_raw,
               closing_value, hsn_code, gst_rate
        FROM `tabTally Stock Item`
        WHERE {' AND '.join(conds)}
        ORDER BY closing_value DESC
        LIMIT %(limit)s
        """,
        params, as_dict=True,
    )
    return {"count": len(rows),
            "distinct_names": sorted({r["item_name"] for r in rows}),
            "rows": rows}


@frappe.whitelist(methods=["GET"])
def stock_summary(company=None, group=None, by="group", limit=100):
    """
    Closing stock by product family or by item — what we are holding.

    Value is authoritative; quantity is only meaningful within one unit, so
    quantities are NOT summed across items measured differently.
    """
    _require_reader()
    conds = ["closing_value <> 0"]
    params = {"limit": _limit(limit, 100)}
    _company_clause(company, conds, params)
    if group:
        conds.append("stock_group = %(group)s")
        params["group"] = group
    col = "stock_group" if by == "group" else "item_name"
    rows = frappe.db.sql(
        f"""
        SELECT company, {col} AS name, COUNT(*) AS items,
               SUM(closing_value) AS value,
               COUNT(DISTINCT closing_qty_unit) AS unit_variants,
               MIN(closing_qty_unit) AS unit,
               SUM(closing_qty) AS qty
        FROM `tabTally Stock Item`
        WHERE {' AND '.join(conds)}
        GROUP BY company, {col}
        ORDER BY value DESC
        LIMIT %(limit)s
        """,
        params, as_dict=True,
    )
    for r in rows:
        # A summed quantity is nonsense when the rows use different units.
        if int(r.get("unit_variants") or 0) > 1:
            r["qty"] = None
            r["qty_note"] = "mixed units — not summable"
    return {
        "by": by,
        "count": len(rows),
        "total_value": round(sum(flt(r["value"]) for r in rows), 2),
        "note": ("Stock value is as at the last sync. Never add stock across "
                 "the financial-year company files — the same goods appear in "
                 "each."),
        "rows": rows,
    }


@frappe.whitelist(methods=["GET"])
def hsn_gaps(company=None, limit=200):
    """
    Items carrying stock but no HSN code — a live GST filing exposure.

    Worth surfacing without being asked: an item invoiced without an HSN code
    is a compliance problem long before anyone notices it on a return.
    """
    _require_reader()
    conds = ["(hsn_code IS NULL OR hsn_code = '')", "closing_value <> 0"]
    params = {"limit": _limit(limit, 200)}
    _company_clause(company, conds, params)
    rows = frappe.db.sql(
        f"""
        SELECT company, item_name, stock_group, closing_qty_raw, closing_value
        FROM `tabTally Stock Item`
        WHERE {' AND '.join(conds)}
        ORDER BY closing_value DESC
        LIMIT %(limit)s
        """,
        params, as_dict=True,
    )
    return {"count": len(rows),
            "value_at_risk": round(sum(flt(r["closing_value"]) for r in rows), 2),
            "note": "Items holding stock with no HSN code recorded.",
            "rows": rows}


@frappe.whitelist(methods=["GET"])
def ageing(company=None, party=None, group=None, min_days=None, min_amount=0,
           party_type="receivable", limit=200):
    """
    Unpaid bills, oldest first, with an ageing bucket on each.

    This is the collections list: which invoice, how much is still unpaid, how
    many days past its due date. `min_days=60` gives everything more than two
    months late; `group="AGENT RK"` scopes it to one agent's book.
    """
    _require_reader()
    groups = RECEIVABLE_GROUPS if party_type == "receivable" else PAYABLE_GROUPS
    sign = ">" if party_type == "receivable" else "<"

    conds = [f"outstanding {sign} 0", "is_advance = 0",
             "ABS(outstanding) >= %(min_amount)s"]
    params = {"min_amount": flt(min_amount), "limit": _limit(limit, 200),
              "groups": groups}
    _company_clause(company, conds, params)
    if party:
        conds.append("party = %(party)s")
        params["party"] = party
    if group:
        conds.append("primary_group = %(group)s")
        params["group"] = group
    else:
        conds.append("primary_group IN %(groups)s")
    if min_days is not None:
        conds.append("overdue_days >= %(min_days)s")
        params["min_days"] = int(min_days)

    rows = frappe.db.sql(
        f"""
        SELECT company, party, bill_ref, bill_date, due_date, overdue_days,
               credit_period, outstanding, primary_group, gstin
        FROM `tabTally Bill`
        WHERE {' AND '.join(conds)}
        ORDER BY overdue_days DESC, ABS(outstanding) DESC
        LIMIT %(limit)s
        """,
        params, as_dict=True,
    )
    buckets: dict = {}
    for r in rows:
        r["bill_date"] = str(r["bill_date"] or "")
        r["due_date"] = str(r["due_date"] or "")
        r["bucket"] = _bucket(int(r["overdue_days"] or 0))
        b = buckets.setdefault(r["bucket"], {"bills": 0, "amount": 0.0})
        b["bills"] += 1
        b["amount"] = round(b["amount"] + abs(flt(r["outstanding"])), 2)

    return {
        "party_type": party_type,
        "count": len(rows),
        "total_outstanding": round(sum(abs(flt(r["outstanding"])) for r in rows), 2),
        "buckets": buckets,
        "note": ("Outstanding is what remains unpaid. Overdue days count from "
                 "the due date (bill date plus credit period); negative means "
                 "not yet due."),
        "rows": rows,
    }


@frappe.whitelist(methods=["GET"])
def ageing_summary(company=None, by="group", party_type="receivable", limit=100):
    """
    Ageing totals rolled up by agent group or by party — who to chase first.

    `by="group"` answers "which agent's book is worst"; `by="party"` ranks
    individual customers by how much of their debt is genuinely overdue.
    """
    _require_reader()
    groups = RECEIVABLE_GROUPS if party_type == "receivable" else PAYABLE_GROUPS
    sign = ">" if party_type == "receivable" else "<"
    col = "primary_group" if by == "group" else "party"

    conds = [f"outstanding {sign} 0", "is_advance = 0",
             "primary_group IN %(groups)s"]
    params = {"groups": groups, "limit": _limit(limit, 100)}
    _company_clause(company, conds, params)

    rows = frappe.db.sql(
        f"""
        SELECT {col} AS name, company,
               COUNT(*) AS bills,
               SUM(ABS(outstanding)) AS total,
               SUM(CASE WHEN overdue_days > 0 THEN ABS(outstanding) ELSE 0 END) AS overdue,
               SUM(CASE WHEN overdue_days > 90 THEN ABS(outstanding) ELSE 0 END) AS over_90,
               MAX(overdue_days) AS worst_days
        FROM `tabTally Bill`
        WHERE {' AND '.join(conds)}
        GROUP BY {col}, company
        ORDER BY overdue DESC
        LIMIT %(limit)s
        """,
        params, as_dict=True,
    )
    for r in rows:
        r["overdue_pct"] = (round(100 * flt(r["overdue"]) / flt(r["total"]), 1)
                            if flt(r["total"]) else 0.0)
    return {
        "by": by,
        "count": len(rows),
        "total_outstanding": round(sum(flt(r["total"]) for r in rows), 2),
        "total_overdue": round(sum(flt(r["overdue"]) for r in rows), 2),
        "rows": rows,
    }


@frappe.whitelist(methods=["GET"])
def bills_due_between(from_date=None, to_date=None, company=None,
                      party_type="receivable", limit=300):
    """
    Bills falling due in a date window — "what is due in March".

    Matches on DUE date, not bill date, which is what a collections calendar
    actually needs.
    """
    _require_reader()
    groups = RECEIVABLE_GROUPS if party_type == "receivable" else PAYABLE_GROUPS
    sign = ">" if party_type == "receivable" else "<"

    conds = [f"outstanding {sign} 0", "is_advance = 0",
             "primary_group IN %(groups)s", "due_date IS NOT NULL"]
    params = {"groups": groups, "limit": _limit(limit, 300)}
    _company_clause(company, conds, params)
    if from_date:
        conds.append("due_date >= %(from_date)s")
        params["from_date"] = getdate(from_date)
    if to_date:
        conds.append("due_date <= %(to_date)s")
        params["to_date"] = getdate(to_date)

    rows = frappe.db.sql(
        f"""
        SELECT company, party, bill_ref, bill_date, due_date, overdue_days,
               outstanding, primary_group
        FROM `tabTally Bill`
        WHERE {' AND '.join(conds)}
        ORDER BY due_date ASC, ABS(outstanding) DESC
        LIMIT %(limit)s
        """,
        params, as_dict=True,
    )
    for r in rows:
        r["bill_date"] = str(r["bill_date"] or "")
        r["due_date"] = str(r["due_date"] or "")
    parties = {r["party"] for r in rows}
    return {
        "period": {"from": str(from_date or ""), "to": str(to_date or "")},
        "count": len(rows),
        "party_count": len(parties),
        "total": round(sum(abs(flt(r["outstanding"])) for r in rows), 2),
        "rows": rows,
    }


@frappe.whitelist(methods=["GET"])
def search_ledgers(query=None, limit=25, company=None):
    """Fuzzy ledger lookup — lets Claude resolve 'Acme' to the real name."""
    _require_reader()
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
    _require_reader()
    # Orders excluded: they post nothing, so they cannot unbalance the books —
    # any non-zero net on an order row is noise that buries the real breaks.
    conds = ["v.is_cancelled = 0", _NOT_ORDER_V]
    params: dict[str, Any] = {"tol": flt(tolerance), "limit": _limit(limit, 100),
                              "order_types": ORDER_TYPES}
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
        -- A voucher with no ledger entries is only suspicious if it claims to
        -- be worth something. Stock Journals, Stock Transfers and Job Work
        -- vouchers move goods rather than money: they carry inventory entries,
        -- no ledger entries, and an amount of zero. Flagging those buried the
        -- real hits 6:1 -- 1,345 of 1,623 rows in one April -- which teaches
        -- the reader to ignore this check, the one outcome worse than the bug.
        HAVING ABS(COALESCE(SUM(e.amount), 0)) > %(tol)s
            OR (COUNT(e.name) = 0 AND ABS(COALESCE(v.amount, 0)) > %(tol)s)
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
        "note": ("Entries should net to zero. Non-zero rows indicate a sync or "
                 "export problem. Inventory-only vouchers (Stock Journal, Stock "
                 "Transfer, Job Work) carry no ledger entries by design and are "
                 "not counted. Raise `tolerance` to 1 to hide sub-rupee "
                 "round-off residuals and see only material breaks."),
        "rows": rows,
    }


@frappe.whitelist(methods=["GET"])
def recent_failures(limit=5):
    """
    The last few failed sync runs, with the error each reported.

    Exists so a diagnosis does not require the write-capable key: when a sync
    fails, the read-only side can see WHY without escalating privileges.
    """
    _require_reader()
    rows = frappe.db.sql(
        """
        SELECT name, sync_time, company, status, ledgers_synced,
               vouchers_synced, date_range, detail
        FROM `tabTally Sync Log`
        WHERE status IN ('Failed', 'Partial')
        ORDER BY sync_time DESC
        LIMIT %(limit)s
        """,
        {"limit": _limit(limit, 5)}, as_dict=True,
    )
    out = []
    for r in rows:
        error = ""
        try:
            error = (json.loads(r.detail or "{}") or {}).get("error", "")
        except ValueError:
            error = (r.detail or "")[:500]
        out.append({
            "sync_time": str(r.sync_time),
            "company": r.company,
            "status": r.status,
            "date_range": r.date_range,
            "ledgers_synced": r.ledgers_synced,
            "vouchers_synced": r.vouchers_synced,
            "error": str(error)[:800],
        })
    return {"count": len(out), "rows": out}


@frappe.whitelist(methods=["GET"])
def sync_health():
    """Is the mirror fresh and complete? Claude should check this first."""
    _require_reader()
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
