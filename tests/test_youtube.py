import time

import pytest

from lectern import db
from lectern.config import DATA_DIR
from lectern.sources import youtube
from lectern.sources.youtube import pick_track

DATA_DIR.mkdir(parents=True, exist_ok=True)
db.migrate()

AUTO = {"en-orig": [], "en": [], "fr": [], "hi-orig": [], "hi": [], "de-DE-orig": []}


def test_manual_in_video_language_wins():
    assert pick_track("hi", {"hi": [], "en": []}, AUTO) == ("manual", "hi")


def test_original_auto_track_beats_translations():
    assert pick_track("hi-IN", {}, AUTO) == ("auto", "hi-orig")
    assert pick_track("en-US", {}, AUTO) == ("auto", "en-orig")


def test_manual_english_when_no_original():
    assert pick_track("ta", {"en-GB": []}, {"en": [], "fr": []}) == ("manual", "en-GB")


def test_never_a_translation():
    assert pick_track("ta", {}, {"en": [], "fr": []}) is None
    assert pick_track(None, {}, {"en": [], "fr": [], "de-orig": [], "es-orig": []}) is None


def test_unknown_language():
    assert pick_track(None, {}, {"en": [], "en-orig": [], "de-orig": []}) == ("auto", "en-orig")
    assert pick_track(None, {"live_chat": [], "pt": []}, {}) == ("manual", "pt")


def test_back_off_doubles_and_resets(monkeypatch):
    monkeypatch.setattr(youtube, "YT_MIN_GAP_S", 0)
    monkeypatch.setattr(youtube, "YT_MAX_GAP_S", 0)
    db.kv_set("yt_blocked_until", "0"); db.kv_set("yt_backoff_level", "0")

    def boom():
        raise Exception("ERROR: [youtube] x: Sign in to confirm you're not a bot")
    with pytest.raises(youtube.Blocked) as e1:
        youtube._request(boom, short_gap=True)
    first = e1.value.until - time.time()
    assert 3500 < first <= 3600
    # while blocked, nothing is sent at all
    with pytest.raises(youtube.Blocked):
        youtube._request(lambda: pytest.fail("must not be called"))
    db.kv_set("yt_blocked_until", "0")
    with pytest.raises(youtube.Blocked) as e2:
        youtube._request(lambda: (_ for _ in ()).throw(Exception("HTTP Error 429: Too Many Requests")), short_gap=True)
    assert 7100 < e2.value.until - time.time() <= 7200
    db.kv_set("yt_blocked_until", "0")
    assert youtube._request(lambda: "ok", short_gap=True) == "ok"
    assert youtube.state()["backoff_level"] == 0


def test_error_classes(monkeypatch):
    db.kv_set("yt_blocked_until", "0")
    with pytest.raises(youtube.Transient):
        youtube._request(lambda: (_ for _ in ()).throw(Exception("<urlopen error timed out>")), short_gap=True)
    with pytest.raises(youtube.Unavailable):
        youtube._request(lambda: (_ for _ in ()).throw(Exception("Video unavailable. This video is private")), short_gap=True)


def test_cookie_file_is_copied_not_shared(tmp_path, monkeypatch):
    src = tmp_path / "cookies.txt"
    src.write_text("# Netscape HTTP Cookie File\n")
    src.chmod(0o444)  # like a read-only mount
    monkeypatch.setattr(youtube, "YT_COOKIES", str(src))
    monkeypatch.setattr(youtube, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    with youtube._ydl() as y:
        used = y.params["cookiefile"]
    assert used == str(tmp_path / "data" / "yt-cookies.txt")
    assert src.read_text() == "# Netscape HTTP Cookie File\n"
