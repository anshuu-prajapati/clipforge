"""Forgot password: email link, one-time + expiring token, sessions ended, no account discovery."""
import re
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from clipforge import auth, server
from conftest import PASSWORD, signup


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(server, "_send_safely", lambda to, subject, text, html: sent.append((to, subject, text)))
    return sent


def wait_for(outbox, n):
    for _ in range(50):
        if len(outbox) >= n:
            return
        time.sleep(0.02)


def link_token(text):
    return re.search(r"#/reset\?token=([\w-]+)", text)[1]


def test_reset_flow_end_to_end(client, outbox):
    signup(client, "owner@example.com")
    laptop = TestClient(server.app)
    laptop.post("/auth/login", json={"email": "owner@example.com", "password": PASSWORD})
    stranger = TestClient(server.app)
    known = stranger.post("/auth/forgot", json={"email": "Owner@Example.com"}).json()
    unknown = stranger.post("/auth/forgot", json={"email": "nobody@example.com"}).json()
    assert known == unknown                                   # same answer: no account discovery
    wait_for(outbox, 1)
    assert len(outbox) == 1 and outbox[0][0] == "owner@example.com" and "30 minutes" in outbox[0][2]
    token = link_token(outbox[0][2])
    assert token not in str(list(client.mem.password_resets.find()))     # only the hash is stored
    assert stranger.get(f"/auth/reset/{token}").json() == {"valid": True, "email": "ow•••@example.com"}
    assert stranger.post("/auth/reset", json={"token": token, "password": "password123"}).status_code == 400  # weak
    r = stranger.post("/auth/reset", json={"token": token, "password": "Brand-new-pass-77"})
    assert r.status_code == 200 and r.json()["email"] == "owner@example.com"
    assert stranger.get("/auth/me").status_code == 200        # logged in with the new password
    assert client.get("/auth/me").status_code == 401 and laptop.get("/auth/me").status_code == 401   # everyone else out
    assert stranger.post("/auth/reset", json={"token": token, "password": "Another-pass-88"}).status_code == 400  # one use
    wait_for(outbox, 2)
    assert "changed" in outbox[1][1]                          # security notice
    assert TestClient(server.app).post("/auth/login", json={"email": "owner@example.com", "password": "Brand-new-pass-77"}).status_code == 200


def test_reset_link_expires_and_requests_are_throttled(client, outbox):
    signup(client, "owner@example.com")
    c = TestClient(server.app)
    c.post("/auth/forgot", json={"email": "owner@example.com"})
    wait_for(outbox, 1)
    token = link_token(outbox[0][2])
    client.mem.password_resets.update_many({}, {"$set": {"expires": datetime.now(timezone.utc) - timedelta(minutes=1)}})
    assert c.get(f"/auth/reset/{token}").json()["valid"] is False
    assert c.post("/auth/reset", json={"token": token, "password": "Brand-new-pass-77"}).status_code == 400
    for _ in range(5):
        c.post("/auth/forgot", json={"email": "owner@example.com"})
    time.sleep(0.2)
    assert len(outbox) == auth.MAX_RESETS_EMAIL               # 3 per hour, the rest silently dropped


def test_reset_unlocks_a_locked_out_account(client, outbox):
    signup(client, "owner@example.com")
    c = TestClient(server.app)
    for _ in range(auth.MAX_FAILS_EMAIL):
        c.post("/auth/login", json={"email": "owner@example.com", "password": "wrong-guess"})
    assert c.post("/auth/login", json={"email": "owner@example.com", "password": PASSWORD}).status_code == 429
    c.post("/auth/forgot", json={"email": "owner@example.com"})
    wait_for(outbox, 1)
    assert c.post("/auth/reset", json={"token": link_token(outbox[0][2]), "password": "Brand-new-pass-77"}).status_code == 200
    assert TestClient(server.app).post("/auth/login", json={"email": "owner@example.com", "password": "Brand-new-pass-77"}).status_code == 200


def test_changing_password_in_settings_sends_a_notice_and_kills_reset_links(client, outbox):
    signup(client, "owner@example.com")
    TestClient(server.app).post("/auth/forgot", json={"email": "owner@example.com"})
    wait_for(outbox, 1)
    token = link_token(outbox[0][2])
    assert client.post("/settings/password", json={"current": PASSWORD, "new": "Brand-new-pass-77"}).status_code == 200
    wait_for(outbox, 2)
    assert "changed" in outbox[1][1]
    assert client.get(f"/auth/reset/{token}").json()["valid"] is False
