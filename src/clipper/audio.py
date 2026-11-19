"""Audio analysis: per-window loudness (dBFS RMS) and a crude music-likelihood heuristic."""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

import numpy as np

from .db import Database
from .log import get_logger

log = get_logger("audio")
SR = 16000


def decode_pcm(path: str | Path, start: float = 0.0, duration: float | None = None) -> np.ndarray:
    """Decode to mono float32 @16 kHz. Returns an empty array when ffmpeg fails (never raises)."""
    cmd = ["ffmpeg", "-v", "error", "-ss", str(start)]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=300)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return np.zeros(0, dtype=np.float32)
    if r.returncode != 0 or not r.stdout:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(r.stdout, dtype=np.float32)


def rms_db(x: np.ndarray) -> float:
    if x.size == 0:
        return -90.0
    return float(20 * math.log10(max(float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))), 1e-5)))


def second_buckets(samples: np.ndarray, seg_start: float) -> list[tuple[int, float, int]]:
    """(epoch_second, sum_of_squares, n_samples) for each wall-clock second the samples touch."""
    if samples.size == 0:
        return []
    t = seg_start + np.arange(samples.size) / SR
    sec = np.floor(t).astype(np.int64)
    first = int(sec[0])
    idx = sec - first
    sumsq = np.bincount(idx, weights=np.square(samples, dtype=np.float64))
    cnt = np.bincount(idx)
    return [(first + i, float(sumsq[i]), int(cnt[i])) for i in range(len(cnt)) if cnt[i]]


def build_windows(db: Database, stream_id: int, win: int = 10, ended: bool = False) -> int:
    """Assemble wall-clock-aligned windows from per-second buckets. Tolerates a couple of missing
    seconds (dropped frames); a window is final once a later second exists (or the stream ended)."""
    row = db.one("SELECT MIN(sec) lo, MAX(sec) hi FROM audio_sec WHERE stream_id=?", (stream_id,))
    if row["lo"] is None:
        return 0
    last = db.one("SELECT MAX(t_start) m FROM windows WHERE stream_id=?", (stream_id,))["m"]
    g = int(math.ceil(row["lo"] / win) * win)
    if last is not None:
        g = max(g, int(last) + win)
    n = 0
    while (g + win <= row["hi"]) or (ended and g + win - 1 <= row["hi"]):
        secs = db.query("SELECT sumsq, n FROM audio_sec WHERE stream_id=? AND sec>=? AND sec<?",
                        (stream_id, g, g + win))
        if len(secs) >= win - 2:
            ss, cnt = sum(r["sumsq"] for r in secs), sum(r["n"] for r in secs)
            db_ = float(10 * math.log10(max(ss / max(cnt, 1), 1e-10)))
            db.execute("INSERT OR IGNORE INTO windows(stream_id,t_start,t_end,chat_count,chat_authors,loudness_db) "
                       "VALUES (?,?,?,0,0,?)", (stream_id, float(g), float(g + win), db_))
            n += 1
        g += win
    return n


def music_likelihood(samples: np.ndarray) -> float:
    """0..1. HEURISTIC: loud audio with very little level variation tends to be music/bed.
    Speech and gameplay (gunshots, engines, shouting) fluctuate more. Tune `max_music_likelihood`."""
    frame = SR // 2
    n = samples.size // frame
    if n < 6:
        return 0.0
    levels = np.array([rms_db(samples[i * frame:(i + 1) * frame]) for i in range(n)])
    if levels.mean() < -45:
        return 0.0
    steady = float(np.clip((8.0 - levels.std()) / 6.0, 0.0, 1.0))
    return round(steady, 3)


def analyze_new_segments(db: Database, stream_id: int, win: int = 10, ended: bool = False) -> int:
    """Fold newly completed segments into per-second energy, then emit finished windows.
    Undecodable segments are marked bad instead of poisoning analysis."""
    for seg in db.query("SELECT * FROM segments WHERE stream_id=? AND status='complete' ORDER BY start_ts",
                        (stream_id,)):
        samples = decode_pcm(seg["path"])
        if samples.size == 0:
            db.execute("UPDATE segments SET status='bad' WHERE id=?", (seg["id"],))
            db.log_event("warn", "audio", f"decode failed {Path(seg['path']).name}", stream_id)
            continue
        db.conn.executemany(
            "INSERT INTO audio_sec(stream_id,sec,sumsq,n) VALUES (?,?,?,?) "
            "ON CONFLICT(stream_id,sec) DO UPDATE SET sumsq=sumsq+excluded.sumsq, n=n+excluded.n",
            [(stream_id, s_, q, c) for s_, q, c in second_buckets(samples, seg["start_ts"])])
        db.conn.commit()
        db.execute("UPDATE segments SET status='analyzed' WHERE id=?", (seg["id"],))
    return build_windows(db, stream_id, win, ended)
