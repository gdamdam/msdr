"""Fire-and-forget asyncio tasks that stay referenced until they finish.

The event loop keeps only weak references to tasks, so a task created and
immediately dropped can be garbage-collected mid-flight (ruff RUF006).
"""

import asyncio
from collections.abc import Coroutine

_BACKGROUND: set[asyncio.Task] = set()


def spawn(coro: Coroutine) -> asyncio.Task:
    """Schedule `coro` and hold a reference until it completes."""
    task = asyncio.ensure_future(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task
