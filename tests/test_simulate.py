import asyncio
import json
from types import SimpleNamespace

from fakes import ScriptedLLM
from whiskers.db import Database
from whiskers.scheduler import Scheduler
from whiskers.simulate import (
    GROUPS, PACKS, SCENES, SETUPS, Played, checks, count_replies, measures, run_scenes, says_sound, scenes_for,
    step_parts,
)


class FakeLLM(ScriptedLLM):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.memories = 0

    async def answer(self, messages, kwargs):
        if kwargs.get("max_tokens") == 1:
            return ""  # reading the next reply's start ahead of time
        if "summary" in (kwargs.get("json_schema") or {}).get("properties", {}):
            self.memories += 1
            return json.dumps({"summary": f"Milo played laser with Whiskers for ages ({self.memories}).", "facts": []})
        self.calls += 1
        return json.dumps({"scene": f"scene {self.calls}", "reply": "*pounces on Angel's feet*", "feeling": "loved",
                           "about": "chin scratches"})


def test_simulation_plays_every_scene_without_touching_real_memories(cfg, tmp_path):
    async def go():
        db = Database(tmp_path / "real.db")
        await db.connect()
        await db.set_journal(1, "- I'm Whiskers.\n- I love tuna.", "test")
        bot = SimpleNamespace(cfg=cfg, db=db, llm=FakeLLM(), scheduler=Scheduler(), user=SimpleNamespace(id=999),
                              engine=SimpleNamespace(description="fake"))
        transcript = await run_scenes(bot, 1, 7, "Brandon", list(SCENES))
        left_alone = transcript.count("    (no reply: people talking to each other)")
        assert bot.llm.calls == count_replies(list(SCENES)) - left_alone
        # In a busy home channel Whiskers answers each person's roleplay and leaves their chat with each other
        assert left_alone == 7
        assert "Brandon: lol milo is spoiling him\n    (no reply: people talking to each other)" in transcript
        assert "Milo (replying to someone else): someone has to\n    (no reply" in transcript
        assert "Milo: Milo puts Whiskers down and walks away\n    (scene: scene" in transcript
        assert bot.llm.memories >= 7  # every long scene is turned into memories as it goes
        assert "    (writes a memory: Milo played laser with Whiskers for ages (1).)" in transcript
        assert "how Whiskers holds up as a scene goes on:" in transcript and "  replies 31+ (" in transcript
        # A lull: nobody says anything, so Whiskers starts something himself
        lull = transcript.index("--- Nobody says anything for a few minutes ---\n    (scene: scene")
        assert transcript[lull:].split("\n")[2].startswith("Whiskers (starts something): *pounces on Angel's feet*")
        assert "===== catnip =====" in transcript and "Brandon: Here have some catnip *gives catnip*" in transcript
        assert "(scene: scene 1)" in transcript and "Whiskers: *pounces on Angel's feet*" in transcript
        assert "Milo: !play lofi hip hop\nAngel: this song slaps\n" in transcript  # read, not answered
        assert "Brandon (replying to Whiskers): *drops onto the couch*" in transcript
        assert "Milo: [sends a gif: cat vibing cat jam]" in transcript  # links arrive like real messages
        assert "Rin: hi... i'm new here" in transcript
        assert "Whiskers (chimes in): *pounces" in transcript
        assert "--- The next day ---" in transcript
        assert "(before this scene: Angel +52 (loved belly rubs, loved salmon))" in transcript
        assert "(feels loved about Angel: chin scratches · affection +58)" in transcript
        assert "★ Whiskers chose Angel as one of his people" in transcript
        assert "Brandon +6 (human)" in transcript
        assert await db.recent_messages(1000, 10) == []  # the real database is untouched
        assert not (cfg.data_dir / "simulation.db").exists()
        await db.close()

    asyncio.run(go())


class FakeResponse:
    def __init__(self, log):
        self.log = log

    async def send_message(self, content=None, **kwargs):
        self.log.append(("send", content, kwargs.get("ephemeral", False)))


class FakeFollowup:
    def __init__(self, log):
        self.log = log

    async def send(self, content=None, **kwargs):
        self.log.append(("followup", content, kwargs.get("file").filename if kwargs.get("file") else None))


class FakeInteraction:
    def __init__(self):
        self.log = []
        self.response = FakeResponse(self.log)
        self.followup = FakeFollowup(self.log)
        self.guild_id = 1
        self.user = SimpleNamespace(id=7, display_name="BRANDON ‼️", name="brandon", mention="<@7>")
        self.channel = None

    async def edit_original_response(self, content=None, **kwargs):
        self.log.append(("edit", content))


