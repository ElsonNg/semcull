import io
import socket

import pytest

from semcull.models import Intent
from semcull.store import Store


def response(prepared, choice="complete", evidence="first", confidence=0.9):
    answers = {}
    for key, question in prepared.payload["questions"].items():
        selected = (
            choice
            if key == "outcome"
            else (next(iter(prepared.spans)) if evidence == "first" else evidence)
        )
        answers[key] = {
            "type": "choice",
            "choice": selected,
            "confidence": confidence,
            "probabilities": {option: float(option == selected) for option in question["criteria"]},
        }
    return {
        "model": prepared.payload["model"],
        "answers": answers,
        "usage": {"input_tokens": 100, "output_tokens": 5},
    }


class FakeProvider:
    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def evaluate(self, prepared):
        self.calls.append(prepared)
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, Exception):
            raise reply
        return reply(prepared) if reply else response(prepared)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("SEMCULL_SESSION_ID", raising=False)
    monkeypatch.setattr("semcull.credentials.installation_directory", lambda: tmp_path)

    def blocked(*args, **kwargs):
        raise AssertionError("Live network is forbidden in offline tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "store") as value:
        yield value


@pytest.fixture
def intent():
    return Intent.parse(
        {
            "question": "What outcome is reported?",
            "expectations": {
                "complete": "The operation completed.",
                "failed": "The operation failed.",
            },
        }
    )


@pytest.fixture
def fake():
    return FakeProvider()


@pytest.fixture
def capture(store):
    return lambda data=b"Operation completed.\n": store.capture(io.BytesIO(data))
