import random

from whiskers.brain import Brain, Incoming, chattiness_for, is_allowed
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


def test_reactions_only_when_not_replying_and_enabled(cfg):
    brain = make_brain(cfg, rng=random.Random(1))
    seen = {brain.decide(incoming("who wants treats"), GuildSettings(), 0).reaction for _ in range(200)}
    assert seen & {"🐟", "😋"}
    assert brain.decide(incoming("treats", mentions_me=True), GuildSettings(), 0).reaction is None
    off = GuildSettings(reactions=False)
    assert all(brain.decide(incoming("treats"), off, 0).reaction is None for _ in range(50))


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
    brain._last_answered[(10, 8)] = 9  # its reply to message 3 was posted after message 4
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
    notes = brain.notes(
        incoming("whiskers"), "direct",
        [Profile(1, "Alice", ["works night shifts"], chats=30)],
        [Memory(1, "Alice gave Whiskers a laser pointer.", 0.0)],
        now=3 * 86400.0,
    )
    assert notes[0].startswith("It's Sunday night")
    assert "Alice: they're a friend. works night shifts." in notes
    assert notes[2] == "You remember (3 days ago): Alice gave Whiskers a laser pointer."
    assert notes[-1] == "user1 is talking to you. Answer them as Whiskers."
