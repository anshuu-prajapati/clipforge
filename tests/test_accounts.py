"""Accounts, per-user isolation and owner-only publishing — against an in-memory MongoDB."""
import json

import pytest
from fastapi.testclient import TestClient

from clipforge import auth, server
from conftest import PASSWORD, signup


def test_password_hash_roundtrip():
    h = auth.hash_password("correct horse")
    assert h.startswith("scrypt$") and auth.check_password("correct horse", h)
    assert not auth.check_password("wrong", h) and not auth.check_password("x", "garbage")


def test_signup_login_logout_and_roles(client):
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/config").json()["first_user"] is True
    r = signup(client, "Owner@Example.com", name="Anshu")
    assert r.status_code == 200 and r.json()["role"] == "owner" and r.json()["email"] == "owner@example.com"
    assert client.get("/auth/me").json()["name"] == "Anshu"
    stored = client.mem.users.find_one({"email": "owner@example.com"})
    assert stored["password"].startswith("scrypt$") and PASSWORD not in json.dumps(stored, default=str)
    session = client.mem.sessions.find_one({})
    assert session["token"] != client.cookies.get(auth.COOKIE)            # only the hash is stored
    client.post("/auth/logout")
    assert client.get("/auth/me").status_code == 401
    assert client.post("/auth/login", json={"email": "owner@example.com", "password": "nope-nope"}).status_code == 400
    assert client.post("/auth/login", json={"email": "OWNER@example.com", "password": PASSWORD}).status_code == 200
    other = TestClient(server.app)
    assert signup(other, "member@example.com").json()["role"] == "member"
    assert signup(other, "member@example.com").status_code == 400          # duplicate
    assert signup(TestClient(server.app), "short@example.com", pw="123").status_code == 400


def test_invite_code_and_closed_signup(client, monkeypatch):
    signup(client, "owner@example.com")
    monkeypatch.setenv("CLIPFORGE_SIGNUP_CODE", "letmein")
    assert signup(TestClient(server.app), "a@example.com", code="wrong").status_code == 400
    assert signup(TestClient(server.app), "a@example.com", code="letmein").status_code == 200
    monkeypatch.setenv("CLIPFORGE_SIGNUP", "closed")
    assert signup(TestClient(server.app), "b@example.com", code="letmein").status_code == 400


def test_projects_are_private_per_user(client):
    signup(client, "owner@example.com")
    job = client.post("/jobs", data={"url": "https://youtu.be/abc123xyz", "length": "auto", "max_clips": "3"}).json()
    assert job["user_id"] and "input" not in job
    assert [j["id"] for j in client.get("/jobs").json()] == [job["id"]]
    member = TestClient(server.app)
    signup(member, "member@example.com")
    assert member.get("/jobs").json() == []
    for path in (f"/jobs/{job['id']}", f"/jobs/{job['id']}/clips", f"/jobs/{job['id']}/publish",
                 f"/jobs/{job['id']}/files/clips.json"):
        assert member.get(path).status_code == 404, path
    assert member.delete(f"/jobs/{job['id']}").status_code == 404
    assert TestClient(server.app).get("/jobs").status_code == 401          # logged out


def test_brand_is_per_user_and_new_jobs_use_it(client):
    signup(client, "owner@example.com")
    t = client.get("/brand").json()["template"]
    t["captions"]["highlight_color"] = "#00E676"
    assert client.put("/brand", json=t).status_code == 200
    member = TestClient(server.app)
    signup(member, "member@example.com")
    assert member.get("/brand").json()["template"]["captions"]["highlight_color"] == "#FFD60A"
    job = client.post("/jobs", data={"url": "https://youtu.be/abc123xyz"}).json()
    stored = client.mem.jobs.find_one({"_id": job["id"]})
    assert stored["input"]["template"]["captions"]["highlight_color"] == "#00E676"


def test_publish_needs_your_own_accounts(client):
    signup(client, "owner@example.com")
    member = TestClient(server.app)
    signup(member, "member@example.com")
    job = member.post("/jobs", data={"url": "https://youtu.be/abc123xyz"}).json()
    client.mem.jobs.update_one({"_id": job["id"]}, {"$set": {"manifest": {"source": "x", "clips": [{"id": "clip_01"}]}}})
    assert member.get("/accounts").json()["accounts"] == []
    r = member.post(f"/jobs/{job['id']}/publish", json={"clips": ["clip_01"], "accounts": ["not-mine"]})
    assert r.status_code == 400 and "Settings" in r.json()["detail"]


