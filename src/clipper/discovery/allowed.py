"""Poll the channels you have permission for and report which are live right now.

This is the fully automatic path: you list channels once (config `[[rights.allow]]`), and the
service notices when they go live. Offline channels raise in yt-dlp and are simply skipped.
"""
from __future__ import annotations

from typing import Any, Callable

from ..db import Database
from ..log import get_logger
from .base import StreamInfo
from .youtube import _extract, info_to_stream

log = get_logger("discovery.allowed")


def live_url(platform: str, channel_id: str) -> str | None:
    if platform == "youtube":
        if channel_id.startswith("@"):
            return f"https://www.youtube.com/{channel_id}/live"
        return f"https://www.youtube.com/channel/{channel_id}/live"
    if platform == "twitch":
        return f"https://www.twitch.tv/{channel_id.lower()}"
    return None


def discover_allowed(cfg, db: Database,
                     extract: Callable[[str, bool], dict[str, Any] | None] = _extract) -> list[StreamInfo]:
    out: list[StreamInfo] = []
    for row in db.query("SELECT * FROM sources WHERE status='allowed' ORDER BY platform, channel_id"):
        url = live_url(row["platform"], row["channel_id"])
        if not url:
            continue
        try:
            info = extract(url, False)
        except Exception as e:  # noqa: BLE001 - "not live" surfaces as a yt-dlp error
            log.debug("%s:%s not live (%s)", row["platform"], row["channel_id"], str(e)[:80])
            continue
        if not isinstance(info, dict) or info.get("is_live") is False:
            continue
        if row["platform"] == "youtube":
            s = info_to_stream(info)
        else:
            login = row["channel_id"].lower()
            s = StreamInfo("twitch", login, f"https://www.twitch.tv/{login}", str(info.get("title") or ""),
                           channel_name=str(info.get("uploader") or login),
                           meta={"is_live": True, "availability": "public"}) if info.get("is_live") else None
        if s is None:
            continue
        s.channel_id = row["channel_id"]  # match the allowlist key (handle vs UC id may differ)
        if not s.channel_name:
            s.channel_name = row["name"] or row["channel_id"]
        out.append(s)
    return out
