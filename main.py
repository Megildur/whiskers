"""Start Whiskers: python main.py"""
import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit("Whiskers needs Python 3.10 or newer.")

ROOT = Path(__file__).resolve().parent

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")


def sync_with_git() -> None:
    """With SYNC_WITH_GIT=true, make the code match GitHub exactly before starting.

    Hosting panels run `git pull` on start, which gives up if a file was edited or uploaded by hand, and
    leaves hand-deleted files missing. A hard reset to the branch on GitHub fixes both. Files git doesn't
    track (.env, data/, models/, engine/) are never touched. This runs before the bot's own code is
    imported, so it can repair even a broken copy of it.
    """
    try:
        from dotenv import dotenv_values

        flag = os.getenv("SYNC_WITH_GIT") or dotenv_values(ROOT / ".env").get("SYNC_WITH_GIT") or ""
    except ImportError:
        flag = os.getenv("SYNC_WITH_GIT", "")
    if flag.strip().lower() not in ("1", "true", "yes", "on") or not (ROOT / ".git").exists():
        return

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=180)

    try:
        fetched = git("fetch", "--quiet", "origin")
        if fetched.returncode != 0:
            print(f"[update] Couldn't reach GitHub, starting with the code already here: {fetched.stderr.strip()}")
            return
        upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").stdout.strip()
        if not upstream:
            print("[update] This copy isn't following a GitHub branch, so it can't sync.")
            return
        before = git("rev-parse", "--short", "HEAD").stdout.strip()
        reset = git("reset", "--hard", upstream)
        if reset.returncode != 0:
            print(f"[update] Syncing with GitHub failed: {reset.stderr.strip()}")
            return
        after = git("rev-parse", "--short", "HEAD").stdout.strip()
        print(f"[update] Code matches {upstream} ({before} -> {after})." if before != after
              else f"[update] Code matches {upstream} ({after}).")
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"[update] Couldn't run git ({error}), starting with the code already here.")


def setup_logging(cfg) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(cfg.data_dir / "whiskers.log", maxBytes=5_000_000, backupCount=3,
                                       encoding="utf-8")
    console = logging.StreamHandler(sys.stdout)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    for handler in (file_handler, console):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)


def main() -> None:
    sync_with_git()
    from whiskers.bot import WhiskersBot
    from whiskers.config import load_config

    cfg = load_config()
    if not cfg.discord_token:
        sys.exit("DISCORD_TOKEN is missing. Copy .env.example to .env and put your bot token in it.")
    setup_logging(cfg)
    WhiskersBot(cfg).run(cfg.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
