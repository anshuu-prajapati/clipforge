"""Channel watching: resolve a YouTube channel link to its ID and read its public RSS feed.

The feed (https://www.youtube.com/feeds/videos.xml?channel_id=UC…) lists the ~15 newest uploads,
needs no API key or quota, and isn't behind YouTube's bot check. Only youtube.com is ever fetched.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx

FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
CHANNEL_ID = re.compile(r"\b(UC[\w-]{22})\b")


class WatchError(ValueError):
    pass


def _get(url: str) -> httpx.Response:
    return httpx.get(url, timeout=20, follow_redirects=True,
                     headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


def resolve(link: str) -> dict:
    """Channel link (@handle, /channel/UC…, /c/name, a video link) → {id, title}."""
    link = link.strip()
    if link.startswith("@"):
        link = "https://www.youtube.com/" + link
    if m := re.search(r"/channel/(UC[\w-]{22})", link):
        cid = m[1]
    else:
        if not re.match(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", link, re.I):
            raise WatchError("Paste a YouTube channel link, like https://www.youtube.com/@yourchannel")
        try:
            html = _get(link).text
        except httpx.HTTPError:
            raise WatchError("YouTube didn't answer — try again in a minute.") from None
        m = (re.search(r'"(?:externalId|channelId)":"(UC[\w-]{22})"', html)
             or re.search(r'youtube\.com/channel/(UC[\w-]{22})', html))
        if not m:
            raise WatchError("Couldn't find a channel at that link.")
        cid = m[1]
    feed = latest(cid)
    return {"id": cid, "title": feed["title"]}


def latest(channel_id: str) -> dict:
    """{title, videos: [{id, title, url, published (unix), short}]} — newest first."""
    if not CHANNEL_ID.fullmatch(channel_id):
        raise WatchError("Invalid channel ID.")
    try:
        r = _get(FEED.format(channel_id))
    except httpx.HTTPError:
        raise WatchError("Couldn't reach YouTube's feed.") from None
    if r.status_code == 404:
        raise WatchError("That channel doesn't exist (or has no public videos).")
    if r.status_code >= 400:
        raise WatchError(f"YouTube's feed answered HTTP {r.status_code}.")
    return parse(r.text)


def parse(xml: str) -> dict:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise WatchError("YouTube's feed couldn't be read.") from None
    videos = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        link = (e.find("a:link", NS).get("href") if e.find("a:link", NS) is not None else "") or ""
        pub = e.findtext("a:published", default="", namespaces=NS)
        try:
            ts = datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = 0.0
        if vid:
            videos.append({"id": vid, "title": e.findtext("a:title", default="", namespaces=NS),
                           "url": f"https://www.youtube.com/watch?v={vid}", "published": ts,
                           "short": "/shorts/" in link})
    return {"title": root.findtext("a:title", default="", namespaces=NS), "videos": videos}
