import json
import stat

import pytest

from framenote.credentials import (
    clear_credentials, credential_status, import_credentials,
    load_credentials, main,
)


def source(tmp_path, data=None):
    path = tmp_path / 'input.json'
    path.write_text(json.dumps(data if data is not None else {'SESSDATA': 'secret', 'buvid3': 'device', 'junk': 'ignored'}))
    return path


def test_private_import_is_atomic_and_public_status_has_no_secret(tmp_path, monkeypatch):
    tasks = tmp_path / 'tasks'
    target = tmp_path / 'private' / 'bilibili.json'
    input_path = source(tmp_path)
    import_credentials(input_path, target, forbidden_root=tasks)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    credentials, state = load_credentials(target, forbidden_root=tasks)
    assert state == 'loaded'
    assert credentials.cookies == {'SESSDATA': 'secret', 'buvid3': 'device'}
    assert 'secret' not in repr(credentials)
    status = credential_status(target, forbidden_root=tasks)
    assert status['configured'] and status['usable']
    assert status['fields']['SESSDATA'] and not status['fields']['bili_jct']
    assert 'secret' not in json.dumps(status)
    before = target.read_bytes()
    monkeypatch.setattr('framenote.credentials.os.replace', lambda *args: (_ for _ in ()).throw(OSError('interrupted')))
    with pytest.raises((OSError, ValueError)):
        import_credentials(source(tmp_path, {'sessdata': 'replacement'}), target, forbidden_root=tasks)
    assert target.read_bytes() == before
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize('kind', ['symlink', 'world_readable', 'group_readable', 'task_directory', 'linked_parent'])
def test_load_rejects_symlink_world_readable_and_task_directory(tmp_path, kind):
    tasks = tmp_path / 'tasks'
    parent = tasks if kind == 'task_directory' else tmp_path / 'private'
    parent.mkdir(mode=0o700)
    target = parent / 'bilibili.json'
    target.write_text('{"SESSDATA": "secret"}')
    target.chmod(0o600)
    if kind == 'symlink':
        target = parent / 'linked.json'
        target.symlink_to(parent / 'bilibili.json')
    elif kind == 'linked_parent':
        linked = tmp_path / 'linked'
        linked.symlink_to(parent, target_is_directory=True)
        target = linked / 'bilibili.json'
    elif kind == 'world_readable':
        target.chmod(0o604)
    elif kind == 'group_readable':
        target.chmod(0o640)
    assert load_credentials(target, forbidden_root=tasks) == (None, 'invalid')
    assert credential_status(target, forbidden_root=tasks)['usable'] is False


def test_import_refuses_to_chmod_existing_shared_parent(tmp_path):
    parent = tmp_path / 'shared'
    parent.mkdir(mode=0o755)
    with pytest.raises(ValueError):
        import_credentials(source(tmp_path), parent / 'credentials.json', forbidden_root=tmp_path / 'tasks')
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert not list(parent.iterdir())


@pytest.mark.parametrize('data', [{'SESSDATA': 'secret\r\nInjected: yes'}, {'sessdata': 'secret\t'}, {'bili_jct': 123}, {}, []])
def test_import_rejects_invalid_format_without_secret(tmp_path, data):
    target = tmp_path / 'private' / 'credentials.json'
    with pytest.raises(ValueError) as error:
        import_credentials(source(tmp_path, data), target, forbidden_root=tmp_path / 'tasks')
    assert 'secret' not in str(error.value)
    assert not target.exists()


def test_missing_credentials_and_clear_only_specified_file(tmp_path):
    tasks = tmp_path / 'tasks'
    assert load_credentials(None, forbidden_root=tasks) == (None, 'missing')
    target = tmp_path / 'private' / 'credentials.json'
    assert load_credentials(target, forbidden_root=tasks) == (None, 'missing')
    import_credentials(source(tmp_path), target, forbidden_root=tasks)
    neighbor = target.parent / 'other.json'
    neighbor.write_text('keep')
    clear_credentials(target, forbidden_root=tasks)
    clear_credentials(target, forbidden_root=tasks)
    assert neighbor.read_text() == 'keep'
    assert target.parent.is_dir()


def test_import_and_clear_reject_task_paths_and_symlinks(tmp_path):
    tasks = tmp_path / 'tasks'
    tasks.mkdir(mode=0o700)
    target = tasks / 'credentials.json'
    with pytest.raises(ValueError):
        import_credentials(source(tmp_path), target, forbidden_root=tasks)
    original = source(tmp_path)
    link = tasks / 'link.json'
    link.symlink_to(original)
    with pytest.raises(ValueError):
        clear_credentials(link, forbidden_root=tmp_path / 'other')
    assert original.exists() and link.is_symlink()


def test_cli_import_status_clear_is_local_and_redacted(tmp_path, monkeypatch, capsys):
    input_path = source(tmp_path)
    target = tmp_path / 'private' / 'credentials.json'
    monkeypatch.setenv('FRAMENOTE_WORKSPACE', str(tmp_path / 'workspace'))
    for command, extra in [('import', ['--input', str(input_path)]), ('status', []), ('clear', [])]:
        monkeypatch.setattr('sys.argv', ['credentials', command, '--file', str(target), *extra])
        assert main() == 0
        output = capsys.readouterr()
        assert 'secret' not in output.out + output.err
        if command == 'status':
            assert json.loads(output.out)['usable'] is True
    assert not target.exists()


def test_atomic_reimport_is_visible_on_next_load(tmp_path):
    tasks = tmp_path / 'tasks'
    target = tmp_path / 'private' / 'bilibili.json'
    import_credentials(source(tmp_path), target, forbidden_root=tasks)
    import_credentials(source(tmp_path, {'sessdata': 'updated'}), target, forbidden_root=tasks)
    credentials, state = load_credentials(target, forbidden_root=tasks)
    assert state == 'loaded' and credentials.cookies == {'SESSDATA': 'updated'}
    target.write_text('not JSON')
    assert load_credentials(target, forbidden_root=tasks) == (None, 'invalid')
