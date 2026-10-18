import asyncio
from types import SimpleNamespace

from discord import app_commands

from whiskers.cogs.admin import Admin
from whiskers.db import Database


class Response:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append(content)


def test_roaming_and_home_channels(tmp_path):
    async def go():
        db = Database(tmp_path / "admin.db")
        await db.connect()
        cog = Admin.__new__(Admin)
        cog.bot = SimpleNamespace(db=db)
        interaction = SimpleNamespace(guild_id=1, response=Response(), guild=None)

        await Admin.roam.callback(cog, interaction, app_commands.Choice(name="On", value="everywhere"))
        assert (await db.guild_settings(1)).mode == "everywhere"
        assert "roams every channel it can see" in interaction.response.sent[-1]
        await Admin.roam.callback(cog, interaction, app_commands.Choice(name="Off", value="allowlist"))
        assert (await db.guild_settings(1)).mode == "allowlist"

        cog._access_warning = lambda guild, channel: ""
        channel = SimpleNamespace(id=55, mention="#cat-room")
        await Admin.channel_allow.callback(cog, interaction, channel)
        assert (await db.channel_rules(1))[55].rule == "allow"
        assert "answers every message there" in interaction.response.sent[-1]

        await Admin.mischief.callback(cog, interaction, app_commands.Choice(name="Often", value="often"))
        assert (await db.guild_settings(1)).mischief == "often"
        assert "start some trouble on his own" in interaction.response.sent[-1]
        await Admin.mischief.callback(cog, interaction, app_commands.Choice(name="Off", value="off"))
        assert (await db.guild_settings(1)).mischief == "off" and "only reacts" in interaction.response.sent[-1]
        await db.close()

    asyncio.run(go())
