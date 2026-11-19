# Architecture

## Status of the original project
The original clipper was not available when this work started (the build session began with an empty
working directory), so this is a **fresh implementation of the same goal**, not a migration of old code.
Nothing was preserved because nothing could be inspected. If you have the old project, diff it against
the modules below and port any working logic (e.g. site-specific discovery tweaks).

## Data flow

```
discover ─► rights gate ─► capture (yt-dlp | ffmpeg, 30 s segments, wall-clock names)
                │                         │
                │                         ▼
                │                 buffer index (SQLite) ─► audio: per-second energy ─► 10 s windows
                ▼                                                                      │
       chat ingest (YouTube via yt-dlp live_chat, Twitch anonymous IRC) ──► chat counts ─┤
                                                                                          ▼
                                   detection (robust z-scores, both must spike, cooldown dedupe)
                                                                                          ▼
                       scoring (rules) ─► candidate ─► wait for post-roll ─► extract 30 s (20 s before)
                                                                                          ▼
        faster-whisper ─► story (title/hook from chat) ─► ASS captions ─► compose 1080x1920 ─► QC gate
                                                                                          ▼
                                                  metadata .json ─► review queue (CLI / local web UI)
```

| Requirement | Module |
|---|---|
| Stream discovery | `discovery/youtube.py` (yt-dlp search filtered to Live, ranked by concurrent viewers), `discovery/twitch.py` (Helix, optional) |
| Source eligibility and rights metadata | `rights.py` (`sources` table: default-deny allowlist + basis + note) |
| Capture manager | `capture.py` (supervised yt-dlp→ffmpeg pipeline, backoff, stop on stream end) |
| Rolling buffer / timestamps | `buffer.py` (segment index, completeness, gap detection, pruning with protection) |
| Chat ingestion | `chat/` (reconnect with backoff, availability flag) |
| Audio analysis | `audio.py` (per-second energy buckets → 10 s loudness; music heuristic) |
| Event detection | `detection.py` |
| Candidate scoring | `scoring.py` |
| Clip extraction | `extract.py` |
| Transcription | `transcribe.py` (faster-whisper, model picked by `hardware.py`) |
| Editorial story generation | `story.py` (deterministic) |
| Caption rendering | `captions.py` (ASS) |
| Video composition | `compose.py` (blurred fill + fitted gameplay + burned text) |
| Quality validation | `validate.py` |
| Review queue | `review.py`, `web.py` (localhost dashboard), CLI |
| Metadata generation | `metadata.py` |
| Email delivery | `mailer.py` (SMTP from env; outbox table with dedupe, daily cap, min score, retry/backoff, size fitting) |
| Background service | `daemon.py` (supervised Runner + Mailer, heartbeat for health checks), `deploy/`, `Dockerfile` |
| Allowlist discovery | `discovery/allowed.py` (polls permitted channels for "live now") |
| Logging / Config / Storage | `log.py`, `config.py` (TOML, typo-checked), `db.py` (SQLite, WAL) |

## How it works without paid APIs
Everything is deterministic or runs locally: yt-dlp + ffmpeg for media, SQLite for state, numpy for
statistics, faster-whisper (open weights, CPU or GPU) for transcripts. Titles come from n-gram counting of
chat (weighted by unique authors, with the pre-spike background subtracted), not an LLM. A local LLM could be
added later behind `story.build_story` without touching the rest.

## Detection details
* Every 10 s window gets a robust z-score (median/MAD over the previous 30 windows) for chat message count
  and loudness. A candidate needs **both** ≥ thresholds, ≥ 5 messages, and loudness above a floor.
* Chat is shifted by `capture.latency_offset_seconds` to line up viewer delay with the recording.
* Candidates within `cooldown_seconds` are merged (the better one wins) so one moment gives one clip.
* Score (0-1) = chat/audio strength + coincidence + hype/topic words + author diversity; spammy chat is penalised.
  Every candidate stores human-readable reasons.
* If chat is unavailable the pipeline degrades: no triggers, unless `audio_only_fallback = true` (much higher bar).

## Platform restrictions, limits, rights risks
* **Rights.** Being able to record a stream does not make you allowed to reuse it. YouTube's and Twitch's terms
  restrict downloading, and streamers/publishers hold copyright (and Rockstar/Take-Two's content policy applies to GTA
  footage). The allowlist records *your* statement of permission; it cannot verify it. Default is deny.
* **Music/third-party audio.** Clips with steady, music-like audio are rejected by a crude heuristic
  (`music_likelihood`); it will miss some music and flag some non-music. Review remains manual.
* **YouTube discovery** uses search with the Live filter; YouTube's trending feed needs login, so "trending" is
  approximated by concurrent viewers. yt-dlp breaks when YouTube changes; keep it updated (`pip install -U yt-dlp`).
* **YouTube chat via yt-dlp** (`live_chat` subtitle) is unofficial and can lag or fail; some streams disable chat.
* **Latency.** Segments are cut at keyframes, so durations are ~30 s but not exact; windows are built from
  per-second energy so this doesn't matter for detection, but the clip start can be off by about a second.
* **Reaction vs. event.** Chat reacts a few seconds after the event; tune `latency_offset_seconds`.
* **CPU cost.** Composition is ~realtime-capable on 4 cores; Whisper `small` on CPU is slower than realtime on weak
  machines, so `clipper doctor` picks a smaller model. Candidates are processed sequentially in the runner loop.
* **Not verified in this build environment:** real YouTube/Twitch connections (network policy), and faster-whisper
  transcription (model download blocked). Both are covered by parser/unit tests or by feeding synthetic data
  through the same code, but have not been exercised live.

## Failure modes handled
| Failure | Behaviour |
|---|---|
| Stream ends | capture exits cleanly → final scan/analysis/clipping → stream marked `ended` |
| yt-dlp / ffmpeg crash or flaps | supervised restart with exponential backoff; gives up after 6 quick failures |
| Zero-byte / undecodable segment | marked `bad`, skipped; never breaks analysis |
| Dropped footage inside the clip window | extraction refuses (`gaps` / `partially covered`) → candidate `failed` with the reason |
| Chat unavailable or disconnecting | reconnect with backoff; flagged unavailable after 5 failures; degraded mode |
| Malformed chat/metadata/JSON | parsers return `None`; bad rows skipped; `Database.loads` falls back to defaults |
| Duplicate candidates / re-processing | cooldown merge + unique `(stream_id, event_ts)`; re-running a file creates nothing new |
| ffmpeg compose failure | retried up to 3 times, then `failed` with error text |
| Bad output (silent, black, wrong size, music-heavy) | QC marks `rejected_qc`; never enters the review queue |
| Disk growth | rolling buffer pruned after `retention_minutes`; candidate footage is protected |

## Delivery & operations notes
* Email goes through SMTP (iCloud/Gmail app password), because Claude's mailbox connectors only exist inside a Claude session.
* A clip is mailed at most once (`outbox` primary key); failed sends retry up to 12 times with exponential backoff.
* Clips are mailed only after QC passed and score >= `email.min_score`, capped at `email.max_per_day`.
* Virality is a *heuristic* (simultaneous chat + loudness spike, hype words, author diversity). It does not predict views or revenue.

## Not implemented (by design for the MVP)
Automatic publishing; a local LLM; per-game event detectors (GTA 6-specific logic is limited to topic keywords);
GPU-specific tuning; DRM/members-only streams (blocked by the eligibility check).
