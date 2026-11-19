"""Twitch discovery via Helix (free developer app). Skipped silently without credentials."""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from ..log import get_logger
from ..util import safe_int
from .base import StreamInfo

log = get_logger("discovery.twitch")


def _http(url: str, headers: dict | None = None, data: bytes | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - fixed https hosts
        return json.loads(r.read().decode())


def parse_streams(payload: dict) -> list[StreamInfo]:
    out = []
    for d in (payload or {}).get("data") or []:
        login = d.get("user_login")
        if not login or d.get("type") not in (None, "live"):
            continue
        out.append(StreamInfo(
            platform="twitch", video_id=login, url=f"https://www.twitch.tv/{login}",
            title=str(d.get("title") or ""), channel_id=login, channel_name=str(d.get("user_name") or login),
            viewers=safe_int(d.get("viewer_count")),
            meta={"is_live": True, "game": d.get("game_name"), "availability": "public"}))
    return out


def discover(cfg) -> list[StreamInfo]:
    cid, sec = os.environ.get("TWITCH_CLIENT_ID"), os.environ.get("TWITCH_CLIENT_SECRET")
    if not (cid and sec):
        log.info("Twitch discovery skipped: TWITCH_CLIENT_ID/SECRET not set")
        return []
    tok = _http("https://id.twitch.tv/oauth2/token", data=urllib.parse.urlencode(
        {"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"}).encode())
    hdr = {"Client-Id": cid, "Authorization": f"Bearer {tok['access_token']}"}
    out: list[StreamInfo] = []
    for q in cfg.discovery.queries:
        games = _http("https://api.twitch.tv/helix/search/categories?" +
                      urllib.parse.urlencode({"query": q, "first": 3}), hdr).get("data") or []
        for g in games[:1]:
            out += parse_streams(_http("https://api.twitch.tv/helix/streams?" + urllib.parse.urlencode(
                {"game_id": g["id"], "first": 20}), hdr))
    return [s for s in out if s.viewers >= cfg.discovery.min_viewers]
