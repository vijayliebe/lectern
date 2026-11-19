"""OpenRouter provider, with the HTTP stream replaced by canned events (no network, no cost)."""
import io
import json
import urllib.error

import pytest

from lectern import llm
from lectern.captions import Segment
from lectern.sources.youtube import pick_track

HINDI = [Segment(0, 3, "नमस्ते दोस्तों, आज हम ग्रेडिएंट डिसेंट सीखेंगे"), Segment(3, 7, "यह एक ऑप्टिमाइज़ेशन एल्गोरिदम है")]


@pytest.fixture
def openrouter(monkeypatch):
    calls = []

    def use(*replies):
        it = iter(replies)

        def fake_stream(body):
            calls.append(body)
            r = next(it)
            if isinstance(r, Exception):
                raise r
            yield from r
        monkeypatch.setattr(llm, "LLM_PROVIDER", "openrouter")
        monkeypatch.setattr(llm, "LLM_MODEL", "anthropic/claude-opus-5.5")
        monkeypatch.setattr(llm, "_openrouter_stream", fake_stream)
        monkeypatch.setattr(llm.time, "sleep", lambda s: None)
        return calls
    return use


def events(text, finish="stop"):
    parts = [text[i:i + 7] for i in range(0, len(text), 7)]
    out = [{"model": "anthropic/claude-opus-5.5", "choices": [{"delta": {"content": p}, "finish_reason": None}]} for p in parts]
    out.append({"model": "anthropic/claude-opus-5.5", "choices": [{"delta": {}, "finish_reason": finish}]})
    out.append({"choices": [], "usage": {"prompt_tokens": 1200, "completion_tokens": 300,
                                         "prompt_tokens_details": {"cached_tokens": 1000}, "cost": 0.01}})
    return out


def test_hindi_transcript_goes_in_with_english_rule(openrouter):
    calls = openrouter(events("## In short\nGradient descent [00:03]."))
    r = llm.summary(llm.transcript_text(HINDI, "ग्रेडिएंट डिसेंट", "hi"))
    assert r.text.startswith("## In short") and r.input_tokens == 1200 and r.output_tokens == 300
    body = calls[0]
    assert body["model"] == "anthropic/claude-opus-5.5" and body["stream"] is True
    assert "Write in English, whatever language the transcript is in" in body["messages"][0]["content"]
    transcript_part = body["messages"][1]["content"][0]
    assert transcript_part["cache_control"] == {"type": "ephemeral"}
    assert "Transcript language: hi" in transcript_part["text"] and "[00:00] नमस्ते" in transcript_part["text"]
    assert body["reasoning"]["effort"] == "medium" and "response_format" not in body


def test_translation_uses_json_schema(openrouter, monkeypatch):
    calls = openrouter(events(json.dumps({"lines": ["Hello friends, today we will learn gradient descent",
                                                     "It is an optimization algorithm"]})))
    segs, _ = llm.translate(HINDI)
    assert [s.text for s in segs] == ["Hello friends, today we will learn gradient descent", "It is an optimization algorithm"]
    assert [s.start for s in segs] == [0, 3]
    rf = calls[0]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert calls[0]["provider"] == {"require_parameters": True} and calls[0]["reasoning"]["effort"] == "low"


def test_refusal_cutoff_and_stream_error(openrouter):
    openrouter(events("", "content_filter"))
    with pytest.raises(llm.LLMError, match="declined"):
        llm.notes("t")
    openrouter(events("half an answ", "length"))
    with pytest.raises(llm.LLMError, match="cut off"):
        llm.notes("t")
    openrouter([{"error": {"code": 502, "message": "Provider returned error"}}])
    with pytest.raises(llm.LLMError, match="Provider returned error"):
        llm.notes("t")


def http_error(code, msg):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps({"error": {"message": msg}}).encode()))


def test_http_errors(openrouter):
    openrouter(http_error(402, "Insufficient credits"))
    with pytest.raises(llm.LLMError, match="credits"):
        llm.summary("t")
    openrouter(http_error(401, "No auth credentials found"))
    with pytest.raises(llm.LLMError, match="key was rejected"):
        llm.summary("t")
    calls = openrouter(http_error(529, "overloaded"), http_error(429, "slow down"), events("ok"))
    before = len(calls)
    assert llm.summary("t").text == "ok" and len(calls) - before == 3  # retried twice, then succeeded


def test_hindi_youtube_track_choice():
    # Hindi video: creator's Hindi captions first, then YouTube's own Hindi speech recognition,
    # never the machine-translated English track
    auto = {"hi-orig": [], "hi": [], "en": [], "fr": []}
    assert pick_track("hi", {"hi": []}, auto) == ("manual", "hi")
    assert pick_track("hi", {}, auto) == ("auto", "hi-orig")
    assert pick_track("hi-IN", {}, {"hi-IN-orig": [], "en": []}) == ("auto", "hi-IN-orig")
    assert pick_track(None, {}, {"hi-orig": [], "en": [], "es": []}) == ("auto", "hi-orig")
