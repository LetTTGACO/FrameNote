"""Independent regression probes for the PersonalOS export contract."""
import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "review_vault_client", Path(__file__).resolve().parents[1] / "skills/framenote/scripts/headless.py")
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


@pytest.fixture
def authored_video(tmp_path):
    work = tmp_path / "staging"
    materials = work / "materials"
    (materials / "frames").mkdir(parents=True)
    frame = "c" * 32
    frame_path = f"materials/frames/{frame}.jpg"
    (work / frame_path).write_bytes(b"review-image")
    (materials / "frames.json").write_text(json.dumps([
        {"frame_id": frame, "actual_time": 65.25, "relative_path": frame_path}]))
    (materials / "bundle.json").write_text(json.dumps({
        "schema_version": 3, "task_id": "review_task", "service_url": "http://review.example:18121",
        "download_complete": True, "task": {"status": "completed",
        "video_url": "https://www.youtube.com/watch?v=review_video"}}))
    (work / "chapters-transcript.md").write_text(
        "# 分章原稿\n\n## Chapter\n\n[00:00–00:10] 原始识别。\n\n[回到笔记](notes.md#Summary)\n")
    vault = tmp_path / "vault"
    vault.mkdir()
    return work, vault, frame_path


def publish(work, vault):
    return client.export_note(work, vault, title="Review video", date="2026-10-25",
                              author="reviewer", tags=["review"])


def test_review_inline_export_survives_loss_of_staging_directory(authored_video):
    work, vault, frame = authored_video
    (work / "notes.md").write_text(
        f"# Video\n\n## Summary\n\n![Frame]({frame})\n\n[Raw](chapters-transcript.md#Chapter)\n")
    result = publish(work, vault)
    note = Path(result["note_path"]).read_text()
    assert "![[08_Attachments/Images/videos/2026-10-25-Review video/01m05s.jpg]]" in note
    assert "[[08_Attachments/Files/FrameNote/2026-10-25-Review video-原稿#Chapter|Raw]]" in note
    assert "[00:00–00:10] 原始识别。" in Path(result["transcript_path"]).read_text()
    assert (vault / result["images"][0]).read_bytes() == b"review-image"


def test_review_wikilink_aliases_in_evidence_table_are_escaped(authored_video):
    work, vault, _ = authored_video
    (work / "notes.md").write_text(
        "# Video\n\n## Summary\n\n| Time | Evidence |\n| --- | --- |\n"
        "| 00:00 | [Raw](chapters-transcript.md#Chapter) |\n")
    result = publish(work, vault)
    row = next(line for line in Path(result["note_path"]).read_text().splitlines() if line.startswith("| 00:00"))
    assert "#Chapter\\|Raw]]" in row


def test_review_reference_style_images_are_copied_and_links_are_portable(authored_video):
    work, vault, frame = authored_video
    (work / "notes.md").write_text(
        f"# Video\n\n## Summary\n\n![Frame][selected-frame]\n\n[selected-frame]: {frame}\n")
    result = publish(work, vault)
    note = Path(result["note_path"]).read_text()
    assert "![[08_Attachments/Images/videos/2026-10-25-Review video/01m05s.jpg]]" in note
    assert len(list(vault.rglob("*.jpg"))) == 1
    assert frame not in note


def test_review_inline_image_title_is_supported(authored_video):
    work, vault, frame = authored_video
    (work / "notes.md").write_text(
        f'# Video\n\n## Summary\n\n![Frame]({frame} "实际帧时间 65.25 秒")\n')
    result = publish(work, vault)
    assert len(result["images"]) == 1


def test_review_failed_exports_can_retry_after_storage_recovers(authored_video, monkeypatch):
    work, vault, frame = authored_video
    (work / "notes.md").write_text(f"# Video\n\n## Summary\n\n![Frame]({frame})\n")
    original_open = Path.open

    def storage_full(path, mode="r", *args, **kwargs):
        if mode == "xb" and path.suffix == ".md":
            raise OSError("simulated storage full")
        return original_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", storage_full)
        for _ in range(2):
            with pytest.raises(OSError, match="simulated storage full"):
                publish(work, vault)
    assert not [p for p in vault.rglob("*") if p.is_file()]
    result = publish(work, vault)
    assert Path(result["note_path"]).is_file()


def test_review_references_and_raw_quotes_preserve_existing_user_edits(authored_video):
    work, vault, frame = authored_video
    (work / "notes.md").write_text(
        f"# Video\n\n## Summary\n\n![Frame][shot]\n\n"
        "[External][Docs]\n\n"
        f"[shot]: <{frame}> \"截图说明\"\n"
        '[Docs]: https://example.com/docs "外部资料"\n')
    original_raw = (work / "chapters-transcript.md").read_text()
    quoted_example = ('\n```md\n![sample][shot]\n'
                      '[shot]: materials/frames/example-only.jpg\n```\n'
                      '\n原稿中的 `![literal](materials/frames/not-a-real-image.jpg)` 按原样保留。\n')
    (work / "chapters-transcript.md").write_text(original_raw + quoted_example)
    result = publish(work, vault)
    note_path = Path(result["note_path"])
    assert '[External](https://example.com/docs "外部资料")' in note_path.read_text()
    assert quoted_example in Path(result["transcript_path"]).read_text()
    edited = note_path.read_text().replace('source: "reviewer"', 'source: "reviewer"\ncustom_field: "用户属性"')
    edited += "\n用户后续讨论，不应丢失。\n"
    note_path.write_text(edited)
    before = {path.relative_to(vault): path.read_bytes() for path in vault.rglob("*") if path.is_file()}
    repeat = publish(work, vault)
    assert repeat["existing"] is True
    assert {path.relative_to(vault): path.read_bytes() for path in vault.rglob("*") if path.is_file()} == before
