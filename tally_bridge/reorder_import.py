"""
reorder_import.py — receiver for Tally's "Web portal" export of the Reorder Report.

TallyPrime's E: Export can POST a report straight to a URL instead of writing a
file (Export Settings > Export to: Web portal). That removes the download-and-
forward step: whoever revises the reorder levels runs the report, presses
export, and the levels land here.

Why this exists at all: the reorder LEVEL is the one column of that report
which cannot be read over Tally's XML gateway. Probed exhaustively 2026-08-21 —
native REORDERLEVEL reads back empty, the stock item carries no UDFs, and the
custom `ReorderReport` returns only its variable block because it prompts for a
group interactively. Every OTHER column is recomputed live by
sync_agent/reorder_fetch.py. So levels arrive here, occasionally, and the rest
is derived continuously.

Design rules, in order of importance:

  1. NEVER LOSE A PAYLOAD. The raw XML is written verbatim before anything is
     parsed, and a parse failure still returns success to Tally. This earned
     its keep immediately: the first real export parsed to 475 rows of zeroes
     because the shape was nothing like the guess, and the stored copy is what
     made the fix possible without re-exporting. Re-run with reparse_import().
  2. Fail closed on auth. The shared secret must be configured or the endpoint
     refuses every request — an unset secret must never mean "open".
  3. Treat the body as hostile. It arrives unauthenticated-by-Frappe from a
     machine on the internet: size-capped, parsed with entity expansion off,
     and only ever stored — never evaluated.

Setup (once) — either one works:

    * Desk > Tally Reorder Settings > Import Token, or
    * bench --site <site> set-config tally_reorder_token "<64-char random>"

The Settings doctype exists because Frappe Cloud's Site Config dialog will not
always accept a custom key. Generate the value with `openssl rand -hex 32`.

Then in Tally: Export Settings > Export to: Web portal, URL:

    https://<site>/api/method/tally_bridge.reorder_import.receive?token=<token>

with File Format: XML (Data Interchange). Secure Server: Yes if the site is
HTTPS, which it should be — the token is a bearer credential in the query
string and must not cross the network in clear.
"""

from __future__ import annotations

import hmac
import re
import xml.etree.ElementTree as ET

import frappe
from frappe.utils import now_datetime, add_to_date

# A whole group's report is a few MB; 32 is generous without inviting a
# memory-exhaustion attempt.
MAX_BYTES = 32 * 1024 * 1024
MAX_POSTS_PER_HOUR = 60

# Tag -> field. The report's headers are:
#   S no. | ITEM | Group | SIZE | IN STOCK | UNPACK QTY | STITCHING |
#   PENDING ORDER | REORDER LEVEL | DEFICIT /SURPLUS
# but Tally names the XML tags after its own TDL variables, not the headers —
# the report's variable block is ROITEMGRPNAME / ROARTICLENAMEN /
# ROSIZECOLORSIZENAME, so "article name" arrives with a trailing N and "size"
# arrives wrapped in colour. Exact matching misses all of those, so these are
# ORDERED SUBSTRING rules, first match wins.
#
# Order is load-bearing. "ITEMGRPNAME" contains both "grp" and "item"; group
# must win or every row's group would be read as its item name. Likewise
# "stockgroup" contains "stock", and "reorderlevel" contains "order".
_FIELD_RULES = [
    # Observed live 2026-08-21, Panty export: SROITEMNAME / SROITEMGRP /
    # SROSIZE / SROINSTOCK / SRUNPACKQTY / SROSTITCHING / SRPENDINGORD /
    # SROREORDLBL / SRODEFSURP, with SERNO marking each row.
    ("reorderlevel", "reorder_level"),
    ("reordlbl", "reorder_level"),      # SROREORDLBL — "lbl", not "level"
    ("reord", "reorder_level"),
    ("deficit", "deficit"),
    ("defsurp", "deficit"),             # SRODEFSURP — truncated both halves
    ("surplus", "deficit"),
    ("surp", "deficit"),
    ("pendingorder", "pending_order"),
    ("pendingord", "pending_order"),    # SRPENDINGORD — truncated
    ("pendingqty", "pending_order"),
    ("pending", "pending_order"),
    ("grp", "stock_group"),
    ("group", "stock_group"),
    ("unpack", "unpack_qty"),
    ("stitching", "stitching"),
    ("instock", "in_stock"),
    ("closingstock", "in_stock"),
    ("size", "size"),
    ("article", "item_name"),
    ("itemname", "item_name"),
    ("stockitem", "item_name"),
    ("item", "item_name"),
    ("level", "reorder_level"),
]

