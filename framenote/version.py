"""Release identity without importing service or inference dependencies."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any

from . import __version__

MATERIAL_SCHEMAS_READ = [1, 2, 3]
MATERIAL_SCHEMAS_WRITE = [2, 3]


def build_info(root: Path | None = None) -> dict[str, Any]:
    """Prefer the exact checkout; installed wheels use embedded build identity."""
    root = (root or Path(__file__).resolve().parent.parent).resolve()
    try:
        def git(*args: str) -> str:
            return subprocess.run(["git", "-C", str(root), *args], check=True,
                                  capture_output=True, text=True, timeout=3).stdout.strip()

        if Path(git("rev-parse", "--show-toplevel")).resolve() == root:
            commit = git("rev-parse", "HEAD")
            if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
                return {"version": __version__, "commit": commit,
                        "dirty": bool(git("status", "--porcelain", "--untracked-files=normal"))}
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        value = json.loads((root / "framenote" / "_build_info.json").read_text(encoding="utf-8"))
        if (isinstance(value, dict) and value.get("version") == __version__
                and isinstance(value.get("commit"), str)
                and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value["commit"])
                and type(value.get("dirty")) is bool):
            return {key: value[key] for key in ("version", "commit", "dirty")}
    except (OSError, ValueError, TypeError):
        pass
    return {"version": __version__, "commit": None, "dirty": None}


def main() -> None:
    print(json.dumps(build_info(), sort_keys=True))


if __name__ == "__main__":
    main()
