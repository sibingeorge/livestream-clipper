"""Email delivery of finished clips via SMTP (credentials from the environment only).

Flow: clips that passed QC and score >= email.min_score are queued in `outbox` (one row per clip,
so a clip is never mailed twice), then sent with retry/backoff. A broken mail server never
blocks recording or clipping; clips simply stay queued until delivery works.

SMTP env vars: SMTP_HOST, SMTP_PORT (587 STARTTLS or 465 SSL), SMTP_USER, SMTP_PASSWORD
(an app-specific password), MAIL_FROM (defaults to SMTP_USER), MAIL_TO.
"""
from __future__ import annotations

import json
import mimetypes
import os
import smtplib
import ssl
import threading
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Callable

from .config import Config
from .db import Database
from .log import get_logger
from .util import media_duration, run

log = get_logger("mail")
MAX_ATTEMPTS = 12


@dataclass
class SmtpSettings:
    host: str
    port: int
    user: str
    password: str
    sender: str
    to: str

    @classmethod
    def from_env(cls) -> "SmtpSettings":
        e = os.environ
        missing = [k for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_TO") if not e.get(k)]
        if missing:
            raise RuntimeError(f"email not configured; set {', '.join(missing)} in .env")
        return cls(e["SMTP_HOST"], int(e.get("SMTP_PORT", "587")), e["SMTP_USER"], e["SMTP_PASSWORD"],
                   e.get("MAIL_FROM") or e["SMTP_USER"], e["MAIL_TO"])


def connect(s: SmtpSettings) -> smtplib.SMTP:
    ctx = ssl.create_default_context()
    if s.port == 465:
        c: smtplib.SMTP = smtplib.SMTP_SSL(s.host, s.port, context=ctx, timeout=60)
    else:
        c = smtplib.SMTP(s.host, s.port, timeout=60)
        c.starttls(context=ctx)
    c.login(s.user, s.password)
    return c


def fit_attachment(path: Path, max_mb: float, work: Path) -> Path:
    """Return `path` if small enough, else a re-encoded copy under the limit."""
    limit = int(max_mb * 1024 * 1024)
    if path.stat().st_size <= limit:
        return path
    dur = media_duration(str(path)) or 30.0
    kbps = max(int((limit * 0.85 * 8) / dur / 1000) - 96, 300)
    work.mkdir(parents=True, exist_ok=True)
    out = work / f"{path.stem}_mail.mp4"
    r = run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-c:v", "libx264", "-preset", "veryfast",
             "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.3)}k", "-bufsize", f"{kbps * 2}k",
             "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)], timeout=900)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"could not shrink clip for email: {r.stderr[-200:]}")
    if out.stat().st_size > limit:
        raise RuntimeError("clip still exceeds the email size limit after re-encode")
    return out


def build_message(row, meta: dict, s: SmtpSettings, video: Path, meta_path: Path | None) -> EmailMessage:
    src = meta.get("source", {})
    det = meta.get("detection", {})
    q = meta.get("quality", {})
    title = meta.get("title") or row["title"] or f"Clip {row['id']}"
    msg = EmailMessage()
    msg["From"], msg["To"] = s.sender, s.to
    msg["Subject"] = f"[Clip #{row['id']} · score {det.get('score', 0):.2f}] {title}"[:200]
    lines = [
        f"TITLE: {title}", "", "DESCRIPTION:", meta.get("description", ""), "",
        "HASHTAGS: " + " ".join(meta.get("hashtags", [])), "",
        f"Source: {src.get('channel')} ({src.get('platform')}) - {src.get('stream_url')}",
        f"Rights basis on file: {src.get('rights_basis')}",
        f"Window (UTC epoch): {src.get('window_utc')}", "",
        f"Why it was picked (score {det.get('score')}): " + "; ".join(det.get("reasons", [])),
        f"Quality checks: {'passed' if q.get('ok') else 'FAILED'} {q.get('issues') or ''}",
        f"Captions: {'yes' if meta.get('captions') else 'no (transcription unavailable)'}", "",
        "Before posting: confirm you still have permission from the channel/publisher, give credit as agreed, "
        "and watch the clip once. Nothing has been published automatically.",
    ]
    msg.set_content("\n".join(lines))
    ctype, _ = mimetypes.guess_type(video.name)
    maintype, subtype = (ctype or "video/mp4").split("/")
    msg.add_attachment(video.read_bytes(), maintype=maintype, subtype=subtype, filename=f"{row['id']:05d}.mp4")
    if meta_path and meta_path.exists():
        msg.add_attachment(meta_path.read_bytes(), maintype="application", subtype="json",
                           filename=f"{row['id']:05d}.json")
    return msg


# ----------------------------------------------------------------------------------- queueing

def enqueue_new(db: Database, cfg: Config, now: float | None = None) -> int:
    """Queue eligible clips once. Honors min_score and the per-day cap (older clips win ties)."""
    now = now or time.time()
    day_start = now - (now % 86400)
    used = db.one("SELECT COUNT(*) n FROM outbox WHERE status IN ('queued','sent') AND queued_at>=?",
                  (day_start,))["n"]
    n = 0
    rows = db.query("SELECT c.id, d.score FROM clips c LEFT JOIN candidates d ON d.id=c.candidate_id "
                    "WHERE c.status='review' AND c.id NOT IN (SELECT clip_id FROM outbox) ORDER BY c.id")
    for r in rows:
        if (r["score"] or 0) < cfg.email.min_score:
            db.execute("INSERT INTO outbox(clip_id,status,queued_at,last_error) VALUES (?, 'skipped', ?, ?)",
                       (r["id"], now, f"score {r['score']} < min_score {cfg.email.min_score}"))
        elif used >= cfg.email.max_per_day:
            db.execute("INSERT INTO outbox(clip_id,status,queued_at,last_error) VALUES (?, 'skipped', ?, ?)",
                       (r["id"], now, "daily email cap reached"))
        else:
            db.execute("INSERT INTO outbox(clip_id,status,queued_at) VALUES (?, 'queued', ?)", (r["id"], now))
            used += 1
            n += 1
    return n


def send_pending(db: Database, cfg: Config, settings: SmtpSettings | None = None,
                 connector: Callable[[SmtpSettings], smtplib.SMTP] = connect,
                 now: float | None = None) -> int:
    """Send queued clips that are due. Returns number sent. Failures back off exponentially."""
    now = now or time.time()
    due = db.query("SELECT * FROM outbox WHERE status='queued' AND next_try<=? ORDER BY clip_id", (now,))
    if not due:
        return 0
    settings = settings or SmtpSettings.from_env()
    sent = 0
    conn = None
    try:
        for ob in due:
            clip = db.one("SELECT * FROM clips WHERE id=?", (ob["clip_id"],))
            try:
                video = Path(clip["path"])
                if not video.exists():
                    raise FileNotFoundError(f"clip file missing: {video}")
                meta_path = Path(clip["meta_path"]) if clip["meta_path"] else None
                meta = {}
                if meta_path and meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text())
                    except ValueError:
                        meta = {}
                meta.setdefault("detection", {}).setdefault(
                    "score", (db.one("SELECT score FROM candidates WHERE id=?", (clip["candidate_id"],)) or {"score": 0})["score"])
                attach = fit_attachment(video, cfg.email.max_attachment_mb, cfg.work_dir / "mail")
                msg = build_message(clip, meta, settings, attach, meta_path)
                if conn is None:
                    conn = connector(settings)
                conn.send_message(msg)
                db.execute("UPDATE outbox SET status='sent', sent_at=?, last_error=NULL WHERE clip_id=?",
                           (time.time(), ob["clip_id"]))
                log.info("emailed clip #%s to %s", ob["clip_id"], settings.to)
                sent += 1
            except Exception as e:  # noqa: BLE001 - never let delivery break the service
                attempts = ob["attempts"] + 1
                give_up = attempts >= MAX_ATTEMPTS or isinstance(e, FileNotFoundError)
                delay = min(6 * 3600, 60 * 2 ** attempts)
                db.execute("UPDATE outbox SET attempts=?, last_error=?, next_try=?, status=? WHERE clip_id=?",
                           (attempts, str(e)[:300], now + delay, "failed" if give_up else "queued", ob["clip_id"]))
                db.log_event("error", "mail", f"clip {ob['clip_id']} attempt {attempts}: {e}")
                log.warning("email for clip #%s failed (attempt %s): %s", ob["clip_id"], attempts, e)
                if conn is not None and isinstance(e, (smtplib.SMTPException, OSError)):
                    try:
                        conn.quit()
                    except Exception:  # noqa: BLE001
                        pass
                    conn = None
    finally:
        if conn is not None:
            try:
                conn.quit()
            except Exception:  # noqa: BLE001
                pass
    return sent


def send_test(settings: SmtpSettings | None = None) -> None:
    s = settings or SmtpSettings.from_env()
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.sender, s.to, "clipper: test email"
    msg.set_content("SMTP is configured correctly. Clips will arrive in this mailbox.")
    c = connect(s)
    c.send_message(msg)
    c.quit()


class Mailer(threading.Thread):
    def __init__(self, db: Database, cfg: Config, stop: threading.Event):
        super().__init__(daemon=True, name="mailer")
        self.db, self.cfg, self.stop_evt = db, cfg, stop

    def run(self) -> None:
        while not self.stop_evt.is_set():
            try:
                enqueue_new(self.db, self.cfg)
                send_pending(self.db, self.cfg)
            except RuntimeError as e:  # not configured: say so loudly but keep clipping
                log.error("%s", e)
            except Exception as e:  # noqa: BLE001
                log.exception("mailer loop error: %s", e)
            self.stop_evt.wait(self.cfg.email.send_interval_seconds)
