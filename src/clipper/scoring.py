"""Rule-based clip-potential scoring. Deterministic, explainable, no models."""
from __future__ import annotations

import re

from .config import ScoringCfg

_WORD = re.compile(r"[a-z0-9']+")


def _contains(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def score_candidate(chat_z: float, loud_z: float, messages: list[tuple[str, str]],
                    cfg: ScoringCfg, chat_thr: float, loud_thr: float,
                    degraded: bool = False) -> tuple[float, list[str]]:
    """messages: (author, text) around the event. Returns (score 0..1, human-readable reasons)."""
    reasons: list[str] = []
    texts = [t.lower() for _, t in messages]
    n = len(texts)
    authors = len({a for a, _ in messages})
    hype = sum(any(_contains(t, w) for w in cfg.hype_words) for t in texts)
    topic = sum(any(_contains(t, w) for w in cfg.topic_words) for t in texts)

    c_comp = min(max(chat_z, 0) / 8, 1.0)
    l_comp = min(max(loud_z, 0) / 8, 1.0)
    both = 1.0 if (chat_z >= chat_thr and loud_z >= loud_thr) else 0.0
    hype_c = min(hype / 5, 1.0)
    topic_c = min(topic / 3, 1.0)
    div = min(authors / max(n, 1) / 0.6, 1.0) if n else 0.0

    if degraded:
        score = 0.70 * l_comp + 0.10 * hype_c
        reasons.append("degraded: chat unavailable, audio-only")
    else:
        score = 0.28 * c_comp + 0.28 * l_comp + 0.14 * both + 0.10 * hype_c + 0.05 * topic_c + 0.15 * div
    if n >= 6 and authors / n < 0.3:
        score *= 0.6
        reasons.append("chat looks spammy (few unique authors)")
    reasons.append(f"chat z={chat_z:.1f}, audio z={loud_z:.1f}")
    if hype:
        reasons.append(f"{hype} hype messages")
    if topic:
        reasons.append(f"{topic} on-topic messages")
    return round(min(score, 1.0), 3), reasons
