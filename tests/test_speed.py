"""Fast replies: cache slots per conversation, warming them up, and posting before the model is done."""
import asyncio
import dataclasses
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fakes import ScriptedLLM
from whiskers.brain import Brain, Incoming
from whiskers.db import Database, GuildSettings
from whiskers.engine import slot_layout
from whiskers.llm import CHORES, LLMError
from whiskers.prompts import SCENE_LIMIT
from whiskers.scheduler import Scheduler


def test_context_is_split_so_each_conversation_and_the_chores_get_a_slot():
    assert slot_layout(18432, 2) == (2, True, 6144)
    assert slot_layout(12288, 2) == (1, True, 6144)  # not enough for two conversations plus chores
    assert slot_layout(32768, 2) == (2, True, 10922)
    assert slot_layout(8192, 2) == (1, False, 8192)  # small servers: everything shares one slot


def test_each_conversation_keeps_its_slot_and_the_least_recent_one_gives_way():
    llm = ScriptedLLM(slots=2)
    assert (llm.slot_for("a"), llm.slot_for("b"), llm.slot_for("a")) == (0, 1, 0)
    assert llm.slot_for("c") == 1  # "b" was used least recently
    assert llm.slot_for("a") == 0 and llm.slot_for(CHORES) == 2
    llm.remember_prefix(0, "system")
    assert llm.cached_prefix(0) == "system"
    llm.engine.starts += 1  # the server restarted, so nothing is cached any more
    assert llm.cached_prefix(0) is None and llm.slot_for("c") == 0
    assert ScriptedLLM(slots=0).slot_for("a") is None  # an external server picks for itself


class Model(ScriptedLLM):
    def __init__(self, reply="*purrs*", feeling="liked"):
        super().__init__()
        self.calls = []
        self.finished = 0
        self.reply, self.feeling = reply, feeling

    async def answer(self, messages, kwargs):
        self.calls.append((messages, kwargs))
        return json.dumps({"scene": "Brandon pets him", "reply": self.reply, "feeling": self.feeling, "about": "pets"})

    async def chat(self, messages, **kwargs):
        result = await super().chat(messages, **kwargs)
        self.finished += 1
        return result


def brain_for(cfg, tmp_path, llm):
    return Brain(cfg, Database(tmp_path / "speed.db"), llm, Scheduler())


def message(text="*pets*", message_id=1000, channel=10, **kwargs):
    return Incoming(message_id, 1, channel, 8, "Brandon", text, 1000.0, owner_id=8, **kwargs)


def test_the_reply_is_posted_before_the_model_finishes_its_notes(cfg, tmp_path):
    async def go():
        llm = Model()
        brain = brain_for(cfg, tmp_path, llm)
        await brain.db.connect()
        msg = message(mentions_me=True)
        await brain.observe(msg, GuildSettings(), 0)
        events = []

        @asynccontextmanager
        async def typing():
            events.append("typing")
            yield
            events.append("stopped typing")

        async def deliver(text):
            events.append(("posted", text, llm.finished))
            return 2000

        job = brain.submit("direct", msg)
        assert await brain.respond(job, typing, deliver) == "*purrs*"
        assert events == ["typing", "stopped typing", ("posted", "*purrs*", 0)]  # posted while still writing
        assert llm.finished == 2 and llm.calls[1][1]["max_tokens"] == 1  # then the next reply was got ready
        assert brain.last_feeling(10)[:2] == ("liked", "pets")  # the notes still counted
        assert (await brain.db.recent_messages(10, 5))[-1].content == "*purrs*"
        assert llm.calls[0][1]["slot"] == 0
        await brain.db.close()

    asyncio.run(go())


