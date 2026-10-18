"""/whiskers: server setup for people with Manage Server."""
from __future__ import annotations

import asyncio
import io
import logging
import time
from collections import deque
from typing import TYPE_CHECKING, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from ..llm import LLMError
from ..simulate import GROUPS, PACKS, SCENES, TRANSCRIPT_FILE, Step, count_replies, run_scenes, scenes_for
from ..text import person_name
from ..ui import ask_to_confirm, embed

if TYPE_CHECKING:
    from ..bot import WhiskersBot

ChannelTarget = Union[
    discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel,
    discord.CategoryChannel, discord.Thread,
]
log = logging.getLogger("whiskers.admin")

UNSET_RULE = {
    "allowlist": "Whiskers stays out of it, since roaming is off",
    "everywhere": "Whiskers roams there: it answers when called and joins in when it wants",
}
SPEECH_NAMES = {False: "cat sounds and actions only", True: "has learned to communicate"}
MISCHIEF_NAMES = {
    "off": "he only reacts",
    "sometimes": "now and then, at most every 3 hours in a quiet channel (6 if ignored)",
    "often": "a lot, up to once an hour in a quiet channel (2 if ignored)",
}


def _on(value: bool) -> str:
    return "on" if value else "off"


def _duration(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


SECONDS_PER_STEP = 28  # a simulated reply plus reading ahead for the next one, with nemo-12b on a CPU


def _expected_time(scenes: list[str]) -> str:
    """Roughly how long a set of scenes takes, for the pack menu."""
    minutes = count_replies(scenes) * SECONDS_PER_STEP // 60 + 1
    return f"~{minutes} min" if minutes < 120 else f"~{round(minutes / 60)} h"


class SimulationProgress:
    """Keeps a public message up to date while /whiskers simulate runs.

    The command's own reply is edited for the first 14 minutes; Discord stops allowing that at 15, so a
    longer run continues in a normal channel message.
    """

    REPLY_EDIT_LIMIT = 14 * 60
    PACE_WINDOW = 50  # the time left is worked out from this many recent replies

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
        self._finished_at: deque[float] = deque(maxlen=self.PACE_WINDOW)  # when recent replies were done

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

    def time_left(self) -> float | None:
        """Seconds left at the pace of recent replies. None until there are a few: the first reply of a run
        can take minutes (a cold cache, chores that were waiting), so it says little about the rest."""
        times = self._finished_at
        if len(times) < 3:
            return None
        return (times[-1] - times[0]) / (len(times) - 1) * (self.total - self.done)

    def text(self, headline: str | None = None) -> str:
        filled = round(20 * self.done / self.total) if self.total else 20
        bar = "▓" * filled + "░" * (20 - filled)
        if headline is None:
            where = (f" · scene {self.last.scene_number} of {self.scenes}: **{self.last.scene}**"
                     if self.last else "")
            headline = f"🧪 **Simulation running**{where}"
            left = self.time_left()
            if left is not None:
                eta = f" · about {_duration(left)} left"
            else:
                eta = " · timing the first replies" if self.done else " · warming up"
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
        except Exception as error:  # a network hiccup with Discord mustn't stop the simulation
            log.warning("Couldn't update the simulation progress: %s", error)

    async def update(self, step: Step) -> None:
        self.done, self.total, self.last = step.done, step.total, step
        self.reply_started = time.monotonic()
        self._finished_at.append(self.reply_started)
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
        for attempt in range(2):
            file = discord.File(io.BytesIO(transcript.encode()), filename="whiskers-simulation.txt")
            try:
                if self.elapsed() < self.REPLY_EDIT_LIMIT:
                    await self.interaction.followup.send(note, file=file, allowed_mentions=mention)
                else:
                    await self.interaction.channel.send(note, file=file, allowed_mentions=mention)
                return
            except discord.Forbidden as error:  # no permission to attach files
                log.warning("Couldn't post the transcript file: %s", error)
                break
            except Exception as error:  # a network hiccup: try once more
                log.warning("Couldn't post the transcript file: %s", error)
                await asyncio.sleep(5)
        try:
            if len(transcript) <= 20000:  # short enough to post as text
                for start in range(0, len(transcript), 1900):
                    await self.interaction.channel.send(f"```\n{transcript[start:start + 1900]}\n```")
            else:
                await self.interaction.channel.send(
                    f"{user.mention} I couldn't attach the transcript. It's saved on the server as "
                    f"`data/{TRANSCRIPT_FILE}`.", allowed_mentions=mention)
        except Exception as error:
            log.warning("Couldn't post the transcript (it's in data/%s): %s", TRANSCRIPT_FILE, error)


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

    @channel.command(name="allow", description="Make a channel, thread or category Whiskers' home: it answers every message")
    @app_commands.describe(channel="The channel, thread or category")
    async def channel_allow(self, interaction: discord.Interaction, channel: ChannelTarget) -> None:
        await self.bot.db.set_channel_rule(interaction.guild_id, channel.id, rule="allow")
        await interaction.response.send_message(
            f"🏠 {channel.mention} is now one of Whiskers' home channels: it answers every message there. When "
            "several arrive while it's busy, it answers them together in one reply."
            f"{self._access_warning(interaction.guild, channel)}",
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
            f"{channel.mention} has no rule now. {UNSET_RULE[settings.mode]}.", ephemeral=True,
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
        roaming = "**on**: it answers when called and joins in when it wants, in every channel it can see" \
            if settings.mode == "everywhere" else "**off** (turn it on with `/whiskers roam`)"
        result = embed("Where Whiskers lives", f"Roaming: {roaming}")
        result.add_field(name="Home channels (answers every message)",
                         value=", ".join(allowed) or "none yet: use `/whiskers channel allow`", inline=False)
        result.add_field(name="Blocked", value=", ".join(blocked) or "none", inline=False)
        result.add_field(name="Chattiness when roaming",
                         value="\n".join([f"Server default: {settings.chattiness}%", *chatty]), inline=False)
        return result

    @app_commands.command(name="roam", description="Let Whiskers wander into every channel it can see, not just its homes")
    @app_commands.describe(roaming="On: it answers when called and joins in when it wants, anywhere it can see. "
                                   "Off: only its home channels.")
    @app_commands.choices(roaming=[
        app_commands.Choice(name="On: every channel it can see (except blocked ones)", value="everywhere"),
        app_commands.Choice(name="Off: only its home channels", value="allowlist"),
    ])
    async def roam(self, interaction: discord.Interaction, roaming: app_commands.Choice[str]) -> None:
        await self.bot.db.update_guild_settings(interaction.guild_id, mode=roaming.value)
        if roaming.value == "everywhere":
            text = ("🐾 Whiskers now roams every channel it can see. It answers when someone mentions it, replies to "
                    "it or says its name, and joins in or reacts when something catches its interest "
                    "(`/whiskers chattiness` sets how often). In its home channels it still answers everything. "
                    "`/whiskers channel block` keeps it out of a channel.")
        else:
            text = "🏠 Whiskers stays in its home channels now (`/whiskers channel allow` adds one)."
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="chattiness", description="How often Whiskers joins in uninvited while roaming")
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
            f"While roaming, Whiskers will join in on about {percent}% of messages {where}. It always answers when "
            "someone mentions it, replies to it or says its name, and answers everything in its home channels.",
            ephemeral=True,
        )

    @app_commands.command(name="mischief", description="How often Whiskers starts trouble on his own")
    @app_commands.describe(level="How often he starts games and shenanigans without being asked")
    @app_commands.choices(level=[
        app_commands.Choice(name=f"{name.capitalize()}: {about}", value=name) for name, about in MISCHIEF_NAMES.items()
    ])
    async def mischief(self, interaction: discord.Interaction, level: app_commands.Choice[str]) -> None:
        await self.bot.db.update_guild_settings(interaction.guild_id, mischief=level.value)
        if level.value == "off":
            text = "😇 Whiskers only reacts now: he won't start any trouble on his own."
        else:
            text = (f"😼 Mischief: **{level.value}**. Now and then Whiskers starts something himself in the middle of a "
                    f"chat, and when one of his home channels goes quiet while people are around he may start some "
                    f"trouble on his own ({MISCHIEF_NAMES[level.value]}).")
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="options", description="Turn Whiskers' extra behaviours on or off")
    @app_commands.describe(
        reactions="React to messages with emoji (it never needs the AI, so it's instant)",
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
        if engine.chat_slots:
            chores = " (+1 for background chores)" if engine.chore_slot is not None else ""
            brain_lines.append(f"Channels kept ready: {engine.chat_slots}{chores}")
        if llm.tokens_per_second:
            brain_lines.append(f"Speed: {llm.tokens_per_second:.1f} tokens/s (a token is about ¾ of a word)")
        if llm.last_reply_seconds:
            brain_lines.append(f"Last reply took {llm.last_reply_seconds:.0f}s")
        brain_lines.append(f"Replies waiting: {bot.scheduler.pending_replies}")
        settings = await bot.db.guild_settings(interaction.guild_id)
        chosen = await bot.db.chosen_people(interaction.guild_id)
        human = f"<@{settings.owner_id or interaction.guild.owner_id}>"
        if chosen:
            human += "\nAlso chose: " + ", ".join(f"<@{p.user_id}>" for p in chosen)
        result.add_field(name="Human", value=human, inline=False)
        result.add_field(name="Mischief", value=f"{settings.mischief.capitalize()}: {MISCHIEF_NAMES[settings.mischief]} "
                                                f"(`/whiskers mischief` changes it)", inline=False)
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
    @app_commands.describe(pack="Which set of scenes to play (default: core). The big ones take hours.",
                           scene="Play just one scene instead of a pack")
    @app_commands.choices(pack=[
        app_commands.Choice(name=f"{name} ({len(scenes)} scene{'s' if len(scenes) != 1 else ''}, "
                                 f"{_expected_time(list(scenes))})",
                            value=name)
        for name, scenes in PACKS.items()
    ] + [
        app_commands.Choice(name=f"{group} ({len(scenes_for(group))} scenes, {_expected_time(scenes_for(group))})",
                            value=group)
        for group in GROUPS
    ] + [app_commands.Choice(name=f"all ({len(SCENES)} scenes, {_expected_time(list(SCENES))})", value="all")])
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
