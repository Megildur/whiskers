"""The Discord client that ties everything together."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import signal
import time

import discord
from discord import app_commands
from discord.ext import commands

from .brain import Brain
from .config import Config
from .db import Database
from .engine import create_engine
from .llm import LLMClient
from .scheduler import Scheduler

log = logging.getLogger("whiskers")

EXTENSIONS = ("whiskers.cogs.chat", "whiskers.cogs.admin", "whiskers.cogs.public")
CAT_ACTIVITIES = [
    "chasing a laser dot", "napping in a sunbeam", "knocking things off the table", "staring at the wall",
    "guarding the treat drawer", "batting at a string", "sitting in a box", "judging everyone",
]


class WhiskersBot(commands.Bot):
    def __init__(self, cfg: Config) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        intents.dm_messages = cfg.allow_dms
        super().__init__(
            command_prefix=commands.when_mentioned, intents=intents, help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.cfg = cfg
        self.db = Database(cfg.data_dir / "whiskers.db", cfg.bot_name)
        self.engine = create_engine(cfg)
        self.llm = LLMClient(cfg, self.engine)
        self.scheduler = Scheduler()
        self.brain = Brain(cfg, self.db, self.llm, self.scheduler)
        self._background: list[asyncio.Task] = []

    async def on_message(self, message: discord.Message) -> None:
        """Chat is handled by cogs/chat.py; there are no prefix commands."""

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.engine.start()  # downloads and loads in the background; the bot comes online meanwhile
        for extension in EXTENSIONS:
            await self.load_extension(extension)
        self.tree.on_error = self._on_command_error
        await self._sync_commands()
        self._background = [asyncio.create_task(self._chores()), asyncio.create_task(self._presence())]
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(self.close()))
        except (NotImplementedError, RuntimeError):
            pass

    async def close(self) -> None:
        for task in self._background:
            task.cancel()
        await self.engine.stop()
        await self.llm.close()
        await super().close()
        await self.db.close()

    async def _sync_commands(self) -> None:
        """Publish slash commands, but only when they've changed since the last start."""
        payload = json.dumps([c.to_dict(self.tree) for c in self.tree.get_commands()], sort_keys=True)
        digest = hashlib.sha256(f"{self.application_id}:{payload}".encode()).hexdigest()
        marker = self.cfg.data_dir / "commands.sha256"
        if marker.exists() and marker.read_text().strip() == digest:
            return
        await self.tree.sync()
        marker.write_text(digest)
        log.info("Slash commands published. They can take a minute to show up in Discord.")

    async def _on_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure) and interaction.response.is_done():
            return
        log.error("Command /%s failed", interaction.command.qualified_name if interaction.command else "?",
                  exc_info=error)
        text = "Something went wrong running that command."
        if isinstance(error, app_commands.CheckFailure):
            text = "You can't use this command here."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    async def _chores(self) -> None:
        """While nobody's talking to Whiskers, turn chat into memories and let it grow."""
        last_prune = 0.0
        while True:
            await asyncio.sleep(20)
            try:
                if time.monotonic() - last_prune > 6 * 3600:
                    await self.db.prune()
                    last_prune = time.monotonic()
                if not self.engine.ready or self.scheduler.idle_for() < 30:
                    continue
                chore = await self.brain.next_chore()
                if chore:
                    await self.scheduler.run_chore(chore)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("A background chore failed; it will be retried")
                await asyncio.sleep(60)

    async def _presence(self) -> None:
        await self.wait_until_ready()
        shown: tuple[str, discord.Status] | None = None
        activity, rotate_at = random.choice(CAT_ACTIVITIES), 0.0
        while not self.is_closed():
            if self.engine.ready:
                if time.monotonic() > rotate_at:
                    activity, rotate_at = random.choice(CAT_ACTIVITIES), time.monotonic() + 1800
                wanted = (activity, discord.Status.online)
            else:
                wanted = (f"💤 waking up: {self.engine.status}"[:128], discord.Status.idle)
            if wanted != shown:
                try:
                    await self.change_presence(status=wanted[1], activity=discord.CustomActivity(name=wanted[0]))
                    shown = wanted
                except (discord.HTTPException, ConnectionError):
                    pass
            await asyncio.sleep(30)
