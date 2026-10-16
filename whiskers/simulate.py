"""Test scenes for /whiskers simulate.

Each scene is played through the real model with a throwaway memory, so live chats and Whiskers' real
memories aren't touched. The transcript includes Whiskers' private scene notes, which show what it
understood. Change the personality or prompts, run the same scenes again and compare.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

from .brain import Brain, Incoming
from .db import Database, GuildSettings

if TYPE_CHECKING:
    from .bot import WhiskersBot

# Who says each line: "owner" is the person running the simulation, as Whiskers' human.
# "friend" is someone Whiskers knows well; "new" is someone it has never met.
SCENES: dict[str, list[tuple[str, str]]] = {
    "catnip": [
        ("owner", "Here have some catnip *gives catnip*"),
        ("owner", "Good kitty"),
        ("owner", "*pets*"),
        ("owner", "*picks you up*"),
    ],
    "window": [
        ("friend", "*kicks you out the window*"),
        ("owner", "Angel why would you do that"),
        ("friend", "*opens the door and calls you back inside*"),
    ],
    "dinner": [
        ("owner", "*puts down a bowl of tuna*"),
        ("owner", "Go on, it's safe"),
        ("owner", "*waits*"),
    ],
    "stranger": [
        ("new", "hi kitty"),
        ("new", "*holds out a hand*"),
        ("new", "*gives you a treat*"),
        ("new", "*pets you*"),
    ],
    "chaos": [
        ("owner", "*sits on the couch*"),
        ("friend", "*throws a toy mouse across the room*"),
        ("new", "*sits on the floor next to the mouse*"),
        ("owner", "Whiskers come here"),
    ],
    "belly": [
        ("friend", "*rubs your belly*"),
        ("friend", "*keeps rubbing*"),
        ("friend", "OW you bit me!!"),
    ],
}

FRIEND = (11, "Angel")
NEWCOMER = (12, "Tearyu")

log = logging.getLogger("whiskers.simulate")
REPLY_TIMEOUT = 240  # seconds; a normal reply takes well under a minute


@dataclass
class Step:
    """One finished reply, for progress updates."""
    done: int
    total: int
    scene: str
    scene_number: int
    scene_count: int
    speaker: str
    said: str
    note: str
    reply: str
    seconds: float


def count_replies(scenes: list[str]) -> int:
    return sum(len(SCENES[name]) for name in scenes)


async def run_scenes(bot: WhiskersBot, guild_id: int, owner_id: int, owner_name: str, scenes: list[str],
                     on_step: Callable[[Step], Awaitable[None]] | None = None,
                     lines: list[str] | None = None) -> str:
    """Play the scenes and return the transcript. `lines` fills up as it goes, so a caller can still
    share what was played if something fails halfway."""
    cfg = bot.cfg
    path = cfg.data_dir / "simulation.db"
    path.unlink(missing_ok=True)
    db = Database(path, cfg.bot_name)
    await db.connect()
    ids = itertools.count(int(time.time() * 1000) << 22)  # increasing, like Discord message IDs
    lines = [] if lines is None else lines
    lines += [f"Whiskers simulation · code {getattr(bot, 'version', 'unknown')} · model: {bot.engine.description} · "
              f"temperature {cfg.temperature}", ""]
    log.info("Simulation started: %s", ", ".join(scenes))
    total, done = count_replies(scenes), 0
    try:
        real = await bot.db.journal(guild_id)
        await db.set_journal(guild_id, real.text, "copied for simulation")
        await db.set_can_speak(guild_id, real.can_speak)
        people = {"owner": (owner_id, owner_name), "friend": FRIEND, "new": NEWCOMER}
        for number, name in enumerate(scenes):
            brain = Brain(cfg, db, bot.llm, bot.scheduler, rng=random.Random(number))
            brain.user_id = bot.user.id if bot.user else 0
            channel_id = 1000 + number  # a fresh "room" per scene
            await db.touch_profile(guild_id, FRIEND[0], FRIEND[1], chatted=False)
            await db.conn.execute("UPDATE profiles SET chats = 40 WHERE guild_id = ? AND user_id = ?",
                                  (guild_id, FRIEND[0]))
            await db.conn.commit()
            lines.append(f"===== {name} =====")
            for who, text in SCENES[name]:
                user_id, user_name = people[who]
                msg = Incoming(next(ids), guild_id, channel_id, user_id, user_name, text, time.time(),
                               mentions_me=True, owner_id=owner_id)
                await brain.observe(msg, GuildSettings(), 0)
                started = time.monotonic()
                async with bot.scheduler.reply_turn():  # takes turns with real chat
                    reply = await asyncio.wait_for(brain.compose(msg, "direct"), timeout=REPLY_TIMEOUT)
                seconds = time.monotonic() - started
                await brain.record_own_message(msg, next(ids), reply, time.time())
                note = brain.last_scene(channel_id)
                lines += [
                    f"{user_name}: {text}",
                    f"    (scene: {note or '-'})",
                    f"{cfg.bot_name}: {reply}    [{seconds:.0f}s]",
                    "",
                ]
                done += 1
                log.info("Simulation %d/%d (%s): %s -> %s", done, total, name, text, reply)
                if on_step:
                    await on_step(Step(done, total, name, number + 1, len(scenes), user_name, text, note, reply,
                                       seconds))
    finally:
        await db.close()
        path.unlink(missing_ok=True)
    return "\n".join(lines)
