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
from frappe.utils import (flt, get_datetime, getdate, now_datetime, add_days,
                          add_to_date)

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

# The agent syncs every 15 minutes, so anything past a couple of hours means
# roughly eight consecutive runs produced nothing. The old threshold was 24
# HOURS — 96 missed runs — which is how a dead scheduled task twice went a
# full day unnoticed while sync_health cheerfully reported is_fresh: true.
# Kept above one hour so a slow run, a reboot or a busy-Tally spell does not
# raise a false alarm.
STALE_AFTER_HOURS = 2

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

# Hygiene: a voucher counts only if it was actually POSTED. Cancelled ones are
# reversed-out entries; "optional" ones are Tally's drafts — entered, visible,
# and deliberately not posted to any ledger. Both carry a party and an amount
# and are indistinguishable from real entries in an export.
#
# These filters were written long before they could work. Until 2026-08-15 the
# voucher FETCH omitted IsCancelled and IsOptional, so every mirrored row
# landed `is_cancelled = 0, is_optional = 0` and the conditions matched
# everything. They only start filtering once the agent runs the
# `filter_dotted_rich` variant AND the affected date range has been re-synced.
_POSTED = "is_cancelled = 0 AND is_optional = 0"
_POSTED_V = "v.is_cancelled = 0 AND v.is_optional = 0"

# Ledger groups that RESTATE other groups rather than posting alongside them.
# Tally files Profit & Loss A/c under the reserved primary group "Primary".
BALANCING_GROUPS = ("Primary",)

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

        # Distributor-facing master fields, shipped by newer agents. Only
        # written when the payload actually carries them: an older agent that
        # never fetched these must not blank what a newer run stored.
        _DIST_FIELDS = ("credit_limit", "credit_days", "credit_period",
                        "price_level", "mobile", "address", "mailing_name",
                        "state", "pincode", "country", "gst_registration_type",
                        "alias", "agent", "agent_source")
        if any(k in row for k in _DIST_FIELDS):
            values.update({
                "credit_limit": flt(row.get("credit_limit")),
                "credit_days": int(row.get("credit_days") or 0),
                "credit_period": row.get("credit_period") or "",
                "price_level": row.get("price_level") or "",
                "mobile": row.get("mobile") or "",
                "address": row.get("address") or "",
                "mailing_name": row.get("mailing_name") or "",
                "state": row.get("state") or "",
                "pincode": row.get("pincode") or "",
                "country": row.get("country") or "",
                "gst_registration_type": row.get("gst_registration_type") or "",
                "alias": row.get("alias") or "",
                "agent": row.get("agent") or "",
                "agent_source": row.get("agent_source") or "",
            })

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
            "reference": row.get("reference") or "",
            "reference_date": row.get("reference_date") or None,
            "amount": flt(row.get("amount")),
            "is_cancelled": 1 if row.get("is_cancelled") else 0,
            "is_optional": 1 if row.get("is_optional") else 0,
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

    # Never clear a company's bills for an empty payload. The snapshot is
    # destructive by design, so an upstream fetch that returns nothing would
    # otherwise wipe a good mirror and report success.
    if int(replace or 0) and company and rows:
        frappe.db.delete("Tally Bill", {"company": company})

    # Party groups and GSTIN are denormalised so ageing can be sliced without
    # a join per row. BOTH groups are needed and they are not interchangeable:
    # `primary_group` is the root ("Sundry Debtors") that separates receivable
    # from payable, while `parent_group` is the immediate group ("AGENT RK")
    # that ageing filters on by agent.
    parties = {}
    if company:
        for l in frappe.get_all(
            "Tally Ledger", filters={"company": company},
            fields=["ledger_name", "parent_group", "primary_group", "gstin"],
            limit_page_length=0,
        ):
            parties[l.ledger_name] = (l.parent_group, l.primary_group, l.gstin)

    created = 0
    errors: list = []
    unmatched: set = set()
    for i, row in enumerate(rows):
        party = (row.get("party") or "").strip()
        ref = (row.get("name") or "").strip()
        if not party or not ref:
            continue
        savepoint = f"bill_{i}"
        try:
            frappe.db.savepoint(savepoint)
            parent, grp, gstin = parties.get(party, ("", "", ""))
            if not grp:
                unmatched.add(party)
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
                "parent_group": parent or "",
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
            if len(errors) < 50:
                errors.append({"bill": f"{party} / {ref}"[:140],
                               "error": f"{type(exc).__name__}: {exc}"[:300]})
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception as rb:
                # The savepoint is gone, so the whole transaction unwinds and
                # every bill inserted in this batch is lost. Record it before
                # leaving — a silent break here reported success on zero rows.
                frappe.db.rollback()
                errors.append({
                    "bill": "(batch aborted)",
                    "error": f"Rollback to savepoint failed after {created} "
                             f"insert(s); the whole batch was discarded: "
                             f"{type(rb).__name__}: {rb}"[:300],
                })
                created = 0
                break

    frappe.db.commit()
    out = {"created": created, "received": len(rows)}
    if errors:
        out["failed"] = len(errors)
        out["errors"] = errors
    if unmatched:
        # A bill whose party is missing from Tally Ledger gets no group, and
        # every ageing query filters on group — so it would be mirrored and
        # then be invisible. Surface it rather than let it vanish.
        out["unmatched_parties"] = sorted(unmatched)[:50]
        out["unmatched_count"] = len(unmatched)
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
        f"""
        SELECT company,
               COUNT(*) AS voucher_count,
               MIN(voucher_date) AS first_voucher,
               MAX(voucher_date) AS last_voucher,
               SUM(amount) AS total_value
        FROM `tabTally Voucher`
        WHERE {_POSTED} AND company != ''
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
        f"""
        SELECT v.company, COUNT(*) FROM `tabTally Voucher Entry` e
        INNER JOIN `tabTally Voucher` v ON v.name = e.parent
        WHERE e.ledger = %(n)s AND {_POSTED_V}
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
    conds = ["e.ledger = %(ledger)s", _POSTED_V, _NOT_ORDER_V]
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
    conds = [_POSTED]
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
    # Profit & Loss A/c is a BALANCING FIGURE, not a posting group: it already
    # summarises the Sales, Purchase, Direct and Indirect ledgers that appear
    # beside it. Counting both double-counts the year's result.
    #
    # Tally files it under the reserved primary group "Primary", which no
    # ordinary ledger uses, so the group name identifies it without matching on
    # an English caption that a renamed ledger could break.
    #
    # Measured on (26-27), 2026-08-15: the headline "trial balance is out by
    # ₹32.6cr" was TWO faults stacked. Most of it was simply summing all nine
    # company files together — scoped to one file the difference was
    # −₹16.41cr, of which ₹15.07cr was this single ledger. Excluding it leaves
    # ~₹1.34cr genuinely unexplained, which is a real but ordinary-sized gap.
    balancing = [r for r in rows if (r.get("group") or "") in BALANCING_GROUPS]
    posting = [r for r in rows if (r.get("group") or "") not in BALANCING_GROUPS]

    total_debit = sum(flt(r.debit) for r in posting)
    total_credit = sum(flt(r.credit) for r in posting)
    out = {
        "company_filter": company or "all companies",
        "note": ("Grouped by company. A trial balance is only meaningful within "
                 "ONE company file — do not sum across years."),
        "rows": rows,
        "total_debit": round(total_debit, 2),
        "total_credit": round(total_credit, 2),
        "difference": round(total_debit - total_credit, 2),
    }
    if balancing:
        # Shown, never hidden — it is a real balance and the user may be
        # looking for it. It is simply not part of the cross-check.
        out["balancing_figures"] = balancing
        out["balancing_note"] = (
            "Profit & Loss A/c (group 'Primary') is listed in `rows` but is "
            "EXCLUDED from total_debit/total_credit/difference: it restates "
            "the revenue and expense ledgers already counted above, so "
            "including it would double-count the year's result."
        )
    return out


