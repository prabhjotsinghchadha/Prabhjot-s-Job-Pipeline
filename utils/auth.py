"""
Firebase ID token verification — no service account required.

The frontend signs users in with the Firebase JS SDK and sends the ID token
as `Authorization: Bearer <token>`. We verify the RS256 signature against
Google's published securetoken certificates, then enforce the optional
email allowlist (ALLOWED_EMAILS) so a friends-only deployment stays closed
even though Firebase itself allows open signups.

Env:
  FIREBASE_PROJECT_ID  — enables multi-tenant mode; JWT audience + issuer.
  ALLOWED_EMAILS       — optional comma-separated allowlist (case-insensitive).
                         Empty/unset = any account in this Firebase project.
"""

import os
import time
import threading

import jwt
import requests
from cryptography import x509

_CERTS_URL = (
    "https://www.googleapis.com/robot/v1/metadata/x509/"
    "securetoken@system.gserviceaccount.com"
)

_cert_cache = {"keys": {}, "expires": 0.0}
_cert_lock = threading.Lock()


class AuthError(Exception):
    """Raised when a token is missing, invalid, expired, or not allowlisted."""


def _public_keys() -> dict:
    """Fetch and cache Google's securetoken signing certs (keyed by kid)."""
    with _cert_lock:
        if time.time() < _cert_cache["expires"] and _cert_cache["keys"]:
            return _cert_cache["keys"]
        resp = requests.get(_CERTS_URL, timeout=10)
        resp.raise_for_status()
        keys = {}
        for kid, pem in resp.json().items():
            cert = x509.load_pem_x509_certificate(pem.encode())
            keys[kid] = cert.public_key()
        # Honor Cache-Control max-age; default to 1 hour.
        max_age = 3600
        cc = resp.headers.get("Cache-Control", "")
        for part in cc.split(","):
            part = part.strip()
            if part.startswith("max-age="):
                try:
                    max_age = int(part.split("=", 1)[1])
                except ValueError:
                    pass
        _cert_cache["keys"] = keys
        _cert_cache["expires"] = time.time() + max_age
        return keys


def _allowed_emails() -> set:
    raw = os.environ.get("ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def verify_firebase_token(token: str) -> dict:
    """
    Verify a Firebase ID token. Returns {uid, email, name}.
    Raises AuthError on any failure.
    """
    project_id = os.environ.get("FIREBASE_PROJECT_ID")
    if not project_id:
        raise AuthError("Firebase auth is not configured")
    if not token:
        raise AuthError("Missing token")

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise AuthError(f"Malformed token: {e}")

    key = _public_keys().get(header.get("kid"))
    if key is None:
        # Certs rotate — force one refresh before giving up.
        _cert_cache["expires"] = 0.0
        key = _public_keys().get(header.get("kid"))
        if key is None:
            raise AuthError("Unknown signing key")

    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            audience=project_id,
            issuer=f"https://securetoken.google.com/{project_id}",
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("Token expired")
    except jwt.PyJWTError as e:
        raise AuthError(f"Invalid token: {e}")

    uid = claims.get("user_id") or claims.get("sub")
    if not uid:
        raise AuthError("Token has no uid")

    email = (claims.get("email") or "").lower()
    allowed = _allowed_emails()
    if allowed and email not in allowed:
        raise AuthError(f"{email or 'this account'} is not on the allowlist")

    return {"uid": uid, "email": email, "name": claims.get("name", "")}
