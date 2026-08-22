"""Create the role the sync agent publishes the readiness report under.

The agent authenticates as a machine user that otherwise only reads
(`Accounts User`). Rather than hand it System Manager so it can write one
doctype, this role carries exactly that one permission — the doctype's own
permission block grants it create/write, and nothing else on the site does.

Idempotent: re-running a patch is normal on Frappe, and `bench migrate` runs
every patch that has not been recorded as applied on this site.
"""

import frappe

ROLE = "Dispatch Readiness Publisher"


def execute():
	if frappe.db.exists("Role", ROLE):
		return
	frappe.get_doc({
		"doctype": "Role",
		"role_name": ROLE,
		"desk_access": 1,
		"is_custom": 0,
	}).insert(ignore_permissions=True)
