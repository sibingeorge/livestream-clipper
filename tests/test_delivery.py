import smtplib
import subprocess
import time

import pytest

from clipper import mailer, rights
from clipper.config import load_config, load_dotenv
from clipper.db import Database
from clipper.discovery.allowed import discover_allowed, live_url

SETTINGS = mailer.SmtpSettings("smtp.example", 587, "me@example.com", "pw", "me@example.com", "dest@example.com")


class FakeSMTP:
    def __init__(self):
        self.sent, self.quit_called = [], False

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        self.quit_called = True


def seed_clip(db, cfg, score, tmp_path, cid, make_file=True):
    path = tmp_path / f"c{cid}.mp4"
    if make_file:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=320x180:d=2",
                        "-f", "lavfi", "-i", "sine=d=2", "-c:v", "libx264", "-c:a", "aac", str(path)], check=True)
    db.execute("INSERT INTO candidates(id,stream_id,event_ts,score,reasons,status,created_at) VALUES (?,?,?,?,?,?,?)",
               (cid, 1, cid, score, "[]", "done", time.time()))
    db.execute("INSERT INTO clips(id,candidate_id,stream_id,path,title,status,created_at) VALUES (?,?,?,?,?,?,?)",
               (cid, cid, 1, str(path), f"Title {cid}", "review", time.time()))


def test_config_allowlist_toml_env_and_sync(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text('[discovery]\nmode = "allowlist"\n[email]\nmin_score = 0.6\n'
                 '[[rights.allow]]\nplatform = "youtube"\nchannel_id = "UC1"\nbasis = "written-permission"\n'
                 '[[rights.allow]]\nplatform = "twitch"\nchannel_id = "bad"\n'          # no basis -> skipped
                 '[[rights.allow]]\nplatform = "tiktok"\nchannel_id = "x"\nbasis = "owned"\n')  # unsupported
    cfg = load_config(p)
    assert cfg.email.min_score == 0.6 and len(cfg.rights.allow) == 3
    db = Database(":memory:")
    assert rights.sync_config(db, cfg.rights.allow) == 1
    assert rights.check_source(db, "youtube", "UC1", {"is_live": True}).allowed
    rights.set_source(db, "youtube", "UC2", "denied")
    assert rights.sync_config(db, [{"platform": "youtube", "channel_id": "UC2", "basis": "owned"}]) == 0
    assert not rights.check_source(db, "youtube", "UC2").allowed       # explicit deny is never overridden
    env = tmp_path / ".env"
    env.write_text('# c\nSMTP_HOST=smtp.example\nMAIL_TO="a@b.c"\n')
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("MAIL_TO", "keep@real.env")
    load_dotenv(env)
    import os
    assert os.environ["SMTP_HOST"] == "smtp.example" and os.environ["MAIL_TO"] == "keep@real.env"


def test_discover_allowed_only_live_allowlisted_channels(cfg):
    db = Database(":memory:")
    rights.set_source(db, "youtube", "UCLIVE", "allowed", name="Live One", basis="owned")
    rights.set_source(db, "youtube", "UCOFF", "allowed", basis="owned")
    rights.set_source(db, "youtube", "UCDENIED", "denied")
    rights.set_source(db, "twitch", "Streamer", "allowed", basis="licensed")
    seen = []

    def fake(url, flat):
        seen.append(url)
        if "UCLIVE" in url:
            return {"id": "vid1", "title": "T", "is_live": True, "channel_id": "UCLIVE", "concurrent_view_count": 9}
        if "twitch" in url:
            return {"is_live": True, "title": "tw", "uploader": "Streamer"}
        raise RuntimeError("not currently live")

    found = discover_allowed(cfg, db, fake)
    assert sorted((s.platform, s.channel_id) for s in found) == [("twitch", "Streamer"), ("youtube", "UCLIVE")]
    assert not any("UCDENIED" in u for u in seen)               # never even polls denied channels
    assert live_url("youtube", "@h") == "https://www.youtube.com/@h/live"
    assert all(rights.check_source(db, s.platform, s.channel_id, s.meta).allowed for s in found)


def test_smtp_settings_require_env(monkeypatch):
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="SMTP_HOST"):
        mailer.SmtpSettings.from_env()


def test_enqueue_respects_min_score_cap_and_never_duplicates(cfg, db, tmp_path):
    cfg.email.min_score, cfg.email.max_per_day = 0.5, 2
    for i, sc in enumerate([0.9, 0.3, 0.8, 0.7], start=1):
        seed_clip(db, cfg, sc, tmp_path, i, make_file=False)
    assert mailer.enqueue_new(db, cfg) == 2                      # clip1, clip3 (clip2 low score, clip4 over cap)
    assert mailer.enqueue_new(db, cfg) == 0                      # idempotent
    st = {r["clip_id"]: (r["status"], r["last_error"]) for r in db.query("SELECT * FROM outbox")}
    assert st[1][0] == "queued" and st[3][0] == "queued"
    assert "min_score" in st[2][1] and "cap" in st[4][1]


def test_send_pending_delivers_once_with_attachments(cfg, db, tmp_path):
    seed_clip(db, cfg, 0.9, tmp_path, 1)
    mailer.enqueue_new(db, cfg)
    smtp = FakeSMTP()
    assert mailer.send_pending(db, cfg, SETTINGS, lambda s: smtp) == 1
    assert mailer.send_pending(db, cfg, SETTINGS, lambda s: smtp) == 0     # no double send
    msg = smtp.sent[0]
    assert msg["To"] == "dest@example.com" and "Title 1" in msg["Subject"]
    names = [p.get_filename() for p in msg.iter_attachments()]
    assert names == ["00001.mp4"]                                          # no json sidecar for this fixture
    assert "Before posting" in msg.get_body().get_content() and smtp.quit_called
    assert db.one("SELECT status FROM outbox")["status"] == "sent"


def test_failed_send_backs_off_and_retries(cfg, db, tmp_path):
    seed_clip(db, cfg, 0.9, tmp_path, 1)
    mailer.enqueue_new(db, cfg)

    def broken(_s):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    now = time.time()
    assert mailer.send_pending(db, cfg, SETTINGS, broken, now=now) == 0
    ob = db.one("SELECT * FROM outbox")
    assert ob["status"] == "queued" and ob["attempts"] == 1 and ob["next_try"] > now
    assert "535" in ob["last_error"]
    assert mailer.send_pending(db, cfg, SETTINGS, broken, now=now + 1) == 0   # not due yet: no new attempt
    assert db.one("SELECT attempts FROM outbox")["attempts"] == 1
    smtp = FakeSMTP()
    assert mailer.send_pending(db, cfg, SETTINGS, lambda s: smtp, now=now + 3 * 3600) == 1


def test_missing_clip_file_fails_permanently(cfg, db, tmp_path):
    seed_clip(db, cfg, 0.9, tmp_path, 1, make_file=False)
    mailer.enqueue_new(db, cfg)
    mailer.send_pending(db, cfg, SETTINGS, lambda s: FakeSMTP())
    assert db.one("SELECT status FROM outbox")["status"] == "failed"


def test_oversize_clip_is_shrunk_to_fit(tmp_path):
    big = tmp_path / "big.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=8",
                    "-c:v", "libx264", "-b:v", "6M", "-pix_fmt", "yuv420p", str(big)], check=True)
    assert big.stat().st_size > 1_000_000
    out = mailer.fit_attachment(big, 0.8, tmp_path / "w")
    assert out != big and out.stat().st_size <= 0.8 * 1024 * 1024
    assert mailer.fit_attachment(out, 5, tmp_path / "w") == out             # small file untouched
