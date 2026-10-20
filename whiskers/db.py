"""Everything Whiskers remembers, in one SQLite file (data/whiskers.db).

- guild_settings / channel_rules: where Whiskers may talk and how chatty it is.
- messages: recent chat in the channels Whiskers is allowed in (pruned after a week).
- memories: short diary entries summarised from that chat, searchable by keyword.
- profiles: what Whiskers knows about each person and how it feels about them.
- journal: who Whiskers has become, rewritten as the server shapes it (old versions kept for undo).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import aiosqlite

log = logging.getLogger("whiskers.db")

MAX_FACTS = 12
# How much each feeling about a thing counts towards whether Whiskers likes it overall, so one bad moment
# doesn't undo three good ones ("laser off" after an afternoon of loving the laser).
FEELING_WEIGHT = {"loved": 2, "liked": 1, "neutral": 0, "disliked": -1, "hated": -2}
# Someone being mean to Whiskers, as opposed to him just not enjoying something (a bath, being put down, a game
# ending, someone leaving). Only this makes him trust a person less.
MISTREATMENT = re.compile(
    r"\b(hit|hits|hitting|smack\w*|slap\w*|punch\w*|kick\w*|thr[eo]w\w*|toss\w*|shov\w*|push(es|ed|ing)?|yank\w*|"
    r"pull(s|ed|ing)? (his |my |her |your |the )?(tail|ears?|whiskers|fur)|tail pull\w*|"
    r"grab\w* (his |my |her |your |the )?tail|squeez\w*|pinch\w*|pok(e|es|ed|ing)|flick\w*|step(s|ped|ping)? on|"
    r"stomp\w*|spray\w*|squirt\w*|water bottle|yell\w*|scream\w* at|shout\w* at|scar(e|es|ed|ing)|spook\w*|"
    r"startl\w*|trap\w*|lock(s|ed|ing)? (him|me|in|out)|blow(s|n|ing)? in|blew in|hurt\w*|mean|cruel\w*|rough\w*|"
    r"abus\w*|insult\w*|called names|being called|name.calling|bull(y|ied|ying)|chas(e|ed|ing) (him|me)|"
    r"being chased|cornered|kidnap\w*|catnap\w*)\b",
    re.I,
)


def mistreated(text: str) -> bool:
    return bool(MISTREATMENT.search(text or ""))

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'allowlist',
    chattiness INTEGER NOT NULL DEFAULT 5,
    reactions INTEGER NOT NULL DEFAULT 1,
    name_trigger INTEGER NOT NULL DEFAULT 1,
    ping_on_reply INTEGER NOT NULL DEFAULT 0,
    owner_id INTEGER NOT NULL DEFAULT 0,
    mischief TEXT NOT NULL DEFAULT 'sometimes'
);
CREATE TABLE IF NOT EXISTS channel_rules (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    rule TEXT,
    chattiness INTEGER,
    PRIMARY KEY (guild_id, channel_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL UNIQUE,
    author_id INTEGER NOT NULL,
    author_name TEXT NOT NULL,
    is_self INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    summarized INTEGER NOT NULL DEFAULT 0,
    unprompted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS messages_channel ON messages (channel_id, id);
CREATE INDEX IF NOT EXISTS messages_guild ON messages (guild_id, id);
CREATE INDEX IF NOT EXISTS messages_pending ON messages (summarized, channel_id);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_guild ON memories (guild_id, created_at);
CREATE TABLE IF NOT EXISTS memory_people (
    memory_id INTEGER NOT NULL REFERENCES memories (id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (memory_id, user_id)
);
CREATE INDEX IF NOT EXISTS memory_people_user ON memory_people (user_id);
CREATE TABLE IF NOT EXISTS profiles (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    facts TEXT NOT NULL DEFAULT '[]',
    chats INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    affection REAL NOT NULL DEFAULT 0,
    feelings TEXT NOT NULL DEFAULT '[]',
    chosen INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    guild_id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL,
    since_reflection INTEGER NOT NULL DEFAULT 0,
    reflected_at REAL NOT NULL DEFAULT 0,
    can_speak INTEGER NOT NULL DEFAULT 0,
    reflections INTEGER NOT NULL DEFAULT 0,
    born_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS journal_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    version INTEGER NOT NULL,
    replaced_at REAL NOT NULL,
    reason TEXT NOT NULL
);
"""

