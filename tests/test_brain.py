import random

from fakes import ScriptedLLM
from whiskers.brain import Brain, Incoming, ReplyJob, chattiness_for, is_allowed
from whiskers.db import ChannelRule, GuildSettings, StoredMessage
from whiskers.scheduler import Scheduler


class SilentRandom(random.Random):
    """random() always returns 0.5, so chances below 50% never fire and above always do."""

    def random(self):
        return 0.5


def make_brain(cfg, rng=None):
    return Brain(cfg, db=None, llm=None, scheduler=Scheduler(), rng=rng or SilentRandom())


def incoming(content="hello", author=1, channel=10, **kwargs):
    return Incoming(message_id=kwargs.pop("message_id", 500), guild_id=1, channel_id=channel, author_id=author,
                    author_name=f"user{author}", content=content, created_at=1000.0, **kwargs)


# ---- channel rules ----

def test_allowlist_mode_needs_an_allow_rule():
    settings = GuildSettings(mode="allowlist")
    assert not is_allowed(settings, {}, [10, 99])
    assert is_allowed(settings, {99: ChannelRule("allow")}, [10, 99])  # allowed through its category


def test_channel_rule_beats_category_rule():
    rules = {10: ChannelRule("block"), 99: ChannelRule("allow")}
    assert not is_allowed(GuildSettings(), rules, [10, 99])


def test_everywhere_mode_respects_blocks_on_threads_parent():
    settings = GuildSettings(mode="everywhere")
    assert is_allowed(settings, {}, [11, 10])
    assert not is_allowed(settings, {10: ChannelRule("block")}, [11, 10])  # thread inside a blocked channel


def test_chattiness_override_falls_back_to_server_default():
    settings = GuildSettings(chattiness=5)
    assert chattiness_for(settings, {}, [10, 99]) == 5
    assert chattiness_for(settings, {99: ChannelRule(None, 40)}, [10, 99]) == 40


# ---- deciding ----

def test_mentions_replies_and_name_are_direct(cfg):
    brain = make_brain(cfg)
    settings = GuildSettings(chattiness=0)
    assert brain.decide(incoming(mentions_me=True), settings, 0).reply == "direct"
    assert brain.decide(incoming(replies_to_me=True), settings, 0).reply == "direct"
    assert brain.decide(incoming("hey WHISKERS come here"), settings, 0).reply == "direct"
    assert brain.decide(incoming("whiskersbot is down"), settings, 0).reply is None


def test_name_trigger_can_be_turned_off(cfg):
    brain = make_brain(cfg)
    assert brain.decide(incoming("whiskers!"), GuildSettings(name_trigger=False), 0).reply is None


def test_random_chime_in_uses_chattiness(cfg):
    brain = make_brain(cfg)
    assert brain.decide(incoming("nice weather"), GuildSettings(), 40).reply is None
    assert brain.decide(incoming("nice weather"), GuildSettings(), 60).reply == "ambient"
    assert brain.decide(incoming("I bought tuna"), GuildSettings(), 25).reply == "ambient"  # cat words x2.5


def test_follow_up_after_cat_answered_is_likely_for_the_cat(cfg):
    brain = make_brain(cfg)
    brain._last_spoke_to[(10, 1)] = 1000.0
    assert brain.decide(incoming("good kitty"), GuildSettings(), 0, now=1060.0).reply == "ambient"
    assert brain.decide(incoming("good kitty", author=2), GuildSettings(), 0, now=1060.0).reply is None


def test_cat_does_not_chime_in_right_after_speaking(cfg):
    brain = make_brain(cfg)
    brain._last_spoke[10] = 1000.0
    assert brain.decide(incoming("nice weather", author=2), GuildSettings(), 90, now=1010.0).reply is None


class EagerRandom(random.Random):
    """random() always returns 0, so every chance fires."""

    def random(self):
        return 0.0


