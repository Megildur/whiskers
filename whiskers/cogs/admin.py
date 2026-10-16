"""/whiskers: server setup for people with Manage Server."""
from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import TYPE_CHECKING, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from ..llm import LLMError
from ..simulate import PACKS, SCENES, Step, count_replies, run_scenes, scenes_for
from ..text import person_name
from ..ui import ask_to_confirm, embed

if TYPE_CHECKING:
    from ..bot import WhiskersBot

ChannelTarget = Union[
    discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel,
    discord.CategoryChannel, discord.Thread,
]
log = logging.getLogger("whiskers.admin")

MODE_NAMES = {"allowlist": "only in allowed channels", "everywhere": "everywhere except blocked channels"}
SPEECH_NAMES = {False: "cat sounds and actions only", True: "has learned to communicate"}


def _on(value: bool) -> str:
    return "on" if value else "off"


def _duration(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


class SimulationProgress:
    """Keeps a public message up to date while /whiskers simulate runs.

    The command's own reply is edited for the first 14 minutes; Discord stops allowing that at 15, so a
    longer run continues in a normal channel message.
    """

    REPLY_EDIT_LIMIT = 14 * 60

    def __init__(self, interaction: discord.Interaction, total: int, scenes: int) -> None:
        self.interaction = interaction
        self.total = total
        self.scenes = scenes
        self.done = 0
        self.started = time.monotonic()
        self.last: Step | None = None
        self.channel_message: discord.Message | None = None
        self.reply_started = time.monotonic()
        self.finished = False
        self._ticker: asyncio.Task | None = None

    def start(self) -> None:
        """Refresh the message every 15 seconds while a reply is being written, so it's clearly alive."""
        async def tick() -> None:
            while not self.finished:
                await asyncio.sleep(15)
                if not self.finished:
                    await self._show(self.text())
        self._ticker = asyncio.create_task(tick())

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def text(self, headline: str | None = None) -> str:
        filled = round(20 * self.done / self.total) if self.total else 20
        bar = "▓" * filled + "░" * (20 - filled)
        if headline is None:
            where = (f" · scene {self.last.scene_number} of {self.scenes}: **{self.last.scene}**"
                     if self.last else "")
            headline = f"🧪 **Simulation running**{where}"
            if self.done:
                left = self.elapsed() / self.done * (self.total - self.done)
                eta = f" · about {_duration(left)} left"
            else:
                eta = " · warming up"
            headline += f"\n`{bar}` {self.done}/{self.total} replies{eta}"
            waited = time.monotonic() - self.reply_started
            headline += f"\n⏳ writing reply {self.done + 1}/{self.total}… {waited:.0f}s"
        parts = [headline]
        if self.last:
            parts.append(
                f"> **{self.last.speaker}:** {self.last.said[:200]}\n"
                f"> -# scene: {(self.last.note or '-')[:200]}\n"
                f"> **Whiskers:** {self.last.reply[:600]}  ({self.last.seconds:.0f}s)"
            )
        return "\n".join(parts)[:1990]

    async def _show(self, content: str) -> None:
        try:
            if self.elapsed() < self.REPLY_EDIT_LIMIT:
                await self.interaction.edit_original_response(content=content)
            elif self.channel_message is None:
                self.channel_message = await self.interaction.channel.send(content)
            else:
                await self.channel_message.edit(content=content)
        except discord.HTTPException as error:
            log.warning("Couldn't update the simulation progress: %s", error)

    async def update(self, step: Step) -> None:
        self.done, self.last = step.done, step
        self.reply_started = time.monotonic()
        await self._show(self.text())

    async def finish(self, headline: str, lines: list[str]) -> None:
        self.finished = True
        if self._ticker:
            self._ticker.cancel()
        await self._show(self.text(headline))
        if not lines:
            return
        user = self.interaction.user
        note = (f"{user.mention} here's the transcript. Each reply shows Whiskers' private scene note, so you can "
                "see what it understood.")
        mention = discord.AllowedMentions(users=[user])
        transcript = "\n".join(lines)
        file = discord.File(io.BytesIO(transcript.encode()), filename="whiskers-simulation.txt")
        try:
            if self.elapsed() < self.REPLY_EDIT_LIMIT:
                await self.interaction.followup.send(note, file=file, allowed_mentions=mention)
            else:
                await self.interaction.channel.send(note, file=file, allowed_mentions=mention)
            return
        except discord.HTTPException as error:
            log.warning("Couldn't post the transcript file (%s); posting it as text.", error)
        try:  # no permission to attach files: post the text instead
            for start in range(0, len(transcript), 1900):
                await self.interaction.channel.send(f"```\n{transcript[start:start + 1900]}\n```")
        except discord.HTTPException as error:
            log.warning("Couldn't post the transcript: %s", error)


class JournalEditor(discord.ui.Modal, title="Edit Whiskers' journal"):
    text = discord.ui.TextInput(
        label="One line per fact, written as the cat", style=discord.TextStyle.paragraph, max_length=1500,
    )

    def __init__(self, bot: WhiskersBot, current: str) -> None:
        super().__init__()
        self.bot = bot
        self.text.default = current

    async def on_submit(self, interaction: discord.Interaction) -> None:
        lines = [line.strip() for line in self.text.value.splitlines() if line.strip()]
        text = "\n".join(line if line.startswith("- ") else f"- {line.lstrip('-•* ')}" for line in lines)
        journal = await self.bot.db.set_journal(interaction.guild_id, text, f"edited by {interaction.user}")
        await interaction.response.send_message(
            embed=embed(f"Journal updated (version {journal.version})", journal.text), ephemeral=True,
        )


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class Admin(commands.GroupCog, group_name="whiskers", group_description="Set up where Whiskers can talk and how it acts"):
    channel = app_commands.Group(name="channel", description="Choose the channels Whiskers can talk in")
    personality = app_commands.Group(name="personality", description="Steer how Whiskers is growing")
    memory = app_commands.Group(name="memory", description="Manage what Whiskers remembers")

    def __init__(self, bot: WhiskersBot) -> None:
        self.bot = bot
        self._simulating = False
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.bot.cfg.owner_ids or interaction.permissions.manage_guild:
            return True
        await interaction.response.send_message("You need the **Manage Server** permission for this.", ephemeral=True)
        return False

    def _access_warning(self, guild: discord.Guild, channel: ChannelTarget) -> str:
        if isinstance(channel, discord.CategoryChannel):
            return ""
        permissions = channel.permissions_for(guild.me)
        can_send = permissions.send_messages_in_threads if isinstance(channel, discord.Thread) else permissions.send_messages
        if permissions.view_channel and can_send:
            return ""
        return "\n\n⚠️ I can't see or send messages there yet. Check my role's permissions for that channel."

    # ---- channels ----

    @channel.command(name="allow", description="Let Whiskers live in a channel, thread or whole category")
    @app_commands.describe(channel="The channel, thread or category")
    async def channel_allow(self, interaction: discord.Interaction, channel: ChannelTarget) -> None:
        await self.bot.db.set_channel_rule(interaction.guild_id, channel.id, rule="allow")
        settings = await self.bot.db.guild_settings(interaction.guild_id)
        note = ""
        if settings.mode == "everywhere":
            note = "\n\nWhiskers already talks everywhere that isn't blocked, so this matters only in allowlist mode."
        await interaction.response.send_message(
            f"🐾 Whiskers can now talk in {channel.mention}.{note}{self._access_warning(interaction.guild, channel)}",
            ephemeral=True,
        )

    @channel.command(name="block", description="Keep Whiskers out of a channel, thread or whole category")
    @app_commands.describe(channel="The channel, thread or category")
    async def channel_block(self, interaction: discord.Interaction, channel: ChannelTarget) -> None:
        await self.bot.db.set_channel_rule(interaction.guild_id, channel.id, rule="block")
        await interaction.response.send_message(
            f"🚫 Whiskers will stay out of {channel.mention} and won't read or remember messages there.",
            ephemeral=True,
        )

    @channel.command(name="unset", description="Remove a channel's allow/block rule and chattiness override")
    @app_commands.describe(channel="The channel, thread or category")
    async def channel_unset(self, interaction: discord.Interaction, channel: ChannelTarget) -> None:
        await self.bot.db.set_channel_rule(interaction.guild_id, channel.id, rule=None, chattiness=None)
        settings = await self.bot.db.guild_settings(interaction.guild_id)
        await interaction.response.send_message(
            f"{channel.mention} now follows the server default ({MODE_NAMES[settings.mode]}).", ephemeral=True,
        )

    @channel.command(name="list", description="Show where Whiskers can talk")
    async def channel_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=await self._channels_embed(interaction.guild_id), ephemeral=True)

    async def _channels_embed(self, guild_id: int) -> discord.Embed:
        settings = await self.bot.db.guild_settings(guild_id)
        rules = await self.bot.db.channel_rules(guild_id)
        allowed = [f"<#{cid}>" for cid, r in rules.items() if r.rule == "allow"]
        blocked = [f"<#{cid}>" for cid, r in rules.items() if r.rule == "block"]
        chatty = [f"<#{cid}>: {r.chattiness}%" for cid, r in rules.items() if r.chattiness is not None]
        result = embed("Where Whiskers lives", f"Mode: **{MODE_NAMES[settings.mode]}** (change with `/whiskers mode`)")
        result.add_field(name="Allowed", value=", ".join(allowed) or "none yet: use `/whiskers channel allow`", inline=False)
        result.add_field(name="Blocked", value=", ".join(blocked) or "none", inline=False)
        result.add_field(name="Chattiness", value="\n".join([f"Server default: {settings.chattiness}%", *chatty]), inline=False)
        return result

    @app_commands.command(name="mode", description="Talk only in allowed channels, or everywhere except blocked ones")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Only in allowed channels (default)", value="allowlist"),
        app_commands.Choice(name="Everywhere except blocked channels", value="everywhere"),
    ])
    async def mode(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        await self.bot.db.update_guild_settings(interaction.guild_id, mode=mode.value)
        await interaction.response.send_message(f"Whiskers now talks {MODE_NAMES[mode.value]}.", ephemeral=True)

    @app_commands.command(name="chattiness", description="How often Whiskers joins conversations without being called")
    @app_commands.describe(
        percent="Chance per message, 0 to 100. 0 means only when called. Default is 5.",
        channel="Set it for just this channel or category instead of the whole server",
    )
    async def chattiness(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 100],
                         channel: Optional[ChannelTarget] = None) -> None:
        if channel is None:
            await self.bot.db.update_guild_settings(interaction.guild_id, chattiness=percent)
            where = "across the server"
        else:
            await self.bot.db.set_channel_rule(interaction.guild_id, channel.id, chattiness=percent)
            where = f"in {channel.mention}"
        await interaction.response.send_message(
            f"Whiskers will join in on about {percent}% of messages {where}. It always answers when someone "
            "mentions it, replies to it or says its name.", ephemeral=True,
        )

    @app_commands.command(name="options", description="Turn Whiskers' extra behaviours on or off")
    @app_commands.describe(
        reactions="React to messages with emoji now and then",
        name_trigger="Saying Whiskers' name counts as talking to it",
        ping_on_reply="Ping people when Whiskers replies to them",
    )
    async def options(self, interaction: discord.Interaction, reactions: Optional[bool] = None,
                      name_trigger: Optional[bool] = None, ping_on_reply: Optional[bool] = None) -> None:
        changes = {k: v for k, v in {"reactions": reactions, "name_trigger": name_trigger,
                                     "ping_on_reply": ping_on_reply}.items() if v is not None}
        settings = (await self.bot.db.update_guild_settings(interaction.guild_id, **changes) if changes
                    else await self.bot.db.guild_settings(interaction.guild_id))
        await interaction.response.send_message(
            f"Reactions: **{_on(settings.reactions)}** · Name trigger: **{_on(settings.name_trigger)}** · "
            f"Ping on reply: **{_on(settings.ping_on_reply)}**", ephemeral=True,
        )

    @app_commands.command(name="owner", description="Choose who Whiskers belongs to (default: the server owner)")
    @app_commands.describe(member="Whiskers' human. Leave empty to see who it is now.")
    async def owner(self, interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
        if member is not None:
            if member.bot:
                await interaction.response.send_message("Whiskers can't belong to a bot.", ephemeral=True)
                return
            await self.bot.db.update_guild_settings(interaction.guild_id, owner_id=member.id)
            await interaction.response.send_message(
                f"🐾 Whiskers now belongs to {member.mention} and adores them more than anyone.", ephemeral=True,
            )
            return
        settings = await self.bot.db.guild_settings(interaction.guild_id)
        owner_id = settings.owner_id or interaction.guild.owner_id
        default = "" if settings.owner_id else " (the server owner, by default)"
        await interaction.response.send_message(f"Whiskers' human is <@{owner_id}>{default}.", ephemeral=True)

    @app_commands.command(name="status", description="Whiskers' settings and how its brain is doing")
    async def status(self, interaction: discord.Interaction) -> None:
        bot = self.bot
        engine, llm = bot.engine, bot.llm
        result = await self._channels_embed(interaction.guild_id)
        result.title = "Whiskers' status"
        brain_lines = [f"State: **{engine.status}**", f"Model: {engine.description}"]
        if engine.threads:
            brain_lines.append(f"CPU threads: {engine.threads}")
        if llm.tokens_per_second:
            brain_lines.append(f"Speed: {llm.tokens_per_second:.1f} tokens/s (a token is about ¾ of a word)")
        if llm.last_reply_seconds:
            brain_lines.append(f"Last reply took {llm.last_reply_seconds:.0f}s")
        brain_lines.append(f"Replies waiting: {bot.scheduler.pending_replies}")
        settings = await bot.db.guild_settings(interaction.guild_id)
        result.add_field(name="Human", value=f"<@{settings.owner_id or interaction.guild.owner_id}>", inline=False)
        result.add_field(name="Brain", value="\n".join(brain_lines), inline=False)
        counts = await bot.db.counts(interaction.guild_id)
        journal = await bot.db.journal(interaction.guild_id)
        result.add_field(name="Memory", value=(
            f"{counts['memories']} memories · {counts['profiles']} people · {counts['messages']} recent messages\n"
            f"Journal version {journal.version} (`/cat journal` to read it)\n"
            f"Speech: {SPEECH_NAMES[journal.can_speak]}"
        ), inline=False)
        result.set_footer(text=f"Code version {getattr(bot, 'version', 'unknown')}")
        await interaction.response.send_message(embed=result, ephemeral=True)

    @app_commands.command(name="simulate", description="Play test scenes with Whiskers and post the transcript")
    @app_commands.describe(pack="Which set of scenes to play (default: core). 'all' takes about 45 minutes.",
                           scene="Play just one scene instead of a pack")
    @app_commands.choices(pack=[
        app_commands.Choice(name=f"{name} ({len(scenes)} scene{'s' if len(scenes) != 1 else ''}, "
                                 f"~{count_replies(list(scenes)) * 20 // 60 + 1} min)",
                            value=name)
        for name, scenes in PACKS.items()
    ] + [app_commands.Choice(name=f"all ({len(SCENES)} scenes, ~{count_replies(list(SCENES)) * 20 // 60} min)",
                             value="all")])
    async def simulate(self, interaction: discord.Interaction, pack: Optional[app_commands.Choice[str]] = None,
                       scene: Optional[str] = None) -> None:
        if not self.bot.engine.ready:
            await interaction.response.send_message(f"Whiskers' brain isn't ready yet ({self.bot.engine.status}).",
                                                    ephemeral=True)
            return
        if self._simulating:
            await interaction.response.send_message("A simulation is already running. Wait for it to finish.",
                                                    ephemeral=True)
            return
        scenes = scenes_for(pack.value if pack else None, scene)
        if not scenes:
            await interaction.response.send_message(f"There's no scene called {scene!r}.", ephemeral=True)
            return
        progress = SimulationProgress(interaction, count_replies(scenes), len(scenes))
        await interaction.response.send_message(progress.text())
        progress.start()
        lines: list[str] = []
        self._simulating = True
        try:
            await run_scenes(self.bot, interaction.guild_id, interaction.user.id,
                             person_name(interaction.user.display_name, interaction.user.name), scenes,
                             on_step=progress.update, lines=lines)
        except Exception as error:  # share whatever was played, then report what went wrong
            log.exception("Simulation failed")
            reason = ("a reply took longer than 4 minutes, so the AI engine looks stuck"
                      if isinstance(error, asyncio.TimeoutError) else str(error)[:300] or type(error).__name__)
            await progress.finish(f"⚠️ **Simulation stopped** after {progress.done} of {progress.total} replies: "
                                  f"{reason}", lines)
        else:
            await progress.finish(f"✅ **Simulation finished**: {progress.total} replies in "
                                  f"{_duration(progress.elapsed())}.", lines)
        finally:
            self._simulating = False

    @simulate.autocomplete("scene")
    async def _scene_names(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        matches = [name for name in SCENES if current.lower() in name]
        return [app_commands.Choice(name=name, value=name) for name in matches[:25]]

    # ---- personality ----

    @personality.command(name="edit", description="Rewrite Whiskers' journal by hand")
    async def personality_edit(self, interaction: discord.Interaction) -> None:
        journal = await self.bot.db.journal(interaction.guild_id)
        await interaction.response.send_modal(JournalEditor(self.bot, journal.text))

    @personality.command(name="evolve", description="Make Whiskers reflect on recent chats and grow right now")
    async def personality_evolve(self, interaction: discord.Interaction) -> None:
        if not self.bot.engine.ready:
            await interaction.response.send_message(f"Whiskers' brain isn't ready yet ({self.bot.engine.status}).",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            new = await self.bot.brain.reflect_now(interaction.guild_id)
        except LLMError as error:
            await interaction.followup.send(f"Whiskers couldn't think it through: {error}", ephemeral=True)
            return
        if new is None:
            await interaction.followup.send("Whiskers thought about it and hasn't changed.", ephemeral=True)
        else:
            await interaction.followup.send(embed=embed("Whiskers grew a little", new), ephemeral=True)

    @personality.command(name="undo", description="Go back to the previous version of the journal")
    async def personality_undo(self, interaction: discord.Interaction) -> None:
        journal = await self.bot.db.undo_journal(interaction.guild_id)
        if journal is None:
            await interaction.response.send_message("There's no earlier version to go back to.", ephemeral=True)
            return
        await interaction.response.send_message(embed=embed("Journal restored", journal.text), ephemeral=True)

    @personality.command(name="speech", description="Choose whether Whiskers can communicate beyond meows")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Just a cat: sounds and actions only (default)", value="cat"),
        app_commands.Choice(name="Has learned to communicate (the journal says how)", value="communicate"),
    ])
    async def personality_speech(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        can_speak = mode.value == "communicate"
        await self.bot.db.set_can_speak(interaction.guild_id, can_speak)
        if can_speak:
            text = ("Whiskers can now get its thoughts across. Add a line to its journal with "
                    "`/whiskers personality edit` to say how, for example \"- People can hear my thoughts now.\"")
        else:
            text = ("Whiskers is just a cat again: only meows, purrs and actions. Left alone, it can learn to "
                    "communicate on its own once the server has spent enough time teaching it.")
        await interaction.response.send_message(text, ephemeral=True)

    @personality.command(name="reset", description="Turn Whiskers back into a brand-new ordinary cat")
    async def personality_reset(self, interaction: discord.Interaction) -> None:
        if not await ask_to_confirm(interaction, "Reset Whiskers to an ordinary cat that just arrived and can't talk? Its memories of "
                                                 "people stay.", "Reset"):
            return
        await self.bot.db.reset_journal(interaction.guild_id)
        await interaction.edit_original_response(content="Whiskers is an ordinary cat again. 🐈")

    # ---- memory ----

    @memory.command(name="forget", description="Make Whiskers forget everything about a member")
    async def memory_forget(self, interaction: discord.Interaction, member: discord.User) -> None:
        await self.bot.db.forget_user(interaction.guild_id, member.id)
        await interaction.response.send_message(f"Whiskers forgot everything about {member.mention}.", ephemeral=True)

    @memory.command(name="wipe", description="Make Whiskers forget all chats, memories and people in this server")
    async def memory_wipe(self, interaction: discord.Interaction) -> None:
        if not await ask_to_confirm(interaction, "Wipe all of Whiskers' memories for this server? Its journal "
                                                 "and settings stay. This can't be undone.", "Wipe"):
            return
        await self.bot.db.wipe_memories(interaction.guild_id)
        await interaction.edit_original_response(content="Whiskers' memory of this server is wiped.")


async def setup(bot: WhiskersBot) -> None:
    await bot.add_cog(Admin(bot))
