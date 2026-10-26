"""Per-user YouTube / Instagram connections, set up in the browser with OAuth.

Each user connects their own accounts from Settings; tokens are encrypted (Fernet) before they
are stored on the user's MongoDB document, with the key in CLIPFORGE_SECRET_KEY (generated into
.env on first use).

YouTube    Google OAuth client of type "Web application":
           GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET,
           authorized redirect URI  <PUBLIC_URL>/connect/youtube/callback
Instagram  Meta app → Instagram → "API setup with Instagram login" (Business login):
           INSTAGRAM_APP_ID, INSTAGRAM_APP_SECRET,
           OAuth redirect URI       <PUBLIC_URL>/connect/instagram/callback   (Meta requires https)
           Instagram-Login tokens publish from a public video URL, which Cloudinary provides.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional
from urllib.parse import urlencode

import httpx

from . import env
from .publish import GOOGLE_TOKEN_URI, IG_VERSION, YT_SCOPES, PublishError, _ig, youtube_account

IG_SCOPES = "instagram_business_basic,instagram_business_content_publish"


# =========================================================================== encryption at rest
def _fernet():
    from cryptography.fernet import Fernet
    key = os.environ.get("CLIPFORGE_SECRET_KEY")
    if not key:
        key = Fernet.generate_key().decode()
        env.save("CLIPFORGE_SECRET_KEY", key)    # keep it: without it stored tokens can't be read
    return Fernet(key.encode())


def seal(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def unseal(token: str) -> str:
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise PublishError("Stored login can't be decrypted (CLIPFORGE_SECRET_KEY changed) — reconnect "
                           "the account in Settings.") from None


# =========================================================================== YouTube (Google)
def google_client() -> Optional[tuple[str, str]]:
    cid, secret = os.environ.get("GOOGLE_OAUTH_CLIENT_ID"), os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    return (cid, secret) if cid and secret else None


def _google_flow(redirect_uri: str, code_verifier: Optional[str] = None):
    from google_auth_oauthlib.flow import Flow
    cid, secret = google_client()
    if redirect_uri.startswith("http://"):        # local development: oauthlib refuses plain http otherwise
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    return Flow.from_client_config(
        {"web": {"client_id": cid, "client_secret": secret, "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                 "token_uri": GOOGLE_TOKEN_URI, "redirect_uris": [redirect_uri]}},
        scopes=YT_SCOPES, redirect_uri=redirect_uri, code_verifier=code_verifier,
        autogenerate_code_verifier=code_verifier is None)


def google_auth_url(redirect_uri: str, state: str) -> tuple[str, str]:
    """(consent URL, PKCE code verifier to keep for the callback)."""
    flow = _google_flow(redirect_uri)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true", state=state)
    return url, flow.code_verifier


def google_finish(redirect_uri: str, code: str, code_verifier: str) -> dict:
    flow = _google_flow(redirect_uri, code_verifier)
    flow.fetch_token(code=code)
    creds = flow.credentials
    if not creds.refresh_token:
        raise PublishError("Google did not grant offline access — try connecting again.")
    acct = youtube_account(creds) or {}
    if not acct.get("id"):
        raise PublishError("This Google account has no YouTube channel. Create one at youtube.com first.")
    return {"refresh_token": seal(creds.refresh_token), "id": acct["id"], "name": acct["title"],
            "connected_at": time.time()}


def youtube_creds(conn: dict):
    from google.oauth2.credentials import Credentials
    cid, secret = google_client() or (None, None)
    if not cid:
        raise PublishError("YouTube sign-in isn't configured on this server (GOOGLE_OAUTH_CLIENT_ID).")
    return Credentials(None, refresh_token=unseal(conn["refresh_token"]), token_uri=GOOGLE_TOKEN_URI,
                       client_id=cid, client_secret=secret, scopes=YT_SCOPES)


def google_revoke(conn: dict) -> None:
    try:
        httpx.post("https://oauth2.googleapis.com/revoke", params={"token": unseal(conn["refresh_token"])}, timeout=15)
    except Exception:  # noqa: BLE001 - already revoked / offline: forgetting it locally is what matters
        pass


# =========================================================================== Instagram (Business login)
def instagram_app() -> Optional[tuple[str, str]]:
    aid, secret = os.environ.get("INSTAGRAM_APP_ID"), os.environ.get("INSTAGRAM_APP_SECRET")
    return (aid, secret) if aid and secret else None


def instagram_auth_url(redirect_uri: str, state: str) -> str:
    aid, _ = instagram_app()
    return "https://www.instagram.com/oauth/authorize?" + urlencode(
        {"client_id": aid, "redirect_uri": redirect_uri, "response_type": "code", "scope": IG_SCOPES,
         "state": state, "enable_fb_login": "0", "force_reauth": "true"})


def instagram_finish(redirect_uri: str, code: str) -> dict:
    aid, secret = instagram_app()
    short = _ig("POST", "https://api.instagram.com/oauth/access_token",
                data={"client_id": aid, "client_secret": secret, "grant_type": "authorization_code",
                      "redirect_uri": redirect_uri, "code": code.split("#")[0]})
    long = _ig("GET", "https://graph.instagram.com/access_token",
               params={"grant_type": "ig_exchange_token", "client_secret": secret, "access_token": short["access_token"]})
    me = _ig("GET", f"https://graph.instagram.com/{IG_VERSION}/me",
             params={"fields": "user_id,username,account_type", "access_token": long["access_token"]})
    if me.get("account_type") not in ("BUSINESS", "MEDIA_CREATOR", "CREATOR"):
        raise PublishError("Switch this Instagram account to a professional account (Creator or Business) first.")
    now = time.time()
    return {"token": seal(long["access_token"]), "id": str(me.get("user_id") or me.get("id")),
            "name": "@" + me.get("username", "?"), "connected_at": now, "refreshed": now,
            "expires": now + int(long.get("expires_in", 60 * 86400))}


def instagram_token(conn: dict, save: Callable[[dict], None]) -> str:
    """Decrypted token; refreshed (and saved) weekly so the 60-day token never runs out."""
    token = unseal(conn["token"])
    if time.time() - conn.get("refreshed", 0) > 7 * 86400:
        try:
            r = _ig("GET", "https://graph.instagram.com/refresh_access_token",
                    params={"grant_type": "ig_refresh_token", "access_token": token})
            token = r["access_token"]
            now = time.time()
            save({**conn, "token": seal(token), "refreshed": now, "expires": now + int(r.get("expires_in", 5184000))})
        except PublishError:
            if time.time() > conn.get("expires", 0):
                raise PublishError("Your Instagram connection expired — reconnect it in Settings.") from None
    return token
