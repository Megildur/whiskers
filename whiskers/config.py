"""Settings read from the .env file. Per-server settings (channels, chattiness) live in the database."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class ModelPreset:
    repos: tuple[str, ...]  # Hugging Face repos to try in order (mirrors in case one disappears)
    quant: str
    fallback_file: str
    size_gb: float
    note: str
    thinking: bool = False  # the model has a "thinking" mode that must be switched off for chat
    # Mistral's chat template moves the system prompt next to the newest message, which would make the
    # engine re-read the whole personality for every reply. Sending it at the start of the chat avoids that.
    system_in_first_turn: bool = False
    temperature: float = 0.85


# Every preset is a GGUF on Hugging Face. The exact file is looked up by quant name at download time;
# fallback_file is only used if that lookup fails.
MODEL_PRESETS: dict[str, ModelPreset] = {
    "nemo-12b": ModelPreset(
        ("bartowski/Mistral-Nemo-Instruct-2407-GGUF", "QuantFactory/Mistral-Nemo-Instruct-2407-GGUF"),
        "Q4_K_M", "Mistral-Nemo-Instruct-2407-Q4_K_M.gguf", 7.5,
        "Best at roleplay and following the conversation. Needs about 10 GB of RAM; replies take ~3x longer "
        "than qwen3-4b.",
        system_in_first_turn=True, temperature=0.9,
    ),
    "llama3.1-8b": ModelPreset(
        ("bartowski/Meta-Llama-3.1-8B-Instruct-GGUF", "lmstudio-community/Meta-Llama-3.1-8B-Instruct-GGUF"),
        "Q4_K_M", "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf", 4.9,
        "Good roleplay and context, faster than nemo-12b. Needs about 7 GB of RAM; replies take ~2x longer "
        "than qwen3-4b.",
        temperature=0.8,
    ),
    "qwen3-4b": ModelPreset(
        ("unsloth/Qwen3-4B-Instruct-2507-GGUF",), "Q4_K_M", "Qwen3-4B-Instruct-2507-Q4_K_M.gguf", 2.5,
        "Small and quick, for servers with 4-8 GB of RAM. Weaker at roleplay.",
    ),
    "llama3.2-3b": ModelPreset(
        ("bartowski/Llama-3.2-3B-Instruct-GGUF",), "Q4_K_M", "Llama-3.2-3B-Instruct-Q4_K_M.gguf", 2.0,
        "About 25% faster than qwen3-4b, a little less sharp. Needs about 3.5 GB of RAM.",
    ),
    "qwen3.5-4b": ModelPreset(
        ("unsloth/Qwen3.5-4B-GGUF",), "Q4_K_M", "Qwen3.5-4B-Q4_K_M.gguf", 2.7,
        "Newest small Qwen. Its design caches old chat less well on CPU, so replies can start slower.",
        thinking=True,
    ),
    "gemma3-4b": ModelPreset(
        ("bartowski/google_gemma-3-4b-it-GGUF",), "Q4_K_M", "google_gemma-3-4b-it-Q4_K_M.gguf", 2.5,
        "Nice writing style, slower on CPU than the others.",
    ),
    "qwen2.5-1.5b": ModelPreset(
        ("Qwen/Qwen2.5-1.5B-Instruct-GGUF",), "Q4_K_M", "qwen2.5-1.5b-instruct-q4_k_m.gguf", 1.1,
        "Tiny and fast, for servers with 2-3 GB of RAM. Noticeably dumber.",
    ),
}
DEFAULT_MODEL = "qwen3-4b"


def code_version() -> str:
    """The git commit this copy is running, like "aedf6d1 (Oct 16 22:40)", or "unknown"."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "log", "-1", "--format=%h (%cd)", "--date=format:%b %d %H:%M"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    value = _str(name)
    try:
        return int(value) if value else default
    except ValueError:
        raise SystemExit(f"{name} in .env must be a whole number, got {value!r}")


def _float(name: str, default: float) -> float:
    value = _str(name)
    try:
        return float(value) if value else default
    except ValueError:
        raise SystemExit(f"{name} in .env must be a number, got {value!r}")