@frappe.whitelist(methods=["GET"])
def summary_by_voucher_type(from_date=None, to_date=None, company=None):
    """Volume and value per voucher type — the shape of the period."""
    _require_reader()
    conds = [_POSTED]
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
    total = frappe.db.sql(
        f"SELECT COUNT(*) FROM `tabTally Stock Item` WHERE {' AND '.join(conds)}",
        params,
    )[0][0]
    rows = frappe.db.sql(
        f"""
        SELECT company, item_name, stock_group, part_no, base_units,
               closing_qty, closing_qty_unit, closing_qty_raw,
               closing_value, hsn_code, gst_rate
        FROM `tabTally Stock Item`
        WHERE {' AND '.join(conds)}
        -- Items HOLDING stock first, by magnitude. ABS() is kept deliberately
        -- even though the agent now un-flips the exported value sign (see
        -- fetch_stock_items): genuinely negative stock — 46 items on this
        -- book, issued beyond what was received — is exactly what someone
        -- searching for an item wants to see, not something to sort to the
        -- bottom. Before the sign fix, `closing_value DESC` floated the dead
        -- zero rows to the top and a style with 249 boxes on hand answered
        -- as "0.00 across all variants".
        ORDER BY ABS(closing_qty) DESC, ABS(closing_value) DESC, item_name
        LIMIT %(limit)s
        """,
        params, as_dict=True,
    )
    out = {"count": len(rows),
           "total_matches": total,
           "distinct_names": sorted({r["item_name"] for r in rows}),
           "rows": rows}
    if total > len(rows):
        # A capped list must SAY it is capped: a silently truncated result
        # reads as "this is everything" and produces confidently wrong
        # answers about whatever sorted below the cut.
        out["note"] = (f"Showing {len(rows)} of {total} matching items — "
                       f"narrow the query or raise `limit` to see the rest.")
    return out


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
        # The agent book is the IMMEDIATE group ("AGENT RK"); primary_group is
        # always the root ("Sundry Debtors"), so filtering it by an agent name
        # matched nothing and every agent-scoped ageing came back empty.
        conds.append("parent_group = %(group)s")
        params["group"] = group
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
    # "by group" means by agent book, which is the immediate group. Rolling up
    # on primary_group would put every debtor in one "Sundry Debtors" row.
    col = "parent_group" if by == "group" else "party"

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
    conds = [_POSTED_V, _NOT_ORDER_V]
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
    # Freshness is measured from the last SUCCESS, never from the last row.
    # A task that fails every 15 minutes writes a recent row each time, so
    # keying off `last_log` would report a mirror as fresh while it sat days
    # behind. `state["last_successful_sync"]` already filters status=Success.
    last_success = state.get("last_successful_sync")
    if isinstance(last_success, str):
        last_success = get_datetime(last_success) if last_success else None

    stale_hours = None
    if last_success:
        stale_hours = round(
            (now_datetime() - last_success).total_seconds() / 3600, 1)

    # Time since the last ATTEMPT of any status. Read together with
    # stale_hours this separates the two failure modes: attempts recent but
    # stale_hours large means the task runs and Tally rejects it; BOTH large
    # means the task is not running at all and no error will ever be logged
    # — the silent death that leaves no trace anywhere.
    attempt_hours = None
    if last_log and last_log.get("sync_time"):
        attempt_hours = round(
            (now_datetime() - last_log["sync_time"]).total_seconds() / 3600, 1)

    recent_failures = frappe.db.count(
        "Tally Sync Log",
        {"status": "Failed", "sync_time": [">", add_days(now_datetime(), -1)]},
    )

    return {
        **state,
        "last_sync_status": last_log.get("status") if last_log else None,
        "last_sync_time": str(last_log.get("sync_time")) if last_log else None,
        "hours_since_last_sync": stale_hours,
        "hours_since_last_attempt": attempt_hours,
        "failures_last_24h": recent_failures,
        "is_fresh": stale_hours is not None and stale_hours < STALE_AFTER_HOURS,
        "stale_after_hours": STALE_AFTER_HOURS,
        "diagnosis": _freshness_diagnosis(stale_hours, attempt_hours),
    }


