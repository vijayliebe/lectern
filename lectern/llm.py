"""Summaries, study notes and English translations of a transcript, with an LLM.

Two providers: OpenRouter (the default; any model it offers, Claude Opus 5.5 unless changed) and the
Anthropic API directly. Every request puts the transcript first, marked for prompt caching, so the
summary and the study notes for one video share the cached transcript. Output is always in English,
whatever the language of the transcript.
"""
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import anthropic

from .captions import Segment
from .config import LLM_EFFORT, LLM_MODEL, LLM_PROVIDER, LLM_TRANSLATE_EFFORT, OPENROUTER_KEY, OPENROUTER_URL

log = logging.getLogger("lectern.llm")

# Server-side fallback: if the model declines a request, the API re-runs it on the model
# Anthropic recommends for that case, inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM = """You turn lecture and tutorial transcripts into study material for a learner.

Rules:
- Write in English, whatever language the transcript is in.
- Use only what the transcript says. Don't add facts, examples or claims that aren't in it. \
If the speaker is wrong about something, you may add a short "Note:" saying so.
- The transcript comes from captions or speech recognition, so it has no punctuation in places \
and some words are misheard. Fix obvious mishearings silently (for example technical terms).
- Timestamps: each transcript line starts with its time, like [12:34] or [1:02:03]. When you \
point to a moment, write the time in exactly that format, in square brackets, using a time that \
appears in the transcript. These become links that jump to that moment in the video.
- Format: Markdown. Math in LaTeX: $x^2$ inline, $$...$$ for display. Code in fenced code blocks \
with a language. No HTML."""

SUMMARY_TASK = """Write a summary of this video.

Use exactly this structure:

## In short
Two or three sentences: what the video covers and what the viewer will be able to do or understand after it.

## Key points
The most important ideas, as bullets. Each bullet is one or two sentences and ends with the timestamp where it is explained. Between 4 and 12 bullets, depending on how much the video covers.

## Outline
The video's parts in order. One line per part: `- [mm:ss] Title of the part: one sentence about it.`

Keep it short: the whole summary should be readable in about two minutes."""

NOTES_TASK = """Write detailed study notes for this video: notes a learner can study from instead of rewatching it.

Structure:
- Split the notes into the video's parts, in order. Start each part with a level-2 heading that begins with the part's timestamp: `## [mm:ss] Title`. Use no other level-2 headings except the last one below.
- Under each heading, explain the ideas clearly and completely, in your own well-organised words: definitions (term in **bold**), how things work and why, formulas, worked examples and code from the video, and common mistakes the speaker mentions. Use level-3 headings, bullets, tables and code blocks where they help. Add more timestamps in brackets for important moments inside a part.
- Finish with `## Review questions`: 5 to 10 questions that check understanding. Write each as a numbered item with the question in **bold**, and its answer as a nested bullet under it that starts with "Answer:".

