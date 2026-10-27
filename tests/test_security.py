"""Security hardening: brute force, sessions, CSRF, headers, and the server-file / SSRF / injection holes."""
import json

from fastapi.testclient import TestClient

from clipforge import auth, server
from conftest import PASSWORD, signup


def test_password_policy(client):
    for weak, why in (("password123", "easy"), ("aaaaaaaa", "easy"), ("short", "8 characters"), ("ownerXYZ123", "email")):
        r = signup(TestClient(server.app), "owner@example.com", pw=weak)
        assert r.status_code == 400 and why in r.json()["detail"], weak
    assert signup(client, "owner@example.com").status_code == 200


def test_login_is_throttled_per_email(client):
    signup(client, "owner@example.com")
    attacker = TestClient(server.app)
    for _ in range(auth.MAX_FAILS_EMAIL):
        assert attacker.post("/auth/login", json={"email": "owner@example.com", "password": "guess-guess"}).status_code == 400
    blocked = attacker.post("/auth/login", json={"email": "owner@example.com", "password": PASSWORD})
    assert blocked.status_code == 429 and "Too many" in blocked.json()["detail"]   # even the right password waits
    signup(TestClient(server.app), "other@example.com")
    assert TestClient(server.app).post("/auth/login", json={"email": "other@example.com", "password": PASSWORD}).status_code == 200


def test_remember_me_and_device_list(client):
    signup(client, "owner@example.com")
    short = TestClient(server.app)
    r = short.post("/auth/login", json={"email": "owner@example.com", "password": PASSWORD, "remember": False})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age" not in cookie   # browser-session cookie
    assert "max-age" in client.post("/auth/login", json={"email": "owner@example.com", "password": PASSWORD}).headers["set-cookie"].lower()
    sessions = client.get("/settings/sessions").json()
    assert len(sessions) == 3 and sum(s["current"] for s in sessions) == 1
    other = next(s for s in sessions if not s["remember"])
    assert client.delete(f"/settings/sessions/{other['id']}").status_code == 200
    assert short.get("/auth/me").status_code == 401


