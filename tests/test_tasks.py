"""msdr.tasks.spawn: fire-and-forget tasks stay referenced (RUF006)."""

import asyncio

from msdr import tasks


async def test_spawn_holds_reference_until_done():
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return 42

    task = tasks.spawn(work())
    assert task in tasks._BACKGROUND
    gate.set()
    assert await task == 42
    await asyncio.sleep(0)  # let the done callback run
    assert task not in tasks._BACKGROUND
