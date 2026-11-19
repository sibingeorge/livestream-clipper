"""Metadata sidecar for each draft: title, description, credit, scores, rights basis."""
from __future__ import annotations

import json
from pathlib import Path


def write_metadata(path: Path, *, story, stream, candidate, quality, clip_window, rights_basis,
                   captions: bool) -> Path:
    data = {
        "title": story.title, "description": story.description, "hashtags": story.hashtags,
        "hook": story.hook, "keywords": story.keywords, "chat_quotes": story.chat_quotes,
        "source": {"platform": stream["platform"], "channel": stream["channel_name"],
                   "channel_id": stream["channel_id"], "stream_url": stream["url"],
                   "stream_title": stream["title"], "window_utc": clip_window,
                   "rights_basis": rights_basis},
        "detection": {"score": candidate["score"], "chat_z": candidate["chat_z"], "loud_z": candidate["loud_z"],
                      "reasons": json.loads(candidate["reasons"] or "[]")},
        "quality": {"ok": quality.ok, "issues": quality.issues, "metrics": quality.metrics},
        "captions": captions, "published": False,
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