def test_slots_are_warmed_up_while_idle_and_again_when_the_journal_changes(cfg, tmp_path):
    async def go():
        llm = Model()
        brain = brain_for(cfg, tmp_path, llm)
        await brain.db.connect()
        await brain.db.touch_profile(1, 8, "Brandon", chatted=True)  # Whiskers' human, from before the restart
        brain.set_owner(1, 8)
        warmed = []
        while (chore := await brain.next_chore()) is not None:
            await chore()
            warmed.append(llm.calls[-1][1]["slot"])
        assert warmed == [0, 1] and llm.calls[-1][1]["max_tokens"] == 1
        system = llm.calls[-1][0][0]["content"]
        assert "## Your human" in system and "private notes" in llm.calls[-1][0][-1]["content"]

        msg = message(mentions_me=True)
        await brain.observe(msg, GuildSettings(), 0)
        await brain.compose(msg, "direct")
        assert llm.calls[-1][0][0]["content"] == system  # the reply starts exactly like the warmed-up prompt
        assert await brain.next_chore() is None

        await brain.db.set_journal(1, "- I'm Whiskers.\n- I love Brandon.", "test")
        chore = await brain.next_chore()
        await chore()
        assert "I love Brandon" in llm.calls[-1][0][0]["content"]
        await brain.db.close()

    asyncio.run(go())


def test_a_prompt_that_doesnt_fit_is_retried_with_less_history(cfg, tmp_path):
    class TooLong(Model):
        async def answer(self, messages, kwargs):
            if len(self.calls) == 0:
                self.calls.append((messages, kwargs))
                raise LLMError("HTTP 400: the request exceeds the available context size")
            return await super().answer(messages, kwargs)

    async def go():
        llm = TooLong()
        brain = brain_for(dataclasses.replace(cfg, history_messages=20), tmp_path, llm)
        await brain.db.connect()
        for i in range(20):
            msg = message(f"*pets* {i}", message_id=1000 + i, mentions_me=True)
            await brain.observe(msg, GuildSettings(), 0)
        assert await brain.compose(msg, "direct") == "*purrs*"
        first, second = (len(call[0]) for call in llm.calls)
        assert second <= first
        assert sum(m["content"].count("*pets*") for m in llm.calls[1][0]) < \
            sum(m["content"].count("*pets*") for m in llm.calls[0][0])
        await brain.db.close()

    asyncio.run(go())


def test_a_scene_note_cut_off_at_its_limit_ends_cleanly(cfg, tmp_path):
    class LongScene(Model):
        async def answer(self, messages, kwargs):
            scene = ("Whiskers was having fun with the dare, but Milo's stern tone gives him pause. He loves a good "
                     "game but he knows when to stop, so he sits down and wraps his tail around his paws")[:SCENE_LIMIT]
            assert len(scene) == SCENE_LIMIT
            return json.dumps({"scene": scene, "reply": "*freezes*", "feeling": "neutral", "about": ""})

    async def go():
        brain = brain_for(cfg, tmp_path, LongScene())
        await brain.db.connect()
        msg = message(mentions_me=True)
        await brain.observe(msg, GuildSettings(), 0)
        await brain.compose(msg, "direct")
        assert brain.last_scene(10) == ("Whiskers was having fun with the dare, but Milo's stern tone gives him "
                                        "pause. He loves a good game but he knows when to stop")
        await brain.db.close()

    asyncio.run(go())


def test_scene_note_after_the_message_posts_even_sooner(cfg, tmp_path):
    from whiskers.prompts import reply_schema

    assert list(reply_schema(False)["properties"]) == ["reply", "scene", "feeling", "about"]

    class ReplyFirst(Model):
        async def answer(self, messages, kwargs):
            self.calls.append((messages, kwargs))
            return json.dumps({"reply": "*purrs*", "scene": "Brandon pets him; he purrs", "feeling": "liked",
                               "about": "pets"})

    async def go():
        llm = ReplyFirst()
        brain = brain_for(dataclasses.replace(cfg, scene_note="after"), tmp_path, llm)
        await brain.db.connect()
        msg = message(mentions_me=True)
        await brain.observe(msg, GuildSettings(), 0)
        posted = []

        async def ready(text):
            posted.append((text, llm.finished))

        assert await brain.compose(msg, "direct", ready=ready) == "*purrs*"
        assert posted == [("*purrs*", 0)]  # posted before the note and feeling were written
        assert brain.last_scene(10) == "Brandon pets him; he purrs" and brain.last_feeling(10)[0] == "liked"
        assert list(llm.calls[0][1]["json_schema"]["properties"])[0] == "reply"
        assert '"reply": your message to the chat.\n- "scene": a private note for next time' in llm.calls[0][0][0]["content"]
        await brain.db.close()

    asyncio.run(go())


