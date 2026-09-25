# Contributing

Use Python 3.11+ and run `uv sync --group dev`, then `uv run pytest`.
Tests must use fake providers and isolated temporary stores. Do not add real
credentials, captured private logs, or paid API calls to the default suite.

Keep functions focused and comments limited to non-obvious decisions. Add
behavioural tests for changed CLI contracts, source fidelity, coverage, retries,
and persistence. Update the spec and agent skill when public behaviour changes.

Live evaluation and publication are separate, explicit release activities.