def test_reactions_fit_the_message_and_can_be_turned_off(cfg):
    brain = make_brain(cfg, rng=random.Random(1))

    def reactions(text, **kwargs):
        return {brain.decide(incoming(text, channel=100 + i, **kwargs), GuildSettings(), 0, now=1000.0).reaction
                for i in range(300)} - {None}

    assert reactions("who wants treats") <= {"🐟", "😋", "👀", "🐾", "😺"} and reactions("who wants treats") & {"🐟", "😋"}
    assert "😹" in reactions("LMAOOO 💀")
    assert "🎉" in reactions("I GOT THE JOB!!!")
    assert "😿" in reactions("i had a really bad day")
    assert "😾" in reactions("whiskers is so ugly lol", mentions_me=True)  # even while answering, now and then
    assert "👀" in reactions("[sends a gif: cat vibing]")
    off = GuildSettings(reactions=False)
    assert all(brain.decide(incoming("treats"), off, 0).reaction is None for _ in range(50))


def test_reactions_are_spaced_out_and_can_replace_a_chime_in(cfg):
    brain = make_brain(cfg, rng=EagerRandom())
    assert brain.decide(incoming("tuna!", channel=5), GuildSettings(), 0, now=1000.0).reaction
    assert brain.decide(incoming("tuna!", channel=5), GuildSettings(), 0, now=1010.0).reaction is None
    assert brain.decide(incoming("tuna!", channel=5), GuildSettings(), 0, now=1025.0).reaction
    swapped = brain.decide(incoming("tuna!", channel=6), GuildSettings(), 50, now=1000.0)
    assert swapped.reply is None and swapped.reaction  # an emoji instead of writing a whole reply


def test_home_channels_answer_every_message_and_each_person_separately(cfg):
    brain = make_brain(cfg)
    quiet = GuildSettings(reactions=False)
    assert brain.decide(incoming("lol", author=2), quiet, 0, home=True).reply == "home"
    called = brain.decide(incoming("whiskers hi", author=2), quiet, 0, home=True)
    assert (called.reply, called.to_me) == ("home", True)
    assert brain.decide(incoming("whiskers hi", author=2), quiet, 0).reply == "direct"
    assert brain.decide(incoming("lol", author=2), quiet, 0).reply is None  # elsewhere it only chimes in sometimes

    first = brain.submit("home", incoming("*picks him up*", author=2, message_id=10))
    first.started = True  # Whiskers is writing a reply that read message 10
    other = brain.submit("home", incoming("*shakes the treat bag*", author=3, message_id=11))
    again = brain.submit("home", incoming("*puts him in the tub*", author=2, message_id=12))
    assert other is not None and again is not None  # someone else's interaction isn't lost in user2's
    assert brain.submit("home", incoming("*runs the water*", author=2, message_id=13)) is None  # joins their wait
    assert again.message.message_id == 13

    brain._answered_upto[(10, 2)] = 11  # the first reply was posted, but it hadn't read 12 and 13
    assert not brain.already_answered(again) and not brain.already_answered(other)
    brain._answered_upto[(10, 2)] = 13
    assert brain.already_answered(again) and not brain.already_answered(other)


def test_people_talking_to_each_other_at_home_get_no_reply(cfg):
    brain = make_brain(cfg)
    brain.user_id = 99
    quiet = GuildSettings(reactions=False)

    def home(text, **kwargs):
        return brain.decide(incoming(text, author=2, **kwargs), quiet, 0, now=1000.0, home=True).reply

    assert home("eh maybe") == "home"  # nobody else here: they're talking to Whiskers
    brain._room[10] = {3: ("General Kenobi", 990.0), 2: ("user2", 995.0)}
    assert home("eh maybe") is None
    assert home("And losing some context. Needs work still") is None
    assert home("Cat do tail thumping", replies_to_other=True) is None  # a reply to someone else
    assert home("*hands him a treat*", replies_to_other=True) == "home"  # roleplay always gets an answer
    assert home("user2 puts more treats in the bowl") == "home"  # roleplay in the third person
    assert home("I put him in the water and grab the shampoo") == "home"
    assert home("good kitty") == "home"
    assert home("whiskers come here") == "home"
    brain._last_author[10] = 99
    assert home("are you hungry?") == "home"  # "you" straight after Whiskers' own message
    brain._room[10] = {3: ("General Kenobi", 600.0), 2: ("user2", 995.0)}  # Kenobi left a while ago
    assert home("eh maybe") == "home"


# ---- queueing ----

def test_messages_from_someone_already_waiting_are_merged(cfg):
    brain = make_brain(cfg)
    job = brain.submit("direct", incoming("whiskers", message_id=1))
    assert brain.submit("direct", incoming("whiskers??", message_id=2)) is None
    assert job.message.message_id == 2