# Tags that mark the START of a row rather than carrying a value.
_SERIAL_TAGS = {"serno", "sno", "srno", "slno", "serialno", "sr", "srlno"}

_NUMERIC = {"in_stock", "unpack_qty", "stitching", "pending_order",
            "reorder_level", "deficit"}


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive(token: str | None = None):
    """
    Accept one Reorder Report export from Tally and store it.

    Answers Tally with a minimal XML envelope. Tally treats a non-2xx as a
    failed export and may discard the payload, so anything short of an auth or
    size rejection returns success — the import row records what went wrong.
    """
    _authenticate(token)
    _check_rate()

    body = frappe.request.get_data() or b""
    if len(body) > MAX_BYTES:
        frappe.throw("Payload too large", frappe.ValidationError)
    if not body:
        frappe.throw("Empty payload", frappe.ValidationError)

    raw = _decode(body)
    ip = (frappe.local.request_ip or "")[:45]

    doc = frappe.get_doc({
        "doctype": "Tally Reorder Import",
        "received_on": now_datetime(),
        "status": "Received",
        "byte_size": len(body),
        "source_ip": ip,
        "raw_xml": raw,
    })
    # ignore_permissions: the guest caller is authenticated by shared secret,
    # not by a Frappe session, so there is no role to check against.
    doc.insert(ignore_permissions=True)
    frappe.db.commit()

    try:
        rows = parse_reorder_xml(raw)
        _store_levels(rows, doc.name)
        doc.db_set({"status": "Parsed", "row_count": len(rows),
                    "company": (rows[0].get("company") or "") if rows else "",
                    "group_name": (rows[0].get("stock_group") or "") if rows else ""},
                   update_modified=False)
        frappe.db.commit()
        return _ok(f"stored {len(rows)} rows")
    except Exception as exc:
        # Deliberately swallowed. The payload is already safe on disk; failing
        # the request here would make Tally report an export error and could
        # cost us the only copy.
        frappe.log_error(frappe.get_traceback(), "Reorder import parse failed")
        doc.db_set({"status": "Parse Failed", "error": str(exc)[:1000]},
                   update_modified=False)
        frappe.db.commit()
        return _ok("stored raw; parse failed and was logged")


@frappe.whitelist(methods=["POST"])
def reparse_import(name: str):
    """
    Re-run the parser over a stored payload, after the parser learns the shape.

    This is the reason raw_xml is kept: the first export can arrive before
    anyone knows what the report's XML looks like, and it must still be usable
    once they do.
    """
    if "System Manager" not in frappe.get_roles():
        frappe.throw("Not permitted", frappe.PermissionError)
    doc = frappe.get_doc("Tally Reorder Import", name)
    rows = parse_reorder_xml(doc.raw_xml or "")
    _store_levels(rows, doc.name)
    doc.db_set({"status": "Parsed", "row_count": len(rows), "error": ""},
               update_modified=False)
    return {"rows": len(rows)}


# ---------------------------------------------------------------------------
# Parsing — shape-tolerant on purpose
# ---------------------------------------------------------------------------

def parse_reorder_xml(raw: str) -> list[dict]:
    """
    Pull reorder rows out of whatever Tally sent.

    Two shapes are handled, because this report uses the awkward one:

      FLAT (what the live report actually sends) — no row wrapper at all.
      Every field is a sibling under <ENVELOPE>, with <SERNO> marking where
      each row begins:

          <SERNO>1</SERNO><SROITEMNAME>..</SROITEMNAME><SROSIZE>42</SROSIZE>..
          <SERNO>2</SERNO><SROITEMNAME>..</SROITEMNAME>..

      NESTED — each row is its own element. Common in Tally exports generally,
      just not in this one.

    Both are attempted and the better result wins, scored by how many FIELDS
    were extracted rather than how many rows. Row count alone would be fooled:
    reading the flat shape as nested yields exactly as many rows, each holding
    only an item name, and that near-miss is precisely what shipped empty
    numbers the first time.
    """
    if not raw.strip():
        return []
    root = _safe_parse(raw)

    flat = _parse_flat(root)
    nested = _parse_nested(root)
    return flat if _score(flat) >= _score(nested) else nested


