"""Embed source identity in both archives; project metadata lives in pyproject.toml."""
import json
from pathlib import Path
import sys

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from framenote.version import build_info

IDENTITY = build_info(ROOT)


def write_identity(directory):
    path = Path(directory) / "framenote" / "_build_info.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(IDENTITY, sort_keys=True) + "\n", encoding="utf-8")


class BuildPy(build_py):
    def run(self):
        super().run()
        write_identity(self.build_lib)


class Sdist(sdist):
    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        write_identity(base_dir)


setup(cmdclass={"build_py": BuildPy, "sdist": Sdist})
