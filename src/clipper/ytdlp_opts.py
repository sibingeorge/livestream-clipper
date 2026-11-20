"""Shared yt-dlp settings (cookies for sign-in/bot checks), configured once at startup."""
from __future__ import annotations

from typing import Any

_cookies_file: str | None = None
_cookies_browser: str | None = None


def configure(cfg) -> None:
    global _cookies_file, _cookies_browser
    _cookies_file = cfg.ytdlp.cookies_file or None
    _cookies_browser = cfg.ytdlp.cookies_from_browser or None


def options(base: dict[str, Any] | None = None) -> dict[str, Any]:
    """yt-dlp Python API options, with cookies if configured."""
    o = dict(base or {})
    if _cookies_file:
        o["cookiefile"] = _cookies_file
    elif _cookies_browser:
        o["cookiesfrombrowser"] = (_cookies_browser,)
    return o


def cli_args() -> list[str]:
    """Same settings as yt-dlp command-line flags."""
    if _cookies_file:
        return ["--cookies", _cookies_file]
    if _cookies_browser:
        return ["--cookies-from-browser", _cookies_browser]
    return []
