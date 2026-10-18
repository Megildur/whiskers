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
    assert repeated_actions(recent, ignore={"Brandon"}) == ["nuzzles"]  # purring again is fine
    assert repeated_actions(recent, ignore={"Brandon"}, times=3) == []
    assert repeated_actions(recent + ["*nuzzles the couch*"], ignore={"Brandon"}, times=3) == ["nuzzles"]
    assert repeated_actions(["*zoomies*", "*naps*"]) == []


def test_person_name_turns_decorated_display_names_into_names():
    from whiskers.text import person_name

    assert person_name("ANGEL ‼️") == "Angel"
    assert person_name("~*Lily*~") == "Lily"
    assert person_name("💀💀", "skull_guy") == "skull guy"
    assert person_name("怠竜 Tearyū") == "怠竜 Tearyū"


def test_stray_asterisks_and_first_person_actions_are_tidied():
    from whiskers.text import as_stage_directions, cat_only

    assert cat_only("*glares up at Angel* MEOW*") == "*glares up at Angel* MEOW"
    assert as_stage_directions("*flops onto my back, hiding my face* mrrp *grooms myself*") == (
        "*flops onto its back, hiding its face* mrrp *grooms itself*")
    assert as_stage_directions("my *naps*") == "my *naps*"  # only inside actions


def test_cat_only_keeps_actions_the_model_wrote_sloppily():
    from whiskers.text import cat_only

    # From real simulations: narration outside the asterisks, asterisks used as commas, stray braces
    assert cat_only("*launches straight up*, lands already running, and vanishes under the bed *HSSS*") == (
        "*launches straight up, lands already running, and vanishes under the bed* *HSSS*")
    assert cat_only("*YOWLS * CRASHLANDS into the bushes outside * GLARES UP AT THE WINDOW*") == (
        "*yowls, crashlands into the bushes outside, glares up at the window*")
    assert cat_only("*tilts head* } MRRP? *paws at Brandon's hand* MEEP. }") == (
        "*tilts head* MRRP? *paws at Brandon's hand* MEEP.")
    assert cat_only("*pauses, ears twitching*, MREOW") == "*pauses, ears twitching* MREOW"
    assert cat_only("*hops down* PRRP *sniffs* MRAP? mlem") == "*hops down* PRRP *sniffs* MRAP? mlem"
    assert cat_only("*stares at Tearyu") == "*stares at Tearyu*"
    assert cat_only("*purrs* and you are my best friend") == "*purrs*"  # speech is still dropped


def test_makes_sound_hears_sounds_said_and_done():
    from whiskers.text import makes_sound

    for text in ["*kneads* MRRP!", "mrrp?", "*starts purring*", "*lands with a yowl*", "*hisses at the vacuum*",
                 "*sprints to the kitchen* MEOW!", "*chirps at the birds*"]:
        assert makes_sound(text), text
    for text in ["*trots over*", "*flops down dramatically on his back*", "*tilts head, then swipes at the mirror*"]:
        assert not makes_sound(text), text


def test_parse_feeling_reads_how_whiskers_felt():
    from whiskers.text import parse_feeling

    assert parse_feeling('{"scene": "x", "feeling": "Hated", "about": "tail pulling.", "reply": "*hiss*"}') == (
        "hated", "tail pulling")
    assert parse_feeling('{"scene": "x", "feeling": "loved", "about": "tuna", "reply": "*ea') == ("loved", "tuna")
    assert parse_feeling('{"scene": "x", "reply": "*naps*"}') == ("", "")


def test_links_reach_whiskers_as_what_people_see():
    assert readable("lol https://tenor.com/view/cat-vibing-cat-jam-gif-18913937") == "lol [sends a gif: cat vibing cat jam]"
    assert readable("https://giphy.com/gifs/cat-funny-kitten-3o7TKSjRrfIPjeiVyM") == "[sends a gif: cat funny kitten]"
    assert readable("https://media.tenor.com/abc/tenor.gif") == "[sends a gif]"
    assert readable("https://imgflip.com/gif/8abc12") == readable("https://i.imgflip.com/8abc12.gif") == "[sends a gif]"
    assert readable("https://cdn.discordapp.com/attachments/1/2/IMG_123.PNG") == "[sends a picture]"
    assert readable("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "[shares a YouTube video]"
    assert readable("https://www.tiktok.com/@cats/video/72345 the end") == "[shares a TikTok] the end"
    assert readable("https://x.com/cats/status/1") == "[shares a tweet]"
    assert readable("see https://example.com/page?x=1.") == "see [shares a link: example.com]"
    assert readable("movie night <t:1700000000:R>") == "movie night [a time]"


def test_run_together_sounds_and_a_sound_holding_an_actions_asterisk():
    from whiskers.text import cat_only

    assert cat_only("*yowls and wriggles* MRRPMRRPMRRP!") == "*yowls and wriggles* MRRPMRRPMRRP!"
    assert cat_only("*MRRP! *dashes to Brandon, tail straight up*") == "MRRP! *dashes to Brandon, tail straight up*"
    assert cat_only("*MEOW! *sits down to think about it* MRRP") == "MEOW! *sits down to think about it* MRRP"
    assert cat_only("*thinks that you are weird* *naps*") == "*naps*"
    # Speech tacked onto an action is cut off, and the action stays
    assert cat_only("*looks up at him with wide eyes, then back to the box, as if to say 'what? This is awesome!'* "
                    "MRRP!") == "*looks up at him with wide eyes, then back to the box* MRRP!"
    assert cat_only("*stares at Kai and says hi*") == "*stares at Kai*"
    assert cat_only("*looks at Milo with a chirp* MREEP?") == "*looks at Milo with a chirp* MREEP?"


def test_cats_swish_their_tails_instead_of_wagging():
    from whiskers.text import no_wagging

    assert no_wagging("*wags his tail slowly* MRRP?") == "*swishes his tail slowly* MRRP?"
    assert no_wagging("*flops into the food, tail wagging*") == "*flops into the food, tail swishing*"
    assert no_wagging("*Wags tail* and wagons") == "*Swishes tail* and wagons"


def test_replies_are_read_from_json_even_while_being_written():
    from whiskers.text import narration_as_action, read_reply, written_reply

    assert read_reply('{"scene": "x", "reply": "*naps*", "feeling": "Loved", "about": "tuna."}') == (
        "x", "*naps*", "loved", "tuna")
    assert read_reply("just some text") == ("", "just some text", "", "")
    assert written_reply('{"scene": "x", "reply": "*na') is None
    assert written_reply('{"scene": "x", "reply": "*naps*"') == "*naps*"
    assert narration_as_action("Whiskers leans into the hand and purrs.", "Whiskers") == "*leans into the hand and purrs*"
    assert narration_as_action("He flops over!", "Whiskers") == "*flops over*"
    assert narration_as_action("Hello there, I am a cat.", "Whiskers") == ""
    assert narration_as_action("What do you want?", "Whiskers") == ""
