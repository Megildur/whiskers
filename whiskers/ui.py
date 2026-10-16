"""Bits of Discord UI shared by the command cogs."""
from __future__ import annotations

import discord

COLOR = discord.Color.from_rgb(244, 162, 97)


def embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=title, description=description[:4000], color=COLOR)


class Confirm(discord.ui.View):
    """Yes/cancel buttons that only the person who ran the command can press."""

    def __init__(self, user_id: int, confirm_label: str) -> None:
        super().__init__(timeout=60)
        self.user_id = user_id
        self.confirmed = False
        self.confirm.label = confirm_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = True
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)
        self.stop()


async def ask_to_confirm(interaction: discord.Interaction, question: str, confirm_label: str) -> bool:
    view = Confirm(interaction.user.id, confirm_label)
    await interaction.response.send_message(question, view=view, ephemeral=True)
    if await view.wait():  # timed out
        try:
            await interaction.edit_original_response(content="Timed out, nothing changed.", view=None)
        except discord.HTTPException:
            pass
    return view.confirmed
