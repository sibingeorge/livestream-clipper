"""YouTube live discovery through yt-dlp (no API key, no quota).

Uses YouTube's search results filtered to "Live" and ranks by concurrent viewers
from per-video metadata. YouTube's trending feed needs login, so this is the closest
free public signal; ranking is done here, not by YouTube.
"""
from __future__ import annotations

from typing import Any, Callable
from urllib.parse import quote_plus

from ..log import get_logger
from ..util import safe_int
from .base import StreamInfo

log = get_logger("discovery.youtube")
LIVE_FILTER = "EgJAAQ%3D%3D"  # sp= parameter: Type=Live


def _extract(url: str, flat: bool) -> dict[str, Any] | None:
    import yt_dlp

    from ..ytdlp_opts import options
    with yt_dlp.YoutubeDL(options({"quiet": True, "no_warnings": True, "skip_download": True,
                                   "extract_flat": "in_playlist" if flat else False, "socket_timeout": 20})) as ydl:
        return ydl.extract_info(url, download=False)


def info_to_stream(info: dict[str, Any]) -> StreamInfo | None:
    """Tolerant mapping of yt-dlp metadata; returns None when unusable."""
    if not isinstance(info, dict):
        return None
    vid = info.get("id")
    if not vid:
        return None
    if info.get("is_live") is False or info.get("live_status") in ("is_upcoming", "was_live", "not_live", "post_live"):
        return None
    return StreamInfo(
        platform="youtube", video_id=str(vid),
        url=info.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
        title=str(info.get("title") or ""), channel_id=info.get("channel_id"),
        channel_name=str(info.get("channel") or info.get("uploader") or ""),
        viewers=safe_int(info.get("concurrent_view_count") or info.get("view_count")),
        meta={"availability": info.get("availability"), "age_limit": info.get("age_limit"),
              "is_live": info.get("is_live", info.get("live_status") == "is_live"),
              "live_status": info.get("live_status")})


def discover(cfg, extract: Callable[[str, bool], dict | None] = _extract) -> list[StreamInfo]:
    """Search results already carry channel id, concurrent viewers and live status, so no per-video
    lookups are needed (those trigger YouTube's bot check from datacenter IPs and are slow)."""
    found: dict[str, StreamInfo] = {}
    for q in cfg.discovery.queries:
        url = f"https://www.youtube.com/results?search_query={quote_plus(q)}&sp={LIVE_FILTER}"
        try:
            res = extract(url, True) or {}
        except Exception as e:  # noqa: BLE001
            log.warning("search %r failed: %s", q, e)
            continue
        for entry in res.get("entries") or []:
            s = info_to_stream(entry) if isinstance(entry, dict) else None
            if s and s.viewers >= cfg.discovery.min_viewers and s.video_id not in found:
                found[s.video_id] = s
    return sorted(found.values(), key=lambda s: s.viewers, reverse=True)
