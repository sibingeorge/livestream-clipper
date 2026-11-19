"""Chat ingestion: any source yielding ChatMessage -> SQLite, with reconnect + availability state."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator

from ..db import Database
from ..log import get_logger

log = get_logger("chat")


@dataclass
class ChatMessage:
    ts: float
    author: str
    text: str


class ChatIngestor:
    """Runs `source_factory()` (a generator of ChatMessage) in a thread.

    If the source raises or ends while the stream is live, it reconnects with backoff.
    After MAX_FAILS consecutive immediate failures chat is flagged unavailable and
    the pipeline degrades (audio-only fallback if enabled, otherwise no triggers).
    """

    MAX_FAILS = 5

    def __init__(self, db: Database, stream_id: int, source_factory: Callable[[], Iterator[ChatMessage]]):
        self.db, self.stream_id, self.factory = db, stream_id, source_factory
        self.available = True
        self.count = 0
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"chat-{stream_id}")

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        fails = 0
        while not self._stop.is_set():
            started, got = time.time(), 0
            try:
                batch: list[tuple] = []
                for m in self.factory():
                    if self._stop.is_set():
                        break
                    if not m.text.strip():
                        continue
                    batch.append((self.stream_id, m.ts, m.author[:64], m.text[:500]))
                    got += 1
                    if len(batch) >= 50:
                        self._flush(batch)
                self._flush(batch)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] chat source error: %s", self.stream_id, e)
            self.count += got
            if self._stop.is_set():
                break
            fails = 0 if (got or time.time() - started > 60) else fails + 1
            if fails >= self.MAX_FAILS:
                self.available = False
                self.db.log_event("warn", "chat", "chat unavailable; degraded mode", self.stream_id)
                break
            self._stop.wait(min(60, 2 ** fails))

    def _flush(self, batch: list[tuple]) -> None:
        self.db.insert_many("INSERT INTO chat_messages(stream_id,ts,author,text) VALUES (?,?,?,?)", batch)
        batch.clear()


def make_source(platform: str, url: str, video_id: str, work_dir) -> Callable[[], Iterator[ChatMessage]]:
    from . import twitch, youtube
    if platform == "youtube":
        return lambda: youtube.stream_chat(url, work_dir)
    if platform == "twitch":
        return lambda: twitch.stream_chat(video_id)
    raise ValueError(f"no chat source for platform {platform}")
