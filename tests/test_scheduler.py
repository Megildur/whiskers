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


def test_real_replies_go_ahead_of_a_running_simulation():
    async def go():
        scheduler = Scheduler()
        order = []

        async def simulation():
            for i in range(3):
                async with scheduler.background_turn():
                    order.append(f"sim {i}")
                    await asyncio.sleep(0.05)

        async def person():
            await asyncio.sleep(0.02)  # arrives while the first simulation reply is being written
            async with scheduler.reply_turn():
                order.append("real")

        await asyncio.gather(simulation(), person())
        assert order == ["sim 0", "real", "sim 1", "sim 2"]  # waits for one simulation reply at most

    asyncio.run(go())


def test_a_put_off_chore_can_take_its_turn_and_finish():
    async def go():
        scheduler = Scheduler()
        order = []
        started = asyncio.Event()

        async def journal():
            started.set()
            order.append("journal starts")
            await asyncio.sleep(0.05)
            order.append("journal done")

        async def reply():
            await started.wait()
            async with scheduler.reply_turn():
                order.append("reply")

        chore = asyncio.create_task(scheduler.run_chore(journal, take_turn=True))
        await asyncio.gather(reply(), chore)
        assert chore.result() is True
        assert order == ["journal starts", "journal done", "reply"]  # not interrupted: the reply waited

    asyncio.run(go())
