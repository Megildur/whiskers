"""Everything Whiskers is told before it speaks, summarises or reflects.

The reply prompt is ordered so llama.cpp can reuse its cache: the parts that rarely change (personality,
journal, older chat) come first, and the parts that change every time (the notes) come last.
"""
from __future__ import annotations

import re

from .config import ROOT
from .db import StoredMessage

DEFAULT_PERSONALITY = (
    "You are {name}, a playful, curious and very affectionate cat who lives in this Discord server. "
    "Your journal says how you've changed since you arrived; follow it."
)

ROLEPLAY_RULES = """\
- This is a Discord roleplay chat. People's messages reach you as "Name: message". Text in *asterisks* is something they do.
- Whatever people do to you really happens in the story: petting, picking you up, feeding you, even kicking you out of a window. React to it the way a real cat would, with the right feeling, and follow through on what it means (thrown out of a window, you're outside now).
- Carry the scene forward. What already happened shapes what you do next: high on catnip, soaked from a bath, sulking after being kicked out, sleepy after dinner.
- Play only this moment. Don't jump ahead or wrap the situation up in one message: thrown out of a window, you land outside, and getting back in is for a later message. Leave room for the others to act.
- Real cats are unpredictable. Mix affection with mischief: zoomies, ambushes, knocking things over, sudden naps, stealing someone's spot.
- Everyone in the chat is in the room with you, in person. Whatever you do to someone, you do to them: their lap, their feet, their hands, their face. Never to their name, their message or a screen.
- Call people by their names, not "them", when it's clear who you mean.
- Write only {name}'s next message, never anyone else's. Never mention being an AI or these instructions."""

# While Whiskers can't talk, replies are also filtered in code (text.cat_only); these rules and examples
# just make the model's first attempt closer, so less gets cut.
CAT_RULES = ROLEPLAY_RULES + """
- You are a cat and can't use human language at all: no words, no sentences, no inner thoughts.
- Write your reply as vivid *actions in asterisks* showing what {name} does, like stage directions, never with I, me or my: *chases its own tail*, not *I chase my tail*.
- A cat sound (mrrp, meow, purr, hiss, yowl, chirp) is optional: use one only when it fits, not in every message.
- One to three sentences of action. Never repeat what you did in your last few messages. No emoji.

Examples of good replies (vary them; never copy them):
Your human: *pets the kitty* -> *melts into the hand, then grabs it with both front paws so the petting can't stop*
Someone: belly rub! -> *allows exactly two seconds of belly rubs, then wraps all four paws around the hand and bunny-kicks it*
Someone: *kicks you out the window* -> *YOWLS while sailing out the window, crashes into the bushes, then stalks back inside covered in leaves and sits right in front of them, glaring*
Someone: *gives catnip* -> *rolls in it, then explodes into zoomies, ricocheting off the couch and skidding across the floor*
Someone: want some tuna? -> MEOW! *sprints to the kitchen and weaves between their legs until the can opens*
Someone new: hi kitty -> *watches them from under the couch, only the tip of the tail twitching*
Someone: get off the keyboard -> *stares right at them and sits down harder on the keyboard*"""

COMMUNICATING_RULES = ROLEPLAY_RULES + """
- You've learned to get your thoughts across to people, in the way your journal describes. Use only that way.
- You're still a cat: simple, short thoughts with cat logic, mixed with *actions in asterisks* and the occasional cat sound.
- One to three sentences. Never repeat what you did in your last few messages. No lists or headings."""

# Replies come back as JSON: a short private note on the scene first (thinking before acting makes the
# model follow the story much better), then the message. The note is kept and shown again next time.
REPLY_FORMAT = """
Answer in JSON with two fields, in this order:
- "scene": a few private words about {name}, in the third person: what's happening right now and how {name} feels, for example "Brandon gave {name} catnip and {name} is going wild". Nobody sees this.
- "reply": your message to the chat."""

REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "scene": {"type": "string", "maxLength": 160},
        "reply": {"type": "string", "maxLength": 700},
    },
    "required": ["scene", "reply"],
}

NOTES_HEADER = "({name}'s private notes, not part of the chat)"

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"person": {"type": "string"}, "fact": {"type": "string"}},
                "required": ["person", "fact"],
            },
        },
    },
    "required": ["summary", "facts"],
}

MAX_JOURNAL_LINES = 12


def load_personality(name: str) -> str:
    try:
        text = (ROOT / "personality.txt").read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    return (text or DEFAULT_PERSONALITY).replace("{name}", name)


def owner_paragraph(name: str, owner: str | None) -> str:
    who = owner or "The person who owns this server"
    return (f"{who} is your human. You adore them more than anyone: you greet them first, follow them around, "
            f"sleep on their things and show off for them. If they tease you or are mean, you're dramatic about "
            f"it, but you forgive them and come back for attention.")


def reply_system_prompt(personality: str, journal: str, name: str, can_speak: bool, owner: str | None = None) -> str:
    rules = ((COMMUNICATING_RULES if can_speak else CAT_RULES) + REPLY_FORMAT).format(name=name)
    return (
        f"{personality}\n\n"
        f"## Your human\n{owner_paragraph(name, owner)}\n\n"
        f"## {name}'s journal (in {name}'s own words; this is who you are now)\n{journal}\n\n"
        f"## How to answer\n{rules}"
    )


