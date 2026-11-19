"""Rolling buffer / timestamp index for recorded segments."""
from __future__ import annotations

import re
import time
from pathlib import Path

from .db import Database
from .log import get_logger
from .util import media_duration

log = get_logger("buffer")
SEG_RE = re.compile(r"seg_(\d+)\.ts$")


class Buffer:
    def __init__(self, db: Database, stream_id: int, directory: Path, seg_seconds: int = 30):
        self.db, self.stream_id, self.dir, self.seg_seconds = db, stream_id, Path(directory), seg_seconds

    def scan(self, ended: bool = False) -> int:
        """Index new files. A segment is 'complete' once a newer one exists (or the stream ended).
        Zero-byte/undecodable segments are marked 'bad' instead of poisoning analysis."""
        files = sorted(p for p in self.dir.glob("seg_*.ts") if SEG_RE.search(p.name))
        for p in files:
            m = SEG_RE.search(p.name)
            self.db.execute("INSERT OR IGNORE INTO segments(stream_id,path,start_ts,status) VALUES (?,?,?, 'open')",
                            (self.stream_id, str(p), float(m.group(1))))
        starts = sorted(float(SEG_RE.search(p.name).group(1)) for p in files)
        newest = starts[-1] if starts else None
        n = 0
        for row in self.db.query("SELECT * FROM segments WHERE stream_id=? AND status='open'", (self.stream_id,)):
            if row["start_ts"] == newest and not ended:
                continue
            p = Path(row["path"])
            dur = media_duration(str(p)) if p.exists() and p.stat().st_size > 0 else None
            if not dur or dur <= 0:
                self.db.execute("UPDATE segments SET status='bad' WHERE id=?", (row["id"],))
                self.db.log_event("warn", "buffer", f"bad segment {p.name}", self.stream_id)
            else:
                self.db.execute("UPDATE segments SET status='complete', duration=? WHERE id=?", (dur, row["id"]))
                n += 1
        return n

    def covering(self, t0: float, t1: float):
        """Usable segments overlapping [t0, t1], oldest first."""
        return self.db.query(
            "SELECT * FROM segments WHERE stream_id=? AND status IN ('complete','analyzed') "
            "AND start_ts < ? AND start_ts + duration > ? ORDER BY start_ts", (self.stream_id, t1, t0))

    def coverage_end(self) -> float:
        row = self.db.one("SELECT MAX(start_ts+duration) AS e FROM segments WHERE stream_id=? "
                          "AND status IN ('complete','analyzed')", (self.stream_id,))
        return row["e"] or 0.0

    def protect(self, t0: float, t1: float) -> None:
        for r in self.covering(t0, t1):
            self.db.execute("UPDATE segments SET protected=1 WHERE id=?", (r["id"],))

    def prune(self, keep_seconds: float, now: float | None = None) -> int:
        cutoff = (now or time.time()) - keep_seconds
        n = 0
        for r in self.db.query("SELECT * FROM segments WHERE stream_id=? AND protected=0 AND start_ts<? "
                               "AND status!='deleted'", (self.stream_id, cutoff)):
            try:
                Path(r["path"]).unlink(missing_ok=True)
            except OSError as e:
                log.warning("cannot delete %s: %s", r["path"], e)
                continue
            self.db.execute("UPDATE segments SET status='deleted' WHERE id=?", (r["id"],))
            n += 1
        return n
