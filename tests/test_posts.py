"""Carousel posts: Claude's plan → slides on disk → edit → Instagram (now or scheduled); per-user isolation."""
import time

import pytest
from fastapi.testclient import TestClient

from clipforge import carousel, publish, server, storage
from conftest import signup

PLAN = {"slides": [{"kind": "hook", "title": "3 ways to *grow* faster", "body": "Number 2 surprises everyone."},
                   {"kind": "point", "title": "Post daily", "body": "Consistency beats perfection."},
                   {"kind": "point", "title": "Hook in 1 second", "body": "Start with the payoff."},
                   {"kind": "cta", "title": "Try one today", "body": "Save this post."}],
        "caption": "Growing is a system.", "hashtags": ["#creator", "fyp", "growth"]}


@pytest.fixture
def me(client, monkeypatch):
    signup(client, "me@example.com")
    client.mem.users.update_one({"email": "me@example.com"}, {"$set": {"accounts": [
        {"id": "ig1", "platform": "instagram", "ext_id": "1784", "name": "chao.seditz", "default": True,
         "status": "ok", "kind": "facebook_login"},
        {"id": "yt1", "platform": "youtube", "ext_id": "UC" + "m" * 22, "name": "ITG", "default": True, "status": "ok"}]}})
    client.written = []

    def write(prompt="", transcript="", title="", slides=7, language="", handle="", images=False):
        client.written.append({"prompt": prompt, "transcript": transcript, "title": title, "slides": slides,
                               "images": images})
        plan = carousel.clean(PLAN)
        if images:
            for s in plan["slides"][:-1]:
                s["image_query"] = "creator " + s["title"][:10]
        return plan
    monkeypatch.setattr(carousel, "write", write)
    monkeypatch.setattr(server, "_start_post", server._make_post)          # run synchronously
    return client


def test_prompt_to_slides_edit_and_isolation(me):
    p = me.post("/posts", json={"prompt": "3 ways to grow on Instagram", "slides": 4}).json()
    assert me.written[0]["slides"] == 4
    p = me.get(f"/posts/{p['id']}").json()
    assert p["state"] == "ready" and p["handle"] == "@chao.seditz" and len(p["images"]) == 4
    assert p["plan"]["hashtags"] == ["creator", "growth"]                    # '#' stripped, spam dropped
    img = me.get(p["images"][0])
    assert img.status_code == 200 and img.content[:3] == b"\xff\xd8\xff"     # a JPEG
    assert me.get(f"/posts/{p['id']}/files/..%2F..%2Fsecret.jpg").status_code == 404
    slides = p["plan"]["slides"][:3]
    slides[1]["title"] = "Post *every* day"
    e = me.patch(f"/posts/{p['id']}", json={"slides": slides, "theme": "light"}).json()
    assert e["version"] == 2 and len(e["images"]) == 3 and e["plan"]["slides"][-1]["kind"] == "cta"
    assert me.patch(f"/posts/{p['id']}", json={"theme": "neon"}).status_code == 400
    assert me.patch(f"/posts/{p['id']}", json={"handle": "<script>"}).status_code == 400
    other = TestClient(server.app)
    signup(other, "other@example.com")
    assert other.get("/posts").json() == []
    assert other.get(p["images"][0]).status_code == 404
    assert other.delete(f"/posts/{p['id']}").status_code == 404
    assert me.delete(f"/posts/{p['id']}").json() == {"ok": True} and me.get("/posts").json() == []


def test_validation_and_failures(me, monkeypatch):
    assert me.post("/posts", json={}).status_code == 400
    assert me.post("/posts", json={"prompt": "x", "slides": 11}).status_code == 400
    assert me.post("/posts", json={"prompt": "x", "theme": "neon"}).status_code == 400
    assert me.post("/posts", json={"job": "nope"}).status_code == 404

    def broken(*a, **k):
        raise carousel.CarouselError("Writing posts needs ANTHROPIC_API_KEY in the server's .env.")
    monkeypatch.setattr(carousel, "write", broken)
    p = me.post("/posts", json={"prompt": "x"}).json()
    p = me.get(f"/posts/{p['id']}").json()
    assert p["state"] == "failed" and "ANTHROPIC_API_KEY" in p["error"]
    assert me.post(f"/posts/{p['id']}/publish", json={"accounts": ["ig1"]}).status_code == 409


def test_from_a_project_uses_its_transcript(me):
    me.mem.jobs.insert_one({"_id": "job1", "user_id": me.mem.users.find_one()["_id"], "state": "done",
                            "title": "My podcast", "manifest": {"language": "en", "clips": [
                                {"id": "clip_01", "hook": "The big idea", "lines": [{"words": [{"text": "hello"}]}]}]}})
    p = me.post("/posts", json={"job": "job1"}).json()
    assert me.written[0]["title"] == "My podcast" and "hello" in me.written[0]["transcript"]
    assert me.get(f"/posts/{p['id']}").json()["title"] == "My podcast"


