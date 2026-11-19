# livestream-clipper

Local-first live-stream moment detector and **Shorts draft studio**. It records a stream, watches chat
and audio together, and when *both* spike it cuts a 30 s vertical clip (20 s lead-in), captions it locally,
titles it from what chat said, and drops it in a **review queue**. It never publishes anything.

* **No paid APIs.** yt-dlp, ffmpeg, SQLite, numpy and (optionally) faster-whisper, all running locally.
  Claude Code is only the tool that wrote this code, not a runtime dependency.
* **Default-deny rights gate.** Nothing is recorded unless you allow-listed the channel with a stated basis.
  Re-uploading streamers' footage without permission is the fastest way to get strikes or demonetised, so
  automation here means *automatic for channels you are cleared to use*, not scraping everything trending.

## Run it unattended (daemon)

```bash
cp config.example.toml config.toml   # add the channels you have permission for under [[rights.allow]]
cp .env.example .env                 # SMTP settings + MAIL_TO (your inbox). Git-ignored; never commit it.
docker compose up -d --build         # or see docs/DEPLOY.md for systemd
clipper mail-test                    # confirms the email path works
```
The daemon checks your allow-listed channels for live streams, records them, detects chat+audio spikes,
builds a captioned 9:16 clip, and **emails it to you with the video attached**. You review it in your inbox and
post it yourself. Full guide: [docs/DEPLOY.md](docs/DEPLOY.md).

## Manual / development commands

```bash
pip install -e '.[asr]'            # '.[asr]' adds faster-whisper (captions); omit to skip captions
clipper doctor                     # hardware report + which Whisper model will be used
clipper discover                   # list live streams; each is OK / BLK (blocked by rights gate)
clipper rights allow youtube UCxxxxxxxx --basis written-permission --note "email 2026-11-01"
clipper run                        # same loop as the daemon, without email
clipper status | outbox | review   # counts, email delivery, local web review UI
```

Dry run on a file you own (no network): `clipper process-file game.mp4 --chat-log chat.jsonl --channel me --basis owned`
(`chat.jsonl` = JSON lines `{"t": seconds_from_start, "author": "...", "text": "..."}`).

## Output

`clips/review/` → `clips/approved|rejected|needs_edit/`; each clip has a `.json` sidecar (title, description,
hashtags, source credit, rights basis, scores + reasons, QC metrics, `"published": false`).

## Optional: Twitch discovery

Register a free app at https://dev.twitch.tv/console and export `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET`
(see `.env.example`; real values are git-ignored). Twitch *chat* needs no credentials (anonymous IRC).

## Tests

`pip install -e '.[dev]' && pytest` generates synthetic media with ffmpeg and runs the pipeline end to end.
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for design, limits, rights risks and failure modes.
