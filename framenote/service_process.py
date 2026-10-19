"""Bounded subprocess execution with cancellation and child reaping."""
from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Callable

from .task_store import public_error


async def run_process(args: list[str], *, timeout: float = 7200,
                      log: Path | None = None, progress: Callable[[str], None] | None = None,
                      env: dict[str, str] | None = None) -> bytes:
    with (log.open("ab") if log else open(os.devnull, "wb")) as errors:
        process = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=errors, start_new_session=True, env=env,
        )

        async def consume() -> bytes:
            assert process.stdout is not None
            data = bytearray()
            while line := await process.stdout.readline():
                data.extend(line)
                if len(data) > 4 * 1024**2:
                    raise RuntimeError("Subprocess output exceeded 4 MiB")
                if progress:
                    progress(line.decode("utf-8", errors="replace"))
            await process.wait()
            if process.returncode:
                detail = ""
                if log and log.is_file():
                    with log.open("rb") as stream:
                        stream.seek(max(0, log.stat().st_size - 1200))
                        detail = public_error(RuntimeError(stream.read().decode("utf-8", errors="replace")))
                raise RuntimeError(f"{Path(args[0]).name} exited with code {process.returncode}: {detail or 'see task process log'}")
            return bytes(data)

        try:
            return await asyncio.wait_for(consume(), timeout=timeout)
        finally:
            # A leader can exit before descendants; stop the whole owned group.
            def signal_group(value: int) -> bool:
                try:
                    os.killpg(process.pid, value)
                    return True
                except ProcessLookupError:
                    return False

            alive = signal_group(signal.SIGTERM)
            deadline = asyncio.get_running_loop().time() + 3
            while alive and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(.05)
                alive = signal_group(0)
            if alive:
                signal_group(signal.SIGKILL)
            await process.wait()
