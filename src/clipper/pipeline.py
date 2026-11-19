"""Orchestration: candidate -> clip draft, the live runner, and offline recording mode."""
from __future__ import annotations

import json
import shutil
import signal
import threading
import time
from pathlib import Path

from . import audio, detection
from .buffer import Buffer
from .captions import build_ass
from .chat import ChatIngestor
from .chat.base import make_source
from .compose import ComposeError, compose
from .config import Config
from .db import Database
from .discovery import discover_all, register_streams
from .extract import ExtractError, clip_window, extract_clip
from .log import get_logger
from .metadata import write_metadata
from .story import build_story
from .transcribe import transcribe
from .util import ffprobe_json, run, slug
from .validate import validate_clip
from .capture import Capture

log = get_logger("pipeline")
MAX_ATTEMPTS = 3


def promote_ready(db: Database, buf: Buffer, cfg: Config, ended: bool) -> int:
    """pending -> ready once the whole clip window (incl. post-roll) has been recorded."""
    n = 0
    for c in db.query("SELECT * FROM candidates WHERE stream_id=? AND status='pending'", (buf.stream_id,)):
        t0, t1 = clip_window(c["event_ts"], cfg)
        if buf.coverage_end() >= t1 - 0.5 or ended:
            buf.protect(t0 - 5, t1 + 5)  # keep footage out of rolling-buffer pruning
            db.execute("UPDATE candidates SET status='ready' WHERE id=?", (c["id"],))
            n += 1
    return n


def process_candidate(db: Database, cfg: Config, cand_id: int, buf: Buffer) -> int | None:
    """Build one review draft. Failures are recorded on the candidate and retried up to MAX_ATTEMPTS."""
    cand = db.one("SELECT * FROM candidates WHERE id=?", (cand_id,))
    stream = db.one("SELECT * FROM streams WHERE id=?", (cand["stream_id"],))
    db.execute("UPDATE candidates SET status='processing', attempts=attempts+1 WHERE id=?", (cand_id,))
    work = cfg.work_dir / f"cand_{cand_id}"
    try:
        t0, t1 = clip_window(cand["event_ts"], cfg)
        raw = extract_clip(buf, cand["event_ts"], cfg, work / "raw.mp4")
        words = transcribe(str(raw), cfg.asr)
        off = cfg.capture.latency_offset_seconds
        spike_from = cand["event_ts"] - cfg.detection.window_seconds - 2  # the spike window (+ small lead)

        def chat_between(a: float, b: float) -> list[tuple[str, str]]:
            return [(r["author"], r["text"]) for r in db.query(
                "SELECT author,text FROM chat_messages WHERE stream_id=? AND ts>=? AND ts<? ORDER BY ts",
                (cand["stream_id"], a + off, b + off))]

        story = build_story(chat_between(spike_from, cand["event_ts"] + 5), words, stream["channel_name"] or "",
                            context=chat_between(t0, spike_from),
                            context_scale=(cand["event_ts"] + 5 - spike_from) / max(spike_from - t0, 1.0))
        credit = f"Source: {stream['channel_name']} ({stream['platform']})" if stream["channel_name"] else ""
        ass = build_ass(words, story.hook, credit, cfg.clip.width, cfg.clip.height, cfg.clip.length)
        out_dir = cfg.clips_dir / "review"
        out = compose(raw, ass, out_dir / f"{cand_id:05d}_{slug(story.title)}.mp4", cfg)
        report = validate_clip(out, cfg, expect_captions=cfg.asr.enabled, has_captions=bool(words))
        # Missing captions (ASR not installed) is a warning, not a hard failure.
        hard = [i for i in report.issues if not i.startswith("captions expected")]
        report.ok = not hard
        src = db.one("SELECT basis FROM sources WHERE platform=? AND channel_id=?",
                     (stream["platform"], stream["channel_id"]))
        meta_path = write_metadata(out.with_suffix(".json"), story=story, stream=stream, candidate=cand,
                                   quality=report, clip_window=[t0, t1],
                                   rights_basis=src["basis"] if src else None, captions=bool(words))
        status = "review" if report.ok else "rejected_qc"
        cur = db.execute("INSERT INTO clips(candidate_id,stream_id,path,title,status,quality,meta_path,created_at) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (cand_id, cand["stream_id"], str(out), story.title, status,
                          db.dumps({"issues": report.issues, **report.metrics}), str(meta_path), time.time()))
        db.execute("UPDATE candidates SET status='done', error=NULL WHERE id=?", (cand_id,))
        log.info("clip #%s '%s' -> %s", cur.lastrowid, story.title, status)
        return cur.lastrowid
    except (ExtractError, ComposeError, OSError) as e:
        attempts = db.one("SELECT attempts FROM candidates WHERE id=?", (cand_id,))["attempts"]
        status = "failed" if attempts >= MAX_ATTEMPTS or isinstance(e, ExtractError) else "ready"
        db.execute("UPDATE candidates SET status=?, error=? WHERE id=?", (status, str(e)[:500], cand_id))
        db.log_event("error", "pipeline", f"candidate {cand_id}: {e}", cand["stream_id"])
        log.error("candidate %s failed (%s): %s", cand_id, status, e)
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def tick_stream(db: Database, cfg: Config, buf: Buffer, chat_available: bool = True,
                ended: bool = False, now: float | None = None) -> list[int]:
    """One analysis pass for a stream. Safe to call repeatedly."""
    buf.scan(ended=ended)
    audio.analyze_new_segments(db, buf.stream_id, cfg.detection.window_seconds, ended)
    detection.process_windows(db, buf.stream_id, cfg, chat_available, now)
    promote_ready(db, buf, cfg, ended)
    clips = []
    for c in db.query("SELECT id FROM candidates WHERE stream_id=? AND status='ready' ORDER BY score DESC",
                      (buf.stream_id,)):
        cid = process_candidate(db, cfg, c["id"], buf)
        if cid:
            clips.append(cid)
    if not ended:
        buf.prune(cfg.capture.retention_minutes * 60)
    return clips


