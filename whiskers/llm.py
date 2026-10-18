"""Talks to the model through the OpenAI-compatible chat API (llama-server, Ollama, ...)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

import aiohttp

from .config import Config
from .engine import Engine

log = logging.getLogger("whiskers.llm")

THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)
CHORES = "chores"  # the conversation key for background chores


class LLMError(Exception):
    pass


@dataclass
class ChatResult:
    text: str
    tokens: int
    seconds: float
    prompt_tokens: int = 0  # prompt tokens the model had to read (llama-server only)
    cached_tokens: int = 0  # prompt tokens reused from its cache instead


class LLMClient:
    def __init__(self, cfg: Config, engine: Engine) -> None:
        self.cfg = cfg
        self.engine = engine
        self._session: aiohttp.ClientSession | None = None
        self.tokens_per_second: float | None = None
        self.prompt_tokens_per_second: float | None = None
        self.last_reply_seconds: float | None = None
        self._starts = 0
        self._slots: dict[object, int] = {}  # conversation -> its llama-server slot, least recently used first
        self._prefixes: dict[int, str] = {}  # slot -> the system prompt it has cached
        # Conversations that lost their slot, saved to disk: conversation -> (file, the system prompt in it),
        # least recently saved first. And a saved copy of a slot holding the current system prompt.
        self._saved: dict[object, tuple[str, str]] = {}
        self._system_file: tuple[str, str] | None = None  # (system prompt, file)

    # ---- llama-server slots: one per conversation, so each keeps its own cache ----

    def _check_restart(self) -> None:
        if self.engine.starts != self._starts:  # a fresh server has nothing cached (and no saved slots)
            self._starts = self.engine.starts
            self._slots.clear()
            self._prefixes.clear()
            self._saved.clear()
            self._system_file = None

    def conversation_slots(self) -> list[int]:
        return list(range(self.engine.chat_slots))

    def _assign(self, conversation: object) -> tuple[int | None, object | None, bool]:
        """(slot, the conversation it was taken from or None, whether the slot is new to this conversation)."""
        self._check_restart()
        if conversation == CHORES:
            return self.engine.chore_slot, None, False
        count = self.engine.chat_slots
        if not count:
            return None, None, False
        if conversation in self._slots:
            self._slots[conversation] = self._slots.pop(conversation)  # now the most recently used
            return self._slots[conversation], None, False
        used = set(self._slots.values())
        free = [s for s in range(count) if s not in used]
        evicted = None
        if free:
            slot = free[0]
        else:  # take the least recently used conversation's slot
            evicted = next(iter(self._slots))
            slot = self._slots.pop(evicted)
        self._slots[conversation] = slot
        return slot, evicted, True

    def slot_for(self, conversation: object) -> int | None:
        """The slot a conversation (a channel, or CHORES) uses. None lets the server choose."""
        return self._assign(conversation)[0]

    @property
    def can_save_slots(self) -> bool:
        return getattr(self.engine, "slot_dir", None) is not None

    async def ready_slot(self, conversation: object, system: str) -> int | None:
        """The slot for a conversation, with as much of its prompt already loaded as possible.

        Reading a prompt from scratch is the slowest part of a reply on a CPU: a whole conversation takes one
        or two minutes. Saving a slot to disk and restoring it takes a second or two, so when a channel loses
        its slot to another one it's saved, and it's restored when it comes back. A slot that doesn't have the
        current system prompt yet (a channel's first reply, a new journal) gets a copy from one that does.
        """
        slot, evicted, new = self._assign(conversation)
        if slot is None or not self.can_save_slots or conversation == CHORES:
            return slot
        try:
            if evicted is not None and self._prefixes.get(slot):
                await self._save_conversation(evicted, slot)
            if new and conversation in self._saved:
                filename, prefix = self._saved.pop(conversation)
                result = await self._slot_action(slot, "restore", filename)
                self._prefixes[slot] = prefix
                self._delete(filename)
                log.info("Restored channel %s's conversation into slot %d (%s tokens in %.1fs).", conversation,
                         slot, result.get("n_restored"), result.get("seconds", 0))
                return slot
            if self._prefixes.get(slot) != system:
                await self.load_system(slot, system)
        except LLMError as error:
            log.warning("Couldn't move a conversation in or out of slot %d: %s", slot, error)
        return slot

    async def load_system(self, slot: int, system: str) -> bool:
        """Copy the current system prompt into a slot from a saved copy, or from another slot that has it.
        False if none has it yet (then it has to be read the slow way)."""
        self._check_restart()
        if not self.can_save_slots:
            return False
        if not (self._system_file and self._system_file[0] == system):
            donor = next((s for s, prefix in self._prefixes.items() if prefix == system and s != slot), None)
            if donor is None:
                return False
            await self.save_system(donor, system)
        result = await self._slot_action(slot, "restore", self._system_file[1])
        self._prefixes[slot] = system
        log.info("Copied Whiskers' prompt into slot %d (%s tokens in %.1fs).", slot, result.get("n_restored"),
                 result.get("seconds", 0))
        return True

    async def save_system(self, slot: int, system: str) -> None:
        """Keep a copy of a slot that has the current system prompt, for slots that don't have it yet."""
        if self.can_save_slots and slot is not None:
            await self._slot_action(slot, "save", "system.bin")
            self._system_file = (system, "system.bin")

    async def _save_conversation(self, conversation: object, slot: int) -> None:
        keep = max(0, getattr(self.cfg, "llm_saved_channels", 0))
        if not keep:
            return
        filename = f"chat-{conversation}.bin"
        result = await self._slot_action(slot, "save", filename)
        self._saved.pop(conversation, None)
        self._saved[conversation] = (filename, self._prefixes.get(slot, ""))
        while len(self._saved) > keep:  # forget the channel that's been away longest
            old = next(iter(self._saved))
            self._delete(self._saved.pop(old)[0])
        log.info("Saved channel %s's conversation to make room in slot %d (%s tokens in %.1fs).", conversation,
                 slot, result.get("n_saved"), result.get("seconds", 0))

    def _delete(self, filename: str) -> None:
        try:
            (self.engine.slot_dir / filename).unlink(missing_ok=True)
        except OSError:
            pass

    async def _slot_action(self, slot: int, action: str, filename: str) -> dict:
        """Save a llama-server slot to a file in the engine's slot folder, or restore one from it."""
        started = time.monotonic()
        url = f"{self.engine.base_url.removesuffix('/v1')}/slots/{slot}?action={action}"
        try:
            async with self._get_session().post(url, json={"filename": filename},
                                                timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    raise LLMError(f"{action} failed: HTTP {resp.status}: {(await resp.text())[:200]}")
                result = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise LLMError(f"{action} failed: {error}") from error
        result["seconds"] = time.monotonic() - started
        return result

    def cached_prefix(self, slot: int) -> str | None:
        self._check_restart()
        return self._prefixes.get(slot)

    def remember_prefix(self, slot: int | None, prefix: str) -> None:
        if slot is not None:
            self._prefixes[slot] = prefix

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # llama-server closes the connection after a streamed reply, so a reused keep-alive connection
            # fails with "Server disconnected". A new connection per request avoids that and costs nothing here.
            # trust_env lets an external server sit behind a proxy; the local engine is never proxied.
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True),
                trust_env=self.cfg.llm_backend == "external",
            )
        return self._session

    async def chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float,
        stop: list[str] | None = None,
        json_schema: dict | None = None,
        should_stop: Callable[[str], bool] | None = None,
        on_text: Callable[[str], None] | None = None,
        extra: dict | None = None,
        slot: int | None = None,
    ) -> ChatResult:
        """Stream a chat completion. Cancelling the task closes the connection, which stops generation.

        `on_text` sees the text so far as it streams in. `slot` pins the request to a llama-server slot."""
        if not self.engine.ready:
            raise LLMError("the AI engine isn't ready yet")
        managed = self.cfg.llm_backend == "managed"
        body: dict = {
            "model": self.engine.model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": 0.92,
            "stream": True,
        }
        if stop:
            body["stop"] = stop if managed else stop[:4]
        if json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": json_schema, "strict": True},
            }
        if managed:
            body.update(min_p=0.05, repeat_penalty=1.08, cache_prompt=True, **(extra or {}))
            if slot is not None:
                body["id_slot"] = slot
        else:
            body["stream_options"] = {"include_usage": True}
        headers = {"Authorization": f"Bearer {self.cfg.llm_api_key}"} if self.cfg.llm_api_key else {}

        started = time.monotonic()
        parts: list[str] = []
        tokens = 0
        timings: dict = {}
        # CPU prompt processing can take a few minutes for a long uncached prompt, so be patient before the
        # first token, but don't let one stuck request hold up everyone for long.
        timeout = aiohttp.ClientTimeout(total=600, sock_connect=10, sock_read=300)
        try:
            async with self._get_session().post(
                f"{self.engine.base_url}/chat/completions", json=body, headers=headers, timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    raise LLMError(f"HTTP {resp.status}: {(await resp.text())[:300]}")
                async for raw in resp.content:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if "error" in chunk:
                        if not parts:
                            raise LLMError(str(chunk["error"])[:300])
                        # e.g. the output was cut off mid-JSON; the caller can still use what arrived
                        log.warning("The AI engine reported an error after replying: %s", str(chunk["error"])[:200])
                        break
                    timings = chunk.get("timings") or timings
                    usage = chunk.get("usage")
                    if usage and usage.get("completion_tokens"):
                        tokens = usage["completion_tokens"]
                    for choice in chunk.get("choices") or []:
                        piece = (choice.get("delta") or {}).get("content")
                        if piece:
                            parts.append(piece)
                            tokens = max(tokens, len(parts))
                    if on_text and parts:
                        on_text("".join(parts))
                    if should_stop and parts and should_stop("".join(parts)):
                        break
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise LLMError(f"couldn't reach the AI engine: {error}") from error

        seconds = time.monotonic() - started
        if timings.get("predicted_per_second"):
            self.tokens_per_second = float(timings["predicted_per_second"])
        elif tokens and seconds:
            self.tokens_per_second = tokens / seconds
        if timings.get("prompt_per_second") and timings.get("prompt_n", 0) > 16:
            self.prompt_tokens_per_second = float(timings["prompt_per_second"])
        text = THINK_BLOCK.sub("", "".join(parts))
        if "<think>" in text:  # thinking never finished; nothing usable after it
            text = text.split("<think>", 1)[0]
        prompt_tokens, cached_tokens = int(timings.get("prompt_n") or 0), int(timings.get("cache_n") or 0)
        log.debug("Read %d prompt tokens (%d cached) and wrote %d tokens in %.1fs", prompt_tokens, cached_tokens,
                  tokens, seconds)
        return ChatResult(text=text.strip(), tokens=tokens, seconds=seconds, prompt_tokens=prompt_tokens,
                          cached_tokens=cached_tokens)


def parse_json(text: str) -> dict | None:
    """Parse a JSON object from model output, tolerating stray text around it."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(text[start:end + 1])
            return value if isinstance(value, dict) else None
        except ValueError:
            return None
    return None