def _freshness_diagnosis(stale_hours, attempt_hours):
    """One line naming WHICH way the sync is broken, or None if it is fine."""
    if stale_hours is None:
        return "No successful sync has ever been recorded."
    if stale_hours < STALE_AFTER_HOURS:
        return None
    if attempt_hours is not None and attempt_hours < STALE_AFTER_HOURS:
        return (f"The agent is running but not succeeding: last attempt "
                f"{attempt_hours}h ago, last SUCCESS {stale_hours}h ago. "
                f"Call recent_failures for the error.")
    return (f"The agent has not run at all for {attempt_hours}h — no success "
            f"and no failure logged. A scheduled task that dies before its "
            f"logging starts leaves no trace here, so check the task on the "
            f"Tally server itself; do not read the empty failure list as "
            f"health.")


def check_sync_freshness():
    """Hourly scheduled job — logs a warning if the agent has gone quiet."""
    health = sync_health()
    if not health.get("is_fresh"):
        frappe.log_error(
            title=f"Tally sync is stale — {health.get('diagnosis') or ''}"[:140],
            message=json.dumps(health, indent=2, default=str),
        )


# ===========================================================================
# Sales Order queue (chat -> queue -> importer -> Tally).  The ONLY write
# path toward Tally, and it carries a rate per line — this Tally build
# REFUSES zero-value vouchers, so an unpriced row could never import.
# No MRP, ever.
# ===========================================================================
#
# A queue row is a REQUEST, not a posting: nothing reaches Tally until the
# LAN-side importer picks the row up, re-validates it, and sends the XML —
# then reports back through mark_order_result() so every state a row passes
# through is visible here, failures included.

ORDER_STATUSES = ("Pending", "Importing", "Imported", "Failed", "Cancelled")

# The full lifecycle, enforced server-side. "Importing" is a CLAIM: the
# importer sets it just before talking to Tally, so a crash mid-import leaves
# the row parked in Importing — surfaced as `stuck` by pending_sales_orders()
# — rather than eligible for a second attempt that could double-post the
# order into the live books. Imported and Cancelled are terminal.
ORDER_TRANSITIONS = {
    "Pending": ("Importing", "Cancelled"),
    "Importing": ("Imported", "Failed"),
    "Failed": ("Pending",),  # retry, once the cause is fixed
}


def _suggest(doctype: str, name_field: str, company: str, query: str) -> list:
    """Up to 5 near-matches within one company file, for a helpful throw."""
    rows = frappe.get_all(
        doctype,
        filters={"company": company, name_field: ["like", f"%{query}%"]},
        fields=[name_field], limit=5,
    )
    return sorted({r[name_field] for r in rows})


@frappe.whitelist(methods=["POST"])
def queue_sales_order(order=None):
    """
    Queue ONE priced Sales Order for import into Tally.

    Deliberate exception: gated on _require_reader, not _require_writer. The
    read key may CREATE queue rows only, because a queued row is inert — a
    Sales Order posts to no ledger, and nothing reaches Tally until the
    server-side importer independently validates and imports it. Handing the
    chat side the writer key just to enqueue would also hand it the
    mirror-ingestion endpoints, a far larger surface than this one insert.

    Validation happens HERE, before the row exists: Tally silently
    auto-creates any master it does not recognise on import, so a typo'd
    party name would become a brand-new ledger in the live books. Party and
    item names must therefore exact-match existing masters in the target
    company file — no fuzzy matching, ever.
    """
    _require_reader()

    # Same semantics as _parse_payload, for a dict: accept a JSON string
    # (form-encoded) or a real object (JSON body).
    if isinstance(order, str):
        order = json.loads(order)
    if not isinstance(order, dict):
        frappe.throw("`order` must be a JSON object")

    # Voucher type is hard-whitelisted. The queue only ever carries Sales
    # Orders; refuse anything else at the door rather than trusting the
    # importer to notice downstream.
    vtype = (order.get("voucher_type") or "Sales Order").strip()
    if vtype != "Sales Order":
        frappe.throw(f"Refused: only 'Sales Order' may be queued, got '{vtype}'.")

    order_key = (order.get("order_key") or "").strip()
    company = (order.get("company") or "").strip()
    party = (order.get("party_ledger") or "").strip()
    lines = order.get("lines") or []
    if not order_key:
        frappe.throw("`order_key` is required — it is the idempotency key.")
    if not company or not party or not lines:
        frappe.throw("`company`, `party_ledger` and `lines` are all required.")

    # Idempotency BEFORE validation: a re-send of an already-queued order
    # must report its current state even if a master was renamed since the
    # first send. The docname IS the order_key, so this lookup is exact.
    existing_status = frappe.db.get_value("Tally Order Queue", order_key, "status")
    if existing_status:
        return {"queued": False, "name": order_key, "status": existing_status}

    if not frappe.db.exists("Tally Ledger",
                            {"company": company, "ledger_name": party}):
        near = _suggest("Tally Ledger", "ledger_name", company, party)
        frappe.throw(
            f"Party '{party}' does not exist in company '{company}'. "
            f"The name must exact-match an existing Tally ledger — Tally "
            f"auto-creates unknown masters on import, which this check "
            f"prevents. "
            + (f"Close matches: {', '.join(near)}" if near
               else "No similar ledger names found.")
        )

    for i, line in enumerate(lines, start=1):
        item = (line.get("item_name") or "").strip()
        if not item:
            frappe.throw(f"Line {i}: `item_name` is required.")
        if not frappe.db.exists("Tally Stock Item",
                                {"company": company, "item_name": item}):
            near = _suggest("Tally Stock Item", "item_name", company, item)
            frappe.throw(
                f"Line {i}: item '{item}' does not exist in company "
                f"'{company}'. The name must exact-match an existing Tally "
                f"stock item. "
                + (f"Close matches: {', '.join(near)}" if near
                   else "No similar item names found.")
            )
        if flt(line.get("qty")) <= 0:
            frappe.throw(f"Line {i} ({item}): qty must be greater than zero.")
        # A rate is what makes the voucher importable at all: this Tally
        # build refuses zero-value vouchers (proven live 2026-08-13), so a
        # rateless row would queue happily and then fail at the last step.
        # Refused here, where the caller can still do something about it.
        if flt(line.get("rate")) <= 0:
            frappe.throw(
                f"Line {i} ({item}): a positive `rate` is required — Tally "
                f"refuses zero-value vouchers, so an unpriced order cannot "
                f"be imported."
            )
        if flt(line.get("discount")) < 0 or flt(line.get("discount")) >= 100:
            frappe.throw(
                f"Line {i} ({item}): discount {line.get('discount')!r} is "
                f"not a sane percentage."
            )

    doc = frappe.get_doc({
        "doctype": "Tally Order Queue",
        "order_key": order_key,
        "company": company,
        "party_ledger": party,
        "order_no": (order.get("order_no") or "").strip(),
        "order_date": getdate(order.get("order_date")) if order.get("order_date") else None,
        "status": "Pending",
        "source": (order.get("source") or "claude-chat").strip(),
        "queued_at": now_datetime(),
    })
    for line in lines:
        doc.append("lines", {
            "item_name": (line.get("item_name") or "").strip(),
            # Batch names are sizes and often arrive numeric ("28"): keep
            # them as text, exactly as Tally stores batch names.
            "size_batch": str(line.get("size_batch") or "").strip(),
            "qty": flt(line.get("qty")),
            "unit": (line.get("unit") or "Doz").strip(),
            "rate": flt(line.get("rate")),
            # 50 is what every priced line in this book carries; Tally records
            # only the first step of the chain.
            "discount": flt(line.get("discount") or 50),
            "due_days": int(line.get("due_days") or 0),
        })
    doc.name = order_key
    try:
        doc.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        # Two concurrent sends of the same order_key: the second loses the
        # race on the primary key. Report the surviving row instead of
        # erroring — to the caller this is exactly the idempotent case.
        frappe.db.rollback()
        return {"queued": False, "name": order_key,
                "status": frappe.db.get_value("Tally Order Queue", order_key, "status")}
    frappe.db.commit()
    return {"queued": True, "name": doc.name, "lines": len(doc.lines)}


