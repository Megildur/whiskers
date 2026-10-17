import asyncio
import time

from whiskers.db import Database, default_journal


def run(coro):
    return asyncio.run(coro)


async def open_db(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.connect()
    return db


def test_settings_and_channel_rules(tmp_path):
    async def go():
        db = await open_db(tmp_path)
        assert (await db.guild_settings(1)).mode == "allowlist"
        await db.update_guild_settings(1, mode="everywhere", chattiness=20, owner_id=42)
        settings = await db.guild_settings(1)
        assert (settings.mode, settings.chattiness, settings.owner_id) == ("everywhere", 20, 42)

        await db.set_channel_rule(1, 10, rule="allow")
        await db.set_channel_rule(1, 10, chattiness=50)  # keeps the rule
        rule = (await db.channel_rules(1))[10]
        assert (rule.rule, rule.chattiness) == ("allow", 50)
        await db.set_channel_rule(1, 10, rule=None, chattiness=None)
        assert 10 not in await db.channel_rules(1)
        await db.close()

    run(go())


def test_memory_search_and_forgetting_a_person(tmp_path):
    async def go():
        db = await open_db(tmp_path)
        await db.touch_profile(1, 7, "Alice", chatted=True)
        await db.add_memory(1, 10, "Alice gave Whiskers a laser pointer.", [7])
        await db.add_memory(1, 10, "Bob talked about his new bike.", [8])
        await db.add_memory(2, 20, "A laser show in another server.", [])

        found = await db.search_memories(1, ["laser"])
        assert [m.content for m in found] == ["Alice gave Whiskers a laser pointer."]
        assert [m.content for m in await db.memories_about(1, 7)] == ["Alice gave Whiskers a laser pointer."]

        await db.add_message(1, 10, 555, 7, "Alice", "hi", time.time())
        await db.forget_user(1, 7)
        assert await db.profile(1, 7) is None
        assert await db.search_memories(1, ["laser"]) == []
        assert await db.recent_messages(10, 10) == []
        assert len(await db.search_memories(1, ["bike"])) == 1
        await db.close()

    run(go())


def test_facts_merge_without_duplicates(tmp_path):
    async def go():
        db = await open_db(tmp_path)
        await db.touch_profile(1, 7, "Alice", chatted=False)
        await db.add_facts(1, 7, ["Works night shifts", "Has a dog"])
        await db.add_facts(1, 7, ["works night shifts.", "Has a dog named Rex"])
        assert (await db.profile(1, 7)).facts == ["works night shifts.", "Has a dog named Rex"]
        await db.close()

    run(go())


def test_journal_history_and_undo(tmp_path):
    async def go():
        db = await open_db(tmp_path)
        assert (await db.journal(1)).text == default_journal("Whiskers")
        await db.set_journal(1, "- v2", "test")
        await db.set_journal(1, "- v3", "test")
        assert (await db.journal(1)).version == 3
        assert (await db.undo_journal(1)).text == "- v2"
        assert (await db.undo_journal(1)).text == default_journal("Whiskers")
        assert await db.undo_journal(1) is None
        await db.close()

    run(go())


def test_reflection_becomes_due_after_enough_cat_messages(tmp_path):
    async def go():
        db = await open_db(tmp_path)
        for _ in range(3):
            await db.count_cat_message(1)
        assert await db.reflection_due(every=3, min_seconds=0) == [1]
        await db.mark_reflected(1)
        assert await db.reflection_due(every=3, min_seconds=0) == []
        await db.close()

    run(go())


def test_summary_batches_busy_or_quiet_channels(tmp_path):
    async def go():
        db = await open_db(tmp_path)
        now = time.time()
        for i in range(5):
            await db.add_message(1, 10, i, 7, "Alice", f"busy {i}", now)
        for i in range(6):
            await db.add_message(1, 20, 100 + i, 7, "Alice", f"old {i}", now - 3600)
        batch = await db.summary_batch(full=30, quiet=6, quiet_seconds=2700)
        assert [m.channel_id for m in batch] == [20] * 6
        await db.mark_summarized([m.id for m in batch])
        assert await db.summary_batch(full=5) != []  # channel 10 now counts as busy enough
        await db.close()

    run(go())


def test_old_databases_get_the_new_journal_columns(tmp_path):
    import sqlite3

    old = sqlite3.connect(tmp_path / "test.db")
    old.execute("CREATE TABLE journal (guild_id INTEGER PRIMARY KEY, text TEXT NOT NULL, version INTEGER NOT NULL "
                "DEFAULT 1, updated_at REAL NOT NULL, since_reflection INTEGER NOT NULL DEFAULT 0, "
                "reflected_at REAL NOT NULL DEFAULT 0)")
    old.execute("INSERT INTO journal (guild_id, text, updated_at) VALUES (1, '- old', 5)")
    old.execute("CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'allowlist', "
                "chattiness INTEGER NOT NULL DEFAULT 5, reactions INTEGER NOT NULL DEFAULT 1, name_trigger INTEGER "
                "NOT NULL DEFAULT 1, ping_on_reply INTEGER NOT NULL DEFAULT 0)")
    old.execute("INSERT INTO guild_settings (guild_id, chattiness) VALUES (1, 12)")
    old.execute("CREATE TABLE profiles (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, name TEXT NOT NULL, "
                "facts TEXT NOT NULL DEFAULT '[]', chats INTEGER NOT NULL DEFAULT 0, first_seen REAL NOT NULL, "
                "last_seen REAL NOT NULL, PRIMARY KEY (guild_id, user_id))")
    old.execute("INSERT INTO profiles (guild_id, user_id, name, chats, first_seen, last_seen) VALUES (1, 5, 'Angel', 9, 1, 2)")
    old.commit()
    old.close()

    async def go():
        db = await open_db(tmp_path)
        journal = await db.journal(1)
        assert (journal.text, journal.can_speak, journal.reflections) == ("- old", False, 0)
        assert journal.born_at > 0
        settings = await db.guild_settings(1)
        assert (settings.chattiness, settings.owner_id) == (12, 0)
        angel = await db.profile(1, 5)
        assert (angel.chats, angel.affection, angel.feelings, angel.chosen) == (9, 0.0, [], False)
        await db.close()

    run(go())


def test_speech_switch_and_reset(tmp_path):
    async def go():
        db = await open_db(tmp_path)
        await db.set_can_speak(1, True)
        assert (await db.journal(1)).can_speak
        await db.mark_reflected(1)
        assert (await db.journal(1)).reflections == 1
        journal = await db.reset_journal(1)
        assert (journal.can_speak, journal.reflections, journal.text) == (False, 0, default_journal("Whiskers"))
        await db.close()

    run(go())


def test_feelings_and_chosen_people_are_saved(tmp_path):
    from whiskers.db import Feeling

    async def go():
        db = await open_db(tmp_path)
        await db.touch_profile(1, 5, "Angel", chatted=True)
        await db.touch_profile(1, 6, "Tearyu", chatted=True)
        await db.save_feelings(1, 5, 64.0, [Feeling("belly rubs", "loved", 100.0)], True)
        await db.save_feelings(1, 6, -20.0, [Feeling("tail pulling", "hated", 200.0)], False)
        angel = await db.profile(1, 5)
        assert (angel.affection, angel.chosen, angel.feelings) == (64.0, True, [Feeling("belly rubs", "loved", 100.0)])
        assert [p.name for p in await db.chosen_people(1)] == ["Angel"]
        assert {p.name for p in await db.people(1)} == {"Angel", "Tearyu"}
        await db.close()

    run(go())
