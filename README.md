# Tally Bridge (Frappe app)

Stores a read-only mirror of TallyPrime data and exposes it for querying.

## DocTypes

| DocType | Keyed on | Purpose |
|---|---|---|
| `Tally Ledger` | ledger name | Masters with balances, group, GSTIN, contact |
| `Tally Voucher` | Tally GUID | Transactions with a child table of entries |
| `Tally Voucher Entry` | — | Per-ledger debit/credit lines of a voucher |
| `Tally Sync Log` | auto | One record per sync run, success or failure |

## API

**Ingestion** (System Manager only — used by the sync agent):
`upsert_ledgers`, `upsert_vouchers`, `log_sync`, `get_sync_state`

**Analytics** (read-only — used by the MCP server):
`outstanding`, `ledger_statement`, `day_book`, `trial_balance`,
`summary_by_voucher_type`, `search_ledgers`, `unbalanced_vouchers`,
`sync_health`

All endpoints are parameterised against SQL injection and capped at
`MAX_ROWS = 2000`.

## Install

```bash
bench get-app tally_bridge /path/to/tally_bridge
bench --site yoursite install-app tally_bridge
bench --site yoursite migrate
```

## Configuration

If your chart of accounts uses non-default group names for parties, edit
`RECEIVABLE_GROUPS` / `PAYABLE_GROUPS` at the top of `api.py`.

## Requirements

Frappe v15. On Frappe Cloud, custom apps require a **private bench group**,
which needs a site plan of USD 25/month or higher with a payment method on
file — it is not available on the free trial or the $5/$10 shared plans.
Self-hosted benches have no such restriction.