def test_requests_from_other_websites_are_blocked(client):
    signup(client, "owner@example.com")
    evil = client.post("/settings/logout-all", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    assert client.post("/settings/logout-all", headers={"Origin": "http://testserver"}).status_code == 200


def test_security_headers_and_no_public_api_docs(client):
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_links_must_be_public_web_addresses(client):
    signup(client, "owner@example.com")
    for bad in (r"C:\Users\someone\video.mp4", "/etc/passwd", "file:///etc/passwd", "http://127.0.0.1:8000/x",
                "http://192.168.1.10/cam.mp4", "http://metadata.internal/latest", "http://[::1]/x"):
        r = client.post("/jobs", data={"url": bad})
        assert r.status_code == 400, bad
    assert client.post("/jobs", data={"url": "https://youtu.be/abc123xyz"}).status_code == 200


def test_style_cannot_reach_server_files_or_inject_ffmpeg(client, tmp_path):
    signup(client, "owner@example.com")
    base = client.get("/brand").json()["template"]

    def put(mut):
        t = json.loads(json.dumps(base))
        mut(t)
        return client.put("/brand", json=t)

    secret = tmp_path / "someone_elses.mp4"
    secret.write_bytes(b"x")
    assert put(lambda t: t["logo"].update(path=str(secret))).status_code == 400            # not your upload
    assert put(lambda t: t["music"].update(path=r"C:\Windows\win.ini")).status_code == 400
    assert put(lambda t: t["motion"].update(progress_color="#fff:x=0[a];movie=secret")).status_code == 400
    assert put(lambda t: t["music"].update(volume="0.1,amovie=x")).status_code == 400       # numbers must be numbers
    assert put(lambda t: t["layout"].update(aspect="9:16;x")).status_code == 400
    assert put(lambda t: t["captions"].update(font="Arial}{\\fn")).status_code == 400
    assert put(lambda t: t.update(fonts_dir=str(tmp_path))).status_code == 400
    # a file the user uploaded themselves is fine
    up = client.post("/brand/asset", files={"file": ("logo.png", b"\x89PNG....", "image/png")}).json()
    assert put(lambda t: t["logo"].update(path=up["path"])).status_code == 200
    assert client.post("/brand/asset", files={"file": ("evil.exe", b"MZ", "application/octet-stream")}).status_code == 400
    # another user can't point at it
    member = TestClient(server.app)
    signup(member, "member@example.com")
    t = member.get("/brand").json()["template"]
    t["logo"]["path"] = up["path"]
    assert member.put("/brand", json=t).status_code == 400
    # a job submitted with a crafted style is refused too
    bad = json.loads(json.dumps(base))
    bad["motion"]["progress_color"] = "red;movie=x"
    assert client.post("/jobs", data={"url": "https://youtu.be/abc123xyz", "template": json.dumps(bad)}).status_code == 400


def test_downloads_are_owner_only(client, tmp_path):
    signup(client, "owner@example.com")
    job = client.post("/jobs", data={"url": "https://youtu.be/abc123xyz"}).json()
    (tmp_path / job["id"]).mkdir(exist_ok=True)
    (tmp_path / job["id"] / "clip_01.mp4").write_bytes(b"video")
    client.mem.jobs.update_one({"_id": job["id"]}, {"$set": {"manifest": {"source": "x", "clips": [{"id": "clip_01", "file": "clip_01.mp4"}]}}})
    r = client.get(f"/jobs/{job['id']}/clips/clip_01/download")
    assert r.status_code == 200 and r.content == b"video" and "attachment" in r.headers["content-disposition"]
    member = TestClient(server.app)
    signup(member, "member@example.com")
    assert member.get(f"/jobs/{job['id']}/clips/clip_01/download").status_code == 404
    assert TestClient(server.app).get(f"/jobs/{job['id']}/clips/clip_01/download").status_code == 401


def test_youtube_cookies_upload_is_owner_only(client, monkeypatch):
    monkeypatch.delenv("YTDLP_COOKIES", raising=False)
    signup(client, "owner@example.com")
    good = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1999999999\tSID\tabc123\n"
    r = client.post("/admin/youtube-cookies", files={"file": ("cookies.txt", good.encode(), "text/plain")})
    assert r.status_code == 200 and r.json()["present"] and r.json()["cookies"] == 1
    assert server.os.environ["YTDLP_COOKIES"] == str(server._cookies_path())
    assert client.get("/settings").json()["youtube_cookies"]["present"] is True
    bad = client.post("/admin/youtube-cookies", files={"file": ("x.txt", b"hello world", "text/plain")})
    assert bad.status_code == 400
    member = TestClient(server.app)
    signup(member, "member@example.com")
    assert member.get("/settings").json()["youtube_cookies"] is None
    assert member.post("/admin/youtube-cookies", files={"file": ("c.txt", good.encode(), "text/plain")}).status_code == 403
    assert "abc123" not in client.get("/admin/youtube-cookies").text           # contents never sent back
    assert client.delete("/admin/youtube-cookies").json() == {"present": False}
    assert "YTDLP_COOKIES" not in server.os.environ


def test_youtube_bot_check_gives_a_clear_message(tmp_path, monkeypatch):
    from clipforge import media
    def blocked(cmd, cwd=None):
        raise media.ClipforgeError("ffmpeg_failed", "ERROR: [youtube] x: Sign in to confirm you’re not a bot.")
    monkeypatch.setattr(media, "run", blocked)
    monkeypatch.delenv("YTDLP_COOKIES", raising=False)
    try:
        media.fetch("https://www.youtube.com/watch?v=x", tmp_path)
    except media.ClipforgeError as e:
        assert e.code == "youtube_blocked" and "cookies in Settings" in str(e) and "upload the file" in str(e)
    else:
        raise AssertionError("expected youtube_blocked")
