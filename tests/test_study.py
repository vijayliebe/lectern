"""Summaries, notes and translation, with a stand-in for the Claude API (no network)."""
import json
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from lectern import api, db, llm, study, worker
from lectern.captions import Segment
from lectern.config import DATA_DIR

DATA_DIR.mkdir(parents=True, exist_ok=True)
db.migrate()
KEY = {"Authorization": "Bearer test-key-0123456789abcdef"}


class FakeClaude:
    """Records requests; answers with queued replies (text, stop_reason)."""
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.beta = NS(messages=NS(stream=self.stream))

    def stream(self, **kw):
        self.calls.append(kw)
        text, stop = self.replies.pop(0)
        msg = NS(stop_reason=stop, stop_details=NS(category="cyber") if stop == "refusal" else None,
                 content=[NS(type="text", text=text)], model=kw["model"], id="msg_test",
                 usage=NS(input_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=50))
        class Ctx:
            def __enter__(s): return NS(get_final_message=lambda: msg)
            def __exit__(s, *a): return False
        return Ctx()


def make_video(tmp_path, n=3, lang="en"):
    f = tmp_path / f"v{n}{lang}.mp4"
    f.write_bytes(b"x")
    with db.tx() as c:
        cur = c.execute("INSERT INTO video (path, size_bytes, mtime, title, status) VALUES (?,?,?,?, 'queued')",
                        (str(f), 1, 1.0, "Gradient descent"))
        vid = cur.lastrowid
    worker.save(vid, [Segment(i * 2.0, i * 2.0 + 2, f"line {i}") for i in range(n)], lang, "file", "x.vtt")
    return vid


def test_transcript_text_has_timestamps():
    t = llm.transcript_text([Segment(5, 6, "hello"), Segment(3725, 3726, "bye")], "T", "de")
    assert "[00:05] hello" in t and "[1:02:05] bye" in t and "Transcript language: de" in t


def test_summary_job_uses_cache_and_fallback(tmp_path, monkeypatch):
    fake = FakeClaude([("## In short\nIt explains [00:02] things.", "end_turn")])
    monkeypatch.setattr(llm, "_client", fake)
    vid = make_video(tmp_path)
    assert study.request(vid, "summary")["status"] == "queued"
    study.run(study.get(vid, "summary"))
    a = study.view(study.get(vid, "summary"))
    assert a["status"] == "done" and a["content"].startswith("## In short")
    call = fake.calls[0]
    assert call["fallbacks"] == "default" and llm.FALLBACK_BETA in call["betas"]
    first = call["messages"][0]["content"][0]
    assert first["cache_control"] == {"type": "ephemeral"} and "[00:02] line 1" in first["text"]
    assert "thinking" not in call and "temperature" not in call


def test_new_transcript_drops_old_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_client", FakeClaude([("summary", "end_turn")]))
    vid = make_video(tmp_path, n=4)
    study.request(vid, "summary"); study.run(study.get(vid, "summary"))
    worker.save(vid, [Segment(0, 1, "new")], "en", "whisper", "small")
    assert study.get(vid, "summary") is None


def test_refusal_is_a_clear_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_client", FakeClaude([("", "refusal")]))
    vid = make_video(tmp_path, n=5)
    study.request(vid, "notes")
    with pytest.raises(llm.LLMError, match="declined"):
        study.run(study.get(vid, "notes"))


def test_translation_keeps_timing_and_retries_bad_length(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "TRANSLATE_BATCH", 2)
    fake = FakeClaude([
        (json.dumps({"lines": ["one"]}), "end_turn"),           # wrong count -> retried
        (json.dumps({"lines": ["zero", "one"]}), "end_turn"),
        (json.dumps({"lines": ["two"]}), "end_turn"),
    ])
    monkeypatch.setattr(llm, "_client", fake)
    vid = make_video(tmp_path, n=3, lang="hi")
    study.request(vid, "translation"); study.run(study.get(vid, "translation"))
    segs = study.translated_segments(vid)
    assert [(s.start, s.text) for s in segs] == [(0.0, "zero"), (2.0, "one"), (4.0, "two")]
    assert fake.calls[0]["output_config"]["format"]["type"] == "json_schema"
    assert fake.calls[0]["output_config"]["effort"] == "low"


def test_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "MEDIA_ROOTS", [tmp_path.resolve()])
    c = TestClient(api.app)
    vid = make_video(tmp_path, n=2, lang="hi")
    assert c.get(f"/v1/videos/{vid}/summary").status_code == 401
    assert c.get(f"/v1/videos/{vid}/summary", headers=KEY).json()["status"] == "none"
    assert c.post(f"/v1/videos/{vid}/notes", headers=KEY).json()["status"] == "queued"
    assert c.post(f"/v1/videos/{vid}/bogus", headers=KEY).status_code == 404
    assert c.get(f"/v1/videos/{vid}/transcript?lang=en", headers=KEY).status_code == 409
    assert c.get(f"/v1/videos/{vid}/transcript", headers=KEY).status_code == 200
    # the fixed /retry route must not be swallowed by /{kind}
    r = c.post(f"/v1/videos/{vid}/retry", headers=KEY)
    assert r.status_code == 200 and r.json()["status"] == "queued"
