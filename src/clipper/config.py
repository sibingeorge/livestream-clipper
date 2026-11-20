"""Configuration: dataclass defaults, optionally overridden by a TOML file."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class DiscoveryCfg:
    queries: list[str] = field(default_factory=lambda: ["GTA 6", "GTA VI", "Grand Theft Auto 6"])
    max_candidates: int = 10
    min_viewers: int = 0
    # allowlist: poll the channels you have permission for (recommended, fully automatic).
    # trending: search trending/most-viewed live streams; still gated by the allowlist.
    mode: str = "allowlist"
    interval_seconds: int = 120


@dataclass
class CaptureCfg:
    segment_seconds: int = 30
    max_height: int = 720
    retention_minutes: int = 20
    latency_offset_seconds: float = 0.0


@dataclass
class DetectionCfg:
    window_seconds: int = 10
    baseline_windows: int = 30
    min_baseline_windows: int = 12
    chat_z: float = 3.0
    loud_z: float = 2.5
    min_chat_messages: int = 5
    min_loudness_db: float = -40.0
    cooldown_seconds: int = 45
    audio_only_fallback: bool = False
    audio_only_loud_z: float = 5.0


@dataclass
class ClipCfg:
    pre_roll: int = 20
    length: int = 30
    width: int = 1080
    height: int = 1920


@dataclass
class ScoringCfg:
    min_score: float = 0.45
    hype_words: list[str] = field(default_factory=lambda: [
        "omg", "wtf", "insane", "no way", "lets go", "clip", "clip it", "holy", "crazy", "bro"])
    topic_words: list[str] = field(default_factory=lambda: [
        "gta", "trailer", "leak", "vice city", "lucia", "jason", "rockstar", "release", "gameplay"])


@dataclass
class AsrCfg:
    enabled: bool = True
    model: str = "auto"
    language: str = "en"


@dataclass
class QualityCfg:
    max_music_likelihood: float = 0.7
    min_mean_volume_db: float = -45.0
    max_black_ratio: float = 0.5


@dataclass
class ReviewCfg:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass
class YtdlpCfg:
    # YouTube may demand sign-in ("confirm you're not a bot"), mostly from datacenter IPs.
    # Provide a cookies.txt exported from a browser (keep it private; it is git-ignored) or a browser name.
    cookies_file: str = ""
    cookies_from_browser: str = ""


@dataclass
class RightsCfg:
    # [[rights.allow]] platform = "youtube", channel_id = "UC...", basis = "written-permission", note = "..."
    allow: list[dict] = field(default_factory=list)


@dataclass
class EmailCfg:
    """Recipient/limits only. SMTP credentials come from the environment (.env), never from config."""
    enabled: bool = True
    min_score: float = 0.5        # only email clips at least this promising
    max_per_day: int = 20         # protects your mailbox
    max_attachment_mb: float = 18.0  # iCloud/Gmail cap ~20-25 MB; larger clips are re-encoded to fit
    send_interval_seconds: int = 30


@dataclass
class DaemonCfg:
    max_streams: int = 2
    restart_backoff_seconds: int = 15


@dataclass
class Config:
    data_dir: Path = Path("data")
    clips_dir: Path = Path("clips")
    discovery: DiscoveryCfg = field(default_factory=DiscoveryCfg)
    capture: CaptureCfg = field(default_factory=CaptureCfg)
    detection: DetectionCfg = field(default_factory=DetectionCfg)
    clip: ClipCfg = field(default_factory=ClipCfg)
    scoring: ScoringCfg = field(default_factory=ScoringCfg)
    asr: AsrCfg = field(default_factory=AsrCfg)
    quality: QualityCfg = field(default_factory=QualityCfg)
    review: ReviewCfg = field(default_factory=ReviewCfg)
    rights: RightsCfg = field(default_factory=RightsCfg)
    ytdlp: YtdlpCfg = field(default_factory=YtdlpCfg)
    email: EmailCfg = field(default_factory=EmailCfg)
    daemon: DaemonCfg = field(default_factory=DaemonCfg)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipper.db"

    @property
    def buffer_dir(self) -> Path:
        return self.data_dir / "buffer"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.clips_dir, self.buffer_dir, self.work_dir, self.log_dir):
            Path(d).mkdir(parents=True, exist_ok=True)


def _merge(obj: Any, data: dict[str, Any], path: str = "") -> None:
    """Apply `data` onto dataclass `obj`; unknown keys raise so typos are caught."""
    known = {f.name: f for f in fields(obj)}
    for key, val in data.items():
        if key not in known:
            raise ValueError(f"unknown config key: {path}{key}")
        cur = getattr(obj, key)
        if is_dataclass(cur):
            if not isinstance(val, dict):
                raise ValueError(f"config section {path}{key} must be a table")
            _merge(cur, val, f"{path}{key}.")
        elif isinstance(cur, Path):
            setattr(obj, key, Path(val))
        else:
            if isinstance(cur, bool) != isinstance(val, bool) and not isinstance(cur, list):
                raise ValueError(f"config key {path}{key}: expected {type(cur).__name__}")
            setattr(obj, key, val)


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines). Real environment variables win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv()
    cfg = Config()
    p = Path(path) if path else Path("config.toml")
    if p.exists():
        with p.open("rb") as fh:
            _merge(cfg, tomllib.load(fh))
    elif path:
        raise FileNotFoundError(path)
    return cfg
