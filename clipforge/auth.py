"""Email + password accounts with server-side sessions.

Passwords are hashed with scrypt (stdlib) and a per-user salt. Sessions are random tokens kept
in an HttpOnly cookie; MongoDB stores only their SHA-256, so a database leak can't be replayed.

Hardening: failed logins are throttled per email and per IP; very common passwords are refused;
"remember me" sessions last 30 days and renew while in use, others expire after 12 idle hours;
every session records device + IP so users can see and end them.
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
SESSION_DAYS = 30                     # "remember me"
SHORT_HOURS = 12                      # otherwise: idle timeout
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_FAILS_EMAIL, MAX_FAILS_IP, LOCK_MINUTES = 5, 20, 15
COMMON = {"password", "password1", "password123", "12345678", "123456789", "1234567890", "qwerty123", "qwertyuiop",
          "11111111", "00000000", "abc12345", "iloveyou", "admin123", "welcome1", "letmein1", "football", "baseball",
          "sunshine", "princess", "dragon123", "monkey123", "passw0rd", "p@ssw0rd", "clipforge", "12341234",
          "asdfghjk", "zaq12wsx", "1q2w3e4r", "qwer1234", "superman", "trustno1", "abcd1234"}


class AuthError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


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


def password_problem(password: str, email: str = "") -> Optional[str]:
    if len(password) < 8:
        return "Use a password with at least 8 characters."
    if len(password) > 256:
        return "That password is too long."
    low = password.lower()
    if low in COMMON or len(set(low)) <= 2:
        return "That password is too easy to guess — pick something less common."
    local = email.split("@")[0].lower()
    if len(local) >= 4 and local in low:
        return "Don't use your email address in your password."
    return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def signup_mode() -> str:
    """open | code | closed — who may create accounts after the first (owner) one."""
    if os.environ.get("CLIPFORGE_SIGNUP", "").lower() == "closed":
        return "closed"
    return "code" if os.environ.get("CLIPFORGE_SIGNUP_CODE") else "open"


def create_user(email: str, password: str, name: str = "", code: str = "") -> dict:
    email = email.strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise AuthError("Enter a valid email address.")
    if problem := password_problem(password, email):
        raise AuthError(problem)
    first = db().users.estimated_document_count() == 0
    mode = signup_mode()
    # an invite code also guards the very first (owner) account — on a public server a stranger
    # must never be able to claim ownership before you do
    if mode == "code" and not hmac.compare_digest(code.strip(), os.environ["CLIPFORGE_SIGNUP_CODE"]):
        raise AuthError("That invite code isn't right.")
    if not first and mode == "closed":
        raise AuthError("Sign-ups are closed. Ask the owner for an account.")
    if db().users.find_one({"email": email}):
        raise AuthError("An account with this email already exists — log in instead.")
    user = {"_id": uuid.uuid4().hex[:16], "email": email, "name": name.strip()[:60] or email.split("@")[0],
            "password": hash_password(password), "role": "owner" if first else "member",
            "created": time.time(), "brand": None}
    db().users.insert_one(user)
    return user


# =========================================================================== brute-force throttle
def _fails(key: str, minutes: int = LOCK_MINUTES) -> int:
    return db().login_attempts.count_documents({"key": key, "at": {"$gte": _now() - timedelta(minutes=minutes)}})


def _fail(*keys: str) -> None:
    for k in keys:
        db().login_attempts.insert_one({"key": k, "at": _now()})


def login(email: str, password: str, ip: str = "") -> dict:
    email = email.strip().lower()
    ekey, ikey = f"email:{email}", f"ip:{ip}"
    if _fails(ekey) >= MAX_FAILS_EMAIL or (ip and _fails(ikey) >= MAX_FAILS_IP):
        raise AuthError(f"Too many failed attempts. Try again in {LOCK_MINUTES} minutes.")
    user = db().users.find_one({"email": email})
    if not user or not check_password(password, user["password"]):
        _fail(ekey, *([ikey] if ip else []))
        raise AuthError("Wrong email or password.")
    db().login_attempts.delete_many({"key": ekey})
    return user


# =========================================================================== password reset
RESET_MINUTES = 30
MAX_RESETS_EMAIL, MAX_RESETS_IP = 3, 10            # per hour


def create_reset(email: str, ip: str = "") -> Optional[tuple[dict, str]]:
    """(user, one-time token) — or None for unknown emails / too many requests (caller answers the same)."""
    email = email.strip().lower()
    ekey, ikey = f"reset:{email}", f"resetip:{ip}"
    if _fails(ekey, 60) >= MAX_RESETS_EMAIL or (ip and _fails(ikey, 60) >= MAX_RESETS_IP):
        return None
    _fail(ekey, *([ikey] if ip else []))
    user = db().users.find_one({"email": email})
    if not user:
        return None
    token = secrets.token_urlsafe(32)
    now = _now()
    db().password_resets.insert_one({"token": _token_hash(token), "user_id": user["_id"], "created": now,
                                     "expires": now + timedelta(minutes=RESET_MINUTES), "ip": ip[:64]})
    return user, token


def reset_user(token: str) -> Optional[dict]:
    r = db().password_resets.find_one({"token": _token_hash(token or "")})
    if not r or _utc(r["expires"]) < _now():
        return None
    return db().users.find_one({"_id": r["user_id"]})


def use_reset(token: str, new_password: str) -> dict:
    """Set the new password, burn every reset link, and end every session of the account."""
    user = reset_user(token)
    if not user:
        raise AuthError("This reset link is invalid or has expired — request a new one.")
    if problem := password_problem(new_password, user["email"]):
        raise AuthError(problem)
    db().users.update_one({"_id": user["_id"]}, {"$set": {"password": hash_password(new_password)}})
    db().password_resets.delete_many({"user_id": user["_id"]})
    db().sessions.delete_many({"user_id": user["_id"]})
    db().login_attempts.delete_many({"key": f"email:{user['email']}"})   # a locked-out owner can log in again
    return user


# =========================================================================== sessions
def _ttl(remember: bool) -> timedelta:
    return timedelta(days=SESSION_DAYS) if remember else timedelta(hours=SHORT_HOURS)


def new_session(user_id: str, remember: bool = True, ip: str = "", agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    db().sessions.insert_one({"token": _token_hash(token), "sid": secrets.token_hex(6), "user_id": user_id,
                              "remember": remember, "created": now, "last_seen": now, "expires": now + _ttl(remember),
                              "ip": ip[:64], "agent": agent[:200]})
    return token


def user_for(token: Optional[str], ip: str = "") -> Optional[dict]:
    """Session → user. Sliding expiry: an active session keeps renewing."""
    if not token:
        return None
    s = db().sessions.find_one({"token": _token_hash(token)})
    now = _now()
    if not s or _utc(s["expires"]) < now:
        return None
    if now - _utc(s.get("last_seen", s["expires"] - _ttl(True))) > timedelta(minutes=5):
        db().sessions.update_one({"_id": s["_id"]}, {"$set": {"last_seen": now, "expires": now + _ttl(s.get("remember", True)),
                                                             **({"ip": ip[:64]} if ip else {})}})
    return db().users.find_one({"_id": s["user_id"]})


def end_session(token: Optional[str]) -> None:
    if token:
        db().sessions.delete_one({"token": _token_hash(token)})


def sessions_of(user_id: str, current_token: Optional[str]) -> list[dict]:
    cur = _token_hash(current_token) if current_token else None
    out = []
    for s in db().sessions.find({"user_id": user_id}).sort("last_seen", -1):
        if _utc(s["expires"]) < _now():
            continue
        out.append({"id": s.get("sid") or str(s["_id"]), "current": s["token"] == cur, "ip": s.get("ip", ""),
                    "agent": s.get("agent", ""), "remember": s.get("remember", True),
                    "created": _utc(s.get("created", s["expires"])).timestamp(),
                    "last_seen": _utc(s.get("last_seen", s["expires"])).timestamp(),
                    "expires": _utc(s["expires"]).timestamp()})
    return out


def end_session_id(user_id: str, sid: str) -> bool:
    return db().sessions.delete_one({"user_id": user_id, "sid": sid}).deleted_count == 1


def safe_user(user: dict) -> dict:
    return {"id": user["_id"], "email": user["email"], "name": user.get("name", ""), "role": user.get("role", "member")}
