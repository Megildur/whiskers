from whiskers.text import ago, clean_reply, keywords, readable, split_message


def test_readable_turns_custom_emoji_into_names_and_tidies_spaces():
    assert readable("hi  <:catjam:123456>   there\n\n<a:wave:42>") == "hi :catjam: there\n:wave:"


def test_keywords_skip_stopwords_and_duplicates():
    assert keywords("Do you remember the laser pointer? The LASER was great") == [
        "remember", "laser", "pointer", "great",
    ]


def test_clean_reply_strips_own_label_and_cuts_other_speakers():
    raw = "Whiskers: *purrs loudly*\nAlice: good kitty!\nWhiskers: *meows*"
    assert clean_reply(raw, "Whiskers", ["Alice"]) == "*purrs loudly*"


def test_clean_reply_handles_bold_labels_and_notes_leaks():
    raw = "**Whiskers:** *knocks your cup over*\n(Whiskers's private notes, not part of the chat)\n- secret"
    assert clean_reply(raw, "Whiskers", []) == "*knocks your cup over*"


def test_clean_reply_keeps_ordinary_colons_and_blocks_mass_pings():
    raw = "Note: *stares* @everyone"
    assert clean_reply(raw, "Whiskers", ["Alice"]) == "Note: *stares* @​everyone"


def test_clean_reply_unwraps_quotes():
    assert clean_reply('"*mrrp*"', "Whiskers", []) == "*mrrp*"


def test_split_message_respects_discord_limit():
    chunks = split_message("word " * 1000, limit=2000)
    assert all(len(c) <= 2000 for c in chunks)
    assert " ".join(chunks).split() == ["word"] * 1000


def test_ago():
    assert ago(30) == "just now"
    assert ago(3 * 3600) == "3 hours ago"
    assert ago(86400) == "1 day ago"
