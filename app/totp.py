"""TOTP (RFC 6238) enrolment and verification, plus one-time recovery codes."""
import hashlib
import hmac
import io
import secrets
import time

import pyotp
import segno

_PERIOD = 30
_WINDOW = 1  # accept one step either side for clock drift
# Lower-case base32 letters and digits minus the look-alikes l and o.
_RECOVERY_ALPHABET = "abcdefghijkmnpqrstuvwxyz234567"
_RECOVERY_COUNT = 10


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=issuer)


def qr_svg(uri: str) -> str:
    # Explicit dark-on-white (with quiet zone) so it scans in both page themes.
    # omitsize leaves only a viewBox, so the page's CSS can scale it to fill its box.
    out = io.BytesIO()
    segno.make(uri, error="m").save(
        out, kind="svg", xmldecl=False, svgns=True, scale=4, border=2,
        dark="#000", light="#fff", omitsize=True,
    )
    return out.getvalue().decode("utf-8")


def verify_code(secret: str, code: str, last_step: int | None, now: float | None = None) -> int | None:
    """Return the matched time-step, or None. Steps <= last_step are replays."""
    try:
        if not isinstance(code, str):
            return None
        code = code.replace(" ", "")
        if len(code) != 6 or not (code.isascii() and code.isdigit()):
            return None
        current = int((time.time() if now is None else now) // _PERIOD)
        totp = pyotp.TOTP(secret, interval=_PERIOD)
        matched = None
        for step in range(current - _WINDOW, current + _WINDOW + 1):
            # Compare every step so timing doesn't reveal which one matched.
            if hmac.compare_digest(totp.generate_otp(step), code) and (
                last_step is None or step > last_step
            ):
                matched = step
        return matched
    except Exception:  # malformed secret etc.: never raise on bad input
        return None


def new_recovery_codes() -> list[str]:
    codes: set[str] = set()
    while len(codes) < _RECOVERY_COUNT:
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        codes.add(f"{raw[:5]}-{raw[5:]}")
    return list(codes)


def hash_recovery_code(code: str) -> str:
    normal = code.lower().replace(" ", "").replace("-", "")
    return hashlib.sha256(normal.encode()).hexdigest()
