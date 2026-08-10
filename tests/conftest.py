import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "publicador-home"
    monkeypatch.setenv("PUBLICADOR_HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    return home


@pytest.fixture
def sample_spec_path() -> str:
    return str(Path(__file__).parent / "fixtures" / "sample_spec.json")


@pytest.fixture
def scheduled_spec_path() -> str:
    return str(Path(__file__).parent / "fixtures" / "scheduled_spec.json")


@pytest.fixture
def media_fixture_path() -> str:
    return str(Path(__file__).parent / "fixtures" / "sample.mp4")
