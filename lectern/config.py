"""All settings come from environment variables (see README)."""
import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


DATA_DIR = Path(os.environ.get("LECTERN_DATA_DIR", "/data")).resolve()
DB_PATH = DATA_DIR / "lectern.db"
MODELS_DIR = DATA_DIR / "models"

API_KEY = os.environ.get("LECTERN_API_KEY", "")
# Only files under these folders may be read (comma-separated). Anything else is refused.
MEDIA_ROOTS = [Path(p.strip()).resolve() for p in os.environ.get("LECTERN_MEDIA_ROOTS", "/media").split(",") if p.strip()]

YOUTUBE = _bool("LECTERN_YOUTUBE", True)
YT_NAME_SEARCH = _bool("LECTERN_YT_NAME_SEARCH", True)
YT_MIN_GAP_S = _float("LECTERN_YT_MIN_GAP", 10)        # random pause between YouTube requests ...
YT_MAX_GAP_S = _float("LECTERN_YT_MAX_GAP", 30)        # ... in seconds
YT_BACKOFF_S = _float("LECTERN_YT_BACKOFF", 3600)      # first pause after YouTube pushes back; doubles, max 24 h
YT_COOKIES = os.environ.get("LECTERN_YT_COOKIES") or None  # optional cookies.txt, if YouTube insists

WHISPER = _bool("LECTERN_WHISPER", True)
WHISPER_MODEL = os.environ.get("LECTERN_WHISPER_MODEL", "small")
WHISPER_THREADS = int(_float("LECTERN_WHISPER_THREADS", 4))
WHISPER_COMPUTE = os.environ.get("LECTERN_WHISPER_COMPUTE", "int8")
WHISPER_BEAM = int(_float("LECTERN_WHISPER_BEAM", 1))  # 1 = greedy: about twice as fast on a CPU

SUBTITLE_EXTS = (".vtt", ".srt")

# Summaries, study notes and English translation. On only when an API key is set.
# Provider: OpenRouter by default (OPENROUTER_API_KEY), or Anthropic directly (ANTHROPIC_API_KEY).
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
_provider = os.environ.get("LECTERN_LLM_PROVIDER", "").strip().lower()
if _provider not in ("openrouter", "anthropic"):
    _provider = "anthropic" if ANTHROPIC_KEY and not OPENROUTER_KEY else "openrouter"
LLM_PROVIDER = _provider
LLM = bool(OPENROUTER_KEY if LLM_PROVIDER == "openrouter" else ANTHROPIC_KEY) and _bool("LECTERN_LLM", True)
LLM_MODEL = os.environ.get("LECTERN_LLM_MODEL") or (
    "anthropic/claude-opus-5.5" if LLM_PROVIDER == "openrouter" else "claude-opus-5-5")
# Chat can use its own (for example cheaper, faster) model; by default the same one.
LLM_CHAT_MODEL = os.environ.get("LECTERN_LLM_CHAT_MODEL", "").strip() or LLM_MODEL
OPENROUTER_URL = os.environ.get("LECTERN_OPENROUTER_URL", "https://openrouter.ai/api/v1").rstrip("/")
LLM_EFFORT = os.environ.get("LECTERN_LLM_EFFORT", "medium")              # summary and study notes
LLM_TRANSLATE_EFFORT = os.environ.get("LECTERN_LLM_TRANSLATE_EFFORT", "low")
LLM_CHAT_EFFORT = os.environ.get("LECTERN_LLM_CHAT_EFFORT", "low")      # chat: quick answers