@pytest.fixture
def fake_verify(monkeypatch):
    from clipforge import accounts

    def yt(acct):
        token = accounts.unseal(acct["refresh_token"])
        if token == "bad":
            raise accounts.PublishError("The refresh token is invalid or expired.")
        return {"ext_id": "UC-" + token, "name": "Channel " + token}

    def ig(acct):
        tail = accounts.unseal(acct["access_token"])[-3:]
        return {"ext_id": "1784" + tail, "name": "@ig" + tail, "kind": "facebook_login"}

    monkeypatch.setattr(accounts, "verify_youtube", yt)
    monkeypatch.setattr(accounts, "verify_instagram", ig)


def test_many_accounts_added_by_hand(client, fake_verify, monkeypatch):
    signup(client, "owner@example.com")

    def add(**f):
        return client.post("/accounts", json=f)

    r = add(platform="youtube", client_id="cid.apps", client_secret="GOCSPX-secret", refresh_token="aaa", label="Main")
    assert r.status_code == 200, r.text
    a1 = r.json()
    assert a1["name"] == "Channel aaa" and a1["default"] is True and a1["client_id"] == "cid.apps"
    assert a1["stored"] == {"client_secret": "••••cret", "refresh_token": "••••"} and "GOCSPX" not in r.text
    a2 = add(platform="youtube", client_id="cid2.apps", client_secret="GOCSPX-other", refresh_token="bbb").json()
    assert a2["default"] is False
    assert add(platform="youtube", client_id="cid.apps", client_secret="x", refresh_token="aaa").status_code == 400
    bad = add(platform="youtube", client_id="cid.apps", client_secret="x", refresh_token="bad")
    assert bad.status_code == 400 and "refresh token" in bad.json()["detail"]
    ig1 = add(platform="instagram", access_token="EAA-token-111").json()
    ig2 = add(platform="instagram", access_token="EAA-token-222").json()
    assert ig1["name"] == "@ig111" and ig2["name"] == "@ig222"
    stored = client.mem.users.find_one({"email": "owner@example.com"})["accounts"]
    assert len(stored) == 4 and "EAA-token" not in str(stored) and "GOCSPX" not in str(stored)
    # edit: new token keeps the stored secret; label only; make default
    e = client.patch(f"/accounts/{a1['id']}", json={"refresh_token": "ccc"}).json()
    assert e["name"] == "Channel ccc" and e["stored"]["client_secret"] == "••••cret"
    assert client.patch(f"/accounts/{a2['id']}", json={"label": "Second"}).json()["label"] == "Second"
    client.patch(f"/accounts/{a2['id']}", json={"default": True})
    accts = {a["id"]: a for a in client.get("/accounts").json()["accounts"]}
    assert accts[a2["id"]]["default"] and not accts[a1["id"]]["default"]
    # publish to two chosen accounts → one status per account
    calls = []
    monkeypatch.setattr(server, "_publish_one", lambda job, clip, aid, opts: calls.append(aid))
    job = client.post("/jobs", data={"url": "https://youtu.be/abc123xyz"}).json()
    client.mem.jobs.update_one({"_id": job["id"]}, {"$set": {"manifest": {"source": "x", "clips": [{"id": "clip_01"}]}}})
    r = client.post(f"/jobs/{job['id']}/publish", json={"clips": ["clip_01"], "accounts": [a1["id"], ig2["id"]]})
    assert r.status_code == 200, r.text
    import time as _t
    _t.sleep(0.2)
    assert sorted(calls) == sorted([a1["id"], ig2["id"]])
    st = client.get(f"/jobs/{job['id']}/publish").json()["status"]["clip_01"]
    assert st[ig2["id"]]["platform"] == "instagram" and st[ig2["id"]]["account"] == "@ig222"
    # remove → the remaining account becomes the default
    assert client.delete(f"/accounts/{a2['id']}").status_code == 200
    left = [a for a in client.get("/accounts").json()["accounts"] if a["platform"] == "youtube"]
    assert [a["id"] for a in left] == [a1["id"]] and left[0]["default"]
    assert TestClient(server.app).get("/accounts").status_code == 401


def test_import_env_accounts_owner_only(client, fake_verify, monkeypatch):
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "envcid")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "envsecret")
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "env")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "EAA-env-999")
    signup(client, "owner@example.com")
    assert set(client.get("/accounts").json()["env"]) == {"youtube", "instagram"}
    r = client.post("/accounts/import-env").json()
    assert sorted(r["added"]) == ["@ig999", "Channel env"] and r["errors"] == []
    member = TestClient(server.app)
    signup(member, "member@example.com")
    assert member.get("/accounts").json()["env"] == []
    assert member.post("/accounts/import-env").status_code == 403


