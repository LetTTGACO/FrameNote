"""Validated native-service configuration, resolved once at startup."""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import sys
from typing import Mapping


def _positive(value: int | float, name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(name + " must be positive and finite")


@dataclass(frozen=True)
class SenseVoiceConfig:
    model_dir: Path
    vad_dir: Path
    python: str
    threads: int = 4
    timeout_seconds: float = 7200

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_dir", self.model_dir.expanduser().resolve())
        object.__setattr__(self, "vad_dir", self.vad_dir.expanduser().resolve())
        if type(self.threads) is not int:
            raise ValueError("ASR threads must be an integer")
        _positive(self.threads, "ASR threads")
        _positive(self.timeout_seconds, "ASR timeout")
        if not self.python.strip():
            raise ValueError("ASR Python is required")


@dataclass(frozen=True)
class Settings:
    workspace: Path
    sensevoice: SenseVoiceConfig
    asr_backend: str = "sensevoice"
    galaxy_enabled: bool = False
    galaxy_base_url: str = "https://downloader-api.bhwa233.com"
    max_upload_bytes: int = 2147483648
    process_timeout: float = 7200
    bilibili_credentials: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", self.workspace.expanduser().resolve())
        if self.bilibili_credentials is not None:
            object.__setattr__(self, "bilibili_credentials", Path(os.path.abspath(self.bilibili_credentials.expanduser())))
        if self.asr_backend != "sensevoice":
            raise ValueError("Unknown ASR backend: " + self.asr_backend)
        if type(self.max_upload_bytes) is not int:
            raise ValueError("Upload size must be an integer")
        _positive(self.max_upload_bytes, "Upload size")
        _positive(self.process_timeout, "Process timeout")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, base_dir: Path | None = None) -> Settings:
        values = os.environ if env is None else env
        base = (base_dir or Path.cwd()).expanduser().resolve()
        def path(key: str, default: str) -> Path:
            value = Path(values.get(key, default)).expanduser()
            return (value if value.is_absolute() else base / value).resolve()
        python = values.get("FRAMENOTE_ASR_PYTHON", sys.executable)
        if "/" in python or python.startswith("~"):
            value = Path(python).expanduser()
            python = os.path.abspath(value if value.is_absolute() else base / value)
        timeout = float(values.get("FRAMENOTE_PROCESS_TIMEOUT", "7200"))
        return cls(
            workspace=path("FRAMENOTE_WORKSPACE", "workspace"),
            bilibili_credentials=(base / Path(values["FRAMENOTE_BILIBILI_CREDENTIALS"]).expanduser()
                                  if values.get("FRAMENOTE_BILIBILI_CREDENTIALS") else None),
            sensevoice=SenseVoiceConfig(path("FRAMENOTE_SENSEVOICE_MODEL", "models/sensevoice"),
                                       path("FRAMENOTE_VAD_MODEL", "models/vad"), python,
                                       int(values.get("FRAMENOTE_ASR_THREADS", "4")), timeout),
            asr_backend=values.get("FRAMENOTE_ASR_BACKEND", "sensevoice"),
            galaxy_enabled=values.get("FRAMENOTE_GALAXY_ENABLED", "false").lower() in {"1", "true", "yes"},
            galaxy_base_url=values.get("FRAMENOTE_GALAXY_BASE_URL", "https://downloader-api.bhwa233.com").rstrip("/"),
            max_upload_bytes=int(values.get("FRAMENOTE_MAX_UPLOAD_BYTES", "2147483648")), process_timeout=timeout,
        )