@frappe.whitelist(methods=["GET"])
def pending_sales_orders(limit=20):
    """
    The import backlog, oldest first — what the importer will pick up next.

    Also counts Importing rows older than 30 minutes as `stuck_importing`.
    Importing is a claim taken just before talking to Tally, so a row parked
    there for half an hour means the importer died mid-import. Those rows
    are deliberately NOT retried automatically — Tally may or may not have
    accepted the voucher — so a human decides, and this count is how anyone
    finds out there is something to decide.
    """
    _require_reader()
    rows = frappe.get_all(
        "Tally Order Queue",
        filters={"status": "Pending"},
        fields=["name", "order_key", "company", "party_ledger", "order_no",
                "order_date", "source", "queued_at"],
        order_by="queued_at asc, creation asc",
        limit=_limit(limit, 20),
    )
    for r in rows:
        r["order_date"] = str(r["order_date"] or "")
        r["queued_at"] = str(r["queued_at"] or "")
        r["lines"] = frappe.get_all(
            "Tally Order Queue Line",
            filters={"parent": r["name"], "parenttype": "Tally Order Queue"},
            fields=["item_name", "size_batch", "qty", "unit", "rate",
                    "discount", "due_days"],
            order_by="idx asc",
            limit_page_length=0,
        )
    stuck = frappe.db.count(
        "Tally Order Queue",
        {"status": "Importing",
         "modified": ["<", add_to_date(now_datetime(), minutes=-30)]},
    )
    return {"count": len(rows), "stuck_importing": stuck, "rows": rows}


@frappe.whitelist(methods=["POST"])
def mark_order_result(order_key=None, status=None, tally_vch_number=None, error=None):
    """
    Advance one queued order through its lifecycle. Importer-only.

    Only the transitions in ORDER_TRANSITIONS are accepted; anything else is
    rejected with the current state spelled out. This is what makes the
    idempotency story hold end to end: a second importer instance, a replayed
    request, or a manual poke through the API cannot move a row backwards or
    re-open a terminal state — Pending -> Importing can only happen once, so
    an order can only ever be sent to Tally once.
    """
    _require_writer()
    if not order_key:
        frappe.throw("`order_key` is required")
    if status not in ORDER_STATUSES:
        frappe.throw(f"`status` must be one of: {', '.join(ORDER_STATUSES)}")

    # Row lock so the read and the write are one atomic step: without it two
    # racing callers could both see Pending and both claim Importing.
    current = frappe.db.get_value("Tally Order Queue", order_key, "status",
                                  for_update=True)
    if current is None:
        frappe.throw(f"No queued order '{order_key}'.")
    allowed = ORDER_TRANSITIONS.get(current, ())
    if status not in allowed:
        frappe.throw(
            f"Invalid transition {current} -> {status} for '{order_key}'. "
            + (f"From {current} the allowed next states are: {', '.join(allowed)}."
               if allowed else f"{current} is a terminal state.")
        )

    values = {"status": status}
    if status == "Imported":
        values["imported_at"] = now_datetime()
    if tally_vch_number:
        values["tally_vch_number"] = str(tally_vch_number).strip()
    if error:
        # Tally's LINEERROR text can run to pages; the first 500 chars carry
        # the actual reason.
        values["error"] = str(error)[:500]
    elif status == "Pending":
        # Failed -> Pending retry: clear the stale error so the row does not
        # keep reading as failed while it waits for another attempt.
        values["error"] = ""
    frappe.db.set_value("Tally Order Queue", order_key, values)
    frappe.db.commit()
    return {"ok": True, "name": order_key, "from": current, "status": status}


