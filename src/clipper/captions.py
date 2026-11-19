"""ASS subtitle generation: word-grouped captions, a hook line, and a source credit."""
from __future__ import annotations

from .transcribe import Word

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Cap,DejaVu Sans,{cap},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,6,2,2,60,60,{capv},1
Style: Hook,DejaVu Sans,{hook},&H0000E5FF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,7,3,8,60,60,{hookv},1
Style: Src,DejaVu Sans,{src},&H00DDDDDD,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,3,1,2,60,60,{srcv},1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""


def ts(sec: float) -> str:
    sec = max(sec, 0.0)
    cs = int(round(sec * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def group_words(words: list[Word], max_words: int = 4, max_gap: float = 0.7) -> list[tuple[float, float, str]]:
    lines, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w.start - cur[-1].end > max_gap):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    out = []
    for i, ln in enumerate(lines):
        end = ln[-1].end
        if i + 1 < len(lines):
            end = min(max(end, ln[-1].end), lines[i + 1][0].start)  # never overlap next caption
        out.append((ln[0].start, max(end, ln[0].start + 0.3), " ".join(x.text for x in ln).upper()))
    return out


def build_ass(words: list[Word], hook: str, source_credit: str, width: int, height: int,
              length: float, hook_seconds: float = 3.0) -> str:
    s = width / 1080
    head = HEADER.format(w=width, h=height, cap=int(78 * s), capv=int(height * 0.2), hook=int(74 * s),
                         hookv=int(height * 0.1), src=int(34 * s), srcv=int(height * 0.04))
    ev = []
    if hook:
        ev.append(f"Dialogue: 1,{ts(0)},{ts(hook_seconds)},Hook,,0,0,0,,{esc(hook)}")
    for a, b, t in group_words(words):
        ev.append(f"Dialogue: 0,{ts(a)},{ts(b)},Cap,,0,0,0,,{esc(t)}")
    if source_credit:
        ev.append(f"Dialogue: 0,{ts(0)},{ts(length)},Src,,0,0,0,,{esc(source_credit)}")
    return head + "\n".join(ev) + "\n"
