"""Chat with one video: the whole transcript goes into the prompt (no retrieval needed for a single
video), the conversation follows, and the answer streams back piece by piece.

Lectern keeps no chat history: the caller sends the conversation each time. The transcript is the
first block of the first message, marked for prompt caching, so follow-up questions about the same
video reuse it instead of paying for it again.
"""
import json
import logging
import time
import urllib.error
from collections.abc import Iterator

import anthropic

from . import llm
from .config import LLM_CHAT_EFFORT, LLM_MODEL, LLM_PROVIDER

log = logging.getLogger("lectern.chat")

SYSTEM = """You help a learner who is watching a video lecture or tutorial. You have the video's \
full transcript. Answer their questions about it.

Rules:
- Answer in English, unless the learner writes to you in another language: then answer in that \
language. The transcript may be in another language (for example Hindi); that doesn't change the \
language of your answer.
- Base your answers on the video. When something is explained in the video, point to the moment \
with its timestamp in square brackets, exactly as written in the transcript, like [12:34] or \
[1:02:03]. These become links that jump to that moment.
- If the video doesn't cover what they ask, say so in one short sentence. You may then answer from \
general knowledge, but say clearly that this part is not from the video.
- The transcript comes from captions or speech recognition, so some words are misheard. Read past \
obvious mishearings.
- Be clear and to the point: usually a short paragraph or a few bullets. Go longer only when the \
learner asks for detail, a step-by-step explanation, practice questions or code.
- Format: Markdown. Math in LaTeX: $x^2$ inline, $$...$$ for display. Code in fenced code blocks. No HTML."""

MAX_TURNS = 40          # messages kept from the conversation (older ones are dropped)
MAX_MESSAGE_CHARS = 8000


def build_messages(transcript: str, conversation: list[dict], at: float | None) -> list[dict]:
    """[transcript + first question, answer, question, ...]. The transcript block never changes,
    so the cached prefix stays valid for the whole conversation."""
    conv = [{"role": m["role"], "content": str(m["content"])[:MAX_MESSAGE_CHARS]}
            for m in conversation if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()]
    conv = conv[-MAX_TURNS:]
    while conv and conv[0]["role"] != "user":
        conv.pop(0)
    if not conv or conv[-1]["role"] != "user":
        raise llm.LLMError("The conversation must end with a question")
    if at is not None:
        conv[-1] = {"role": "user", "content": f"[I'm at {llm.fmt_ts(at)} in the video.]\n\n{conv[-1]['content']}"}
    first = [{"type": "text", "text": transcript, "cache_control": {"type": "ephemeral"}},
             {"type": "text", "text": conv[0]["content"]}]
    return [{"role": "user", "content": first}] + conv[1:]


# Each stream yields ("delta", text) pieces, then exactly one ("done", Result) or raises LLMError.

def _openrouter(messages: list[dict]) -> Iterator[tuple[str, object]]:
    body = {"model": LLM_MODEL, "messages": [{"role": "system", "content": SYSTEM}] + messages,
            "max_completion_tokens": 16000, "reasoning": {"effort": LLM_CHAT_EFFORT, "exclude": True},
            "stream": True, "stream_options": {"include_usage": True}}
    for attempt in range(3):
        started, finish, model, usage, parts = False, None, LLM_MODEL, {}, []
        try:
            for ev in llm._openrouter_stream(body):
                if "error" in ev:
                    raise llm.LLMError(f"OpenRouter: {ev['error'].get('message', ev['error'])}")
                model = ev.get("model") or model
                usage = ev.get("usage") or usage
                for ch in ev.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content") or ""
                    if piece:
                        started = True
                        parts.append(piece)
                        yield "delta", piece
                    finish = ch.get("finish_reason") or finish
            break
        except urllib.error.HTTPError as e:
            detail = llm._error_detail(e)
            if e.code in (401, 403):
                raise llm.LLMError(f"The OpenRouter API key was rejected: {detail}") from None
            if e.code == 402:
                raise llm.LLMError("OpenRouter: not enough credits on the account") from None
            if started or e.code in (400, 404, 413, 422) or attempt == 2:
                raise llm.LLMError(f"OpenRouter error {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if started or attempt == 2:
                raise llm.LLMError(f"Could not reach OpenRouter: {e}") from None
        time.sleep(2 * 2 ** attempt)  # only before any text was sent
    if finish == "content_filter":
        raise llm.LLMError("The model declined to answer that")
    if finish == "error":
        raise llm.LLMError("The model provider failed while answering; try again")
    yield "done", llm.Result("".join(parts), model, usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0)


def _anthropic(messages: list[dict]) -> Iterator[tuple[str, object]]:
    parts = []
    try:
        with llm.client().beta.messages.stream(
            model=LLM_MODEL, max_tokens=16000, system=SYSTEM, messages=messages,
            output_config={"effort": LLM_CHAT_EFFORT},
            betas=[llm.FALLBACK_BETA], fallbacks="default",
        ) as stream:
            for text in stream.text_stream:
                parts.append(text)
                yield "delta", text
            msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise llm.LLMError("The Anthropic API key was rejected") from None
    except anthropic.RateLimitError:
        raise llm.LLMError("Rate limited by the Anthropic API; try again in a minute") from None
    except anthropic.APIStatusError as e:
        raise llm.LLMError(f"Anthropic API error {e.status_code}: {e.message}") from None
    except anthropic.APIConnectionError:
        raise llm.LLMError("Could not reach the Anthropic API") from None
    if msg.stop_reason == "refusal":
        raise llm.LLMError("The model declined to answer that")
    u = msg.usage
    inp = (u.input_tokens or 0) + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
    yield "done", llm.Result("".join(parts), msg.model, inp, u.output_tokens or 0)


def stream(transcript: str, conversation: list[dict], at: float | None = None) -> Iterator[str]:
    """Server-sent events for the HTTP response: {"delta": "..."} pieces, then {"done": ...} or {"error": ...}."""
    def event(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"
    try:
        messages = build_messages(transcript, conversation, at)
        source = _openrouter(messages) if LLM_PROVIDER == "openrouter" else _anthropic(messages)
        for kind, value in source:
            if kind == "delta":
                yield event({"delta": value})
            else:
                r: llm.Result = value
                if r.text.strip():
                    log.info("chat answer: %s, %d input / %d output tokens", r.model, r.input_tokens, r.output_tokens)
                    yield event({"done": True, "model": r.model, "input_tokens": r.input_tokens,
                                 "output_tokens": r.output_tokens})
                else:
                    yield event({"error": "The model returned nothing"})
    except llm.LLMError as e:
        yield event({"error": str(e)})
    except Exception as e:  # never leave the client hanging without a final event
        log.exception("chat failed")
        yield event({"error": f"{type(e).__name__}: {e}"})
