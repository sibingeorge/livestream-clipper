"""YouTube live chat through yt-dlp's `live_chat` pseudo-subtitle.

yt-dlp writes one JSON object per line (`replayChatItemAction`) to
`<name>.live_chat.json[.part]` while the stream is live. We tail that file.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Iterator

from ..log import get_logger
from .base import ChatMessage

log = get_logger("chat.youtube")


def parse_line(line: str) -> ChatMessage | None:
    """Parse one yt-dlp live_chat JSON line; None for non-text / malformed lines."""
    try:
        obj = json.loads(line)
        action = obj["replayChatItemAction"]["actions"][0]["addChatItemAction"]["item"]
        r = action["liveChatTextMessageRenderer"]
        text = "".join(run.get("text") or run.get("emoji", {}).get("shortcuts", [""])[0]
                       for run in r["message"]["runs"])
        ts = int(r["timestampUsec"]) / 1_000_000
        return ChatMessage(ts, r.get("authorName", {}).get("simpleText", "?"), text)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None


def stream_chat(url: str, work_dir, poll: float = 0.5) -> Iterator[ChatMessage]:
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    for old in work.glob("chat.live_chat.json*"):
        old.unlink(missing_ok=True)
    proc = subprocess.Popen(
        ["yt-dlp", "-q", "--no-warnings", "--skip-download", "--write-subs", "--sub-langs", "live_chat",
         "-o", str(work / "chat"), url], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    pos, path, idle = 0, None, 0.0
    try:
        while True:
            if path is None or not path.exists():
                cands = sorted(work.glob("chat.live_chat.json*"))
                path = cands[0] if cands else None
            if path and path.exists():
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    while True:
                        line = fh.readline()
                        if not line.endswith("\n"):
                            break
                        pos = fh.tell()
                        m = parse_line(line)
                        if m:
                            idle = 0.0
                            yield m
            if proc.poll() is not None and (path is None or not path.exists() or idle > 5):
                err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                if path is None:
                    raise RuntimeError(f"yt-dlp produced no live chat: {err.strip()[-200:]}")
                return
            time.sleep(poll)
            idle += poll
    finally:
        if proc.poll() is None:
            proc.terminate()