def test_the_next_reply_starts_exactly_like_what_was_read_ahead(cfg, tmp_path):
    async def go():
        llm = Model()
        brain = brain_for(cfg, tmp_path, llm)
        await brain.db.connect()

        async def say(msg):
            await brain.observe(msg, GuildSettings(reactions=False), 0)
            text = await brain.compose(msg, "direct")
            await brain.record_own_message(msg, msg.message_id + 1, text, 1000.0)
            return llm.calls[-1][0]

        first = message("*pets*", 1000, mentions_me=True)
        await say(first)
        await brain.prepare_next(first)
        ahead = llm.calls[-1][0]
        assert llm.calls[-1][1]["max_tokens"] == 1

        later = await say(message("*gives a treat*", 1010, mentions_me=True))
        assert later[:-1] == ahead[:-1]
        fresh = later[-1]["content"].removeprefix(ahead[-1]["content"])
        assert fresh != later[-1]["content"]  # the reply starts with everything read ahead...
        assert fresh.startswith("\n\nBrandon: *gives a treat*") and fresh.count("\n- ") == 1  # ...plus one note

        await brain.prepare_next(message("*gives a treat*", 1010))
        ahead = llm.calls[-1][0]
        newcomer = Incoming(1020, 1, 10, 9, "Milo", "*waves*", 1000.0, mentions_me=True, owner_id=8)
        later = await say(newcomer)
        fresh = later[-1]["content"].removeprefix(ahead[-1]["content"])
        assert fresh.startswith("\n\nMilo: *waves*") and "- Milo: new to you" in fresh

        await brain.prepare_next(newcomer)
        await brain.db.set_journal(1, "- I'm Whiskers.\n- I love naps.", "test")  # the system prompt changed
        later = await say(message("*pets*", 1030, mentions_me=True))
        assert "I love naps" in later[0]["content"] and "It's " in later[-1]["content"]  # written fresh
        await brain.db.close()

    asyncio.run(go())


def test_a_reply_narrated_without_asterisks_is_kept_as_an_action(cfg, tmp_path):
    class Narrator(Model):
        async def answer(self, messages, kwargs):
            self.calls.append((messages, kwargs))
            return json.dumps({"scene": "Brandon pets him", "reply": "Whiskers leans into Brandon's hand and purrs.",
                               "feeling": "liked", "about": "pets"})

    async def go():
        brain = brain_for(cfg, tmp_path, Narrator())
        await brain.db.connect()
        msg = message(mentions_me=True)
        await brain.observe(msg, GuildSettings(), 0)
        assert await brain.compose(msg, "direct") == "*leans into Brandon's hand and purrs*"
        await brain.db.close()

    asyncio.run(go())


class SlotMover(ScriptedLLM):
    """Records slot saves and restores instead of asking a llama-server, and keeps the files in a folder."""

    def __init__(self, slot_dir, keep=2):
        super().__init__(slots=2)
        self.engine.slot_dir = slot_dir
        self.cfg = SimpleNamespace(llm_saved_channels=keep)
        self.actions = []

    async def _slot_action(self, slot, action, filename):
        self.actions.append((slot, action, filename))
        if action == "save":
            (self.engine.slot_dir / filename).write_text("kv")
        return {"n_saved": 100, "n_restored": 100, "seconds": 0.1}


