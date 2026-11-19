"""Editorial story generation: deterministic title/hook/description from chat + transcript.

No LLM. The goal is an honest, readable framing of what the audience reacted to, not clickbait
that misrepresents the footage.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .transcribe import Word

STOP = set("""a an the and or but if so to of in on at for with is are was were be been it its this that these those
i me my you your we our they them he she his her im ive u ur just like get got do does did not no yes yeah yea
what when where who why how can could would should will have has had there here then than too very really
lol lmao lmfao haha hahaha kek xd pog poggers omegalul kappa lul""".split())
BLOCK = ("fuck", "shit", "nigg", "fag", "retard", "cunt", "bitch", "kys", "nazi")
URL = re.compile(r"https?://\S+|\bwww\.\S+")
MENTION = re.compile(r"@\w+")
TOKEN = re.compile(r"[a-zA-Z0-9']+")
# Twitch emotes: CamelCase or Capitalised-with-digits words, e.g. PogChamp, Kreygasm, 4Head
EMOTE = re.compile(r"^(?:[A-Z][a-z]+[A-Z]\w*|\d[A-Za-z]+|[A-Z]{2,}[a-z]+)$")


@dataclass
class Story:
    title: str
    hook: str
    description: str
    hashtags: list[str]
    keywords: list[str] = field(default_factory=list)
    chat_quotes: list[str] = field(default_factory=list)


def clean_messages(msgs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    out = []
    for a, t in msgs:
        t = MENTION.sub("", URL.sub("", t)).strip()
        if not t or any(b in t.lower() for b in BLOCK) or len(t) > 140:
            continue
        out.append((a, t))
    return out


def _tokens(text: str) -> list[str]:
    toks = []
    for raw in text.split():
        if EMOTE.match(raw.strip(".,!?")):
            continue
        toks += [w.lower() for w in TOKEN.findall(raw)]
    return toks


def _author_sets(msgs):
    uni: dict[str, set[str]] = {}
    bi: dict[str, set[str]] = {}
    for a, t in msgs:
        w = _tokens(t)
        for i, x in enumerate(w):
            if x not in STOP and len(x) > 2:
                uni.setdefault(x, set()).add(a)
            if i + 1 < len(w) and w[i + 1] not in STOP and x not in STOP:
                bi.setdefault(f"{x} {w[i + 1]}", set()).add(a)
    return uni, bi


def top_phrases(msgs: list[tuple[str, str]], n: int = 3,
                context: list[tuple[str, str]] | None = None, context_scale: float = 1.0) -> list[str]:
    """Most-repeated unigrams/bigrams, weighted by unique authors (one spammer counts once).

    `context` = chat from before the spike; `context_scale` = spike_duration / context_duration.
    A phrase no more frequent than its background rate (x1.5) is chatter, not the reaction."""
    uni, bi = _author_sets(msgs)
    base_uni, base_bi = _author_sets(context or [])
    scored = ([(len(v) * 2, k) for k, v in bi.items() if len(v) >= 2 and len(v) > 1.5 * context_scale * len(base_bi.get(k, ()))]
              + [(len(v), k) for k, v in uni.items() if len(v) > 1.5 * context_scale * len(base_uni.get(k, ()))])
    scored.sort(key=lambda x: (-x[0], x[1]))
    out: list[str] = []
    for _, k in scored:
        if not any(k in o or o in k for o in out):
            out.append(k)
        if len(out) == n:
            break
    return out


def transcript_keywords(words: list[Word], n: int = 4) -> list[str]:
    c = Counter(w.text.lower().strip(".,!?\"'") for w in words)
    return [k for k, v in c.most_common(30) if k not in STOP and len(k) > 3][:n]


def build_story(msgs: list[tuple[str, str]], words: list[Word], channel: str = "",
                game_tag: str = "GTA6", context: list[tuple[str, str]] | None = None,
                context_scale: float = 1.0) -> Story:
    """msgs = chat during the spike; context = chat just before it (background to subtract)."""
    msgs = clean_messages(msgs)
    phrases = top_phrases(msgs, context=clean_messages(context or []), context_scale=context_scale)
    tkeys = transcript_keywords(words)
    focus = phrases[0] if phrases else (tkeys[0] if tkeys else "")
    if focus:
        title = f"Chat loses it over \"{focus}\""
    else:
        title = "The moment chat went wild"
    if len(title) > 70:
        title = title[:67].rstrip() + "..."
    hook = (f"CHAT ON \"{focus.upper()}\"" if focus and len(focus) <= 18 else "CHAT WENT WILD")
    quotes = []
    seen = set()
    for _, t in msgs:
        k = t.lower()
        if k not in seen and len(t) <= 80:
            seen.add(k)
            quotes.append(t)
        if len(quotes) == 3:
            break
    desc_lines = [title]
    if channel:
        desc_lines.append(f"Moment from {channel}'s live stream (credit and source in the metadata file).")
    if quotes:
        desc_lines.append("Chat said: " + " | ".join(quotes))
    tags = [f"#{game_tag}", "#Shorts", "#Gaming"]
    return Story(title, hook, "\n".join(desc_lines), tags, phrases + tkeys, quotes)