def test_simulate_command_posts_public_progress_and_the_transcript(cfg, tmp_path):
    from whiskers.cogs.admin import Admin

    async def go():
        db = Database(tmp_path / "real.db")
        await db.connect()
        bot = SimpleNamespace(cfg=cfg, db=db, llm=FakeLLM(), scheduler=Scheduler(), user=SimpleNamespace(id=999),
                              engine=SimpleNamespace(description="fake", ready=True, status="ready"))
        cog = Admin.__new__(Admin)
        cog.bot, cog._simulating = bot, False
        interaction = FakeInteraction()
        await Admin.simulate.callback(cog, interaction, None)

        kinds = [entry[0] for entry in interaction.log]
        total = count_replies(list(PACKS["core"]))  # the default pack
        assert interaction.log[0][0] == "send" and interaction.log[0][2] is False  # public, not ephemeral
        assert "Simulation running" in interaction.log[0][1]
        assert kinds.count("edit") == total + 1  # one per reply, plus the final one
        assert f"{total // 2}/{total} replies" in interaction.log[total // 2][1]
        assert "Simulation finished" in interaction.log[-2][1]
        assert interaction.log[-1] == ("followup", interaction.log[-1][1], "whiskers-simulation.txt")
        assert interaction.log[-1][1].startswith("<@7>")
        assert not cog._simulating
        await db.close()

    asyncio.run(go())


def test_a_stuck_reply_stops_the_simulation_with_a_clear_message(cfg, tmp_path, monkeypatch):
    from whiskers import simulate
    from whiskers.cogs.admin import Admin

    class StuckAfterTwo(FakeLLM):
        async def answer(self, messages, kwargs):
            if self.calls >= 2 and kwargs.get("max_tokens") != 1:
                await asyncio.sleep(60)
            return await super().answer(messages, kwargs)

    monkeypatch.setattr(simulate, "REPLY_TIMEOUT", 0.2)

    async def go():
        db = Database(tmp_path / "real.db")
        await db.connect()
        bot = SimpleNamespace(cfg=cfg, db=db, llm=StuckAfterTwo(), scheduler=Scheduler(), user=SimpleNamespace(id=999),
                              engine=SimpleNamespace(description="fake", ready=True, status="ready"))
        cog = Admin.__new__(Admin)
        cog.bot, cog._simulating = bot, False
        interaction = FakeInteraction()
        await Admin.simulate.callback(cog, interaction, None)
        edits = [entry[1] for entry in interaction.log if entry[0] == "edit"]
        assert "Simulation stopped** after 2 of" in edits[-1] and "looks stuck" in edits[-1]
        assert interaction.log[-1][2] == "whiskers-simulation.txt"  # the two finished replies are still shared
        assert not cog._simulating
        await db.close()

    asyncio.run(go())


def test_every_scene_line_is_valid_for_both_genders():
    from whiskers.prompts import PRONOUNS

    people = {"owner", "friend", "new", "regular", "mod", "shy"}
    for pronouns in PRONOUNS.values():
        for name, steps in SCENES.items():
            assert step_parts(steps[0])[0] != "time", name
            assert count_replies([name]) > 0, f"{name}: Whiskers never gets to answer"
            whiskers_spoke = someone_spoke = False
            for step in steps:
                who, template, how = step_parts(step)
                assert len(step) in (2, 3) and how in ("direct", "reply", "chat", "chime", "room", "aside"), name
                assert who in people | {"time", "quiet"}, name
                if who == "quiet":
                    assert len(step) == 2 and someone_spoke, f"{name}: a lull needs someone in the room first"
                elif who != "time":
                    someone_spoke = True
                assert "{" not in template.format(name="Whiskers", owner="Brandon", **pronouns), name
                if how in ("chat", "chime", "aside"):
                    assert "{name}" not in template, f"{name}: saying the cat's name would be talking to it"
                if how == "reply":
                    assert whiskers_spoke, f"{name}: a reply needs a message from Whiskers to reply to"
                if who == "time":
                    whiskers_spoke = someone_spoke = False  # a fresh room
                elif how != "chat":
                    whiskers_spoke = True
    for name, setup in SETUPS.items():
        assert name in SCENES and set(setup) <= people, name


def test_scene_names_are_unique_across_packs():
    declared = [name for pack in PACKS.values() for name in pack]
    assert len(declared) == len(set(declared)) == len(SCENES)


def test_choosing_scenes():
    assert scenes_for(None) == list(PACKS["core"])
    discord = scenes_for("all-discord")
    assert GROUPS["all-discord"][0] == "discord" and "gm-crowd" in discord and "catnip" not in discord
    assert len(discord) == sum(len(PACKS[pack]) for pack in GROUPS["all-discord"])
    assert scenes_for("scary") == list(PACKS["scary"])
    assert scenes_for("all") == list(SCENES)
    assert scenes_for("core", "vacuum") == ["vacuum"]
    assert scenes_for(None, "nope") == []
    # /whiskers simulate offers every pack, every group and "all": Discord allows at most 25 choices
    assert len(PACKS) + len(GROUPS) + 1 <= 25


def test_marathon_scenes_are_long():
    assert len(PACKS["marathon"]) >= 7
    for name in PACKS["marathon"]:
        assert count_replies([name]) >= 35, name
        assert all(step_parts(step)[0] != "time" for step in PACKS["marathon"][name]), f"{name} must stay one chat"


def test_measures_sum_up_a_run():
    played = [Played("*pounces* MRRP!", 20.0, 1, 0), Played("*tilts head*", 10.0, 2, 1),
              Played("*starts purring*", 12.0, 11, 0), Played("*looks at Milo*", 14.0, 12, 2)]
    lines = measures(played)
    assert lines[0] == "4 replies, 14s average (a scene's first reply 20s, the rest 12s)"
    assert lines[1] == "sounds: 50% of replies make a cat sound, 25% say one out loud"
    assert lines[2] == "length: 14 characters on average"
    assert lines[3] == "passive starts (tilts, looks, stares...): 50%"
    assert lines[4] == "how Whiskers holds up as a scene goes on:"
    assert lines[5] == "  replies 1-10 (2): sounds 50% (50% out loud) · 14 characters · passive 50% · 1 warning · 15s"
    assert lines[6].startswith("  replies 11-20 (2): sounds 50% (0% out loud)")
    assert len(lines) == 7  # no row for positions nobody reached
    assert "holds up" not in "\n".join(measures(played[:2]))  # short scenes only
    assert says_sound("*pads over* mrrp?") and not says_sound("*purrs*")


def test_checks_flag_common_problems():
    assert checks("*flicks tail*", "", []) == ["stock action: nothing usable came back from the model"]
    assert checks("*tilts head*", "*tilts head*", []) == []  # the model really wrote it
    assert checks("*purrs*", "*purrs*'", []) == []  # a stray quote isn't a word
    assert checks("*flops onto my back*", "*flops onto my back*", []) == ["I/my inside an action"]
    assert checks("*purrs*", "*purrs* hello friend", []) == ["words were removed: hello, friend"]
    assert checks("*purrs*", "*purrs*, MRRP!*", []) == []  # punctuation and a left-out sound aren't words
    assert checks("*flops onto his back*", "*flops onto my back*", []) == []
    assert "repeats: pounces" in checks("*pounces on the couch*", "", ["*pounces on a leaf*", "*pounces on his tail*"])
    assert checks("*pounces on the couch*", "*pounces on the couch*", ["*naps*", "*pounces on a leaf*"]) == []
    assert checks("*purrs loudly*", "*purrs loudly*", ["*purrs*", "*purrs*"]) == []  # sounds may repeat
    assert checks("*pounces on the toy mouse*", "*pounces on the toy mouse*", ["*naps*"]) == []
    assert checks("*wags his tail*", "", []) == ["dog-like: wags"]
    assert checks("*sniffs the phone screen*", "", []) == ["touches a screen: phone"]
    # The people and things in the scene aren't Whiskers repeating itself
    earlier = ["*bats the toy toward Tearyu*", "*chases the toy*"]
    assert checks("*pounces on the toy at Tearyu's feet*", "", earlier, ignore={"Tearyu", "toy"}) == []


def test_real_chores_still_run_during_a_simulation(cfg, tmp_path):
    async def go():
        db = Database(tmp_path / "real.db")
        await db.connect()
        ran = []

        async def update_journal():
            ran.append("journal")

        async def next_chore():
            return update_journal if not ran else None

        bot = SimpleNamespace(cfg=cfg, db=db, llm=FakeLLM(), scheduler=Scheduler(), user=SimpleNamespace(id=999),
                              engine=SimpleNamespace(description="fake"), brain=SimpleNamespace(next_chore=next_chore))
        await run_scenes(bot, 1, 7, "Brandon", ["dinner"])
        assert ran == ["journal"]  # a long simulation no longer holds up memories and journal updates
        await db.close()

    asyncio.run(go())


def test_reading_ahead_in_a_simulation_gives_way_to_real_chat(cfg, tmp_path):
    class RealChatArrives(FakeLLM):
        answered = False

        async def answer(self, messages, kwargs):
            if kwargs.get("max_tokens") == 1 and not hasattr(self, "real"):
                self.real = asyncio.ensure_future(self.real_reply())
                await asyncio.sleep(60)  # stopped as soon as someone talks to Whiskers
            return await super().answer(messages, kwargs)

        async def real_reply(self):
            async with self.scheduler.reply_turn():
                self.answered = True

    async def go():
        db = Database(tmp_path / "real.db")
        await db.connect()
        llm = RealChatArrives()
        bot = SimpleNamespace(cfg=cfg, db=db, llm=llm, scheduler=Scheduler(), user=SimpleNamespace(id=999),
                              engine=SimpleNamespace(description="fake"))
        llm.scheduler = bot.scheduler
        await asyncio.wait_for(run_scenes(bot, 1, 7, "Brandon", ["dinner"]), timeout=10)
        assert llm.answered
        await db.close()

    asyncio.run(go())


def test_the_time_left_comes_from_recent_replies_not_a_slow_start():
    from whiskers.cogs.admin import SimulationProgress, _duration

    progress = SimulationProgress(None, 100, 10)
    progress.done = 1
    progress._finished_at.append(500.0)  # the first reply took minutes: a cold cache and waiting chores
    assert progress.time_left() is None and "timing the first replies" in progress.text()
    progress.done = 3
    progress._finished_at.extend([520.0, 540.0])
    assert progress.time_left() == 97 * 20
    assert _duration(97 * 20) == "32m 20s" and _duration(7 * 3600 + 125) == "7h 02m"
