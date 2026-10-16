import asyncio
import json
from types import SimpleNamespace

from whiskers.db import Database
from whiskers.llm import ChatResult
from whiskers.scheduler import Scheduler
from whiskers.simulate import SCENES, run_scenes


class FakeLLM:
    def __init__(self):
        self.calls = 0
        self.last_reply_seconds = None

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return ChatResult(json.dumps({"scene": f"scene {self.calls}", "reply": "*pounces on Angel's feet*"}), 5, 0.1)


def test_simulation_plays_every_scene_without_touching_real_memories(cfg, tmp_path):
    async def go():
        db = Database(tmp_path / "real.db")
        await db.connect()
        await db.set_journal(1, "- I'm Whiskers.\n- I love tuna.", "test")
        bot = SimpleNamespace(cfg=cfg, db=db, llm=FakeLLM(), scheduler=Scheduler(), user=SimpleNamespace(id=999),
                              engine=SimpleNamespace(description="fake"))
        transcript = await run_scenes(bot, 1, 7, "Brandon", list(SCENES))
        assert bot.llm.calls == sum(len(lines) for lines in SCENES.values())
        assert "===== catnip =====" in transcript and "Brandon: Here have some catnip *gives catnip*" in transcript
        assert "(scene: scene 1)" in transcript and "Whiskers: *pounces on Angel's feet*" in transcript
        assert await db.recent_messages(1000, 10) == []  # the real database is untouched
        assert not (cfg.data_dir / "simulation.db").exists()
        await db.close()

    asyncio.run(go())
