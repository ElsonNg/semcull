import io
import json
from pathlib import Path

import pytest
from conftest import FakeProvider, response

from semcull.cli import main
from semcull.config import load_config
from semcull.jev import ProviderError


@pytest.fixture
def invoke(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-test-key")
    config = tmp_path / "config.toml"
    config.write_text(f'[storage]\ndirectory="{tmp_path / "store"}"\n')
    provider = FakeProvider()

    def call(args, data=b"Operation completed.\n", raw=False):
        stdout, stderr = (io.BytesIO() if raw else io.StringIO()), io.StringIO()
        code = main(
            ["--config", str(config), *args],
            stdin=io.BytesIO(data),
            stdout=stdout,
            stderr=stderr,
            provider_factory=lambda *_: provider,
        )
        return code, stdout.getvalue(), stderr.getvalue()

    call.provider = provider
    return call


def test_end_to_end_offline(invoke, monkeypatch):
    code, out, err = invoke(
        ["check", "--question", "What happened?", "--expect", "complete=Completed"]
    )
    assert code == 0 and err == ""
    first = json.loads(out)
    obs, evaluation = first["observation_id"], first["evaluation_id"]
    monkeypatch.delenv("TYPESAFE_API_KEY")
    code, out, err = invoke(["show", obs, "--all"], raw=True)
    assert code == 0 and out == b"Operation completed.\n" and err == ""
    code, out, err = invoke(["result", obs])
    assert code == 0 and json.loads(out)["request"]["intent"]["question"] == "What happened?"
    code, out, err = invoke(["check", obs, "--eval", evaluation, "--only", "next"])
    assert code == 0 and json.loads(out)["reason"] == "no_new_text"
    assert len(invoke.provider.calls) == 1
    code, out, err = invoke(["delete", obs])
    assert code == 0 and json.loads(out)["status"] == "deleted"
    assert invoke(["show", obs], raw=True)[0] == 2


def test_cli_session_ids_reuse_and_isolate_stores(invoke, monkeypatch, tmp_path):
    monkeypatch.setenv("SEMCULL_SESSION_ID", "agent-session-one")
    code, out, err = invoke(
        ["check", "--question", "What happened?", "--expect", "complete=Completed"]
    )
    assert code == 0 and err == ""
    obs = json.loads(out)["observation_id"]
    first_root = load_config(str(tmp_path / "config.toml")).root
    assert first_root.exists()

    monkeypatch.setenv("SEMCULL_SESSION_ID", "agent-session-two")
    code, _, err = invoke(["show", obs], raw=True)
    assert code == 2 and json.loads(err)["code"] == "not_found"

    monkeypatch.setenv("SEMCULL_SESSION_ID", "agent-session-one")
    code, out, err = invoke(["show", obs, "--all"], raw=True)
    assert code == 0 and out == b"Operation completed.\n" and err == ""


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["check"],
        ["check", "--question", "q"],
        ["delete"],
        ["show", "../bad"],
        ["check", "--only", "remaining"],
        ["check", "--question", "q", "--expect", "other=x"],
        ["check", "--question", "q", "--expect", "x=a", "--expect", "x=b"],
    ],
)
def test_usage_errors_are_json_and_no_stdout(invoke, args):
    code, out, err = invoke(args)
    assert code == 2 and out == ""
    assert json.loads(err)["level"] == "error"
    assert not invoke.provider.calls


