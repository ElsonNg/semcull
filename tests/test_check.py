import asyncio
from dataclasses import replace

import pytest
from conftest import FakeProvider, response

from semcull.check import compact_result, get_result, inherited, plan, resolve_context, run_check
from semcull.config import Config
from semcull.jev import ProviderError
from semcull.models import Intent, SemcullError


async def no_sleep(seconds):
    pass


def run(store, obs, intent, provider, config=None, **kwargs):
    return asyncio.run(
        run_check(store, obs, intent, config or Config(), provider, sleep=no_sleep, **kwargs)
    )


def test_full_tail_and_no_new_text(store, capture, intent, fake):
    obs = capture()
    first, code = run(store, obs, intent, fake)
    assert code == 0 and first["coverage"]["has_unexamined"] is False
    parent = first["evaluation_id"]
    _, previous = inherited(store, obs, parent)
    second, code = run(store, obs, intent, fake, parent=parent, previous=previous, selector="next")
    assert code == 0 and second["reason"] == "no_new_text" and second["results"] == []
    assert second["evaluation_id"] != parent and len(fake.calls) == 1


def test_tail_next_and_independent_branch(store, capture, intent, fake):
    obs = capture(b"x" * 20000)
    first, _ = run(store, obs, intent, fake)
    assert first["results"][0]["examined_bytes"] == [16000, 20000]
    parent = first["evaluation_id"]
    _, previous = inherited(store, obs, parent)
    a, _ = run(store, obs, intent, fake, parent=parent, previous=previous, selector="next")
    b, _ = run(store, obs, intent, fake, parent=parent, previous=previous, selector="next")
    assert a["results"][0]["examined_bytes"] == b["results"][0]["examined_bytes"] == [12000, 16000]
    assert a["coverage"]["evaluated_bytes"] == 8000
    assert first["coverage"]["evaluated_bytes"] == 4000


def test_explicit_range_budget_preserves_unfinished(store, capture, intent, fake):
    obs = capture(b"x" * 12000 + b"\n")
    result, code = run(
        store,
        obs,
        intent,
        fake,
        replace(Config(), max_attempts=1, parallel=1),
        selector="lines:1-1",
    )
    assert code == 3 and result["run_status"] == "partial"
    assert result["unfinished_bytes"] == [[4000, 12001]]
    assert result["coverage"]["evaluated_bytes"] == 4000


def test_three_retryable_failures_stop_invocation_but_not_next(store, capture, intent):
    obs = capture()
    provider = FakeProvider(
        [ProviderError("transport_error", retryable=True, usage_unknown=True)] * 3
    )
    result, code = run(store, obs, intent, provider, replace(Config(), parallel=1))
    assert code == 3 and len(provider.calls) == 3
    assert result["run_status"] == "failed" and result["coverage"]["evaluated_bytes"] == 0
    detailed = store.evaluation_result(obs, result["evaluation_id"])
    assert detailed["usage"]["unknown"] is True
    next_result, code = run(store, obs, intent, provider)
    assert code == 0 and next_result["run_status"] == "completed"


def test_nonretryable_immediate(store, capture, intent):
    provider = FakeProvider([ProviderError("authentication_failed")])
    result, code = run(store, capture(), intent, provider)
    assert code == 3 and len(provider.calls) == 1


@pytest.mark.parametrize("outcome", ["failed", "insufficient_evidence", "ambiguous", "other"])
def test_adverse_and_abstention_are_successes(store, capture, intent, outcome):
    provider = FakeProvider([lambda request: response(request, choice=outcome)])
    result, code = run(store, capture(), intent, provider)
    assert code == 0 and result["results"][0]["outcome"] == outcome
    assert result["coverage"]["has_unexamined"] is False


def test_new_intent_and_model_reset_coverage(store, capture, intent, fake):
    obs = capture()
    result, _ = run(store, obs, intent, fake)
    evaluation = result["evaluation_id"]
    fresh = Intent.inline("new question", ["new=new state"])
    _, _, parent, previous = resolve_context(store, obs, fresh, None, Config())
    assert parent is None and previous == []
    _, effective, parent, previous = resolve_context(
        store, obs, None, evaluation, replace(Config(), model="jev-9.9.9"), model_override=True
    )
    assert effective.model == "jev-9.9.9" and parent is None and previous == []
    _, effective, _, _ = resolve_context(
        store, obs, None, evaluation, replace(Config(), model="jev-9.9.9")
    )
    assert effective.model == Config().model


def test_saved_selection_requires_eval_when_multiple(store, capture, intent, fake):
    obs = capture()
    first, _ = run(store, obs, intent, fake)
    assert store.select_evaluation(obs, None) == first["evaluation_id"]
    run(store, obs, intent, fake)
    with pytest.raises(SemcullError, match="Multiple"):
        store.select_evaluation(obs, None)


def test_recovery_without_manifest(store, capture, intent):
    obs = capture()
    req = {
        "intent": intent.as_dict(),
        "model": Config().model,
        "policy": Config().policy(),
        "parent_evaluation_id": None,
    }
    evaluation = store.create_evaluation(obs, req)
    store.save_window(
        obs,
        evaluation,
        {"result": {"outcome": "complete", "examined_bytes": [0, 5], "evidence": []}},
    )
    saved = store.evaluation_result(obs, evaluation)
    assert saved["run_status"] == "incomplete"
    _, ranges = inherited(store, obs, evaluation)
    assert ranges == [(0, 5)]