def test_connect_flow_adds_an_account_for_the_user_who_started_it(client, monkeypatch):
    from clipforge import connections
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setattr(connections, "google_auth_url",
                        lambda redirect, state: (f"https://accounts.google.com/o?state={state}", "verifier"))
    monkeypatch.setattr(connections, "google_finish", lambda redirect, code, verifier: {
        "refresh_token": connections.seal("refresh-123"), "id": "UC1", "name": "My Channel", "connected_at": 1})
    monkeypatch.setattr(connections, "google_revoke", lambda conn: None)
    signup(client, "owner@example.com")
    r = client.get("/connect/youtube/start?next=%23%2Fjob%2Fabc", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/")
    state = client.mem.oauth_states.find_one({})["_id"]
    stranger = TestClient(server.app)                       # the provider redirect carries no cookie
    r = stranger.get(f"/connect/youtube/callback?state={state}&code=abc", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/#/job/abc?connected=youtube")
    accts = client.get("/accounts").json()["accounts"]
    assert [(a["name"], a["method"], a["default"]) for a in accts] == [("My Channel", "oauth", True)]
    assert "refresh-123" not in str(client.mem.users.find_one({"email": "owner@example.com"}))
    again = stranger.get(f"/connect/youtube/callback?state={state}&code=abc", follow_redirects=False)
    assert "error=" in again.headers["location"]           # states are single-use
    member = TestClient(server.app)
    signup(member, "member@example.com")
    assert member.get("/accounts").json()["accounts"] == []
    assert client.delete(f"/accounts/{accts[0]['id']}").status_code == 200
    assert client.get("/accounts").json()["accounts"] == []


def test_connect_denied_or_unconfigured(client, monkeypatch):
    monkeypatch.delenv("INSTAGRAM_APP_ID", raising=False)
    signup(client, "owner@example.com")
    r = client.get("/connect/instagram/start", follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    r = client.get("/connect/youtube/callback?state=nope&code=x", follow_redirects=False)
    assert "expired" in r.headers["location"]


def test_settings_profile_password_sessions_and_delete(client):
    signup(client, "owner@example.com", name="Old")
    signup(TestClient(server.app), "taken@example.com")
    assert client.patch("/settings/profile", json={"name": "New Name"}).json()["name"] == "New Name"
    assert client.patch("/settings/profile", json={"email": "taken@example.com"}).status_code == 400
    assert client.patch("/settings/profile", json={"email": "me@example.com"}).json()["email"] == "me@example.com"
    other_device = TestClient(server.app)
    other_device.post("/auth/login", json={"email": "me@example.com", "password": PASSWORD})
    assert client.post("/settings/password", json={"current": "wrong-one", "new": "newpassword1"}).status_code == 400
    assert client.post("/settings/password", json={"current": PASSWORD, "new": "newpassword1"}).status_code == 200
    assert other_device.get("/auth/me").status_code == 401     # other sessions ended
    assert client.get("/auth/me").status_code == 200            # this one kept
    job = client.post("/jobs", data={"url": "https://youtu.be/abc123xyz"}).json()
    client.mem.jobs.update_one({"_id": job["id"]}, {"$set": {"state": "done"}})
    assert client.post("/settings/delete-account", json={"password": PASSWORD}).status_code == 400
    assert client.post("/settings/delete-account", json={"password": "newpassword1"}).status_code == 200
    assert client.mem.users.find_one({"email": "me@example.com"}) is None
    assert client.mem.jobs.count_documents({"_id": job["id"]}) == 0
    assert client.get("/auth/me").status_code == 401


def test_tokens_are_encrypted_at_rest(monkeypatch):
    from cryptography.fernet import Fernet
    from clipforge import connections
    monkeypatch.setenv("CLIPFORGE_SECRET_KEY", Fernet.generate_key().decode())
    sealed = connections.seal("IGAA-very-secret")
    assert "IGAA" not in sealed and connections.unseal(sealed) == "IGAA-very-secret"


def test_first_user_claims_local_projects(client, tmp_path):
    d = tmp_path / "oldjob123456"
    d.mkdir()
    (d / "job.json").write_text(json.dumps({"id": "oldjob123456", "state": "done", "title": "Old", "created": 1}))
    (d / "clips.json").write_text(json.dumps({"source": "x", "duration": 60, "clips": []}))
    signup(client, "owner@example.com")
    jobs = client.get("/jobs").json()
    assert [j["title"] for j in jobs] == ["Old"]
    assert client.get("/jobs/oldjob123456/clips").json()["duration"] == 60


def test_public_url_drives_redirects_and_secure_cookie(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://clips.example.com/")
    client = TestClient(server.app, base_url="https://testserver")   # secure cookies only travel over https
    r = signup(client, "owner@example.com")
    assert "secure" in r.headers["set-cookie"].lower()
    red = client.get("/settings").json()["redirects"]
    assert red == {"youtube": "https://clips.example.com/connect/youtube/callback",
                   "instagram": "https://clips.example.com/connect/instagram/callback"}