def _score(rows: list[dict]) -> int:
    """Total fields extracted — a row carrying only a name is nearly worthless."""
    return sum(len(r) for r in rows)


def _parse_flat(root) -> list[dict]:
    """
    Read a flat field stream into rows.

    A row ends when a serial tag appears, or when a field repeats that the
    current row already holds — the second rule means a missing or renamed
    SERNO cannot merge two rows into one.
    """
    # The element carrying the most leaf children is the data container.
    best_parent, best_leaves = None, 0
    for el in root.iter():
        leaves = sum(1 for c in el if not len(c))
        if leaves > best_leaves:
            best_parent, best_leaves = el, leaves
    if best_parent is None:
        return []

    rows: list[dict] = []
    cur: dict = {}
    for leaf in best_parent:
        if len(leaf):
            continue
        key = _norm(leaf.tag)
        if key in _SERIAL_TAGS:
            if cur:
                rows.append(cur)
                cur = {}
            continue
        field = _field_for(leaf.tag)
        if not field:
            continue
        if field in cur:
            rows.append(cur)
            cur = {}
        text = (leaf.text or "").strip()
        if not text:
            continue
        cur[field] = _number(text) if field in _NUMERIC else text
    if cur:
        rows.append(cur)
    return [r for r in rows if r.get("item_name")]


def _parse_nested(root) -> list[dict]:
    """Read the shape where each row is its own element."""
    best: list[dict] = []
    for parent in root.iter():
        by_tag: dict[str, list] = {}
        for c in parent:
            # Only an element with children can be a row; a bare leaf is a
            # field, and treating it as a row is the bug this guards against.
            if len(c):
                by_tag.setdefault(c.tag, []).append(c)
        for _tag, group in by_tag.items():
            if len(group) < 2:
                continue
            rows = [_row_from(el) for el in group]
            rows = [r for r in rows if r.get("item_name")]
            if _score(rows) > _score(best):
                best = rows
    return best


def _row_from(el) -> dict:
    """Map one element's leaf children onto reorder fields."""
    row: dict = {}
    for leaf in el.iter():
        if len(leaf):
            continue
        field = _field_for(leaf.tag)
        if not field:
            continue
        text = (leaf.text or "").strip()
        if not text:
            continue
        row[field] = _number(text) if field in _NUMERIC else text
    return row


def _norm(tag: str) -> str:
    """'DEFICIT /SURPLUS' / 'RO_ReorderLevel' -> 'deficitsurplus' / 'reorderlevel'."""
    tag = tag.split("}")[-1].split(":")[-1]
    tag = re.sub(r"^(RO|TB)[_-]?", "", tag, flags=re.I)
    return re.sub(r"[^a-z]", "", tag.lower())


def _field_for(tag: str) -> str | None:
    """First matching rule wins — see the ordering note on _FIELD_RULES."""
    key = _norm(tag)
    if not key:
        return None
    for needle, field in _FIELD_RULES:
        if needle in key:
            return field
    return None


def _number(text: str) -> float:
    """
    '(-)7.50' / '-7.50 Doz' / '1,234.50' -> float.

    Tally prints negatives as '(-)7.50' in this report, which float() will not
    take and which a naive strip turns into a POSITIVE 7.50 — a deficit read
    as a surplus, i.e. exactly the wrong production decision.
    """
    t = text.replace(",", "").strip()
    negative = "(-)" in t or t.lstrip().startswith("-")
    m = re.search(r"[\d.]+", t)
    if not m:
        return 0.0
    try:
        value = float(m.group())
    except ValueError:
        return 0.0
    return -value if negative else value


