"""Event detection: every 10 s window gets robust z-scores for chat rate and loudness;
a candidate fires only when BOTH spike. Duplicates inside the cooldown are merged."""
from __future__ import annotations

import time

import numpy as np

from .config import Config
from .db import Database
from .log import get_logger
from .scoring import score_candidate

log = get_logger("detect")
CHAT_LAG = 5.0  # wait this long after a window ends so late chat can arrive


def robust_z(history: list[float], value: float, min_scale: float) -> float:
    h = np.asarray(history, dtype=float)
    med = float(np.median(h))
    mad = float(np.median(np.abs(h - med))) * 1.4826
    scale = max(mad, 0.1 * abs(med), min_scale)
    return (value - med) / scale


def process_windows(db: Database, stream_id: int, cfg: Config, chat_available: bool = True,
                    now: float | None = None) -> list[int]:
    """Score unscored windows in order; returns ids of newly created/updated candidates."""
    d = cfg.detection
    now = now if now is not None else time.time()
    off = cfg.capture.latency_offset_seconds
    created: list[int] = []
    pending = db.query("SELECT * FROM windows WHERE stream_id=? AND chat_z IS NULL AND t_end<=? "
                       "ORDER BY t_start", (stream_id, now - CHAT_LAG - off))
    for w in pending:
        msgs = db.query("SELECT author,text FROM chat_messages WHERE stream_id=? AND ts>=? AND ts<?",
                        (stream_id, w["t_start"] + off, w["t_end"] + off))
        chat_n, authors = len(msgs), len({m["author"] for m in msgs})
        hist = db.query("SELECT chat_count,loudness_db FROM windows WHERE stream_id=? AND t_start<? "
                        "AND chat_z IS NOT NULL ORDER BY t_start DESC LIMIT ?",
                        (stream_id, w["t_start"], d.baseline_windows))
        if len(hist) < d.min_baseline_windows:
            db.execute("UPDATE windows SET chat_count=?,chat_authors=?,chat_z=0,loud_z=0 WHERE stream_id=? AND t_start=?",
                       (chat_n, authors, stream_id, w["t_start"]))
            continue
        cz = robust_z([h["chat_count"] for h in hist], chat_n, 1.0)
        lz = robust_z([h["loudness_db"] for h in hist], w["loudness_db"], 1.5)
        db.execute("UPDATE windows SET chat_count=?,chat_authors=?,chat_z=?,loud_z=? WHERE stream_id=? AND t_start=?",
                   (chat_n, authors, cz, lz, stream_id, w["t_start"]))
        loud_ok = w["loudness_db"] >= d.min_loudness_db
        if chat_available:
            hit = cz >= d.chat_z and lz >= d.loud_z and chat_n >= d.min_chat_messages and loud_ok
            degraded = False
        else:
            hit = d.audio_only_fallback and lz >= d.audio_only_loud_z and loud_ok
            degraded = True
        if not hit:
            continue
        cid = _add_candidate(db, stream_id, w["t_end"], cz, lz,
                             [(m["author"], m["text"]) for m in msgs], cfg, degraded)
        if cid:
            created.append(cid)
    return created


def _add_candidate(db, stream_id, event_ts, cz, lz, msgs, cfg, degraded) -> int | None:
    score, reasons = score_candidate(cz, lz, msgs, cfg.scoring, cfg.detection.chat_z,
                                     cfg.detection.loud_z, degraded)
    if score < cfg.scoring.min_score:
        log.info("[%s] spike at %.0f dropped: score %.2f < %.2f", stream_id, event_ts, score,
                 cfg.scoring.min_score)
        return None
    cd = cfg.detection.cooldown_seconds
    near = db.one("SELECT * FROM candidates WHERE stream_id=? AND ABS(event_ts-?)<? AND status!='superseded' "
                  "ORDER BY score DESC LIMIT 1", (stream_id, event_ts, cd))
    if near:
        # Duplicate of an existing candidate: keep the better one, only while unprocessed.
        if score > (near["score"] or 0) and near["status"] in ("pending", "ready"):
            db.execute("UPDATE candidates SET event_ts=?,chat_z=?,loud_z=?,score=?,reasons=?,status='pending' WHERE id=?",
                       (event_ts, cz, lz, score, db.dumps(reasons), near["id"]))
            return near["id"]
        return None
    cur = db.execute("INSERT INTO candidates(stream_id,event_ts,chat_z,loud_z,score,reasons,status,created_at) "
                     "VALUES (?,?,?,?,?,?,'pending',?)",
                     (stream_id, event_ts, cz, lz, score, db.dumps(reasons), time.time()))
    log.info("[%s] candidate #%s at %.0f score %.2f", stream_id, cur.lastrowid, event_ts, score)
    return cur.lastrowid
