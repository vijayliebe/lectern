"""Things next to the video file: subtitle files and yt-dlp's .info.json."""
import json
import re
from pathlib import Path

from ..captions import Segment, lang_from_filename, parse_subtitles
from ..config import SUBTITLE_EXTS

YT_ID_IN_NAME = re.compile(r"\[([A-Za-z0-9_-]{11})\]$")


def info_json(video: Path) -> dict:
    """The parts of yt-dlp's .info.json we use, or {} if there is none."""
    f = video.with_name(video.stem + ".info.json")
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {"title": d.get("title"), "duration": d.get("duration"), "language": d.get("language")}
    if d.get("extractor_key", "Youtube").lower().startswith("youtube") and d.get("id"):
        out["youtube_id"] = d["id"]
    return out


def id_from_filename(video: Path) -> str | None:
    """yt-dlp's own default name ends with ' [<id>]'."""
    m = YT_ID_IN_NAME.search(video.stem)
    return m.group(1) if m else None


def subtitles(video: Path, prefer_lang: str | None) -> tuple[list[Segment], str | None, str] | None:
    """Best subtitle file next to the video: the video's language, then English, then any."""
    try:
        files = [p for p in video.parent.iterdir()
                 if p.name.startswith(video.stem + ".") and p.suffix.lower() in SUBTITLE_EXTS]
    except OSError:
        return None
    if not files:
        return None
    base = (prefer_lang or "").split("-")[0].lower() or None

    def rank(p: Path):
        lang = lang_from_filename(video.stem, p.name)
        return (lang != base if base else True, lang != "en", "-orig" not in p.name, p.name)

    for f in sorted(files, key=rank):
        try:
            segs = parse_subtitles(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if segs:
            return segs, lang_from_filename(video.stem, f.name) or base, f.name
    return None


def media_duration(video: Path) -> float | None:
    try:
        import av  # comes with faster-whisper
        with av.open(str(video)) as c:
            return c.duration / 1_000_000 if c.duration else None
    except Exception:
        return None
