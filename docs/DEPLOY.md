# Running it unattended

You need an always-on machine (a home server, mini PC, or small VPS with ~4 cores / 8 GB RAM; a GPU is optional).
This was developed in a throwaway sandbox, so **it is not running anywhere until you start it on your machine**.

## 1. Configure (once)
```bash
cp config.example.toml config.toml   # list the channels you have permission to clip under [[rights.allow]]
cp .env.example .env                 # SMTP settings + MAIL_TO (your inbox); never commit this file
```
**Email credentials.** A background service cannot use Claude's mailbox connectors (those only exist inside a Claude
session), so it sends through SMTP. For an iCloud mailbox create an *app-specific password*
(account.apple.com → Sign-In and Security → App-Specific Passwords) and use `smtp.mail.me.com:587`; the
SMTP user is your Apple ID email. Gmail works the same way with an app password.

Check it: `clipper mail-test` must deliver a test message to `MAIL_TO`.

## 2. Start
Docker (recommended): `docker compose up -d --build`, then `docker compose logs -f`.
Without Docker: `pip install -e '.[asr]'`, install ffmpeg, then use `deploy/clipper.service` with systemd.

`clipper status` shows streams/candidates/clips/outbox counts; `clipper outbox` shows email delivery.
Docker's health check runs `clipper health` (the loop writes a heartbeat; stale > 5 min = unhealthy).

## 3. What happens
Every `interval_seconds` it checks each allow-listed channel for a live stream, records it, and watches chat + audio.
When both spike it builds a 30 s vertical clip (20 s lead-in), captions it, runs quality checks, and queues an email
with the video attached (re-encoded if it would exceed the mail size limit). Failed emails retry with backoff; a mail
outage never stops recording. Nothing is ever posted for you.

## Maintenance
* Rebuild/upgrade yt-dlp regularly (`docker compose build --pull --no-cache`): platforms change and break old versions.
* Disk: the rolling buffer self-prunes; delete old `clips/` as you go.
* Tune `[detection]`/`[scoring]` after looking at a week of real clips; defaults are untested on live streams.
