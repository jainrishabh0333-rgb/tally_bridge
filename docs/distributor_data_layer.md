# Distributor data layer — schema proposal

What a distributor needs, where it comes from in Tally, and where it lands in
Frappe. Everything below was measured against the live book
(`SN JAIN INDUSTRIES PVT LTD - (26-27)`, TallyPrime Edit Log 7.0) on
2026-08-15 — not assumed. Where the book does not hold something the brief
asked for, that is recorded here rather than papered over.

## What the live book actually contains

| Brief asks for | Measured reality | Consequence |
|---|---|---|
| Credit limit + credit period per party | `CREDITLIMIT` exports, but is **empty on all 123 ledgers under AGENT RK**; `BILLCREDITPERIOD` is `0` on all of them | Fields are mirrored so they light up the day someone sets them. `portal.get_summary()` returns `credit_limit: null` and says "not set in Tally" — it never invents a limit. |
| Delivery notes | **No Delivery Note voucher type exists.** The only challans are `Delivery Challan(Branch Transfer)` / `Receipt Challan(Branch Transfer)` (13 + 12 in a fortnight), which are internal branch movements, not customer deliveries | Goods reach a distributor as a **Sales invoice**. Delivered-vs-pending is computed from invoice lines joined to the order. The `Tally Delivery Note` doctype and its fetcher still ship, driven by a configurable voucher-type list, so the day the book starts issuing them the portal picks them up. |
| Price-list rate for the distributor's price level | `PRICELEVEL` is empty on every ledger sampled; no price-level masters in use | Catalogue rates come from `Tally Item Rate`, harvested from the party's own recent Sales/Sales Order lines. Honest name, honest source. |
| Sales order line due date | Present, but **per size** (`BATCHALLOCATIONS.ORDERDUEDATE`), not per item line | Order lines are stored one row per (item, size) — which is also how orders are placed here. |
| Receipt voucher number | **Empty on all 25 receipts sampled** (`NUMBERINGSTYLE` = `None`) | Receipts are keyed on GUID and shown by date + amount + bank, never by a voucher number that does not exist. |

Two joins are confirmed to exist in the export and carry the whole design:

* A Sales invoice carries the order number at header level (`REFERENCE`) **and**
  per size (`ALLINVENTORYENTRIES.BATCHALLOCATIONS.ORDERNO`). That is how
  delivered-vs-pending is computed per size.
* A Receipt's `BILLALLOCATIONS` carry `NAME` (the bill ref), `BILLTYPE`
  (`Agst Ref`) and `AMOUNT` — 17 of 52 allocations in the sample were against a
  named bill, the rest on account. That is how a payment ties to a bill.

## Naming conventions (deliberately not the brief's)

The brief asks for `tally_guid` / `last_synced_at`. Every existing doctype in
this app uses `guid` / `last_synced` / `company`, and `api.py` keys on them.
Consistency with the app wins: new doctypes use **`guid`, `last_synced`,
`company`**, and the brief's requirement — that every synced row carries its
Tally identity, its sync time and its company file — is met.

Docnames are always `_docname(company, natural_key, guid)`, i.e.
`company::guid`. This is not optional: Tally reuses GUIDs across financial-year
company files, and an unscoped docname silently overwrote 633 ledgers once
already.

## Doctypes

### Extended: `Tally Ledger`

New fields, all optional, all from the Ledger collection (every method below was
accepted by this build — verified, no `LINEERROR`):

| Field | Tally source | Note |
|---|---|---|
| `credit_limit` (Currency) | `CREDITLIMIT` | empty in this book today |
| `credit_days` (Int) | parsed from `BILLCREDITPERIOD` | 0 in this book today |
| `credit_period` (Data) | `BILLCREDITPERIOD` verbatim | |
| `address` (Small Text) | `ADDRESS.LIST` → lines joined | multi-line list |
| `state` / `pincode` / `country` | `LEDGERSTATENAME` / `PINCODE` / `COUNTRYNAME` | |
| `mobile` (Data) | `LEDGERMOBILE` | populated on 63 of 123 sampled |
| `mailing_name` (Data) | `MAILINGNAME` | |
| `gst_registration_type` (Data) | `GSTREGISTRATIONTYPE` | |
| `price_level` (Data) | `PRICELEVEL` | empty in this book today |
| `agent` (Data) | resolved, see below | |
| `agent_source` (Select: group / udf / none) | resolved | so the portal never re-derives it |

**Agent resolution.** The brief says the agent is held "via ledger group or a
UDF — inspect and handle both". Inspected: the ledger carries **no UDF tags at
all**; the agent is the immediate group (`PARENT` = `AGENT RK`) sitting under
`Sundry Debtors`. So `agent` = the immediate group when the resolved
`primary_group` is `Sundry Debtors` and the group is not itself
`Sundry Debtors`; when a UDF named `Agent`/`SalesMan`/`Salesperson` is present
it wins, and `agent_source` records which of the two answered.

### New: `Tally Sales Order` + `Tally Sales Order Line`

One parent per Sales Order voucher, one **line per (item, size)** — sizes are
batch allocations, and this is the granularity both Tally and the order pad use.

Parent: `company`, `guid`, `voucher_number`, `voucher_date`, `party`,
`reference`, `narration`, `amount`, `is_cancelled`, `is_optional`,
`order_status`, `order_key`, `queue_ref`, `alter_id`, `last_synced`.

