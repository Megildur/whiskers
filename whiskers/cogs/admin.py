"""/whiskers: server setup for people with Manage Server."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from ..llm import LLMError
from ..ui import ask_to_confirm, embed

if TYPE_CHECKING:
    from ..bot import WhiskersBot

ChannelTarget = Union[
    discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel,
    discord.CategoryChannel, discord.Thread,
]
MODE_NAMES = {"allowlist": "only in allowed channels", "everywhere": "everywhere except blocked channels"}
SPEECH_NAMES = {False: "cat sounds and actions only", True: "has learned to communicate"}


def _on(value: bool) -> str:
    return "on" if value else "off"


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
        await interaction.response.send_message(embed=result, ephemeral=True)

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