class Runner:
    """Live loop: discover -> rights gate -> capture + chat -> analyse -> drafts, while streams are live."""

    def __init__(self, cfg: Config, db: Database, max_streams: int | None = None, rediscover_s: int | None = None):
        self.cfg, self.db = cfg, db
        self.max_streams = max_streams or cfg.daemon.max_streams
        self.rediscover_s = rediscover_s or cfg.discovery.interval_seconds
        self.active: dict[int, tuple[Capture, ChatIngestor, Buffer]] = {}
        self.stop_evt = threading.Event()

    def _start(self, sid: int) -> None:
        s = self.db.one("SELECT * FROM streams WHERE id=?", (sid,))
        bdir = self.cfg.buffer_dir / f"stream_{sid}"
        cap = Capture(sid, s["url"], bdir, self.cfg, self.db)
        try:
            src = make_source(s["platform"], s["url"], s["video_id"], self.cfg.work_dir / f"chat_{sid}")
        except ValueError:
            src = None
        chat = ChatIngestor(self.db, sid, src) if src else None
        cap.start()
        if chat:
            chat.start()
        self.db.execute("UPDATE streams SET state='recording', started_at=? WHERE id=?", (time.time(), sid))
        self.active[sid] = (cap, chat, Buffer(self.db, sid, bdir, self.cfg.capture.segment_seconds))
        log.info("recording stream %s: %s", sid, s["title"])

    def _finish(self, sid: int, reason: str) -> None:
        cap, chat, buf = self.active.pop(sid)
        cap.stop()
        if chat:
            chat.stop()
        tick_stream(self.db, self.cfg, buf, chat.available if chat else False, ended=True)
        self.db.execute("UPDATE streams SET state='ended', state_reason=?, ended_at=? WHERE id=?",
                        (reason, time.time(), sid))
        log.info("stream %s finished: %s", sid, reason)

    def _discover(self):
        """allowlist mode polls only channels you have permission for; trending mode also searches
        popular live streams (everything is still passed through the rights gate afterwards)."""
        from .discovery.allowed import discover_allowed
        found = []
        try:
            found += discover_allowed(self.cfg, self.db)
        except Exception as e:  # noqa: BLE001
            log.warning("allowlist discovery failed: %s", e)
        if self.cfg.discovery.mode == "trending":
            found += discover_all(self.cfg)
        return found

    def _heartbeat(self) -> None:
        try:
            self.cfg.heartbeat_path.write_text(str(time.time()))
        except OSError:
            pass

    def run(self, once: bool = False) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: self.stop_evt.set())
            except ValueError:
                pass  # not main thread
        last_disc = 0.0
        while not self.stop_evt.is_set():
            self._heartbeat()
            if time.time() - last_disc > self.rediscover_s and len(self.active) < self.max_streams:
                last_disc = time.time()
                found = self._discover()
                res = register_streams(self.db, found)
                if found and not any(ok for _, ok, _ in res):
                    log.warning("found %d live streams but none are on the rights allowlist "
                                "(add [[rights.allow]] entries to config.toml)", len(found))
                for sid, ok, _ in res:
                    if ok and sid not in self.active and len(self.active) < self.max_streams:
                        self._start(sid)
            for sid in list(self.active):
                cap, chat, buf = self.active[sid]
                try:
                    tick_stream(self.db, self.cfg, buf, chat.available if chat else False)
                except Exception as e:  # noqa: BLE001 - one bad tick must not kill the loop
                    log.exception("tick failed for stream %s: %s", sid, e)
                    self.db.log_event("error", "runner", f"tick: {e}", sid)
                if cap.finished.is_set():
                    self._finish(sid, cap.reason or "capture ended")
            if once and not self.active:
                break
            self.stop_evt.wait(5)
        for sid in list(self.active):
            self._finish(sid, "shutdown")


