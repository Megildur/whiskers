import asyncio

from whiskers.scheduler import Scheduler


def test_reply_interrupts_a_background_chore():
    async def go():
        scheduler = Scheduler()
        chore_started = asyncio.Event()

        async def slow_chore():
            chore_started.set()
            await asyncio.sleep(10)

        chore = asyncio.create_task(scheduler.run_chore(slow_chore))
        await chore_started.wait()
        async with scheduler.reply_turn():
            assert scheduler.pending_replies == 1
        assert await chore is False  # interrupted, to be retried later

    asyncio.run(go())


def test_chores_wait_while_replies_are_pending():
    async def go():
        scheduler = Scheduler()
        ran = []

        async def chore():
            ran.append(True)

        async with scheduler.reply_turn():
            assert await scheduler.run_chore(chore) is False
        assert await scheduler.run_chore(chore) is True
        assert ran == [True]

    asyncio.run(go())


def test_replies_take_turns():
    async def go():
        scheduler = Scheduler()
        active = 0
        peak = 0

        async def reply():
            nonlocal active, peak
            async with scheduler.reply_turn():
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(*(reply() for _ in range(5)))
        assert peak == 1

    asyncio.run(go())
