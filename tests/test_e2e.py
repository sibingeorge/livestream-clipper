import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from clipper.buffer import Buffer
from clipper.config import Config
from clipper.db import Database
from clipper.extract import ExtractError, extract_clip
from clipper.pipeline import process_recording
from clipper.review import decide, queue
from clipper.util import ffprobe_json
from clipper.web import make_handler
from conftest import BASE


@pytest.fixture(scope="session")
def built(tmp_path_factory, media):
    """Run the whole pipeline once; tests get private copies so they can mutate freely."""
    root = tmp_path_factory.mktemp("built")
    c = Config(data_dir=root / "data", clips_dir=root / "clips")
    c.asr.enabled = False
    c.ensure_dirs()
    d = Database(c.db_path)
    video, chat = media
    ids = process_recording(d, c, video, chat, "TestChan", "owned")
    d.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return root, ids


@pytest.fixture
def processed(built, tmp_path):
    root, ids = built
    new = tmp_path / "copy"
    shutil.copytree(root, new)
    c = Config(data_dir=new / "data", clips_dir=new / "clips")
    c.asr.enabled = False
    d = Database(c.db_path)
    for table, cols in (("segments", ["path"]), ("clips", ["path", "meta_path"])):
        for col in cols:
            d.execute(f"UPDATE {table} SET {col}=replace({col}, ?, ?)", (str(root), str(new)))
    return c, d, ids


def test_full_pipeline_makes_valid_vertical_draft(processed):
    cfg, db, ids = processed
    assert len(ids) == 1, "exactly one clip for one coincident spike (no duplicates)"
    clip = db.one("SELECT * FROM clips WHERE id=?", (ids[0],))
    assert clip["status"] == "review", clip["quality"]
    info = ffprobe_json(clip["path"])
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1080, 1920)
    assert abs(float(info["format"]["duration"]) - 30) < 1.0
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    meta = json.loads(Path(clip["meta_path"]).read_text())
    assert meta["published"] is False and meta["source"]["rights_basis"] == "owned"
    assert meta["title"] and "#GTA6" in meta["hashtags"] and meta["detection"]["reasons"]
    # event is the burst at t=150-160 -> clip window starts 20 s before the end of the burst window
    assert meta["source"]["window_utc"] == [BASE + 140, BASE + 170]
    assert db.one("SELECT status FROM candidates")["status"] == "done"


def test_rolling_buffer_protects_candidate_footage(processed):
    cfg, db, _ = processed
    buf = Buffer(db, 1, cfg.buffer_dir / "stream_1")
    pruned = buf.prune(keep_seconds=0, now=BASE + 10_000)
    assert pruned > 0
    kept = db.query("SELECT * FROM segments WHERE protected=1 AND status!='deleted'")
    assert kept, "segments backing the clip window must survive pruning"


def test_review_queue_moves_files(processed):
    cfg, db, ids = processed
    assert [c["id"] for c in queue(db)] == ids
    decide(db, cfg, ids[0], "approve", "looks good")
    row = db.one("SELECT * FROM clips WHERE id=?", (ids[0],))
    assert row["status"] == "approved" and "/approved/" in row["path"] and Path(row["path"]).exists()
    assert Path(row["meta_path"]).exists()
    with pytest.raises(ValueError):
        decide(db, cfg, ids[0], "publish")
    with pytest.raises(KeyError):
        decide(db, cfg, 999, "reject")


def test_dashboard_requires_token_and_supports_range(processed):
    cfg, db, ids = processed
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, cfg, "tok"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert "Clip review" in urllib.request.urlopen(base + "/").read().decode()
        req = urllib.request.Request(base + f"/video/{ids[0]}", headers={"Range": "bytes=0-99"})
        r = urllib.request.urlopen(req)
        assert r.status == 206 and len(r.read()) == 100
        bad = urllib.request.Request(base + f"/decide/{ids[0]}", data=b"decision=approve&token=wrong")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad)
        assert e.value.code == 403
        assert db.one("SELECT status FROM clips")["status"] == "review"
    finally:
        srv.shutdown()


def test_extract_fails_cleanly_on_gap_and_missing_footage(processed, tmp_path):
    cfg, db, _ = processed
    buf = Buffer(db, 1, cfg.buffer_dir / "stream_1")
    with pytest.raises(ExtractError, match="no footage"):
        extract_clip(buf, BASE + 99_999, cfg, tmp_path / "x.mp4")
    mid = db.query("SELECT id FROM segments WHERE start_ts BETWEEN ? AND ? ORDER BY start_ts",
                   (BASE + 145, BASE + 160))[0]
    db.execute("UPDATE segments SET status='bad' WHERE id=?", (mid["id"],))  # drop a segment inside the window
    with pytest.raises(ExtractError, match="partially covered"):
        extract_clip(buf, BASE + 160, cfg, tmp_path / "x.mp4")
    # a window long enough to span three segments exposes the hole in the middle
    cfg.clip.pre_roll, cfg.clip.length = 30, 60
    with pytest.raises(ExtractError, match="gaps"):
        extract_clip(buf, BASE + 160, cfg, tmp_path / "x.mp4")


