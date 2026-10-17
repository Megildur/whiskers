"""Small text helpers: tidying Discord messages for the model and the model's output for Discord."""
from __future__ import annotations

import json
import re
from typing import NamedTuple
from urllib.parse import urlparse

CUSTOM_EMOJI = re.compile(r"<a?:(\w+):\d+>")
DISCORD_TIME = re.compile(r"<t:\d+(?::\w)?>")
URL = re.compile(r"https?://[^\s<>]+")
IMAGE_FILE = re.compile(r"\.(png|jpe?g|webp|bmp)$", re.I)
VIDEO_FILE = re.compile(r"\.(mp4|mov|webm)$", re.I)
WORD = re.compile(r"[a-z0-9']{3,}")
SPEAKER_LABEL = re.compile(r"^\**\s*([^\s:*][^:\n*]{0,40}?)\s*(?:\*\*)?\s*:\s*(?:\*\*)?\s*")

# Sounds a cat can make, stretched any amount and run together: meeeow, mreow, mrrrp, prrt, hsss, mrrpmrrp...
CAT_SOUND = re.compile(
    r"^(?:m+r*e+o+w+|m+r*e+w+|m+i*a+o+w*|m+r+(?:r|p|a+p|o+w+)+|m+e{2,}p+|p+u*r{2,}[prt]*|p+r+[pt]+|h+i*s{2,}|"
    r"c+h+i*r+u*p+|t+r+i*l+|n+y+a+n*|y+e*o+w+l*|r+a+w+r+|b+r{2,}[pt]*|g+r{2,}|r{2,}[pt]*|m{2,}|m+l+e+m+|"
    r"b+l+e+p+|n+o+m+)+$",
    re.I,
)
ACTION_OR_PLAIN = re.compile(r"(\*[^*\n]+\*|(?<!\w)_[^_\n]+_(?!\w))")
SPEECH_IN_ACTION = re.compile(
    r"[\"“”:]|\b(says?|said|whispers?|speaks?|talks?|telepath\w*)\b|\b(thinks?|thoughts?)\s+(that|you|i|he|she|they)\b", re.I,
)
# Plain text right after an action that carries the action on: "*launches up*, lands running and hides"
NARRATION = re.compile(r"^(?:[,;]|(?:and|then|before|while|until|as)\b)", re.I)
NOT_NARRATION = re.compile(r"[\"“”?]|\b(i|i'm|me|my|you|you're|your|we|hi|hello|hey)\b", re.I)

STOPWORDS = frozenset("""
the and you your you're for are was were but not with this that have has had what when where who why how
just like can could would should will its it's about from they them their there then than into out our
very really too also some any all get got going gonna want did does doing don't i'm im lol lmao yes yeah
okay one two now here him her his she he we me my mine yours been being because which while over more
much most own same such only other again still ever even well back make made know think see say said
""".split())


def describe_link(url: str) -> str:
    """A link as what people see in Discord: "[sends a gif: cat vibing]" instead of a long URL."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    path = parsed.path.rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    if host.endswith("imgflip.com") and "/gif/" in path:
        return "[sends a gif]"
    if host.endswith("tenor.com") or host.endswith("giphy.com") or slug.lower().endswith(".gif"):
        if "/view/" in path:  # tenor.com/view/cat-vibing-gif-18913937
            words = re.sub(r"-gif-\d+$", "", slug)
        elif "/gifs/" in path and "-" in slug:  # giphy.com/gifs/cat-funny-3o7TKSjRrfIPjeiVyM
            words = slug.rsplit("-", 1)[0]
        else:
            words = ""
        words = " ".join(words.replace("-", " ").split())
        return f"[sends a gif: {words}]" if words else "[sends a gif]"
    if host.endswith("discordapp.com") or host.endswith("discordapp.net"):
        if IMAGE_FILE.search(path):
            return "[sends a picture]"
        if VIDEO_FILE.search(path):
            return "[sends a video]"
    if host in ("youtube.com", "youtu.be", "music.youtube.com"):
        return "[shares a YouTube video]"
    if host.endswith("tiktok.com"):
        return "[shares a TikTok]"
    if host in ("twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com"):
        return "[shares a tweet]"
    if host.endswith("twitch.tv"):
        return "[shares a Twitch stream]"
    if host.endswith("spotify.com"):
        return "[shares a song]"
    return f"[shares a link: {host}]" if host else "[shares a link]"


def readable(content: str) -> str:
    """Discord markup the model doesn't need to see, made plain."""
    content = CUSTOM_EMOJI.sub(r":\1:", content)
    content = DISCORD_TIME.sub("[a time]", content)
    content = URL.sub(lambda m: describe_link(m.group(0)), content)
    lines = (" ".join(line.split()) for line in content.splitlines())
    return "\n".join(line for line in lines if line)


