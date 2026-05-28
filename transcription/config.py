from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


SUPPORTED_INPUT_EXTENSIONS = {".wav", ".mp3", ".aac", ".flac", ".ogg", ".mp4", ".m4a"}

FFMPEG_CONVERT_EXTENSIONS = {".mp4", ".m4a"}


@dataclass(slots=True)
class AppConfig:
    source_dir: Path
    processed_dir: Path
    one_file: Path | None
    api_key: str
    model: str
    mode: str
    language_code: str | None
    batch_mode: str
    job_poll_interval: float
    num_speakers: int | None
