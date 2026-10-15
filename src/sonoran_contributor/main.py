"""`sonoran-contributor`: the RF agent, the mesh jobs, or both.

    SOURCES set    -> the RF agent runs (rf_agent.agent), as `rf-agent` did
    MESH_NODE set  -> mesh jobs run (sonoran_contributor.worker)

Both use the same station, source and broker login. With neither set
there is nothing to do, and it says so.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys

from rf_agent import agent as rf
from rf_agent.config import ConfigError as RfConfigError
from rf_agent.config import load as load_rf
from sonoran_contributor.config import ConfigError, load_mesh, mesh_enabled


async def _main() -> int:
    env = os.environ
    tasks: list[asyncio.Task[None]] = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    level = env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if env.get("SOURCES", "").strip():
        try:
            rf_config = load_rf(env)
        except RfConfigError as exc:
            print(f"sonoran-contributor: {exc}", file=sys.stderr)
            return 2
        tasks.append(asyncio.create_task(rf.main(rf_config, stop), name="rf"))
    if mesh_enabled(env):
        try:
            mesh_config = load_mesh(env)
        except ConfigError as exc:
            print(f"sonoran-contributor: {exc}", file=sys.stderr)
            return 2
        from sonoran_contributor import worker

        tasks.append(asyncio.create_task(worker.run(mesh_config, stop), name="mesh"))
    if not tasks:
        print(
            "sonoran-contributor: nothing to do. Set SOURCES for the RF agent, "
            "MESH_NODE for mesh jobs, or both.",
            file=sys.stderr,
        )
        return 2
    await asyncio.gather(*tasks)
    return 0


def run() -> None:
    """Console entry point, `sonoran-contributor`."""
    sys.exit(asyncio.run(_main()))
