"""Shared fixtures: the dashboard against an in-memory MongoDB (no real data, keys or network)."""
import ipaddress

import mongomock
import pytest
from fastapi.testclient import TestClient

from clipforge import auth, db as dbmod, server

PASSWORD = "Clip-forge-2026!"


@pytest.fixture
def client(tmp_path, monkeypatch):
    mem = mongomock.MongoClient().clipforge
    monkeypatch.setattr(dbmod, "db", lambda: mem)
    monkeypatch.setattr(auth, "db", lambda: mem)
    monkeypatch.setattr(server, "db", lambda: mem)
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "BRAND_ASSETS", tmp_path / "brand_assets")
    monkeypatch.setattr(server, "_start", lambda job_id: None)        # don't run the real pipeline

    def resolve(host):                                                # offline DNS: IP literals as-is
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            return ["10.0.0.5"] if host.endswith(".internal") else ["142.250.70.14"]
    monkeypatch.setattr(server, "_resolve", resolve)
    monkeypatch.delenv("CLIPFORGE_SIGNUP_CODE", raising=False)
    monkeypatch.delenv("CLIPFORGE_SIGNUP", raising=False)
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    from cryptography.fernet import Fernet
    monkeypatch.setenv("CLIPFORGE_SECRET_KEY", Fernet.generate_key().decode())   # never touch the real .env
    c = TestClient(server.app)
    c.mem = mem
    return c


def signup(c, email, pw=PASSWORD, **kw):
    return c.post("/auth/signup", json={"email": email, "password": pw, **kw})