def test_direct_reply_replaces_a_queued_chime_in(cfg):
    brain = make_brain(cfg)
    ambient = brain.submit("ambient", incoming("tuna", author=2))
    assert brain.submit("ambient", incoming("more tuna", author=3)) is None
    direct = brain.submit("direct", incoming("whiskers", author=4))
    assert ambient.cancelled and direct is not None


def test_queued_reply_is_skipped_if_whiskers_already_answered_that_person(cfg):
    brain = make_brain(cfg)
    first = brain.submit("direct", incoming("whiskers want tuna?", author=8, message_id=3))
    first.started = True  # Whiskers is busy answering message 3
    second = brain.submit("direct", incoming("whiskers??", author=8, message_id=4))
    assert second is not None and not brain.already_answered(second)
    brain._answered_upto[(10, 8)] = 3  # its reply to message 3 was posted, but hadn't read message 4
    assert not brain.already_answered(second)
    brain._answered_upto[(10, 8)] = 4  # message 4 came in before it started reading
    assert brain.already_answered(second)

    other = brain.submit("direct", incoming("whiskers hi", author=7, message_id=5))
    assert not brain.already_answered(other)  # someone else still gets their own answer


# ---- context window ----

def rows(ids, gap_after=None):
    out = []
    t = 0.0
    for i in ids:
        t += 4 * 3600 if gap_after is not None and i == gap_after + 1 else 10
        out.append(StoredMessage(i, 1, 10, i, 1, "a", False, f"m{i}", t))
    return out


def test_window_start_moves_in_jumps_so_prompts_share_a_prefix(cfg):
    brain = make_brain(cfg)  # history_messages=8 in the test config
    first = brain.window(10, rows(range(1, 9)))
    assert first[0].id == 5  # starts at half the history
    assert brain.window(10, rows(range(2, 10)))[0].id == 5  # same start as before
    assert brain.window(10, rows(range(5, 13)))[0].id == 5
    assert brain.window(10, rows(range(6, 14)))[0].id == 10  # jumped forward


def test_long_silence_starts_a_fresh_context(cfg):
    brain = make_brain(cfg)
    assert brain.window(10, rows(range(1, 7), gap_after=4))[0].id == 5


def test_notes_describe_people_memories_and_what_to_do(cfg):
    from whiskers.db import Memory, Profile

    brain = make_brain(cfg)
    notes = brain.context_notes(
        0, [Profile(1, "Alice", ["works night shifts"], chats=30)],
        [Memory(1, "Alice gave Whiskers a laser pointer.", 0.0)], now=3 * 86400.0,
    )
    assert notes[0] == "It's Sunday night."
    assert "Alice: a good friend you trust. works night shifts." in notes
    assert notes[2] == "You remember (3 days ago): Alice gave Whiskers a laser pointer."
    assert brain.reply_note(incoming("whiskers"), "direct").startswith(
        'user1 just said or did: "whiskers". React as a cat would, with actions and cat sounds but no human words')
    assert brain.reply_note(incoming("whiskers"), "direct", can_speak=True).startswith(
        'user1 just said or did: "whiskers". Answer them as Whiskers')
    assert brain.reply_note(incoming("LMAO 💀", replies_to_me=True), "direct").startswith(
        'user1 replied to your message: "LMAO 💀". React as a cat would')


def test_whiskers_adores_its_human_and_warms_up_to_others(cfg):
    from whiskers.db import Profile

    brain = make_brain(cfg)
    notes = brain.context_notes(1, [Profile(1, "Brandon", chats=0), Profile(2, "Angel", chats=0)], [])
    assert "Brandon: your human, the person you love most in the world." in notes
    assert any(n.startswith("Angel: new to you") and "friendly" in n for n in notes)


