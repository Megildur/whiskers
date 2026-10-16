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


class LLMError(Exception):
    pass


@dataclass
class ChatResult:
    text: str
    tokens: int
    seconds: float


class LLMClient:
    def __init__(self, cfg: Config, engine: Engine) -> None:
        self.cfg = cfg
        self.engine = engine
        self._session: aiohttp.ClientSession | None = None
        self.tokens_per_second: float | None = None
        self.prompt_tokens_per_second: float | None = None
        self.last_reply_seconds: float | None = None

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
        extra: dict | None = None,
    ) -> ChatResult:
        """Stream a chat completion. Cancelling the task closes the connection, which stops generation."""
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
        else:
            body["stream_options"] = {"include_usage": True}
        headers = {"Authorization": f"Bearer {self.cfg.llm_api_key}"} if self.cfg.llm_api_key else {}

        started = time.monotonic()
        parts: list[str] = []
        tokens = 0
        timings: dict = {}
        # CPU prompt processing can take minutes for a long uncached prompt, so be patient before the first token.
        timeout = aiohttp.ClientTimeout(total=900, sock_connect=10, sock_read=600)
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
                        raise LLMError(str(chunk["error"])[:300])
                    timings = chunk.get("timings") or timings
                    usage = chunk.get("usage")
                    if usage and usage.get("completion_tokens"):
                        tokens = usage["completion_tokens"]
                    for choice in chunk.get("choices") or []:
                        piece = (choice.get("delta") or {}).get("content")
                        if piece:
                            parts.append(piece)
                            tokens = max(tokens, len(parts))
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
        log.debug("Generated %d tokens in %.1fs", tokens, seconds)
        return ChatResult(text=text.strip(), tokens=tokens, seconds=seconds)


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
