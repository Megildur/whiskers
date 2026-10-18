"""When Whiskers speaks, what it knows when it does, and how it grows.

Nothing in here touches Discord directly, so it can be tested on its own. cogs/chat.py turns Discord
messages into `Incoming` and sends what comes back.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import AsyncContextManager, Awaitable, Callable

from .config import Config
from .db import ChannelRule, Database, Feeling, GuildSettings, Journal, Memory, Profile, StoredMessage, similar
from .llm import CHORES, ChatResult, LLMClient, LLMError, parse_json
from .prompts import (
    PRONOUNS, SCENE_LIMIT, SUMMARY_SCHEMA, journal_from_lines, load_personality, parse_journal, prefill_messages,
    reflection_messages, reflection_schema, reply_messages, reply_schema, reply_system_prompt, summary_messages,
    transcript,
)
from .scheduler import Scheduler
from .text import (
    ago, as_stage_directions, cat_only, clean_reply, keywords, makes_sound, narration_as_action, no_wagging, read_reply,
    repeated_actions, written_reply,
)

log = logging.getLogger("whiskers.brain")

CAT_WORDS = re.compile(
    r"\b(cats?|kitty|kitties|kittens?|meow\w*|purr\w*|treats?|tuna|fish|catnip|laser|mouse|mice|yarn|zoomies)\b", re.I,
)
# Emoji Whiskers reacts with, by what a message is about, and the chance of reacting to each.
INSULT = r"(ugly|stupid|dumb|worst|annoying|mid|fat)"
REACTIONS: list[tuple[re.Pattern, list[str], float]] = [
    (re.compile(r"\b(good (kitty|cat|boy|girl)|pets?|pat(s|ting)?|scritch\w*|cuddl\w*|snuggl\w*|hugs?|love (you|u)|ily)\b"
                r"|[❤💕🥰😍]", re.I), ["😻", "😽", "💕"], 0.5),
    (re.compile(r"\b(treats?|tuna|fish|salmon|chicken|snacks?|dinner|lunch|breakfast|food|hungry|pizza|sushi|ramen|"
                r"tacos?|cooking)\b|[🍕🍣🍜🐟🍗🌮]", re.I), ["🐟", "😋", "👀"], 0.4),
    (re.compile(r"\b(thunder|storm|fireworks|earthquake)\b|⛈", re.I), ["🙀"], 0.45),
    (re.compile(r"\b(baths?|vacuum|vet|dogs?|pupp(y|ies)|cucumber|spray bottle)\b|[🐶🛁]", re.I), ["🙀", "😾"], 0.45),
    (re.compile(r"\b(laser|mouse|mice|yarn|toys?|string|feathers?|birds?|bugs?|box(es)?|package)\b|[🐦📦🐭]", re.I),
     ["👀", "😼"], 0.4),
    (re.compile(r"\b(good ?night|gn+|sleep\w*|naps?|tired|bed ?time|exhausted)\b|[💤😴]", re.I), ["💤", "😴"], 0.45),
    (re.compile(r"\b(good ?morning|gm|morning)\b|[☀🌅]", re.I), ["☀️", "😺"], 0.4),
    (re.compile(r"\b(congrats|congratulations|birthday|hbd|we won|gg|let'?s go+|got the job|passed|promoted)\b"
                r"|[🎉🥳🎂🏆]", re.I), ["🎉", "😸"], 0.5),
    (re.compile(r"\b(sad|crying|cried|bad day|depressed|lonely|stressed|failed|hospital|miss (you|u|him|her))\b|[😢😞💔]",
                re.I), ["😿", "🫂"], 0.45),
    (re.compile(r"\b(lol|lmao+|lmfao|haha+|rofl|i'?m dead)\b|[😂🤣💀]", re.I), ["😹"], 0.2),
    (re.compile(r"\b(cats?|kitty|kitties|kittens?|meow\w*|purr\w*)\b|[🐱😺😸🐈]", re.I), ["😼", "🐾"], 0.4),
    (re.compile(r"\b(hi|hello|hey|hiya|welcome)\b|👋", re.I), ["👋", "😺"], 0.25),
    (re.compile(r"\[(sends a (picture|video|gif)|shares a (TikTok|YouTube video|tweet))", re.I), ["👀"], 0.3),
]
RANDOM_REACTIONS = ["🐾", "👀", "😺"]
REACTION_GAP = 20  # seconds between reactions in a channel, so a busy chat isn't covered in emoji
FALLBACK_ACTIONS = [
    "*stares at you, then slowly blinks*", "*flicks tail*", "mrrp?", "*yawns and stretches*", "*tilts head*",
]
# DRY sampling stops a model from repeating the same phrases message after message (llama.cpp only).
ANTI_REPEAT = {"dry_multiplier": 0.8, "dry_base": 1.75, "dry_allowed_length": 2, "dry_penalty_last_n": 1024}
HISTORY_CHAR_BUDGET = 10000  # about 3,000 tokens of chat; HISTORY_MESSAGES usually runs out first
MESSAGE_CHAR_LIMIT = 400
SILENCE_RESETS_CONTEXT = 3 * 3600
SPEECH_MIN_REFLECTIONS = 5  # journal updates before Whiskers may learn to communicate

# How each reply's "feeling" moves Whiskers' affection for that person (-100 to 100). Hurting Whiskers
# counts for more than a treat, so one kick out of a window takes a few kind days to make up for.
FEELING_EFFECT = {"loved": 6, "liked": 3, "neutral": 0, "disliked": -12, "hated": -25}
DAILY_WARMING = 15  # most affection kindness can add per person per day: trust is earned over days
CHOOSE_AT = 60  # affection at which Whiskers picks someone as one of its people, next to its human
UNCHOOSE_BELOW = 40  # and below which they stop being one
CHOOSE_MIN_CHATS = 8
MAX_CHOSEN = 3
MAX_FEELINGS = 10  # things remembered per person
WARY_AT = -12
QUIET_AFTER = 4  # messages in a row without a cat sound before Whiskers is reminded it has a voice


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
    owner_id: int = 0  # Whiskers' human in this server


@dataclass
class Decision:
    # "direct": someone talked to Whiskers. "home": a message in a channel where Whiskers answers everything.
    # "ambient": Whiskers butts in on its own.
    reply: str | None = None
    reaction: str | None = None


@dataclass
class Prepared:
    """Notes for a channel's next reply, already read into the model's cache (Brain.prepare_next)."""
    notes: list[str]
    people: set[int]
    memories: set[int]
    rows: list[int]  # the chat the notes follow, ending with Whiskers' latest message
    system: str
    part_of_day: str


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


def relationship(chats: int, is_owner: bool = False, affection: float = 0.0, chosen: bool = False) -> str:
    """How Whiskers feels about someone, for the model's notes."""
    if is_owner:
        return "your human, the person you love most in the world"
    if chosen:
        return "one of your chosen people: you love them almost as much as your human"
    if affection <= -40:
        return "you can't stand them after how they've treated you: you hiss, swat or stalk off when they come near"
    if affection <= WARY_AT:
        return "you don't trust them after how they've treated you: ears back, you keep your distance, but kindness can win you back"
    if affection >= 35:
        return "you really like them and go to them for attention"
    if chats >= 25:
        return "a good friend you trust"
    if chats >= 6 or affection >= 12:
        return "you like them and are happy to see them"
    if chats >= 2 or affection >= 5:
        return "you're getting to know them and starting to like them"
    return "new to you: you're curious about them and a little shy, but friendly"


