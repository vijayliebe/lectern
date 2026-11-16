"""YouTube captions via yt-dlp, carefully.

* One request at a time, with a random pause between requests (LECTERN_YT_MIN_GAP..MAX_GAP seconds).
* If YouTube answers 429 or asks for a bot check, every YouTube request stops for LECTERN_YT_BACKOFF
  seconds, doubling each time it happens again (max 24 h). A success resets it.
* Machine-translated caption tracks are never requested: they are what YouTube rate-limits first.
"""
import difflib
import random
import re
import threading
import time

from .. import db
from ..captions import Segment, parse_json3, parse_subtitles
import shutil
from pathlib import Path

from ..config import DATA_DIR, YT_BACKOFF_S, YT_COOKIES, YT_MAX_GAP_S, YT_MIN_GAP_S

BLOCK_SIGNS = ("http error 429", "too many requests", "sign in to confirm", "not a bot", "rate-limit",
               "rate limit", "unusual traffic")
TRANSIENT_SIGNS = ("urlopen error", "timed out", "temporary failure", "name resolution", "connection reset",
                   "remote end closed", "network is unreachable", "http error 5")


class Blocked(Exception):
    """YouTube pushed back. `until` is when to try again (epoch seconds)."""
    def __init__(self, until: float):
        super().__init__(f"YouTube is limiting requests; trying again at {time.strftime('%H:%M', time.localtime(until))}")
        self.until = until


class Transient(Exception):
    """Network trouble; try again in a few minutes."""


class Unavailable(Exception):
    """The video is private, removed, or otherwise has no usable page."""


_lock = threading.Lock()
_last = 0.0


def blocked_until() -> float:
    v = db.kv_get("yt_blocked_until")
    return float(v) if v else 0.0


def state() -> dict:
    until = blocked_until()
    return {"blocked_until": until if until > time.time() else None,
            "backoff_level": int(db.kv_get("yt_backoff_level") or 0)}


def _push_back() -> Blocked:
    level = int(db.kv_get("yt_backoff_level") or 0) + 1
    until = time.time() + min(YT_BACKOFF_S * 2 ** (level - 1), 86400)
    db.kv_set("yt_backoff_level", str(level))
    db.kv_set("yt_blocked_until", str(until))
    return Blocked(until)


def _ok() -> None:
    if db.kv_get("yt_backoff_level") not in (None, "0"):
        db.kv_set("yt_backoff_level", "0")


def _request(fn, short_gap: bool = False):
    """Run one YouTube request, respecting the pause and the back-off. Classifies errors."""
    global _last
    with _lock:
        until = blocked_until()
        if until > time.time():
            raise Blocked(until)
        gap = random.uniform(1, 3) if short_gap else random.uniform(YT_MIN_GAP_S, YT_MAX_GAP_S)
        wait = _last + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            result = fn()
        except Exception as e:  # yt-dlp raises DownloadError/ExtractorError; urllib raises HTTPError
            msg = str(e).lower()
            if any(s in msg for s in BLOCK_SIGNS):
                raise _push_back() from None
            if any(s in msg for s in TRANSIENT_SIGNS):
                raise Transient(str(e)) from None
            raise Unavailable(str(e)) from None
        finally:
            _last = time.time()
        _ok()
        return result


def _ydl(**extra):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "socket_timeout": 30, "extractor_retries": 0, "retries": 0, **extra}
    cookies = _cookie_copy()
    if cookies:
        opts["cookiefile"] = cookies
    return yt_dlp.YoutubeDL(opts)


def _cookie_copy() -> str | None:
    """yt-dlp writes the cookie file back every time it closes. To share a cookies.txt with MeTube
    safely (even mounted read-only), work on a private copy, refreshed whenever the original changes."""
    if not YT_COOKIES:
        return None
    src, dst = Path(YT_COOKIES), DATA_DIR / "yt-cookies.txt"
    try:
        if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
            shutil.copyfile(src, dst)
            dst.chmod(0o600)
    except OSError:
        return str(dst) if dst.exists() else None
    return str(dst)


def _norm(t: str) -> str:
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", t.lower())
    return " ".join(re.findall(r"\w+", t))


def search(title: str, duration: float) -> str | None:
    """Find the video by name. Accepted only if the length matches (±2 s) and the title is similar."""
    if not title or not duration:
        return None
    def run():
        with _ydl(extract_flat="in_playlist") as y:
            return y.extract_info(f"ytsearch5:{title}", download=False)
    res = _request(run)
    best, best_score = None, 0.0
    for e in (res or {}).get("entries") or []:
        d = e.get("duration")
        if not d or abs(d - duration) > max(2.0, duration * 0.005):
            continue
        score = difflib.SequenceMatcher(None, _norm(title), _norm(e.get("title") or "")).ratio()
        if score > best_score:
            best, best_score = e.get("id"), score
    return best if best_score >= 0.5 else None


def _base(code: str | None) -> str | None:
    return code.split("-")[0].lower() if code else None


def pick_track(language: str | None, manual: dict, auto: dict) -> tuple[str, str] | None:
    """Return (kind, key). Never a machine translation."""
    manual = {k: v for k, v in manual.items() if k != "live_chat"}
    lang = _base(language)
    origs = [k for k in auto if k.endswith("-orig")]
    if lang:
        for k in sorted(manual, key=lambda k: k != language):
            if _base(k) == lang:
                return "manual", k
        for k in origs:
            if _base(k) == lang:
                return "auto", k
    for k in sorted(manual, key=lambda k: k != "en"):
        if _base(k) == "en":
            return "manual", k
    if not lang:
        if len(manual) == 1:
            return "manual", next(iter(manual))
        if "en-orig" in origs:
            return "auto", "en-orig"
        if len(origs) == 1:
            return "auto", origs[0]
    return None


def captions(video_id: str) -> tuple[list[Segment], str | None, str, dict] | None:
    """(segments, language, detail, info) or None when the video has no usable caption track."""
    def run():
        with _ydl() as y:
            return y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False, process=False)
    info = _request(run)
    meta = {"title": info.get("title"), "duration": info.get("duration"), "language": info.get("language")}
    choice = pick_track(info.get("language"), info.get("subtitles") or {}, info.get("automatic_captions") or {})
    if not choice:
        return None
    kind, key = choice
    tracks = (info.get("subtitles") if kind == "manual" else info.get("automatic_captions"))[key]
    fmt = next((t for t in tracks if t.get("ext") == "json3"), None) or next((t for t in tracks if t.get("ext") == "vtt"), None)
    if not fmt:
        return None
    def fetch():
        with _ydl() as y:
            return y.urlopen(fmt["url"]).read()
    data = _request(fetch, short_gap=True)
    segs = parse_json3(data) if fmt["ext"] == "json3" else parse_subtitles(data.decode("utf-8", "replace"))
    if not segs:
        return None
    return segs, _base(key.removesuffix("-orig")), f"{kind} {key}", meta
