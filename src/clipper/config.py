"""Configuration: dataclass defaults, optionally overridden by a TOML file."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class DiscoveryCfg:
    queries: list[str] = field(default_factory=lambda: ["GTA 6", "GTA VI", "Grand Theft Auto 6"])
    max_candidates: int = 10
    min_viewers: int = 0


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


def load_config(path: str | Path | None = None) -> Config:
    cfg = Config()
    p = Path(path) if path else Path("config.toml")
    if p.exists():
        with p.open("rb") as fh:
            _merge(cfg, tomllib.load(fh))
    elif path:
        raise FileNotFoundError(path)
    return cfg
