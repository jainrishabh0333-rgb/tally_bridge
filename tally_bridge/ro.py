"""
ro.py — short-path alias for the Reorder Report receiver.

Exists for one reason: TallyPrime's Export Settings URL field is
length-limited, and the natural path plus a long token overruns it.

    /api/method/tally_bridge.reorder_import.receive?token=<64 chars>   ~150
    /api/method/tally_bridge.ro.post?t=<8 chars>                        ~65

An earlier attempt put this alias in the app's __init__.py to save five more
characters. Don't do that: an app's __init__.py is imported during Frappe's
own bootstrap, and a whitelisted function there did not resolve —
`frappe.get_attr("tally_bridge.ro")` kept answering "module has no attribute".
A plain module is boring and works.

A short token is safe HERE specifically because receive() rate-limits to 60
posts an hour: eight hex characters is 4.3 billion possibilities, so guessing
one at that rate takes on the order of eight thousand years. Do not copy that
reasoning to an endpoint without a rate limit.
"""

import frappe


@frappe.whitelist(allow_guest=True, methods=["POST"])
def post(t: str | None = None, token: str | None = None):
    """Forward to reorder_import.receive. Same auth, limits and storage."""
    from tally_bridge.reorder_import import receive
    return receive(token=t or token)
