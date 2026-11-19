"""Chat with a video, with stand-ins for OpenRouter and Anthropic (no network, no cost)."""
import json
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from lectern import api, chat, db, llm, worker
from lectern.captions import Segment
from lectern.config import DATA_DIR

DATA_DIR.mkdir(parents=True, exist_ok=True)
db.migrate()
KEY = {"Authorization": "Bearer test-key-0123456789abcdef"}
HINDI = llm.transcript_text([Segment(0, 3, "नमस्ते दोस्तों"), Segment(65, 70, "ग्रेडिएंट डिसेंट एक एल्गोरिदम है")], "ML", "hi")


def parse(events):
    return [json.loads(e[len("data: "):]) for e in events]


def or_events(*pieces, finish="stop"):
    out = [{"model": "anthropic/claude-opus-5.5", "choices": [{"delta": {"content": p}}]} for p in pieces]
    out.append({"choices": [{"delta": {}, "finish_reason": finish}]})
    out.append({"choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 40}})
    return out


@pytest.fixture
def openrouter(monkeypatch):
    sent = []

    def use(evs):
        def fake(body):
            sent.append(body)
            yield from evs
        monkeypatch.setattr(chat, "LLM_PROVIDER", "openrouter")
        monkeypatch.setattr(llm, "_openrouter_stream", fake)
        return sent
    return use


def test_messages_keep_cached_transcript_first():
    conv = [{"role": "user", "content": "What is this about?"}, {"role": "assistant", "content": "Gradient descent [01:05]."},
            {"role": "user", "content": "Explain it simply"}]
    m = chat.build_messages(HINDI, conv, at=65)
    assert m[0]["role"] == "user" and m[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert m[0]["content"][0]["text"] == HINDI and m[0]["content"][1]["text"] == "What is this about?"
    assert [x["role"] for x in m] == ["user", "assistant", "user"]
    assert m[-1]["content"].startswith("[I'm at 01:05 in the video.]")
    # the cached prefix is byte-identical on the next turn
    m2 = chat.build_messages(HINDI, conv + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "more"}], None)
    assert m2[0] == {**m[0], "content": m[0]["content"]} and m2[0]["content"][0] == m[0]["content"][0]


def test_conversation_must_end_with_a_question():
    with pytest.raises(llm.LLMError):
        chat.build_messages(HINDI, [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}], None)


def test_openrouter_streams_pieces_in_english_rule(openrouter):
    sent = openrouter(or_events("Gradient ", "descent is ", "explained at [01:05]."))
    out = parse(chat.stream(HINDI, [{"role": "user", "content": "Is video mein kya bataya hai?"}]))
    assert "".join(e.get("delta", "") for e in out) == "Gradient descent is explained at [01:05]."
    assert out[-1]["done"] and out[-1]["input_tokens"] == 900
    body = sent[0]
    assert "Answer in English, unless the learner writes to you in another language" in body["messages"][0]["content"]
    assert body["reasoning"]["effort"] == "low" and body["stream"] is True


def test_errors_end_the_stream_cleanly(openrouter):
    openrouter(or_events(finish="content_filter"))
    assert parse(chat.stream(HINDI, [{"role": "user", "content": "q"}]))[-1] == {"error": "The model declined to answer that"}
    openrouter([{"error": {"message": "Provider returned error"}}])
    assert "Provider returned error" in parse(chat.stream(HINDI, [{"role": "user", "content": "q"}]))[-1]["error"]


def test_anthropic_provider(monkeypatch):
    class Stream:
        text_stream = iter(["Hello ", "there"])
        def get_final_message(self):
            return NS(stop_reason="end_turn", model="claude-opus-5-5",
                      usage=NS(input_tokens=10, cache_read_input_tokens=800, cache_creation_input_tokens=0, output_tokens=5))
    calls = []
    class Ctx:
        def __enter__(s): return Stream()
        def __exit__(s, *a): return False
    monkeypatch.setattr(chat, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(llm, "_client", NS(beta=NS(messages=NS(stream=lambda **kw: calls.append(kw) or Ctx()))))
    out = parse(chat.stream(HINDI, [{"role": "user", "content": "hi"}]))
    assert [e.get("delta") for e in out[:2]] == ["Hello ", "there"] and out[-1]["input_tokens"] == 810
    assert calls[0]["fallbacks"] == "default" and calls[0]["output_config"] == {"effort": "low"}


def test_chat_route(tmp_path, openrouter):
    openrouter(or_events("Answer [00:00]."))
    f = tmp_path / "talk.mp4"; f.write_bytes(b"x")
    with db.tx() as c:
        vid = c.execute("INSERT INTO video (path, size_bytes, mtime, status) VALUES (?,?,?, 'queued')", (str(f), 1, 1.0)).lastrowid
    c = TestClient(api.app)
    body = {"messages": [{"role": "user", "content": "What is it about?"}], "at": 12}
    assert c.post(f"/v1/videos/{vid}/chat", json=body).status_code == 401
    assert c.post(f"/v1/videos/{vid}/chat", json=body, headers=KEY).status_code == 409  # no transcript yet
    worker.save(vid, [Segment(0, 2, "hello")], "en", "file", "x.vtt")
    r = c.post(f"/v1/videos/{vid}/chat", json=body, headers=KEY)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
    assert evs[0] == {"delta": "Answer [00:00]."} and evs[-1]["done"]
    assert c.post(f"/v1/videos/{vid}/chat", json={"messages": [{"role": "system", "content": "x"}]}, headers=KEY).status_code == 422


def test_chat_uses_its_own_model_when_set(openrouter, monkeypatch):
    monkeypatch.setattr(chat, "LLM_CHAT_MODEL", "google/gemini-3-flash")
    sent = openrouter(or_events("ok"))
    list(chat.stream(HINDI, [{"role": "user", "content": "q"}]))
    assert sent[0]["model"] == "google/gemini-3-flash"
