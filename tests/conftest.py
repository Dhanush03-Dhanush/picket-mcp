import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point PICKET_HOME at a throwaway dir so tests never touch ~/.claude/picket."""
    monkeypatch.setenv("PICKET_HOME", str(tmp_path))
    return tmp_path