def test_publish_now_and_scheduled(me, monkeypatch):
    sent = []
    monkeypatch.setattr(storage, "enabled", lambda: True)
    monkeypatch.setattr(storage, "upload", lambda path, folder, kind: {"url": f"https://res.cloudinary.com/x/{path.name}"})
    monkeypatch.setattr(publish, "instagram_carousel", lambda token, uid, urls, caption, prog:
                        sent.append((uid, urls, caption)) or {"id": "m1", "url": "https://instagram.com/p/1"})
    from clipforge import accounts
    monkeypatch.setattr(accounts, "instagram_token", lambda acct, save: "EAAtoken")
    monkeypatch.setattr(server.threading, "Thread", lambda target, args, daemon: type("T", (), {
        "start": lambda self: target(*args)})())
    p = me.post("/posts", json={"prompt": "grow"}).json()
    assert me.post(f"/posts/{p['id']}/publish", json={"accounts": ["yt1"]}).status_code == 400   # Instagram only
    me.post(f"/posts/{p['id']}/publish", json={"accounts": ["ig1"]})
    uid, urls, caption = sent[0]
    assert uid == "1784" and len(urls) == 4 and urls[0].endswith("slide_01.jpg")
    assert caption.startswith("Growing is a system.") and caption.endswith("#creator #growth")
    st = me.get(f"/posts/{p['id']}").json()["publish"]["ig1"]
    assert st["state"] == "done" and st["url"] == "https://instagram.com/p/1"
    later = time.time() + 3600
    me.post(f"/posts/{p['id']}/publish", json={"accounts": ["ig1"], "start_at": later})
    assert me.get(f"/posts/{p['id']}").json()["publish"]["ig1"]["state"] == "waiting"
    assert me.mem.schedule.count_documents({"post": p["id"]}) == 1
    me.post(f"/posts/{p['id']}/publish", json={"accounts": ["ig1"], "start_at": later + 60})
    assert me.mem.schedule.count_documents({"post": p["id"]}) == 1                # rescheduling replaces


def test_auto_clip_also_makes_a_carousel(me, monkeypatch):
    queued = []
    monkeypatch.setattr(server, "_queue_publish", lambda *a, **k: [])
    monkeypatch.setattr(server, "_queue_post", lambda pid, uid, accts, at: queued.append((pid, [a["id"] for a in accts], at)))
    uid = me.mem.users.find_one()["_id"]
    me.mem.watches.insert_one({"_id": "w1", "user_id": uid, "channel_id": "UC" + "m" * 22, "enabled": True,
                               "auto_publish": True, "accounts": ["ig1", "yt1"], "make_post": True, "every_hours": 6})
    me.mem.jobs.insert_one({"_id": "j1", "user_id": uid, "state": "done", "title": "New upload",
                            "auto": {"watch": "w1", "video": "v1", "publish": True},
                            "manifest": {"clips": [{"id": "clip_01", "score": 90}]}})
    server._after_job("j1")
    post_id = me.mem.jobs.find_one({"_id": "j1"})["auto"]["post"]
    assert me.mem.posts.find_one({"_id": post_id})["state"] == "ready"
    assert queued[0][0] == post_id and queued[0][1] == ["ig1"] and queued[0][2] > time.time() + 3 * 3600 - 60


def test_slide_rendering_handles_long_text(tmp_path):
    long = carousel.clean({"slides": [{"title": "word " * 40, "body": "x" * 400}] * 3, "caption": "", "hashtags": []})
    files = carousel.render(long, tmp_path, carousel.look_for("brand"))
    assert [f.name for f in files] == ["slide_01.jpg", "slide_02.jpg", "slide_03.jpg"]
    from PIL import Image
    assert Image.open(files[0]).size == (1080, 1350)


@pytest.fixture
def pexels(monkeypatch):
    from io import BytesIO

    from PIL import Image

    from clipforge import images
    buf = BytesIO()
    Image.new("RGB", (800, 1200), "#3366aa").save(buf, "PNG")
    png = buf.getvalue()
    monkeypatch.setenv("PIXABAY_API_KEY", "test")
    photo = lambda i: {"id": f"pixabay:{i}", "thumb": f"https://cdn.pixabay.com/{i}/m.jpg",  # noqa: E731
                       "full": f"https://pixabay.com/get/{i}.jpg", "alt": "", "page": f"https://pixabay.com/photos/{i}/",
                       "credit": f"Image by P{i} from Pixabay", "source": "pixabay"}
    calls = []
    monkeypatch.setattr(images, "search", lambda q, n=15, page=1: calls.append(q) or [photo(1), photo(2), photo(3)])
    monkeypatch.setattr(images, "photo", lambda pid: photo(int(pid.split(":")[1])))
    monkeypatch.setattr(images, "download", lambda p, dest: images.save_jpeg(png, dest))
    return {"calls": calls, "png": png}


