app_name = "tally_bridge"
app_title = "Tally Bridge"
app_publisher = "SN Jain Industries"
app_description = "Read-only mirror of TallyPrime data, queryable by Claude via MCP"
app_email = "md@snjainindustries.com"
app_license = "MIT"

# Roles that may read the mirrored data.
# The sync agent authenticates as a user with System Manager (write access).

required_apps = []

# Scheduled tasks — flag stale syncs so a silently dead agent gets noticed.
scheduler_events = {
    "hourly": [
        "tally_bridge.api.check_sync_freshness",
    ],
}

# Keep sync logs from growing unbounded.
doc_events = {}

# Uncomment to restrict API access further via fixtures/roles.
fixtures = []