def person_name(display_name: str, fallback: str = "someone") -> str:
    """A Discord display name as a plain name, so the model treats it as a person, not a thing.

    "ANGEL ‼️" -> "Angel", "xX_Shadow_Xx" stays, emoji and decorations go.
    """
    kept = "".join(ch if ch.isalnum() or ch in " '-_." else " " for ch in display_name)
    name = " ".join(kept.split()).strip(" -_.'")
    if not name:
        name = " ".join("".join(ch if ch.isalnum() else " " for ch in fallback).split()) or "someone"
    if name.isupper() and len(name) > 3:
        name = name.title()
    return name[:32]


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


def is_cat_sound(token: str) -> bool:
    letters = re.sub(r"[^A-Za-z-]", "", token)
    if not letters.strip("-"):
        return False  # punctuation or emoji on their own; small models scatter emoji everywhere
    return all(CAT_SOUND.match(part) for part in letters.split("-") if part)


def _calm(action: str) -> str:
    """*GLARES UP AT THE WINDOW* -> *glares up at the window*. Short shouts like *YOWLS* stay."""
    letters = [ch for ch in action if ch.isalpha()]
    if len(letters) >= 12 and sum(ch.isupper() for ch in letters) > len(letters) * 0.6:
        return action.lower()
    return action


def cat_only(text: str) -> str:
    """Keep only what a cat that can't talk could do: *actions* and cat sounds.

    Small models drift into human words even when told not to, so this is enforced here instead of
    trusting the prompt. Returns "" if nothing cat-like is left.
    """
    text = re.sub(r"\*{2,}", "", text)  # bold markers; single asterisks are actions
    text = re.sub(r"[{}]", " ", text)  # stray JSON braces
    text = re.sub(r"(?<=\w) \* (?=\w)", ", ", text)  # *yowls * crash-lands* : asterisks used as commas
    if text.count("*") % 2:  # "*MRRP! *dashes over*": a sound took the action's opening asterisk
        text = re.sub(r"\*\s*([^*\s][^*]*?)\s+\*(?=\S)",
                      lambda m: m.group(1) + " *" if all(is_cat_sound(t) for t in m.group(1).split()) else m.group(0),
                      text, count=1)
    if text.count("*") % 2 and re.search(r"\*\s*\w[^*]*$", text):
        text += "*"  # an action that was never closed
    kept: list[str] = []
    for part in ACTION_OR_PLAIN.split(text):
        part = part.strip()
        if not part:
            continue
        if part[0] in "*_" and part[-1] == part[0] and len(part) > 2:
            action = " ".join(part[1:-1].split()).strip(" ,;")
            if action and not SPEECH_IN_ACTION.search(action):
                kept.append(f"*{_calm(action)}*")
            continue
        words = part.split()
        trailing: list[str] = []
        while words and is_cat_sound(words[-1]):
            trailing.insert(0, words.pop())
        rest = " ".join(words)
        narration = rest.strip(" ,;.")
        if (narration and kept and kept[-1].startswith("*") and NARRATION.match(rest)
                and not NOT_NARRATION.search(rest) and not SPEECH_IN_ACTION.search(rest)):
            # The model forgot the asterisks partway through an action; keep it as part of that action.
            joiner = ", " if rest[0] in ",;" else " "
            kept[-1] = kept[-1][:-1] + joiner + _calm(narration) + "*"
            words = []
        sounds = [token.strip("*_") for token in words + trailing if is_cat_sound(token)]
        if sounds:
            kept.append(" ".join(sounds))
    result = " ".join(kept)
    if len(result) > 400:
        result = result[:400].rsplit("*", 1)[0].rstrip()
        if result.count("*") % 2:
            result += "*"
    return result


def _sound_key(token: str) -> str:
    return re.sub(r"(.)\1+", r"\1", re.sub(r"[^a-z]", "", token.lower()))  # "MRRRP!" -> "mrp"


