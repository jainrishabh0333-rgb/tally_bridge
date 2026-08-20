# Copyright (c) 2026, SN Jain Industries
# For license information, please see license.txt

"""
The tests that make the portal's security claim checkable, not asserted:

  1. Distributor A can never read distributor B's orders, bills, statement,
     network or invoices — including with forged parameters.
  2. Replaying any upsert or place_order payload produces zero duplicates.

Run on the site (they need a database and the snj_dms grant doctype):

    bench --site <site> run-tests --app tally_bridge

Tests are skipped, loudly, when snj_dms is not installed — the portal
resolves parties through its DMS Portal Access grant and cannot be tested
without it.
"""

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tally_bridge import portal
from tally_bridge.api import (
    _docname,
    upsert_invoices,
    upsert_receipts,
    upsert_sales_orders,
    upsert_stock_batches,
)

COMPANY = "_Test Distributor Co"
PARTY_A = "_Test Distributor A"
PARTY_B = "_Test Distributor B"
USER_A = "_test.distributor.a@example.com"
USER_B = "_test.distributor.b@example.com"


def _mk_ledger(name, mobile=""):
    doc = frappe.get_doc({
        "doctype": "Tally Ledger",
        "ledger_name": name,
        "company": COMPANY,
        "parent_group": "AGENT TEST",
        "primary_group": "Sundry Debtors",
        "group_path": "Current Assets > Sundry Debtors > AGENT TEST",
        "opening_balance": 1000.0,
        "closing_balance": 5000.0,
        "agent": "AGENT TEST",
        "agent_source": "group",
        "mobile": mobile,
        "guid": f"test-guid-{name}",
    })
    doc.name = _docname(COMPANY, name, f"test-guid-{name}")
    doc.insert(ignore_permissions=True, ignore_if_duplicate=True)
    return doc


def _mk_bill(party, ref, outstanding, overdue_days=0):
    doc = frappe.get_doc({
        "doctype": "Tally Bill",
        "party": party,
        "bill_ref": ref,
        "company": COMPANY,
        "bill_date": "2026-07-01",
        "due_date": "2026-07-31",
        "overdue_days": overdue_days,
        "outstanding": outstanding,
        "primary_group": "Sundry Debtors",
        "parent_group": "AGENT TEST",
    })
    doc.name = _docname(f"{COMPANY}|{party}", ref)
    doc.insert(ignore_permissions=True, ignore_if_duplicate=True)


def _so_payload(party, guid, voucher_number, qty=10.0):
    return {
        "guid": guid,
        "company": COMPANY,
        "voucher_number": voucher_number,
        "date": "2026-08-01",
        "party": party,
        "reference": "",
        "narration": "",
        "amount": 4800.0,
        "is_cancelled": False,
        "is_optional": False,
        "alter_id": "1",
        "lines": [{
            "item_name": "_Test Item Bra",
            "size_batch": "28",
            "qty": qty,
            "unit": "Doz",
            "rate": 1200.0,
            "rate_unit": "Doz",
            "discount": 50.0,
            "discount2": 20.0,
            "amount": 4800.0,
            "due_date": "2026-08-10",
            "order_no": voucher_number,
        }],
    }