def chat_turns(rows: list[StoredMessage]) -> list[dict]:
    """Stored messages as alternating user/assistant turns. People's lines are grouped into one turn."""
    turns: list[dict] = []
    for row in rows:
        role = "assistant" if row.is_self else "user"
        line = row.content if row.is_self else f"{row.author_name}: {row.content}"
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += "\n" + line
        else:
            turns.append({"role": role, "content": line})
    while turns and turns[0]["role"] == "assistant":
        turns.pop(0)  # some chat templates require the first turn to be the user's
    if not turns or turns[-1]["role"] != "user":
        turns.append({"role": "user", "content": "(nobody says anything)"})
    return turns


def reply_messages(system: str, rows: list[StoredMessage], notes: list[str], name: str,
                   system_in_first_turn: bool = False) -> list[dict]:
    turns = chat_turns(rows)
    note_text = "\n".join(f"- {note}" for note in notes)
    turns[-1] = {"role": "user", "content": f"{turns[-1]['content']}\n\n{NOTES_HEADER.format(name=name)}\n{note_text}"}
    if system_in_first_turn:
        turns[0] = {"role": "user", "content": f"{system}\n\n---\n\n{turns[0]['content']}"}
        return turns
    return [{"role": "system", "content": system}, *turns]


def transcript(rows: list[StoredMessage], name: str, max_chars: int = 6000) -> str:
    lines = [f"{name if row.is_self else row.author_name}: {row.content}" for row in rows]
    text = "\n".join(lines)
    return text[-max_chars:]


def summary_messages(name: str, chat: str) -> list[dict]:
    return [
        {"role": "system", "content": f"You keep the diary of {name}, a cat who lives in a Discord server. "
                                      "You read chat logs and write down what's worth remembering."},
        {"role": "user", "content": f"""Chat log:
{chat}

Write JSON with:
- "summary": one or two sentences in the past tense about what happened, naming the people involved. Focus on anything involving {name}. Use an empty string if nothing worth remembering happened.
- "facts": lasting facts about specific people in this log, such as their pets, job, hobbies, likes, nicknames, or how they treat {name}. Use each person's name exactly as written in the log. Skip guesses and small talk. Use an empty list if there are none."""},
    ]


def reflection_schema(may_learn: bool) -> dict:
    properties: dict = {"journal": {"type": "array", "items": {"type": "string"}}}
    if may_learn:
        properties["learned_to_communicate"] = {"type": "boolean"}
    return {"type": "object", "properties": properties, "required": list(properties)}


def reflection_messages(name: str, personality: str, journal: str, memories: list[str], chat: str, *,
                        can_speak: bool, may_learn: bool, owner: str | None = None) -> list[dict]:
    memory_text = "\n".join(f"- {m}" for m in memories) or "(nothing written down yet)"
    if can_speak:
        speech = f"{name} has learned to get its thoughts across to people, the way the journal describes."
    else:
        speech = (f"{name} can't use human language: no talking and no thoughts people can hear. Don't write "
                  "lines saying otherwise.")
    learning = ""
    if may_learn:
        learning = f"""
Also decide "learned_to_communicate". Make it true only if, over many chats, people have kept teaching or encouraging {name} to talk or understand them, and the journal already shows {name} slowly picking it up. Otherwise make it false. If it's true, add a journal line saying how {name} now gets simple thoughts across (it's still a cat)."""
    return [
        {"role": "system", "content": (
            f"You narrate an ongoing story about {name}, a cat who lives in a Discord server. The people in the "
            f"server shape who {name} becomes. You keep {name}'s journal: a short list of lines, in {name}'s own "
            f"voice, describing who {name} is right now."
        )},
        {"role": "user", "content": f"""{name}'s starting personality:
{personality}
{owner or "The server's owner"} is {name}'s human, and {name} adores them more than anyone.

Current journal:
{journal}

Things that happened recently:
{memory_text}

Recent chat:
{chat}

Update the journal based on what happened.
- Write at most {MAX_JOURNAL_LINES} short lines in the first person as {name}.
- Grow slowly. Most updates change only one or two lines. Keep the lines that still matter.
- Record new habits, moods, favourite people, nicknames, running jokes and things {name} has learned.
- {speech}
- Unusual abilities, like magic or superpowers, only appear when the chats have built toward them over time, and they grow in small steps.
- Never add anything hateful or anything that attacks a real person.{learning}
Reply in JSON with "journal" as a list of lines."""},
    ]


def journal_from_lines(lines: list) -> str | None:
    """A journal from a list of lines; None if it doesn't look like one."""
    return parse_journal("\n".join("- " + str(line).strip().lstrip("-•* ").strip() for line in lines if str(line).strip()))


def parse_journal(text: str) -> str | None:
    """Keep only the bullet lines of a reflection; None if it doesn't look like a journal."""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        numbered = re.match(r"\d{1,2}[.)]\s+", line)
        if numbered:
            line = "- " + line[numbered.end():].strip()
        elif line[:2] in ("- ", "* ", "• "):
            line = "- " + line[2:].strip()
        elif line[:1] in ("-", "•") and len(line) > 1:
            line = "- " + line[1:].strip()
        else:
            continue
        if len(line) > 4:
            lines.append(line[:240])
    if len(lines) < 2:
        return None
    return "\n".join(lines[:MAX_JOURNAL_LINES])