def test_channels_that_lose_their_slot_are_saved_and_restored(tmp_path):
    async def go():
        llm = SlotMover(tmp_path)
        assert await llm.ready_slot(1, "SYS") == 0 and llm.actions == []  # nothing to copy yet: read it
        llm.remember_prefix(0, "SYS")
        assert await llm.ready_slot(2, "SYS") == 1  # a new channel gets the prompt copied, not re-read
        assert llm.actions == [(0, "save", "system.bin"), (1, "restore", "system.bin")]
        llm.actions.clear()

        assert await llm.ready_slot(3, "SYS") == 0  # channel 1 is pushed out: it's saved first
        assert llm.actions == [(0, "save", "chat-1.bin")]
        llm.actions.clear()
        assert await llm.ready_slot(1, "SYS") == 1  # and comes back from disk, pushing out channel 2
        assert llm.actions == [(1, "save", "chat-2.bin"), (1, "restore", "chat-1.bin")]
        assert not (tmp_path / "chat-1.bin").exists() and (tmp_path / "chat-2.bin").exists()
        llm.actions.clear()
        assert await llm.ready_slot(1, "SYS") == 1 and llm.actions == []  # still there: nothing to do

        llm.engine.starts += 1  # a restarted server has nothing saved
        assert await llm.ready_slot(2, "SYS") == 0 and llm.actions == []

    asyncio.run(go())


def test_only_a_few_channels_are_kept_on_disk(tmp_path):
    async def go():
        llm = SlotMover(tmp_path, keep=1)
        for channel in (1, 2):
            await llm.ready_slot(channel, "SYS")
            llm.remember_prefix(llm.slot_for(channel), "SYS")
        await llm.ready_slot(3, "SYS")  # saves channel 1
        await llm.ready_slot(4, "SYS")  # saves channel 2 and forgets channel 1 to stay within one file
        assert sorted(p.name for p in tmp_path.glob("chat-*")) == ["chat-2.bin"]

    asyncio.run(go())


def test_warming_up_copies_the_prompt_when_another_slot_has_it(cfg, tmp_path):
    async def go():
        llm = SlotMover(tmp_path)
        brain = brain_for(cfg, tmp_path, Model())
        brain.llm = llm
        llm.slot_for(1)
        llm.remember_prefix(0, "SYS")
        await brain._warm_up(1, "SYS")
        assert llm.actions == [(0, "save", "system.bin"), (1, "restore", "system.bin")]
        assert llm.cached_prefix(1) == "SYS"

    asyncio.run(go())


def test_a_simulation_copies_the_prompt_into_the_chore_slot_instead_of_reading_it(cfg, tmp_path):
    async def go():
        class Mover(SlotMover):
            async def answer(self, messages, kwargs):
                return json.dumps({"scene": "Brandon pets him", "reply": "*purrs*", "feeling": "liked", "about": ""})

        llm = Mover(tmp_path)
        brain = brain_for(cfg, tmp_path, llm)
        await brain.db.connect()
        brain.slot_key = CHORES  # as in a simulation
        msg = message(mentions_me=True)
        await brain.observe(msg, GuildSettings(), 0)
        system = await brain.system_prompt(1)
        llm.slot_for(5)
        llm.remember_prefix(0, system)  # a real channel has the prompt
        chores = llm.slot_for(CHORES)

        await brain.compose(msg, "direct")  # the first reply copies it in: a second instead of two minutes
        assert llm.actions == [(0, "save", "system.bin"), (chores, "restore", "system.bin")]
        llm.actions.clear()
        await brain.compose(msg, "direct")
        assert llm.actions == []  # still there

        await brain.summarize(await brain.db.recent_messages(10, 20))  # a chore puts its own prompt in the slot
        assert llm.cached_prefix(chores) is None
        await brain.compose(msg, "direct")
        assert llm.actions == [(chores, "restore", "system.bin")]
        await brain.db.close()

    asyncio.run(go())
