"""Lightweight local review dashboard (stdlib only, bound to localhost)."""
from __future__ import annotations

import html
import json
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from .config import Config
from .db import Database
from .review import decide, queue

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Clip review</title><style>
body{{font:15px system-ui;margin:0;background:#111;color:#eee}}header{{padding:12px 20px;background:#1b1b1b}}
nav a{{color:#8ab4ff;margin-right:14px}}.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;padding:16px}}
.card{{background:#1c1c1c;border-radius:10px;padding:12px}}video{{width:100%;max-height:480px;background:#000;border-radius:6px}}
h3{{margin:.5em 0 .2em;font-size:15px}}small,li{{color:#aaa}}ul{{padding-left:18px;margin:.3em 0}}
button{{padding:6px 12px;margin:4px 4px 0 0;border:0;border-radius:6px;cursor:pointer}}
.ok{{background:#2e7d32;color:#fff}}.no{{background:#b71c1c;color:#fff}}.ed{{background:#6d4c00;color:#fff}}
input[type=text]{{width:100%;box-sizing:border-box;margin:6px 0;padding:6px;background:#111;color:#eee;border:1px solid #333;border-radius:6px}}
.bad{{color:#ff8a80}}</style>
<header><b>Clip review</b> &nbsp; <nav>{nav}</nav><small>Drafts only - nothing is published from here.</small></header>
<div class=grid>{cards}</div>"""
STATUSES = ["review", "needs_edit", "approved", "rejected", "rejected_qc"]


def render(db: Database, status: str, token: str) -> str:
    nav = " ".join(f'<a href="/?status={s}">{s}</a>' for s in STATUSES)
    cards = []
    for c in queue(db, status):
        q = db.loads(c["quality"], {}) or {}
        meta = {}
        try:
            meta = json.loads(Path(c["meta_path"]).read_text()) if c["meta_path"] else {}
        except (OSError, ValueError):
            pass
        issues = "".join(f'<li class=bad>{html.escape(str(i))}</li>' for i in q.get("issues", []))
        reasons = "".join(f"<li>{html.escape(str(r))}</li>" for r in db.loads(c["reasons"], []) or [])
        src = meta.get("source", {})
        form = ""
        if status in ("review", "needs_edit"):
            form = (f'<form method=post action="/decide/{c["id"]}"><input type=hidden name=token value="{token}">'
                    f'<input type=text name=note placeholder="note (optional)">'
                    f'<button class=ok name=decision value=approve>Approve</button>'
                    f'<button class=ed name=decision value=needs_edit>Needs edit</button>'
                    f'<button class=no name=decision value=reject>Reject</button></form>')
        cards.append(
            f'<div class=card><video controls preload=metadata src="/video/{c["id"]}"></video>'
            f'<h3>{html.escape(c["title"] or "")}</h3>'
            f'<small>score {c["score"] or 0:.2f} · {html.escape(str(src.get("channel", "")))} · '
            f'rights: {html.escape(str(src.get("rights_basis", "?")))}</small>'
            f'<ul>{reasons}{issues}</ul>{form}</div>')
    return PAGE.format(nav=nav, cards="".join(cards) or "<p style='padding:20px'>Nothing here.</p>")


def make_handler(db: Database, cfg: Config, token: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, body: bytes, ctype="text/html; charset=utf-8", headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path, _, qs = self.path.partition("?")
            if path == "/":
                status = parse_qs(qs).get("status", ["review"])[0]
                status = status if status in STATUSES else "review"
                return self._send(200, render(db, status, token).encode())
            m = re.fullmatch(r"/video/(\d+)", path)
            if m:
                row = db.one("SELECT path FROM clips WHERE id=?", (int(m.group(1)),))
                p = Path(row["path"]) if row else None
                if not p or not p.exists():
                    return self._send(404, b"not found")
                size = p.stat().st_size
                start, end = 0, size - 1
                rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
                code = 200
                if rng and (rng.group(1) or rng.group(2)):
                    if rng.group(1):
                        start = int(rng.group(1))
                        end = int(rng.group(2)) if rng.group(2) else size - 1
                    else:
                        start = max(0, size - int(rng.group(2)))
                    end = min(end, size - 1)
                    code = 206
                with p.open("rb") as fh:
                    fh.seek(start)
                    data = fh.read(end - start + 1)
                h = {"Accept-Ranges": "bytes"}
                if code == 206:
                    h["Content-Range"] = f"bytes {start}-{end}/{size}"
                return self._send(code, data, "video/mp4", h)
            self._send(404, b"not found")

        def do_POST(self):
            m = re.fullmatch(r"/decide/(\d+)", self.path)
            n = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(min(n, 10_000)).decode())
            if not m or form.get("token", [""])[0] != token:
                return self._send(403, b"forbidden")
            try:
                decide(db, cfg, int(m.group(1)), form.get("decision", [""])[0], form.get("note", [""])[0])
            except (ValueError, KeyError) as e:
                return self._send(400, str(e).encode())
            self._send(303, b"", headers={"Location": "/"})
    return H


def serve(db: Database, cfg: Config) -> None:
    token = secrets.token_urlsafe(16)
    srv = ThreadingHTTPServer((cfg.review.host, cfg.review.port), make_handler(db, cfg, token))
    print(f"Review dashboard: http://{cfg.review.host}:{cfg.review.port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
