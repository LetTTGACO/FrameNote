"""PersonalOS export through the standalone client, with no live vault or service."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


spec = importlib.util.spec_from_file_location(
    "vault_client", Path(__file__).resolve().parents[1] / "skills/framenote/scripts/headless.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def workspace(tmp_path):
    work = tmp_path / "work"
    materials = work / "materials"
    (materials / "frames").mkdir(parents=True)
    bundle = {"schema_version": 3, "task_id": "A", "service_url": "http://server:18121",
              "download_complete": True, "task": {"status": "completed",
              "video_url": "https://www.bilibili.com/video/BV1234567890/?p=2"}}
    (materials / "bundle.json").write_text(json.dumps(bundle))
    frames = [{"frame_id": "a" * 32, "actual_time": 11.04,
               "relative_path": "materials/frames/" + "a" * 32 + ".jpg"},
              {"frame_id": "b" * 32, "actual_time": 90,
               "relative_path": "materials/frames/" + "b" * 32 + ".jpg"}]
    (materials / "frames.json").write_text(json.dumps(frames))
    for frame in frames:
        (work / frame["relative_path"]).write_bytes(frame["frame_id"].encode())
    (work / "notes.md").write_text(
        '---\ntitle: 旧标题\ncreated: "2026-10-20"\nbundle: materials/bundle.json\n---\n'
        '# 镜头教程\n\n[速览](#overview)\n\n<a id="overview"></a>\n\n## 一分钟速览\n'
        '[项目](https://example.com/project)\n\n## 引用资料\n来源见原视频。\n\n'
        '## 议题笔记\n![角色](materials/frames/' + 'a' * 32 + '.jpg)\n'
        '[原稿](chapters-transcript.md#chapter-1)\n\n## 后续讨论\n待讨论。\n\n'
        '## 原稿与证据回查\n[纠错](materials/corrections.md)\n')
    (work / "chapters-transcript.md").write_text(
        '# 分章原稿\n\nASR，VAD 近似时间，尚未逐字校对。\n\n'
        '<a id="chapter-1"></a>\n\n## 角色参考\n**00:00–00:20** 原始识别牛来。\n'
        '[返回](notes.md#overview)\n')
    (materials / "corrections.md").write_text(
        '# 纠错记录\n牛来依据画面核对。\n![依据](frames/' + 'a' * 32 + '.jpg)\n')
    return work


def export(workspace, vault, **kwargs):
    return module.export_note(workspace, vault, title="镜头教程", date="2026-10-25",
                              author="codex", tags=["AI视频"], **kwargs)


def test_export_keeps_reading_structure_and_full_evidence_without_raw_files(workspace, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    before = {p: p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    result = export(workspace, vault)
    note = Path(result["note_path"])
    transcript = Path(result["transcript_path"])
    assert note == vault / "03_Bookmarks/Videos/2026-10-20-镜头教程.md"
    assert transcript == vault / "08_Attachments/Files/FrameNote/2026-10-20-镜头教程-原稿.md"
    body = note.read_text()
    assert "date: \"2026-10-20\"" in body
    assert "source: \"codex\"" in body and "type: \"summary\"" in body
    assert "framenote_task_id: \"A\"" in body and "bundle:" not in body
    assert "## 引用资料" in body and "## 后续讨论" in body
    assert "[[#一分钟速览|速览]]" in body
    assert "[[08_Attachments/Files/FrameNote/2026-10-20-镜头教程-原稿#角色参考|原稿]]" in body
    assert "![[08_Attachments/Images/videos/2026-10-20-镜头教程/00m11s.jpg]]" in body
    assert "[项目](https://example.com/project)" in body
    raw = transcript.read_text()
    assert "原始识别牛来。" in raw and "## 识别纠错与疑点" in raw
    assert "[[03_Bookmarks/Videos/2026-10-20-镜头教程#一分钟速览|返回]]" in raw
    assert len(list(vault.rglob("*.jpg"))) == 1
    assert not list(vault.rglob("*.json"))
    assert {p: p.read_bytes() for p in workspace.rglob("*") if p.is_file()} == before


def test_repeat_export_finds_same_video_and_preserves_vault_edits(workspace, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    result = export(workspace, vault)
    note = Path(result["note_path"])
    note.write_text(note.read_text() + "\n用户的新讨论。\n")
    before = {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    bundle_path = workspace / "materials/bundle.json"
    bundle = json.loads(bundle_path.read_text())
    bundle["task_id"] = "B"
    bundle["task"]["video_url"] += "&utm_source=share"
    bundle_path.write_text(json.dumps(bundle))
    result = module.export_note(workspace, vault, title="新标题", date="2026-10-26", author="hermes", tags=[])
    assert result["existing"] is True
    assert result["note_path"] == str(note)
    assert {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()} == before
    assert module.note_context(note)["task_id"] == "A"


def test_copied_single_script_exports_under_python_no_site(workspace, tmp_path):
    script = tmp_path / "standalone.py"
    shutil.copyfile(module.__file__, script)
    vault = tmp_path / "vault"
    vault.mkdir()
    environment = {k: v for k, v in os.environ.items() if not k.startswith("FRAMENOTE_")}
    result = subprocess.run([sys.executable, "-S", str(script), "export", "--out-dir", str(workspace),
                             "--vault-dir", str(vault), "--title", "镜头教程", "--date", "2026-10-25"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)["note_path"]).is_file()


@pytest.mark.parametrize("problem", ["incomplete", "missing_frame", "broken_heading", "outside_link", "symlink"])
def test_export_preflights_before_writing_vault_files(workspace, tmp_path, problem):
    vault = tmp_path / "vault"
    vault.mkdir()
    if problem == "incomplete":
        marker = workspace / "materials/bundle.json"
        bundle = json.loads(marker.read_text())
        bundle["download_complete"] = False
        marker.write_text(json.dumps(bundle))
    elif problem == "missing_frame":
        (workspace / "materials/frames" / ("a" * 32 + ".jpg")).unlink()
    elif problem == "broken_heading":
        note = workspace / "notes.md"
        note.write_text(note.read_text().replace("#chapter-1", "#不存在"))
    elif problem == "outside_link":
        note = workspace / "notes.md"
        note.write_text(note.read_text() + "\n[外部文件](../../outside.md)\n")
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        (vault / "08_Attachments").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        export(workspace, vault)
    assert not [p for p in vault.rglob("*") if p.is_file()]


def test_actual_frame_time_names_collisions_and_code_examples(workspace, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    index = workspace / "materials/frames.json"
    frames = json.loads(index.read_text())
    frames[1]["actual_time"] = 11.08
    frames[1]["requested_time"] = 12
    index.write_text(json.dumps(frames))
    note = workspace / "notes.md"
    note.write_text(note.read_text() + '\n![第二张](materials/frames/' + 'b' * 32 + '.jpg)\n'
                    '```md\n![示例](materials/frames/not-real.jpg)\n```\n')
    result = export(workspace, vault)
    assert sorted(Path(path).name for path in result["images"]) == ["00m11s-2.jpg", "00m11s.jpg"]
    assert '![示例](materials/frames/not-real.jpg)' in Path(result["note_path"]).read_text()


def test_name_collision_never_overwrites_unrelated_notes(workspace, tmp_path):
    vault = tmp_path / "vault"
    target = vault / "03_Bookmarks/Videos/2026-10-20-镜头教程.md"
    target.parent.mkdir(parents=True)
    target.write_text("用户的另一篇同名笔记。")
    result = export(workspace, vault)
    assert result["note_path"] != str(target)
    assert target.read_text() == "用户的另一篇同名笔记。"


def test_find_note_understands_platform_ids_but_keeps_bilibili_parts(workspace, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    result = export(workspace, vault)
    assert module.find_notes(vault, source_url="https://m.bilibili.com/video/BV1234567890?p=2&vd_source=abc")[0]["note_path"] == result["note_path"]
    assert module.find_notes(vault, source_url="https://www.bilibili.com/video/BV1234567890?p=1") == []
    note = Path(result["note_path"])
    note.write_text(note.read_text().replace("https://www.bilibili.com/video/BV1234567890/?p=2", "https://youtu.be/abc123"))
    assert module.find_notes(vault, source_url="https://www.youtube.com/watch?v=abc123&list=playlist")


def test_note_context_supports_old_bundle_and_new_service_identity(workspace, tmp_path):
    legacy = module.note_context(workspace / "notes.md")
    assert legacy["task_id"] == "A" and legacy["service_url"] == "http://server:18121"
    vault = tmp_path / "vault"
    vault.mkdir()
    result = export(workspace, vault)
    (workspace / "materials/bundle.json").unlink()
    current = module.note_context(Path(result["note_path"]))
    assert current["task_id"] == "A" and current["service_url"] == "http://server:18121"


def test_vault_configuration_resolves_per_file_and_respects_overrides(tmp_path):
    config = tmp_path / "user/client.json"
    config.parent.mkdir()
    config.write_text('{"vault_dir":"../knowledge"}')
    result = module.resolve_config(cwd=tmp_path, user_path=config, environ={})
    assert result["vault_dir"] == str(tmp_path / "knowledge")
    result = module.resolve_config(cwd=tmp_path, user_path=config,
        environ={"FRAMENOTE_VAULT_DIR": "environment-vault"}, vault_dir="explicit-vault")
    assert result["vault_dir"] == str(tmp_path / "explicit-vault")
    assert result["sources"]["vault_dir"] == "command line"


def test_local_cli_lookup_and_context_keep_task_service_pair(workspace, tmp_path, monkeypatch, capsys):
    vault = tmp_path / "vault"
    vault.mkdir()
    result = export(workspace, vault)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "user_config_path", lambda: tmp_path / "missing.json")
    for key in module.CONFIG_KEYS:
        monkeypatch.delenv("FRAMENOTE_" + key.upper(), raising=False)
    monkeypatch.setattr(sys, "argv", ["headless.py", "--vault-dir", str(vault), "find-note",
                                    "https://www.bilibili.com/video/BV1234567890?p=2"])
    module.main()
    found = json.loads(capsys.readouterr().out)["notes"][0]
    assert found["service_url"] == "http://server:18121"
    monkeypatch.setattr(sys, "argv", ["headless.py", "note-context", result["note_path"]])
    module.main()
    assert json.loads(capsys.readouterr().out)["task_id"] == "A"