def test_how_whiskers_treats_people_follows_how_they_treated_it(cfg):
    from whiskers.brain import relationship
    from whiskers.db import Feeling, Profile

    assert relationship(40, affection=-20).startswith("you don't trust them")
    assert relationship(40, affection=-12).startswith("a good friend")  # one bad moment isn't distrust
    assert relationship(40, affection=-50).startswith("you can't stand them")
    assert "not scared of them" in relationship(0)  # someone new is met with curiosity, not fear
    assert relationship(3, affection=40).startswith("you really like them")
    assert relationship(3, affection=40, chosen=True).startswith("one of your chosen people")
    assert relationship(3, is_owner=True, affection=-50).startswith("your human")

    brain = make_brain(cfg)
    rough = Profile(2, "Tearyu", chats=3, affection=-20,
                    feelings=[Feeling("a treat", "liked", 0.0), Feeling("tail pulling", "hated", 86400.0 - 600)])
    notes = brain.context_notes(0, [rough], [], news=["Tearyu did something"], now=86400.0)
    line = next(n for n in notes if n.startswith("Tearyu:"))
    assert "you don't trust them" in line
    assert line.endswith("You love a treat with them. You don't like tail pulling. "
                         "You're still sore about the tail pulling (10 minutes ago).")
    later = brain.person_note(rough, 0, 86400.0 * 2)
    assert "still sore" not in later and "You don't like tail pulling." in later  # the sulk passes, the dislike stays
    friend = Profile(3, "Kenobi", chats=30, affection=40,
                     feelings=[Feeling("laser pointer", "loved", 0.0, 6), Feeling("bath", "disliked", 10.0)])
    line = brain.person_note(friend, 0, 86400.0)
    assert "You love laser pointer with them. You don't like bath." in line and "sore" not in line
    assert "Tearyu did something" in notes
    assert brain.reply_note(incoming("hi kitty", author=2), "direct").endswith('Your "feeling" is about user2.')


def test_people_whiskers_likes_get_more_chime_ins(cfg):
    from whiskers.db import Profile

    brain = make_brain(cfg)
    msg = incoming("nice weather", author=2, owner_id=1)
    assert brain.fondness(msg, Profile(2, "Angel", chosen=True)) == 1.5
    assert brain.fondness(msg, Profile(2, "Tearyu", affection=-30)) == 0.5
    assert brain.fondness(incoming("hi", author=1, owner_id=1), None) == 1.5
    assert brain.decide(msg, GuildSettings(), 40, fondness=1.5).reply == "ambient"  # 40% x 1.5 beats the 50% roll
    assert brain.decide(msg, GuildSettings(), 40).reply is None


# ---- speaking: just a cat until the server teaches it ----

class FakeLLM(ScriptedLLM):
    def __init__(self, reply):
        super().__init__()
        self.reply = reply
        self.calls = []

    async def answer(self, messages, kwargs):
        self.calls.append((messages, kwargs))
        return self.reply


def brain_with(cfg, tmp_path, reply):
    from whiskers.db import Database

    return Brain(cfg, db=Database(tmp_path / "brain.db"), llm=FakeLLM(reply), scheduler=Scheduler(),
                 rng=SilentRandom())


def test_a_cat_that_cant_talk_has_its_words_removed(cfg, tmp_path):
    import asyncio

    async def go():
        brain = brain_with(cfg, tmp_path, "*tilts head* Mrrp? Huh. You're new. Do you have food?")
        await brain.db.connect()
        msg = incoming("hey whiskers", mentions_me=True)
        await brain.observe(msg, GuildSettings(), 0)
        assert await brain.compose(msg, "direct") == "*tilts head* Mrrp?"
        system = brain.llm.calls[-1][0][0]["content"]
        assert "can't use human language" in system

        await brain.db.set_can_speak(1, True)
        assert await brain.compose(msg, "direct") == "*tilts head* Mrrp? Huh. You're new. Do you have food?"

        owner_msg = incoming("*kicks you out the window*", author=8, message_id=600, owner_id=8)
        await brain.observe(owner_msg, GuildSettings(), 0)
        await brain.compose(owner_msg, "direct")
        system = brain.llm.calls[-1][0][0]["content"]
        assert "user8 is your human" in system
        assert brain.llm.calls[-1][1]["extra"]["dry_multiplier"] > 0  # anti-repetition sampling
        await brain.db.close()

    asyncio.run(go())