def _bool(name: str, default: bool) -> bool:
    value = _str(name).lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _ids(name: str) -> frozenset[int]:
    return frozenset(int(part) for part in _str(name).replace(" ", "").split(",") if part.isdigit())


@dataclass(frozen=True)
class Config:
    discord_token: str
    bot_name: str
    cat_gender: str  # "boy" or "girl": which pronouns Whiskers gets
    owner_ids: frozenset[int]
    timezone: str
    allow_dms: bool

    llm_backend: str  # "managed" runs llama.cpp for you, "external" talks to a server you run (Ollama etc.)
    model: str
    model_url: str
    model_path: str
    hf_token: str
    llama_cpp_version: str
    llama_server_path: str
    llm_threads: int  # 0 = detect from the server's CPU limit
    llm_context: int
    llm_slots: int
    llm_cache_ram_mb: int
    llm_port: int
    llm_base_url: str
    llm_model_name: str
    llm_api_key: str

    max_reply_tokens: int
    temperature: float
    history_messages: int
    reflect_every: int
    reflect_min_hours: float
    speech_after_days: float
    max_queue: int

    data_dir: Path
    engine_dir: Path
    models_dir: Path

    @property
    def preset(self) -> ModelPreset | None:
        return MODEL_PRESETS.get(self.model)


def load_config() -> Config:
    backend = _str("LLM_BACKEND", "managed").lower()
    if backend not in ("managed", "external"):
        raise SystemExit("LLM_BACKEND in .env must be 'managed' or 'external'")
    model = _str("MODEL", DEFAULT_MODEL).lower()
    if backend == "managed" and model not in MODEL_PRESETS and not (_str("MODEL_URL") or _str("MODEL_PATH")):
        names = ", ".join(MODEL_PRESETS)
        raise SystemExit(f"MODEL={model!r} isn't a preset ({names}). Pick one, or set MODEL_URL or MODEL_PATH.")
    data_dir = ROOT / _str("DATA_DIR", "data")
    gender = _str("CAT_GENDER", "boy").lower()
    if gender not in ("boy", "girl"):
        raise SystemExit("CAT_GENDER in .env must be 'boy' or 'girl'")
    return Config(
        discord_token=_str("DISCORD_TOKEN"),
        bot_name=_str("BOT_NAME", "Whiskers") or "Whiskers",
        cat_gender=gender,
        owner_ids=_ids("OWNER_IDS"),
        timezone=_str("TIMEZONE", "UTC"),
        allow_dms=_bool("ALLOW_DMS", False),
        llm_backend=backend,
        model=model,
        model_url=_str("MODEL_URL"),
        model_path=_str("MODEL_PATH"),
        hf_token=_str("HF_TOKEN"),
        llama_cpp_version=_str("LLAMA_CPP_VERSION", "b11696"),
        llama_server_path=_str("LLAMA_SERVER_PATH"),
        llm_threads=_int("LLM_THREADS", 0),
        llm_context=_int("LLM_CONTEXT", 8192),
        llm_slots=_int("LLM_SLOTS", 2),
        llm_cache_ram_mb=_int("LLM_CACHE_RAM_MB", 512),
        llm_port=_int("LLM_PORT", 0),
        llm_base_url=_str("LLM_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/"),
        llm_model_name=_str("LLM_MODEL_NAME"),
        llm_api_key=_str("LLM_API_KEY"),
        max_reply_tokens=_int("MAX_REPLY_TOKENS", 300),
        temperature=_float("TEMPERATURE", MODEL_PRESETS[model].temperature if model in MODEL_PRESETS else 0.85),
        history_messages=max(6, _int("HISTORY_MESSAGES", 24)),
        reflect_every=max(5, _int("REFLECT_EVERY", 25)),
        reflect_min_hours=_float("REFLECT_MIN_HOURS", 2.0),
        speech_after_days=_float("SPEECH_AFTER_DAYS", 7.0),
        max_queue=max(1, _int("MAX_QUEUE", 6)),
        data_dir=data_dir,
        engine_dir=ROOT / "engine",
        models_dir=ROOT / "models",
    )
