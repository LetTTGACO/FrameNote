"""Explicit backend selection; importing contracts never loads a model."""
from __future__ import annotations
from typing import TYPE_CHECKING
from .types import ASRBackend

if TYPE_CHECKING:
    from ..service_settings import Settings


def create_backend(settings: Settings) -> ASRBackend:
    if settings.asr_backend == "sensevoice":
        from .sensevoice import SenseVoiceBackend
        return SenseVoiceBackend(settings.sensevoice)
    raise ValueError("Unknown ASR backend: " + settings.asr_backend)