class DistributorPortalTestCase(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not frappe.db.exists("DocType", "DMS Portal Access"):
            raise unittest.SkipTest(
                "snj_dms is not installed — the portal resolves parties "
                "through DMS Portal Access and cannot be tested without it.")

    def setUp(self):
        frappe.set_user("Administrator")
        _mk_ledger(PARTY_A, mobile="9800000001")
        _mk_ledger(PARTY_B, mobile="9800000002")
        frappe.get_doc({
            "doctype": "Tally Stock Item",
            "item_name": "_Test Item Bra",
            "company": COMPANY,
            "base_units": "Doz",
            "closing_qty": 100.0,
        }).insert(ignore_permissions=True, ignore_if_duplicate=True)

        # The portal is pinned to one company file; tests pin it to theirs.
        self._saved_company = frappe.db.get_single_value(
            "DMS Settings", "default_company")
        frappe.db.set_single_value("DMS Settings", "default_company", COMPANY)

        for user, party in ((USER_A, PARTY_A), (USER_B, PARTY_B)):
            if not frappe.db.exists("DMS Portal Access", user):
                frappe.get_doc({
                    "doctype": "DMS Portal Access",
                    "user_email": user,
                    "party_ledger": _docname(COMPANY, party, f"test-guid-{party}"),
                    "enabled": 1,
                }).insert(ignore_permissions=True)

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.set_single_value(
            "DMS Settings", "default_company", self._saved_company)
        super().tearDown()

    # -- isolation ----------------------------------------------------------

    def test_bills_are_party_scoped(self):
        _mk_bill(PARTY_A, "BILL/A/1", 1500.0, overdue_days=10)
        _mk_bill(PARTY_B, "BILL/B/1", 2500.0, overdue_days=99)

        frappe.set_user(USER_A)
        out = portal.get_bills()
        refs = {r["bill_ref"] for r in out["rows"]}
        self.assertIn("BILL/A/1", refs)
        self.assertNotIn("BILL/B/1", refs)
        self.assertEqual(out["total"], 1500.0)

    def test_orders_are_party_scoped(self):
        frappe.set_user("Administrator")
        upsert_sales_orders(orders=[
            _so_payload(PARTY_A, "so-guid-a1", "SO/A/1"),
            _so_payload(PARTY_B, "so-guid-b1", "SO/B/1"),
        ])

        frappe.set_user(USER_A)
        out = portal.get_orders()
        numbers = {o["order_no"] for o in out["orders"]}
        self.assertIn("SO/A/1", numbers)
        self.assertNotIn("SO/B/1", numbers)

    def test_forged_order_key_is_refused(self):
        frappe.set_user("Administrator")
        upsert_sales_orders(orders=[_so_payload(PARTY_B, "so-guid-b2", "SO/B/2")])

        frappe.set_user(USER_A)
        # A knows (or guesses) B's order number. The endpoint must answer
        # exactly as it would for a number that does not exist at all.
        with self.assertRaises(frappe.DoesNotExistError):
            portal.get_order(order_key="SO/B/2")

    def test_statement_is_party_scoped(self):
        frappe.set_user("Administrator")
        for party, guid in ((PARTY_A, "v-a"), (PARTY_B, "v-b")):
            doc = frappe.get_doc({
                "doctype": "Tally Voucher",
                "guid": guid, "company": COMPANY,
                "voucher_type": "Sales", "voucher_number": f"INV-{party[-1]}",
                "voucher_date": "2026-08-01", "party": party,
                "amount": 1000.0,
                "entries": [
                    {"ledger": party, "amount": 1000.0, "is_debit": 1},
                    {"ledger": "_Test Sales Ledger", "amount": -1000.0,
                     "is_debit": 0},
                ],
            })
            doc.name = _docname(COMPANY, guid, guid)
            doc.insert(ignore_permissions=True, ignore_if_duplicate=True)

        frappe.set_user(USER_A)
        out = portal.get_statement()
        vchs = {r["vch"] for r in out["rows"]}
        self.assertIn("INV-A", vchs)
        self.assertNotIn("INV-B", vchs)

    def test_network_never_shows_unrelated_parties(self):
        frappe.set_user(USER_A)
        out = portal.get_network()
        names = {s["name"] for s in out["sub_parties"]}
        self.assertNotIn(PARTY_B, names)

    def test_place_order_ignores_forged_party(self):
        frappe.set_user("Administrator")
        frappe.get_doc({
            "doctype": "Tally Item Rate",
            "item_name": "_Test Item Bra", "party": "", "company": COMPANY,
            "rate": 1200.0, "unit": "Doz", "discount": 50.0,
            "discount2": 20.0, "net_rate": 480.0,
        }).insert(ignore_permissions=True, ignore_if_duplicate=True)

        frappe.set_user(USER_A)
        out = portal.place_order(payload={
            "order_key": "TEST-FORGE-1",
            # Hostile payload: names B as the party. Must be ignored.
            "party_ledger": PARTY_B,
            "party": PARTY_B,
            "lines": [{"item_name": "_Test Item Bra", "size_batch": "",
                       "qty": 2}],
        })
        self.assertTrue(out["queued"])
        row = frappe.db.get_value("Tally Order Queue", "TEST-FORGE-1",
                                  ["party_ledger", "source"], as_dict=True)
        self.assertEqual(row.party_ledger, PARTY_A)
        self.assertEqual(row.source, "distributor-portal")

    def test_no_grant_means_no_access(self):
        frappe.set_user("Administrator")
        frappe.db.set_value("DMS Portal Access", USER_A, "enabled", 0)
        frappe.set_user(USER_A)
        with self.assertRaises(frappe.PermissionError):
            portal.get_summary()

    # -- idempotency --------------------------------------------------------

    def test_upsert_sales_orders_replay_is_clean(self):
        frappe.set_user("Administrator")
        payload = [_so_payload(PARTY_A, "so-guid-replay", "SO/A/R")]
        first = upsert_sales_orders(orders=payload)
        second = upsert_sales_orders(orders=payload)
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["unchanged"], 1)      # AlterID short-circuit
        self.assertEqual(frappe.db.count(
            "Tally Sales Order",
            {"company": COMPANY, "voucher_number": "SO/A/R"}), 1)

    def test_upsert_receipts_replay_is_clean(self):
        frappe.set_user("Administrator")
        payload = [{
            "guid": "rcpt-guid-1", "company": COMPANY,
            "voucher_number": "", "date": "2026-08-05", "party": PARTY_A,
            "amount": 5000.0, "mode": "_Test Bank", "alter_id": "7",
            "allocations": [{"bill_ref": "BILL/A/1", "bill_type": "Agst Ref",
                             "amount": 5000.0}],
        }]
        upsert_receipts(receipts=payload)
        upsert_receipts(receipts=payload)
        self.assertEqual(frappe.db.count(
            "Tally Receipt", {"company": COMPANY, "party": PARTY_A}), 1)

    def test_place_order_replay_returns_existing(self):
        frappe.set_user("Administrator")
        frappe.get_doc({
            "doctype": "Tally Item Rate",
            "item_name": "_Test Item Bra", "party": "", "company": COMPANY,
            "rate": 1200.0, "unit": "Doz", "discount": 50.0,
            "discount2": 20.0, "net_rate": 480.0,
        }).insert(ignore_permissions=True, ignore_if_duplicate=True)

        frappe.set_user(USER_A)
        payload = {"order_key": "TEST-REPLAY-1",
                   "lines": [{"item_name": "_Test Item Bra", "qty": 1}]}
        first = portal.place_order(payload=payload)
        second = portal.place_order(payload=payload)
        self.assertTrue(first["queued"])
        self.assertFalse(second["queued"])
        self.assertEqual(frappe.db.count(
            "Tally Order Queue", {"order_key": "TEST-REPLAY-1"}), 1)

        # And B cannot replay A's key to read its status.
        frappe.set_user(USER_B)
        with self.assertRaises(frappe.ValidationError):
            portal.place_order(payload=payload)

    def test_stock_batch_replay_keeps_newest(self):
        frappe.set_user("Administrator")
        newer = [{"item_name": "_Test Item Bra", "batch_name": "28",
                  "company": COMPANY, "closing_qty": 40.0,
                  "closing_qty_unit": "Doz", "as_of": "2026-08-10",
                  "source_voucher": "SO/NEW"}]
        older = [{"item_name": "_Test Item Bra", "batch_name": "28",
                  "company": COMPANY, "closing_qty": 99.0,
                  "closing_qty_unit": "Doz", "as_of": "2026-08-01",
                  "source_voucher": "SO/OLD"}]
        upsert_stock_batches(batches=newer)
        out = upsert_stock_batches(batches=older)    # replayed old data
        self.assertEqual(out["stale_skipped"], 1)
        qty = frappe.db.get_value(
            "Tally Stock Batch",
            {"company": COMPANY, "item_name": "_Test Item Bra",
             "batch_name": "28"}, "closing_qty")
        self.assertEqual(float(qty), 40.0)

    # -- fulfilment + matching ---------------------------------------------

    def test_invoice_updates_delivered_and_stage(self):
        frappe.set_user("Administrator")
        upsert_sales_orders(orders=[_so_payload(PARTY_A, "so-guid-f1",
                                                "SO/A/F1", qty=10.0)])
        upsert_invoices(invoices=[{
            "guid": "inv-guid-f1", "company": COMPANY,
            "invoice_no": "SNJ/T/1", "date": "2026-08-05", "party": PARTY_A,
            "amount": 2400.0, "taxable_value": 2286.0, "igst": 114.0,
            "alter_id": "3",
            "lines": [{"item_name": "_Test Item Bra", "size_batch": "28",
                       "qty": 5.0, "unit": "Doz", "rate": 1200.0,
                       "amount": 2400.0, "order_no": "SO/A/F1"}],
        }])
        so = frappe.db.get_value(
            "Tally Sales Order",
            {"company": COMPANY, "voucher_number": "SO/A/F1"},
            ["name", "order_status"], as_dict=True)
        self.assertEqual(so.order_status, "Partial")
        line = frappe.db.get_value(
            "Tally Sales Order Line",
            {"parent": so.name}, ["delivered_qty", "pending_qty"],
            as_dict=True)
        self.assertEqual(float(line.delivered_qty), 5.0)
        self.assertEqual(float(line.pending_qty), 5.0)

    # -- catalogue PDFs and order photos ------------------------------------

    def test_catalogue_needs_a_grant_and_hides_inactive(self):
        frappe.set_user("Administrator")
        cat = frappe.get_doc({
            "doctype": "Portal Catalogue", "title": "_Test Festive 2026",
            "file": "/private/files/_test_cat.pdf", "active": 1,
            "added_on": "2026-08-01",
        }).insert(ignore_permissions=True)

        frappe.set_user(USER_A)
        titles = {r["title"] for r in portal.get_catalogues()["rows"]}
        self.assertIn("_Test Festive 2026", titles)

        frappe.set_user("Administrator")
        frappe.db.set_value("Portal Catalogue", cat.name, "active", 0)
        frappe.set_user(USER_A)
        titles = {r["title"] for r in portal.get_catalogues()["rows"]}
        self.assertNotIn("_Test Festive 2026", titles)
        # Unpublished answers exactly like absent.
        with self.assertRaises(frappe.DoesNotExistError):
            portal.download_catalogue(catalogue=cat.name)

        frappe.set_user("Administrator")
        frappe.db.set_value("DMS Portal Access", USER_A, "enabled", 0)
        frappe.set_user(USER_A)
        with self.assertRaises(frappe.PermissionError):
            portal.get_catalogues()

    def test_order_photo_isolation_and_replay(self):
        import base64
        payload = base64.b64encode(b"fake-jpeg-bytes").decode()

        frappe.set_user(USER_A)
        first = portal.upload_order_photo(client_key="PH-TEST-1",
                                          filename="pad.jpg", content=payload)
        again = portal.upload_order_photo(client_key="PH-TEST-1",
                                          filename="pad.jpg", content=payload)
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertEqual(frappe.db.count(
            "Distributor Order Photo", {"client_key": "PH-TEST-1"}), 1)

        # A's photo appears in A's orders as Received...
        stages = {o["stage"] for o in portal.get_orders()["orders"]}
        self.assertIn("Received", stages)

        # ...and never in B's anything — including a replay of A's key.
        frappe.set_user(USER_B)
        self.assertEqual(
            [o for o in portal.get_orders()["orders"] if o.get("photo")], [])
        with self.assertRaises(frappe.ValidationError):
            portal.upload_order_photo(client_key="PH-TEST-1",
                                      filename="pad.jpg", content=payload)

    def test_queue_failure_text_never_reaches_the_distributor(self):
        frappe.set_user("Administrator")
        frappe.get_doc({
            "doctype": "Tally Order Queue", "order_key": "Q-FAIL-1",
            "company": COMPANY, "party_ledger": PARTY_A,
            "status": "Pending", "queued_at": frappe.utils.now_datetime(),
        }).insert(ignore_permissions=True)
        frappe.db.set_value("Tally Order Queue", "Q-FAIL-1",
                            {"status": "Failed",
                             "error": "ledger 'X' missing in company file"})

        frappe.set_user(USER_A)
        rows = [o for o in portal.get_orders()["orders"]
                if o.get("order_key") == "Q-FAIL-1"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stage"], "Being entered")
        blob = frappe.as_json(rows[0])
        self.assertNotIn("ledger 'X' missing", blob)

    def test_transport_copy_is_ownership_gated(self):
        frappe.set_user("Administrator")
        upsert_invoices(invoices=[{
            "guid": "inv-guid-lr1", "company": COMPANY,
            "invoice_no": "SNJ/T/LR1", "date": "2026-08-05", "party": PARTY_B,
            "amount": 1000.0, "alter_id": "2",
            "dispatched_through": "VRL Logistics", "lr_no": "LR-991",
            "lines": [],
        }])
        inv = frappe.db.get_value(
            "Tally Invoice", {"company": COMPANY, "invoice_no": "SNJ/T/LR1"},
            "name")
        frappe.db.set_value("Tally Invoice", inv, "transport_copy",
                            "/private/files/_test_lr.pdf")

        # A asking for B's LR answers exactly like a bill with no copy.
        frappe.set_user(USER_A)
        with self.assertRaises(frappe.DoesNotExistError):
            portal.download_transport_copy(invoice=inv)

    def test_sync_never_blanks_an_uploaded_transport_copy(self):
        frappe.set_user("Administrator")
        payload = [{
            "guid": "inv-guid-lr2", "company": COMPANY,
            "invoice_no": "SNJ/T/LR2", "date": "2026-08-06", "party": PARTY_A,
            "amount": 500.0, "alter_id": "1", "lines": [],
        }]
        upsert_invoices(invoices=payload)
        inv = frappe.db.get_value(
            "Tally Invoice", {"company": COMPANY, "invoice_no": "SNJ/T/LR2"},
            "name")
        frappe.db.set_value("Tally Invoice", inv, "transport_copy",
                            "/private/files/_test_lr2.pdf")
        # Re-sync with a NEW alter_id so the update path actually runs.
        payload[0]["alter_id"] = "2"
        upsert_invoices(invoices=payload)
        self.assertEqual(
            frappe.db.get_value("Tally Invoice", inv, "transport_copy"),
            "/private/files/_test_lr2.pdf")

    def test_reorder_repeats_own_order_only(self):
        frappe.set_user("Administrator")
        upsert_sales_orders(orders=[_so_payload(PARTY_A, "so-guid-ra",
                                                "SO/A/RA")])
        frappe.get_doc({
            "doctype": "Tally Item Rate",
            "item_name": "_Test Item Bra", "party": "", "company": COMPANY,
            "rate": 1200.0, "unit": "Doz", "discount": 50.0,
            "discount2": 20.0, "net_rate": 480.0,
        }).insert(ignore_permissions=True, ignore_if_duplicate=True)

        frappe.set_user(USER_B)
        with self.assertRaises(frappe.DoesNotExistError):
            portal.reorder(source_order="SO/A/RA", order_key="RE-B-1")

        frappe.set_user(USER_A)
        out = portal.reorder(source_order="SO/A/RA", order_key="RE-A-1")
        self.assertTrue(out["queued"])
        row = frappe.db.get_value("Tally Order Queue", "RE-A-1",
                                  ["party_ledger"], as_dict=True)
        self.assertEqual(row.party_ledger, PARTY_A)

    def test_balance_confirmation_freezes_and_dedupes(self):
        frappe.set_user(USER_A)
        first = portal.confirm_balance()
        again = portal.confirm_balance()
        self.assertTrue(first["confirmed"])
        self.assertFalse(again["confirmed"])
        self.assertEqual(first["balance"], 5000.0)   # the seeded ledger figure

    def test_claims_are_separate_from_orders_and_isolated(self):
        import base64
        payload = base64.b64encode(b"fake-claim-bytes").decode()
        frappe.set_user(USER_A)
        portal.upload_order_photo(client_key="CL-TEST-1", filename="dmg.jpg",
                                  content=payload, kind="Claim")
        self.assertEqual(portal.get_claims()["count"], 1)
        # A claim never shows up amid orders...
        self.assertEqual(
            [o for o in portal.get_orders()["orders"] if o.get("photo")], [])
        # ...and never in another party's claims.
        frappe.set_user(USER_B)
        self.assertEqual(portal.get_claims()["count"], 0)

    def test_payment_intimation_auto_confirms(self):
        frappe.set_user(USER_A)
        out = portal.submit_payment_intimation(payload={
            "amount": 5000.0, "mode": "NEFT", "utr_ref": "UTR123",
            "paid_on": "2026-08-04",
        })
        frappe.set_user("Administrator")
        upsert_receipts(receipts=[{
            "guid": "rcpt-guid-m1", "company": COMPANY, "voucher_number": "",
            "date": "2026-08-05", "party": PARTY_A, "amount": 5000.0,
            "mode": "_Test Bank", "instrument_no": "UTR123", "alter_id": "9",
            "allocations": [],
        }])
        portal.match_payment_intimations()
        status = frappe.db.get_value("Payment Intimation", out["name"],
                                     "status")
        self.assertEqual(status, "Confirmed")
