"""Two background workers.

  fetch:   queued -> subtitle file next to the video -> YouTube captions -> done
                                                       (nothing found) -> whisper
  whisper: whisper -> transcribing -> done

They are separate so an hour of speech-to-text never holds up the quick ones.
"""
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .captions import Segment
from .config import WHISPER, WHISPER_MODEL, YOUTUBE, YT_NAME_SEARCH
from .sources import local, whisper, youtube

log = logging.getLogger("lectern.worker")
wake_fetch = threading.Event()
wake_whisper = threading.Event()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _set(vid: int, **cols) -> None:
    cols["updated_at"] = _iso(time.time())
    db.conn().execute(f"UPDATE video SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", (*cols.values(), vid))


def save(vid: int, segs: list[Segment], language: str | None, source: str, detail: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM segment WHERE video_id=?", (vid,))
        c.execute("DELETE FROM artifact WHERE video_id=?", (vid,))  # made from the old transcript
        c.executemany("INSERT INTO segment (video_id, idx, start_s, end_s, text) VALUES (?,?,?,?,?)",
                      [(vid, i, s.start, s.end, s.text) for i, s in enumerate(segs)])
        c.execute("""UPDATE video SET status='done', source=?, source_detail=?, language=?, error=NULL,
                     progress=NULL, next_try_at=NULL, updated_at=datetime('now') WHERE id=?""",
                  (source, detail, language, vid))
    log.info("video %s: %d segments from %s (%s, %s)", vid, len(segs), source, detail, language)


def _clean_title(stem: str) -> str:
    return re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]$", "", stem).replace("_", " ").strip()


def _wait(vid: int, until: float, why: str) -> None:
    _set(vid, status="queued", next_try_at=_iso(until), error=why)


def _no_youtube(vid: int, why: str) -> None:
    if WHISPER:
        _set(vid, status="whisper", error=None, next_try_at=None)
        wake_whisper.set()
    else:
        _set(vid, status="failed", error=why)


def process(v: dict) -> None:
    vid, path = v["id"], Path(v["path"])
    if not path.is_file():
        return _set(vid, status="failed", error="file not found")
    info = local.info_json(path)

    found = local.subtitles(path, info.get("language"))
    if found:
        segs, lang, name = found
        return save(vid, segs, lang, "file", name)

    if not YOUTUBE:
        return _no_youtube(vid, "No subtitles found, and YouTube and speech-to-text are switched off")

    yid, how = v["youtube_id"], v["youtube_how"]
    if not yid and how != "search":  # not given and never searched before
        yid, how = info.get("youtube_id"), "info.json"
        if not yid:
            yid, how = local.id_from_filename(path), "filename"
    try:
        if not yid and how != "search" and YT_NAME_SEARCH:
            title = v["title"] or info.get("title") or _clean_title(path.stem)
            duration = v["duration_s"] or info.get("duration") or local.media_duration(path)
            yid, how = youtube.search(title, duration), "search"
            _set(vid, youtube_id=yid, youtube_how=how)
        elif yid and yid != v["youtube_id"]:
            _set(vid, youtube_id=yid, youtube_how=how)
        if not yid:
            return _no_youtube(vid, "Not found on YouTube and no subtitles")
        got = youtube.captions(yid)
    except youtube.Blocked as e:
        return _wait(vid, e.until, str(e))
    except youtube.Transient as e:
        return _wait(vid, time.time() + 600, f"Network problem, retrying soon: {e}"[:300])
    except youtube.Unavailable as e:
        return _no_youtube(vid, f"YouTube: {e}"[:300])
    if not got:
        return _no_youtube(vid, "The YouTube video has no captions")
    segs, lang, detail, _ = got
    save(vid, segs, lang, "youtube", detail)


def _next(status: str) -> dict | None:
    return db.one(f"""SELECT * FROM video WHERE status=? AND (next_try_at IS NULL OR next_try_at <= datetime('now'))
                      ORDER BY priority DESC, id LIMIT 1""", (status,))


def fetch_loop() -> None:
    while True:
        v = _next("queued")
        if not v:
            wake_fetch.wait(30)
            wake_fetch.clear()
            continue
        _set(v["id"], status="fetching")
        try:
            process(v)
        except Exception as e:  # keep the worker alive whatever happens
            log.exception("video %s failed", v["id"])
            _set(v["id"], status="failed", error=f"{type(e).__name__}: {e}"[:300])


def whisper_loop() -> None:
    whisper.lower_priority()
    while True:
        v = _next("whisper")
        if not v:
            wake_whisper.wait(60)
            wake_whisper.clear()
            continue
        vid = v["id"]
        _set(vid, status="transcribing", progress=0)
        last = [0.0]

        def progress(p: float):
            if time.time() - last[0] > 5:
                last[0] = time.time()
                _set(vid, progress=round(p, 3))
        try:
            t0 = time.time()
            segs, lang = whisper.transcribe(Path(v["path"]), progress)
            if not segs:
                _set(vid, status="failed", error="No speech found", progress=None)
                continue
            save(vid, segs, lang, "whisper", WHISPER_MODEL)
            log.info("video %s: speech-to-text took %.0f s", vid, time.time() - t0)
        except Exception as e:
            log.exception("speech-to-text failed for video %s", vid)
            _set(vid, status="failed", error=f"Speech-to-text: {type(e).__name__}: {e}"[:300], progress=None)


def start() -> None:
    threading.Thread(target=fetch_loop, name="fetch", daemon=True).start()
    if WHISPER:
        threading.Thread(target=whisper_loop, name="whisper", daemon=True).start()
