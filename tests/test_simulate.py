import asyncio
import json
from types import SimpleNamespace

from fakes import ScriptedLLM
from whiskers.db import Database
from whiskers.scheduler import Scheduler
from whiskers.simulate import (
    GROUPS, PACKS, SCENES, SETUPS, checks, count_replies, run_scenes, scenes_for, step_parts,
)


class FakeLLM(ScriptedLLM):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def answer(self, messages, kwargs):
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
        assert bot.llm.calls == count_replies(list(SCENES))
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
            if self.calls >= 2:
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
            whiskers_spoke = False
            for step in steps:
                who, template, how = step_parts(step)
                assert len(step) in (2, 3) and how in ("direct", "reply", "chat", "chime"), name
                assert who in people | {"time"}, name
                assert "{" not in template.format(name="Whiskers", owner="Brandon", **pronouns), name
                if how in ("chat", "chime"):
                    assert "{name}" not in template, f"{name}: saying the cat's name would be talking to it"
                if how == "reply":
                    assert whiskers_spoke, f"{name}: a reply needs a message from Whiskers to reply to"
                if who == "time":
                    whiskers_spoke = False  # a fresh room
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


def test_checks_flag_common_problems():
    assert checks("*flicks tail*", "", []) == ["stock action: nothing usable came back from the model"]
    assert checks("*flops onto my back*", "*flops onto my back*", []) == ["I/my inside an action"]
    assert checks("*purrs*", "*purrs* hello friend", []) == ["words were removed: hello, friend"]
    assert checks("*purrs*", "*purrs*, MRRP!*", []) == []  # punctuation and a left-out sound aren't words
    assert checks("*flops onto his back*", "*flops onto my back*", []) == []
    assert "repeats: purrs" in checks("*purrs loudly*", "", ["*purrs*", "*naps*"])
    assert checks("*pounces on the toy mouse*", "*pounces on the toy mouse*", ["*naps*"]) == []
    assert checks("*wags his tail*", "", []) == ["dog-like: wags"]
    assert checks("*sniffs the phone screen*", "", []) == ["touches a screen: phone"]
    # The people and things in the scene aren't Whiskers repeating itself
    earlier = ["*bats the toy toward Tearyu*", "*chases the toy*"]
    assert checks("*pounces on the toy at Tearyu's feet*", "", earlier, ignore={"Tearyu", "toy"}) == []
