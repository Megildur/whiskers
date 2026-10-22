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


def test_the_reply_quotes_the_message_it_actually_answers():
    from whiskers.brain import Incoming, ReplyJob

    async def go():
        sent = []

        class Channel:
            async def send(self, text, reference=None, allowed_mentions=None):
                sent.append(reference.message_id if reference else None)
                return SimpleNamespace(id=900)

        def message(message_id, author):
            return Incoming(message_id, 1, 10, author, f"user{author}", "*pets him*", 1000.0)

        job = ReplyJob("home", message(1, 3), (10, 3))

        async def respond(job, typing, deliver, react=None):
            job.message = message(2, 3)  # they wrote again before the reply started
            await deliver("*purrs*")

        bot = SimpleNamespace(db=SimpleNamespace(guild_settings=lambda guild_id: asyncio.sleep(0, SimpleNamespace(
                                  ping_on_reply=False, reactions=True))),
                              brain=SimpleNamespace(respond=respond, last_message_id=lambda channel_id: 5))
        cog = Chat.__new__(Chat)
        cog.bot = bot
        await cog._answer(job, Channel())
        assert sent == [2]  # the newest message it answers, not the one the reply was first queued for

    asyncio.run(go())


def test_an_emoji_for_how_he_felt_goes_on_the_message_he_answered(caplog):
    import discord

    from whiskers.brain import Incoming, ReplyJob

    async def go():
        added = []

        class Partial:
            def __init__(self, message_id, refuse):
                self.id, self.refuse, self.channel = message_id, refuse, SimpleNamespace(id=10)

            async def add_reaction(self, emoji):
                if self.refuse:
                    raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
                added.append((self.id, emoji))

        class Channel:
            refuse = False

            async def send(self, text, reference=None, allowed_mentions=None):
                return SimpleNamespace(id=900)

            def get_partial_message(self, message_id):
                return Partial(message_id, self.refuse)

        job = ReplyJob("home", Incoming(1, 1, 10, 3, "user3", "*pets him*", 1000.0), (10, 3))

        async def respond(job, typing, deliver, react=None):
            await deliver("*purrs*")
            await react(job.message.message_id, "😻")

        bot = SimpleNamespace(db=SimpleNamespace(guild_settings=lambda guild_id: asyncio.sleep(0, SimpleNamespace(
                                  ping_on_reply=False, reactions=True))),
                              brain=SimpleNamespace(respond=respond, last_message_id=lambda channel_id: 1))
        cog = Chat.__new__(Chat)
        cog.bot, cog._cant_react = bot, set()
        channel = Channel()
        await cog._answer(job, channel)
        assert added == [(1, "😻")]
        channel.refuse = True  # no permission to react: said once in the log, not on every message
        await cog._answer(job, channel)
        await cog._answer(job, channel)
        assert sum("Can't add emoji reactions" in r.message for r in caplog.records) == 1

    asyncio.run(go())
