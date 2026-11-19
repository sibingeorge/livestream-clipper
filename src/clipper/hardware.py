"""Hardware detection so we never pull a model the machine can't run."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, asdict

from .util import run


@dataclass
class Hardware:
    cpu_cores: int
    ram_gb: float
    gpu_name: str | None
    vram_gb: float | None
    ffmpeg: bool
    ytdlp: bool

    @property
    def has_cuda(self) -> bool:
        return self.gpu_name is not None

    def whisper_choice(self) -> tuple[str, str, str]:
        """(model, device, compute_type) sized to the machine."""
        if self.has_cuda and (self.vram_gb or 0) >= 10:
            return "large-v3", "cuda", "float16"
        if self.has_cuda and (self.vram_gb or 0) >= 5:
            return "medium", "cuda", "float16"
        if self.has_cuda:
            return "small", "cuda", "int8_float16"
        if self.ram_gb >= 12 and self.cpu_cores >= 8:
            return "small", "cpu", "int8"
        if self.ram_gb >= 6:
            return "base", "cpu", "int8"
        return "tiny", "cpu", "int8"

    def as_dict(self) -> dict:
        d = asdict(self)
        d["whisper"] = dict(zip(("model", "device", "compute"), self.whisper_choice()))
        return d


def _ram_gb() -> float:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    return 0.0


def detect_hardware() -> Hardware:
    gpu = vram = None
    if shutil.which("nvidia-smi"):
        r = run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], 10)
        if r.returncode == 0 and r.stdout.strip():
            try:
                name, mem = [x.strip() for x in r.stdout.splitlines()[0].split(",")]
                gpu, vram = name, round(float(mem) / 1024, 1)
            except ValueError:
                pass
    return Hardware(os.cpu_count() or 1, _ram_gb(), gpu, vram,
                    shutil.which("ffmpeg") is not None, shutil.which("yt-dlp") is not None
                    or _module_ok("yt_dlp"))


def _module_ok(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False
