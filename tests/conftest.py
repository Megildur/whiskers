import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whiskers.config import load_config  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    return dataclasses.replace(load_config(), data_dir=tmp_path, timezone="UTC", history_messages=8)
