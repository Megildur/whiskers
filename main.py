"""Start Whiskers: python main.py"""
import logging
import sys
from logging.handlers import RotatingFileHandler

if sys.version_info < (3, 10):
    sys.exit("Whiskers needs Python 3.10 or newer.")

from whiskers.bot import WhiskersBot  # noqa: E402
from whiskers.config import load_config  # noqa: E402

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")


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
    cfg = load_config()
    if not cfg.discord_token:
        sys.exit("DISCORD_TOKEN is missing. Copy .env.example to .env and put your bot token in it.")
    setup_logging(cfg)
    WhiskersBot(cfg).run(cfg.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