def test_learning_to_talk_needs_time_and_reflections(cfg, tmp_path):
    import asyncio
    import dataclasses
    import json

    async def go():
        reply = json.dumps({"journal": ["I'm Whiskers.", "Brandon taught me to share thoughts."],
                            "learned_to_communicate": True})
        brain = brain_with(dataclasses.replace(cfg, speech_after_days=7), tmp_path, reply)
        await brain.db.connect()
        await brain.db.count_cat_message(1)

        await brain.reflect(1)  # brand-new cat: the model's "true" is ignored and not even asked for
        assert "learned_to_communicate" not in brain.llm.calls[-1][1]["json_schema"]["properties"]
        assert not (await brain.db.journal(1)).can_speak

        for _ in range(5):
            await brain.db.mark_reflected(1)
        await brain.db.conn.execute("UPDATE journal SET born_at = born_at - 8 * 86400")
        await brain.reflect(1)  # a week later, after enough reflections, it may learn
        journal = await brain.db.journal(1)
        assert journal.can_speak
        assert journal.text == "- I'm Whiskers.\n- Brandon taught me to share thoughts."
        await brain.db.close()

    asyncio.run(go())


def test_a_cat_that_has_been_silent_is_reminded_it_has_a_voice(cfg, tmp_path):
    import asyncio
    import dataclasses
    import json

    async def go():
        longer = dataclasses.replace(cfg, history_messages=20)
        brain = brain_with(longer, tmp_path, json.dumps({"scene": "Brandon is around", "reply": "*watches Brandon*"}))
        await brain.db.connect()
        for i in range(5):
            msg = incoming(f"*does thing {i}*", author=8, message_id=700 + i * 10, mentions_me=True, owner_id=8)
            await brain.observe(msg, GuildSettings(), 0)
            sent = await brain.compose(msg, "direct")
            await brain.record_own_message(msg, 701 + i * 10, sent, 2000.0 + i)
            if i == 3:
                assert "You haven't made a sound" not in brain.llm.calls[-1][0][-1]["content"]
        assert "You haven't made a sound in a while" in brain.llm.calls[-1][0][-1]["content"]
        await brain.db.close()

    asyncio.run(go())


def test_the_scene_carries_over_and_repetition_is_pointed_out(cfg, tmp_path):
    import asyncio
    import json

    async def go():
        reply = json.dumps({"scene": "Brandon gave me catnip and I'm going wild",
                            "reply": "*rolls in the catnip, purrs and nuzzles the bag*"})
        brain = brain_with(cfg, tmp_path, reply)
        await brain.db.connect()
        for i, text in enumerate(["*gives catnip*", "good kitty", "*pets*", "*scratches your chin*"]):
            msg = incoming(text, author=8, message_id=700 + i * 10, mentions_me=True, owner_id=8)
            await brain.observe(msg, GuildSettings(), 0)
            sent = await brain.compose(msg, "direct")
            assert sent == "*rolls in the catnip, purrs and nuzzles the bag*"  # the scene note is never posted
            await brain.record_own_message(msg, 701 + i * 10, sent, 2000.0 + i)
        last_turn = brain.llm.calls[-1][0][-1]["content"]
        assert "Where things stood after your last message: Brandon gave me catnip and I'm going wild" in last_turn
        assert "You've been repeating yourself (" in last_turn and "nuzzles" in last_turn
        assert "purrs" not in last_turn.split("repeating yourself")[1].split(")")[0]  # sounds may repeat
        assert "You haven't made a sound" not in last_turn
        assert brain.llm.calls[-1][1]["json_schema"]["required"] == ["scene", "reply", "feeling", "about"]
        await brain.db.close()

    asyncio.run(go())


