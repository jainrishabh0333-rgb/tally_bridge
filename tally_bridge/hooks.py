app_name = "tally_bridge"
app_title = "Tally Bridge"
app_publisher = "SN Jain Industries"
app_description = "Read-only mirror of TallyPrime data, queryable by Claude via MCP"
app_email = "md@snjainindustries.com"
app_license = "MIT"

# Roles that may read the mirrored data.
# The sync agent authenticates as a user with System Manager (write access).

required_apps = []

# Scheduled tasks — flag stale syncs so a silently dead agent gets noticed;
# confirm distributor payment intimations against mirrored receipts; keep
# harvested catalogue rates current; purge dead OTP rows.
scheduler_events = {
    "hourly": [
        "tally_bridge.api.check_sync_freshness",
        "tally_bridge.portal.match_payment_intimations",
    ],
    "daily": [
        "tally_bridge.api.refresh_item_rates",
        "tally_bridge.portal_auth.purge_expired_otps",
    ],
}

# Keep sync logs from growing unbounded.
doc_events = {}

# Uncomment to restrict API access further via fixtures/roles.
fixtures = []
