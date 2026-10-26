"""Accounts, per-user isolation and owner-only publishing — against an in-memory MongoDB."""
import json

import mongomock
import pytest
from fastapi.testclient import TestClient

from clipforge import auth, db as dbmod, server


@pytest.fixture
def client(tmp_path, monkeypatch):
    mem = mongomock.MongoClient().clipforge
    monkeypatch.setattr(dbmod, "db", lambda: mem)
    monkeypatch.setattr(auth, "db", lambda: mem)
    monkeypatch.setattr(server, "db", lambda: mem)
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "_start", lambda job_id: None)        # don't run the real pipeline
    monkeypatch.delenv("CLIPFORGE_SIGNUP_CODE", raising=False)
    monkeypatch.delenv("CLIPFORGE_SIGNUP", raising=False)
    c = TestClient(server.app)
    c.mem = mem
    return c


def signup(c, email, pw="password123", **kw):
    return c.post("/auth/signup", json={"email": email, "password": pw, **kw})


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
    assert stored["password"].startswith("scrypt$") and "password123" not in json.dumps(stored, default=str)
    session = client.mem.sessions.find_one({})
    assert session["token"] != client.cookies.get(auth.COOKIE)            # only the hash is stored
    client.post("/auth/logout")
    assert client.get("/auth/me").status_code == 401
    assert client.post("/auth/login", json={"email": "owner@example.com", "password": "nope-nope"}).status_code == 400
    assert client.post("/auth/login", json={"email": "OWNER@example.com", "password": "password123"}).status_code == 200
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


def test_publishing_is_owner_only(client):
    signup(client, "owner@example.com")
    member = TestClient(server.app)
    signup(member, "member@example.com")
    job = member.post("/jobs", data={"url": "https://youtu.be/abc123xyz"}).json()
    assert member.get("/publish/accounts").status_code == 403
    r = member.post(f"/jobs/{job['id']}/publish", json={"clips": ["clip_01"], "platforms": ["youtube"]})
    assert r.status_code == 403


def test_first_user_claims_local_projects(client, tmp_path):
    d = tmp_path / "oldjob123456"
    d.mkdir()
    (d / "job.json").write_text(json.dumps({"id": "oldjob123456", "state": "done", "title": "Old", "created": 1}))
    (d / "clips.json").write_text(json.dumps({"source": "x", "duration": 60, "clips": []}))
    signup(client, "owner@example.com")
    jobs = client.get("/jobs").json()
    assert [j["title"] for j in jobs] == ["Old"]
    assert client.get("/jobs/oldjob123456/clips").json()["duration"] == 60
