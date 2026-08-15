"""
tally_bridge.portal_auth — OTP login for distributors.

The ONLY guest-reachable surface of the portal, so it is deliberately small
and deliberately boring:

  * A distributor identifies with the mobile number their Tally ledger
    carries, or with their portal login email. Either way the code is
    DELIVERED to the grant's login email today — no SMS gateway exists yet
    (MD's decision, 2026-08-15). Setting `sms_provider` in site_config flips
    delivery to SMS through Frappe's SMS Settings with zero code changes:
    the flag names the provider (msg91, twilio, ...) purely for the
    operator's records; the gateway URL and parameters live in SMS Settings
    as always.
  * send_otp answers identically whether or not the identifier is known —
    a caller can never use it to discover which numbers are registered.
  * The code is 6 digits, hashed at rest (never stored, logged or returned —
    except in developer mode, so the flow is testable end to end), valid 10
    minutes, dead after 3 wrong attempts.
  * Rate limits: 3 sends per identifier per hour, 10 per IP per hour,
    counted from Portal OTP rows — server state, not client claims.
"""

from __future__ import annotations

import hashlib
import secrets

import frappe
from frappe.utils import add_to_date, cint, now_datetime

OTP_TTL_MINUTES = 10
MAX_VERIFY_ATTEMPTS = 3
MAX_SENDS_PER_ID_HOUR = 3
MAX_SENDS_PER_IP_HOUR = 10

# The neutral answer for every "no" case. One string, so the timing and the
# text can never disagree between paths.
_NEUTRAL = ("If this account is registered for the distributor portal, "
            "a sign-in code has been sent.")


def _hash(identifier: str, otp: str) -> str:
    # Salted with the site's own secret so a leaked table alone cannot be
    # brute-forced offline against a known identifier.
    salt = frappe.local.conf.get("encryption_key") or frappe.local.site
    return hashlib.sha256(f"{salt}:{identifier}:{otp}".encode()).hexdigest()


