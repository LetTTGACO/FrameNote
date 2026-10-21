# Repository guidelines

Use this guide when changing FrameNote code or documentation. It identifies the
canonical files, implementation boundaries, and checks you need before handing
off a change. For service setup, see the [deployment guide](docs/DEPLOYMENT.md).

## Find the relevant code

The repository separates runtime code, the client workflow, and operations:

- `framenote/` contains the FastAPI service, serial jobs, media acquisition,
  persistence, and frame extraction.
- `framenote/asr/` contains automatic speech recognition (ASR) contracts,
  adapters, and the model lock that pins downloadable assets.
- `skills/framenote/` contains the canonical Skill and standard-library client.
  Edit these files when changing the workflow. See the
  [Skill installation instructions](README.md#安装并配置-skill) for project entrypoints.
- `tests/` mirrors runtime responsibilities; put shared fixtures in
  `tests/conftest.py`.
- `docs/` describes contracts and operations; `deploy/` contains environment and
  systemd templates. Generated media and notes belong in ignored `workspace/`.

## Set up a development environment

Install Python 3.10–3.12, `ffmpeg`, `ffprobe`, `curl`, and `libsndfile` before
starting. From the repository root, complete these steps:

1. Create a virtual environment:

   ```bash
   python3 -m venv .venv
   ```

2. Activate it:

   ```bash
   . .venv/bin/activate
   ```

3. Install the development dependencies:

   ```bash
   python -m pip install -e '.[dev]'
   ```

4. Run the behavior tests to confirm the environment works:

   ```bash
   python -m pytest -q
   ```

   The two optional distribution tests are skipped unless explicitly enabled.

For real inference, also install `'.[asr]'`, prepare and verify the models, and
load the service configuration as described in the
[deployment guide](docs/DEPLOYMENT.md). Start one worker with:

```bash
python -m uvicorn framenote.service:app \
  --host 127.0.0.1 --port 18121 --workers 1 --no-proxy-headers
```

Use a separate port and workspace when another instance is running. Confirm
that `/api/ready` returns HTTP `200` before running inference.

## Preserve implementation boundaries

Use four-space indentation, `snake_case` for functions and modules,
`PascalCase` for classes, and typed public interfaces. Match the surrounding
formatting; the project has no configured formatter.

Keep `Pipeline` backend-neutral. Before extending ASR, read the
[ASR contract](docs/ASR.md), implement the adapter, and register it through
`asr.create_backend`. Load heavy dependencies inside the adapter or isolated
worker.

Keep `service.env`, `.framenote.json`, models, and task materials untracked.
Client defaults live in `~/.config/framenote/client.json`; project overrides
follow the execution directory, independently of Skill installation.
Before deployment, follow the [deployment guide](docs/DEPLOYMENT.md) and
preserve existing tasks.

## Verify changes

Use pytest and pytest-asyncio, name test files `test_*.py`, and use temporary
directories and fake backends for offline checks. Cover the behavior you change,
especially timestamps, cancellation cleanup, legacy compatibility, and raw-file
preservation.

Run the relevant checks from the activated environment:

```bash
python -m pytest -q
python -m mypy framenote
python -m build
```

Confirm that tests and type checks pass and that the build produces a wheel and
source distribution in `dist/`. For packaging changes, enable the distribution
checks as follows:

1. Populate a wheel cache:

   ```bash
   python -m pip download --dest /tmp/framenote-wheels .
   ```

2. Run the distribution tests:

   ```bash
   FRAMENOTE_RUN_PACKAGING=1 \
     FRAMENOTE_WHEELHOUSE=/tmp/framenote-wheels \
     python -m pytest tests/test_distribution.py -q
   ```

   Confirm that both tests pass. They check archive contents and installation
   outside the source directory.

CI runs checks on Python 3.10 and 3.12; Python 3.12 also runs the distribution
checks. Real ASR smoke tests supplement CI. Record their environment and results
separately, following the [native validation record](docs/VALIDATION-NATIVE.md).

## Prepare the handoff

Use concise imperative commit subjects with the existing `feat:`, `fix:`,
`docs:`, or `refactor:` prefixes. Add a scope when useful, such as `fix(deploy):`.

In a pull request, explain the problem, resulting behavior, validation, and
relevant issues. When generated output changes, include sanitized examples or
screenshots. Report which checks ran and any validation still needed.
