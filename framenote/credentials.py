"""Local-only Bilibili credential management; never expose credential values."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any

from .task_store import atomic_json

COOKIE_NAMES = ('SESSDATA', 'bili_jct', 'buvid3', 'buvid4', 'b_nut', 'b_lsid')


@dataclass(frozen=True)
class BilibiliCredentials:
    cookies: dict[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'cookies', _cookies(self.cookies))


def _cookies(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError('Invalid Bilibili credential format')
    result: dict[str, str] = {}
    for key, item in value.items():
        name = 'SESSDATA' if key == 'sessdata' else key
        if name not in COOKIE_NAMES:
            continue
        if not isinstance(item, str) or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in item) or ';' in item:
            raise ValueError('Invalid Bilibili credential format')
        if item:
            result[name] = item
    if not result:
        raise ValueError('Invalid Bilibili credential format')
    return result


def _private_path(path: Path, forbidden_root: Path) -> Path:
    # Preserve lexical components until after checking them: resolve() hides links.
    path = Path(os.path.abspath(path.expanduser()))
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Credential paths must not use symbolic links')
    resolved = path.resolve()
    if resolved.is_relative_to(forbidden_root.expanduser().resolve()):
        raise ValueError('Credentials must be outside task materials')
    return path


def _private_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('Credentials require a private regular file')


def load_credentials(path: Path | None, *, forbidden_root: Path) -> tuple[BilibiliCredentials | None, str]:
    if path is None:
        return None, 'missing'
    try:
        target = _private_path(path, forbidden_root)
        _private_file(target)
        # O_NOFOLLOW also protects the final component if it changes after lstat.
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError('Credentials require a private regular file')
            return BilibiliCredentials(json.load(stream)), 'loaded'
    except FileNotFoundError:
        return None, 'missing'
    except (OSError, ValueError, UnicodeError):
        return None, 'invalid'


def import_credentials(input_path: Path, target: Path, *, forbidden_root: Path) -> None:
    try:
        credentials = BilibiliCredentials(json.loads(input_path.read_text(encoding='utf-8')))
    except (OSError, ValueError, UnicodeError):
        raise ValueError('Cannot import Bilibili credentials: invalid input') from None
    target = _private_path(target, forbidden_root)
    if target.exists():
        _private_file(target)
    parent = target.parent
    # Only newly created dedicated directories receive private permissions.
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True)
    parent_info = parent.stat()
    if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) != 0o700:
        raise ValueError('Credential directory must already be private (0700)')
    staging = Path(tempfile.mkdtemp(prefix='.bilibili-', dir=parent))
    temporary = staging / 'credentials.json'
    try:
        atomic_json(temporary, credentials.cookies)
        temporary.chmod(0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
        staging.rmdir()


def credential_status(path: Path, *, forbidden_root: Path) -> dict:
    credentials, state = load_credentials(path, forbidden_root=forbidden_root)
    return {'configured': path is not None, 'usable': state == 'loaded',
            'fields': {name: credentials is not None and name in credentials.cookies for name in COOKIE_NAMES}}


def clear_credentials(path: Path, *, forbidden_root: Path) -> None:
    target = _private_path(path, forbidden_root)
    if target.exists():
        _private_file(target)
        target.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description='Manage local private Bilibili credentials')
    subparsers = parser.add_subparsers(dest='command', required=True)
    for command in ('import', 'status', 'clear'):
        subparser = subparsers.add_parser(command)
        subparser.add_argument('--file', type=Path, required=True)
        if command == 'import':
            subparser.add_argument('--input', type=Path, required=True)
    args = parser.parse_args()
    from .service_settings import Settings
    try:
        forbidden_root = Settings.from_env().workspace / 'tasks'
        if args.command == 'import':
            import_credentials(args.input, args.file, forbidden_root=forbidden_root)
        elif args.command == 'clear':
            clear_credentials(args.file, forbidden_root=forbidden_root)
        else:
            print(json.dumps(credential_status(args.file, forbidden_root=forbidden_root)))
    except (OSError, ValueError):
        print('Bilibili credential operation failed: check input, path and private permissions', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