def _clean_identifier(value: str) -> str:
    """Normalise to either a lowercase email or the last 10 digits."""
    value = (value or "").strip()
    if "@" in value:
        return value.lower()
    digits = "".join(c for c in value if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _find_user(identifier: str):
    """
    Identifier -> (portal user, ledger docname, mobile), or (None, None, "").

    An email identifier matches a grant directly. A mobile identifier walks:
    a Tally Ledger in the pinned company whose mobile/phone ends with these
    10 digits -> an enabled DMS Portal Access grant for that party -> the
    grant's user. Every link is server-side state; nothing the caller sends
    can steer the resolution anywhere else.
    """
    if not identifier:
        return None, None, ""

    if "@" in identifier:
        grant = frappe.db.get_value(
            "DMS Portal Access", {"user_email": identifier, "enabled": 1},
            ["user_email", "party_ledger"], as_dict=True)
        if grant and frappe.db.get_value("User", grant.user_email, "enabled"):
            mobile = frappe.db.get_value("Tally Ledger", grant.party_ledger,
                                         "mobile") or ""
            return grant.user_email, grant.party_ledger, mobile
        return None, None, ""

    if len(identifier) < 10:
        return None, None, ""
    company = ""
    if frappe.db.exists("DocType", "DMS Settings"):
        company = frappe.db.get_single_value("DMS Settings", "default_company") or ""

    conds = ["(REPLACE(REPLACE(mobile, ' ', ''), '-', '') LIKE %(m)s "
             "OR REPLACE(REPLACE(phone, ' ', ''), '-', '') LIKE %(m)s)"]
    params = {"m": f"%{identifier}"}
    if company:
        conds.append("company = %(company)s")
        params["company"] = company
    ledgers = frappe.db.sql(
        f"SELECT name, ledger_name FROM `tabTally Ledger` "
        f"WHERE {' AND '.join(conds)} LIMIT 5",
        params, as_dict=True)

    for led in ledgers:
        grant = frappe.db.get_value(
            "DMS Portal Access",
            {"party_ledger": led.name, "enabled": 1}, "user_email")
        if not grant:
            # After an FY rollover the grant may point at another year's row
            # for the same party — match by name as the resolver does.
            same_party = frappe.get_all(
                "Tally Ledger", filters={"ledger_name": led.ledger_name},
                pluck="name")
            grant = frappe.db.get_value(
                "DMS Portal Access",
                {"party_ledger": ["in", same_party], "enabled": 1},
                "user_email")
        if grant and frappe.db.get_value("User", grant, "enabled"):
            return grant, led.name, identifier
    return None, None, ""


def _over_rate_limit(identifier: str, ip: str) -> bool:
    hour_ago = add_to_date(now_datetime(), hours=-1)
    by_id = frappe.db.count(
        "Portal OTP", {"mobile": identifier, "sent_at": [">", hour_ago]})
    if by_id >= MAX_SENDS_PER_ID_HOUR:
        return True
    if ip:
        by_ip = frappe.db.count(
            "Portal OTP", {"ip": ip, "sent_at": [">", hour_ago]})
        if by_ip >= MAX_SENDS_PER_IP_HOUR:
            return True
    return False


def _deliver(otp: str, user_email: str, mobile: str) -> None:
    """
    Email today; SMS the day site_config gains `sms_provider`.

    The flag is the switch, not the configuration: gateway URL and request
    parameters stay in Desk > SMS Settings, so onboarding MSG91 or Twilio is
    a settings exercise, never a deploy.
    """
    message = (f"{otp} is your SN Jain distributor portal sign-in code. "
               f"Valid {OTP_TTL_MINUTES} minutes.")
    provider = (frappe.conf.get("sms_provider") or "").strip()

    if provider:
        gateway = frappe.db.get_single_value("SMS Settings", "sms_gateway_url")
        if not gateway:
            frappe.throw(
                f"site_config sets sms_provider = {provider!r}, but Desk > "
                "SMS Settings has no gateway URL. Fill the gateway in there "
                "and OTP delivery switches to SMS — nothing else changes.")
        if not mobile:
            frappe.throw("This account's ledger has no mobile number in "
                         "Tally, so an SMS code cannot be sent. Ask the "
                         "office to add one.")
        from frappe.core.doctype.sms_settings.sms_settings import send_sms
        send_sms([mobile], message, success_msg=False)
        return

    frappe.sendmail(
        recipients=[user_email],
        subject="Your SN Jain portal sign-in code",
        message=(f"<p style='font-size:16px'>{message}</p>"
                 "<p>If you did not request this, ignore this mail — "
                 "the code dies on its own.</p>"),
        now=True,
    )


@frappe.whitelist(allow_guest=True, methods=["POST"])
def send_otp(identifier=None, mobile=None):
    """Send a sign-in code to a registered distributor."""
    identifier = _clean_identifier(identifier or mobile)
    ip = frappe.local.request_ip or ""
    if not identifier or ("@" not in identifier and len(identifier) < 10):
        return {"message": _NEUTRAL}
    if _over_rate_limit(identifier, ip):
        # Same neutral answer: a rate-limited attacker learns nothing, and a
        # rate-limited genuine user retries within the hour anyway.
        return {"message": _NEUTRAL}

    user, ledger, target_mobile = _find_user(identifier)

    # The attempt is recorded even when no user matched — the rate limiter
    # counts sends per identifier/IP, and unknown identifiers must burn the
    # same budget as known ones or probing stays free.
    otp = f"{secrets.randbelow(10**6):06d}"
    row = frappe.get_doc({
        "doctype": "Portal OTP",
        "mobile": identifier,
        "user": user or "",
        "party_ledger": ledger or "",
        "otp_hash": _hash(identifier, otp) if user else "",
        "expires_at": add_to_date(now_datetime(), minutes=OTP_TTL_MINUTES),
        "attempts": 0,
        "verified": 0,
        "sent_at": now_datetime(),
        "ip": ip,
    })
    row.insert(ignore_permissions=True)
    frappe.db.commit()

    out = {"message": _NEUTRAL}
    if user:
        _deliver(otp, user, target_mobile)
        if frappe.conf.get("developer_mode"):
            out["developer_otp"] = otp
    return out


@frappe.whitelist(allow_guest=True, methods=["POST"])
def verify_otp(identifier=None, mobile=None, otp=None):
    """Verify the code and log the distributor in."""
    identifier = _clean_identifier(identifier or mobile)
    otp = (str(otp) if otp is not None else "").strip()
    if not identifier or not otp:
        frappe.throw("The code did not match. Please try again.",
                     frappe.AuthenticationError)

    row = frappe.db.get_value(
        "Portal OTP",
        {"mobile": identifier, "verified": 0, "user": ["!=", ""]},
        ["name", "user", "otp_hash", "expires_at", "attempts"],
        order_by="sent_at desc", as_dict=True)

    generic = "The code did not match. Please try again."
    if not row or not row.otp_hash:
        frappe.throw(generic, frappe.AuthenticationError)
    if now_datetime() > row.expires_at:
        frappe.throw("That code has expired — request a new one.",
                     frappe.AuthenticationError)
    if cint(row.attempts) >= MAX_VERIFY_ATTEMPTS:
        frappe.throw("Too many wrong attempts — request a new code.",
                     frappe.AuthenticationError)

    if _hash(identifier, otp) != row.otp_hash:
        frappe.db.set_value("Portal OTP", row.name, "attempts",
                            cint(row.attempts) + 1, update_modified=False)
        frappe.db.commit()
        frappe.throw(generic, frappe.AuthenticationError)

    frappe.db.set_value("Portal OTP", row.name, "verified", 1,
                        update_modified=False)
    frappe.db.commit()

    frappe.local.login_manager.login_as(row.user)
    return {"logged_in": True, "home_page": "/portal"}


def purge_expired_otps():
    """Daily: authentication state is not history — old rows go."""
    frappe.db.delete("Portal OTP",
                     {"expires_at": ["<", add_to_date(now_datetime(), days=-2)]})
    frappe.db.commit()
