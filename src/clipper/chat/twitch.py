"""Anonymous Twitch chat over IRC (justinfan nick, read-only, no OAuth)."""
from __future__ import annotations

import random
import re
import socket
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


def stream_chat(channel: str, host: str = "irc.chat.twitch.tv", port: int = 6667) -> Iterator[ChatMessage]:
    channel = channel.lower().lstrip("#")
    with socket.create_connection((host, port), timeout=30) as s:
        s.settimeout(120)
        s.sendall(b"CAP REQ :twitch.tv/tags\r\n")
        s.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\n".encode())
        s.sendall(f"JOIN #{channel}\r\n".encode())
        buf = ""
        while True:
            data = s.recv(4096)
            if not data:
                return
            buf += data.decode("utf-8", "replace")
            *lines, buf = buf.split("\r\n")
            for line in lines:
                if line.startswith("PING"):
                    s.sendall(b"PONG :tmi.twitch.tv\r\n")
                    continue
                m = parse_line(line)
                if m:
                    yield m