Length: thorough but not padded. Cover everything the video teaches; skip greetings, sponsor messages and channel housekeeping."""

TRANSLATE_TASK = """Translate these subtitle lines into natural English. They are consecutive lines of one video, \
numbered. Translate each line on its own, keeping the meaning of the whole passage in mind. Keep \
technical terms correct. Return exactly {n} lines, in the same order, with no numbers."""


def fmt_ts(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def transcript_text(segments: list[Segment], title: str | None, language: str | None) -> str:
    head = f"Video title: {title or 'unknown'}\nTranscript language: {language or 'unknown'}\n\nTranscript:\n"
    return head + "\n".join(f"[{fmt_ts(s.start)}] {s.text}" for s in segments)


@dataclass
class Result:
    text: str
    model: str
    input_tokens: int
    output_tokens: int


class LLMError(Exception):
    pass


_client = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4)  # reads ANTHROPIC_API_KEY
    return _client


def _run(content: list[dict], effort: str, output_format: dict | None = None) -> Result:
    if LLM_PROVIDER == "openrouter":
        return _run_openrouter(content, effort, output_format)
    return _run_anthropic(content, effort, output_format)


# ---------------------------------------------------------------- OpenRouter (default)

def _openrouter_stream(body: dict):
    """POST to OpenRouter and yield the parsed server-sent events. Streaming avoids edge timeouts
    on answers that take minutes (long study notes)."""
    req = urllib.request.Request(
        f"{OPENROUTER_URL}/chat/completions", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json",
                 "X-Title": "Lectern"})
    with urllib.request.urlopen(req, timeout=300) as r:  # timeout = longest silence between events
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue  # blank lines and ": OPENROUTER PROCESSING" keep-alive comments
            data = line[5:].strip()
            if data == "[DONE]":
                return
            yield json.loads(data)


def _run_openrouter(content: list[dict], effort: str, output_format: dict | None) -> Result:
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}],
        "max_completion_tokens": 64000,
        "reasoning": {"effort": effort, "exclude": True},
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if output_format:
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "result", "strict": True, "schema": output_format["schema"]}}
        body["provider"] = {"require_parameters": True}  # only providers that honour the schema

    text, finish, model, usage = [], None, LLM_MODEL, {}
    for attempt in range(4):
        try:
            for ev in _openrouter_stream(body):
                if "error" in ev:
                    raise LLMError(f"OpenRouter: {ev['error'].get('message', ev['error'])}")
                model = ev.get("model") or model
                usage = ev.get("usage") or usage
                for ch in ev.get("choices") or []:
                    text.append((ch.get("delta") or {}).get("content") or "")
                    finish = ch.get("finish_reason") or finish
            break
        except urllib.error.HTTPError as e:
            detail = _error_detail(e)
            if e.code in (401, 403):
                raise LLMError(f"The OpenRouter API key was rejected: {detail}") from None
            if e.code == 402:
                raise LLMError("OpenRouter: not enough credits on the account") from None
            if e.code in (400, 404, 413, 422):
                raise LLMError(f"OpenRouter rejected the request: {detail}") from None
            if attempt == 3:  # 408, 429, 5xx, 524, 529: retried with back-off
                raise LLMError(f"OpenRouter error {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == 3:
                raise LLMError(f"Could not reach OpenRouter: {e}") from None
        text, finish, usage = [], None, {}
        time.sleep(5 * 2 ** attempt)

    if finish == "content_filter":
        raise LLMError("The model declined this video")
    if finish == "length":
        raise LLMError("The answer was too long and got cut off")
    if finish == "error":
        raise LLMError("The model provider failed while answering; try again")
    out = "".join(text).strip()
    if not out:
        raise LLMError("The model returned nothing")
    inp, cached = usage.get("prompt_tokens") or 0, (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    log.info("%s via OpenRouter: %d input tokens (%d from cache), %d output tokens, cost %s",
             model, inp, cached, usage.get("completion_tokens") or 0, usage.get("cost"))
    return Result(out, model, inp, usage.get("completion_tokens") or 0)


def _error_detail(e: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(e.read()[:2000])
        return str((body.get("error") or {}).get("message") or body)[:200]
    except (ValueError, AttributeError, OSError):
        return e.reason or str(e.code)


# ---------------------------------------------------------------- Anthropic API directly

def _run_anthropic(content: list[dict], effort: str, output_format: dict | None) -> Result:
    output_config = {"effort": effort}
    if output_format:
        output_config["format"] = output_format
    try:
        with client().beta.messages.stream(
            model=LLM_MODEL,
            max_tokens=64000,
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
            output_config=output_config,
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise LLMError("The Anthropic API key was rejected") from None
    except anthropic.PermissionDeniedError as e:
        raise LLMError(f"Not allowed: {e.message}") from None
    except anthropic.BadRequestError as e:
        raise LLMError(f"Request rejected: {e.message}") from None
    except anthropic.RateLimitError:
        raise LLMError("Rate limited by the Anthropic API; try again in a minute") from None
    except anthropic.APIStatusError as e:
        raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from None
    except anthropic.APIConnectionError:
        raise LLMError("Could not reach the Anthropic API") from None

    if msg.stop_reason == "refusal":
        category = getattr(msg.stop_details, "category", None) if msg.stop_details else None
        raise LLMError(f"The model declined this video ({category or 'no reason given'})")
    if msg.stop_reason == "max_tokens":
        raise LLMError("The answer was too long and got cut off")
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    if not text:
        raise LLMError("The model returned nothing")
    u = msg.usage
    inp = (u.input_tokens or 0) + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
    log.info("%s: %d input tokens (%d from cache), %d output tokens, message %s",
             msg.model, inp, u.cache_read_input_tokens or 0, u.output_tokens, msg.id)
    return Result(text, msg.model, inp, u.output_tokens or 0)


def _with_transcript(transcript: str, task: str) -> list[dict]:
    return [
        {"type": "text", "text": transcript, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": task},
    ]


def summary(transcript: str) -> Result:
    return _run(_with_transcript(transcript, SUMMARY_TASK), LLM_EFFORT)


def notes(transcript: str) -> Result:
    return _run(_with_transcript(transcript, NOTES_TASK), LLM_EFFORT)


TRANSLATE_BATCH = 120


def translate(segments: list[Segment]) -> tuple[list[Segment], Result]:
    """English subtitles with the original timing: lines go in numbered batches and come back as a
    JSON list of the same length."""
    out: list[Segment] = []
    model, tin, tout = LLM_MODEL, 0, 0
    for i in range(0, len(segments), TRANSLATE_BATCH):
        batch = segments[i:i + TRANSLATE_BATCH]
        numbered = "\n".join(f"{n + 1}. {s.text}" for n, s in enumerate(batch))
        schema = {"type": "json_schema", "schema": {
            "type": "object",
            "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
            "required": ["lines"], "additionalProperties": False}}
        lines = None
        for _attempt in range(2):
            r = _run([{"type": "text", "text": TRANSLATE_TASK.format(n=len(batch)) + "\n\n" + numbered}],
                     LLM_TRANSLATE_EFFORT, schema)
            model, tin, tout = r.model, tin + r.input_tokens, tout + r.output_tokens
            try:
                lines = json.loads(r.text)["lines"]
            except (ValueError, KeyError, TypeError):
                lines = None
            if isinstance(lines, list) and len(lines) == len(batch):
                break
            log.warning("translation batch at line %d came back with the wrong number of lines; retrying", i)
            lines = None
        if lines is None:
            raise LLMError("The translation didn't line up with the subtitles")
        out += [Segment(s.start, s.end, str(t).strip() or s.text) for s, t in zip(batch, lines)]
    return out, Result("", model, tin, tout)
