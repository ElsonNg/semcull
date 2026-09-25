"""One optional TOML file. Secrets never become configuration fields."""

from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from .models import SemcullError


@dataclass(frozen=True)
class Config:
    directory: str = ""
    max_observations: int = 200
    show_max_bytes: int = 16384
    check_max_bytes: int = 32768
    window: int = 4000
    full_budget: int = 16000
    request_budget: int = 24000
    state_question_budget: int = 24000
    max_attempts: int = 8
    parallel: int = 4
    timeout: float = 10.0
    max_seconds: float = 60.0
    max_tokens: int = 96000
    model: str = "jev-1.13.0"
    min_confidence: float = 0.5

    def validate(self) -> Config:
        self.session_id
        for key in (
            "max_observations",
            "show_max_bytes",
            "check_max_bytes",
            "window",
            "full_budget",
            "request_budget",
            "state_question_budget",
            "max_attempts",
            "parallel",
            "max_tokens",
        ):
            value = getattr(self, key)
            if type(value) is not int or value <= 0:
                raise SemcullError("invalid_config", f"{key} must be a positive integer.")
        for key in ("timeout", "max_seconds", "min_confidence"):
            value = getattr(self, key)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise SemcullError("invalid_config", f"{key} must be finite and numeric.")
        if self.timeout <= 0 or self.max_seconds <= 0 or not 0 <= self.min_confidence <= 1:
            raise SemcullError("invalid_config", "Invalid timeout or confidence setting.")
        if self.window < 4 or self.check_max_bytes < 4096:
            raise SemcullError(
                "invalid_config", "window must be at least 4 and check_max_bytes at least 4096."
            )
        if not isinstance(self.model, str) or not re.fullmatch(r"jev-\d+\.\d+\.\d+", self.model):
            raise SemcullError(
                "invalid_config", "Use an explicit versioned Jev model, not an alias."
            )
        if not isinstance(self.directory, str) or (
            self.directory and not Path(self.directory).is_absolute()
        ):
            raise SemcullError("invalid_config", "Storage directory must be absolute.")
        if self.request_budget > 60000 or self.state_question_budget > 30000:
            raise SemcullError(
                "invalid_config", "Request budgets exceed conservative supported context limits."
            )
        return self

    @property
    def session_id(self) -> str | None:
        if "SEMCULL_SESSION_ID" not in os.environ:
            return None
        value = os.environ["SEMCULL_SESSION_ID"]
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            encoded = b""
        if not value.strip() or not encoded or len(encoded) > 1024:
            raise SemcullError(
                "invalid_config", "SEMCULL_SESSION_ID must contain 1 to 1024 UTF-8 bytes."
            )
        return value

    @property
    def root(self) -> Path:
        base = (
            Path(self.directory)
            if self.directory
            else Path(tempfile.gettempdir()) / f"semcull-{os.getuid()}"
        )
        session_id = self.session_id
        if session_id is None:
            return base
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return base / "sessions" / digest

    def policy(self) -> dict:
        return {"version": 1, "min_confidence": self.min_confidence}


SECTIONS = {
    "storage": {"directory", "max_observations"},
    "output": {"show_max_bytes", "check_max_bytes"},
    "inspection": {"window", "full_budget", "request_budget", "state_question_budget"},
    "provider": {"model", "timeout", "min_confidence"},
    "limits": {"max_attempts", "parallel", "max_seconds", "max_tokens"},
}


def load_config(path: str | None = None, overrides: dict | None = None) -> Config:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base and not Path(base).is_absolute():
        raise SemcullError("invalid_config", "XDG_CONFIG_HOME must be absolute.")
    selected = (
        Path(path)
        if path is not None
        else (Path(base) if base else Path.home() / ".config") / "semcull/config.toml"
    )
    try:
        with selected.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError:
        if path is not None:
            raise SemcullError(
                "config_not_found", "Explicit configuration file was not found."
            ) from None
        document = {}
    except tomllib.TOMLDecodeError:
        raise SemcullError("invalid_config", "Configuration is not valid TOML.") from None
    except OSError:
        raise SemcullError("storage_error", "Could not read configuration.", 4) from None
    settings = {}
    for section, values in document.items():
        if (
            section not in SECTIONS
            or not isinstance(values, dict)
            or set(values) - SECTIONS[section]
        ):
            raise SemcullError("invalid_config", "Unknown configuration section or setting.")
        settings.update(values)
    settings.update({k: v for k, v in (overrides or {}).items() if v is not None})
    if set(settings) - {f.name for f in fields(Config)}:
        raise SemcullError("invalid_config", "Unknown configuration override.")
    return Config(**settings).validate()
