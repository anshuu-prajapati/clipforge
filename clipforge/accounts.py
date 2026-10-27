"""Social accounts a user attaches in Settings — any number per platform.

Added by hand (client ID / secret / refresh token for YouTube, access token for Instagram) or via
OAuth. Every save is verified against the platform, so the account shows its real channel name or
@username. Secrets are encrypted (connections.seal) and never returned to the browser.

Stored on the user document as  accounts: [ {id, platform, label, name, ext_id, kind, method,
created, default, client_id, client_secret*, refresh_token*, access_token*, refreshed, expires} ]
(* = encrypted)
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Callable, Optional

from .connections import seal, unseal
from .publish import GOOGLE_TOKEN_URI, PublishError, _ig, _ig_base, youtube_account

SECRET_FIELDS = ("client_secret", "refresh_token", "access_token")


def _tail(secret: Optional[str]) -> Optional[str]:
    return f"••••{secret[-4:]}" if secret and len(secret) > 8 else ("••••" if secret else None)


def public(acct: dict) -> dict:
    """What the browser may see: no secrets, only masked hints of what is stored."""
    hints = {}
    for f in SECRET_FIELDS:
        if acct.get(f):
            try:
                hints[f] = _tail(unseal(acct[f]))
            except PublishError:
                hints[f] = "unreadable — re-enter"
    out = {k: acct.get(k) for k in ("id", "platform", "label", "name", "ext_id", "kind", "method", "created",
                                     "default", "expires", "status", "last_error", "checked")}
    out["client_id"] = acct.get("client_id")
    out["stored"] = hints
    return out


# =========================================================================== YouTube
def youtube_creds(acct: dict):
    from google.oauth2.credentials import Credentials
    cid = acct.get("client_id") or os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    secret = unseal(acct["client_secret"]) if acct.get("client_secret") else os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not (cid and secret):
        raise PublishError("This YouTube account needs a Client ID and Client Secret — edit it in Settings.")
    return Credentials(None, refresh_token=unseal(acct["refresh_token"]), token_uri=GOOGLE_TOKEN_URI,
                       client_id=cid, client_secret=secret)


def verify_youtube(acct: dict) -> dict:
    """Log in with the stored credentials; returns {ext_id, name, warning?}."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    creds = youtube_creds(acct)
    try:
        creds.refresh(Request())
    except RefreshError as e:
        msg = str(e)
        if "invalid_client" in msg:
            raise PublishError("Google rejected the Client ID / Client Secret.") from None
        if "invalid_grant" in msg:
            raise PublishError("The refresh token is invalid or expired, or belongs to a different Client ID.") from None
        raise PublishError(f"Google refused these credentials: {msg[:160]}") from None
    try:
        info = youtube_account(creds) or {}
    except Exception:  # noqa: BLE001 - token may only carry the upload scope
        return {"ext_id": None, "name": "YouTube channel",
                "warning": "Logged in, but couldn't read the channel name (add the youtube.readonly scope)."}
    if not info.get("id"):
        raise PublishError("This Google account has no YouTube channel.")
    return {"ext_id": info["id"], "name": info["title"]}


# =========================================================================== Instagram
def verify_instagram(acct: dict) -> dict:
    """Instagram-Login token (IGAA…) or Facebook-Login token (EAA…) → {ext_id, name, kind}."""
    token = unseal(acct["access_token"])
    if token.startswith("IG"):
        me = _ig("GET", f"{_ig_base(token)}/me", params={"fields": "user_id,username,account_type", "access_token": token})
        if me.get("account_type") not in (None, "BUSINESS", "MEDIA_CREATOR", "CREATOR"):
            raise PublishError("Switch this Instagram account to a professional account (Creator or Business).")
        return {"ext_id": str(me.get("user_id") or me.get("id")), "name": "@" + me.get("username", "?"),
                "kind": "instagram_login"}
    uid = acct.get("ext_id")
    if not uid:
        pages = _ig("GET", f"{_ig_base(token)}/me/accounts",
                    params={"fields": "instagram_business_account{id,username}", "access_token": token})
        linked = [p["instagram_business_account"] for p in pages.get("data", []) if p.get("instagram_business_account")]
        if not linked:
            raise PublishError("No Instagram account is linked to a Facebook Page this token can see — "
                               "enter the Instagram account ID, or link the account to your Page.")
        uid = linked[0]["id"]
    try:
        d = _ig("GET", f"{_ig_base(token)}/{uid}", params={"fields": "username", "access_token": token})
    except PublishError as e:
        if acct.get("ext_id") and ("(#12)" in str(e) or "(#100)" in str(e) or "does not exist" in str(e)):
            raise PublishError(f"{uid} isn't an Instagram account ID (it looks like a Facebook user or Page ID). "
                               "Leave the Instagram account ID empty — ClipForge finds it — or use the ID "
                               "that starts with 1784.") from None
        raise
    return {"ext_id": uid, "name": "@" + d.get("username", "?"), "kind": "facebook_login"}


