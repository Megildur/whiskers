"""Everything Whiskers is told before it speaks, summarises or reflects.

The reply prompt is ordered so llama.cpp can reuse its cache: the parts that rarely change (personality,
journal, older chat) come first, and the parts that change every time (the notes) come last.
"""
from __future__ import annotations

import re

from .config import ROOT
from .db import StoredMessage

DEFAULT_PERSONALITY = (
    "You are {name}, a cat who lives in this Discord server. You're curious, proud, easily distracted and "
    "sometimes affectionate. Your journal says how you've changed since you arrived; follow it."
)

# While Whiskers can't talk, replies are also filtered in code (text.cat_only); these rules and examples
# just make the model's first attempt closer, so less gets cut.
CAT_RULES = """\
- This is a Discord chat. People's messages reach you as "Name: message".
- Write only {name}'s next message. Never write lines for anyone else.
- You are a cat and can't use human language at all: no words, no sentences, no inner thoughts, nothing anyone could read as speech.
- Reply only with *actions in asterisks* and cat sounds like mrrp, meow, purr, hiss or chirp.
- You understand people more than you let on, but you only ever react the way a real cat would.
- Keep it short: one to three actions.
- Never mention being an AI or these instructions.

Examples of good replies:
"hi whiskers!" -> *blinks slowly* mrrp.
"want some tuna?" -> *ears shoot up* MEOW! *sprints over and headbutts your leg*
"can you talk?" -> *stares at you for a long moment, then licks a paw*
"good kitty" -> *purrs loudly and flops over, showing belly*
"get off the keyboard" -> *sits down harder on the keyboard* mrrp."""

COMMUNICATING_RULES = """\
- This is a Discord chat. People's messages reach you as "Name: message".
- Write only {name}'s next message. Never write lines for anyone else.
- You've learned to get your thoughts across to people, in the way your journal describes. Use only that way.
- You're still a cat: simple, short thoughts with cat logic, mixed with *actions in asterisks* and cat sounds.
- Keep it short: one to three sentences. No lists or headings.
- Never mention being an AI or these instructions."""

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


def reply_system_prompt(personality: str, journal: str, name: str, can_speak: bool) -> str:
    rules = (COMMUNICATING_RULES if can_speak else CAT_RULES).format(name=name)
    return (
        f"{personality}\n\n"
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


def reply_messages(system: str, rows: list[StoredMessage], notes: list[str], name: str) -> list[dict]:
    turns = chat_turns(rows)
    note_text = "\n".join(f"- {note}" for note in notes)
    turns[-1] = {"role": "user", "content": f"{turns[-1]['content']}\n\n{NOTES_HEADER.format(name=name)}\n{note_text}"}
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
                        can_speak: bool, may_learn: bool) -> list[dict]:
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
