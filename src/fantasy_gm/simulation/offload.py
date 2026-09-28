"""Worker boundary for CPU-heavy simulation.

Async services must never run simulators on the event loop. ``run_offloop`` submits a pure,
picklable ``simulate(request)`` call to an executor (a ``ProcessPoolExecutor`` in production;
later possibly a remote worker queue) and awaits the result. Determinism is unaffected because
all randomness derives from the request's ``SeedSpec``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Executor


async def run_offloop[Req, Res](
    simulate: Callable[[Req], Res], request: Req, executor: Executor | None = None
) -> Res:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, simulate, request)