# ===========================================================================
# Distributor mirror ingestion (sync agent -> Frappe).
#
# Sales orders, invoices, delivery notes and receipts follow the voucher
# pattern: keyed on (company, guid), AlterID short-circuit, savepoint per
# row. Child lines are replaced wholesale on update — Tally re-exports the
# complete voucher, so diffing lines would only invent a place for state to
# rot.
# ===========================================================================

def _party_groups(company: str) -> dict:
    """party name -> immediate group, for denormalising onto mirrored docs."""
    if not company:
        return {}
    return dict(frappe.db.sql(
        "SELECT ledger_name, parent_group FROM `tabTally Ledger` "
        "WHERE company = %(c)s", {"c": company},
    ))


def _upsert_mirror_docs(doctype: str, rows: list, stamp, fields_fn,
                        lines_field: str = "", label_key: str = "voucher_number"):
    """
    Shared engine for the four voucher-shaped mirrors.

    fields_fn(row, company, groups) -> (fields dict, lines list). Returns the
    usual counters plus the set of (company, order_no) pairs whose fulfilment
    must be recomputed — the caller decides what to do with them.
    """
    created = updated = skipped = 0
    errors: list = []
    groups_cache: dict = {}
    touched: set = set()

    for i, row in enumerate(rows):
      guid = (row.get("guid") or "").strip()
      company = (row.get("company") or "").strip()
      if not guid or not company:
          continue
      savepoint = f"dm_{i}"
      try:
        frappe.db.savepoint(savepoint)
        if company not in groups_cache:
            groups_cache[company] = _party_groups(company)
        alter_id = row.get("alter_id") or ""
        existing = frappe.db.get_value(
            doctype, {"guid": guid, "company": company},
            ["name", "alter_id"], as_dict=True,
        )
        if existing and alter_id and existing.alter_id == alter_id:
            skipped += 1
            continue

        fields, lines = fields_fn(row, company, groups_cache[company])
        fields["last_synced"] = stamp

        if existing:
            doc = frappe.get_doc(doctype, existing.name)
            doc.update(fields)
            if lines_field:
                doc.set(lines_field, [])
        else:
            doc = frappe.get_doc({"doctype": doctype, "guid": guid, **fields})
            doc.name = _docname(company, guid, guid)
        if lines_field:
            for line in lines:
                doc.append(lines_field, line)

        doc.flags.ignore_permissions = True
        if existing:
            doc.save(ignore_permissions=True)
            updated += 1
        else:
            doc.insert(ignore_permissions=True)
            created += 1

        # Any order number this document names needs its fulfilment redone.
        for line in lines or []:
            if line.get("order_no"):
                touched.add((company, line["order_no"]))
        for key in ("voucher_number", "reference", "order_ref"):
            if doctype == "Tally Sales Order" and fields.get(key):
                touched.add((company, fields[key]))
        frappe.db.release_savepoint(savepoint)
      except Exception as exc:
        try:
            frappe.db.rollback(save_point=savepoint)
        except Exception:
            frappe.db.rollback()
            errors.append({"doc": guid[:140],
                           "error": f"{type(exc).__name__}: {exc}"[:300]})
            errors.append({"doc": "(batch stopped)",
                           "error": "transaction was rolled back by the database; "
                                    "remaining rows in this batch were not attempted"})
            break
        if len(errors) < 50:
            errors.append({
                "doc": f"{row.get(label_key) or ''} / {row.get('party') or ''}".strip()[:140],
                "company": company[:140],
                "error": f"{type(exc).__name__}: {exc}"[:300],
            })

    out = {"created": created, "updated": updated, "unchanged": skipped}
    if errors:
        out["failed"] = len(errors)
        out["errors"] = errors
    return out, touched


def _date_or_none(value):
    return getdate(value) if value else None


def _so_fields(row, company, groups):
    party = (row.get("party") or "").strip()
    ref = (row.get("reference") or "").strip()
    narration = row.get("narration") or ""

    # Join back to the queue: the importer writes the order_key into BOTH the
    # voucher reference and the narration ("... via Claude (<key>)"), so
    # either match links a portal-placed order to its Tally voucher. A
    # hand-punched order matches neither and stays unlinked, correctly.
    order_key = queue_ref = ""
    for candidate in (ref, _key_from_narration(narration)):
        if candidate and frappe.db.exists("Tally Order Queue", candidate):
            order_key = queue_ref = candidate
            break

    fields = {
        "company": company,
        "voucher_number": row.get("voucher_number") or "",
        "voucher_date": _date_or_none(row.get("date")),
        "party": party,
        "party_group": groups.get(party, ""),
        "reference": ref,
        "narration": narration,
        "amount": flt(row.get("amount")),
        "is_cancelled": 1 if row.get("is_cancelled") else 0,
        "is_optional": 1 if row.get("is_optional") else 0,
        "order_status": "Cancelled" if row.get("is_cancelled") else "Open",
        "order_key": order_key,
        "queue_ref": queue_ref,
        "alter_id": row.get("alter_id") or "",
    }
    lines = [{
        "item_name": (l.get("item_name") or "").strip(),
        "size_batch": str(l.get("size_batch") or "").strip(),
        "godown": l.get("godown") or "",
        "qty": flt(l.get("qty")),
        "unit": l.get("unit") or "",
        "billed_qty": flt(l.get("billed_qty")),
        "rate": flt(l.get("rate")),
        "rate_unit": l.get("rate_unit") or "",
        "discount": flt(l.get("discount")),
        "discount2": flt(l.get("discount2")),
        "amount": flt(l.get("amount")),
        "due_date": _date_or_none(l.get("due_date")),
        "order_no": l.get("order_no") or "",
        "preclosed_qty": flt(l.get("preclosed_qty")),
    } for l in (row.get("lines") or []) if l.get("item_name")]
    return fields, lines


_NARRATION_KEY = None


