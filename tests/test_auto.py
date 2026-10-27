"""Auto-clip channels: own channel → clips + auto-publish; other creators → clips for review only."""
import time

import pytest
from fastapi.testclient import TestClient

from clipforge import server, watch
from conftest import signup

MINE = "UC" + "m" * 22
OTHER = "UC" + "o" * 22


@pytest.fixture
def feeds(monkeypatch):
    data = {MINE: {"title": "ITG Anshu", "videos": []}, OTHER: {"title": "Some Creator", "videos": []}}
    monkeypatch.setattr(watch, "latest", lambda cid: data[cid])
    monkeypatch.setattr(watch, "resolve", lambda link: {"id": MINE if "mine" in link else OTHER,
                                                        "title": "ITG Anshu" if "mine" in link else "Some Creator"})
    return data


def video(vid, minutes_ago, short=False):
    return {"id": vid, "title": f"Video {vid}", "url": f"https://www.youtube.com/watch?v={vid}",
            "published": time.time() - minutes_ago * 60, "short": short}


@pytest.fixture
def owner(client):
    signup(client, "owner@example.com")
    client.mem.users.update_one({"email": "owner@example.com"}, {"$set": {"accounts": [
        {"id": "yt1", "platform": "youtube", "ext_id": MINE, "name": "ITG Anshu", "default": True, "status": "ok"},
        {"id": "ig1", "platform": "instagram", "ext_id": "1784", "name": "@chao", "default": True, "status": "ok",
         "kind": "facebook_login"}]}})
    return client


def test_own_channel_is_auto_published_other_creators_need_permission(owner, feeds):
    feeds[MINE]["videos"] = [video("old1", 600)]
    w = owner.post("/watches", json={"url": "https://www.youtube.com/@mine"}).json()
    assert w["owned"] and w["auto_publish"] and sorted(w["accounts"]) == ["ig1", "yt1"]
    assert owner.post("/watches", json={"url": "https://www.youtube.com/@mine"}).status_code == 400   # duplicate
    r = owner.post("/watches", json={"url": "https://www.youtube.com/@other"})
    assert r.status_code == 400 and "permission" in r.json()["detail"]
    o = owner.post("/watches", json={"url": "https://www.youtube.com/@other", "permission": True}).json()
    assert o["owned"] is False and o["auto_publish"] is False
    bad = owner.patch(f"/watches/{o['id']}", json={"auto_publish": True})
    assert bad.status_code == 400 and "own channels" in bad.json()["detail"]
    assert owner.patch(f"/watches/{w['id']}", json={"post_count": 2, "every_hours": 3}).json()["post_count"] == 2
    stranger = TestClient(server.app)
    signup(stranger, "member@example.com")
    assert stranger.get("/watches").json() == []
    assert stranger.patch(f"/watches/{w['id']}", json={"enabled": False}).status_code == 404


def test_new_uploads_become_projects_once(owner, feeds):
    feeds[MINE]["videos"] = [video("old1", 600)]
    w = owner.post("/watches", json={"url": "https://www.youtube.com/@mine"}).json()
    assert owner.post(f"/watches/{w['id']}/check").json()["created"] == []          # backlog is never processed
    feeds[MINE]["videos"] = [video("new1", 120), video("fresh", 5), video("sh1", 90, short=True), video("old1", 600)]
    created = owner.post(f"/watches/{w['id']}/check").json()["created"]
    assert len(created) == 1                                                          # fresh waits, Short skipped
    job = owner.mem.jobs.find_one({"_id": created[0]})
    assert job["source"].endswith("v=new1") and job["auto"] == {"watch": w["id"], "video": "new1", "publish": True}
    assert owner.post(f"/watches/{w['id']}/check").json()["created"] == []           # not twice
    feeds[MINE]["videos"][1]["published"] -= 3600                                     # the premiere is now old enough
    assert len(owner.post(f"/watches/{w['id']}/check").json()["created"]) == 1
    recent = owner.get("/watches").json()[0]["recent"]
    assert {r["title"] for r in recent} == {"Video new1", "Video fresh"}


