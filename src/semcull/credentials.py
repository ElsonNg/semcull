"""Read the Jev key from the environment or Semcull's install directory."""

from __future__ import annotations

import os
import shlex
import tomllib
from pathlib import Path
from typing import Mapping

from .models import SemcullError


def installation_directory() -> Path:
    package_dir = Path(__file__).resolve().parent
    for candidate in (package_dir, *package_dir.parents):
        try:
            project = tomllib.loads((candidate / "pyproject.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if project.get("project", {}).get("name") == "semcull":
            return candidate
    return package_dir


def _dotenv_key(path: Path) -> str:
    try:
        lines = path.open(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError:
        raise SemcullError("credential_file_error", "Could not read Semcull's install .env file.") from None

    key = ""
    with lines:
        try:
            for line in lines:
                entry = line.strip()
                if not entry or entry.startswith("#"):
                    continue
                if entry.startswith("export "):
                    entry = entry[7:].lstrip()
                name, separator, value = entry.partition("=")
                if not separator or name.strip() != "TYPESAFE_API_KEY":
                    continue
                try:
                    parts = shlex.split(value, comments=True)
                except ValueError:
                    raise SemcullError(
                        "invalid_credentials_file",
                        "TYPESAFE_API_KEY in Semcull's install .env file has invalid quoting.",
                    ) from None
                if len(parts) > 1:
                    raise SemcullError(
                        "invalid_credentials_file",
                        "TYPESAFE_API_KEY in Semcull's install .env file must be one value.",
                    )
                key = parts[0].strip() if parts else ""
        except UnicodeError:
            raise SemcullError(
                "invalid_credentials_file", "Semcull's install .env file must be UTF-8 text."
            ) from None
    return key


def api_key(environ: Mapping[str, str] | None = None, env_file: Path | None = None) -> str:
    environ = os.environ if environ is None else environ
    key = environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    return _dotenv_key(env_file or installation_directory() / ".env")