def _key_from_narration(narration: str) -> str:
    """The importer's '... via Claude (<order_key>).' convention."""
    global _NARRATION_KEY
    if _NARRATION_KEY is None:
        import re
        _NARRATION_KEY = re.compile(r"via Claude \(([^)]+)\)")
    m = _NARRATION_KEY.search(narration or "")
    return m.group(1).strip() if m else ""


@frappe.whitelist(methods=["POST"])
def upsert_sales_orders(orders=None):
    """Mirror Sales Order vouchers, one line per (item, size). Idempotent."""
    _require_writer()
    rows = _parse_payload(orders, "orders")
    stamp = now_datetime()
    out, touched = _upsert_mirror_docs(
        "Tally Sales Order", rows, stamp, _so_fields, lines_field="lines")
    out["fulfilment"] = _recompute_fulfilment(touched)
    frappe.db.commit()
    return out


def _invoice_fields(row, company, groups):
    party = (row.get("party") or "").strip()
    fields = {
        "company": company,
        "invoice_no": row.get("invoice_no") or row.get("voucher_number") or "",
        "voucher_date": _date_or_none(row.get("date")),
        "party": party,
        "party_group": groups.get(party, ""),
        "amount": flt(row.get("amount")),
        "taxable_value": flt(row.get("taxable_value")),
        "cgst": flt(row.get("cgst")),
        "sgst": flt(row.get("sgst")),
        "igst": flt(row.get("igst")),
        "cess": flt(row.get("cess")),
        "round_off": flt(row.get("round_off")),
        "reference": row.get("reference") or "",
        "bill_refs": row.get("bill_refs") or "",
        "is_cancelled": 1 if row.get("is_cancelled") else 0,
        "is_optional": 1 if row.get("is_optional") else 0,
        "narration": row.get("narration") or "",
        # Dispatch details from Tally, when the build exports them. Note that
        # `transport_copy` is deliberately ABSENT here: it is uploaded by the
        # office on the mirrored doc, and a sync update must never blank it.
        "dispatched_through": row.get("dispatched_through") or "",
        "lr_no": row.get("lr_no") or "",
        "destination": row.get("destination") or "",
        "alter_id": row.get("alter_id") or "",
    }
    lines = [{
        "item_name": (l.get("item_name") or "").strip(),
        "size_batch": str(l.get("size_batch") or "").strip(),
        "godown": l.get("godown") or "",
        "qty": flt(l.get("qty")),
        "unit": l.get("unit") or "",
        "rate": flt(l.get("rate")),
        "rate_unit": l.get("rate_unit") or "",
        "discount": flt(l.get("discount")),
        "discount2": flt(l.get("discount2")),
        "amount": flt(l.get("amount")),
        "order_no": l.get("order_no") or "",
        "order_due_date": _date_or_none(l.get("due_date")),
    } for l in (row.get("lines") or []) if l.get("item_name")]
    return fields, lines


@frappe.whitelist(methods=["POST"])
def upsert_invoices(invoices=None):
    """Mirror Sales invoices with GST breakup and lines. Idempotent."""
    _require_writer()
    rows = _parse_payload(invoices, "invoices")
    stamp = now_datetime()
    out, touched = _upsert_mirror_docs(
        "Tally Invoice", rows, stamp, _invoice_fields,
        lines_field="lines", label_key="invoice_no")
    # An invoice against an order changes that order's delivered/pending.
    out["fulfilment"] = _recompute_fulfilment(touched)
    frappe.db.commit()
    return out


def _dn_fields(row, company, groups):
    party = (row.get("party") or "").strip()
    fields = {
        "company": company,
        "voucher_number": row.get("voucher_number") or "",
        "voucher_date": _date_or_none(row.get("date")),
        "party": party,
        "party_group": groups.get(party, ""),
        "order_ref": row.get("order_ref") or row.get("reference") or "",
        "vehicle_no": row.get("vehicle_no") or "",
        "lr_no": row.get("lr_no") or "",
        "dispatched_through": row.get("dispatched_through") or "",
        "destination": row.get("destination") or "",
        "is_cancelled": 1 if row.get("is_cancelled") else 0,
        "narration": row.get("narration") or "",
        "alter_id": row.get("alter_id") or "",
    }
    lines = [{
        "item_name": (l.get("item_name") or "").strip(),
        "size_batch": str(l.get("size_batch") or "").strip(),
        "godown": l.get("godown") or "",
        "qty": flt(l.get("qty")),
        "unit": l.get("unit") or "",
        "rate": flt(l.get("rate")),
        "rate_unit": l.get("rate_unit") or "",
        "amount": flt(l.get("amount")),
        "order_no": l.get("order_no") or "",
        "order_due_date": _date_or_none(l.get("due_date")),
    } for l in (row.get("lines") or []) if l.get("item_name")]
    return fields, lines


@frappe.whitelist(methods=["POST"])
def upsert_delivery_notes(notes=None):
    """Mirror delivery notes, for books that issue them. Idempotent."""
    _require_writer()
    rows = _parse_payload(notes, "notes")
    stamp = now_datetime()
    out, touched = _upsert_mirror_docs(
        "Tally Delivery Note", rows, stamp, _dn_fields, lines_field="lines")
    out["fulfilment"] = _recompute_fulfilment(touched)
    frappe.db.commit()
    return out


def _receipt_fields(row, company, groups):
    party = (row.get("party") or "").strip()
    fields = {
        "company": company,
        "voucher_number": row.get("voucher_number") or "",
        "voucher_date": _date_or_none(row.get("date")),
        "party": party,
        "party_group": groups.get(party, ""),
        "amount": flt(row.get("amount")),
        "mode": row.get("mode") or "",
        "instrument_no": row.get("instrument_no") or "",
        "instrument_date": _date_or_none(row.get("instrument_date")),
        "transaction_type": row.get("transaction_type") or "",
        "narration": row.get("narration") or "",
        "is_cancelled": 1 if row.get("is_cancelled") else 0,
        "alter_id": row.get("alter_id") or "",
    }
    allocations = [{
        "bill_ref": a.get("bill_ref") or "",
        "bill_type": a.get("bill_type") or "",
        "amount": flt(a.get("amount")),
    } for a in (row.get("allocations") or [])]
    return fields, allocations