def test_feelings_add_up_until_whiskers_chooses_someone_and_hurt_undoes_it(cfg, tmp_path):
    import asyncio
    import json

    async def go():
        brain = brain_with(cfg, tmp_path, "")
        await brain.db.connect()
        msg = incoming("*gives you salmon*", author=5, mentions_me=True, owner_id=8)
        await brain.observe(msg, GuildSettings(), 0)
        await brain.db.conn.execute("UPDATE profiles SET chats = 20, affection = 50")

        brain.llm.reply = json.dumps({"scene": "salmon!", "feeling": "loved", "about": "salmon.", "reply": "*eats*"})
        await brain.compose(msg, "direct")
        assert brain.last_feeling(10) == ("loved", "salmon", 55.0, None)
        await brain.compose(msg, "direct")  # the same thing again adds up in one memory of it
        angel = await brain.db.profile(1, 5)
        assert (angel.affection, angel.chosen, [(f.about, f.score) for f in angel.feelings]) == (60.0, True, [("salmon", 4)])
        assert brain.last_feeling(10)[3] == "chosen"

        brain.llm.reply = json.dumps({"scene": "x", "feeling": "neutral", "about": "", "reply": "*naps*"})
        await brain.compose(msg, "direct")
        last_turn = brain.llm.calls[-1][0][-1]["content"]
        system = brain.llm.calls[-1][0][0]["content"]
        assert "You've just decided that user5 is one of your people now" in last_turn
        assert "user5" not in system  # the notes say who's chosen, so the cached system prompt stays the same
        assert "user5: one of your chosen people" in last_turn
        assert (await brain.db.profile(1, 5)).affection == 60.0  # neutral changes nothing

        brain.llm.reply = json.dumps({"scene": "x", "feeling": "disliked", "about": "being put down", "reply": "*sulks*"})
        await brain.compose(msg, "direct")  # something he didn't enjoy, but nobody was mean to him
        angel = await brain.db.profile(1, 5)
        assert angel.affection == 60.0 and ("being put down", -1) in [(f.about, f.score) for f in angel.feelings]

        brain.llm.reply = json.dumps({"scene": "x", "feeling": "hated", "about": "water spray", "reply": "*hisses*"})
        await brain.compose(msg, "ambient")  # chiming in on someone's chat doesn't count
        assert (await brain.db.profile(1, 5)).affection == 60.0 and brain.last_feeling(10) is None
        await brain.compose(msg, "direct")
        assert brain.last_feeling(10)[2:] == (45.0, None)  # being hurt counts straight away, but one spray
        await brain.compose(msg, "direct")  # doesn't undo a friendship
        await brain.compose(msg, "direct")  # and one bad day takes away at most DAILY_COOLING
        angel = await brain.db.profile(1, 5)
        assert (angel.affection, angel.chosen) == (30.0, True)
        brain._cooled[(1, 5)] = (0, 30.0)  # the next day, it happens again
        await brain.compose(msg, "direct")
        angel = await brain.db.profile(1, 5)
        assert (angel.affection, angel.chosen) == (15.0, False) and brain.last_feeling(10)[3] == "dropped"
        assert [f.about for f in angel.feelings] == ["salmon", "being put down", "water spray"]
        await brain.db.close()

    asyncio.run(go())


def test_whiskers_human_is_never_chosen_and_a_sound_can_repeat(cfg, tmp_path):
    import asyncio
    import json

    async def go():
        reply = json.dumps({"scene": "x", "feeling": "loved", "about": "tuna", "reply": "*eats* MRRP!"})
        brain = brain_with(cfg, tmp_path, reply)
        await brain.db.connect()
        sent = []
        for i in range(12):
            msg = incoming("*gives tuna*", author=8, message_id=900 + i * 10, mentions_me=True, owner_id=8)
            await brain.observe(msg, GuildSettings(), 0)
            sent.append(await brain.compose(msg, "direct"))
            await brain.record_own_message(msg, 901 + i * 10, sent[-1], 3000.0 + i)
        human = await brain.db.profile(1, 8)
        assert human.affection == 15.0 and not human.chosen  # kindness only adds up so far in one day
        brain._warmed[(1, 8)] = (0, 15.0)  # a new day
        await brain.feel(msg, "loved", "tuna")
        assert (await brain.db.profile(1, 8)).affection == 20.0
        assert sent[:3] == ["*eats* MRRP!"] * 3  # a happy cat may well say so every time
        await brain.db.close()

    asyncio.run(go())


def test_reactions_fit_the_mood_of_the_message(cfg):
    brain = make_brain(cfg, rng=EagerRandom())

    def react(text, channel):
        return brain.decide(incoming(text, channel=channel), GuildSettings(), 0, now=1000.0).reaction

    assert react("the thunder just shook the whole house", 1) == "🙀"
    assert react("today i woke up late and missed the bus", 2) != "☀️"
    assert react("it's angel's birthday today!!", 3) == "🎉"


def test_whiskers_knows_when_his_human_is_talking(cfg):
    brain = make_brain(cfg)
    note = brain.reply_note(incoming("*shakes the treat bag*", author=1, owner_id=1), "direct")
    assert note.startswith('user1 (your human) just said or did: "*shakes the treat bag*"')


