FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[asr]' && pip install --no-cache-dir -U yt-dlp
RUN useradd -m clipper && mkdir -p /app/data /app/clips && chown -R clipper /app
USER clipper
ENV HF_HOME=/app/data/hf
HEALTHCHECK --interval=2m --timeout=20s --start-period=2m CMD clipper health || exit 1
CMD ["clipper", "daemon"]
