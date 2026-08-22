# Copyright (c) 2026, SN Jain Industries and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

# Keep a working history, not an archive. Each row carries the whole page
# (a few hundred KB), and nobody reads a readiness report from two months ago
# — the order book it describes has turned over completely by then.
KEEP = 60


class DispatchReadinessSnapshot(Document):
	def after_insert(self):
		# Pruning is housekeeping, not part of the upload. If it fails the
		# report has still landed, and losing today's figures over a tidy-up
		# error would be the worse outcome — so it is logged, not raised.
		try:
			old = frappe.get_all(
				self.doctype,
				fields=["name"],
				order_by="as_of desc",
				limit_start=KEEP,
				limit_page_length=500,
			)
			for row in old:
				frappe.delete_doc(self.doctype, row.name,
				                  ignore_permissions=True, force=True)
		except Exception:
			frappe.log_error(frappe.get_traceback(),
			                 "Dispatch Readiness Snapshot prune")