# Columns added after the first release; older databases get them on startup.
ADDED_COLUMNS = {
    "journal": {
        "can_speak": "INTEGER NOT NULL DEFAULT 0",
        "reflections": "INTEGER NOT NULL DEFAULT 0",
        "born_at": "REAL NOT NULL DEFAULT 0",
    },
    "guild_settings": {
        "owner_id": "INTEGER NOT NULL DEFAULT 0",
        "mischief": "TEXT NOT NULL DEFAULT 'sometimes'",
    },
    "messages": {
        "unprompted": "INTEGER NOT NULL DEFAULT 0",  # Whiskers started something on his own (Brain.start_mischief)
    },
    "profiles": {
        "affection": "REAL NOT NULL DEFAULT 0",
        "feelings": "TEXT NOT NULL DEFAULT '[]'",
        "chosen": "INTEGER NOT NULL DEFAULT 0",
    },
}

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5 (content, content='memories', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts (rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts (memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
"""


def default_journal(name: str) -> str:
    return (
        f"- I'm {name}, a cat. I just moved into this Discord server.\n"
        "- I can't talk. I meow, purr, hiss and do cat things.\n"
        "- My human is my favourite person. Everyone else, I'm still getting to know."
    )


@dataclass
class GuildSettings:
    mode: str = "allowlist"  # "allowlist": only allowed channels. "everywhere": all except blocked ones.
    chattiness: int = 5  # % chance to join a conversation uninvited
    reactions: bool = True
    name_trigger: bool = True  # saying the cat's name counts as talking to it
    ping_on_reply: bool = False
    owner_id: int = 0  # Whiskers' human; 0 means the server owner
    mischief: str = "sometimes"  # how often Whiskers starts trouble on his own: "off", "sometimes" or "often"


@dataclass
class ChannelRule:
    rule: str | None = None  # "allow", "block" or None
    chattiness: int | None = None


@dataclass
class StoredMessage:
    id: int
    guild_id: int
    channel_id: int
    message_id: int
    author_id: int
    author_name: str
    is_self: bool
    content: str
    created_at: float


@dataclass
class Memory:
    id: int
    content: str
    created_at: float


@dataclass
class Feeling:
    """Something a person did that Whiskers loved, liked, disliked or hated, and how he feels about it overall."""
    about: str  # "belly rubs", "tail pulling"
    feeling: str  # the most recent time
    at: float
    score: int | None = None  # the feelings about it added up (FEELING_WEIGHT): above 0 he likes it overall

    def __post_init__(self) -> None:
        if self.score is None:
            self.score = FEELING_WEIGHT.get(self.feeling, 0)


@dataclass
class Profile:
    user_id: int
    name: str
    facts: list[str] = field(default_factory=list)
    chats: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    affection: float = 0.0  # -100 (can't stand them) to 100 (adores them)
    feelings: list[Feeling] = field(default_factory=list)  # oldest first
    chosen: bool = False  # Whiskers picked them as one of its people, next to its human


@dataclass
class Journal:
    text: str
    version: int
    updated_at: float
    since_reflection: int = 0
    reflected_at: float = 0.0
    can_speak: bool = False  # False: only cat sounds and actions, enforced in code
    reflections: int = 0
    born_at: float = 0.0  # when Whiskers arrived in this server


def _row_message(row) -> StoredMessage:
    return StoredMessage(row["id"], row["guild_id"], row["channel_id"], row["message_id"], row["author_id"],
                         row["author_name"], bool(row["is_self"]), row["content"], row["created_at"])


def _row_profile(row) -> Profile:
    feelings = [Feeling(f["about"], f["feeling"], f["at"], f.get("score")) for f in json.loads(row["feelings"] or "[]")]
    return Profile(row["user_id"], row["name"], json.loads(row["facts"]), row["chats"], row["first_seen"],
                   row["last_seen"], row["affection"], feelings, bool(row["chosen"]))


def similar(a: str, b: str) -> bool:
    a, b = a.lower().strip(" ."), b.lower().strip(" .")
    return a == b or a in b or b in a


FILLER = frozenset("being getting having with from that this into onto over under about after before their there "
                   "them they your yours".split())


def same_thing(a: str, b: str) -> bool:
    """Whether two feelings are about the same thing: "laser pointer" and "laser off", "bath" and "bath time"."""
    if similar(a, b):
        return True
    words = [{w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in FILLER} for text in (a, b)]
    return bool(words[0] & words[1])


class Database:
    def __init__(self, path: Path, bot_name: str = "Whiskers") -> None:
        self.path = path
        self.bot_name = bot_name
        self.fts = True
        self._conn: aiosqlite.Connection | None = None
        self._settings: dict[int, GuildSettings] = {}
        self._rules: dict[int, dict[int, ChannelRule]] = {}

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database.connect() wasn't called"
        return self._conn

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        try:
            await self._conn.executescript(FTS_SCHEMA)
        except aiosqlite.OperationalError as error:
            self.fts = False
            log.warning("SQLite full-text search isn't available (%s); memory search will be simpler.", error)
        await self._conn.commit()

    async def _migrate(self) -> None:
        for table, columns in ADDED_COLUMNS.items():
            async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
                existing = {row[1] for row in await cur.fetchall()}
            for column, definition in columns.items():
                if column not in existing:
                    await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        await self.conn.execute("UPDATE journal SET born_at = ? WHERE born_at = 0", (time.time(),))
        async with self.conn.execute("SELECT value FROM meta WHERE key = 'fair-feelings'") as cur:
            done = await cur.fetchone()
        if not done:
            await self._forgive_misjudged()
            await self.conn.execute("INSERT INTO meta (key, value) VALUES ('fair-feelings', ?)", (str(time.time()),))

    async def _forgive_misjudged(self) -> None:
        """Once, for a database from before feelings were judged fairly. Every dislike used to cost someone
        affection, even them leaving or a game ending, so Whiskers ended up wary of his friends. Give back what
        the remembered ones took, and forget a grudge with no real mistreatment behind it."""
        old_cost = {"disliked": 12, "hated": 25}
        async with self.conn.execute("SELECT guild_id, user_id, name, affection, feelings FROM profiles") as cur:
            rows = await cur.fetchall()
        for row in rows:
            feelings = json.loads(row["feelings"] or "[]")
            unfair = sum(old_cost.get(f["feeling"], 0) for f in feelings if not mistreated(f["about"]))
            hurt = any(f["feeling"] in old_cost and mistreated(f["about"]) for f in feelings)
            affection = min(100.0, row["affection"] + unfair)
            if affection < 0 and not hurt:
                affection = 0.0
            if affection != row["affection"]:
                await self.conn.execute("UPDATE profiles SET affection = ? WHERE guild_id = ? AND user_id = ?",
                                        (affection, row["guild_id"], row["user_id"]))
                log.info("Whiskers forgave %s for dislikes that weren't their fault: affection %+.0f -> %+.0f.",
                         row["name"], row["affection"], affection)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ---- settings ----

    async def guild_settings(self, guild_id: int) -> GuildSettings:
        cached = self._settings.get(guild_id)
        if cached:
            return cached
        async with self.conn.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)) as cur:
            row = await cur.fetchone()
        settings = GuildSettings() if row is None else GuildSettings(
            row["mode"], row["chattiness"], bool(row["reactions"]), bool(row["name_trigger"]),
            bool(row["ping_on_reply"]), row["owner_id"], row["mischief"],
        )
        self._settings[guild_id] = settings
        return settings

    async def update_guild_settings(self, guild_id: int, **changes) -> GuildSettings:
        allowed = {"mode", "chattiness", "reactions", "name_trigger", "ping_on_reply", "owner_id", "mischief"}
        if set(changes) - allowed:
            raise ValueError(f"unknown settings: {set(changes) - allowed}")
        await self.conn.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
        for key, value in changes.items():
            await self.conn.execute(f"UPDATE guild_settings SET {key} = ? WHERE guild_id = ?", (value, guild_id))
        await self.conn.commit()
        self._settings.pop(guild_id, None)
        return await self.guild_settings(guild_id)

    async def channel_rules(self, guild_id: int) -> dict[int, ChannelRule]:
        cached = self._rules.get(guild_id)
        if cached is not None:
            return cached
        async with self.conn.execute(
            "SELECT channel_id, rule, chattiness FROM channel_rules WHERE guild_id = ?", (guild_id,)
        ) as cur:
            rules = {row["channel_id"]: ChannelRule(row["rule"], row["chattiness"]) for row in await cur.fetchall()}
        self._rules[guild_id] = rules
        return rules

    async def set_channel_rule(self, guild_id: int, channel_id: int, *, rule: str | None = ...,
                               chattiness: int | None = ...) -> None:
        """Set the allow/block rule and/or chattiness override. Arguments left out keep their value."""
        current = (await self.channel_rules(guild_id)).get(channel_id, ChannelRule())
        new_rule = current.rule if rule is ... else rule
        new_chattiness = current.chattiness if chattiness is ... else chattiness
        if new_rule is None and new_chattiness is None:
            await self.conn.execute("DELETE FROM channel_rules WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id))
        else:
            await self.conn.execute(
                "INSERT INTO channel_rules (guild_id, channel_id, rule, chattiness) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (guild_id, channel_id) DO UPDATE SET rule = excluded.rule, chattiness = excluded.chattiness",
                (guild_id, channel_id, new_rule, new_chattiness),
            )
        await self.conn.commit()
        self._rules.pop(guild_id, None)

    # ---- messages ----

    async def add_message(self, guild_id: int, channel_id: int, message_id: int, author_id: int, author_name: str,
                          content: str, created_at: float, is_self: bool = False, unprompted: bool = False) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO messages (guild_id, channel_id, message_id, author_id, author_name, is_self, content, "
            "created_at, unprompted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, message_id, author_id, author_name, int(is_self), content[:1000], created_at,
             int(unprompted)),
        )
        await self.conn.commit()

    async def last_unprompted(self, channel_id: int) -> tuple[float, float | None] | None:
        """When Whiskers last posted on his own in a channel, and when someone first spoke after that (None if
        nobody has yet). None if he never has, as far as the stored chat goes."""
        async with self.conn.execute(
            "SELECT id, created_at FROM messages WHERE channel_id = ? AND unprompted = 1 ORDER BY id DESC LIMIT 1",
            (channel_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        async with self.conn.execute(
            "SELECT MIN(created_at) AS at FROM messages WHERE channel_id = ? AND id > ? AND is_self = 0",
            (channel_id, row["id"]),
        ) as cur:
            answered = await cur.fetchone()
        return row["created_at"], answered["at"] if answered else None

    async def update_message(self, message_id: int, content: str) -> None:
        await self.conn.execute("UPDATE messages SET content = ? WHERE message_id = ?", (content[:1000], message_id))
        await self.conn.commit()

    async def delete_messages(self, message_ids: list[int]) -> None:
        await self.conn.executemany("DELETE FROM messages WHERE message_id = ?", [(i,) for i in message_ids])
        await self.conn.commit()

    async def recent_messages(self, channel_id: int, limit: int) -> list[StoredMessage]:
        async with self.conn.execute(
            "SELECT * FROM messages WHERE channel_id = ? ORDER BY id DESC LIMIT ?", (channel_id, limit)
        ) as cur:
            rows = await cur.fetchall()
        return [_row_message(row) for row in reversed(rows)]

    async def active_channels(self, guild_id: int, since: float) -> list[int]:
        """Channels where someone other than Whiskers has said something since `since`."""
        async with self.conn.execute(
            "SELECT DISTINCT channel_id FROM messages WHERE guild_id = ? AND is_self = 0 AND created_at > ?",
            (guild_id, since),
        ) as cur:
            return [row["channel_id"] for row in await cur.fetchall()]

    async def recent_guild_messages(self, guild_id: int, limit: int) -> list[StoredMessage]:
        async with self.conn.execute(
            "SELECT * FROM messages WHERE guild_id = ? ORDER BY id DESC LIMIT ?", (guild_id, limit)
        ) as cur:
            rows = await cur.fetchall()
        return [_row_message(row) for row in reversed(rows)]

    async def summary_batch(self, *, full: int = 30, quiet: int = 6, quiet_seconds: float = 2700,
                            max_rows: int = 40, channel_id: int | None = None) -> list[StoredMessage]:
        """The next chunk of chat to turn into memories: a busy channel, or one that has gone quiet.
        `channel_id` looks at that channel only."""
        now = time.time()
        only = " AND channel_id = ?" if channel_id is not None else ""
        async with self.conn.execute(
            "SELECT channel_id, COUNT(*) AS n, MAX(created_at) AS newest FROM messages "
            f"WHERE summarized = 0{only} GROUP BY channel_id", () if channel_id is None else (channel_id,)
        ) as cur:
            channels = await cur.fetchall()
        for row in channels:
            if row["n"] >= full or (row["n"] >= quiet and now - row["newest"] > quiet_seconds):
                async with self.conn.execute(
                    "SELECT * FROM messages WHERE channel_id = ? AND summarized = 0 ORDER BY id LIMIT ?",
                    (row["channel_id"], max_rows),
                ) as cur:
                    return [_row_message(r) for r in await cur.fetchall()]
        return []

    async def mark_summarized(self, ids: list[int]) -> None:
        await self.conn.executemany("UPDATE messages SET summarized = 1 WHERE id = ?", [(i,) for i in ids])
        await self.conn.commit()

    async def prune(self, days: float = 7) -> None:
        cutoff = time.time() - days * 86400
        await self.conn.execute(
            "DELETE FROM messages WHERE created_at < ? AND (summarized = 1 OR created_at < ?)",
            (cutoff, cutoff - 7 * 86400),
        )
        await self.conn.commit()

    # ---- memories ----

    async def add_memory(self, guild_id: int, channel_id: int, content: str, user_ids: list[int],
                         created_at: float | None = None) -> int:
        cur = await self.conn.execute(
            "INSERT INTO memories (guild_id, channel_id, content, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, channel_id, content, created_at or time.time()),
        )
        memory_id = cur.lastrowid
        await self.conn.executemany(
            "INSERT OR IGNORE INTO memory_people (memory_id, user_id) VALUES (?, ?)",
            [(memory_id, uid) for uid in set(user_ids)],
        )
        await self.conn.commit()
        return memory_id

    async def search_memories(self, guild_id: int, words: list[str], limit: int = 3) -> list[Memory]:
        if not words:
            return []
        if self.fts:
            query = " OR ".join('"' + w.replace('"', "") + '"' for w in words)
            sql = ("SELECT m.id, m.content, m.created_at FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                   "WHERE memories_fts MATCH ? AND m.guild_id = ? ORDER BY bm25(memories_fts) LIMIT ?")
            params: tuple = (query, guild_id, limit)
        else:
            sql = ("SELECT id, content, created_at FROM memories WHERE guild_id = ? AND ("
                   + " OR ".join("content LIKE ?" for _ in words) + ") ORDER BY created_at DESC LIMIT ?")
            params = (guild_id, *[f"%{w}%" for w in words], limit)
        async with self.conn.execute(sql, params) as cur:
            return [Memory(r["id"], r["content"], r["created_at"]) for r in await cur.fetchall()]

    async def memories_about(self, guild_id: int, user_id: int, limit: int = 2) -> list[Memory]:
        async with self.conn.execute(
            "SELECT m.id, m.content, m.created_at FROM memories m JOIN memory_people p ON p.memory_id = m.id "
            "WHERE m.guild_id = ? AND p.user_id = ? ORDER BY m.created_at DESC LIMIT ?", (guild_id, user_id, limit),
        ) as cur:
            return [Memory(r["id"], r["content"], r["created_at"]) for r in await cur.fetchall()]

    async def recent_memories(self, guild_id: int, limit: int = 8) -> list[Memory]:
        async with self.conn.execute(
            "SELECT id, content, created_at FROM memories WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?",
            (guild_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [Memory(r["id"], r["content"], r["created_at"]) for r in reversed(rows)]

    # ---- people ----

    async def touch_profile(self, guild_id: int, user_id: int, name: str, *, chatted: bool) -> None:
        now = time.time()
        await self.conn.execute(
            "INSERT INTO profiles (guild_id, user_id, name, chats, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET name = excluded.name, last_seen = excluded.last_seen, "
            "chats = chats + excluded.chats",
            (guild_id, user_id, name, int(chatted), now, now),
        )
        await self.conn.commit()

    async def profile(self, guild_id: int, user_id: int) -> Profile | None:
        async with self.conn.execute(
            "SELECT * FROM profiles WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else _row_profile(row)

    async def people(self, guild_id: int, limit: int = 8) -> list[Profile]:
        """The people Whiskers has seen most recently."""
        async with self.conn.execute(
            "SELECT * FROM profiles WHERE guild_id = ? ORDER BY last_seen DESC LIMIT ?", (guild_id, limit)
        ) as cur:
            return [_row_profile(row) for row in await cur.fetchall()]

    async def chosen_people(self, guild_id: int) -> list[Profile]:
        async with self.conn.execute(
            "SELECT * FROM profiles WHERE guild_id = ? AND chosen = 1 ORDER BY affection DESC", (guild_id,)
        ) as cur:
            return [_row_profile(row) for row in await cur.fetchall()]

    async def save_feelings(self, guild_id: int, user_id: int, affection: float, feelings: list[Feeling],
                            chosen: bool) -> None:
        await self.conn.execute(
            "UPDATE profiles SET affection = ?, feelings = ?, chosen = ? WHERE guild_id = ? AND user_id = ?",
            (affection, json.dumps([asdict(f) for f in feelings]), int(chosen), guild_id, user_id),
        )
        await self.conn.commit()

    async def add_facts(self, guild_id: int, user_id: int, new_facts: list[str]) -> None:
        profile = await self.profile(guild_id, user_id)
        if profile is None:
            return
        facts = list(profile.facts)
        for fact in new_facts:
            fact = " ".join(fact.split())[:200]
            if len(fact) < 4:
                continue
            facts = [f for f in facts if not similar(f, fact)]
            facts.append(fact)
        await self.conn.execute(
            "UPDATE profiles SET facts = ? WHERE guild_id = ? AND user_id = ?",
            (json.dumps(facts[-MAX_FACTS:]), guild_id, user_id),
        )
        await self.conn.commit()

    async def forget_user(self, guild_id: int, user_id: int) -> None:
        await self.conn.execute("DELETE FROM profiles WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        await self.conn.execute("DELETE FROM messages WHERE guild_id = ? AND author_id = ?", (guild_id, user_id))
        await self.conn.execute(
            "DELETE FROM memories WHERE guild_id = ? AND id IN (SELECT memory_id FROM memory_people WHERE user_id = ?)",
            (guild_id, user_id),
        )
        await self.conn.commit()

    async def wipe_memories(self, guild_id: int) -> None:
        for table in ("messages", "memories", "profiles"):
            await self.conn.execute(f"DELETE FROM {table} WHERE guild_id = ?", (guild_id,))
        await self.conn.commit()

    async def counts(self, guild_id: int) -> dict[str, int]:
        result = {}
        for table in ("messages", "memories", "profiles"):
            async with self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE guild_id = ?", (guild_id,)) as cur:
                result[table] = (await cur.fetchone())[0]
        return result

    # ---- journal ----

    async def journal(self, guild_id: int) -> Journal:
        async with self.conn.execute("SELECT * FROM journal WHERE guild_id = ?", (guild_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return Journal(default_journal(self.bot_name), 1, 0.0)
        return Journal(row["text"], row["version"], row["updated_at"], row["since_reflection"], row["reflected_at"],
                       bool(row["can_speak"]), row["reflections"], row["born_at"])

    async def set_journal(self, guild_id: int, text: str, reason: str) -> Journal:
        current = await self.journal(guild_id)
        now = time.time()
        await self.conn.execute(
            "INSERT INTO journal_history (guild_id, text, version, replaced_at, reason) VALUES (?, ?, ?, ?, ?)",
            (guild_id, current.text, current.version, now, reason),
        )
        await self.conn.execute(
            "INSERT INTO journal (guild_id, text, version, updated_at, since_reflection, reflected_at, born_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?) ON CONFLICT (guild_id) DO UPDATE SET text = excluded.text, "
            "version = excluded.version, updated_at = excluded.updated_at, since_reflection = 0, "
            "reflected_at = excluded.reflected_at",
            (guild_id, text, current.version + 1, now, now, now),
        )
        await self.conn.commit()
        return await self.journal(guild_id)

    async def undo_journal(self, guild_id: int) -> Journal | None:
        async with self.conn.execute(
            "SELECT id, text FROM journal_history WHERE guild_id = ? ORDER BY id DESC LIMIT 1", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        journal = await self.set_journal(guild_id, row["text"], "undo")
        # set_journal saved the undone version as history too; drop it and the entry we restored.
        await self.conn.execute(
            "DELETE FROM journal_history WHERE id = ? OR id = (SELECT MAX(id) FROM journal_history WHERE guild_id = ?)",
            (row["id"], guild_id),
        )
        await self.conn.commit()
        return journal

    async def count_cat_message(self, guild_id: int) -> None:
        await self.conn.execute(
            "INSERT INTO journal (guild_id, text, version, updated_at, since_reflection, born_at) "
            "VALUES (?, ?, 1, ?, 1, ?) ON CONFLICT (guild_id) DO UPDATE SET since_reflection = since_reflection + 1",
            (guild_id, default_journal(self.bot_name), time.time(), time.time()),
        )
        await self.conn.commit()

    async def mark_reflected(self, guild_id: int) -> None:
        await self.conn.execute(
            "UPDATE journal SET since_reflection = 0, reflected_at = ?, reflections = reflections + 1 WHERE guild_id = ?",
            (time.time(), guild_id),
        )
        await self.conn.commit()

    async def set_can_speak(self, guild_id: int, can_speak: bool) -> None:
        now = time.time()
        await self.conn.execute(
            "INSERT OR IGNORE INTO journal (guild_id, text, version, updated_at, born_at) VALUES (?, ?, 1, ?, ?)",
            (guild_id, default_journal(self.bot_name), now, now),
        )
        await self.conn.execute("UPDATE journal SET can_speak = ? WHERE guild_id = ?", (int(can_speak), guild_id))
        await self.conn.commit()

    async def reset_journal(self, guild_id: int) -> Journal:
        """Back to a brand-new ordinary cat that has just arrived."""
        await self.set_journal(guild_id, default_journal(self.bot_name), "reset")
        await self.conn.execute(
            "UPDATE journal SET can_speak = 0, reflections = 0, born_at = ? WHERE guild_id = ?", (time.time(), guild_id)
        )
        await self.conn.commit()
        return await self.journal(guild_id)

    async def reflection_due(self, every: int, min_seconds: float) -> list[int]:
        async with self.conn.execute(
            "SELECT guild_id FROM journal WHERE guild_id != 0 AND since_reflection >= ? AND reflected_at < ? "
            "ORDER BY since_reflection DESC",
            (every, time.time() - min_seconds),
        ) as cur:
            return [row["guild_id"] for row in await cur.fetchall()]
