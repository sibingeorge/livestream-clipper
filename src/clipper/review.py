"""Review queue: list drafts, approve / reject / needs-edit. Nothing is ever published automatically."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from .config import Config
from .db import Database

DECISIONS = {"approve": ("approved", "approved"), "reject": ("rejected", "rejected"),
             "needs_edit": ("needs_edit", "review")}


def queue(db: Database, status: str = "review"):
    return db.query("SELECT c.*, d.score, d.reasons FROM clips c LEFT JOIN candidates d ON d.id=c.candidate_id "
                    "WHERE c.status=? ORDER BY d.score DESC, c.id", (status,))


def decide(db: Database, cfg: Config, clip_id: int, decision: str, note: str = "") -> str:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {sorted(DECISIONS)}")
    row = db.one("SELECT * FROM clips WHERE id=?", (clip_id,))
    if row is None:
        raise KeyError(f"no clip {clip_id}")
    status, folder = DECISIONS[decision]
    path = Path(row["path"])
    meta = Path(row["meta_path"]) if row["meta_path"] else None
    dest_dir = cfg.clips_dir / folder
    dest_dir.mkdir(parents=True, exist_ok=True)
    new_path, new_meta = row["path"], row["meta_path"]
    if path.exists() and path.parent != dest_dir:
        new_path = str(dest_dir / path.name)
        shutil.move(str(path), new_path)
        for ext in (".ass",):
            sib = path.with_suffix(ext)
            if sib.exists():
                sib.unlink()
        if meta and meta.exists():
            new_meta = str(dest_dir / meta.name)
            shutil.move(str(meta), new_meta)
    db.execute("UPDATE clips SET status=?, path=?, meta_path=?, reviewed_at=?, review_note=? WHERE id=?",
               (status, new_path, new_meta, time.time(), note, clip_id))
    return status