def test_bad_segment_and_malformed_chat_do_not_crash(cfg, db, tmp_path):
    d = cfg.buffer_dir / "stream_7"
    d.mkdir(parents=True)
    (d / f"seg_{int(BASE)}.ts").write_bytes(b"")                      # zero-byte (ffmpeg died)
    (d / f"seg_{int(BASE) + 30}.ts").write_bytes(b"garbage")           # undecodable
    (d / f"seg_{int(BASE) + 60}.ts").write_bytes(b"")                  # newest, still 'open'
    db.execute("INSERT INTO streams(id,platform,video_id,state) VALUES (7,'local','v','recording')")
    buf = Buffer(db, 7, d)
    buf.scan()
    st = {r["start_ts"]: r["status"] for r in db.query("SELECT * FROM segments")}
    assert st[BASE] == "bad" and st[BASE + 30] == "bad" and st[BASE + 60] == "open"
    from clipper.db import Database
    assert Database.loads("{not json", {"x": 1}) == {"x": 1}


def test_rerun_is_idempotent(cfg, db, media):
    video, chat = media
    a = process_recording(db, cfg, video, chat, "TestChan", "owned")
    b = process_recording(db, cfg, video, chat, "TestChan", "owned")
    assert len(a) == 1 and b == []  # no duplicate candidates/clips on re-run


def test_compose_with_captions_passes_validation(cfg, tmp_path, media):
    import subprocess
    from clipper.captions import build_ass
    from clipper.compose import compose
    from clipper.transcribe import Word
    from clipper.validate import validate_clip
    raw = tmp_path / "raw.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "140", "-t", "30", "-i", str(media[0]),
                    "-c:v", "libx264", "-c:a", "aac", str(raw)], check=True)
    words = [Word(2 + i * 0.35, 2.3 + i * 0.35, w) for i, w in enumerate("they showed the whole trailer".split())]
    out = compose(raw, build_ass(words, "HOOK", "Source: X", 1080, 1920, 30), tmp_path / "o" / "c.mp4", cfg)
    rep = validate_clip(out, cfg, expect_captions=True, has_captions=True)
    assert rep.ok, rep.issues


def test_validation_rejects_bad_clips(cfg, tmp_path):
    import subprocess
    from clipper.validate import validate_clip
    assert not validate_clip(tmp_path / "missing.mp4", cfg).ok
    bad = tmp_path / "black.mp4"   # wrong size, black, silent audio
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=black:s=640x360:d=10",
                    "-f", "lavfi", "-i", "anullsrc", "-t", "10", "-c:v", "libx264", "-c:a", "aac", str(bad)], check=True)
    rep = validate_clip(bad, cfg)
    text = " ".join(rep.issues)
    assert not rep.ok and "duration" in text and "resolution" in text and "black" in text and "quiet" in text


def test_runner_trending_mode_still_default_denies(cfg, db, monkeypatch):
    from clipper import pipeline
    from clipper.discovery.base import StreamInfo
    cfg.discovery.mode = "trending"
    monkeypatch.setattr(pipeline, "discover_all", lambda c: [StreamInfo("youtube", "v1", "u", channel_id="UCx")])
    started = []
    monkeypatch.setattr(pipeline.Runner, "_start", lambda self, sid: started.append(sid))
    pipeline.Runner(cfg, db).run(once=True)          # not on the allowlist: never recorded
    assert db.one("SELECT state FROM streams")["state"] == "blocked" and started == []


def test_runner_allowlist_mode_starts_only_permitted_live_channels(cfg, db, monkeypatch):
    from clipper import pipeline, rights
    from clipper.discovery.base import StreamInfo
    rights.set_source(db, "youtube", "UCok", "allowed", basis="owned")
    live = [StreamInfo("youtube", "v1", "u", channel_id="UCok", meta={"is_live": True}),
            StreamInfo("youtube", "v2", "u", channel_id="UCother", meta={"is_live": True})]
    monkeypatch.setattr("clipper.discovery.allowed.discover_allowed", lambda c, d: live)
    started = []
    monkeypatch.setattr(pipeline.Runner, "_start", lambda self, sid: started.append(sid))
    pipeline.Runner(cfg, db).run(once=True)
    states = {r["video_id"]: r["state"] for r in db.query("SELECT * FROM streams")}
    assert states == {"v1": "discovered", "v2": "blocked"} and len(started) == 1


def test_real_clip_is_emailed_end_to_end(processed):
    from clipper import mailer
    cfg, db, ids = processed
    cfg.email.min_score = 0.5

    class Fake:
        sent = []

        def send_message(self, m):
            self.sent.append(m)

        def quit(self):
            pass

    f = Fake()
    s = mailer.SmtpSettings("h", 587, "u", "p", "u@x", "dest@x")
    assert mailer.enqueue_new(db, cfg) == 1
    assert mailer.send_pending(db, cfg, s, lambda _s: f) == 1
    msg = f.sent[0]
    atts = {a.get_filename(): a for a in msg.iter_attachments()}
    assert set(atts) == {f"{ids[0]:05d}.mp4", f"{ids[0]:05d}.json"}
    assert len(atts[f"{ids[0]:05d}.mp4"].get_content()) > 100_000       # real video bytes
    body = msg.get_body().get_content()
    assert "Rights basis on file: owned" in body and "HASHTAGS: #GTA6" in body and "score 0." in msg["Subject"]
