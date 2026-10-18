import asyncio
import time
from types import SimpleNamespace

from whiskers.cogs.chat import Chat
from whiskers.db import Database
from whiskers.scheduler import Scheduler


def test_mischief_is_only_started_in_home_channels(tmp_path):
    async def go():
        db = Database(tmp_path / "chat.db")
        await db.connect()
        now = time.time()
        for channel_id in (55, 66):  # people talked in both channels a moment ago
            await db.add_message(1, channel_id, channel_id * 10, 8, "Brandon", "brb", now - 60)
        await db.set_channel_rule(1, 55, rule="allow")  # only 55 is a home channel

        asked = []

        async def start_mischief(guild_id, channel_id, level, owner_id):
            asked.append((guild_id, channel_id, level, owner_id))
            return SimpleNamespace(kind="mischief", channel=channel_id)

        channels = {cid: SimpleNamespace(id=cid, category_id=None) for cid in (55, 66)}
        guild = SimpleNamespace(id=1, owner_id=8, get_channel_or_thread=channels.get)
        bot = SimpleNamespace(db=db, engine=SimpleNamespace(ready=True), scheduler=Scheduler(), guilds=[guild],
                              brain=SimpleNamespace(start_mischief=start_mischief))
        cog = Chat.__new__(Chat)
        cog.bot, cog._tasks = bot, set()
        cog._can_send = lambda channel: True
        answered = []

        async def answer(job, channel):
            answered.append(channel.id)

        cog._answer = answer

        await cog.start_mischief()
        await asyncio.sleep(0)
        assert asked == [(1, 55, "sometimes", 8)] and answered == [55]

        await db.update_guild_settings(1, mischief="off")
        await cog.start_mischief()
        assert len(asked) == 1  # switched off

        await db.update_guild_settings(1, mischief="often")
        async with bot.scheduler.reply_turn():  # busy answering someone: not now
            await cog.start_mischief()
        assert len(asked) == 1
        await db.close()

    asyncio.run(go())
