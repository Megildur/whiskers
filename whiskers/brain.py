"""When Whiskers speaks, what it knows when it does, and how it grows.

Nothing in here touches Discord directly, so it can be tested on its own. cogs/chat.py turns Discord
messages into `Incoming` and sends what comes back.
"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import AsyncContextManager, Awaitable, Callable

from .config import Config
from .db import ChannelRule, Database, GuildSettings, Memory, Profile, StoredMessage
from .llm import LLMClient, LLMError, parse_json
from .prompts import (
    NOTES_HEADER, SUMMARY_SCHEMA, journal_from_lines, load_personality, parse_journal, reflection_messages,
    reflection_schema, reply_messages, reply_system_prompt, summary_messages, transcript,
)
from .scheduler import Scheduler
from .text import ago, cat_only, clean_reply, keywords

log = logging.getLogger("whiskers.brain")

CAT_WORDS = re.compile(
    r"\b(cats?|kitty|kitties|kittens?|meow\w*|purr\w*|treats?|tuna|fish|catnip|laser|mouse|mice|yarn|zoomies)\b", re.I,
)
REACTIONS: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"\b(good (kitty|cat|boy|girl)|pets?|pat(s|ting)?|scritch\w*|cuddl\w*|snuggl\w*)\b", re.I), ["😻", "😽"]),
    (re.compile(r"\b(treats?|tuna|fish|salmon|chicken|snacks?|dinner|food)\b", re.I), ["🐟", "😋"]),
    (re.compile(r"\b(baths?|vacuum|vet|dogs?|cucumber|spray bottle)\b", re.I), ["🙀", "😾"]),
    (re.compile(r"\b(laser|mouse|mice|yarn|toy|string|birds?|bugs?)\b", re.I), ["👀"]),
    (re.compile(r"\b(good ?night|gn|sleep\w*|naps?|tired|bed ?time)\b", re.I), ["💤", "😴"]),
]
RANDOM_REACTIONS = ["🐾", "👀", "😺"]
FALLBACK_ACTIONS = [
    "*stares at you, then slowly blinks*", "*flicks tail*", "mrrp?", "*yawns and stretches*", "*tilts head*",
]
HISTORY_CHAR_BUDGET = 6000
MESSAGE_CHAR_LIMIT = 400
SILENCE_RESETS_CONTEXT = 3 * 3600
SPEECH_MIN_REFLECTIONS = 5  # journal updates before Whiskers may learn to communicate


@dataclass
class Incoming:
    message_id: int
    guild_id: int  # 0 in DMs
    channel_id: int
    author_id: int
    author_name: str
    content: str
    created_at: float
    mentions_me: bool = False
    replies_to_me: bool = False
    is_dm: bool = False


@dataclass
class Decision:
    reply: str | None = None  # "direct" (someone talked to Whiskers) or "ambient" (Whiskers butts in)
    reaction: str | None = None


@dataclass(eq=False)
class ReplyJob:
    kind: str
    message: Incoming  # the newest message to answer; updated if the person keeps talking while queued
    key: tuple[int, int]
    started: bool = False
    cancelled: bool = False


def rule_for(rules: dict[int, ChannelRule], chain: list[int]) -> str | None:
    """`chain` is the channel, then its parent channel (for threads), then its category."""
    for channel_id in chain:
        rule = rules.get(channel_id)
        if rule and rule.rule:
            return rule.rule
    return None


def is_allowed(settings: GuildSettings, rules: dict[int, ChannelRule], chain: list[int]) -> bool:
    rule = rule_for(rules, chain)
    if rule:
        return rule == "allow"
    return settings.mode == "everywhere"


def chattiness_for(settings: GuildSettings, rules: dict[int, ChannelRule], chain: list[int]) -> int:
    for channel_id in chain:
        rule = rules.get(channel_id)
        if rule and rule.chattiness is not None:
            return rule.chattiness
    return settings.chattiness


def familiarity(chats: int) -> str:
    if chats <= 1:
        return "you've only just met"
    if chats < 6:
        return "you barely know them"
    if chats < 25:
        return "you know them a bit"
    if chats < 100:
        return "they're a friend"
    return "one of your favourite humans"


class Brain:
    def __init__(self, cfg: Config, db: Database, llm: LLMClient, scheduler: Scheduler,
                 rng: random.Random | None = None) -> None:
        self.cfg = cfg
        self.db = db
        self.llm = llm
        self.scheduler = scheduler
        self.rng = rng or random.Random()
        self.name = cfg.bot_name
        self.personality = load_personality(cfg.bot_name)
        self.user_id = 0  # the bot's Discord ID, set once logged in
        self._name_pattern = re.compile(rf"\b{re.escape(cfg.bot_name)}\b", re.I)
        self._jobs: dict[tuple[int, int], ReplyJob] = {}
        self._last_spoke: dict[int, float] = {}
        self._last_spoke_to: dict[tuple[int, int], float] = {}
        self._last_message: dict[int, int] = {}
        self._last_own: dict[int, int] = {}  # channel -> Whiskers' latest message ID
        self._last_answered: dict[tuple[int, int], int] = {}  # (channel, person) -> Whiskers' latest reply ID
        self._window_start: dict[int, int] = {}
        self._chore_failures: dict[int, int] = {}
        try:
            from zoneinfo import ZoneInfo

            self._tz = ZoneInfo(cfg.timezone)
        except Exception:
            log.warning("Unknown TIMEZONE %r, using UTC.", cfg.timezone)
            self._tz = timezone.utc

    # ---- noticing messages ----

    async def observe(self, msg: Incoming, settings: GuildSettings, chattiness: int) -> Decision:
        """Remember a message from a channel Whiskers lives in and decide whether to react."""
        await self.db.add_message(msg.guild_id, msg.channel_id, msg.message_id, msg.author_id, msg.author_name,
                                  msg.content, msg.created_at)
        self._last_message[msg.channel_id] = msg.message_id
        decision = self.decide(msg, settings, chattiness)
        await self.db.touch_profile(msg.guild_id, msg.author_id, msg.author_name, chatted=decision.reply == "direct")
        return decision

    def decide(self, msg: Incoming, settings: GuildSettings, chattiness: int, now: float | None = None) -> Decision:
        now = time.time() if now is None else now
        decision = Decision()
        talking_to_me = msg.is_dm or msg.mentions_me or msg.replies_to_me or (
            settings.name_trigger and bool(self._name_pattern.search(msg.content))
        )
        if talking_to_me:
            decision.reply = "direct"
        else:
            chance = chattiness / 100
            if now - self._last_spoke_to.get((msg.channel_id, msg.author_id), 0) < 120:
                chance = max(chance, 0.6)  # Whiskers just answered them, so they're probably still talking to it
            elif now - self._last_spoke.get(msg.channel_id, 0) < 30:
                chance = 0.0  # don't take over the channel
            elif CAT_WORDS.search(msg.content):
                chance *= 2.5
            if chance and self.rng.random() < min(chance, 0.9):
                decision.reply = "ambient"
        if settings.reactions and decision.reply is None:
            decision.reaction = self._pick_reaction(msg.content)
        return decision

    def _pick_reaction(self, content: str) -> str | None:
        for pattern, emojis in REACTIONS:
            if pattern.search(content) and self.rng.random() < 0.35:
                return self.rng.choice(emojis)
        if self.rng.random() < 0.01:
            return self.rng.choice(RANDOM_REACTIONS)
        return None

    def last_message_id(self, channel_id: int) -> int | None:
        return self._last_message.get(channel_id)

    # ---- queueing replies ----

    def too_busy(self, kind: str) -> bool:
        if kind == "ambient":
            return self.scheduler.busy
        return self.scheduler.pending_replies >= self.cfg.max_queue

    def submit(self, kind: str, msg: Incoming) -> ReplyJob | None:
        """Queue a reply. None if an existing queued reply already covers this message."""
        key = (msg.channel_id, msg.author_id if kind == "direct" else 0)
        existing = self._jobs.get(key)
        if existing and not existing.started:
            existing.message = msg  # they kept talking; answer the newest message once
            return None
        if kind == "ambient" and any(k[0] == msg.channel_id for k in self._jobs):
            return None
        if kind == "direct":
            ambient = self._jobs.get((msg.channel_id, 0))
            if ambient and not ambient.started:
                ambient.cancelled = True  # someone is talking to Whiskers; that reply covers it
                del self._jobs[ambient.key]
        job = ReplyJob(kind, msg, key)
        self._jobs[key] = job
        return job

    async def respond(self, job: ReplyJob, typing: Callable[[], AsyncContextManager],
                      deliver: Callable[[str], Awaitable[int | None]]) -> str | None:
        """Think of a reply and hand it to `deliver`, which posts it and returns the new message's ID.

        The turn is held until the reply is posted, so the next queued reply can see it.
        """
        try:
            async with self.scheduler.reply_turn():
                if job.cancelled or self.already_answered(job):
                    return None
                job.started = True
                async with typing():
                    text = await self._compose(job.message, job.kind)
                message_id = await deliver(text)
                if message_id is not None:
                    await self.record_own_message(job.message, message_id, text, time.time())
                return text
        finally:
            if self._jobs.get(job.key) is job:
                del self._jobs[job.key]

    def already_answered(self, job: ReplyJob) -> bool:
        """True if Whiskers posted after this message while the job waited, so a second reply would be a repeat.

        Discord message IDs grow over time, so comparing them tells which message came first.
        """
        msg = job.message
        if job.kind == "direct":
            return self._last_answered.get((msg.channel_id, msg.author_id), 0) > msg.message_id
        return self._last_own.get(msg.channel_id, 0) > msg.message_id

    async def record_own_message(self, msg: Incoming, message_id: int, text: str, created_at: float) -> None:
        await self.db.add_message(msg.guild_id, msg.channel_id, message_id, self.user_id, self.name, text,
                                  created_at, is_self=True)
        now = time.time()
        self._last_spoke[msg.channel_id] = now
        self._last_spoke_to[(msg.channel_id, msg.author_id)] = now
        self._last_message[msg.channel_id] = message_id
        self._last_own[msg.channel_id] = message_id
        self._last_answered[(msg.channel_id, msg.author_id)] = message_id
        if msg.guild_id:
            await self.db.count_cat_message(msg.guild_id)

    # ---- writing a reply ----

    def window(self, channel_id: int, rows: list[StoredMessage]) -> list[StoredMessage]:
        """The slice of recent chat to show the model.

        The start only moves forward in jumps (from HISTORY_MESSAGES down to half of it), so most
        prompts begin exactly like the previous one and llama.cpp can skip re-reading them.
        """
        if not rows:
            return rows
        for i in range(len(rows) - 1, 0, -1):
            if rows[i].created_at - rows[i - 1].created_at > SILENCE_RESETS_CONTEXT:
                rows = rows[i:]
                break
        start = self._window_start.get(channel_id)
        if start is None or start < rows[0].id:
            keep = max(4, self.cfg.history_messages // 2)
            start = rows[-keep].id if len(rows) > keep else rows[0].id
            self._window_start[channel_id] = start
        rows = [row for row in rows if row.id >= start]
        while len(rows) > 2 and sum(min(len(r.content), MESSAGE_CHAR_LIMIT) for r in rows) > HISTORY_CHAR_BUDGET:
            rows = rows[1:]
        return [replace(row, content=row.content[:MESSAGE_CHAR_LIMIT]) for row in rows]

    def notes(self, msg: Incoming, kind: str, profiles: list[Profile], memories: list[Memory],
              can_speak: bool = False, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        local = datetime.fromtimestamp(now, self._tz)
        hour = local.hour
        if 5 <= hour < 12:
            part = "morning"
        elif 12 <= hour < 17:
            part = "afternoon"
        elif 17 <= hour < 22:
            part = "evening"
        else:
            part = "night"
        notes = [f"It's {local:%A} {part} ({local:%I:%M %p}).".replace("(0", "(")]
        for profile in profiles:
            line = f"{profile.name}: {familiarity(profile.chats)}."
            if profile.facts:
                line += " " + "; ".join(profile.facts[-6:]) + "."
            notes.append(line)
        for memory in memories:
            notes.append(f"You remember ({ago(now - memory.created_at)}): {memory.content}")
        if kind == "direct" and can_speak:
            notes.append(f"{msg.author_name} is talking to you. Answer them as {self.name}.")
        elif kind == "direct":
            notes.append(f"{msg.author_name} is talking to you. React the way a cat would, with no words.")
        else:
            notes.append(f"Nobody called you, but {msg.author_name}'s message caught your attention. "
                         "Join in the way a cat would.")
        return notes

    def as_cat(self, rows: list[StoredMessage]) -> list[StoredMessage]:
        """Whiskers' own past messages with any human words removed, so old slips don't encourage new ones."""
        return [replace(r, content=cat_only(r.content) or "*meows*") if r.is_self else r for r in rows]

    async def _compose(self, msg: Incoming, kind: str) -> str:
        journal = await self.db.journal(msg.guild_id)
        rows = self.window(msg.channel_id, await self.db.recent_messages(msg.channel_id, self.cfg.history_messages))
        if not journal.can_speak:
            rows = self.as_cat(rows)
        people = [msg.author_id]
        for row in reversed(rows[-10:]):
            if not row.is_self and row.author_id not in people:
                people.append(row.author_id)
        profiles = [p for uid in people[:3] if (p := await self.db.profile(msg.guild_id, uid))]

        words = keywords(" ".join([msg.content] + [r.content for r in rows[-4:] if not r.is_self]))
        memories: list[Memory] = []
        for memory in await self.db.memories_about(msg.guild_id, msg.author_id, 2) + \
                await self.db.search_memories(msg.guild_id, words, 3):
            if all(m.id != memory.id for m in memories):
                memories.append(memory)

        system = reply_system_prompt(self.personality, journal.text, self.name, journal.can_speak)
        notes = self.notes(msg, kind, profiles, memories[:4], journal.can_speak)
        messages = reply_messages(system, rows, notes, self.name)

        names = sorted({r.author_name for r in rows if not r.is_self} | {msg.author_name})
        stops = [f"\n{n}:" for n in names][:12] + ["\n" + NOTES_HEADER.format(name=self.name)[:12]]
        result = await self.llm.chat(
            messages, max_tokens=self.cfg.max_reply_tokens, temperature=self.cfg.temperature, stop=stops,
            should_stop=lambda text: any(s in text for s in stops),
        )
        self.llm.last_reply_seconds = result.seconds
        text = clean_reply(result.text, self.name, names)
        if not journal.can_speak:
            text = cat_only(text)
        return text or self.rng.choice(FALLBACK_ACTIONS)

    # ---- background chores: memories and growing up ----

    async def next_chore(self) -> Callable[[], Awaitable[object]] | None:
        rows = await self.db.summary_batch()
        if rows:
            return lambda: self._summarize(rows)
        due = await self.db.reflection_due(self.cfg.reflect_every, self.cfg.reflect_min_hours * 3600)
        if due:
            return lambda: self.reflect(due[0])
        return None

    async def _summarize(self, rows: list[StoredMessage]) -> None:
        guild_id, channel_id = rows[0].guild_id, rows[0].channel_id
        try:
            result = await self.llm.chat(
                summary_messages(self.name, transcript(rows, self.name)), max_tokens=350, temperature=0.3,
                json_schema=SUMMARY_SCHEMA,
            )
        except LLMError as error:
            failures = self._chore_failures[rows[0].id] = self._chore_failures.get(rows[0].id, 0) + 1
            if failures < 3:
                raise
            log.warning("Skipping a chunk of chat that couldn't be summarised: %s", error)
            await self.db.mark_summarized([r.id for r in rows])
            return
        data = parse_json(result.text) or {}
        people = {r.author_name.lower(): r.author_id for r in rows if not r.is_self}

        summary = str(data.get("summary") or "").strip()
        if len(summary) >= 12:
            involved = [uid for name, uid in people.items() if name in summary.lower()]
            await self.db.add_memory(guild_id, channel_id, summary[:500], involved, rows[-1].created_at)

        facts = data.get("facts") if isinstance(data.get("facts"), list) else []
        for item in facts[:10]:
            if not isinstance(item, dict):
                continue
            person = str(item.get("person") or "").strip().lstrip("@").lower()
            fact = str(item.get("fact") or "").strip()
            user_id = people.get(person) or next(
                (uid for name, uid in people.items() if person and (name.startswith(person) or person.startswith(name))),
                None,
            )
            if user_id and fact:
                await self.db.add_facts(guild_id, user_id, [fact])
        await self.db.mark_summarized([r.id for r in rows])
        log.info("Wrote a memory for channel %s: %s", channel_id, summary or "(nothing worth remembering)")

    async def reflect(self, guild_id: int, reason: str = "evolved") -> str | None:
        """Let the server's influence rewrite Whiskers' journal. Returns the new journal, or None.

        Once Whiskers has been around long enough, this is also where it can learn to communicate.
        """
        journal = await self.db.journal(guild_id)
        may_learn = (
            not journal.can_speak and journal.reflections >= SPEECH_MIN_REFLECTIONS and journal.born_at > 0
            and time.time() - journal.born_at >= self.cfg.speech_after_days * 86400
        )
        memories = [m.content for m in await self.db.recent_memories(guild_id, 8)]
        rows = await self.db.recent_guild_messages(guild_id, 40)
        if not journal.can_speak:
            rows = self.as_cat(rows)
        result = await self.llm.chat(
            reflection_messages(self.name, self.personality, journal.text, memories, transcript(rows, self.name, 4000),
                                can_speak=journal.can_speak, may_learn=may_learn),
            max_tokens=500, temperature=0.7, json_schema=reflection_schema(may_learn),
        )
        data = parse_json(result.text)
        if data and isinstance(data.get("journal"), list):
            new = journal_from_lines(data["journal"])
        else:
            new = parse_journal(result.text)
        changed = new is not None and new.strip() != journal.text.strip()
        if changed:
            await self.db.set_journal(guild_id, new, reason)
        await self.db.mark_reflected(guild_id)
        if may_learn and data and data.get("learned_to_communicate") is True:
            await self.db.set_can_speak(guild_id, True)
            log.info("Whiskers has learned to communicate in guild %s.", guild_id)
        if not changed:
            log.info("Reflection in guild %s changed nothing.", guild_id)
            return None
        log.info("Whiskers grew a little in guild %s:\n%s", guild_id, new)
        return new

    async def reflect_now(self, guild_id: int) -> str | None:
        async with self.scheduler.reply_turn():
            return await self.reflect(guild_id, "evolved on request")
