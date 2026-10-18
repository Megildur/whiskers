"""/cat: commands anyone can use."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..brain import likes_and_dislikes, relationship_label
from ..config import Config
from ..db import Journal
from ..ui import ask_to_confirm, embed

if TYPE_CHECKING:
    from ..bot import WhiskersBot


def next_update(journal: Journal, cfg: Config, now: float | None = None) -> str:
    """When Whiskers will next look back on what happened and maybe change his journal (Brain.reflect)."""
    now = time.time() if now is None else now
    left = cfg.reflect_every - journal.since_reflection
    if left > 0:
        return f"next update after {left} more message{'s' if left != 1 else ''} from {cfg.bot_name}"
    minutes = (journal.reflected_at + cfg.reflect_min_hours * 3600 - now) / 60
    if minutes >= 1:
        return f"next update in about {minutes:.0f} min" if minutes < 90 else f"next update in about {minutes / 60:.0f} h"
    return "next update at the next quiet moment, or within 15 minutes"


@app_commands.guild_only()
class Cat(commands.GroupCog, group_name="cat", group_description="Check in on Whiskers"):
    def __init__(self, bot: WhiskersBot) -> None:
        self.bot = bot
        super().__init__()

    @app_commands.command(name="journal", description="Read Whiskers' journal: who it has become so far")
    async def journal(self, interaction: discord.Interaction) -> None:
        journal = await self.bot.db.journal(interaction.guild_id)
        result = embed(f"{self.bot.cfg.bot_name}'s journal", journal.text)
        footer = (f"Version {journal.version} · {'can communicate' if journal.can_speak else 'cat sounds only'} · "
                  f"{next_update(journal, self.bot.cfg)}")
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
        settings = await self.bot.db.guild_settings(interaction.guild_id)
        is_owner = interaction.user.id == (settings.owner_id or interaction.guild.owner_id)
        result = embed(f"What {name} remembers about you",
                       f"You've talked {profile.chats} times. To {name}, you're "
                       f"{relationship_label(profile.chats, is_owner, profile.affection, profile.chosen)}.")
        result.add_field(name="Things it knows", value="\n".join(f"- {f}" for f in profile.facts) or "nothing yet",
                         inline=False)
        liked, disliked = likes_and_dislikes(profile.feelings)
        if liked or disliked:
            result.add_field(name="How you've treated it", value="\n".join(filter(None, [
                f"😻 Loved: {', '.join(liked[:6])}" if liked else "",
                f"😾 Didn't like: {', '.join(disliked[:6])}" if disliked else "",
            ]))[:1024], inline=False)
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
