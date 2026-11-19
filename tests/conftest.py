import json
import random
import subprocess

import pytest

from clipper.config import Config
from clipper.db import Database

BASE = 1_700_000_000.0


@pytest.fixture
def cfg(tmp_path):
    c = Config(data_dir=tmp_path / "data", clips_dir=tmp_path / "clips")
    c.asr.enabled = False  # no model downloads in tests
    c.ensure_dirs()
    return c


@pytest.fixture
def db(cfg):
    return Database(cfg.db_path)


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    """200 s 16:9 video; quiet pink noise with a loud burst at 150-160 s, plus a chat burst there."""
    d = tmp_path_factory.mktemp("media")
    video = d / "stream.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=200",
         "-f", "lavfi", "-i", "anoisesrc=d=200:c=pink:r=44100:a=0.03",
         "-af", "volume=volume=14:enable='between(t,150,160)'", "-c:v", "libx264", "-g", "30",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video)], check=True)
    rnd = random.Random(1)
    lines = []
    for t in range(0, 200, 4):
        lines.append({"t": t + rnd.random(), "author": f"u{rnd.randint(1, 5)}", "text": "hello stream"})
    phrases = ["NO WAY", "omg trailer", "wtf is that", "LETS GO gta", "insane trailer", "clip it", "holy"]
    for i in range(40):
        lines.append({"t": 150.5 + i * 0.22, "author": f"viewer{i % 14}", "text": phrases[i % len(phrases)]})
    lines.append({"t": 152, "author": "x", "text": "this is not json-safe"})
    chat = d / "chat.jsonl"
    chat.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json at all\n{\"t\": 1}\n")
    return video, chat
