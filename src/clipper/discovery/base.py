"""Stream discovery shared types."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..db import Database
from ..log import get_logger
from ..rights import check_source

log = get_logger("discovery")


@dataclass
class StreamInfo:
    platform: str
    video_id: str
    url: str
    title: str = ""
    channel_id: str | None = None
    channel_name: str = ""
    viewers: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


def discover_all(cfg, youtube=None, twitch=None) -> list[StreamInfo]:
    """Run every discoverer; one platform failing never blocks the others."""
    from . import twitch as tw, youtube as yt
    out: list[StreamInfo] = []
    for name, fn in (("youtube", youtube or yt.discover), ("twitch", twitch or tw.discover)):
        try:
            out.extend(fn(cfg))
        except Exception as e:  # noqa: BLE001 - discovery is best effort
            log.warning("%s discovery failed: %s", name, e)
    seen, uniq = set(), []
    for s in sorted(out, key=lambda s: s.viewers, reverse=True):
        key = (s.platform, s.video_id)
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    return uniq[: cfg.discovery.max_candidates]


def register_streams(db: Database, streams: list[StreamInfo]) -> list[tuple[int, bool, str]]:
    """Upsert streams and apply the rights gate. Returns (stream_id, allowed, reason)."""
    res = []
    for s in streams:
        d = check_source(db, s.platform, s.channel_id, s.meta)
        state = "discovered" if d.allowed else "blocked"
        db.execute(
            "INSERT INTO streams(platform,video_id,channel_id,channel_name,title,url,viewers,state,"
            "state_reason,discovered_at,meta) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(platform,video_id) DO UPDATE SET viewers=excluded.viewers,title=excluded.title,"
            "channel_id=excluded.channel_id,channel_name=excluded.channel_name,"
            "state=CASE WHEN streams.state IN ('recording','ended') THEN streams.state ELSE excluded.state END,"
            "state_reason=excluded.state_reason",
            (s.platform, s.video_id, s.channel_id, s.channel_name, s.title, s.url, s.viewers,
             state, d.reason, time.time(), db.dumps(s.meta)))
        row = db.one("SELECT id FROM streams WHERE platform=? AND video_id=?", (s.platform, s.video_id))
        res.append((row["id"], d.allowed, d.reason))
    return res
