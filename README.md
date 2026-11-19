# livestream-clipper

Local-first live-stream moment detector and **Shorts draft studio**. It records a stream, watches chat
and audio together, and when *both* spike it cuts a 30 s vertical clip (20 s lead-in), captions it locally,
titles it from what chat said, and drops it in a **review queue**. It never publishes anything.

* **No paid APIs.** yt-dlp, ffmpeg, SQLite, numpy and (optionally) faster-whisper, all running locally.
  Claude Code is only the tool that wrote this code, not a runtime dependency.
* **Default-deny rights gate.** Nothing is recorded unless you allow-listed the channel with a stated basis.

## Quick start

```bash
sudo apt install ffmpeg            # or brew install ffmpeg
pip install -e '.[asr]'            # '.[asr]' adds faster-whisper (captions); omit to skip captions
cp config.example.toml config.toml # optional
clipper doctor                     # hardware report + which Whisper model will be used

clipper discover                   # list live streams; each is OK / BLK (blocked by rights gate)
clipper rights allow youtube UCxxxxxxxx --basis written-permission --note "email 2026-11-01"
clipper run                        # record allow-listed live streams, produce drafts into clips/review/
clipper review                     # http://127.0.0.1:8765  approve / needs-edit / reject
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
