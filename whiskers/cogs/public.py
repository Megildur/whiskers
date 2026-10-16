"""/cat: commands anyone can use."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..brain import familiarity
from ..ui import ask_to_confirm, embed

if TYPE_CHECKING:
    from ..bot import WhiskersBot


@app_commands.guild_only()
class Cat(commands.GroupCog, group_name="cat", group_description="Check in on Whiskers"):
    def __init__(self, bot: WhiskersBot) -> None:
        self.bot = bot
        super().__init__()

    @app_commands.command(name="journal", description="Read Whiskers' journal: who it has become so far")
    async def journal(self, interaction: discord.Interaction) -> None:
        journal = await self.bot.db.journal(interaction.guild_id)
        result = embed(f"{self.bot.cfg.bot_name}'s journal", journal.text)
        footer = f"Version {journal.version}"
        if journal.updated_at:
            footer += " · last changed"
            result.timestamp = datetime.fromtimestamp(journal.updated_at, timezone.utc)
        result.set_footer(text=footer)
        await interaction.response.send_message(embed=result)

    @app_commands.command(name="memories", description="See what Whiskers remembers about you")
    async def memories(self, interaction: discord.Interaction) -> None:
        profile = await self.bot.db.profile(interaction.guild_id, interaction.user.id)
        name = self.bot.cfg.bot_name
        if profile is None:
            await interaction.response.send_message(f"{name} doesn't know you yet. *sniffs your hand*", ephemeral=True)
            return
        memories = await self.bot.db.memories_about(interaction.guild_id, interaction.user.id, 5)
        result = embed(f"What {name} remembers about you",
                       f"You've talked {profile.chats} times; {familiarity(profile.chats)}.")
        result.add_field(name="Things it knows", value="\n".join(f"- {f}" for f in profile.facts) or "nothing yet",
                         inline=False)
        if memories:
            result.add_field(name="Moments it remembers", value="\n".join(f"- {m.content}" for m in memories)[:1024],
                             inline=False)
        result.set_footer(text="/cat forget-me makes it forget all of this")
        await interaction.response.send_message(embed=result, ephemeral=True)

    @app_commands.command(name="forget-me", description="Make Whiskers forget everything about you")
    async def forget_me(self, interaction: discord.Interaction) -> None:
        name = self.bot.cfg.bot_name
        if not await ask_to_confirm(interaction, f"Make {name} forget you, your messages and memories that "
                                                 "involve you?", "Forget me"):
            return
        await self.bot.db.forget_user(interaction.guild_id, interaction.user.id)
        await interaction.edit_original_response(content=f"*{name} looks at you like a stranger.* Done.")


async def setup(bot: WhiskersBot) -> None:
    await bot.add_cog(Cat(bot))
