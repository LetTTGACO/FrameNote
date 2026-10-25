import json
from pathlib import Path
import subprocess

import pytest

from framenote import __version__
from framenote.version import build_info


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def test_checkout_identity_tracks_committed_dirty_and_untracked_changes(tmp_path):
    git(tmp_path, "init")
    source = tmp_path / "source.txt"
    source.write_text("committed")
    git(tmp_path, "add", "source.txt")
    git(tmp_path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    expected = {"version": __version__, "commit": git(tmp_path, "rev-parse", "HEAD"), "dirty": False}
    assert build_info(tmp_path) == expected
    source.write_text("modified")
    assert build_info(tmp_path)["dirty"] is True
    source.write_text("committed")
    (tmp_path / "untracked.py").write_text("new")
    assert build_info(tmp_path)["dirty"] is True


def test_installed_identity_survives_an_unrelated_parent_repository(tmp_path):
    git(tmp_path, "init")
    root = tmp_path / "installed"
    (root / "framenote").mkdir(parents=True)
    identity = {"version": __version__, "commit": "a" * 40, "dirty": False}
    (root / "framenote/_build_info.json").write_text(json.dumps(identity))
    assert build_info(root) == identity


@pytest.mark.parametrize("value", [None, [], {"version": "wrong", "commit": "a" * 40, "dirty": False},
                                   {"version": __version__, "commit": "a" * 40, "dirty": "false"}])
def test_missing_or_invalid_identity_is_unknown_not_a_fabricated_commit(tmp_path, value):
    (tmp_path / "framenote").mkdir()
    if value is not None:
        (tmp_path / "framenote/_build_info.json").write_text(json.dumps(value))
    assert build_info(tmp_path) == {"version": __version__, "commit": None, "dirty": None}
