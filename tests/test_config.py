import hashlib

import pytest

from semcull.config import load_config
from semcull.models import SemcullError


def test_defaults_and_missing_explicit(tmp_path):
    assert load_config().max_observations == 200
    with pytest.raises(SemcullError, match="not found"):
        load_config(str(tmp_path / "missing.toml"))


def test_precedence(tmp_path):
    file = tmp_path / "config.toml"
    file.write_text("[storage]\nmax_observations=7\n[inspection]\nwindow=80\n")
    config = load_config(str(file), {"window": 90})
    assert config.max_observations == 7 and config.window == 90


@pytest.mark.parametrize(
    "text",
    [
        "garbage",
        "[storage]\nmax_input_bytes=10",
        "[storage]\nmax_observations=true",
        "[storage]\nmax_observations=0",
        '[provider]\napi_key="secret"',
        '[provider]\nmodel="jev-latest"',
        "[provider]\ntimeout=nan",
        '[storage]\ndirectory="relative"',
    ],
)
def test_reject_invalid_settings(tmp_path, text):
    file = tmp_path / "config.toml"
    file.write_text(text)
    with pytest.raises(SemcullError):
        load_config(str(file))


def test_no_cwd_discovery(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "semcull.toml").write_text("[storage]\nmax_observations=1")
    assert load_config().max_observations == 200


def test_session_id_scopes_root_without_exposing_id(monkeypatch, tmp_path):
    base = tmp_path / "store"
    monkeypatch.setenv("SEMCULL_SESSION_ID", "agent/session:one")
    config = load_config(overrides={"directory": str(base)})
    expected = hashlib.sha256(b"agent/session:one").hexdigest()
    first_root = config.root
    assert first_root == base / "sessions" / expected
    assert "agent/session:one" not in str(first_root)

    monkeypatch.setenv("SEMCULL_SESSION_ID", "agent/session:two")
    assert load_config(overrides={"directory": str(base)}).root != first_root


@pytest.mark.parametrize("value", ["", "   ", "x" * 1025])
def test_invalid_session_ids_are_rejected(monkeypatch, value):
    monkeypatch.setenv("SEMCULL_SESSION_ID", value)
    with pytest.raises(SemcullError, match="SEMCULL_SESSION_ID"):
        load_config()
