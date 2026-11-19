"""Small shared helpers."""
from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any

from .log import get_logger

log = get_logger("util")


def now() -> float:
    return time.time()


def run(cmd: list[str], timeout: float = 600, check: bool = False) -> subprocess.CompletedProcess:
    """Run a command, capturing output. Never raises on non-zero unless check=True."""
    log.debug("run: %s", " ".join(map(str, cmd)))
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              check=check, errors="replace")
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, 124, e.stdout or "", f"timeout after {timeout}s")
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def ffprobe_json(path: str) -> dict[str, Any]:
    r = run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            timeout=60)
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def media_duration(path: str) -> float | None:
    try:
        return float(ffprobe_json(path).get("format", {}).get("duration"))
    except (TypeError, ValueError):
        return None


_SLUG = re.compile(r"[^a-zA-Z0-9]+")


def slug(text: str, maxlen: int = 40) -> str:
    return _SLUG.sub("-", text).strip("-").lower()[:maxlen] or "x"


def safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default
