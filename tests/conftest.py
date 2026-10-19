from pathlib import Path
import subprocess
import sys
import pytest

from framenote.models import required_assets


@pytest.fixture
def video(tmp_path):
    target = tmp_path / "test.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=duration=5:size=160x90:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-c:v", "libx264", "-g", "100",
                    "-c:a", "aac", "-shortest", "-y", str(target)], check=True)
    return target


@pytest.fixture
def model_dirs(tmp_path):
    model, vad = tmp_path/"models/sensevoice", tmp_path/"models/vad"
    for group, names in required_assets().items():
        directory = model if group == "sensevoice" else vad
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            (directory/name).write_bytes(b"test-model")
    return model, vad
