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


def test_cat_only_strips_human_words_but_keeps_actions_and_sounds():
    from whiskers.text import cat_only

    raw = ("*tail flicks lazily, ears perked*\nMrrp... I don't talk. I *meow*. Only when I want food or sun. "
           "*stares at Brandon with one eye* You're not my owner. Mrrp. 🌞")
    assert cat_only(raw) == "*tail flicks lazily, ears perked* Mrrp... *meow* *stares at Brandon with one eye* Mrrp."
    assert cat_only("**MEOW!** _sprints over_ purrrr~ mrrp-mrrp") == "MEOW! *sprints over* purrrr~ mrrp-mrrp"


def test_cat_only_drops_speech_hidden_in_actions_and_plain_sentences():
    from whiskers.text import cat_only

    assert cat_only('*thinks: you are weird* *licks paw* *says "hello"*') == "*licks paw*"
    assert cat_only("Hello there, I am a cat.") == ""
    assert cat_only("Mr. Smith is here") == ""


def test_parse_reply_reads_scene_and_reply_even_when_cut_off():
    from whiskers.text import parse_reply

    assert parse_reply('{"scene": "on Brandon\'s lap", "reply": "*kneads*"}') == ("on Brandon's lap", "*kneads*")
    assert parse_reply('{"scene": "zoomies", "reply": "*skids into the \\"wall') == ("zoomies", '*skids into the "wall')
    assert parse_reply("*plain text*") == ("", "*plain text*")


def test_repeated_actions_finds_what_whiskers_keeps_doing():
    from whiskers.text import repeated_actions

    recent = ["*nuzzles against Brandon's hand, purrs softly* mrrp", "*purrs loudly, starts kneading*",
              "*nuzzles their hand, meowing softly*", "*flops over dramatically*"]
    assert repeated_actions(recent, ignore={"Brandon"}) == ["nuzzles", "purrs"]
    assert repeated_actions(["*zoomies*", "*naps*"]) == []
