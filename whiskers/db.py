"""Everything Whiskers remembers, in one SQLite file (data/whiskers.db).

- guild_settings / channel_rules: where Whiskers may talk and how chatty it is.
- messages: recent chat in the channels Whiskers is allowed in (pruned after a week).
- memories: short diary entries summarised from that chat, searchable by keyword.
- profiles: what Whiskers knows about each person.
- journal: who Whiskers has become, rewritten as the server shapes it (old versions kept for undo).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

log = logging.getLogger("whiskers.db")

MAX_FACTS = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'allowlist',
    chattiness INTEGER NOT NULL DEFAULT 5,
    reactions INTEGER NOT NULL DEFAULT 1,
    name_trigger INTEGER NOT NULL DEFAULT 1,
    ping_on_reply INTEGER NOT NULL DEFAULT 0
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
    summarized INTEGER NOT NULL DEFAULT 0
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
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS journal (
    guild_id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL,
    since_reflection INTEGER NOT NULL DEFAULT 0,
    reflected_at REAL NOT NULL DEFAULT 0
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
        "- I don't know anyone here yet."
    )


@dataclass
class GuildSettings:
    mode: str = "allowlist"  # "allowlist": only allowed channels. "everywhere": all except blocked ones.
    chattiness: int = 5  # % chance to join a conversation uninvited
    reactions: bool = True
    name_trigger: bool = True  # saying the cat's name counts as talking to it
    ping_on_reply: bool = False


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
class Profile:
    user_id: int
    name: str
    facts: list[str] = field(default_factory=list)
    chats: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0


@dataclass
class Journal:
    text: str
    version: int
    updated_at: float
    since_reflection: int = 0
    reflected_at: float = 0.0


def _row_message(row) -> StoredMessage:
    return StoredMessage(row["id"], row["guild_id"], row["channel_id"], row["message_id"], row["author_id"],
                         row["author_name"], bool(row["is_self"]), row["content"], row["created_at"])


def _similar(a: str, b: str) -> bool:
    a, b = a.lower().strip(" ."), b.lower().strip(" .")
    return a == b or a in b or b in a


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
        try:
            await self._conn.executescript(FTS_SCHEMA)
        except aiosqlite.OperationalError as error:
            self.fts = False
            log.warning("SQLite full-text search isn't available (%s); memory search will be simpler.", error)
        await self._conn.commit()

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
            row["mode"], row["chattiness"], bool(row["reactions"]), bool(row["name_trigger"]), bool(row["ping_on_reply"]),
        )
        self._settings[guild_id] = settings
        return settings

    async def update_guild_settings(self, guild_id: int, **changes) -> GuildSettings:
        allowed = {"mode", "chattiness", "reactions", "name_trigger", "ping_on_reply"}
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
                          content: str, created_at: float, is_self: bool = False) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO messages (guild_id, channel_id, message_id, author_id, author_name, is_self, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, message_id, author_id, author_name, int(is_self), content[:1000], created_at),
        )
        await self.conn.commit()

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

    async def recent_guild_messages(self, guild_id: int, limit: int) -> list[StoredMessage]:
        async with self.conn.execute(
            "SELECT * FROM messages WHERE guild_id = ? ORDER BY id DESC LIMIT ?", (guild_id, limit)
        ) as cur:
            rows = await cur.fetchall()
        return [_row_message(row) for row in reversed(rows)]

    async def summary_batch(self, *, full: int = 30, quiet: int = 6, quiet_seconds: float = 2700,
                            max_rows: int = 40) -> list[StoredMessage]:
        """The next chunk of chat to turn into memories: a busy channel, or one that has gone quiet."""
        now = time.time()
        async with self.conn.execute(
            "SELECT channel_id, COUNT(*) AS n, MAX(created_at) AS newest FROM messages "
            "WHERE summarized = 0 GROUP BY channel_id"
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
        if row is None:
            return None
        return Profile(user_id, row["name"], json.loads(row["facts"]), row["chats"], row["first_seen"], row["last_seen"])

    async def add_facts(self, guild_id: int, user_id: int, new_facts: list[str]) -> None:
        profile = await self.profile(guild_id, user_id)
        if profile is None:
            return
        facts = list(profile.facts)
        for fact in new_facts:
            fact = " ".join(fact.split())[:200]
            if len(fact) < 4:
                continue
            facts = [f for f in facts if not _similar(f, fact)]
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
        return Journal(row["text"], row["version"], row["updated_at"], row["since_reflection"], row["reflected_at"])

    async def set_journal(self, guild_id: int, text: str, reason: str) -> Journal:
        current = await self.journal(guild_id)
        now = time.time()
        await self.conn.execute(
            "INSERT INTO journal_history (guild_id, text, version, replaced_at, reason) VALUES (?, ?, ?, ?, ?)",
            (guild_id, current.text, current.version, now, reason),
        )
        await self.conn.execute(
            "INSERT INTO journal (guild_id, text, version, updated_at, since_reflection, reflected_at) "
            "VALUES (?, ?, ?, ?, 0, ?) ON CONFLICT (guild_id) DO UPDATE SET text = excluded.text, "
            "version = excluded.version, updated_at = excluded.updated_at, since_reflection = 0, "
            "reflected_at = excluded.reflected_at",
            (guild_id, text, current.version + 1, now, now),
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
            "INSERT INTO journal (guild_id, text, version, updated_at, since_reflection) VALUES (?, ?, 1, ?, 1) "
            "ON CONFLICT (guild_id) DO UPDATE SET since_reflection = since_reflection + 1",
            (guild_id, default_journal(self.bot_name), time.time()),
        )
        await self.conn.commit()

    async def mark_reflected(self, guild_id: int) -> None:
        await self.conn.execute(
            "UPDATE journal SET since_reflection = 0, reflected_at = ? WHERE guild_id = ?", (time.time(), guild_id)
        )
        await self.conn.commit()

    async def reflection_due(self, every: int, min_seconds: float) -> list[int]:
        async with self.conn.execute(
            "SELECT guild_id FROM journal WHERE guild_id != 0 AND since_reflection >= ? AND reflected_at < ? "
            "ORDER BY since_reflection DESC",
            (every, time.time() - min_seconds),
        ) as cur:
            return [row["guild_id"] for row in await cur.fetchall()]