def instagram_token(acct: dict, save: Callable[[dict], None]) -> str:
    """Decrypted token; Instagram-Login tokens are refreshed weekly so the 60-day login never lapses."""
    token = unseal(acct["access_token"])
    if token.startswith("IG") and time.time() - acct.get("refreshed", 0) > 7 * 86400:
        try:
            r = _ig("GET", "https://graph.instagram.com/refresh_access_token",
                    params={"grant_type": "ig_refresh_token", "access_token": token})
            token, now = r["access_token"], time.time()
            save({"access_token": seal(token), "refreshed": now, "expires": now + int(r.get("expires_in", 5184000))})
        except PublishError:
            if acct.get("expires") and time.time() > acct["expires"]:
                raise PublishError("This Instagram login expired — paste a new access token in Settings.") from None
    return token


# =========================================================================== create / update
def build(platform: str, fields: dict, existing: Optional[dict] = None) -> dict:
    """New or updated account from form fields; blank secret fields keep what is stored. Verifies it."""
    if platform not in ("youtube", "instagram"):
        raise PublishError("Platform must be youtube or instagram.")
    acct = dict(existing or {"id": uuid.uuid4().hex[:12], "platform": platform, "method": "manual",
                             "created": time.time()})
    if fields.get("label") is not None:
        acct["label"] = fields["label"].strip()[:40]
    if platform == "youtube":
        if fields.get("client_id"):
            acct["client_id"] = fields["client_id"].strip()
        for f in ("client_secret", "refresh_token"):
            if fields.get(f):
                acct[f] = seal(fields[f].strip())
        if not acct.get("refresh_token"):
            raise PublishError("Enter the refresh token.")
        if not (acct.get("client_id") or os.environ.get("GOOGLE_OAUTH_CLIENT_ID")):
            raise PublishError("Enter the Client ID and Client Secret.")
        info = verify_youtube(acct)
    else:
        if fields.get("access_token"):
            acct["access_token"] = seal(fields["access_token"].strip())
            acct.pop("refreshed", None)
        if fields.get("ig_user_id") is not None:
            acct["ext_id"] = fields["ig_user_id"].strip() or None
        if not acct.get("access_token"):
            raise PublishError("Enter the access token.")
        info = verify_instagram(acct)
        acct.setdefault("refreshed", time.time())
    acct.update({k: v for k, v in info.items() if k != "warning"})
    acct.update(status="ok", last_error=info.get("warning"), checked=time.time())
    return acct


def from_env() -> list[tuple[str, dict]]:
    """The .env accounts (CLI setup) as form fields, for 'Import from .env'."""
    out = []
    if os.environ.get("YOUTUBE_REFRESH_TOKEN") and os.environ.get("YOUTUBE_CLIENT_ID"):
        out.append(("youtube", {"client_id": os.environ["YOUTUBE_CLIENT_ID"], "label": "from .env",
                                "client_secret": os.environ.get("YOUTUBE_CLIENT_SECRET", ""),
                                "refresh_token": os.environ["YOUTUBE_REFRESH_TOKEN"]}))
    if os.environ.get("INSTAGRAM_ACCESS_TOKEN"):
        out.append(("instagram", {"access_token": os.environ["INSTAGRAM_ACCESS_TOKEN"], "label": "from .env",
                                  "ig_user_id": os.environ.get("INSTAGRAM_USER_ID", "")}))
    return out



