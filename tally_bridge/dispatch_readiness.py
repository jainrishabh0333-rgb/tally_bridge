"""Desk-side storage for the dispatch-readiness report.

The report itself is produced off-site by the sync agent
(`sync_agent/pending_readiness.py --html --json`); this module only keeps the
result and hands it to the `dispatch-readiness` desk page.

Two rules this file exists to enforce:

* **Desk only.** Nothing here is `allow_guest`. The report names parties,
  order numbers and shortfalls, and the party-facing rule says none of that
  leaves the building. `frappe.whitelist()` without `allow_guest` already
  requires a session; the explicit permission checks below make the intent
  visible and cover the case where the doctype's permissions are tightened
  later but this module is forgotten.
* **One row per day.** The upload replaces the day it is for, so a re-run
  after a correction does not leave two versions of the same date lying
  around for someone to read the wrong one.
"""

import json

import frappe

DOCTYPE = "Dispatch Readiness Snapshot"
FIELDS = ["name", "as_of", "window_from", "generated_on", "order_count",
          "coverage_pct", "blocking_items", "unsighted_items"]


def _can(ptype="read"):
	if not frappe.has_permission(DOCTYPE, ptype):
		raise frappe.PermissionError(
			f"Not permitted to {ptype} {DOCTYPE}")


@frappe.whitelist()
def snapshots(limit=60):
	"""The stored report dates, newest first, without their payloads."""
	_can()
	return frappe.get_all(DOCTYPE, fields=FIELDS, order_by="as_of desc",
	                      limit_page_length=int(limit))


@frappe.whitelist()
def get_snapshot(name=None):
	"""One report, page included. Empty name means the most recent."""
	_can()
	if not name:
		rows = frappe.get_all(DOCTYPE, fields=["name"], order_by="as_of desc",
		                      limit_page_length=1)
		if not rows:
			return None
		name = rows[0].name
	if not frappe.db.exists(DOCTYPE, name):
		return None
	doc = frappe.get_doc(DOCTYPE, name)
	return {
		"name": doc.name,
		"as_of": doc.as_of,
		"window_from": doc.window_from,
		"generated_on": doc.generated_on,
		"order_count": doc.order_count,
		"coverage_pct": doc.coverage_pct,
		"blocking_items": doc.blocking_items,
		"unsighted_items": doc.unsighted_items,
		"page_html": doc.page_html,
	}


@frappe.whitelist(methods=["POST"])
def store(as_of, page_html, payload_json=None, window_from=None,
          generated_on=None, order_count=0, coverage_pct=0.0,
          blocking_items=0, unsighted_items=0):
	"""Upsert one day's report. Called by the sync agent with an API key.

	`as_of` is an ISO date and is also the record name, so the same day is
	replaced rather than duplicated.
	"""
	_can("create")
	_can("write")

	values = {
		"window_from": window_from,
		"generated_on": generated_on or frappe.utils.now(),
		"order_count": frappe.utils.cint(order_count),
		"coverage_pct": frappe.utils.flt(coverage_pct),
		"blocking_items": frappe.utils.cint(blocking_items),
		"unsighted_items": frappe.utils.cint(unsighted_items),
		"page_html": page_html,
		"payload_json": payload_json,
	}

	if frappe.db.exists(DOCTYPE, as_of):
		doc = frappe.get_doc(DOCTYPE, as_of)
		doc.update(values)
		doc.save()
		action = "updated"
	else:
		doc = frappe.get_doc(dict(doctype=DOCTYPE, as_of=as_of, **values))
		doc.insert()
		action = "created"
	frappe.db.commit()
	return {"name": doc.name, "action": action,
	        "url": f"/app/dispatch-readiness"}
