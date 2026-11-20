"""Anonymous Twitch chat over IRC (justinfan nick, read-only, no OAuth)."""
from __future__ import annotations

import random
import re
import time
from typing import Iterator

from .base import ChatMessage

PRIVMSG = re.compile(r"^(?:@(?P<tags>\S+) )?:(?P<nick>[^!]+)![^ ]+ PRIVMSG #\S+ :(?P<text>.*)$")


def parse_line(line: str, now: float | None = None) -> ChatMessage | None:
    m = PRIVMSG.match(line.rstrip("\r\n"))
    if not m:
        return None
    ts = now if now is not None else time.time()
    tags = dict(t.split("=", 1) for t in (m.group("tags") or "").split(";") if "=" in t)
    if tags.get("tmi-sent-ts", "").isdigit():
        ts = int(tags["tmi-sent-ts"]) / 1000
    return ChatMessage(ts, tags.get("display-name") or m.group("nick"), m.group("text"))


WS_URL = "wss://irc-ws.chat.twitch.tv:443"


def _proxy_kwargs() -> dict:
    """Honour HTTPS_PROXY (needed behind corporate/sandbox proxies; harmless otherwise)."""
    import os
    from urllib.parse import urlparse
    px = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not px:
        return {}
    u = urlparse(px)
    kw = {"http_proxy_host": u.hostname, "http_proxy_port": u.port or 8080, "proxy_type": "http"}
    if u.username:
        kw["http_proxy_auth"] = (u.username, u.password or "")
    return kw


def stream_chat(channel: str, url: str = WS_URL) -> Iterator[ChatMessage]:
    """Anonymous read-only chat over WebSocket (port 443: works where raw IRC port 6667 is blocked)."""
    import websocket
    channel = channel.lower().lstrip("#")
    ws = websocket.create_connection(url, timeout=120, **_proxy_kwargs())
    try:
        ws.send("CAP REQ :twitch.tv/tags")
        ws.send("PASS SCHMOOPIIE")  # conventional placeholder for anonymous (justinfan) logins
        ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
        ws.send(f"JOIN #{channel}")
        while True:
            frame = ws.recv()
            if not frame:
                return
            for line in frame.split("\r\n"):
                if line.startswith("PING"):
                    ws.send("PONG :tmi.twitch.tv")
                    continue
                m = parse_line(line)
                if m:
                    yield m
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
