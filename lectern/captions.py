"""Caption formats in and out. Everything is normalised to a list of segments: (start_s, end_s, text)."""
import html
import json
import re
from dataclasses import dataclass


@dataclass
class Segment:
    start: float
    end: float
    text: str


_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_TAG = re.compile(r"<[^>]+>")


def _secs(m: re.Match) -> float:
    h, mi, s, ms = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _clean(line: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub("", line))).strip()


def _cues(text: str):
    """Yield (start, end, [lines]) from VTT or SRT text.

    A cue's text runs from its timing line to the first truly empty line. YouTube's auto-captions
    put a line holding a single space right after the timing line, so whitespace-only lines are text.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if "-->" not in line:
            continue
        a, _, b = line.partition("-->")
        ma, mb = _TS.search(a), _TS.search(b)
        body = []
        while i < len(lines) and lines[i] != "" and "-->" not in lines[i]:
            body.append(lines[i])
            i += 1
        if ma and mb:
            yield _secs(ma), _secs(mb), body


def parse_subtitles(text: str) -> list[Segment]:
    """VTT or SRT. Also undoes YouTube's 'rolling' auto-captions, where every cue repeats the line before."""
    out: list[Segment] = []
    last = ""
    for start, end, lines in _cues(text):
        if end - start < 0.05:  # YouTube's 10 ms "snapshot" cues
            continue
        new = []
        for raw in lines:
            line = _clean(raw)
            if line and line != last:
                new.append(line)
                last = line
        if new:  # lines of one cue belong together; repeated lines from rolling captions are dropped
            out.append(Segment(start, end, " ".join(new)))
    # a line lasts until the next one starts (rolling captions overlap otherwise)
    for a, b in zip(out, out[1:]):
        if b.start > a.start:
            a.end = min(a.end, b.start)
    return out


def parse_json3(data: str | bytes) -> list[Segment]:
    """YouTube's json3 caption format."""
    doc = json.loads(data)
    out: list[Segment] = []
    for ev in doc.get("events", []):
        if "segs" not in ev or "tStartMs" not in ev:
            continue
        text = _clean("".join(s.get("utf8", "") for s in ev["segs"]).replace("\n", " "))
        if not text:
            continue
        start = ev["tStartMs"] / 1000
        out.append(Segment(start, start + ev.get("dDurationMs", 0) / 1000, text))
    for a, b in zip(out, out[1:]):
        if b.start > a.start:
            a.end = min(a.end, b.start) if a.end > a.start else b.start
    return out


def _vtt_ts(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def to_vtt(segments: list[Segment]) -> str:
    parts = ["WEBVTT", ""]
    for s in segments:
        end = s.end if s.end > s.start else s.start + 2
        parts += [f"{_vtt_ts(s.start)} --> {_vtt_ts(end)}", s.text, ""]
    return "\n".join(parts)


def lang_from_filename(video_stem: str, sub_name: str) -> str | None:
    """'Talk.en.vtt' -> 'en', 'Talk.en-orig.vtt' -> 'en', 'Talk.vtt' -> None."""
    parts = sub_name[len(video_stem):].split(".")[1:-1]
    if not parts:
        return None
    code = parts[-1].removesuffix("-orig")
    return code.split("-")[0].lower() if re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]+)*", code) else None
