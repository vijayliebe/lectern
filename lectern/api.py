"""Lectern HTTP API. Every endpoint except /v1/health needs `Authorization: Bearer <LECTERN_API_KEY>`."""
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import __version__, db, worker
from .captions import Segment, to_vtt
from .config import API_KEY, DATA_DIR, MEDIA_ROOTS, WHISPER, WHISPER_MODEL, YOUTUBE, YT_NAME_SEARCH
from .sources import youtube

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lectern")


@asynccontextmanager
async def lifespan(_app):
    if len(API_KEY) < 16 or API_KEY == "change-me-to-a-random-string":
        raise RuntimeError("Set LECTERN_API_KEY to your own random string of at least 16 characters "
                           "(e.g. openssl rand -hex 24), and the same value in EduNest's EDUNEST_AI_KEY")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.migrate()
    worker.start()
    log.info("lectern %s: media roots %s, youtube=%s (name search=%s), whisper=%s",
             __version__, [str(r) for r in MEDIA_ROOTS], YOUTUBE, YT_NAME_SEARCH, WHISPER and WHISPER_MODEL)
    yield


app = FastAPI(title="Lectern", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def auth(authorization: str = Header(default="")) -> None:
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(token.encode(), API_KEY.encode()):
        raise HTTPException(401, "bad or missing API key")


class VideoIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    title: str | None = Field(default=None, max_length=500)
    duration_s: float | None = Field(default=None, ge=0)
    youtube_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{11}$")
    priority: int = Field(default=0, ge=0, le=1)  # 1 = someone is waiting for it (e.g. opened the video)


def _check_path(raw: str) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        raise HTTPException(400, "path must be absolute")
    p = p.resolve()
    if not any(p.is_relative_to(r) for r in MEDIA_ROOTS):
        raise HTTPException(403, "path is outside LECTERN_MEDIA_ROOTS")
    if not p.is_file():
        raise HTTPException(404, "file not found")
    return p


def _view(v: dict) -> dict:
    return {k: v[k] for k in ("id", "path", "status", "progress", "source", "source_detail", "language",
                              "error", "youtube_id", "next_try_at", "updated_at")}


def _upsert(body: VideoIn) -> dict:
    p = _check_path(body.path)
    st = p.stat()
    with db.tx() as c:
        v = c.execute("SELECT * FROM video WHERE path=?", (str(p),)).fetchone()
        if v is None:
            c.execute("""INSERT INTO video (path, size_bytes, mtime, title, duration_s, youtube_id, youtube_how, priority)
                         VALUES (?,?,?,?,?,?,?,?)""",
                      (str(p), st.st_size, st.st_mtime, body.title, body.duration_s, body.youtube_id,
                       "given" if body.youtube_id else None, body.priority))
        else:
            v = dict(v)
            changed = (v["size_bytes"], v["mtime"]) != (st.st_size, st.st_mtime)
            new_id = body.youtube_id and body.youtube_id != v["youtube_id"]
            if changed or (new_id and v["source"] != "file"):
                # the file changed or we were told the real YouTube id: start over
                c.execute("DELETE FROM segment WHERE video_id=?", (v["id"],))
                c.execute("""UPDATE video SET size_bytes=?, mtime=?, status='queued', source=NULL, source_detail=NULL,
                             language=NULL, error=NULL, progress=NULL, next_try_at=NULL,
                             youtube_id=?, youtube_how=? WHERE id=?""",
                          (st.st_size, st.st_mtime, body.youtube_id, "given" if body.youtube_id else None, v["id"]))
            c.execute("""UPDATE video SET title=COALESCE(?, title), duration_s=COALESCE(?, duration_s),
                         priority=MAX(priority, ?) WHERE id=?""",
                      (body.title, body.duration_s, body.priority, v["id"]))
        v = dict(c.execute("SELECT * FROM video WHERE path=?", (str(p),)).fetchone())
    if v["status"] == "queued":
        worker.wake_fetch.set()
    return _view(v)


@app.get("/v1/health")
def health():
    return {"ok": True}


@app.post("/v1/videos", dependencies=[Depends(auth)])
def add_video(body: VideoIn):
    """Register a video (idempotent by path). Its transcript is made in the background."""
    return _upsert(body)


@app.post("/v1/videos/batch", dependencies=[Depends(auth)])
def add_videos(body: list[VideoIn]):
    out = []
    for item in body[:5000]:
        try:
            out.append(_upsert(item))
        except HTTPException as e:
            out.append({"path": item.path, "error": e.detail})
    return out


def _get(video_id: int) -> dict:
    v = db.one("SELECT * FROM video WHERE id=?", (video_id,))
    if not v:
        raise HTTPException(404, "unknown video")
    return v


@app.get("/v1/videos/{video_id}", dependencies=[Depends(auth)])
def get_video(video_id: int):
    return _view(_get(video_id))


@app.get("/v1/videos/{video_id}/transcript", dependencies=[Depends(auth)])
def get_transcript(video_id: int, format: str = "json"):
    v = _get(video_id)
    if v["status"] != "done":
        raise HTTPException(409, f"transcript not ready (status: {v['status']})")
    segs = db.all_("SELECT start_s, end_s, text FROM segment WHERE video_id=? ORDER BY idx", (video_id,))
    if format == "vtt":
        return PlainTextResponse(to_vtt([Segment(s["start_s"], s["end_s"], s["text"]) for s in segs]),
                                 media_type="text/vtt")
    return {"video": _view(v), "segments": [{"start": s["start_s"], "end": s["end_s"], "text": s["text"]} for s in segs]}


@app.post("/v1/videos/{video_id}/retry", dependencies=[Depends(auth)])
def retry(video_id: int, whisper: bool = False):
    """Start over. With ?whisper=true, skip subtitles/YouTube and run speech-to-text."""
    v = _get(video_id)
    if v["status"] in ("fetching", "transcribing"):
        raise HTTPException(409, "already working on it")
    if whisper and not WHISPER:
        raise HTTPException(400, "speech-to-text is switched off")
    with db.tx() as c:
        c.execute("DELETE FROM segment WHERE video_id=?", (video_id,))
        c.execute("""UPDATE video SET status=?, source=NULL, source_detail=NULL, language=NULL, error=NULL,
                     progress=NULL, next_try_at=NULL, priority=1,
                     youtube_id=CASE WHEN youtube_how='given' THEN youtube_id END,
                     youtube_how=CASE WHEN youtube_how='given' THEN youtube_how END WHERE id=?""",
                  ("whisper" if whisper else "queued", video_id))
    (worker.wake_whisper if whisper else worker.wake_fetch).set()
    return _view(_get(video_id))


@app.get("/v1/stats", dependencies=[Depends(auth)])
def stats():
    counts = {r["status"]: r["n"] for r in db.all_("SELECT status, COUNT(*) n FROM video GROUP BY status")}
    sources = {r["source"]: r["n"] for r in db.all_("SELECT source, COUNT(*) n FROM video WHERE status='done' GROUP BY source")}
    current = db.one("SELECT id, path, progress FROM video WHERE status='transcribing' LIMIT 1")
    return {"version": __version__, "counts": counts, "sources": sources, "transcribing": current,
            "youtube": {"enabled": YOUTUBE, "name_search": YT_NAME_SEARCH, **youtube.state()},
            "whisper": {"enabled": WHISPER, "model": WHISPER_MODEL}}
