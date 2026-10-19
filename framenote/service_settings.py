"""Configuration for the headless service; no desktop initialization."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    workspace: Path
    sensevoice_model: Path
    vad_model: Path
    asr_python: str = sys.executable
    threads: int = 4
    galaxy_enabled: bool = False
    galaxy_base_url: str = "https://downloader-api.bhwa233.com"
    max_upload_bytes: int = 2 * 1024**3
    process_timeout: float = 7200

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            workspace=Path(os.getenv("FRAMENOTE_WORKSPACE", "workspace")).resolve(),
            sensevoice_model=Path(os.getenv("FRAMENOTE_SENSEVOICE_MODEL", "models/sensevoice" )).resolve(),
            vad_model=Path(os.getenv("FRAMENOTE_VAD_MODEL", "models/vad")).resolve(),
            asr_python=os.getenv("FRAMENOTE_ASR_PYTHON", sys.executable),
            threads=max(1, int(os.getenv("FRAMENOTE_ASR_THREADS", "4"))),
            galaxy_enabled=os.getenv("FRAMENOTE_GALAXY_ENABLED", "false").lower() in {"1", "true", "yes"},
            galaxy_base_url=os.getenv("FRAMENOTE_GALAXY_BASE_URL", "https://downloader-api.bhwa233.com").rstrip("/"),
        )
