"""Video composition: 9:16 canvas, blurred fill + fitted gameplay, burned-in ASS text."""
from __future__ import annotations

from pathlib import Path

from .config import Config


class ComposeError(Exception):
    pass


def compose(raw: Path, ass_text: str, out: Path, cfg: Config) -> Path:
    w, h = cfg.clip.width, cfg.clip.height
    out.parent.mkdir(parents=True, exist_ok=True)
    ass = out.with_suffix(".ass")
    ass.write_text(ass_text, encoding="utf-8")
    # Blur at 1/8 resolution then upscale: visually identical behind a blur, ~10x cheaper.
    sw, sh = w // 8, h // 8
    fc = (f"[0:v]split=2[a][b];[a]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},"
          f"boxblur=3:2,scale={w}:{h}:flags=bilinear,eq=brightness=-0.08[bg];"
          f"[b]scale={w}:-2:force_original_aspect_ratio=decrease[fg];"
          f"[bg][fg]overlay=(W-w)/2:(H-h)/2,ass={ass.name}[v]")
    # cwd = output dir so the filter sees a plain relative filename (no path escaping issues)
    import subprocess
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(Path(raw).resolve()), "-filter_complex", fc,
           "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
           "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
           "-t", str(cfg.clip.length), out.name]
    try:
        r = subprocess.run(cmd, cwd=out.parent, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired as e:
        raise ComposeError("ffmpeg compose timed out") from e
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise ComposeError(f"ffmpeg compose failed: {r.stderr[-400:]}")
    return out