def _sounds(text: str) -> set[str]:
    return {_sound_key(token) for part in ACTION_OR_PLAIN.split(text) if not part.strip().startswith(("*", "_"))
            for token in part.split() if is_cat_sound(token)}


def drop_repeated_sounds(text: str, recent: list[str]) -> str:
    """Leave out a cat sound Whiskers made in either of its last two messages, so it doesn't end every
    reply with "MRRP!". Only when an action is left to carry the reply."""
    used = set().union(*(_sounds(message) for message in recent[-2:])) if recent else set()
    if not used:
        return text
    kept = []
    for part in ACTION_OR_PLAIN.split(text):
        part = part.strip()
        if part.startswith(("*", "_")):
            kept.append(part)
        elif part:
            tokens = [t for t in part.split() if not (is_cat_sound(t) and _sound_key(t) in used)]
            if tokens:
                kept.append(" ".join(tokens))
    result = " ".join(kept)
    return result if "*" in result else text


def as_stage_directions(text: str, his: str = "its", himself: str = "itself") -> str:
    """Inside *actions*, "my"/"myself" become third person: *flops onto my back* -> *flops onto his back*."""
    def fix(match: re.Match) -> str:
        action = re.sub(r"\bmyself\b", himself, match.group(0))
        return re.sub(r"\b[Mm]y\b", his, action)
    return re.sub(r"\*[^*\n]+\*", fix, text)


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


def parse_feeling(text: str) -> tuple[str, str]:
    """The "feeling" and "about" fields of a reply, lowercased and tidied; ("", "") if they're missing."""
    stripped = text.strip()
    try:
        data = json.loads(stripped)
    except ValueError:
        data = None
    if isinstance(data, dict):
        feeling, about = str(data.get("feeling") or ""), str(data.get("about") or "")
    else:
        feeling, about = _json_string_field(stripped, "feeling") or "", _json_string_field(stripped, "about") or ""
    return _tidy_feeling(feeling, about)


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


# In a JSON reply, the message is finished at the closing quote of "reply" (the grammar allows nothing else).
JSON_REPLY_FINISHED = re.compile(r'"reply"\s*:\s*"(?:[^"\\]|\\.)*"')


class ReplyParts(NamedTuple):
    scene: str
    reply: str
    feeling: str
    about: str


def _tidy_feeling(feeling: str, about: str) -> tuple[str, str]:
    return feeling.strip().lower(), " ".join(about.split()).strip(" .\"'")[:60]


def read_reply(text: str) -> ReplyParts:
    """The parts of a JSON reply, even one cut off partway. Plain text is all message."""
    scene, reply = parse_reply(text)
    return ReplyParts(scene, reply, *parse_feeling(text))


def written_reply(text: str) -> str | None:
    """The message of a JSON reply that is still being written, once the model has finished writing it."""
    return parse_reply(text)[1] if JSON_REPLY_FINISHED.search(text) else None


def narration_as_action(text: str, name: str) -> str:
    """"Whiskers leans into the hand and purrs." -> "*leans into the hand and purrs*": an action the model
    wrote without asterisks, which cat_only would otherwise drop. "" if it looks like speech."""
    line = " ".join(text.split())
    if not line or "*" in line or NOT_NARRATION.search(line) or SPEECH_IN_ACTION.search(line):
        return ""
    line = re.sub(rf"^(?:{re.escape(name)}|he|she|the cat)\s+", "", line, flags=re.I).rstrip(" .!")
    return f"*{line}*" if line[:1].islower() else ""


ACTION_FILLER = STOPWORDS | frozenset("""
softly gently slowly quickly then their them against into onto with from over away again while before after
around toward towards each other still just little bit eyes head tail paws paw hand hands legs leg lap ears ear
nose face fur body chest side
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
            if word in ACTION_FILLER or word in ignored or is_cat_sound(word):
                continue  # sounds are spaced out in code (drop_repeated_sounds)
            stem = _stem(word)
            if stem in seen:
                continue
            seen.add(stem)
            counts[stem] = counts.get(stem, 0) + 1
            shown.setdefault(stem, word)
    repeated = sorted((stem for stem, n in counts.items() if n >= 2), key=lambda s: -counts[s])
    return [shown[stem] for stem in repeated[:limit]]