def test_auto_publish_posts_the_best_clips(owner, feeds, monkeypatch):
    feeds[MINE]["videos"] = []
    w = owner.post("/watches", json={"url": "https://www.youtube.com/@mine"}).json()
    feeds[MINE]["videos"] = [video("new1", 120)]
    job_id = owner.post(f"/watches/{w['id']}/check").json()["created"][0]
    owner.mem.jobs.update_one({"_id": job_id}, {"$set": {"state": "done", "manifest": {"clips": [
        {"id": "clip_01", "score": 72}, {"id": "clip_02", "score": 91}, {"id": "clip_03", "score": 40},
        {"id": "clip_04", "score": 65}, {"id": "clip_05", "score": 88}]}}})
    calls = []
    monkeypatch.setattr(server, "_queue_publish", lambda job, uid, clips, accts, privacy, start, every, post=None:
                        calls.append((clips, sorted(a["id"] for a in accts), privacy, every)) or [1] * (len(clips) * len(accts)))
    server._after_job(job_id)
    assert calls == [(["clip_02", "clip_05", "clip_01"], ["ig1", "yt1"], "public", 6)]
    assert owner.mem.jobs.find_one({"_id": job_id})["auto"]["published"] == 3


def test_no_auto_publish_for_other_creators_or_lost_ownership(owner, feeds, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_queue_publish", lambda *a, **k: calls.append(a) or [])
    feeds[OTHER]["videos"] = []
    o = owner.post("/watches", json={"url": "https://www.youtube.com/@other", "permission": True}).json()
    feeds[OTHER]["videos"] = [video("x1", 120)]
    job_id = owner.post(f"/watches/{o['id']}/check").json()["created"][0]
    assert owner.mem.jobs.find_one({"_id": job_id})["auto"]["publish"] is False
    owner.mem.jobs.update_one({"_id": job_id}, {"$set": {"manifest": {"clips": [{"id": "clip_01", "score": 99}]}}})
    server._after_job(job_id)
    assert calls == []
    # own channel, but the YouTube account was removed before the clips were ready → not published
    feeds[MINE]["videos"] = []
    w = owner.post("/watches", json={"url": "https://www.youtube.com/@mine"}).json()
    feeds[MINE]["videos"] = [video("m1", 120)]
    jid = owner.post(f"/watches/{w['id']}/check").json()["created"][0]
    owner.mem.jobs.update_one({"_id": jid}, {"$set": {"manifest": {"clips": [{"id": "clip_01", "score": 99}]}}})
    owner.mem.users.update_one({"email": "owner@example.com"}, {"$pull": {"accounts": {"id": "yt1"}}})
    server._after_job(jid)
    assert calls == [] and "no longer" in owner.mem.jobs.find_one({"_id": jid})["auto"]["skipped"]


def test_feed_parsing():
    xml = """<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
      <title>ITG Anshu</title>
      <entry><yt:videoId>abc123</yt:videoId><title>Long one</title>
        <link rel="alternate" href="https://www.youtube.com/watch?v=abc123"/><published>2026-10-27T10:00:00+00:00</published></entry>
      <entry><yt:videoId>sh9</yt:videoId><title>A short</title>
        <link rel="alternate" href="https://www.youtube.com/shorts/sh9"/><published>2026-10-27T09:00:00+00:00</published></entry>
    </feed>"""
    f = watch.parse(xml)
    assert f["title"] == "ITG Anshu" and [v["id"] for v in f["videos"]] == ["abc123", "sh9"]
    assert [v["short"] for v in f["videos"]] == [False, True] and f["videos"][0]["published"] > 0
    with pytest.raises(watch.WatchError):
        watch.resolve("https://evil.example/@x")