def test_web_photos_are_added_and_editable(me, pexels):
    p = me.post("/posts", json={"prompt": "grow", "images": True}).json()
    p = me.get(f"/posts/{p['id']}").json()
    imgs = [s.get("image") for s in p["plan"]["slides"]]
    assert me.written[0]["images"] is True and len(pexels["calls"]) == 3
    assert [i and i["file"] for i in imgs] == ["img_pb_1.jpg", "img_pb_2.jpg", "img_pb_3.jpg", None]   # unique, none on cta
    assert imgs[0]["credit"] == "Image by P1 from Pixabay" and me.get(f"/posts/{p['id']}/files/img_pb_1.jpg").status_code == 200
    # pick another photo for slide 2, upload one for slide 4, remove slide 1's
    r = me.put(f"/posts/{p['id']}/slides/1/image", json={"photo_id": "pixabay:77"}).json()
    assert r["plan"]["slides"][1]["image"]["file"] == "img_pb_77.jpg" and r["version"] == 2
    r = me.post(f"/posts/{p['id']}/slides/3/image/upload", files={"file": ("me.png", pexels["png"], "image/png")}).json()
    assert r["plan"]["slides"][3]["image"]["source"] == "upload"
    bad = me.post(f"/posts/{p['id']}/slides/3/image/upload", files={"file": ("x.png", b"not an image", "image/png")})
    assert bad.status_code == 400
    r = me.delete(f"/posts/{p['id']}/slides/0/image").json()
    assert "image" not in r["plan"]["slides"][0]
    assert me.put(f"/posts/{p['id']}/slides/9/image", json={"photo_id": "pixabay:1"}).status_code == 404
    # the browser can't point a slide at a file that isn't this post's photo
    slides = r["plan"]["slides"]
    slides[0]["image"] = {"file": "img_px_999.jpg", "page": "javascript:alert(1)"}
    r = me.patch(f"/posts/{p['id']}", json={"slides": slides}).json()
    assert "image" not in r["plan"]["slides"][0] and r["plan"]["slides"][1]["image"]["file"] == "img_pb_77.jpg"
    other = TestClient(server.app)
    signup(other, "other@example.com")
    assert other.get(f"/posts/{p['id']}/files/img_pb_77.jpg").status_code == 404
    assert other.put(f"/posts/{p['id']}/slides/1/image", json={"photo_id": "pixabay:5"}).status_code == 404


def test_photos_off_without_a_key(me, monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    assert me.get("/images/status").json() == {"enabled": False}
    assert me.get("/images/search", params={"q": "laptop"}).status_code == 400
    p = me.post("/posts", json={"prompt": "grow", "images": True}).json()
    p = me.get(f"/posts/{p['id']}").json()
    assert p["state"] == "ready" and "PIXABAY_API_KEY" in p["note"] and not any(s.get("image") for s in p["plan"]["slides"])


def test_downloads_only_from_pexels(tmp_path):
    from clipforge import images
    with pytest.raises(images.ImageError):
        images.download({"full": "http://169.254.169.254/latest/meta-data", "source": "pixabay"}, tmp_path / "x.jpg")
    with pytest.raises(images.ImageError):
        images.download({"full": "https://evil.example/pixabay.com/a.jpg", "source": "pixabay"}, tmp_path / "x.jpg")
    with pytest.raises(images.ImageError):                       # a Pexels host can't pass for a Pixabay photo
        images.download({"full": "https://images.pexels.com/a.jpg", "source": "pixabay"}, tmp_path / "x.jpg")
    with pytest.raises(images.ImageError):
        images.photo("file:///etc/passwd")
    assert images.file_name("pixabay:12") == "img_pb_12.jpg" and images.file_name("pexels:9") == "img_px_9.jpg"


def test_photo_layouts_render(tmp_path):
    from PIL import Image
    Image.new("RGB", (1600, 900), "#884422").save(tmp_path / "img_px_1.jpg")
    plan = carousel.clean({"slides": [{"title": "Hook *here*", "body": "sub", "image": {"file": "img_px_1.jpg"}},
                                      {"title": "Point", "body": "text " * 30, "image": {"file": "img_px_1.jpg"}},
                                      {"title": "Bye", "body": "save", "image": {"file": "img_px_1.jpg"}}],
                           "caption": "", "hashtags": []})
    files = carousel.render(plan, tmp_path, carousel.look_for("light"))
    top = Image.open(files[1]).getpixel((540, 300))
    assert top != (246, 244, 239)                                   # photo band, not the plain light background