@frappe.whitelist(methods=["POST"])
def upsert_receipts(receipts=None):
    """Mirror receipts with bill-wise allocations. Idempotent."""
    _require_writer()
    rows = _parse_payload(receipts, "receipts")
    stamp = now_datetime()
    out, _ = _upsert_mirror_docs(
        "Tally Receipt", rows, stamp, _receipt_fields, lines_field="allocations")
    frappe.db.commit()
    return out


@frappe.whitelist(methods=["POST"])
def upsert_stock_batches(batches=None, company=None):
    """
    Merge harvested per-size stock balances.

    NOT a destructive snapshot: each row is the newest voucher-dated balance
    for one (item, size), so an older observation must never overwrite a
    newer one — replays are safe in either direction.
    """
    _require_writer()
    rows = _parse_payload(batches, "batches")
    stamp = now_datetime()
    written = skipped_stale = 0
    errors: list = []

    for i, row in enumerate(rows):
        item = (row.get("item_name") or "").strip()
        size = str(row.get("batch_name") or row.get("size_batch") or "").strip()
        comp = (row.get("company") or company or "").strip()
        if not item or not size or not comp:
            continue
        savepoint = f"sb_{i}"
        try:
            frappe.db.savepoint(savepoint)
            docname = _docname(comp, f"{item}|{size}")
            as_of = row.get("as_of") or None
            existing = frappe.db.get_value(
                "Tally Stock Batch", docname, ["name", "as_of"], as_dict=True)
            if existing and existing.as_of and as_of and str(existing.as_of) > str(as_of):
                skipped_stale += 1
                frappe.db.release_savepoint(savepoint)
                continue
            values = {
                "item_name": item,
                "batch_name": size,
                "company": comp,
                "closing_qty": flt(row.get("closing_qty")),
                "closing_qty_unit": row.get("closing_qty_unit") or "",
                "as_of": getdate(as_of) if as_of else None,
                "source_voucher": row.get("source_voucher") or "",
                "last_synced": stamp,
            }
            if existing:
                frappe.db.set_value("Tally Stock Batch", docname, values,
                                    update_modified=False)
            else:
                doc = frappe.get_doc({"doctype": "Tally Stock Batch", **values})
                doc.name = docname
                doc.insert(ignore_permissions=True)
            written += 1
            frappe.db.release_savepoint(savepoint)
        except Exception as exc:
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                frappe.db.rollback()
                break
            if len(errors) < 50:
                errors.append({"row": f"{item}/{size}"[:140],
                               "error": f"{type(exc).__name__}: {exc}"[:300]})

    frappe.db.commit()
    out = {"written": written, "stale_skipped": skipped_stale}
    if errors:
        out["failed"] = len(errors)
        out["errors"] = errors
    return out


# ---------------------------------------------------------------------------
# Fulfilment: delivered vs pending, computed here and nowhere else
# ---------------------------------------------------------------------------

def _unit_factors(company: str) -> dict:
    """unit -> (base_unit, conversion) for one company, from Tally Unit."""
    rows = frappe.get_all(
        "Tally Unit", filters={"company": company},
        fields=["unit_name", "base_units", "conversion"], limit_page_length=0)
    return {r.unit_name: (r.base_units or "", flt(r.conversion)) for r in rows}


def _to_unit(qty: float, unit: str, target: str, units: dict) -> "float | None":
    """
    Convert qty between units through the compound-unit chain (Box -> Dzn ->
    Pcs). None when no chain connects them — the caller must then refuse to
    compare rather than compare wrongly.
    """
    if not unit or not target or unit == target:
        return qty
    factor, cur = 1.0, unit
    for _ in range(6):
        base, conv = units.get(cur, ("", 0.0))
        if not base or not conv:
            return None
        factor *= conv
        cur = base
        if cur == target:
            return qty * factor
    return None


def _recompute_fulfilment(touched: set) -> dict:
    """
    Recompute delivered/pending per line and the derived stage, for every
    (company, order_no) pair touched by an upsert.

    Delivered quantity is drawn from invoice lines (this book bills what it
    ships) and delivery-note lines where they exist; per line the LARGER of
    the two sources wins, because when both documents cover the same goods,
    summing them would double-count.
    """
    if not touched:
        return {"orders": 0}

    done = 0
    units_cache: dict = {}
    for company, order_no in touched:
        so_name = frappe.db.get_value(
            "Tally Sales Order",
            {"company": company, "voucher_number": order_no}, "name")
        if not so_name:
            continue
        if company not in units_cache:
            units_cache[company] = _unit_factors(company)
        units = units_cache[company]

        delivered: dict = {}       # (item, size) -> {unit: qty}
        value_delivered = 0.0
        for table, parent in (("Tally Invoice Line", "Tally Invoice"),
                              ("Tally Delivery Note Line", "Tally Delivery Note")):
            rows = frappe.db.sql(
                f"""
                SELECT l.item_name, l.size_batch, l.unit,
                       SUM(l.qty) AS qty, SUM(l.amount) AS amount
                FROM `tab{table}` l
                INNER JOIN `tab{parent}` p ON p.name = l.parent
                WHERE p.company = %(company)s AND l.order_no = %(order_no)s
                  AND p.is_cancelled = 0
                GROUP BY l.item_name, l.size_batch, l.unit
                """,
                {"company": company, "order_no": order_no}, as_dict=True,
            )
            per_source: dict = {}
            for r in rows:
                key = (r.item_name, r.size_batch)
                per_source.setdefault(key, {})
                per_source[key][r.unit or ""] = (
                    per_source[key].get(r.unit or "", 0.0) + flt(r.qty))
                if table == "Tally Invoice Line":
                    value_delivered += flt(r.amount)
            for key, by_unit in per_source.items():
                cur = delivered.setdefault(key, {})
                for unit, qty in by_unit.items():
                    # max per source family, not sum across them
                    cur[unit] = max(cur.get(unit, 0.0), qty)

        doc = frappe.get_doc("Tally Sales Order", so_name)
        total_ordered = total_pending = 0.0
        unresolved = 0
        for line in doc.lines:
            key = (line.item_name, line.size_batch)
            got = 0.0
            for unit, qty in delivered.get(key, {}).items():
                converted = _to_unit(qty, unit, line.unit or unit, units)
                if converted is None:
                    unresolved += 1
                    continue
                got += converted
            pending = max(flt(line.qty) - got, 0.0)
            frappe.db.set_value("Tally Sales Order Line", line.name,
                                {"delivered_qty": round(got, 4),
                                 "pending_qty": round(pending, 4)},
                                update_modified=False)
            total_ordered += flt(line.qty)
            total_pending += pending

        if doc.is_cancelled:
            status = "Cancelled"
        elif total_ordered and total_pending <= total_ordered * 0.005:
            status = "Billed"
        elif total_pending < total_ordered:
            status = "Partial"
        else:
            status = "Open"
        frappe.db.set_value("Tally Sales Order", so_name,
                            {"order_status": status,
                             "delivered_value": round(value_delivered, 2)},
                            update_modified=False)
        done += 1
        if unresolved:
            frappe.log_error(
                title="Fulfilment: unit mismatch",
                message=f"{company} / {order_no}: {unresolved} delivered line(s) "
                        f"in a unit with no conversion chain to the ordered "
                        f"unit were ignored rather than mis-added.")
    return {"orders": done}


