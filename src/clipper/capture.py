"""Capture manager: yt-dlp -> ffmpeg wall-clock-aligned 30 s segments.

yt-dlp resolves/downloads the live stream to stdout, ffmpeg stream-copies it into
`seg_<epoch>.ts` files. The epoch in the filename is the wall-clock start, which gives
timestamp tracking that lines up with chat timestamps. The manager restarts the pipeline
with backoff when it dies, and stops when yt-dlp reports the stream is over.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from .log import get_logger

log = get_logger("capture")


def segment_cmd(out_dir: Path, seconds: int) -> list[str]:
    return ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-i", "pipe:0", "-c", "copy",
            "-f", "segment", "-segment_time", str(seconds), "-segment_atclocktime", "1",
            "-reset_timestamps", "1", "-strftime", "1", "-segment_format", "mpegts",
            str(out_dir / "seg_%s.ts")]


def ytdlp_cmd(url: str, max_height: int) -> list[str]:
    fmt = f"best[height<={max_height}]/bestvideo[height<={max_height}]+bestaudio/best"
    return ["yt-dlp", "-q", "--no-warnings", "-f", fmt, "--no-part", "-o", "-", url]


class Capture:
    """Runs one stream's recording pipeline in a supervisor thread."""

    MAX_FAILS = 6

    def __init__(self, stream_id: int, url: str, out_dir: Path, cfg, db):
        self.stream_id, self.url, self.out_dir, self.cfg, self.db = stream_id, url, Path(out_dir), cfg, db
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._procs: list[subprocess.Popen] = []
        self.thread = threading.Thread(target=self._supervise, daemon=True, name=f"capture-{stream_id}")
        self.finished = threading.Event()
        self.reason = ""

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill()

    def _kill(self) -> None:
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
        self._procs = []

    def _run_once(self) -> tuple[int, float]:
        env = dict(os.environ, TZ="UTC")
        t0 = time.time()
        y = subprocess.Popen(ytdlp_cmd(self.url, self.cfg.capture.max_height),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        f = subprocess.Popen(segment_cmd(self.out_dir, self.cfg.capture.segment_seconds),
                             stdin=y.stdout, stderr=subprocess.PIPE, env=env)
        y.stdout.close()
        self._procs = [y, f]
        yerr = y.stderr.read().decode(errors="replace") if y.stderr else ""
        y.wait()
        f.wait()
        if yerr.strip():
            log.warning("[%s] yt-dlp: %s", self.stream_id, yerr.strip()[-300:])
        return y.returncode, time.time() - t0

    def _supervise(self) -> None:
        fails = 0
        while not self._stop.is_set():
            try:
                rc, ran = self._run_once()
            except Exception as e:  # noqa: BLE001
                log.error("[%s] capture crashed: %s", self.stream_id, e)
                rc, ran = 1, 0.0
            if self._stop.is_set():
                break
            if rc == 0 and ran > 5:
                self.reason = "stream ended"
                break
            # Short-lived or failing run: back off; give up when repeated (stream likely gone)
            fails = 0 if ran > 60 else fails + 1
            self.db.log_event("warn", "capture", f"pipeline exited rc={rc} after {ran:.0f}s (fail {fails})",
                              self.stream_id)
            if fails >= self.MAX_FAILS:
                self.reason = "too many capture failures"
                break
            self._stop.wait(min(60, 2 ** fails))
        self._kill()
        self.finished.set()