def _safe_parse(raw: str):
    """
    Parse untrusted XML with entity expansion disabled.

    Also repairs the two things Tally reliably emits that break a strict
    parser: references to control characters, and undeclared namespace
    prefixes such as <UDF:BLNCQTY>.
    """
    raw = re.sub(r"&#(?:x0*[0-8bcefBCEF]|0*(?:[0-8]|1[124-9]|2[0-9]|3[01]));", "", raw)
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
    raw = re.sub(r"(</?)([A-Za-z][\w.]*):", r"\1\2_", raw)
    parser = ET.XMLParser()
    # Refuse DTDs outright: no entity definitions means no billion-laughs and
    # no external-entity fetch, whatever the platform default happens to be.
    if "<!DOCTYPE" in raw[:4096] or "<!ENTITY" in raw[:4096]:
        frappe.throw("DTD in payload is not accepted", frappe.ValidationError)
    return ET.fromstring(raw, parser=parser)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _store_levels(rows: list[dict], import_name: str) -> None:
    """Upsert one Tally Reorder Level per (item, size). Idempotent."""
    stamp = now_datetime()
    for row in rows:
        item = (row.get("item_name") or "").strip()
        if not item:
            continue
        size = str(row.get("size") or "").strip()
        name = f"{item}::{size}"[:140]
        values = {
            "item_name": item,
            "size": size,
            "stock_group": row.get("stock_group") or "",
            "reorder_level": row.get("reorder_level") or 0.0,
            "in_stock": row.get("in_stock") or 0.0,
            "unpack_qty": row.get("unpack_qty") or 0.0,
            "stitching": row.get("stitching") or 0.0,
            "pending_order": row.get("pending_order") or 0.0,
            "deficit": row.get("deficit") or 0.0,
            "as_of": stamp,
            "source_import": import_name,
        }
        if frappe.db.exists("Tally Reorder Level", name):
            frappe.db.set_value("Tally Reorder Level", name, values,
                                update_modified=False)
        else:
            doc = frappe.get_doc({"doctype": "Tally Reorder Level",
                                  "name": name, **values})
            doc.insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def _expected_token() -> str:
    """
    The shared secret, from site config or from Tally Reorder Settings.

    Site config is checked first because it is the more locked-down of the
    two — but Frappe Cloud's Site Config dialog will not always accept a
    custom key, so the Settings single doctype is a first-class alternative
    rather than a fallback. Either one configures the endpoint; neither being
    set leaves it refusing everything.
    """
    from_conf = frappe.conf.get("tally_reorder_token") or ""
    if from_conf:
        return str(from_conf)
    try:
        settings = frappe.get_single("Tally Reorder Settings")
    except Exception:
        return ""
    if not settings.get("enabled"):
        # Explicitly switched off: report as unconfigured, so the caller gets
        # the same refusal as a site that never set a token.
        return ""
    return str(settings.get_password("import_token", raise_exception=False) or "")


def _authenticate(token: str | None) -> None:
    """
    Shared-secret check, constant-time, failing closed when unconfigured.

    The token may also travel as an X-Tally-Token header, which keeps it out
    of web-server access logs when the exporter can set headers.
    """
    expected = _expected_token()
    if not expected:
        # An unset secret must never read as "no check required".
        frappe.throw(
            "Reorder import is not configured: set the Import Token in Tally "
            "Reorder Settings (or `tally_reorder_token` in site config) "
            "before using this endpoint.", frappe.PermissionError)
    supplied = token or frappe.get_request_header("X-Tally-Token") or ""
    if not hmac.compare_digest(str(supplied), str(expected)):
        frappe.throw("Invalid token", frappe.PermissionError)


def _check_rate() -> None:
    """A legitimate exporter posts a handful of times a day."""
    hour_ago = add_to_date(now_datetime(), hours=-1)
    recent = frappe.db.count("Tally Reorder Import",
                             {"received_on": [">", hour_ago]})
    if recent >= MAX_POSTS_PER_HOUR:
        frappe.throw("Rate limit exceeded", frappe.ValidationError)


def _decode(body: bytes) -> str:
    """
    Tally speaks UTF-16 on the XML gateway but its exporter may send UTF-8.

    A BOM settles it; otherwise the NUL-byte density does, since UTF-16-encoded
    ASCII is half NULs.
    """
    if body[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return body.decode("utf-16", "replace")
    sample = body[:4096]
    if sample.count(b"\x00") > len(sample) // 4:
        return body.decode("utf-16", "replace")
    return body.decode("utf-8", "replace")


def _ok(message: str):
    """Tally wants an XML answer; anything else is logged as an export error."""
    frappe.local.response["type"] = "binary"
    frappe.local.response["filename"] = "response.xml"
    frappe.local.response["filecontent"] = (
        f"<RESPONSE><STATUS>1</STATUS><MESSAGE>{frappe.utils.escape_html(message)}"
        f"</MESSAGE></RESPONSE>".encode("utf-8"))
    frappe.local.response["content_type"] = "text/xml"
    return frappe.local.response
