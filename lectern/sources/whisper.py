"""Speech-to-text on the CPU with faster-whisper. Used only when no subtitles exist anywhere."""
import os
import sys
import threading
from pathlib import Path

from ..captions import Segment
from ..config import MODELS_DIR, WHISPER_BEAM, WHISPER_COMPUTE, WHISPER_MODEL, WHISPER_THREADS

_model = None
_lock = threading.Lock()


def _get_model():
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type=WHISPER_COMPUTE,
                                  cpu_threads=WHISPER_THREADS, download_root=str(MODELS_DIR))
        return _model


def lower_priority() -> None:
    """Be nice to the rest of the NAS (Linux applies 'nice' per thread, and new threads inherit it)."""
    if sys.platform.startswith("linux"):
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
        except OSError:
            pass


def transcribe(path: Path, on_progress=lambda p: None) -> tuple[list[Segment], str | None]:
    model = _get_model()
    segments, info = model.transcribe(str(path), beam_size=WHISPER_BEAM, vad_filter=True,
                                      condition_on_previous_text=False)
    out: list[Segment] = []
    for s in segments:  # a generator: the work happens while we iterate
        text = s.text.strip()
        if text:
            out.append(Segment(s.start, s.end, text))
        if info.duration:
            on_progress(min(s.end / info.duration, 0.999))
    return out, info.language
