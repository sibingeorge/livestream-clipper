"""Quality validation gate. Every draft is checked before it reaches the review queue."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .audio import decode_pcm, music_likelihood
from .config import Config
from .util import ffprobe_json, run


@dataclass
class QualityReport:
    ok: bool
    issues: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def validate_clip(path: Path, cfg: Config, expect_captions: bool = False, has_captions: bool = False) -> QualityReport:
    issues: list[str] = []
    m: dict = {}
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return QualityReport(False, ["file missing or empty"])
    info = ffprobe_json(str(p))
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v:
        return QualityReport(False, ["no video stream"])
    try:
        dur = float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        dur = 0.0
    m.update(duration=dur, width=v.get("width"), height=v.get("height"))
    if abs(dur - cfg.clip.length) > 1.5:
        issues.append(f"duration {dur:.1f}s != {cfg.clip.length}s")
    if (v.get("width"), v.get("height")) != (cfg.clip.width, cfg.clip.height):
        issues.append(f"resolution {v.get('width')}x{v.get('height')} is not {cfg.clip.width}x{cfg.clip.height}")
    if not a:
        issues.append("no audio stream")
    else:
        r = run(["ffmpeg", "-v", "info", "-i", str(p), "-vn", "-af", "volumedetect", "-f", "null", "-"], 120)
        mv = re.search(r"mean_volume:\s*(-?[\d.]+|-inf)", r.stderr)
        mean = float(mv.group(1)) if mv and mv.group(1) != "-inf" else -91.0
        m["mean_volume_db"] = mean
        if mean < cfg.quality.min_mean_volume_db:
            issues.append(f"audio too quiet/silent ({mean:.0f} dB)")
        ml = music_likelihood(decode_pcm(p))
        m["music_likelihood"] = ml
        if ml > cfg.quality.max_music_likelihood:
            issues.append(f"music-heavy audio (likelihood {ml:.2f}); copyright/quality risk")
    r = run(["ffmpeg", "-v", "info", "-i", str(p), "-an", "-vf", "blackdetect=d=0.3:pix_th=0.12",
             "-f", "null", "-"], 180)
    black = sum(float(x) for x in re.findall(r"black_duration:([\d.]+)", r.stderr))
    ratio = black / dur if dur else 1.0
    m["black_ratio"] = round(ratio, 3)
    if ratio > cfg.quality.max_black_ratio:
        issues.append(f"mostly black video ({ratio:.0%})")
    if expect_captions and not has_captions:
        issues.append("captions expected but transcript empty")
    return QualityReport(not issues, issues, m)