def test_whiskers_starts_mischief_when_a_home_channel_goes_quiet(cfg, tmp_path):
    import asyncio
    import json
    from contextlib import asynccontextmanager

    from whiskers.brain import MISCHIEF_AROUND, MISCHIEF_QUIET

    async def go():
        brain = brain_with(cfg, tmp_path, json.dumps({"scene": "bored", "reply": "*steals Brandon's sock and runs*"}))
        brain.rng = EagerRandom()
        await brain.db.connect()
        said = 10_000.0
        msg = incoming("brb food", author=8, message_id=700, owner_id=8)
        msg.created_at = said
        await brain.observe(msg, GuildSettings(), 0, home=True)

        assert await brain.start_mischief(1, 10, "sometimes", 8, now=said + 60) is None  # not quiet yet
        assert await brain.start_mischief(1, 10, "sometimes", 8, now=said + MISCHIEF_AROUND + 60) is None  # nobody's around
        assert await brain.start_mischief(1, 10, "off", 8, now=said + MISCHIEF_QUIET + 60) is None
        job = await brain.start_mischief(1, 10, "sometimes", 8, now=said + MISCHIEF_QUIET + 60)
        assert job.kind == "mischief" and job.message.message_id == 700 and job.message.owner_id == 8
        assert await brain.start_mischief(1, 10, "often", 8, now=said + MISCHIEF_QUIET + 61) is None  # one at a time

        posted = []

        async def deliver(text):
            posted.append(text)
            return 701

        @asynccontextmanager
        async def typing():
            yield

        assert await brain.respond(job, typing, deliver) == "*steals Brandon's sock and runs*"
        note = next(call[0][-1]["content"] for call in brain.llm.calls if call[1].get("json_schema"))
        assert "It has gone quiet" in note and "start some mischief of your own" in note and "user8" in note
        assert "This time: " in note and "keyboard" not in note
        assert (await brain.db.profile(1, 8)).affection == 0  # nobody did anything to him
        assert (await brain.db.last_unprompted(10))[1] is None  # remembered as his own idea, not yet answered
        await brain.db.close()

    asyncio.run(go())


def test_someone_talking_first_cancels_the_mischief(cfg):
    brain = make_brain(cfg)
    mischief = ReplyJob("mischief", incoming("brb", message_id=1), (10, 0))
    brain._jobs[mischief.key] = mischief
    job = brain.submit("home", incoming("back", message_id=2))
    assert mischief.cancelled and job.kind == "home" and brain._jobs == {(10, 1): job}


def test_now_and_then_whiskers_is_nudged_to_start_something(cfg, tmp_path):
    import asyncio

    async def go():
        brain = brain_with(cfg, tmp_path, "")
        await brain.db.connect()
        rows = await brain.db.recent_messages(10, 5)
        brain.rng = EagerRandom()
        notes, _, _ = await brain._context(1, 10, rows, 1, 0, [])
        assert any("mischievous mood" in n for n in notes)
        await brain.db.update_guild_settings(1, mischief="off")
        notes, _, _ = await brain._context(1, 10, rows, 1, 0, [])
        assert not any("mischievous mood" in n for n in notes)
        brain.rng = SilentRandom()
        await brain.db.update_guild_settings(1, mischief="often")
        notes, _, _ = await brain._context(1, 10, rows, 1, 0, [])
        assert not any("mischievous mood" in n for n in notes)  # only now and then
        await brain.db.close()

    asyncio.run(go())


def test_in_a_busy_home_channel_each_reply_is_for_one_person(cfg, tmp_path):
    import asyncio
    import json

    async def go():
        brain = brain_with(cfg, tmp_path, json.dumps({"scene": "busy", "reply": "*flops over in the middle of everyone*"}))
        await brain.db.connect()
        first = incoming("whiskers do you love me?", author=5, message_id=600, owner_id=8)
        await brain.observe(first, GuildSettings(), 0, home=True)
        await brain.record_own_message(first, 601, "*pounces on user5's hand*", 1001.0)
        for i, (author, text) in enumerate([(6, "was this rejection 💀"), (5, "more than Brandon?"),
                                            (6, "ik you love me")]):
            msg = incoming(text, author=author, message_id=610 + i, owner_id=8)
            await brain.observe(msg, GuildSettings(), 0, home=True)
        await brain.compose(msg, "home")
        note = brain.llm.calls[-1][0][-1]["content"]
        assert 'user6 just said or did: "ik you love me"' in note
        assert "user5 is here too and get their own replies, so this one is only for user6" in note
        assert "what user6 did, user6 did, not anyone else" in note
        await brain.db.close()

    asyncio.run(go())


