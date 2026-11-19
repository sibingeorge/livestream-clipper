"""Source eligibility and rights metadata.

Policy: DEFAULT-DENY. A stream is processed only if its channel is explicitly allow-listed
with a stated basis (e.g. "owned", "written-permission", "licensed", "creator-program").
This module records what you told it; it cannot verify legal rights for you.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .db import Database

VALID_BASES = {"owned", "written-permission", "licensed", "creator-program", "other"}


@dataclass
class Decision:
    allowed: bool
    reason: str
    basis: str | None = None


def set_source(db: Database, platform: str, channel_id: str, status: str,
               name: str = "", basis: str = "", note: str = "") -> None:
    if status not in ("allowed", "denied"):
        raise ValueError("status must be allowed|denied")
    if status == "allowed":
        if basis not in VALID_BASES:
            raise ValueError(f"allowing a source requires --basis, one of {sorted(VALID_BASES)}")
    db.execute(
        "INSERT INTO sources(platform,channel_id,name,status,basis,note,updated_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(platform,channel_id) DO UPDATE SET name=excluded.name,status=excluded.status,"
        "basis=excluded.basis,note=excluded.note,updated_at=excluded.updated_at",
        (platform.lower(), channel_id, name, status, basis or None, note, time.time()))


def list_sources(db: Database):
    return db.query("SELECT * FROM sources ORDER BY platform, channel_id")


def check_source(db: Database, platform: str, channel_id: str | None,
                 meta: dict | None = None) -> Decision:
    """Eligibility of a discovered stream. Never raises on malformed metadata."""
    meta = meta or {}
    if not channel_id:
        return Decision(False, "no channel id in metadata; cannot match allowlist")
    row = db.one("SELECT * FROM sources WHERE platform=? AND channel_id=?",
                 (platform.lower(), channel_id))
    if row is None:
        return Decision(False, "not on allowlist (default deny)")
    if row["status"] == "denied":
        return Decision(False, "explicitly denied")
    # Technical ineligibility even when allow-listed
    avail = (meta.get("availability") or "public")
    if avail not in ("public", "unlisted", None):
        return Decision(False, f"availability={avail}")
    if meta.get("age_limit") and int(meta.get("age_limit") or 0) >= 18:
        return Decision(False, "age-restricted")
    if meta.get("is_live") is False:
        return Decision(False, "not live")
    return Decision(True, "allow-listed", row["basis"])


def sync_config(db: Database, entries: list[dict]) -> int:
    """Load [[rights.allow]] entries from config. Invalid entries are skipped (logged), and a source
    you explicitly denied in the database is never silently re-allowed by the config file."""
    from .log import get_logger
    log = get_logger("rights")
    n = 0
    for e in entries or []:
        try:
            platform, cid = str(e["platform"]).lower(), str(e["channel_id"])
            if platform not in ("youtube", "twitch"):
                raise ValueError(f"unsupported platform {platform}")
            row = db.one("SELECT status FROM sources WHERE platform=? AND channel_id=?", (platform, cid))
            if row and row["status"] == "denied":
                log.warning("config allows %s:%s but it is denied in the database; keeping denied", platform, cid)
                continue
            set_source(db, platform, cid, "allowed", name=str(e.get("name", "")),
                       basis=str(e.get("basis", "")), note=str(e.get("note", "")))
            n += 1
        except (KeyError, ValueError) as ex:
            log.error("ignoring invalid [[rights.allow]] entry %r: %s", e, ex)
    return n
