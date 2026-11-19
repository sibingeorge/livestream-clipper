"""Command line interface."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .db import Database
from .hardware import detect_hardware
from .log import setup_logging


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipper", description="Local-first live-stream clip studio")
    ap.add_argument("-c", "--config", help="path to config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="report hardware, tools and chosen model sizes")
    sub.add_parser("discover", help="list live streams and whether they pass the rights gate")

    r = sub.add_parser("rights", help="manage the rights allowlist (default deny)")
    rs = r.add_subparsers(dest="rcmd", required=True)
    for name in ("allow", "deny"):
        p = rs.add_parser(name)
        p.add_argument("platform")
        p.add_argument("channel_id")
        p.add_argument("--name", default="")
        p.add_argument("--basis", default="", help="owned|written-permission|licensed|creator-program|other")
        p.add_argument("--note", default="")
    rs.add_parser("list")

    run = sub.add_parser("run", help="record allow-listed live streams and produce drafts")
    run.add_argument("--max-streams", type=int, default=1)
    run.add_argument("--once", action="store_true", help="exit when no stream is active")

    f = sub.add_parser("process-file", help="run the full pipeline on a local recording (+ optional chat log)")
    f.add_argument("video")
    f.add_argument("--chat-log", help="JSON-lines: {t, author, text}")
    f.add_argument("--channel", required=True)
    f.add_argument("--basis", required=True, help="rights basis for this file")

    sub.add_parser("review", help="start the local review dashboard")
    q = sub.add_parser("queue", help="list clips awaiting review")
    q.add_argument("--status", default="review")
    d = sub.add_parser("decide", help="approve/reject a clip from the CLI")
    d.add_argument("clip_id", type=int)
    d.add_argument("decision", choices=["approve", "reject", "needs_edit"])
    d.add_argument("--note", default="")

    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    cfg.ensure_dirs()
    setup_logging(cfg.log_dir)
    db = Database(cfg.db_path)

    if a.cmd == "doctor":
        hw = detect_hardware()
        print(json.dumps(hw.as_dict(), indent=2))
        if not hw.ffmpeg:
            print("MISSING: ffmpeg", file=sys.stderr)
            return 1
        return 0
    if a.cmd == "discover":
        from .discovery import discover_all, register_streams
        found = discover_all(cfg)
        for s, (sid, ok, why) in zip(found, register_streams(db, found)):
            print(f"{'OK ' if ok else 'BLK'} {s.platform:8} {s.viewers:>7}  {s.channel_name[:24]:24} "
                  f"{s.title[:50]}  [{why}] ({s.channel_id})")
        return 0
    if a.cmd == "rights":
        from .rights import list_sources, set_source
        if a.rcmd == "list":
            for s in list_sources(db):
                print(f"{s['status']:7} {s['platform']:8} {s['channel_id']}  basis={s['basis']}  {s['note'] or ''}")
            return 0
        try:
            set_source(db, a.platform, a.channel_id, "allowed" if a.rcmd == "allow" else "denied",
                       a.name, a.basis, a.note)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"{a.rcmd}ed {a.platform}:{a.channel_id}")
        return 0
    if a.cmd == "run":
        from .pipeline import Runner
        Runner(cfg, db, a.max_streams).run(once=a.once)
        return 0
    if a.cmd == "process-file":
        from .pipeline import process_recording
        ids = process_recording(db, cfg, Path(a.video), Path(a.chat_log) if a.chat_log else None,
                                a.channel, a.basis)
        print(f"created {len(ids)} draft(s): {ids}")
        return 0
    if a.cmd == "review":
        from .web import serve
        serve(db, cfg)
        return 0
    if a.cmd == "queue":
        from .review import queue
        for c in queue(db, a.status):
            print(f"#{c['id']:<4} score={c['score'] or 0:.2f}  {c['title']}  {c['path']}")
        return 0
    if a.cmd == "decide":
        from .review import decide
        print(decide(db, cfg, a.clip_id, a.decision, a.note))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
