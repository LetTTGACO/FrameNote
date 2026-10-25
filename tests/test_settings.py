from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient
from framenote.service import create_app
from framenote.service_settings import SenseVoiceConfig, Settings


def test_settings_defaults_paths_and_precedence(tmp_path, monkeypatch):
    base = tmp_path/"path with spaces"
    settings = Settings.from_env({}, base_dir=base)
    assert settings.workspace == base/"workspace"
    assert settings.sensevoice.model_dir == base/"models/sensevoice"
    assert settings.sensevoice.vad_dir == base/"models/vad"
    assert settings.sensevoice.python == sys.executable
    assert settings.sensevoice.threads == 4
    assert settings.sensevoice.timeout_seconds == settings.process_timeout == 7200
    assert settings.max_upload_bytes == 2147483648
    assert not settings.galaxy_enabled
    custom = Settings.from_env({"FRAMENOTE_WORKSPACE":"~/framenote-test","FRAMENOTE_PROCESS_TIMEOUT":"12","FRAMENOTE_MAX_UPLOAD_BYTES":"123"}, base_dir=base)
    assert custom.workspace == Path.home()/"framenote-test"
    assert custom.sensevoice.timeout_seconds == 12 and custom.max_upload_bytes == 123
    monkeypatch.chdir(tmp_path)
    assert settings.workspace == base/"workspace"
    monkeypatch.setenv("FRAMENOTE_WORKSPACE", str(tmp_path/"wrong"))
    with TestClient(create_app(settings)) as client:
        assert client.app.state.store.root == base/"workspace/tasks"


@pytest.mark.parametrize("env", [
    {"FRAMENOTE_ASR_THREADS":"0"}, {"FRAMENOTE_ASR_THREADS":"-1"},
    {"FRAMENOTE_MAX_UPLOAD_BYTES":"0"}, {"FRAMENOTE_MAX_UPLOAD_BYTES":"-1"},
    {"FRAMENOTE_PROCESS_TIMEOUT":"0"}, {"FRAMENOTE_PROCESS_TIMEOUT":"NaN"},
    {"FRAMENOTE_PROCESS_TIMEOUT":"inf"}, {"FRAMENOTE_ASR_BACKEND":"unknown"},
])
def test_settings_reject_invalid_numbers_and_backend(env, tmp_path):
    with pytest.raises(ValueError):
        Settings.from_env(env, base_dir=tmp_path)


def test_galaxy_master_switch_is_exposed_in_health(tmp_path):
    enabled = Settings.from_env({"FRAMENOTE_GALAXY_ENABLED": "true"}, base_dir=tmp_path)
    assert enabled.galaxy_enabled
    with TestClient(create_app(enabled)) as client:
        assert client.get("/api/health").json()["galaxy_enabled"] is True


def test_bilibili_credentials_optional_path_uses_startup_directory(tmp_path):
    assert Settings.from_env({}, base_dir=tmp_path).bilibili_credentials is None
    assert Settings.from_env({'FRAMENOTE_BILIBILI_CREDENTIALS': ''}, base_dir=tmp_path).bilibili_credentials is None
    configured = Settings.from_env({'FRAMENOTE_BILIBILI_CREDENTIALS': 'private/bilibili.json'}, base_dir=tmp_path)
    assert configured.bilibili_credentials == tmp_path / 'private/bilibili.json'