# ---------------------------------------------------------------------------
# Item rates, refreshed from mirrored lines (no extra Tally traffic)
# ---------------------------------------------------------------------------

def refresh_item_rates(company=None, days=45):
    """
    Rebuild Tally Item Rate from recent mirrored order/invoice lines.

    The book has no rate master and no price levels — rates exist only in
    voucher history. Book-wide row per item: the MOST-SUPPORTED recent
    (rate, discounts) combination, never merely the newest quote (a lone
    stray entry must not reprice the catalogue). Party rows only where a
    party's own most-supported rate differs from the book rate.

    Runs from the scheduler; also callable after a backfill.
    """
    companies = ([company] if company else
                 [r[0] for r in frappe.db.sql(
                     "SELECT DISTINCT company FROM `tabTally Sales Order`")])
    stamp = now_datetime()
    cutoff = add_days(getdate(), -int(days))
    total = 0

    for comp in companies:
        rows = frappe.db.sql(
            """
            SELECT l.item_name, p.party, l.unit, l.rate, l.rate_unit,
                   l.discount, l.discount2,
                   COUNT(*) AS n, MAX(p.voucher_date) AS latest,
                   MAX(p.voucher_number) AS voucher
            FROM `tabTally Sales Order Line` l
            INNER JOIN `tabTally Sales Order` p ON p.name = l.parent
            WHERE p.company = %(company)s AND p.voucher_date >= %(cutoff)s
              AND p.is_cancelled = 0 AND l.rate > 0
            GROUP BY l.item_name, p.party, l.unit, l.rate, l.rate_unit,
                     l.discount, l.discount2
            """,
            {"company": comp, "cutoff": cutoff}, as_dict=True,
        )
        if not rows:
            continue

        # Book-wide winner per item: most lines, then most recent.
        by_item: dict = {}
        by_item_party: dict = {}
        for r in rows:
            by_item.setdefault(r.item_name, []).append(r)
            by_item_party.setdefault((r.item_name, r.party), []).append(r)

        def winner(cands):
            agg: dict = {}
            for c in cands:
                k = (c.unit, c.rate, c.rate_unit, c.discount, c.discount2)
                a = agg.setdefault(k, {"n": 0, "latest": "", "voucher": "", "row": c})
                a["n"] += c.n
                if str(c.latest) > str(a["latest"]):
                    a["latest"], a["voucher"] = str(c.latest), c.voucher
            return max(agg.values(), key=lambda a: (a["n"], a["latest"]))

        frappe.db.delete("Tally Item Rate", {"company": comp})
        for item, cands in by_item.items():
            w = winner(cands)
            book = w["row"]
            net = flt(book.rate) * (1 - flt(book.discount) / 100) \
                                 * (1 - flt(book.discount2) / 100)
            doc = frappe.get_doc({
                "doctype": "Tally Item Rate",
                "item_name": item, "party": "", "company": comp,
                "rate": flt(book.rate), "unit": book.rate_unit or book.unit,
                "discount": flt(book.discount), "discount2": flt(book.discount2),
                "net_rate": round(net, 2),
                "source_voucher": w["voucher"], "source_date": w["latest"] or None,
                "observations": w["n"], "last_synced": stamp,
            })
            doc.name = _docname(comp, f"{item}|")
            doc.insert(ignore_permissions=True)
            total += 1

            # Party overrides, only where they genuinely differ.
            for (p_item, party), p_cands in by_item_party.items():
                if p_item != item or not party:
                    continue
                pw = winner(p_cands)
                pr = pw["row"]
                if (pr.rate, pr.discount, pr.discount2) == \
                   (book.rate, book.discount, book.discount2):
                    continue
                pnet = flt(pr.rate) * (1 - flt(pr.discount) / 100) \
                                    * (1 - flt(pr.discount2) / 100)
                pdoc = frappe.get_doc({
                    "doctype": "Tally Item Rate",
                    "item_name": item, "party": party, "company": comp,
                    "rate": flt(pr.rate), "unit": pr.rate_unit or pr.unit,
                    "discount": flt(pr.discount), "discount2": flt(pr.discount2),
                    "net_rate": round(pnet, 2),
                    "source_voucher": pw["voucher"],
                    "source_date": pw["latest"] or None,
                    "observations": pw["n"], "last_synced": stamp,
                })
                pdoc.name = _docname(comp, f"{item}|{party}")
                pdoc.insert(ignore_permissions=True)
                total += 1

    frappe.db.commit()
    return {"rates": total}
