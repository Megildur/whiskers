"""Runs the AI model next to the bot.

"managed" mode needs no root and no installs, so it works inside hosting panels like Pterodactyl
(WispByte): it downloads llama.cpp's llama-server and a GGUF model, then runs the server as a child
process on 127.0.0.1. If the prebuilt llama.cpp can't run on the host (for example an Alpine image),
it builds llama.cpp from source once instead.

"external" mode talks to an OpenAI-compatible server you run yourself (Ollama, LM Studio, a VPS...).
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import signal
import socket
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import aiohttp

from . import system
from .config import Config

log = logging.getLogger("whiskers.engine")

HF_BASE = "https://huggingface.co"
GITHUB_BASE = "https://github.com/ggml-org/llama.cpp"
GB = 1024 ** 3


class EngineError(Exception):
    pass


class Engine:
    """Something that serves an OpenAI-compatible chat API."""

    base_url: str = ""
    model_name: str = ""
    ready: bool = False
    status: str = "starting"
    description: str = ""
    threads: int | None = None

    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        pass


def _human_size(size: float) -> str:
    return f"{size / GB:.1f} GB" if size >= GB / 10 else f"{size / 1024 ** 2:.0f} MB"


def _die_with_parent() -> None:
    """Runs in the child before exec: if the bot dies, take llama-server down with it."""
    try:
        import ctypes

        ctypes.CDLL(None).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _safe_extract(archive: Path, dest: Path) -> None:
    with tarfile.open(archive) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
            return
        root = dest.resolve()
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if root not in target.parents and target != root:
                raise EngineError(f"Refusing to extract {member.name} outside {dest}")
            if member.issym() or member.islnk():
                link = (target.parent / member.linkname).resolve()
                if root not in link.parents:
                    raise EngineError(f"Refusing to extract link {member.name} -> {member.linkname}")
        tar.extractall(dest)


class ExternalEngine(Engine):
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.base_url = cfg.llm_base_url
        self.model_name = cfg.llm_model_name
        self.description = f"external server at {cfg.llm_base_url}"
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._wait_until_reachable())

    async def _wait_until_reachable(self) -> None:
        headers = {"Authorization": f"Bearer {self.cfg.llm_api_key}"} if self.cfg.llm_api_key else {}
        self.status = f"connecting to {self.base_url}"
        warned = False
        async with aiohttp.ClientSession(trust_env=True) as session:
            while True:
                try:
                    async with session.get(f"{self.base_url}/models", headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            if not self.model_name:
                                data = await resp.json()
                                models = [m.get("id") for m in data.get("data", []) if m.get("id")]
                                if not models:
                                    raise EngineError("the server lists no models; set LLM_MODEL_NAME in .env")
                                self.model_name = models[0]
                            break
                        raise EngineError(f"HTTP {resp.status}")
                except (aiohttp.ClientError, asyncio.TimeoutError, EngineError) as error:
                    if not warned:
                        log.warning("Can't reach the AI server at %s yet (%s). Retrying every 15s.", self.base_url, error)
                        warned = True
                    await asyncio.sleep(15)
        self.description = f"{self.model_name} on {self.base_url}"
        self.status = "ready"
        self.ready = True
        log.info("Connected to %s", self.description)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()


class ManagedEngine(Engine):
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # Generation is limited by memory speed, so more than ~8 threads mostly adds contention on shared hosts.
        self.threads = cfg.llm_threads or min(8, system.cpu_limit())
        self.model_name = "whiskers"
        custom = cfg.model_path or urlparse(cfg.model_url).path
        self.description = Path(custom).name if custom else cfg.model
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._help_text = ""

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
        proc = self._proc
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    # ---- main loop ----

    async def _run(self) -> None:
        try:
            binary = await self._ensure_binary()
            model = await self._ensure_model()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.status = f"error: {error}"
            log.error("The AI engine couldn't start: %s", error, exc_info=not isinstance(error, EngineError))
            return

        self._check_memory(model)
        failures = 0
        while not self._stopping:
            started = time.monotonic()
            try:
                await self._launch(binary, model)
                await self._proc.wait()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error("llama-server failed: %s", error, exc_info=not isinstance(error, EngineError))
            self.ready = False
            if self._stopping:
                return
            failures = failures + 1 if time.monotonic() - started < 300 else 1
            tail = self._log_tail()
            log.error("llama-server stopped (exit code %s). Last log lines:\n%s",
                      self._proc.returncode if self._proc else "?", tail)
            if failures >= 5:
                self.status = "error: llama-server keeps crashing (see data/llama-server.log)"
                log.error("Giving up on llama-server after %d quick crashes.", failures)
                return
            delay = min(120, 5 * 2 ** failures)
            self.status = f"restarting in {delay}s"
            await asyncio.sleep(delay)

    def _log_tail(self, lines: int = 25) -> str:
        try:
            return "\n".join((self.cfg.data_dir / "llama-server.log").read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return "(no log)"

    def _check_memory(self, model: Path) -> None:
        limit = system.memory_limit_bytes()
        if not limit:
            return
        # Weights + KV cache (roughly 0.14 MB per token of context for 3-4B models) + prompt cache + the bot.
        need = model.stat().st_size + self.cfg.llm_context * 0.14 * 1024 ** 2 + self.cfg.llm_cache_ram_mb * 1024 ** 2 + 400 * 1024 ** 2
        log.info("Memory: about %s needed, %s available.", _human_size(need), _human_size(limit))
        if need > limit * 0.95:
            log.warning(
                "This server probably doesn't have enough RAM for this model. If the AI keeps crashing, "
                "pick a smaller MODEL (llama3.2-3b or qwen2.5-1.5b) or lower LLM_CONTEXT in .env."
            )

    async def _launch(self, binary: Path, model: Path) -> None:
        port = self.cfg.llm_port or _free_port()
        self.base_url = f"http://127.0.0.1:{port}/v1"
        args = [
            str(binary), "-m", str(model), "--host", "127.0.0.1", "--port", str(port),
            "-c", str(self.cfg.llm_context), "-np", str(max(1, self.cfg.llm_slots)),
            "-t", str(self.threads), "-tb", str(self.threads), "--alias", self.model_name,
        ]
        optional = [
            ("--no-webui", []),
            ("--jinja", []),
            ("--kv-unified", []),  # slots share the context, so one long prompt can use all of it
            ("--cache-reuse", ["256"]),  # reuse cached chat history even after the window moves
            ("--cache-ram", [str(self.cfg.llm_cache_ram_mb)]),  # default is 8 GB, far too much here
        ]
        if self.cfg.preset and self.cfg.preset.thinking:
            optional += [("--chat-template-kwargs", ['{"enable_thinking":false}']), ("--reasoning-budget", ["0"])]
        for flag, values in optional:
            if flag in self._help_text:
                args += [flag, *values]

        self.status = "loading the model into memory"
        log.info("Starting llama-server with %d threads: %s", self.threads, " ".join(args[1:]))
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        with open(self.cfg.data_dir / "llama-server.log", "wb") as log_file:
            self._proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.DEVNULL, stdout=log_file, stderr=asyncio.subprocess.STDOUT,
                preexec_fn=_die_with_parent,
            )

        started = time.monotonic()
        async with aiohttp.ClientSession() as session:
            while True:
                if self._proc.returncode is not None:
                    raise EngineError("llama-server exited while loading the model")
                try:
                    async with session.get(f"http://127.0.0.1:{port}/health", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            break
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
                if time.monotonic() - started > 900:
                    self._proc.kill()
                    raise EngineError("llama-server took more than 15 minutes to load the model")
                await asyncio.sleep(1)
        self.status = "ready"
        self.ready = True
        log.info("The AI engine is ready (model loaded in %.0fs).", time.monotonic() - started)

    # ---- llama.cpp binary ----

    async def _works(self, binary: Path) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                str(binary), "--help", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except (OSError, asyncio.TimeoutError) as error:
            log.warning("%s doesn't run here: %s", binary, error)
            return False
        text = output.decode(errors="replace")
        if proc.returncode != 0 or "--port" not in text:
            log.warning("%s doesn't run here (exit code %s): %s", binary, proc.returncode, text.strip()[-500:])
            return False
        self._help_text = text
        return True

    async def _ensure_binary(self) -> Path:
        if self.cfg.llama_server_path:
            binary = Path(self.cfg.llama_server_path).expanduser()
            if not await self._works(binary):
                raise EngineError(f"LLAMA_SERVER_PATH={binary} doesn't run")
            return binary

        tag = self.cfg.llama_cpp_version
        base = self.cfg.engine_dir / tag
        prebuilt = base / "prebuilt" / "llama-server"
        built = base / "source" / "llama-server"
        for candidate in (prebuilt, built):
            if candidate.exists() and await self._works(candidate):
                return candidate

        arch = system.release_arch()
        if arch and not system.is_musl():
            try:
                await self._download_prebuilt(tag, arch, prebuilt.parent)
                if await self._works(prebuilt):
                    return prebuilt
                log.warning("The prebuilt llama.cpp doesn't run on this server, so it will be built from source.")
            except (EngineError, aiohttp.ClientError, asyncio.TimeoutError, OSError, tarfile.TarError) as error:
                log.warning("Couldn't use the prebuilt llama.cpp (%s), so it will be built from source.", error)
        await self._build_from_source(tag, built)
        if not await self._works(built):
            raise EngineError("llama.cpp was built but doesn't run; see engine/build.log")
        return built

    async def _download_prebuilt(self, tag: str, arch: str, dest: Path) -> None:
        url = f"{GITHUB_BASE}/releases/download/{tag}/llama-{tag}-bin-ubuntu-{arch}.tar.gz"
        archive = self.cfg.engine_dir / f"llama-{tag}-{arch}.tar.gz"
        self.status = "downloading the AI engine"
        log.info("Downloading llama.cpp %s...", tag)
        async with aiohttp.ClientSession(trust_env=True) as session:
            await self._download(session, url, archive, "AI engine")
        unpack = dest.parent / "unpack"
        shutil.rmtree(unpack, ignore_errors=True)
        await asyncio.to_thread(_safe_extract, archive, unpack)
        folders = [p for p in unpack.iterdir() if p.is_dir()]
        source = folders[0] if len(folders) == 1 and not (unpack / "llama-server").exists() else unpack
        shutil.rmtree(dest, ignore_errors=True)
        shutil.move(str(source), str(dest))
        shutil.rmtree(unpack, ignore_errors=True)
        archive.unlink(missing_ok=True)
        if not (dest / "llama-server").exists():
            raise EngineError("the download didn't contain llama-server")

    async def _pip_tool(self, name: str) -> str:
        """Install cmake or ninja from PyPI into engine/tools (no root needed) and return the program."""
        prefix = self.cfg.engine_dir / "tools"

        def find() -> Path | None:
            # The wheels also install small Python wrapper scripts with the same name; we want the real program.
            for path in prefix.rglob(name):
                try:
                    with open(path, "rb") as file:
                        if path.is_file() and file.read(4) == b"\x7fELF":
                            return path
                except OSError:
                    continue
            return None

        program = find()
        if program is None:
            log.info("Installing %s from PyPI to build llama.cpp...", name)
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet",
                "--prefix", str(prefix), name,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await proc.communicate()
            if proc.returncode != 0:
                raise EngineError(f"couldn't install {name}: {output.decode(errors='replace')[-400:]}")
            program = find()
        if program is None:
            raise EngineError(f"installed {name} but couldn't find its program")
        program.chmod(program.stat().st_mode | 0o111)
        return str(program)

    async def _build_from_source(self, tag: str, output: Path) -> None:
        compiler = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
        if not compiler:
            raise EngineError(
                "the prebuilt llama.cpp can't run here and there's no C++ compiler to build it. Switch the "
                "server's Docker image to a Debian-based Python image, or use LLM_BACKEND=external."
            )
        self.status = "building the AI engine (one time only, 10-30 minutes)"
        log.info("Building llama.cpp %s from source. This happens once and takes 10-30 minutes.", tag)
        work = self.cfg.engine_dir / "build-tmp"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        archive = work / "source.tar.gz"
        async with aiohttp.ClientSession(trust_env=True) as session:
            await self._download(session, f"{GITHUB_BASE}/archive/refs/tags/{tag}.tar.gz", archive, "AI engine source")
        await asyncio.to_thread(_safe_extract, archive, work)
        sources = [p for p in work.iterdir() if p.is_dir()]
        if not sources:
            raise EngineError("the source download was empty")
        src, build = sources[0], work / "build"

        cmake = shutil.which("cmake") or await self._pip_tool("cmake")
        ninja = shutil.which("ninja") or (None if shutil.which("make") else await self._pip_tool("ninja"))
        configure = [
            cmake, "-S", str(src), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release",
            "-DBUILD_SHARED_LIBS=OFF", "-DGGML_NATIVE=ON", "-DGGML_CCACHE=OFF",
            "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF", "-DLLAMA_BUILD_SERVER=ON",
            "-DLLAMA_BUILD_UI=OFF", "-DLLAMA_USE_PREBUILT_UI=OFF", "-DLLAMA_OPENSSL=OFF",
        ]
        if ninja:
            configure += ["-G", "Ninja", f"-DCMAKE_MAKE_PROGRAM={ninja}"]
        compile_ = [cmake, "--build", str(build), "--target", "llama-server", "-j", str(self.threads)]
        build_log = self.cfg.engine_dir / "build.log"
        with open(build_log, "wb") as out:
            for step in (configure, compile_):
                proc = await asyncio.create_subprocess_exec(*step, stdout=out, stderr=asyncio.subprocess.STDOUT)
                if await proc.wait() != 0:
                    tail = "\n".join(build_log.read_text(errors="replace").splitlines()[-20:])
                    raise EngineError(f"building llama.cpp failed (see engine/build.log):\n{tail}")
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(build / "bin" / "llama-server", output)
        shutil.rmtree(work, ignore_errors=True)
        log.info("llama.cpp built.")

    # ---- model ----

    async def _ensure_model(self) -> Path:
        cfg = self.cfg
        if cfg.model_path:
            path = Path(cfg.model_path).expanduser()
            if not path.is_absolute():
                path = cfg.models_dir.parent / path
            if not path.exists():
                raise EngineError(f"MODEL_PATH={path} doesn't exist")
            return path

        cfg.models_dir.mkdir(parents=True, exist_ok=True)
        headers = {"Authorization": f"Bearer {cfg.hf_token}"} if cfg.hf_token else {}
        if cfg.model_url:
            path = cfg.models_dir / (Path(urlparse(cfg.model_url).path).name or "model.gguf")
            if not path.exists():
                async with aiohttp.ClientSession(trust_env=True) as session:
                    await self._download(session, cfg.model_url, path, "model", headers=headers)
            return path

        preset = cfg.preset
        assert preset is not None
        path = cfg.models_dir / f"{cfg.model}-{preset.quant}.gguf"
        if path.exists():
            return path
        log.info("Downloading the %s model (about %.1f GB). This happens once.", cfg.model, preset.size_gb)
        async with aiohttp.ClientSession(trust_env=True) as session:
            sources = []
            for repo in preset.repos:
                filename = await self._find_hf_file(session, repo, preset.quant, headers)
                if filename:
                    sources.insert(0, (repo, filename))  # a file we've seen listed beats a guessed name
                    break
                sources.append((repo, preset.fallback_file))
            for i, (repo, filename) in enumerate(sources):
                url = f"{HF_BASE}/{repo}/resolve/main/{quote(filename)}?download=true"
                try:
                    await self._download(session, url, path, "model", headers=headers,
                                         expected=int(preset.size_gb * GB))
                    break
                except EngineError as error:
                    if i == len(sources) - 1 or "wasn't found" not in str(error):
                        raise
                    log.warning("%s; trying another download source.", error)
        return path

    async def _find_hf_file(self, session: aiohttp.ClientSession, repo: str, quant: str, headers: dict) -> str | None:
        try:
            async with session.get(f"{HF_BASE}/api/models/{repo}/tree/main", headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    log.warning("Hugging Face file list for %s returned HTTP %s", repo, resp.status)
                    return None
                items = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            log.warning("Couldn't list the files of %s: %s", repo, error)
            return None
        matches = [
            item["path"] for item in items
            if item.get("type") == "file" and item["path"].lower().endswith(".gguf")
            and quant.lower() in item["path"].lower() and "mmproj" not in item["path"].lower()
        ]
        return min(matches, key=len) if matches else None

    async def _download(self, session: aiohttp.ClientSession, url: str, dest: Path, label: str,
                        headers: dict | None = None, expected: int | None = None) -> None:
        """Download with resume support, so a restart halfway through doesn't start over."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        if expected:
            free = shutil.disk_usage(dest.parent).free
            have = part.stat().st_size if part.exists() else 0
            if free < expected - have + 200 * 1024 ** 2:
                raise EngineError(f"not enough disk space for the {label}: need about {_human_size(expected - have)}, "
                                  f"{_human_size(free)} free")
        for attempt in range(1, 6):
            have = part.stat().st_size if part.exists() else 0
            request_headers = dict(headers or {})
            if have:
                request_headers["Range"] = f"bytes={have}-"
            try:
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
                async with session.get(url, headers=request_headers, timeout=timeout) as resp:
                    if resp.status == 416 and have:
                        break  # already complete
                    if resp.status in (401, 403):
                        raise EngineError(f"downloading the {label} was refused (HTTP {resp.status}). "
                                          "If it's a gated model, put a Hugging Face token in HF_TOKEN.")
                    if resp.status == 404:
                        raise EngineError(f"the {label} wasn't found at {url} (HTTP 404)")
                    if resp.status not in (200, 206):
                        raise aiohttp.ClientResponseError(resp.request_info, resp.history, status=resp.status)
                    if resp.status == 200:
                        have = 0
                    total = have + resp.content_length if resp.content_length else None
                    done, last_report = have, 0.0
                    with open(part, "ab" if have else "wb") as out:
                        async for chunk in resp.content.iter_chunked(1 << 20):
                            out.write(chunk)
                            done += len(chunk)
                            if time.monotonic() - last_report > 10:
                                last_report = time.monotonic()
                                if total:
                                    self.status = f"downloading the {label}: {done * 100 // total}%"
                                    log.info("Downloading the %s: %d%% (%s of %s)", label, done * 100 // total,
                                             _human_size(done), _human_size(total))
                                else:
                                    self.status = f"downloading the {label}: {_human_size(done)}"
                    if total and done < total:
                        raise aiohttp.ClientPayloadError(f"connection closed at {done} of {total} bytes")
                break
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                if attempt == 5:
                    raise EngineError(f"downloading the {label} failed: {error}") from error
                log.warning("Download of the %s interrupted (%s); retrying in %ds.", label, error, 5 * attempt)
                await asyncio.sleep(5 * attempt)
        part.replace(dest)
        log.info("Downloaded the %s (%s).", label, _human_size(dest.stat().st_size))


def create_engine(cfg: Config) -> Engine:
    return ManagedEngine(cfg) if cfg.llm_backend == "managed" else ExternalEngine(cfg)
