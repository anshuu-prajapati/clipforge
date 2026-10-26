"""Email + password accounts with server-side sessions.

Passwords are hashed with scrypt (stdlib) and a per-user salt. Sessions are random tokens kept
in an HttpOnly cookie; MongoDB stores only their SHA-256, so a database leak can't be replayed.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from .db import db

COOKIE = "cf_session"
SESSION_DAYS = 30
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(ValueError):
    pass


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(h.hex(), digest)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def signup_mode() -> str:
    """open | code | closed — who may create accounts after the first (owner) one."""
    if os.environ.get("CLIPFORGE_SIGNUP", "").lower() == "closed":
        return "closed"
    return "code" if os.environ.get("CLIPFORGE_SIGNUP_CODE") else "open"


def create_user(email: str, password: str, name: str = "", code: str = "") -> dict:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise AuthError("Enter a valid email address.")
    if len(password) < 8:
        raise AuthError("Use a password with at least 8 characters.")
    first = db().users.estimated_document_count() == 0
    if not first:
        mode = signup_mode()
        if mode == "closed":
            raise AuthError("Sign-ups are closed. Ask the owner for an account.")
        if mode == "code" and not hmac.compare_digest(code.strip(), os.environ["CLIPFORGE_SIGNUP_CODE"]):
            raise AuthError("That invite code isn't right.")
    if db().users.find_one({"email": email}):
        raise AuthError("An account with this email already exists — log in instead.")
    user = {"_id": uuid.uuid4().hex[:16], "email": email, "name": name.strip()[:60] or email.split("@")[0],
            "password": hash_password(password), "role": "owner" if first else "member",
            "created": time.time(), "brand": None}
    db().users.insert_one(user)
    return user


def login(email: str, password: str) -> dict:
    user = db().users.find_one({"email": email.strip().lower()})
    if not user or not check_password(password, user["password"]):
        raise AuthError("Wrong email or password.")
    return user


def new_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    db().sessions.insert_one({"token": _token_hash(token), "user_id": user_id,
                              "expires": datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)})
    return token


def user_for(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    s = db().sessions.find_one({"token": _token_hash(token)})
    if not s or s["expires"].replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return None
    return db().users.find_one({"_id": s["user_id"]})


def end_session(token: Optional[str]) -> None:
    if token:
        db().sessions.delete_one({"token": _token_hash(token)})


def safe_user(user: dict) -> dict:
    return {"id": user["_id"], "email": user["email"], "name": user.get("name", ""), "role": user.get("role", "member")}
