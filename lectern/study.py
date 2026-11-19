"""Background jobs that turn a finished transcript into a summary, study notes or an English translation.

Jobs are made only when asked for (they cost money) and are kept until the transcript changes.
One job runs at a time.
"""
import json
import logging
import threading

from . import db, llm
from .captions import Segment

log = logging.getLogger("lectern.study")
KINDS = ("summary", "notes", "translation")
wake = threading.Event()


def segments(video_id: int) -> list[Segment]:
    return [Segment(r["start_s"], r["end_s"], r["text"]) for r in
            db.all_("SELECT start_s, end_s, text FROM segment WHERE video_id=? ORDER BY idx", (video_id,))]


def view(a: dict | None) -> dict:
    if not a:
        return {"status": "none"}
    out = {k: a[k] for k in ("kind", "status", "model", "input_tokens", "output_tokens", "error", "updated_at")}
    if a["status"] == "done" and a["kind"] != "translation":
        out["content"] = a["content"]
    return out


def get(video_id: int, kind: str) -> dict | None:
    return db.one("SELECT * FROM artifact WHERE video_id=? AND kind=?", (video_id, kind))


def request(video_id: int, kind: str, again: bool = False) -> dict:
    """Queue a job unless it's already there (or done, unless `again`)."""
    with db.tx() as c:
        a = c.execute("SELECT status FROM artifact WHERE video_id=? AND kind=?", (video_id, kind)).fetchone()
        if a is None:
            c.execute("INSERT INTO artifact (video_id, kind) VALUES (?, ?)", (video_id, kind))
        elif a["status"] == "failed" or (again and a["status"] == "done"):
            c.execute("""UPDATE artifact SET status='queued', error=NULL, requested_at=datetime('now'),
                         updated_at=datetime('now') WHERE video_id=? AND kind=?""", (video_id, kind))
    wake.set()
    return view(get(video_id, kind))


def _finish(video_id: int, kind: str, content: str, r: llm.Result) -> None:
    db.conn().execute("""UPDATE artifact SET status='done', content=?, model=?, input_tokens=?, output_tokens=?,
                         error=NULL, updated_at=datetime('now') WHERE video_id=? AND kind=?""",
                      (content, r.model, r.input_tokens, r.output_tokens, video_id, kind))


def run(a: dict) -> None:
    vid, kind = a["video_id"], a["kind"]
    v = db.one("SELECT * FROM video WHERE id=?", (vid,))
    segs = segments(vid)
    if not v or v["status"] != "done" or not segs:
        raise llm.LLMError("There is no transcript for this video yet")
    if kind == "translation":
        out, r = llm.translate(segs)
        _finish(vid, kind, json.dumps([[s.start, s.end, s.text] for s in out], ensure_ascii=False), r)
    else:
        text = llm.transcript_text(segs, v["title"], v["language"])
        r = llm.summary(text) if kind == "summary" else llm.notes(text)
        _finish(vid, kind, r.text, r)
    log.info("video %s: %s done", vid, kind)


def loop() -> None:
    while True:
        a = db.one("SELECT * FROM artifact WHERE status='queued' ORDER BY requested_at LIMIT 1")
        if not a:
            wake.wait(60)
            wake.clear()
            continue
        db.conn().execute("UPDATE artifact SET status='working', updated_at=datetime('now') WHERE video_id=? AND kind=?",
                          (a["video_id"], a["kind"]))
        try:
            run(a)
        except llm.LLMError as e:
            log.warning("video %s: %s failed: %s", a["video_id"], a["kind"], e)
            _fail(a, str(e))
        except Exception as e:  # keep the worker alive
            log.exception("video %s: %s failed", a["video_id"], a["kind"])
            _fail(a, f"{type(e).__name__}: {e}")


def _fail(a: dict, msg: str) -> None:
    db.conn().execute("UPDATE artifact SET status='failed', error=?, updated_at=datetime('now') WHERE video_id=? AND kind=?",
                      (msg[:300], a["video_id"], a["kind"]))


def translated_segments(video_id: int) -> list[Segment] | None:
    a = get(video_id, "translation")
    if not a or a["status"] != "done":
        return None
    return [Segment(s, e, t) for s, e, t in json.loads(a["content"])]


def usage() -> dict:
    rows = db.all_("SELECT kind, status, COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o FROM artifact GROUP BY kind, status")
    out = {k: {"done": 0, "queued": 0, "working": 0, "failed": 0} for k in KINDS}
    tokens = {"input": 0, "output": 0}
    for r in rows:
        out.setdefault(r["kind"], {})[r["status"]] = r["n"]
        tokens["input"] += r["i"] or 0
        tokens["output"] += r["o"] or 0
    return {"jobs": out, "tokens": tokens}


def start() -> None:
    threading.Thread(target=loop, name="study", daemon=True).start()