def test_no_credentials_before_capture(invoke, monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    code, out, err = invoke(["check", "--question", "q", "--expect", "complete=yes"])
    assert code == 2 and json.loads(err)["code"] == "missing_credentials"
    assert not (tmp_path / "store").exists()


@pytest.mark.parametrize("selector, expected_calls", [(None, 1), ("lines:1-1", 3)])
def test_verbose_usage_aggregates_current_check(invoke, selector, expected_calls):
    args = ["check", "--verbose", "--question", "What happened?", "--expect", "complete=Completed"]
    if selector:
        args.extend(["--only", selector])
    code, out, err = invoke(args, data=b"x" * 12000)
    result, report = json.loads(out), json.loads(err)
    assert code == 0 and "usage" not in result
    assert report["level"] == "info" and report["code"] == "token_usage"
    assert report["observation_id"] == result["observation_id"]
    assert report["evaluation_id"] == result["evaluation_id"]
    assert report["details"] == {
        "known_input_tokens": expected_calls * 100,
        "known_output_tokens": expected_calls * 5,
        "unknown": False,
        "attempts": expected_calls,
    }


def test_verbose_no_new_text_reports_zero(invoke, monkeypatch):
    _, out, _ = invoke(["check", "--question", "q", "--expect", "complete=Completed"])
    first = json.loads(out)
    monkeypatch.delenv("TYPESAFE_API_KEY")
    code, out, err = invoke(
        [
            "check",
            first["observation_id"],
            "--eval",
            first["evaluation_id"],
            "--only",
            "next",
            "--verbose",
        ]
    )
    assert code == 0 and json.loads(out)["reason"] == "no_new_text"
    assert json.loads(err)["details"] == {
        "known_input_tokens": 0,
        "known_output_tokens": 0,
        "unknown": False,
        "attempts": 0,
    }
    assert len(invoke.provider.calls) == 1


def test_verbose_retry_preserves_unknown_usage(invoke):
    invoke.provider.replies = [ProviderError("transport_error", retryable=True, usage_unknown=True)]
    code, out, err = invoke(
        ["check", "--verbose", "--question", "q", "--expect", "complete=Completed"]
    )
    assert code == 0 and json.loads(out)["run_status"] == "completed"
    assert json.loads(err)["details"] == {
        "known_input_tokens": 100,
        "known_output_tokens": 5,
        "unknown": True,
        "attempts": 2,
    }
    assert "may be higher" in json.loads(err)["message"]


def test_verbose_provider_failure_keeps_error_and_usage_separate(invoke):
    invoke.provider.replies = [ProviderError("invalid_response", usage_unknown=True)]
    code, out, err = invoke(
        ["check", "--verbose", "--question", "q", "--expect", "complete=Completed"]
    )
    reports = [json.loads(line) for line in err.splitlines()]
    assert code == 3 and json.loads(out)["run_status"] == "failed"
    assert [report["level"] for report in reports] == ["info", "error"]
    assert reports[0]["details"] == {
        "known_input_tokens": 0,
        "known_output_tokens": 0,
        "unknown": True,
        "attempts": 1,
    }


def test_verbose_usage_survives_result_persistence_failure(invoke, monkeypatch):
    from semcull.store import Store

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(Store, "save_result", fail)
    code, out, err = invoke(
        ["check", "--verbose", "--question", "q", "--expect", "complete=Completed"]
    )
    reports = [json.loads(line) for line in err.splitlines()]
    assert code == 4 and json.loads(out)["error"]["code"] == "persistence_failed"
    assert reports[0]["details"]["known_input_tokens"] == 100
    assert reports[1]["level"] == "error"


def test_verbose_validation_failure_has_no_usage(invoke):
    code, out, err = invoke(["check", "--verbose"])
    assert code == 2 and out == ""
    assert json.loads(err)["level"] == "error" and not invoke.provider.calls


def test_support_investigation_reuses_source_with_fresh_intents(invoke):
    source = Path(__file__).resolve().parents[1] / "examples" / "customer-support.log"
    investigations = [
        (
            "What concern brought the customer to support?",
            {"billing": "Billing or payment concern", "login_problem": "Cannot log in"},
            "billing",
            "charged twice",
        ),
        (
            "What payment-entry statuses does the customer report seeing?",
            {
                "completed_and_pending": "One completed, one pending",
                "both_completed": "Both completed",
            },
            "completed_and_pending",
            "One says completed",
        ),
        (
            "What next action does the customer explicitly request?",
            {
                "verify_payment": "Verify payment status first",
                "refund": "Refund now",
                "cancel": "Cancel",
            },
            "verify_payment",
            "verify the payment status first",
        ),
    ]
    obs = None
    evaluations = set()
    for question, expectations, choice, needle in investigations:

        def reply(prepared, choice=choice, needle=needle):
            span = next(key for key, (_, _, text) in prepared.spans.items() if needle in text)
            return response(prepared, choice=choice, evidence=span)

        invoke.provider.replies = [reply]
        args = ["check", obs] if obs else ["check", "--file", str(source)]
        args.extend(["--question", question])
        for name, description in expectations.items():
            args.extend(["--expect", f"{name}={description}"])
        code, out, err = invoke(args)
        result = json.loads(out)
        assert code == 0 and err == ""
        if obs:
            assert result["observation_id"] == obs
        obs = result["observation_id"]
        evaluation = result["evaluation_id"]
        assert evaluation not in evaluations
        evaluations.add(evaluation)
        assert result["coverage"] == {
            "evaluated_bytes": source.stat().st_size,
            "total_bytes": source.stat().st_size,
            "has_unexamined": False,
        }
        assert result["results"][0]["outcome"] == choice
        assert needle in result["results"][0]["evidence"][0]["text"]
        code, out, err = invoke(["result", obs, "--eval", evaluation])
        request = json.loads(out)["request"]
        assert code == 0 and request["parent_evaluation_id"] is None
        assert request["intent"] == {"question": question, "expectations": expectations}
    assert len(invoke.provider.calls) == 3
    code, out, err = invoke(["show", obs, "--all"], raw=True)
    assert code == 0 and out == source.read_bytes()


def test_microservice_deployment_outcome_and_diagnosis(invoke):
    examples = Path(__file__).resolve().parents[1] / "examples"
    source = examples / "deployment.log"

    def rollout(prepared):
        span = next(
            key for key, (_, _, text) in prepared.spans.items() if "rollback_completed" in text
        )
        return response(prepared, choice="rolled_back", evidence=span)

    invoke.provider.replies = [rollout]
    code, out, err = invoke(
        [
            "check",
            "--file",
            str(source),
            "--spec",
            str(examples / "deployment-intent.json"),
        ]
    )
    first = json.loads(out)
    assert code == 0 and err == ""
    assert first["results"][0]["outcome"] == "rolled_back"
    assert first["coverage"]["has_unexamined"] is True

    def diagnose(prepared):
        for key, (_, _, text) in prepared.spans.items():
            if "permission denied for table orders" in text or "orders_insert=false" in text:
                return response(prepared, choice="db_permissions", evidence=key)
        return response(prepared, choice="insufficient_evidence", evidence="none")

    invoke.provider.replies = [diagnose] * 10
    code, out, err = invoke(
        [
            "check",
            first["observation_id"],
            "--spec",
            str(examples / "deployment-diagnosis-intent.json"),
            "--only",
            "lines:109-180",
        ]
    )
    diagnosis = json.loads(out)
    assert code == 0 and err == ""
    assert diagnosis["observation_id"] == first["observation_id"]
    assert diagnosis["evaluation_id"] != first["evaluation_id"]
    assert any(result["outcome"] == "db_permissions" for result in diagnosis["results"])
    assert len(diagnosis["results"]) > 1
    data = source.read_bytes()
    lines = data.splitlines(keepends=True)
    assert diagnosis["coverage"]["evaluated_bytes"] == len(b"".join(lines[108:180]))
    assert diagnosis["coverage"]["has_unexamined"] is True
    for result in diagnosis["results"]:
        for evidence in result["evidence"]:
            start, end = evidence["bytes"]
            assert data[start:end].decode() == evidence["text"]


def test_long_startup_progressive_disclosure(invoke):
    source = Path(__file__).resolve().parents[1] / "examples" / "startup-long.log"
    total = source.stat().st_size
    assert total > 1_000_000

    def authentication_failure(prepared):
        span = next(
            key for key, (_, _, text) in prepared.spans.items() if "credentials rejected" in text
        )
        return response(prepared, choice="auth_failed", evidence=span)

    invoke.provider.replies = [
        lambda prepared: response(prepared, choice="insufficient_evidence", evidence="none"),
        authentication_failure,
    ]
    code, out, err = invoke(
        [
            "check",
            "--file",
            str(source),
            "--question",
            "What does this log report about why startup failed?",
            "--expect",
            "auth_failed=Database authentication failed.",
            "--expect",
            "unreachable=The database could not be reached.",
            "--expect",
            "other_failure=Startup failed for another reason.",
        ]
    )
    first = json.loads(out)
    assert code == 0 and err == ""
    assert first["results"][0]["outcome"] == "insufficient_evidence"
    assert first["results"][0]["examined_bytes"] == [total - 4000, total]
    assert first["coverage"] == {
        "evaluated_bytes": 4000,
        "total_bytes": total,
        "has_unexamined": True,
    }
    first_text = "".join(invoke.provider.calls[0].payload["state"].values())
    assert "credentials rejected" not in first_text
    assert "Database authentication failed" not in first_text

    code, out, err = invoke(
        [
            "check",
            first["observation_id"],
            "--eval",
            first["evaluation_id"],
            "--only",
            "next",
            "--verbose",
        ]
    )
    second = json.loads(out)
    assert code == 0 and len(invoke.provider.calls) == 2
    assert json.loads(err)["details"] == {
        "known_input_tokens": 100,
        "known_output_tokens": 5,
        "unknown": False,
        "attempts": 1,
    }
    assert second["observation_id"] == first["observation_id"]
    assert second["evaluation_id"] != first["evaluation_id"]
    result = second["results"][0]
    assert result["examined_bytes"] == [total - 8000, total - 4000]
    assert result["outcome"] == "auth_failed"
    evidence = result["evidence"][0]
    assert "credentials rejected" in evidence["text"]
    start, end = evidence["bytes"]
    with source.open("rb") as stream:
        stream.seek(start)
        assert stream.read(end - start).decode() == evidence["text"]
    assert second["coverage"] == {
        "evaluated_bytes": 8000,
        "total_bytes": total,
        "has_unexamined": True,
    }
    code, out, err = invoke(["result", first["observation_id"], "--eval", first["evaluation_id"]])
    assert code == 0 and json.loads(out)["coverage"] == first["coverage"]


def test_file_source_ignores_stdin(invoke, tmp_path):
    file = tmp_path / "source"
    file.write_text("Completed")
    code, out, err = invoke(
        ["check", "--file", str(file), "--question", "q", "--expect", "complete=yes"], data=b"\xff"
    )
    assert code == 0


def test_show_eval_is_rejected(invoke):
    code, out, _ = invoke(["check", "--question", "q", "--expect", "complete=yes"])
    obs = json.loads(out)["observation_id"]
    code, out, err = invoke(["show", obs, "--eval", "eval_" + "0" * 32])
    assert code == 2 and out == ""


def test_truncation_stdout_is_only_raw_bytes(invoke):
    _, out, _ = invoke(["check", "--question", "q", "--expect", "complete=yes"], b"a" * 20000)
    obs = json.loads(out)["observation_id"]
    code, out, err = invoke(["show", obs], raw=True)
    assert code == 0 and out == b"a" * 16384
    assert "--bytes" in json.loads(err)["message"]
