"""Archive and verify immutable release bundles; never starts or stops a service."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata, resources
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any
import zipfile

from .version import MATERIAL_SCHEMAS_READ, MATERIAL_SCHEMAS_WRITE, build_info


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def environment_snapshot() -> dict[str, Any]:
    """Only package names/versions, never pip URLs, environment variables or paths."""
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True, capture_output=True, text=True)
    packages: dict[str, str] = {}
    for distribution in metadata.distributions():
        name = distribution.metadata["Name"]
        version = distribution.version
        direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
        if "dir_info" in direct_url or "vcs_info" in direct_url:
            raise ValueError("Release dependencies must be installed from built distributions")
        if not name or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) is None:
            raise ValueError("Invalid installed package name")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+!-]*", version) is None:
            raise ValueError("Invalid installed package version")
        packages[re.sub(r"[-_.]+", "-", name).lower()] = version
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "system": platform.system(), "machine": platform.machine(), "packages": packages}


def archive_release(wheel: Path, source: Path, output: Path) -> dict[str, Any]:
    """Run with the installed release interpreter, from outside the checkout."""
    identity = build_info()
    if identity["dirty"] is not False or not identity["commit"]:
        raise ValueError("Release requires a clean, known source commit")
    version = identity["version"]
    if wheel.name != f"framenote-{version}-py3-none-any.whl" or source.name != f"framenote-{version}.tar.gz":
        raise ValueError("Archive filenames must match the running release version")
    with zipfile.ZipFile(wheel) as archive:
        if json.loads(archive.read("framenote/_build_info.json")) != identity:
            raise ValueError("Wheel identity differs from installed release")
    with tarfile.open(source) as archive:
        entry = archive.extractfile(f"framenote-{version}/framenote/_build_info.json")
        if entry is None or json.load(entry) != identity:
            raise ValueError("Source archive identity differs from installed release")
    environment = environment_snapshot()
    if environment["packages"].get("framenote") != version:
        raise ValueError("Installed package metadata differs from running release")
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("Release bundle already exists; never overwrite a release")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="." + output.name + "-", dir=output.parent))
    try:
        shutil.copyfile(wheel, temporary / wheel.name)
        shutil.copyfile(source, temporary / source.name)
        (temporary / "requirements.lock").write_text(
            "".join(f"{name}=={value}\n" for name, value in sorted(environment["packages"].items())), encoding="utf-8")
        (temporary / "models.lock.json").write_bytes(resources.files("framenote.asr").joinpath("models.lock.json").read_bytes())
        manifest = {"format_version": 1, **identity, "tag": "v" + version,
                    "created_at": datetime.now(timezone.utc).isoformat(), "environment": environment,
                    "material_schemas": {"read": MATERIAL_SCHEMAS_READ, "write": MATERIAL_SCHEMAS_WRITE},
                    "files": {path.name: sha256(path) for path in sorted(temporary.iterdir())}}
        (temporary / "release.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (temporary / "SHA256SUMS").write_text(checksums(temporary), encoding="utf-8")
        verify_release(temporary)
        # mkdir claims the final name exclusively, including empty pre-existing directories.
        output.mkdir()
        for path in temporary.iterdir():
            shutil.move(str(path), output / path.name)
    finally:
        shutil.rmtree(temporary)
    return manifest


def checksums(directory: Path) -> str:
    return "".join(f"{sha256(path)}  {path.name}\n" for path in sorted(directory.iterdir()) if path.name != "SHA256SUMS")


def verify_release(directory: Path) -> dict[str, Any]:
    if any(not path.is_file() or path.is_symlink() for path in directory.iterdir()):
        raise ValueError("Release bundle must contain regular files only")
    manifest = json.loads((directory / "release.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Invalid release manifest")
    files = manifest.get("files")
    if manifest.get("format_version") != 1 or not isinstance(files, dict) or not files:
        raise ValueError("Invalid release manifest")
    for name, digest in files.items():
        if (not isinstance(name, str) or Path(name).name != name or name in {"release.json", "SHA256SUMS"}
                or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            raise ValueError("Invalid release file entry")
        if sha256(directory / name) != digest:
            raise ValueError("Release checksum mismatch: " + name)
    if {path.name for path in directory.iterdir()} != set(files) | {"release.json", "SHA256SUMS"}:
        raise ValueError("Unexpected release bundle files")
    if (directory / "SHA256SUMS").read_text(encoding="utf-8") != checksums(directory):
        raise ValueError("Release manifest checksum mismatch")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    archive = commands.add_parser("archive", help="Archive a clean, installed release and its exact dependency inventory")
    archive.add_argument("--wheel", type=Path, required=True)
    archive.add_argument("--sdist", type=Path, required=True)
    archive.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify", help="Verify bundle contents and SHA-256 checksums")
    verify.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        manifest = (archive_release(args.wheel, args.sdist, args.output) if args.command == "archive"
                    else verify_release(args.directory))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, zipfile.BadZipFile, tarfile.TarError) as error:
        parser.exit(1, f"Release operation failed: {error}\n")
    print(json.dumps({key: manifest[key] for key in ("version", "commit", "tag")}, sort_keys=True))


if __name__ == "__main__":
    main()
