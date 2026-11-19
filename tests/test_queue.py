"""Queue order, the 'ahead' count and asking for one video now."""
from fastapi.testclient import TestClient

from lectern import api, db, worker
from lectern.config import DATA_DIR

DATA_DIR.mkdir(parents=True, exist_ok=True)
db.migrate()
KEY = {"Authorization": "Bearer test-key-0123456789abcdef"}


def add(c, tmp_path, name, priority=0):
    f = tmp_path / name
    f.write_bytes(b"x")
    return c.post("/v1/videos", json={"path": str(f), "priority": priority}, headers=KEY).json()


def test_now_moves_a_video_to_the_front(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "MEDIA_ROOTS", [tmp_path.resolve()])
    with db.tx() as c:  # start from an empty queue
        c.execute("UPDATE video SET status='done' WHERE status IN ('queued', 'whisper', 'fetching', 'transcribing')")
    c = TestClient(api.app)
    a, b, d = add(c, tmp_path, "a.mp4"), add(c, tmp_path, "b.mp4"), add(c, tmp_path, "d.mp4")
    assert [c.get(f"/v1/videos/{v['id']}", headers=KEY).json()["ahead"] for v in (a, b, d)] == [0, 1, 2]
    opened = add(c, tmp_path, "d.mp4", priority=1)           # someone opened d: ahead of background work
    assert opened["ahead"] == 0
    r = c.post(f"/v1/videos/{b['id']}/now", headers=KEY).json()  # explicitly asked for b
    assert r["ahead"] == 0 and r["requested"] is True
    assert c.get(f"/v1/videos/{d['id']}", headers=KEY).json()["ahead"] == 1
    assert c.get(f"/v1/videos/{a['id']}", headers=KEY).json()["ahead"] == 2
    assert worker._next("queued")["id"] == b["id"]


def test_now_retries_a_failed_video(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "MEDIA_ROOTS", [tmp_path.resolve()])
    c = TestClient(api.app)
    v = add(c, tmp_path, "failed.mp4")
    db.conn().execute("UPDATE video SET status='failed', error='boom' WHERE id=?", (v["id"],))
    r = c.post(f"/v1/videos/{v['id']}/now", headers=KEY).json()
    assert r["status"] == "queued" and r["error"] is None and r["requested"] is True


def test_now_clears_a_network_wait_but_not_a_youtube_pause(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "MEDIA_ROOTS", [tmp_path.resolve()])
    c = TestClient(api.app)
    v = add(c, tmp_path, "net.mp4")
    db.conn().execute("UPDATE video SET next_try_at=datetime('now', '+10 minutes') WHERE id=?", (v["id"],))
    db.kv_set("yt_blocked_until", "0")
    assert c.post(f"/v1/videos/{v['id']}/now", headers=KEY).json()["next_try_at"] is None
    db.conn().execute("UPDATE video SET next_try_at=datetime('now', '+50 minutes') WHERE id=?", (v["id"],))
    import time
    db.kv_set("yt_blocked_until", str(time.time() + 3000))
    assert c.post(f"/v1/videos/{v['id']}/now", headers=KEY).json()["next_try_at"] is not None
    db.kv_set("yt_blocked_until", "0")
