"""A scripted model for tests, with the real client's slot bookkeeping."""
import asyncio

from whiskers.engine import Engine
from whiskers.llm import ChatResult, LLMClient


class ScriptedLLM(LLMClient):
    """Streams back `answer(messages, kwargs)` in small pieces, like llama-server would."""

    def __init__(self, slots: int = 2) -> None:
        engine = Engine()
        engine.ready, engine.chat_slots, engine.chore_slot, engine.starts = True, slots, slots, 1
        super().__init__(cfg=None, engine=engine)

    async def answer(self, messages: list[dict], kwargs: dict) -> str:
        raise NotImplementedError

    async def chat(self, messages, **kwargs):
        text = await self.answer(messages, kwargs)
        on_text = kwargs.get("on_text")
        for end in range(8, len(text) + 8, 8):
            if on_text:
                on_text(text[:end])
            await asyncio.sleep(0)
        return ChatResult(text, 10, 0.1)