def relationship_label(chats: int, is_owner: bool = False, affection: float = 0.0, chosen: bool = False) -> str:
    """The same, said about Whiskers to the person themselves."""
    if is_owner:
        return "its human"
    if chosen:
        return "one of its chosen people"
    if affection <= -40:
        return "someone it can't stand"
    if affection <= WARY_AT:
        return "someone it doesn't trust"
    if affection >= 35:
        return "a close friend"
    if chats >= 25:
        return "a good friend"
    if chats >= 6 or affection >= 12:
        return "a friend"
    if chats >= 2 or affection >= 5:
        return "someone it's getting to know"
    return "someone new"


def likes_and_dislikes(feelings: list[Feeling]) -> tuple[list[str], list[str]]:
    """What someone did that Whiskers liked, and what it didn't, newest first."""
    newest = list(reversed(feelings))
    return ([f.about for f in newest if f.feeling in ("loved", "liked")],
            [f.about for f in newest if f.feeling in ("disliked", "hated")])


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
        self.pronouns = PRONOUNS[cfg.cat_gender]
        self.user_id = 0  # the bot's Discord ID, set once logged in
        self._name_pattern = re.compile(rf"\b{re.escape(cfg.bot_name)}\b", re.I)
        cat = rf"(cat|kitty|bot|{re.escape(cfg.bot_name)})"
        insults = re.compile(rf"\b{INSULT}\b.{{0,20}}\b{cat}\b|\b{cat}\b.{{0,20}}\b{INSULT}\b|\bbad (cat|kitty)\b", re.I)
        self._reactions = [(insults, ["😾"], 0.6), *REACTIONS]
        self._jobs: dict[tuple[int, int], ReplyJob] = {}
        self._last_spoke: dict[int, float] = {}
        self._last_spoke_to: dict[tuple[int, int], float] = {}
        self._last_message: dict[int, int] = {}
        self._owners: dict[int, int] = {}  # guild -> Whiskers' human
        self._scenes: dict[int, tuple[str, float]] = {}  # channel -> Whiskers' last private note on the scene
        self._raw: dict[int, str] = {}  # channel -> the model's last reply before any filtering, for simulations
        self._felt: dict[int, tuple[str, str, float, str | None]] = {}  # channel -> last feeling, for simulations
        self._news: dict[tuple[int, int], str] = {}  # (guild, person) -> a change of heart to mention next time
        self._warmed: dict[tuple[int, int], tuple[int, float]] = {}  # (guild, person) -> (day, affection gained)
        self._last_reacted: dict[int, float] = {}
        self._read_upto: dict[int, int] = {}  # channel -> newest message the reply being written has read
        self._prepared: dict[int, Prepared] = {}  # channel -> notes for its next reply, already read
        self._seen: dict[int, int] = {}  # channel -> newest message a posted reply had read
        self._active_guild = 0
        self.slot_key: object | None = None  # which cache slot replies use; None means one per channel
        self.scene_first = cfg.scene_note == "before"
        self.reply_schema = reply_schema(self.scene_first)
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

    async def observe(self, msg: Incoming, settings: GuildSettings, chattiness: int, home: bool = False) -> Decision:
        """Remember a message from a channel Whiskers lives in and decide whether to react.

        In a home channel (one that's allowed with /whiskers channel allow) Whiskers answers every message."""
        await self.db.add_message(msg.guild_id, msg.channel_id, msg.message_id, msg.author_id, msg.author_name,
                                  msg.content, msg.created_at)
        self._last_message[msg.channel_id] = msg.message_id
        if msg.guild_id:
            self._active_guild = msg.guild_id
        if msg.owner_id:
            self._owners[msg.guild_id] = msg.owner_id
        profile = await self.db.profile(msg.guild_id, msg.author_id)
        decision = self.decide(msg, settings, chattiness, fondness=self.fondness(msg, profile), home=home)
        await self.db.touch_profile(msg.guild_id, msg.author_id, msg.author_name,
                                    chatted=decision.reply in ("direct", "home"))
        return decision

    def set_owner(self, guild_id: int, owner_id: int) -> None:
        """Whiskers' human in a server, known before anyone talks (so its slots can be warmed up)."""
        self._owners[guild_id] = owner_id

    @staticmethod
    def fondness(msg: Incoming, profile: Profile | None) -> float:
        """How much more (or less) likely Whiskers is to join in when this person talks."""
        if msg.author_id == msg.owner_id or (profile and profile.chosen):
            return 1.5
        if profile and profile.affection <= WARY_AT:
            return 0.5
        return 1.0

    def decide(self, msg: Incoming, settings: GuildSettings, chattiness: int, now: float | None = None,
               fondness: float = 1.0, home: bool = False) -> Decision:
        now = time.time() if now is None else now
        decision = Decision()
        talking_to_me = msg.is_dm or msg.mentions_me or msg.replies_to_me or (
            settings.name_trigger and bool(self._name_pattern.search(msg.content))
        )
        if talking_to_me:
            decision.reply = "direct"
        elif home:
            decision.reply = "home"
        else:
            chance = chattiness / 100
            if now - self._last_spoke_to.get((msg.channel_id, msg.author_id), 0) < 120:
                chance = max(chance, 0.6)  # Whiskers just answered them, so they're probably still talking to it
            elif now - self._last_spoke.get(msg.channel_id, 0) < 30:
                chance = 0.0  # don't take over the channel
            elif CAT_WORDS.search(msg.content):
                chance *= 2.5
            chance *= fondness
            if chance and self.rng.random() < min(chance, 0.9):
                decision.reply = "ambient"
        if settings.reactions and not msg.is_dm:
            # Reactions are cheap: no model call. Fewer when Whiskers is also going to answer.
            scale = 1.0 if decision.reply in (None, "ambient") else 0.4
            decision.reaction = self._pick_reaction(msg, now, fondness, scale)
            if decision.reply == "ambient" and decision.reaction and self.rng.random() < 0.5:
                decision.reply = None  # the reaction says enough
        return decision

    def _pick_reaction(self, msg: Incoming, now: float, fondness: float = 1.0, scale: float = 1.0) -> str | None:
        if now - self._last_reacted.get(msg.channel_id, 0) < REACTION_GAP:
            return None
        emoji = None
        for pattern, emojis, chance in self._reactions:
            if pattern.search(msg.content) and self.rng.random() < chance * scale:
                emoji = self.rng.choice(emojis)
                break
        if emoji is None and fondness <= 0.5 and self.rng.random() < 0.1 * scale:
            emoji = "😾"  # someone Whiskers doesn't trust
        elif emoji is None and fondness >= 1.5 and "*" not in msg.content and self.rng.random() < 0.04 * scale:
            emoji = self.rng.choice(["😻", "😽"])  # its human or one of its people, just chatting
        elif emoji is None and self.rng.random() < 0.02 * scale:
            emoji = self.rng.choice(RANDOM_REACTIONS)
        if emoji:
            self._last_reacted[msg.channel_id] = now
        return emoji

    def last_message_id(self, channel_id: int) -> int | None:
        return self._last_message.get(channel_id)

    # ---- queueing replies ----

    def too_busy(self, kind: str) -> bool:
        if kind == "ambient":
            return self.scheduler.busy
        return self.scheduler.pending_replies >= self.cfg.max_queue

    def submit(self, kind: str, msg: Incoming) -> ReplyJob | None:
        """Queue a reply. None if an existing queued reply already covers this message.

        In a home channel, messages that arrive while Whiskers is busy are answered together in one reply,
        so it never falls minutes behind a fast chat."""
        key = (msg.channel_id, msg.author_id if kind == "direct" else 0)
        existing = self._jobs.get(key)
        if existing and not existing.started:
            existing.message = msg  # they kept talking; answer the newest message once
            return None
        if kind == "ambient" and any(k[0] == msg.channel_id for k in self._jobs):
            return None
        if kind == "direct":
            room = self._jobs.get((msg.channel_id, 0))
            if room and not room.started:
                room.cancelled = True  # someone is talking to Whiskers; that reply covers the rest too
                del self._jobs[room.key]
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
                posted: list[int | None] = []
                async with AsyncExitStack() as stack:
                    await stack.enter_async_context(typing())

                    async def post(text: str) -> None:
                        await stack.aclose()  # stop "typing..." as the message appears
                        posted.append(await deliver(text))

                    # The message is posted as soon as it's written; compose finishes Whiskers' notes after.
                    text = await self.compose(job.message, job.kind, ready=post)
                message_id = posted[0] if posted else None
                if message_id is not None:
                    await self.record_own_message(job.message, message_id, text, time.time())
                    channel = job.message.channel_id
                    self._seen[channel] = max(self._seen.get(channel, 0),
                                              self._read_upto.get(channel, job.message.message_id))
                    if self.scheduler.pending_replies <= 1:  # nobody else is waiting: get the next reply ready
                        try:
                            await self.prepare_next(job.message)
                        except Exception as error:  # only a head start; the next reply works without it
                            log.warning("Couldn't get the next reply ready: %s", error)
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
        if job.kind == "home":  # only if a reply was written after reading this message
            return self._seen.get(msg.channel_id, 0) >= msg.message_id
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

        def size(rows: list[StoredMessage]) -> int:
            return sum(min(len(r.content), MESSAGE_CHAR_LIMIT) for r in rows)

        if len(rows) > 2 and size(rows) > HISTORY_CHAR_BUDGET:  # too long: drop a chunk, not one message at a time
            while len(rows) > 2 and size(rows) > HISTORY_CHAR_BUDGET * 0.75:
                rows = rows[1:]
            self._window_start[channel_id] = rows[0].id
        return [replace(row, content=row.content[:MESSAGE_CHAR_LIMIT]) for row in rows]

    def _part_of_day(self, now: float) -> tuple[str, datetime]:
        local = datetime.fromtimestamp(now, self._tz)
        hour = local.hour
        if 5 <= hour < 12:
            return "morning", local
        if 12 <= hour < 17:
            return "afternoon", local
        if 17 <= hour < 22:
            return "evening", local
        return "night", local

    def person_note(self, profile: Profile, owner_id: int, now: float) -> str:
        line = f"{profile.name}: {relationship(profile.chats, profile.user_id == owner_id, profile.affection, profile.chosen)}."
        if profile.facts:
            line += " " + "; ".join(profile.facts[-3:]) + "."
        felt = [f for f in profile.feelings if f.feeling != "neutral"][-3:]
        if felt:
            line += " You " + "; ".join(f"{f.feeling} {f.about} ({ago(now - f.at)})" for f in reversed(felt)) + "."
        return line

    @staticmethod
    def memory_note(memory: Memory, now: float) -> str:
        return f"You remember ({ago(now - memory.created_at)}): {memory.content}"

    def context_notes(self, owner_id: int, profiles: list[Profile], memories: list[Memory], scene: str | None = None,
                      repeating: list[str] | None = None, news: list[str] | None = None,
                      now: float | None = None, quiet: bool = False) -> list[str]:
        """What Whiskers knows going into its next reply: who's around, what it remembers, where things stand.
        None of it depends on the next message, so it can be read ahead of time."""
        now = time.time() if now is None else now
        part, local = self._part_of_day(now)
        notes = [f"It's {local:%A} {part}."]
        notes += [self.person_note(profile, owner_id, now) for profile in profiles]
        notes += [self.memory_note(memory, now) for memory in memories]
        notes += news or []
        if scene:
            notes.append(f"Where things stood after your last message: {scene}")
        if repeating:
            notes.append(f"You've been repeating yourself ({', '.join(repeating)}). Do something different this time.")
        if quiet:
            notes.append("You haven't made a sound in a while. A mrrp, meow, purr or hiss would fit if the moment "
                         "calls for one.")
        return notes

    def reply_note(self, msg: Incoming, kind: str, can_speak: bool = False) -> str:
        """Which message to answer, and how."""
        latest = msg.content if len(msg.content) <= 160 else msg.content[:160] + "..."
        feeling = f'Your "feeling" is about {msg.author_name}.'
        said = "replied to your message" if msg.replies_to_me and not msg.mentions_me else "just said or did"
        if msg.owner_id and msg.author_id == msg.owner_id:
            said = f"(your human) {said}"
        if kind == "direct" and can_speak:
            return (f'{msg.author_name} {said}: "{latest}". Answer them as {self.name}, carrying on from what\'s been '
                    f"happening. {feeling}")
        if kind == "direct":
            return (f'{msg.author_name} {said}: "{latest}". React as a cat would, with actions and cat sounds but no '
                    f"human words, carrying on from what's been happening. {feeling}")
        if kind == "home":  # Whiskers' own channel: people talk to it without saying its name
            how = "in your own way" if can_speak else "with actions and cat sounds but no human words"
            return (f'{msg.author_name} {said}: "{latest}". React as a cat would, {how}, carrying on from what\'s '
                    f"been happening. If it's clearly meant for someone else, do your own cat thing nearby. {feeling}")
        return (f'Nobody called you, but this caught your attention: {msg.author_name}: "{latest}". '
                f"Join in as a cat would, or do your own cat thing nearby. {feeling}")

    def last_feeling(self, channel_id: int) -> tuple[str, str, float, str | None] | None:
        """(feeling, about, affection now, "chosen"/"dropped"/None) from the last reply here, for simulations."""
        return self._felt.get(channel_id)

    def last_raw_reply(self, channel_id: int) -> str:
        """What the model wrote for its last reply here, before human words were filtered out."""
        return self._raw.get(channel_id, "")

    def last_scene(self, channel_id: int) -> str:
        """Whiskers' latest private note on what's happening in a channel."""
        return self._scenes.get(channel_id, ("", 0.0))[0]

    async def _owner_name(self, guild_id: int) -> str | None:
        owner_id = self._owners.get(guild_id)
        profile = await self.db.profile(guild_id, owner_id) if owner_id else None
        return profile.name if profile else None

    def as_cat(self, rows: list[StoredMessage]) -> list[StoredMessage]:
        """Whiskers' own past messages with any human words removed, so old slips don't encourage new ones."""
        return [replace(r, content=cat_only(r.content) or "*meows*") if r.is_self else r for r in rows]

    async def system_prompt(self, guild_id: int, journal: Journal | None = None) -> str:
        journal = journal or await self.db.journal(guild_id)
        return reply_system_prompt(self.personality, journal.text, self.name, journal.can_speak,
                                   await self._owner_name(guild_id), self.pronouns, self.scene_first)

    @property
    def _system_in_first_turn(self) -> bool:
        preset = self.cfg.preset if self.cfg.llm_backend == "managed" else None
        return bool(preset and preset.system_in_first_turn)

    async def compose(self, msg: Incoming, kind: str, ready: Callable[[str], Awaitable[None]] | None = None) -> str:
        """Write Whiskers' reply to a message.

        `ready` is awaited once with the message as soon as the model has written it; the model then
        finishes its private notes (how the person made Whiskers feel) while the message is already posted.
        """
        journal = await self.db.journal(msg.guild_id)
        rows = self.window(msg.channel_id, await self.db.recent_messages(msg.channel_id, self.cfg.history_messages))
        if not journal.can_speak:
            rows = self.as_cat(rows)
        self._read_upto[msg.channel_id] = max([r.message_id for r in rows if not r.is_self] + [msg.message_id])
        system = await self.system_prompt(msg.guild_id, journal)
        names = sorted({r.author_name for r in rows if not r.is_self} | {msg.author_name})
        now = time.time()
        prepared = self._prepared.pop(msg.channel_id, None)
        if prepared and self._still_fits(prepared, rows, system, now):
            # The notes were read ahead of time; only the newest messages and what they bring are new.
            newest = rows[len(prepared.rows):]
            after = []
            for user_id in dict.fromkeys(r.author_id for r in newest):
                if user_id not in prepared.people and (profile := await self.db.profile(msg.guild_id, user_id)):
                    after.append(self.person_note(profile, msg.owner_id, now))
            words = keywords(" ".join(r.content for r in newest))
            after += [self.memory_note(m, now) for m in await self.db.search_memories(msg.guild_id, words, 1)
                      if m.id not in prepared.memories]
            notes = prepared.notes
        else:
            words = keywords(" ".join([msg.content] + [r.content for r in rows[-4:] if not r.is_self]))
            notes, _, _ = await self._context(msg.guild_id, msg.channel_id, rows, msg.author_id, msg.owner_id, words)
            after = []
        after.append(self.reply_note(msg, kind, journal.can_speak))
        slot = self._slot(msg.channel_id)

        def finish(raw: str) -> str:
            text = no_wagging(as_stage_directions(clean_reply(raw, self.name, names), self.pronouns["his"],
                                                  self.pronouns["himself"]))
            if not journal.can_speak:
                # A message narrated without asterisks ("Whiskers leans in and purrs.") is still an action.
                text = cat_only(text) or narration_as_action(text, self.name)
            if not text:
                log.warning("Nothing usable in the model's reply, so Whiskers falls back to a stock action: %r", raw)
            return text or self.rng.choice(FALLBACK_ACTIONS)

        streamed = [""]
        reply_written = asyncio.Event()

        def watch(text: str) -> None:
            streamed[0] = text
            if ready and not reply_written.is_set() and written_reply(text) is not None:
                reply_written.set()

        async def generate() -> ChatResult:
            history = rows
            while True:
                messages = reply_messages(system, history, notes, self.name,
                                          system_in_first_turn=self._system_in_first_turn, after=after)
                try:
                    return await self.llm.chat(
                        messages, max_tokens=self.cfg.max_reply_tokens, temperature=self.cfg.temperature,
                        json_schema=self.reply_schema, extra=ANTI_REPEAT, on_text=watch, slot=slot,
                    )
                except LLMError as error:
                    if "context" not in str(error).lower() or len(history) <= 4:
                        raise
                    log.warning("The prompt didn't fit; trying again with less chat history.")
                    history = history[len(history) // 2:]

        chat = asyncio.ensure_future(generate())
        text = None
        try:
            if ready:
                waiting = asyncio.ensure_future(reply_written.wait())
                try:
                    await asyncio.wait({chat, waiting}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    waiting.cancel()
                if reply_written.is_set() and not chat.done():
                    text = finish(written_reply(streamed[0]) or "")
                    await ready(text)
            try:
                result = await chat
            except LLMError as error:
                if text is None:
                    raise
                log.warning("The model stopped after the reply was posted: %s", error)
                return text
        finally:
            if not chat.done():
                chat.cancel()
        self.llm.last_reply_seconds = result.seconds
        self.llm.remember_prefix(slot, system)
        log.debug("Reply in %s: read %d prompt tokens (%d cached), wrote %d tokens in %.1fs", msg.channel_id,
                  result.prompt_tokens, result.cached_tokens, result.tokens, result.seconds)
        parts = read_reply(result.text)
        scene, reply = parts.scene, parts.reply
        if len(scene) >= SCENE_LIMIT - 2:
            scene = re.split(r"[;,.](?=[^;,.]*$)", scene)[0].rstrip(" ;,.") or scene  # cut off mid-word: end it cleanly
        if scene:
            self._scenes[msg.channel_id] = (scene[:200], time.time())
            log.debug("Scene in %s: %s", msg.channel_id, scene)
        self._raw[msg.channel_id] = reply
        if text is None:
            text = finish(reply)
            if ready:
                await ready(text)
        self._felt.pop(msg.channel_id, None)
        if kind in ("direct", "home"):  # chiming in on other people's chat says little about how they treat Whiskers
            try:
                await self.feel(msg, parts.feeling, parts.about)
            except Exception:  # the message is already posted; don't lose it over a bookkeeping error
                log.exception("Couldn't record how %s made Whiskers feel", msg.author_name)
        return text

    def _slot(self, channel_id: int) -> int | None:
        return self.llm.slot_for(channel_id if self.slot_key is None else self.slot_key)

    async def _context(self, guild_id: int, channel_id: int, rows: list[StoredMessage], author_id: int,
                       owner_id: int, words: list[str]) -> tuple[list[str], set[int], set[int]]:
        """The context notes for a reply in this chat, and which people and memories they cover."""
        people = [author_id]
        for row in reversed(rows[-10:]):
            if not row.is_self and row.author_id not in people:
                people.append(row.author_id)
        profiles = [p for uid in people[:3] if (p := await self.db.profile(guild_id, uid))]
        memories: list[Memory] = []
        for memory in await self.db.memories_about(guild_id, author_id, 2) + \
                await self.db.search_memories(guild_id, words, 3):
            if all(m.id != memory.id for m in memories):
                memories.append(memory)
        memories = memories[:2]
        scene, scene_at = self._scenes.get(channel_id, ("", 0.0))
        if time.time() - scene_at > SILENCE_RESETS_CONTEXT:
            scene = ""
        names = {r.author_name for r in rows if not r.is_self}
        # Only Whiskers' own habits count as repeating, not the people and things the scene is about.
        props = keywords(" ".join(r.content for r in rows[-12:] if not r.is_self), limit=40)
        own = [r.content for r in rows if r.is_self][-6:]
        # A habit is the same thing in half of Whiskers' last six messages; doing something twice is fine.
        repeating = repeated_actions(own, ignore={self.name, *names, *props}, times=3)
        # Whiskers copies its own recent messages, so a run of silent ones would keep it silent for good.
        quiet = len(own) >= QUIET_AFTER and not any(makes_sound(m) for m in own[-QUIET_AFTER:])
        news = [n for p in profiles if (n := self._news.pop((guild_id, p.user_id), None))]
        notes = self.context_notes(owner_id, profiles, memories, scene, repeating, news, quiet=quiet)
        return notes, {p.user_id for p in profiles}, {m.id for m in memories}

    def _still_fits(self, prepared: Prepared, rows: list[StoredMessage], system: str, now: float) -> bool:
        """True if a reply can start exactly like the prompt that was read ahead of time."""
        count = len(prepared.rows)
        return (prepared.system == system and [r.id for r in rows[:count]] == prepared.rows
                and len(rows) > count and not any(r.is_self for r in rows[count:])
                and prepared.part_of_day == self._part_of_day(now)[0])

    async def prepare_next(self, msg: Incoming) -> None:
        """Read the start of this channel's next reply into the model's cache while people are still typing.

        Everything but the newest messages is known as soon as Whiskers has replied: the chat so far and the
        notes on who's around, what it remembers and where the scene stands. On a CPU, reading those notes
        takes several seconds; doing it now leaves only the newest messages to read when one arrives.
        """
        if self.cfg.llm_backend != "managed":
            return
        journal = await self.db.journal(msg.guild_id)
        rows = self.window(msg.channel_id, await self.db.recent_messages(msg.channel_id, self.cfg.history_messages))
        if not rows or not rows[-1].is_self:
            return  # someone has already said something new; that reply reads it all anyway
        if not journal.can_speak:
            rows = self.as_cat(rows)
        system = await self.system_prompt(msg.guild_id, journal)
        words = keywords(" ".join(r.content for r in rows[-5:] if not r.is_self))
        notes, people, memories = await self._context(msg.guild_id, msg.channel_id, rows, msg.author_id,
                                                      msg.owner_id, words)
        slot = self._slot(msg.channel_id)
        result = await self.llm.chat(prefill_messages(system, rows, notes, self.name, self._system_in_first_turn),
                                     max_tokens=1, temperature=0.0, slot=slot)
        self.llm.remember_prefix(slot, system)
        self._prepared[msg.channel_id] = Prepared(notes, people, memories, [r.id for r in rows], system,
                                                  self._part_of_day(time.time())[0])
        log.debug("Got the next reply in %s ready (read %d prompt tokens in %.1fs).", msg.channel_id,
                  result.prompt_tokens, result.seconds)

    async def feel(self, msg: Incoming, feeling: str, about: str) -> str | None:
        """Remember how what someone just did made Whiskers feel, and let it add up.

        Enough love and Whiskers chooses them as one of its people; enough hurt and it un-chooses them.
        Returns "chosen" or "dropped" when that happens.
        """
        effect = FEELING_EFFECT.get(feeling, 0)
        profile = await self.db.profile(msg.guild_id, msg.author_id) if msg.guild_id else None
        self._felt[msg.channel_id] = (feeling or "neutral", about, profile.affection if profile else 0.0, None)
        if not effect or profile is None:
            return None
        if effect > 0:  # kindness builds trust slowly, a little each day; hurt counts straight away
            day = int(time.time() // 86400)
            warmed_day, warmed = self._warmed.get((msg.guild_id, msg.author_id), (day, 0.0))
            warmed = warmed if warmed_day == day else 0.0
            effect = max(0.0, min(effect, DAILY_WARMING - warmed))
            self._warmed[(msg.guild_id, msg.author_id)] = (day, warmed + effect)
        affection = max(-100.0, min(100.0, profile.affection + effect))
        feelings = list(profile.feelings)
        if about:
            feelings = [f for f in feelings if not similar(f.about, about)]
            feelings.append(Feeling(about, feeling, time.time()))
        chosen, change = profile.chosen, None
        key = (msg.guild_id, msg.author_id)
        if (not chosen and msg.author_id != msg.owner_id and affection >= CHOOSE_AT
                and profile.chats >= CHOOSE_MIN_CHATS and len(await self.db.chosen_people(msg.guild_id)) < MAX_CHOSEN):
            chosen, change = True, "chosen"
            self._news[key] = f"You've just decided that {profile.name} is one of your people now, next to your human."
            log.info("Whiskers chose %s as one of its people in guild %s.", profile.name, msg.guild_id)
        elif chosen and affection < UNCHOOSE_BELOW:
            chosen, change = False, "dropped"
            self._news[key] = f"{profile.name} has hurt you too often lately: they aren't one of your people anymore."
            log.info("Whiskers no longer counts %s as one of its people in guild %s.", profile.name, msg.guild_id)
        await self.db.save_feelings(msg.guild_id, msg.author_id, affection, feelings[-MAX_FEELINGS:], chosen)
        self._felt[msg.channel_id] = (feeling, about, affection, change)
        return change

    def describe_person(self, profile: Profile, is_owner: bool) -> str:
        """One line about someone for the journal update."""
        line = f"{profile.name}: {relationship_label(profile.chats, is_owner, profile.affection, profile.chosen)}"
        liked, disliked = likes_and_dislikes(profile.feelings)
        if liked:
            line += "; enjoyed " + ", ".join(liked[:3])
        if disliked:
            line += "; didn't like " + ", ".join(disliked[:3])
        return line.replace("its ", f"{self.pronouns['his']} ")

    # ---- background chores: memories and growing up ----

    async def next_chore(self) -> Callable[[], Awaitable[object]] | None:
        cold = await self._cold_slot()
        if cold:
            return lambda: self._warm_up(*cold)
        rows = await self.db.summary_batch()
        if rows:
            return lambda: self.summarize(rows)
        due = await self.db.reflection_due(self.cfg.reflect_every, self.cfg.reflect_min_hours * 3600)
        if due:
            return lambda: self.reflect(due[0])
        return None

    async def _cold_slot(self) -> tuple[int, str] | None:
        """A conversation slot that doesn't have the current system prompt cached yet, if any.

        Reading the system prompt is the slowest part of a reply that has nothing cached (on a CPU that can
        be a minute or more), so it's done ahead of time while nobody is talking: after a restart and after
        the journal changes.
        """
        if self.cfg.llm_backend != "managed":
            return None
        guild_id = self._active_guild or next(iter(self._owners), 0)
        if not guild_id:
            return None
        system = await self.system_prompt(guild_id)
        for slot in self.llm.conversation_slots():
            if self.llm.cached_prefix(slot) != system:
                return slot, system
        return None

    async def _warm_up(self, slot: int, system: str) -> None:
        messages = reply_messages(system, [], [], self.name, system_in_first_turn=self._system_in_first_turn)
        result = await self.llm.chat(messages, max_tokens=1, temperature=0.0, slot=slot)
        self.llm.remember_prefix(slot, system)
        log.info("Conversation slot %d is warmed up (read %d prompt tokens in %.0fs).", slot, result.prompt_tokens,
                 result.seconds)

    async def summarize(self, rows: list[StoredMessage]) -> str | None:
        """Turn a chunk of chat into a memory and facts about people. Returns the memory, if one was written."""
        guild_id, channel_id = rows[0].guild_id, rows[0].channel_id
        try:
            result = await self.llm.chat(
                summary_messages(self.name, transcript(rows, self.name), self.pronouns), max_tokens=350,
                temperature=0.3, json_schema=SUMMARY_SCHEMA, slot=self.llm.slot_for(CHORES),
            )
        except LLMError as error:
            failures = self._chore_failures[rows[0].id] = self._chore_failures.get(rows[0].id, 0) + 1
            if failures < 3:
                raise
            log.warning("Skipping a chunk of chat that couldn't be summarised: %s", error)
            await self.db.mark_summarized([r.id for r in rows])
            return None
        data = parse_json(result.text) or {}
        people = {r.author_name.lower(): r.author_id for r in rows if not r.is_self}

        summary = str(data.get("summary") or "").strip()[:500]
        if len(summary) >= 12:
            involved = [uid for name, uid in people.items() if name in summary.lower()]
            await self.db.add_memory(guild_id, channel_id, summary, involved, rows[-1].created_at)

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
        return summary if len(summary) >= 12 else None

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
        owner_id = self._owners.get(guild_id, 0)
        people = [self.describe_person(p, p.user_id == owner_id) for p in await self.db.people(guild_id, 8)]
        rows = await self.db.recent_guild_messages(guild_id, 40)
        if not journal.can_speak:
            rows = self.as_cat(rows)
        result = await self.llm.chat(
            reflection_messages(self.name, self.personality, journal.text, memories, transcript(rows, self.name, 4000),
                                can_speak=journal.can_speak, may_learn=may_learn,
                                owner=await self._owner_name(guild_id), pronouns=self.pronouns, people=people),
            max_tokens=500, temperature=0.7, json_schema=reflection_schema(may_learn), slot=self.llm.slot_for(CHORES),
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
