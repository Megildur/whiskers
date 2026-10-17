"""Fast replies: cache slots per conversation, warming them up, and posting before the model is done."""
import asyncio
import dataclasses
import json
from contextlib import asynccontextmanager

from fakes import ScriptedLLM
from whiskers.brain import Brain, Incoming
from whiskers.db import Database, GuildSettings
from whiskers.engine import slot_layout
from whiskers.llm import CHORES, LLMError
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
                     "game but he knows when to stop")[:120]
            assert len(scene) == 120
            return json.dumps({"scene": scene, "reply": "*freezes*", "feeling": "neutral", "about": ""})

    async def go():
        brain = brain_for(cfg, tmp_path, LongScene())
        await brain.db.connect()
        msg = message(mentions_me=True)
        await brain.observe(msg, GuildSettings(), 0)
        await brain.compose(msg, "direct")
        assert brain.last_scene(10) == "Whiskers was having fun with the dare, but Milo's stern tone gives him pause"
        await brain.db.close()

    asyncio.run(go())


def test_scene_note_after_the_message_posts_even_sooner(cfg, tmp_path):
    from whiskers.prompts import reply_schema

    assert list(reply_schema(False)["properties"]) == ["reply", "scene", "feeling", "about"]
    assert reply_schema(True)["properties"]["scene"]["maxLength"] < reply_schema(False)["properties"]["scene"]["maxLength"]

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
        assert llm.calls[0][1]["grammar"].startswith('root ::= message "\\n" note')
        assert "Line 1: your message to the chat, on one line.\nLine 2: in brackets, a private note for next time" \
            in llm.calls[0][0][0]["content"]
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


def test_replies_come_as_four_short_lines_from_llama_cpp_and_json_from_other_servers(cfg, tmp_path):
    class Lines(Model):
        async def answer(self, messages, kwargs):
            self.calls.append((messages, kwargs))
            return "(Brandon pets him; he melts)\n*melts into the hand*\nfeeling: liked\nabout: pets"

    async def go():
        llm = Lines()
        brain = brain_for(cfg, tmp_path, llm)
        await brain.db.connect()
        msg = message(mentions_me=True)
        await brain.observe(msg, GuildSettings(), 0)
        posted = []

        async def ready(text):
            posted.append((text, llm.finished))

        assert await brain.compose(msg, "direct", ready=ready) == "*melts into the hand*"
        assert posted == [("*melts into the hand*", 0)]
        assert brain.last_scene(10) == "Brandon pets him; he melts" and brain.last_feeling(10)[:2] == ("liked", "pets")
        assert llm.calls[0][1]["json_schema"] is None and "Answer in exactly four lines" in llm.calls[0][0][0]["content"]
        await brain.db.close()

        external = brain_for(dataclasses.replace(cfg, llm_backend="external"), tmp_path, Model())
        assert external.reply_grammar is None and external.reply_schema["required"][1] == "reply"

    asyncio.run(go())
