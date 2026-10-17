"""Listens to Discord messages and hands them to the brain."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

import discord
from discord.ext import commands

from ..brain import Incoming, ReplyJob, chattiness_for, is_allowed, rule_for
from ..db import GuildSettings
from ..llm import LLMError
from ..text import person_name, readable, split_message

if TYPE_CHECKING:
    from ..bot import WhiskersBot

log = logging.getLogger("whiskers.chat")

CHAT_MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)


def channel_chain(channel: discord.abc.Messageable) -> list[int]:
    """The channel, its parent channel (for threads) and its category: where rules are looked up."""
    chain = [channel.id]
    base = channel
    if isinstance(channel, discord.Thread):
        chain.append(channel.parent_id)
        base = channel.parent
    category_id = getattr(base, "category_id", None)
    if category_id:
        chain.append(category_id)
    return chain


@asynccontextmanager
async def typing_indicator(channel: discord.abc.Messageable) -> AsyncIterator[None]:
    """Show "Whiskers is typing..." without failing the reply if Discord refuses."""
    typing = channel.typing()
    try:
        await typing.__aenter__()
    except discord.HTTPException:
        typing = None
    try:
        yield
    finally:
        if typing is not None:
            await typing.__aexit__(None, None, None)


class Chat(commands.Cog):
    def __init__(self, bot: WhiskersBot) -> None:
        self.bot = bot
        self._tasks: set[asyncio.Task] = set()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self.bot.brain.user_id = self.bot.user.id
        for guild in self.bot.guilds:
            settings = await self.bot.db.guild_settings(guild.id)
            self.bot.brain.set_owner(guild.id, settings.owner_id or guild.owner_id or 0)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id or message.type not in CHAT_MESSAGE_TYPES:
            return
        bot = self.bot
        home = False
        if message.guild is None:
            if not bot.cfg.allow_dms:
                return
            settings, chattiness = GuildSettings(), 0
        else:
            settings = await bot.db.guild_settings(message.guild.id)
            rules = await bot.db.channel_rules(message.guild.id)
            chain = channel_chain(message.channel)
            if not is_allowed(settings, rules, chain):
                return  # Whiskers doesn't live here, so it doesn't read or remember this channel
            chattiness = chattiness_for(settings, rules, chain)
            home = rule_for(rules, chain) == "allow"  # its home channels: it answers every message

        incoming = self._incoming(message)
        if incoming is None:
            return
        if message.guild is not None:
            incoming.owner_id = settings.owner_id or message.guild.owner_id or 0
        decision = await bot.brain.observe(incoming, settings, chattiness, home=home)
        if decision.reaction:
            await self._react(message, decision.reaction)
        if not decision.reply or not self._can_send(message.channel):
            return
        # A home channel's waiting reply just takes in the new message, so it never counts as too busy.
        if not bot.engine.ready or (decision.reply != "home" and bot.brain.too_busy(decision.reply)):
            if decision.reply == "direct":
                await self._react(message, "💤")  # asleep (still loading) or too many people waiting
            return
        job = bot.brain.submit(decision.reply, incoming)
        if job:
            task = asyncio.create_task(self._answer(job, message.channel))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _incoming(self, message: discord.Message) -> Incoming | None:
        content = readable(message.clean_content)
        extras = []
        for attachment in message.attachments:
            content_type = attachment.content_type or ""
            if content_type == "image/gif" or attachment.filename.lower().endswith(".gif"):
                extras.append("[sends a gif]")
                continue
            extras.append({"image": "[sends a picture]", "video": "[sends a video]", "audio": "[sends a voice clip]"}
                          .get(content_type.split("/")[0], "[sends a file]"))
        extras += [f"[sends a sticker: {sticker.name}]" for sticker in message.stickers]
        if not content and message.embeds:
            extras.append("[shares a link]")
        text = " ".join([content, *extras]).strip()
        if not text:
            return None
        me = self.bot.user
        mentions_me = me in message.mentions or any(
            role.tags is not None and role.tags.bot_id == me.id for role in message.role_mentions
        )
        replied = message.reference.resolved if message.reference else None
        replies_to_me = isinstance(replied, discord.Message) and replied.author.id == me.id
        return Incoming(
            message_id=message.id,
            guild_id=message.guild.id if message.guild else 0,
            channel_id=message.channel.id,
            author_id=message.author.id,
            author_name=person_name(message.author.display_name, message.author.name),
            content=text[:1000],
            created_at=message.created_at.timestamp(),
            mentions_me=mentions_me,
            replies_to_me=replies_to_me,
            is_dm=message.guild is None,
        )

    def _can_send(self, channel: discord.abc.Messageable) -> bool:
        if isinstance(channel, (discord.DMChannel, discord.GroupChannel)):
            return True
        permissions = channel.permissions_for(channel.guild.me)
        if isinstance(channel, discord.Thread):
            return permissions.send_messages_in_threads
        return permissions.send_messages

    async def _react(self, message: discord.Message, emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass

    async def _answer(self, job: ReplyJob, channel: discord.abc.Messageable) -> None:
        msg = job.message
        settings = await self.bot.db.guild_settings(msg.guild_id) if msg.guild_id else GuildSettings()

        async def deliver(text: str) -> int | None:
            reference = None
            if job.kind == "direct" or self.bot.brain.last_message_id(msg.channel_id) != msg.message_id:
                reference = discord.MessageReference(
                    message_id=msg.message_id, channel_id=msg.channel_id, guild_id=msg.guild_id or None,
                    fail_if_not_exists=False,
                )
            allowed = discord.AllowedMentions(everyone=False, roles=False, users=False,
                                              replied_user=settings.ping_on_reply)
            first: int | None = None
            for chunk in split_message(text):
                try:
                    sent = await channel.send(chunk, reference=reference if first is None else None,
                                              allowed_mentions=allowed)
                except discord.HTTPException as error:
                    log.warning("Couldn't send a reply in %s: %s", msg.channel_id, error)
                    break
                first = first or sent.id
            return first

        try:
            await self.bot.brain.respond(job, typing=lambda: typing_indicator(channel), deliver=deliver)
        except LLMError as error:
            log.warning("Couldn't think of a reply: %s", error)
        except Exception:
            log.exception("Replying failed")

    # Keep the stored chat in line with Discord when people edit or delete messages.

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self.bot.db.delete_messages([payload.message_id])

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        await self.bot.db.delete_messages(list(payload.message_ids))

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        message = getattr(payload, "message", None)
        content = message.clean_content if isinstance(message, discord.Message) else payload.data.get("content")
        if content:
            await self.bot.db.update_message(payload.message_id, readable(content))


async def setup(bot: WhiskersBot) -> None:
    await bot.add_cog(Chat(bot))
