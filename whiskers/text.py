"""Small text helpers: tidying Discord messages for the model and the model's output for Discord."""
from __future__ import annotations

import json
import re

CUSTOM_EMOJI = re.compile(r"<a?:(\w+):\d+>")
WORD = re.compile(r"[a-z0-9']{3,}")
SPEAKER_LABEL = re.compile(r"^\**\s*([^\s:*][^:\n*]{0,40}?)\s*(?:\*\*)?\s*:\s*(?:\*\*)?\s*")

# Sounds a cat can make, stretched any amount: meeeow, mrrrp, purrrr, hsss...
CAT_SOUND = re.compile(
    r"^(?:m+e+o+w+|m+e+w+|m+i*a+o+w*|m+r(?:r|p|o+w+)+|p+u*r{2,}[rt]*|h+i*s{2,}|c+h+i*r+p+|t+r+i+l+|"
    r"n+y+a+n*|y+o+w+l+|r+a+w+r+|b+r{2,}[pt]*|g+r{2,}|m{2,})$",
    re.I,
)
ACTION_OR_PLAIN = re.compile(r"(\*[^*\n]+\*|(?<!\w)_[^_\n]+_(?!\w))")
SPEECH_IN_ACTION = re.compile(r"[\"“”:]|\b(says?|said|whispers?|speaks?|talks?|thinks?|thoughts?|telepath\w*)\b", re.I)

STOPWORDS = frozenset("""
the and you your you're for are was were but not with this that have has had what when where who why how
just like can could would should will its it's about from they them their there then than into out our
very really too also some any all get got going gonna want did does doing don't i'm im lol lmao yes yeah
okay one two now here him her his she he we me my mine yours been being because which while over more
much most own same such only other again still ever even well back make made know think see say said
""".split())


def readable(content: str) -> str:
    """Discord markup the model doesn't need to see, made plain."""
    content = CUSTOM_EMOJI.sub(r":\1:", content)
    lines = (" ".join(line.split()) for line in content.splitlines())
    return "\n".join(line for line in lines if line)


def keywords(text: str, limit: int = 8) -> list[str]:
    seen: list[str] = []
    for word in WORD.findall(text.lower()):
        word = word.strip("'")
        if len(word) >= 3 and word not in STOPWORDS and word not in seen:
            seen.append(word)
    return seen[:limit]


def clean_reply(text: str, bot_name: str, other_names: list[str]) -> str:
    """Turn raw model output into one Discord message from the cat."""
    others = {name.lower() for name in other_names if name}
    bot = bot_name.lower()
    lines: list[str] = []
    for line in text.strip().splitlines():
        stripped = line.strip()
        if stripped.startswith("(") and "private notes" in stripped.lower():
            break
        label = SPEAKER_LABEL.match(stripped)
        if label:
            speaker = label.group(1).strip().lower()
            if speaker == bot:
                line = stripped[label.end():]
            elif speaker in others:
                break  # the model started writing someone else's next message
        lines.append(line)
    text = "\n".join(lines).strip()
    if len(text) >= 2 and text[0] == text[-1] == '"' and text.count('"') == 2:
        text = text[1:-1].strip()
    text = text.replace("@everyone", "@​everyone").replace("@here", "@​here")
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > 1900:
        cut = text[:1900]
        end = max(cut.rfind(". "), cut.rfind("* "), cut.rfind("\n"))
        text = cut[: end + 1] if end > 1000 else cut
    return text.strip()


def split_message(text: str, limit: int = 2000) -> list[str]:
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def ago(seconds: float) -> str:
    minutes = seconds / 60
    if minutes < 2:
        return "just now"
    for size, unit in ((60 * 24 * 30, "month"), (60 * 24 * 7, "week"), (60 * 24, "day"), (60, "hour"), (1, "minute")):
        if minutes >= size:
            count = int(minutes // size)
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return "just now"


def _is_cat_token(token: str) -> bool:
    letters = re.sub(r"[^A-Za-z-]", "", token)
    if not letters.strip("-"):
        return False  # punctuation or emoji on their own; small models scatter emoji everywhere
    return all(CAT_SOUND.match(part) for part in letters.split("-") if part)


def cat_only(text: str) -> str:
    """Keep only what a cat that can't talk could do: *actions* and cat sounds.

    Small models drift into human words even when told not to, so this is enforced here instead of
    trusting the prompt. Returns "" if nothing cat-like is left.
    """
    text = re.sub(r"\*{2,}", "", text)  # bold markers; single asterisks are actions
    kept: list[str] = []
    for part in ACTION_OR_PLAIN.split(text):
        part = part.strip()
        if not part:
            continue
        if part[0] in "*_" and part[-1] == part[0] and len(part) > 2:
            action = " ".join(part[1:-1].split())
            if action and not SPEECH_IN_ACTION.search(action):
                kept.append(f"*{action}*")
            continue
        sounds = [token for token in part.split() if _is_cat_token(token)]
        if sounds:
            kept.append(" ".join(sounds))
    result = " ".join(kept)
    if len(result) > 400:
        result = result[:400].rsplit("*", 1)[0].rstrip()
        if result.count("*") % 2:
            result += "*"
    return result


def _json_string_field(text: str, field: str) -> str | None:
    """Pull one string field out of JSON that may be cut off before its end."""
    match = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)("?)', text)
    if not match:
        return None
    raw = match.group(1)
    try:
        return json.loads(f'"{raw}"')
    except ValueError:
        return raw.replace('\\"', '"').replace("\\n", "\n")


def parse_reply(text: str) -> tuple[str, str]:
    """Split a {"scene": ..., "reply": ...} answer. Falls back to treating the whole text as the reply."""
    stripped = text.strip()
    if not stripped.startswith("{"):
        return "", stripped
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return str(data.get("scene") or "").strip(), str(data.get("reply") or "").strip()
    except ValueError:
        pass
    return (_json_string_field(stripped, "scene") or "").strip(), (_json_string_field(stripped, "reply") or "").strip()


ACTION_FILLER = STOPWORDS | frozenset("""
softly gently slowly quickly then their them against into onto with from over away again while before after
around toward towards each other still just little bit eyes head tail paws paw hand hands
""".split())


def _stem(word: str) -> str:
    word = re.sub(r"(.)\1+", r"\1", word)  # purrrr -> pur, nuzzles -> nuzles
    for suffix in ("ing", "es", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def repeated_actions(messages: list[str], ignore: set[str] = frozenset(), limit: int = 4) -> list[str]:
    """Words Whiskers keeps using across its recent messages, like "purrs" and "nuzzles"."""
    counts: dict[str, int] = {}
    shown: dict[str, str] = {}
    ignored = {w.lower() for w in ignore}
    for message in messages:
        seen = set()
        for word in re.findall(r"[a-z]{3,}", message.lower()):
            if word in ACTION_FILLER or word in ignored:
                continue
            stem = _stem(word)
            if stem in seen:
                continue
            seen.add(stem)
            counts[stem] = counts.get(stem, 0) + 1
            shown.setdefault(stem, word)
    repeated = sorted((stem for stem, n in counts.items() if n >= 2), key=lambda s: -counts[s])
    return [shown[stem] for stem in repeated[:limit]]