`order_key` is the join back to `Tally Order Queue`: the importer writes the key
into the voucher's narration at punch time, and the upsert lifts it back out
(`order_key=<key>`), so a portal-placed order and its Tally voucher become one
row in `portal.get_orders()`. A voucher punched by hand in Tally simply has no
key, which is correct — it was not placed through the portal.

Line: `item_name`, `size_batch`, `godown`, `qty`, `unit`, `billed_qty`, `rate`,
`rate_unit`, `discount`, `discount2`, `amount`, `due_date`, `preclosed_qty`,
`delivered_qty`, `pending_qty`.

`discount2` exists because this book's discount chain is 50 **then** 20: Tally
writes only the first step into `BATCHDISCOUNT` and the second into a UDF
(`BatchDiscount2`). Storing one of them and calling it "the discount" would
misprice every line by 20%.

`delivered_qty` / `pending_qty` are **computed server-side** on each sync from
matching invoice (and delivery-note) lines — never trusted from Tally, which has
no reliable per-size fulfilment figure in this book.

### New: `Tally Invoice` + `Tally Invoice Line`

Chosen over extending `Tally Bill`, and the two are not duplicates:

* `Tally Bill` is a **destructive snapshot of what is still unpaid**, rebuilt
  every sync from the Bills collection. A paid invoice vanishes from it by
  design, and it contains opening bills that predate any mirrored voucher.
* `Tally Invoice` is the **permanent document history** with GST breakup, line
  items and the order it fulfils. It is what a statement, a PDF and a
  delivered-vs-pending calculation need.

Parent: `company`, `guid`, `invoice_no`, `invoice_date`, `party`, `reference`,
`amount` (debit-positive), `taxable_value`, `cgst`, `sgst`, `igst`, `cess`,
`round_off`, `bill_refs`, `is_cancelled`, `alter_id`, `last_synced`.

The GST breakup is derived by classifying the voucher's ledger entries by name
(`IGST Output`, `CGST…`, `SGST…`, `Rounded Off`, sales ledgers) — this book uses
`Sale Central 5%` / `Sale Local 5%` and a single `IGST Output` line, all
verified in the export.

Line: `item_name`, `size_batch`, `godown`, `qty`, `unit`, `rate`, `discount`,
`discount2`, `amount`, `order_no`, `order_due_date`.

### New: `Tally Delivery Note` + `Tally Delivery Note Line`

Same shape as the invoice, plus `vehicle_no`, `lr_no`, `dispatched_through`,
`destination`, `order_ref`. Fed by a configurable list of voucher type names
(`[orders].delivery_types`, default `Delivery Note`, `Delivery Challan`), which
matches nothing in this book today and therefore mirrors nothing — by design,
not by accident.

### New: `Tally Receipt` + `Tally Receipt Allocation`

Parent: `company`, `guid`, `voucher_number` (usually empty here),
`voucher_date`, `party`, `amount`, `mode` (the contra ledger — bank or cash
name), `instrument_no`, `instrument_date`, `narration`, `alter_id`,
`last_synced`.
Child: `bill_ref`, `bill_type`, `amount`. An allocation with no `bill_ref` is an
on-account receipt and is labelled as such rather than dropped.

### New: `Tally Stock Batch`

`company`, `item_name`, `batch_name` (the size), `godown`, `closing_qty`,
`closing_qty_unit`, `closing_qty_raw`, `closing_value`, `last_synced`.
Natural key: `company|item|godown|batch`. This is what lets the catalogue show
"available in 28, 30, 32" without exposing company stock — the portal buckets it
as in / low / out and never returns the number.

### New: `Tally Item Rate`

`company`, `item_name`, `party`, `unit`, `rate`, `discount`, `discount2`,
`net_rate`, `source_voucher`, `source_date`, `last_synced`.

Rates in this book live only in voucher history — there is no rate master and no
price level. Rows are harvested from recent Sales / Sales Order lines: one
book-wide row per item (`party` empty, the most-supported recent rate) plus a
party-specific row when that party has actually been charged differently. The
portal serves the party row when it exists, else the book row.

### New: `Payment Intimation` (+ `Payment Intimation Bill`) and `Distributor Suggestion`

Portal-written, never synced from Tally. Payment Intimation carries
`party_ledger`, `company`, `amount`, `mode`, `utr_ref`, `paid_on`, selected
bills, `status` (Pending Confirmation / Confirmed / Rejected), `matched_receipt`
and `matched_at`. A scheduled job matches Pending rows against incoming
`Tally Receipt` rows for the same party by amount (± ₹1) and, where present,
instrument/UTR reference, then flips the row to Confirmed.

### New: `Portal OTP`

`mobile`, `user`, `otp_hash` (never the code itself), `expires_at`, `attempts`,
`sent_at`, `ip`. Rate limiting needs server-side state; this is it.

## Party resolution — the one rule

The caller's party is resolved **once per request** from the session, through
the existing `DMS Portal Access` grant (`user_email` → `party_ledger`,
`enabled = 1`). It is never accepted as a parameter, never read from a header,
never inferred from a payload. Reusing the grant rather than adding a second
mapping doctype is deliberate: two mappings are two places a party can leak, and
the grant is already hardened (company-pinned, self-relinking across financial
years).

`tally_bridge` does not depend on `snj_dms` at import time — if the grant
doctype is absent the resolver **throws**, it does not fall through to an
unfiltered query.
