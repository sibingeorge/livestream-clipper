"""Clip extraction from the rolling buffer: cut [event-pre_roll, +length] across segments."""
from __future__ import annotations

from pathlib import Path

from .buffer import Buffer
from .config import Config
from .util import run


class ExtractError(Exception):
    pass


def clip_window(event_ts: float, cfg: Config) -> tuple[float, float]:
    t0 = event_ts - cfg.clip.pre_roll
    return t0, t0 + cfg.clip.length


def extract_clip(buf: Buffer, event_ts: float, cfg: Config, out: Path) -> Path:
    t0, t1 = clip_window(event_ts, cfg)
    segs = buf.covering(t0, t1)
    if not segs:
        raise ExtractError("no footage covers the clip window (already pruned or never recorded)")
    gaps = [(a["start_ts"] + a["duration"], b["start_ts"]) for a, b in zip(segs, segs[1:])
            if b["start_ts"] - (a["start_ts"] + a["duration"]) > 2.0]
    if gaps:
        raise ExtractError(f"dropped footage inside clip window: gaps at {gaps}")
    if segs[0]["start_ts"] > t0 + 1.5 or segs[-1]["start_ts"] + segs[-1]["duration"] < t1 - 1.5:
        raise ExtractError("clip window only partially covered by recording")
    out.parent.mkdir(parents=True, exist_ok=True)
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{Path(s['path']).resolve()}'\n" for s in segs))
    r = run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-ss", f"{max(0.0, t0 - segs[0]['start_ts']):.3f}", "-t", str(cfg.clip.length),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "160k", "-ar", "44100", str(out)], timeout=600)
    lst.unlink(missing_ok=True)
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise ExtractError(f"ffmpeg cut failed: {r.stderr[-300:]}")
    return out