def test_out_of_order_parallel_results_are_sorted(store, capture, intent):
    obs = capture(b"a" * 12000)

    class Delayed(FakeProvider):
        async def evaluate(self, prepared):
            start = next(iter(prepared.spans.values()))[0]
            await asyncio.sleep(0.003 if start == 0 else 0)
            return response(prepared)

    result, code = run(store, obs, intent, Delayed(), selector="lines:1-1")
    assert code == 0
    assert [r["examined_bytes"][0] for r in result["results"]] == [0, 4000, 8000]


def test_deletion_does_not_recreate_store(store, capture, intent):
    obs = capture()

    class Deleting(FakeProvider):
        async def evaluate(self, prepared):
            store.delete(obs)
            return response(prepared)

    result, code = run(store, obs, intent, Deleting())
    assert code == 4 and result["error"]["code"] == "persistence_failed"
    assert result["observation_id"] == obs
    assert not (store.root / "observations" / obs).exists()


def test_cutoff_does_not_restart_after_inflight_success(store, capture, intent):
    obs = capture(b"x" * 24000)

    class Mixed(FakeProvider):
        async def evaluate(self, prepared):
            self.calls.append(prepared)
            start = next(iter(prepared.spans.values()))[0]
            if start == 12000:
                await asyncio.sleep(0.01)
                return response(prepared)
            raise ProviderError("transport_error", retryable=True)

    provider = Mixed()
    result, code = run(store, obs, intent, provider, selector="lines:1-1")
    assert code == 3 and len(provider.calls) == 4
    assert result["run_status"] == "partial"
    assert result["results"][0]["examined_bytes"] == [12000, 16000]


def test_cancel_persists_an_incomplete_result(store, capture, intent):
    obs = capture()

    class Blocking(FakeProvider):
        async def evaluate(self, prepared):
            await asyncio.sleep(60)

    async def exercise():
        task = asyncio.create_task(run_check(store, obs, intent, Config(), Blocking()))
        await asyncio.sleep(0.01)
        task.cancel()
        return await task

    result, code = asyncio.run(exercise())
    assert code == 130 and result["error"]["code"] == "interrupted"
    assert store.evaluation_result(obs, result["evaluation_id"])["usage"]["unknown"]


def test_execution_deadline_cancels_inflight(store, capture, intent):
    class Blocking(FakeProvider):
        async def evaluate(self, prepared):
            await asyncio.sleep(60)

    result, code = run(store, capture(), intent, Blocking(), replace(Config(), max_seconds=0.01))
    assert code == 3 and result["error"]["code"] == "time_budget_exhausted"


def test_budget_can_stop_before_any_request(store, capture, intent, fake):
    result, code = run(store, capture(), intent, fake, replace(Config(), max_tokens=1))
    assert code == 3 and result["results"] == [] and fake.calls == []


def test_forced_tail_on_small_input(store, capture, intent):
    obs = capture(b"x" * 8000)
    normal, _ = plan(store, obs, intent, Config(), None, [], None)
    forced, _ = plan(store, obs, intent, Config(), "tail", [], None)
    assert normal == [(0, 8000)] and forced == [(4000, 8000)]


def test_full_input_accounts_for_overhead(store, capture, intent):
    obs = capture(b"x" * 12000)
    selected, _ = plan(store, obs, intent, Config(), None, [], None)
    assert selected[0][0] > 0


def test_unicode_windows_make_progress_without_overlap(store, capture, intent, fake):
    data = "🙂".encode() * 100
    obs = capture(data)
    result, code = run(
        store,
        obs,
        intent,
        fake,
        replace(Config(), window=17, max_attempts=40, max_tokens=500000),
        selector="lines:1-1",
    )
    assert code == 0
    assert result["coverage"]["evaluated_bytes"] == len(data)
    bounds = [r["examined_bytes"] for r in result["results"]]
    assert all(a[1] == b[0] for a, b in zip(bounds, bounds[1:]))


def test_compact_omissions_do_not_modify_saved_shape():
    import json

    value = {
        "schema_version": "1",
        "observation_id": "obs_x",
        "evaluation_id": "eval_x",
        "run_status": "completed",
        "results": [{"outcome": "x", "evidence": [{"text": "x" * 900}]}] * 20,
        "coverage": {"has_unexamined": False},
    }
    result = compact_result(value, 4096)
    assert len(json.dumps(result, separators=(",", ":")).encode()) < 4096
    assert result["details_omitted"] and result["omitted_results"] > 0
    assert len(value["results"]) == 20


def test_recovered_result_includes_coverage_and_unknown_usage(store, capture, intent):
    obs = capture()
    evaluation = store.create_evaluation(
        obs,
        {
            "intent": intent.as_dict(),
            "model": Config().model,
            "policy": Config().policy(),
            "parent_evaluation_id": None,
        },
    )
    store.save_window(
        obs,
        evaluation,
        {"result": {"outcome": "insufficient_evidence", "examined_bytes": [0, 5], "evidence": []}},
    )
    result = get_result(store, obs, evaluation)
    assert result["coverage"]["evaluated_bytes"] == 5
    assert result["usage"]["unknown"] is True