# ----------------------------------------------------------------------------- offline mode

def process_recording(db: Database, cfg: Config, video: Path, chat_log: Path | None, channel: str,
                      basis: str, base_ts: float = 1_700_000_000.0) -> list[int]:
    """Run the full pipeline on a local recording (VOD mode / tests / dry runs).

    chat_log: JSON-lines of {"t": seconds_from_start, "author": str, "text": str}.
    """
    from .rights import set_source
    set_source(db, "local", channel, "allowed", name=channel, basis=basis, note="offline file")
    db.execute("INSERT OR IGNORE INTO streams(platform,video_id,channel_id,channel_name,title,url,state,discovered_at)"
               " VALUES ('local',?,?,?,?,?,'recording',?)",
               (video.name, channel, channel, video.stem, str(video), time.time()))
    sid = db.one("SELECT id FROM streams WHERE platform='local' AND video_id=?", (video.name,))["id"]
    bdir = cfg.buffer_dir / f"stream_{sid}"
    bdir.mkdir(parents=True, exist_ok=True)
    tmp = bdir / "_split"
    tmp.mkdir(exist_ok=True)
    r = run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-c", "copy", "-f", "segment",
             "-segment_time", str(cfg.capture.segment_seconds), "-reset_timestamps", "1",
             "-segment_format", "mpegts", str(tmp / "p_%05d.ts")], timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"cannot split recording: {r.stderr[-300:]}")
    cum = 0.0
    for p in sorted(tmp.glob("p_*.ts")):
        d = float(ffprobe_json(str(p)).get("format", {}).get("duration") or 0)
        p.rename(bdir / f"seg_{int(round(base_ts + cum))}.ts")
        cum += d
    shutil.rmtree(tmp, ignore_errors=True)
    if chat_log and Path(chat_log).exists():
        rows = []
        for line in Path(chat_log).read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
                rows.append((sid, base_ts + float(o["t"]), str(o.get("author", "?")), str(o["text"])))
            except (ValueError, KeyError, TypeError):
                continue  # malformed chat lines are skipped
        db.insert_many("INSERT INTO chat_messages(stream_id,ts,author,text) VALUES (?,?,?,?)", rows)
    buf = Buffer(db, sid, bdir, cfg.capture.segment_seconds)
    clips = tick_stream(db, cfg, buf, chat_available=bool(chat_log), ended=True, now=base_ts + cum + 60)
    db.execute("UPDATE streams SET state='ended', ended_at=? WHERE id=?", (time.time(), sid))
    return clips