def test_mischief_waits_for_an_answer_and_even_longer_when_ignored(cfg, tmp_path):
    import asyncio

    from whiskers.brain import MISCHIEF

    gap = MISCHIEF["sometimes"].gap

    async def go():
        db = brain_with(cfg, tmp_path, "").db
        await db.connect()

        def fresh():  # a new Brain each time, as after a restart: only the stored chat says what happened
            return Brain(cfg, db=db, llm=FakeLLM(""), scheduler=Scheduler(), rng=EagerRandom())

        async def human(channel, mid, at):
            await db.add_message(1, channel, mid, 8, "user8", "hi", at)

        async def mischief(channel, mid, at):
            await db.add_message(1, channel, mid, 99, "Whiskers", "*knocks a mug off the desk*", at, is_self=True,
                                 unprompted=True)

        base = 1_000_000.0
        # Channel 10: nobody answers his antics, so there's no second one, however long it stays quiet
        await human(10, 1, base)
        await mischief(10, 2, base + 600)
        assert await fresh().start_mischief(1, 10, "often", now=base + 1200) is None

        # Channel 20: answered within minutes, so the usual gap
        await human(20, 11, base)
        await mischief(20, 12, base + 600)
        await human(20, 13, base + 900)
        await human(20, 14, base + 600 + gap - 900)  # people chatting again later
        assert await fresh().start_mischief(1, 20, "sometimes", now=base + 600 + gap - 300) is None  # too soon
        await human(20, 15, base + 600 + gap - 600)
        assert await fresh().start_mischief(1, 20, "sometimes", now=base + 600 + gap + 60) is not None

        # Channel 30: nobody said anything for half an hour after it: the next one waits twice as long
        await human(30, 21, base)
        await mischief(30, 22, base + 600)
        await human(30, 23, base + 600 + 1800)
        await human(30, 24, base + 600 + gap)
        assert await fresh().start_mischief(1, 30, "sometimes", now=base + 600 + gap + 360) is None
        await human(30, 25, base + 600 + 2 * gap)
        assert await fresh().start_mischief(1, 30, "sometimes", now=base + 600 + 2 * gap + 360) is not None
        await db.close()

    asyncio.run(go())


def test_a_bad_moment_with_something_he_loves_doesnt_turn_it_into_a_dislike(cfg, tmp_path):
    import asyncio

    from whiskers.brain import likes_and_dislikes

    async def go():
        brain = brain_with(cfg, tmp_path, "")
        await brain.db.connect()
        msg = incoming("*waves the laser around*", author=5, mentions_me=True, owner_id=8)
        await brain.observe(msg, GuildSettings(), 0)
        for feeling, about in [("loved", "laser pointer"), ("loved", "chasing the laser"), ("disliked", "laser off")]:
            await brain.feel(msg, feeling, about)
        milo = await brain.db.profile(1, 5)
        assert [(f.about, f.score) for f in milo.feelings] == [("laser pointer", 3)]
        assert likes_and_dislikes(milo.feelings) == (["laser pointer"], [])
        assert milo.affection == 10.0  # the laser going off isn't Milo being mean

        await brain.feel(incoming("*yanks your tail*", author=5, mentions_me=True, owner_id=8), "hated", "tail")
        assert (await brain.db.profile(1, 5)).affection == -5.0  # this is
        await brain.db.close()

    asyncio.run(go())


def test_mischief_ideas_change_and_skip_what_he_did_lately(cfg):
    from whiskers.brain import MISCHIEF_IDEAS

    brain = make_brain(cfg, rng=random.Random(3))
    ideas = {brain.mischief_idea([]) for _ in range(40)}
    assert len(ideas) >= 10  # not the same antic every time
    lately = ["*steals Brandon's sock and runs*", "*hides in the laundry basket*"]
    for _ in range(40):
        idea = brain.mischief_idea(lately)
        assert "sock" not in idea and "box or bag" not in idea
    assert all("keyboard" not in idea for _, idea in MISCHIEF_IDEAS)
