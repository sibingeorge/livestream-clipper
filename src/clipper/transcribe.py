"""Local speech-to-text with faster-whisper (optional dependency, model chosen by hardware)."""
from __future__ import annotations

from dataclasses import dataclass

from .config import AsrCfg
from .hardware import detect_hardware
from .log import get_logger

log = get_logger("asr")
_MODEL = {}


@dataclass
class Word:
    start: float
    end: float
    text: str


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def transcribe(path: str, cfg: AsrCfg) -> list[Word]:
    """Word-level transcript; [] if disabled, not installed, or it fails (captions are then skipped)."""
    if not cfg.enabled:
        return []
    if not available():
        log.warning("faster-whisper not installed (pip install '.[asr]'); skipping captions")
        return []
    try:
        from faster_whisper import WhisperModel
        hw_model, device, compute = detect_hardware().whisper_choice()
        name = hw_model if cfg.model == "auto" else cfg.model
        key = (name, device, compute)
        if key not in _MODEL:
            log.info("loading whisper %s on %s (%s)", name, device, compute)
            _MODEL[key] = WhisperModel(name, device=device, compute_type=compute)
        segs, _ = _MODEL[key].transcribe(path, language=cfg.language or None, word_timestamps=True,
                                         vad_filter=True, beam_size=1)
        return [Word(w.start, w.end, w.word.strip()) for s in segs for w in (s.words or []) if w.word.strip()]
    except Exception as e:  # noqa: BLE001
        log.error("transcription failed: %s", e)
        return []
