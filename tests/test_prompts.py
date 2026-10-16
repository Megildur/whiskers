from whiskers.db import StoredMessage
from whiskers.prompts import chat_turns, parse_journal, reply_messages


def msg(i, name, content, is_self=False):
    return StoredMessage(i, 1, 10, 100 + i, 0 if is_self else i, name, is_self, content, 1000.0 + i)


def test_turns_alternate_and_group_people():
    rows = [
        msg(1, "Whiskers", "*yawns*", is_self=True),
        msg(2, "Alice", "hi whiskers"),
        msg(3, "Bob", "he's asleep"),
        msg(4, "Whiskers", "*opens one eye*", is_self=True),
        msg(5, "Alice", "treat?"),
    ]
    turns = chat_turns(rows)
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    assert turns[0]["content"] == "Alice: hi whiskers\nBob: he's asleep"


def test_turns_always_end_with_a_user_turn():
    turns = chat_turns([msg(1, "Alice", "hi"), msg(2, "Whiskers", "*meow*", is_self=True)])
    assert turns[-1]["role"] == "user"


def test_notes_go_last_so_the_prompt_prefix_stays_cacheable():
    messages = reply_messages("SYSTEM", [msg(1, "Alice", "hi")], ["It's night."], "Whiskers")
    assert messages[0] == {"role": "system", "content": "SYSTEM"}
    assert messages[-1]["content"].startswith("Alice: hi\n\n(Whiskers's private notes")
    assert messages[-1]["content"].endswith("- It's night.")


def test_parse_journal_keeps_bullets_only():
    text = "Here's the new journal:\n- I'm Whiskers.\n* I love Alice.\n-I hate baths\nThanks!"
    assert parse_journal(text) == "- I'm Whiskers.\n- I love Alice.\n- I hate baths"


def test_parse_journal_accepts_numbered_lists():
    assert parse_journal("1. I'm Whiskers.\n2) I can hear thoughts now.") == "- I'm Whiskers.\n- I can hear thoughts now."


def test_parse_journal_rejects_non_journals():
    assert parse_journal("I can't do that.") is None
    assert parse_journal("- just one line") is None
