__version__ = "0.1.0"

import frappe


@frappe.whitelist(allow_guest=True, methods=["POST"])
def ro(t: str | None = None, token: str | None = None):
    """
    Short alias for reorder_import.receive — exists purely to fit in Tally.

    TallyPrime's Export Settings URL field is length-limited, and the natural
    path plus a 64-character token overruns it:

        /api/method/tally_bridge.reorder_import.receive?token=<64 chars>   ~150

    This trims that to about 88, which fits:

        /api/method/tally_bridge.ro?t=<24 chars>

    `t` and `token` both work. Nothing else differs — the same authentication,
    rate limit, size cap and storage path run underneath.
    """
    from tally_bridge.reorder_import import receive
    return receive(token=t or token)
