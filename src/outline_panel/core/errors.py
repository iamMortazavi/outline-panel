"""
Machine-readable error codes.

Every `HTTPException` in the panel carried a raw English sentence straight to
the UI, which made translating anything impossible: the frontend had no way to
tell "not enough credit" from "unknown server" except by matching prose.

`PanelError` adds a stable `code` and its `params` **beside** the English
`detail`, which is left exactly as it was. So:

  * an old client, a curl user or a test still reads `detail` and sees English;
  * the UI looks up `code` in its dictionary and falls back to `detail` when it
    has no translation — which is also what happens for every error not yet
    converted, so this could be adopted gradually instead of in one sweep.

Codes are dotted and scoped by area (`credit.insufficient`). Never reword one:
the string is the contract, and a translation file elsewhere depends on it.
"""

from __future__ import annotations

from fastapi import HTTPException


class PanelError(HTTPException):
    def __init__(self, status_code: int, code: str, detail: str,
                 **params) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.code = code
        self.params = params


def not_enough_credit(package: str, price: int, credit: int, currency: str):
    return PanelError(
        402, "credit.insufficient",
        f"Not enough credit: {package} costs {price:,} {currency} but you have "
        f"{credit:,} {currency}",
        package=package, price=price, credit=credit, currency=currency,
    )


def buy_a_package():
    return PanelError(403, "credit.must_buy_package",
                      "You buy from the price list — renew by picking a package")


def pick_a_package():
    return PanelError(400, "credit.pick_package", "Pick a package")


def unknown_package():
    return PanelError(404, "package.unknown", "Unknown package")


def unknown_server():
    return PanelError(404, "server.unknown", "Unknown server")


def unknown_key():
    return PanelError(404, "key.unknown", "Unknown key")


def unknown_admin():
    return PanelError(404, "admin.unknown", "Unknown admin")


def unknown_subscription():
    return PanelError(404, "sub.unknown", "Unknown subscription")


def no_permission():
    return PanelError(403, "auth.forbidden",
                      "You do not have permission for this")


def owner_only():
    return PanelError(403, "auth.owner_only", "Owner only")


def not_authenticated():
    return PanelError(401, "auth.required", "Not authenticated")


def session_expired():
    return PanelError(401, "auth.expired", "Session expired")


def bad_login():
    return PanelError(401, "auth.bad_credentials", "Wrong username or password")


def totp_required():
    return PanelError(401, "auth.totp_required", "2FA code required")


def bad_totp():
    return PanelError(401, "auth.totp_invalid", "Invalid 2FA code")


def totp_already_on():
    return PanelError(400, "auth.totp_already_on", "Two-factor is already on")


def totp_not_started():
    return PanelError(400, "auth.totp_not_started", "Start two-factor setup first")


def wrong_password():
    return PanelError(401, "auth.wrong_password", "Current password is wrong")


def too_many_attempts():
    return PanelError(429, "auth.rate_limited",
                      "Too many attempts. Try again in a few minutes.")


def too_many_requests():
    return PanelError(429, "sub.rate_limited",
                      "Too many requests — try again shortly")


def upstream(message: str):
    """An Outline server said no, or could not be reached."""
    return PanelError(502, "outline.unavailable", message, message=message)
