"""Upload clips to YouTube Shorts and Instagram Reels.

All credentials live in .env (git-ignored):

YouTube   YouTube Data API v3 with OAuth (Google Cloud "Desktop app" client):
          YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET. The first connect opens a browser for consent
          and writes YOUTUBE_REFRESH_TOKEN to .env; access tokens are minted from it on demand.
          Note: projects that have not passed Google's API audit can only upload *private* videos.
Instagram Instagram API with Instagram Login (token "IGAA…", graph.instagram.com) or Facebook Login
          (token "EAA…" + INSTAGRAM_USER_ID, graph.facebook.com): INSTAGRAM_ACCESS_TOKEN.
          Reels are sent with resumable upload straight from disk — no public hosting needed.
          Long-lived tokens are refreshed weekly and written back to .env.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx

from . import env

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
             "https://www.googleapis.com/auth/youtube.readonly"]
IG_VERSION = os.environ.get("INSTAGRAM_API_VERSION", "v24.0")

Progress = Callable[[str, float], None]          # (status text, 0-1)


class PublishError(RuntimeError):
    pass


# =========================================================================== YouTube
def _yt_client() -> Optional[tuple[str, str]]:
    cid, secret = os.environ.get("YOUTUBE_CLIENT_ID"), os.environ.get("YOUTUBE_CLIENT_SECRET")
    return (cid, secret) if cid and secret else None


_yt_cache: dict = {}


def _yt_creds(interactive: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    client, refresh = _yt_client(), os.environ.get("YOUTUBE_REFRESH_TOKEN")
    if client and refresh:
        creds = _yt_cache.get(refresh)
        if creds is None:
            creds = Credentials(None, refresh_token=refresh, token_uri=GOOGLE_TOKEN_URI,
                                client_id=client[0], client_secret=client[1], scopes=YT_SCOPES)
        if not creds.valid:
            try:
                creds.refresh(Request())
            except Exception:  # noqa: BLE001 - revoked or client changed → reconnect
                creds = None
        if creds:
            _yt_cache[refresh] = creds
            return creds
    if not interactive:
        return None
    if not client:
        raise PublishError("Add YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET to .env")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_config(
        {"installed": {"client_id": client[0], "client_secret": client[1], "redirect_uris": ["http://localhost"],
                       "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": GOOGLE_TOKEN_URI}},
        YT_SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, timeout_seconds=300, prompt="consent",
                                  success_message="ClipForge is connected to YouTube. You can close this tab.")
    if not creds.refresh_token:
        raise PublishError("Google did not return a refresh token — remove ClipForge from "
                           "myaccount.google.com/permissions and connect again.")
    env.save("YOUTUBE_REFRESH_TOKEN", creds.refresh_token)
    _yt_cache[creds.refresh_token] = creds
    return creds


def youtube_connect() -> dict:
    """Interactive OAuth (opens the browser on this machine)."""
    _yt_creds(interactive=True)
    return youtube_account() or {}


def youtube_account() -> Optional[dict]:
    creds = _yt_creds()
    if not creds:
        return None
    from googleapiclient.discovery import build
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not items:
        return {"id": None, "title": "(no channel on this Google account)"}
    return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}


def youtube_upload(video: Path, title: str, description: str, tags: list[str], privacy: str = "private",
                   publish_at: Optional[datetime] = None, thumbnail: Optional[Path] = None,
                   progress: Progress = lambda s, p: None) -> dict:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    creds = _yt_creds()
    if not creds:
        raise PublishError("YouTube is not connected.")
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
    if publish_at:                                # YouTube schedules natively: private until publishAt
        status = {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                  "publishAt": publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    body = {"snippet": {"title": title[:100], "description": description[:4900], "tags": tags[:15],
                        "categoryId": "22"}, "status": status}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(video), mimetype="video/mp4",
                                                        chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    try:
        while resp is None:
            st, resp = req.next_chunk()
            if st:
                progress("uploading", st.progress() * 0.9)
    except HttpError as e:
        raise PublishError(_google_error(e)) from None
    vid = resp["id"]
    note = None
    if thumbnail and thumbnail.exists():
        progress("setting thumbnail", 0.95)
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumbnail), mimetype="image/jpeg")).execute()
        except HttpError as e:                    # needs a phone-verified channel; not fatal
            note = "thumbnail not set: " + _google_error(e)
    got = resp.get("status", {}).get("privacyStatus", privacy)
    if got == "private" and privacy != "private" and not publish_at:
        note = ((note + "; ") if note else "") + ("YouTube kept the video private — API projects that have not "
                                                  "passed Google's audit can only upload private videos.")
    return {"id": vid, "url": f"https://www.youtube.com/shorts/{vid}", "privacy": got, "note": note}


def _google_error(e) -> str:
    try:
        d = json.loads(e.content)
        return d["error"]["message"]
    except Exception:  # noqa: BLE001
        return str(e)


# =========================================================================== Instagram
def _ig_token() -> Optional[str]:
    return os.environ.get("INSTAGRAM_ACCESS_TOKEN") or None


def _ig_base(token: str) -> str:
    return f"https://graph.{'instagram' if token.startswith('IG') else 'facebook'}.com/{IG_VERSION}"


def _ig(method: str, url: str, **kw) -> dict:
    r = httpx.request(method, url, timeout=120, **kw)
    try:
        d = r.json()
    except ValueError:
        raise PublishError(f"Instagram returned HTTP {r.status_code}: {r.text[:200]}") from None
    if r.status_code >= 400 or "error" in d:
        err = d.get("error", {})
        msg = err.get("error_user_msg") or err.get("message") or str(d)[:300]
        if "version" in msg.lower() and "deprecated" in msg.lower():
            msg += " — set INSTAGRAM_API_VERSION in .env to a current Graph API version."
        raise PublishError(f"Instagram: {msg}")
    return d


def _ig_refresh(token: str) -> str:
    """Long-lived Instagram Login tokens last 60 days; refresh weekly so they never expire."""
    if not token.startswith("IG"):
        return token
    last = float(os.environ.get("INSTAGRAM_TOKEN_REFRESHED") or 0)
    if time.time() - last < 7 * 86400:
        return token
    try:
        r = _ig("GET", "https://graph.instagram.com/refresh_access_token",
                params={"grant_type": "ig_refresh_token", "access_token": token})
    except PublishError:
        env.save("INSTAGRAM_TOKEN_REFRESHED", str(int(time.time() - 6 * 86400)))   # too new: retry tomorrow
        return token
    env.save("INSTAGRAM_ACCESS_TOKEN", r["access_token"])
    env.save("INSTAGRAM_TOKEN_REFRESHED", str(int(time.time())))
    return r["access_token"]


def instagram_account() -> Optional[dict]:
    token = _ig_token()
    if not token:
        return None
    token = _ig_refresh(token)
    if token.startswith("IG"):
        d = _ig("GET", f"{_ig_base(token)}/me", params={"fields": "user_id,username,account_type", "access_token": token})
        return {"id": d.get("user_id") or d.get("id"), "username": d.get("username"), "type": d.get("account_type")}
    uid = os.environ.get("INSTAGRAM_USER_ID")
    if not uid:
        raise PublishError("Facebook-login token: also set INSTAGRAM_USER_ID in .env")
    d = _ig("GET", f"{_ig_base(token)}/{uid}", params={"fields": "username", "access_token": token})
    return {"id": uid, "username": d.get("username"), "type": "BUSINESS"}


def instagram_upload(video: Path, caption: str, cover_ms: int = 0,
                     progress: Progress = lambda s, p: None) -> dict:
    token = _ig_token()
    if not token:
        raise PublishError("Add INSTAGRAM_ACCESS_TOKEN to .env")
    acct = instagram_account()
    base, token = _ig_base(token), _ig_token()
    progress("creating container", 0.02)
    c = _ig("POST", f"{base}/{acct['id']}/media",
            data={"media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200],
                  "share_to_feed": "true", "thumb_offset": str(max(0, cover_ms)), "access_token": token})
    cid = c["id"]
    size = video.stat().st_size
    progress("uploading", 0.1)
    with video.open("rb") as f:
        r = httpx.post(c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{IG_VERSION}/{cid}",
                       headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size),
                                "Content-Type": "application/octet-stream"},
                       content=f.read(), timeout=600)
    if r.status_code >= 400:
        raise PublishError(f"Instagram upload failed: HTTP {r.status_code} {r.text[:200]}")
    for i in range(120):                          # Instagram transcodes the video (usually < 1 min)
        st = _ig("GET", f"{base}/{cid}", params={"fields": "status_code,status", "access_token": token})
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise PublishError(f"Instagram could not process the video: {st.get('status')}")
        progress("Instagram is processing the video", 0.5 + min(0.4, i / 60))
        time.sleep(5)
    else:
        raise PublishError("Instagram took too long to process the video.")
    progress("publishing", 0.95)
    m = _ig("POST", f"{base}/{acct['id']}/media_publish", data={"creation_id": cid, "access_token": token})
    link = _ig("GET", f"{base}/{m['id']}", params={"fields": "permalink", "access_token": token}).get("permalink")
    return {"id": m["id"], "url": link, "username": acct.get("username")}


# =========================================================================== status
def accounts() -> dict:
    """Connection status for the dashboard (never returns secrets)."""
    out = {"youtube": {"configured": bool(_yt_client()), "connected": False},
           "instagram": {"configured": bool(_ig_token()), "connected": False}}
    try:
        a = youtube_account()
        if a:
            out["youtube"].update(connected=True, name=a["title"])
    except Exception as e:  # noqa: BLE001
        out["youtube"]["error"] = str(e)[:200]
    try:
        a = instagram_account()
        if a:
            out["instagram"].update(connected=True, name="@" + (a.get("username") or "?"), type=a.get("type"))
    except Exception as e:  # noqa: BLE001
        out["instagram"]["error"] = str(e)[:200]
    return out
