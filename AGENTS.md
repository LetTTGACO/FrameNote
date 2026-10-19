# Repository Guidelines

## Project Structure & Module Organization

- `framenote/` contains the FastAPI service, MCP tools, serial jobs, media acquisition, persistence, and frame extraction. ASR contracts and adapters live in `framenote/asr/`; its model lock pins downloadable assets.
- `tests/` mirrors these responsibilities; shared fixtures belong in `tests/conftest.py`.
- `skills/framenote/` contains the canonical Skill and standard-library client. `.agents/skills/framenote` links to it; edit the canonical files.
- `docs/` documents contracts and operations; `deploy/` contains environment and systemd templates. Generated media and notes belong in ignored `workspace/`.

## Build, Test, and Development Commands

Use Python 3.10–3.12 with ffmpeg/ffprobe, curl, and libsndfile installed.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q       # Behavior tests
python -m mypy framenote  # Type checks
python -m build          # Wheel and source distribution
```

For real inference, install `'.[asr]'` and prepare/verify models following `README.md`, then run:

```bash
python -m uvicorn framenote.service:app --host 127.0.0.1 --port 18121 --workers 1 --no-proxy-headers
```

Use a separate port and workspace when another instance is running.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions/modules, `PascalCase` classes, and typed public interfaces. Match surrounding formatting; no formatter is configured. Keep `Pipeline` backend-neutral and register adapters through `asr.create_backend`; consult `docs/ASR.md` when extending ASR.

## Testing Guidelines

Use pytest and pytest-asyncio; name files `test_*.py` and functions `test_*`. Cover changed behavior, especially timestamps, cancellation cleanup, legacy compatibility, and raw-file preservation. Use temporary directories and fake backends for offline tests.

For packaging changes, populate a wheelhouse and enable distribution checks:

```bash
python -m pip download --dest /tmp/framenote-wheels .
FRAMENOTE_RUN_PACKAGING=1 FRAMENOTE_WHEELHOUSE=/tmp/framenote-wheels python -m pytest tests/test_distribution.py -q
```

CI checks Python 3.10/3.12; real ASR smoke tests supplement CI.

## Commit & Pull Request Guidelines

Use concise imperative subjects with existing prefixes: `feat:`, `fix:`, `docs:`, or `refactor:`; optional scopes include `fix(deploy):`. PRs should explain the problem, resulting behavior, verification, and relevant issues. Include sanitized examples or screenshots when generated output changes.

## Configuration & Deployment

Keep `service.env`, models, and task materials untracked. Before deployment, follow `docs/DEPLOYMENT.md` and preserve existing tasks.
