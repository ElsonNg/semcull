"""Inspection planning and invocation-scoped scheduling, independent of CLI."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

from .config import Config
from .jev import Prepared, ProviderError, classify, fits, prepare, validate_response
from .models import Intent, SemcullError
from .store import Store
from .windows import coverage, merge, parse_selector, subtract, trim_utf8


def inherited(store: Store, obs: str, evaluation: str) -> tuple[dict, list[tuple[int, int]]]:

    request = store.evaluation_request(obs, evaluation)
    fingerprint = (request.get("intent"), request.get("model"), request.get("policy"))
    seen, ranges = set(), []
    total = store.metadata(obs)["bytes"]
    current = evaluation

    while current:

        if current in seen:
            raise SemcullError("corrupt_record", "Evaluation ancestry contains a cycle.", 4)
        
        seen.add(current)
        parent_request = store.evaluation_request(obs, current)
        if (
            parent_request.get("intent"),
            parent_request.get("model"),
            parent_request.get("policy"),
        ) != fingerprint:
            raise SemcullError(
                "corrupt_record", "Evaluation ancestry changes intent, model, or policy.", 4
            )
        
        value = store.evaluation_result(obs, current)

        for result in value.get("results", []):
            bounds = result.get("examined_bytes")
            if (
                not isinstance(bounds, list)
                or len(bounds) != 2
                or any(type(x) is not int for x in bounds)
                or not 0 <= bounds[0] < bounds[1] <= total
            ):
                raise SemcullError(
                    "corrupt_record", "Saved evaluation contains an invalid range.", 4
                )
            ranges.append(tuple(bounds))


        current = parent_request.get("parent_evaluation_id")

    return request, merge(ranges)


def resolve_context(
    store: Store,
    obs: str,
    intent: Intent | None,
    evaluation: str | None,
    config: Config,
    *,
    model_override=False,
    policy_override=False,
):
    if intent is not None:
        if evaluation:
            raise SemcullError(
                "invalid_arguments", "A fresh intent and --eval are mutually exclusive."
            )
        return intent, config, None, []
    
    selected = store.select_evaluation(obs, evaluation)
    request, ranges = inherited(store, obs, selected)
    saved_intent = Intent.parse(request.get("intent"))


    try:
        model = config.model if model_override else request["model"]
        confidence = (
            config.min_confidence if policy_override else request["policy"]["min_confidence"]
        )
        effective = replace(config, model=model, min_confidence=confidence).validate()

    except (KeyError, TypeError, SemcullError):
        raise SemcullError(
            "corrupt_record", "Saved evaluation configuration is invalid.", 4
        ) from None
    if model != request["model"] or effective.policy() != request["policy"]:
        return saved_intent, effective, None, []
    return saved_intent, effective, selected, ranges


def _window(
    store: Store,
    obs: str,
    bounds: tuple[int, int],
    intent: Intent,
    config: Config,
    *,
    tail=False,
    full=False,
) -> tuple[tuple[int, int], Prepared]:
    
    left, right = bounds
    size = right - left if full else min(right - left, config.window)

    while size > 0:
        start, end = (right - size, right) if tail else (left, left + size)
        data = store.read(obs, (start, end))
        start, data = trim_utf8(data, start, tail=tail)
        
        if not data:
            # A four-byte scalar needs at least four budgeted source bytes.
            if size < min(4, right - left):
                size = min(4, right - left)
                data = store.read(obs, (right - size, right) if tail else (left, left + size))
                start, data = trim_utf8(data, right - size if tail else left, tail=tail)

            if not data:
                raise SemcullError(
                    "invalid_window", "Window cannot contain a complete UTF-8 character."
                )
        try:
            prepared = prepare(intent, data.decode("utf-8"), start, config.model)
            if fits(prepared, config, full=full):
                return (start, start + len(data)), prepared
            
        except SemcullError as exc:
            if exc.code != "request_too_large":
                raise

        if full:
            raise SemcullError(
                "request_too_large", "Full input exceeds the conservative request budget."
            )
        
        if size <= 4:
            break
        size //= 2

    raise SemcullError(
        "request_too_large", "Intent and evidence instructions exceed the request budget."
    )


def plan(
    store: Store,
    obs: str,
    intent: Intent,
    config: Config,
    selector: str | None,
    previous: list[tuple[int, int]],
    parent: str | None,
):
    kind, selected = parse_selector(selector)
    total = store.metadata(obs)["bytes"]

    if kind == "next":
        if not parent:
            raise SemcullError(
                "invalid_selector",
                "--only next requires an existing evaluation with unchanged intent/model/policy.",
            )
        
        remaining = subtract((0, total), previous)
        if not remaining:
            return [], iter(())
        
        bounds, prepared = _window(store, obs, remaining[-1], intent, config, tail=True)
        return [bounds], iter([(bounds, prepared)])
    
    if kind == "lines":
        bounds = store.line_range(obs, *selected)

        def windows():
            cursor = bounds[0]
            while cursor < bounds[1]:
                piece, prepared = _window(store, obs, (cursor, bounds[1]), intent, config)
                yield piece, prepared
                cursor = piece[1]

        return [bounds], windows()
    
    if kind == "auto" and total <= config.full_budget:
        try:
            bounds, prepared = _window(store, obs, (0, total), intent, config, full=True)
            return [bounds], iter([(bounds, prepared)])
        
        except SemcullError as exc:
            if exc.code != "request_too_large":
                raise

    bounds, prepared = _window(store, obs, (0, total), intent, config, tail=True)
    return [bounds], iter([(bounds, prepared)])


async def run_check(
    store: Store,
    obs: str,
    intent: Intent,
    config: Config,
    provider,
    *,
    selector=None,
    parent=None,
    previous=None,
    sleep=asyncio.sleep,
    usage_report: dict | None = None,
) -> tuple[dict, int]:
    
    previous = previous or []
    requested, windows = plan(store, obs, intent, config, selector, previous, parent)

    request = {
        "schema_version": "1",
        "intent": intent.as_dict(),
        "model": config.model,
        "policy": config.policy(),
        "parent_evaluation_id": parent,
        "selector": selector or "auto",
        "requested_bytes": requested,
        "limits": {
            "max_attempts": config.max_attempts,
            "parallel": config.parallel,
            "max_tokens": config.max_tokens,
            "max_seconds": config.max_seconds,
        },
    }

    evaluation = store.create_evaluation(obs, request)
    total = store.metadata(obs)["bytes"]
    successful, records, completed = [], [], []
    attempts = tokens = streak = 0
    input_tokens = output_tokens = 0
    usage_unknown = False
    stop = None
    failure_exit = 3
    interrupted = False
    pending = {}
    retries = []
    exhausted = False
    deadline = time.monotonic() + config.max_seconds

    async def attempt(prepared, number):
        if number > 1:
            await sleep(min(0.25 * 2 ** (number - 2), 2.0))
        return await provider.evaluate(prepared)

    try:
        while True:
            while not stop and len(pending) < config.parallel:
                if time.monotonic() >= deadline:
                    stop = ("time_budget_exhausted", "Inspection time budget reached.")
                    break
                if retries:
                    bounds, prepared, number = retries.pop(0)
                elif not exhausted:
                    item = next(windows, None)
                    if item is None:
                        exhausted = True
                        break
                    bounds, prepared = item
                    number = 1
                else:
                    break
                if (
                    attempts >= config.max_attempts
                    or tokens + prepared.estimate > config.max_tokens
                ):
                    stop = (
                        "budget_exhausted",
                        "Inspection attempt or estimated-token budget reached.",
                    )
                    break
                attempts += 1
                tokens += prepared.estimate
                task = asyncio.create_task(attempt(prepared, number))
                pending[task] = (bounds, prepared, number)
            if not pending:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop = ("time_budget_exhausted", "Inspection time budget reached.")
                usage_unknown = True
                break
            done, _ = await asyncio.wait(
                pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                stop = ("time_budget_exhausted", "Inspection time budget reached.")
                usage_unknown = True
                break
            
            # Completion batches are processed deterministically by source range.
            for task in sorted(done, key=lambda task: pending[task][0]):
                bounds, prepared, number = pending.pop(task)
                record = {
                    "requested_bytes": list(bounds),
                    "attempt": number,
                    "estimated_tokens": prepared.estimate,
                }
                try:
                    response = validate_response(task.result(), prepared)
                except ProviderError as exc:
                    record["error"] = {"code": exc.code, "retryable": exc.retryable}
                    record["usage_unknown"] = exc.usage_unknown
                    usage_unknown |= exc.usage_unknown
                    if exc.retryable:
                        streak += 1
                        if streak >= 3:
                            stop = ("classification_unavailable", "Jev failure limit reached.")
                        elif number < 3 and not stop:
                            retries.append((bounds, prepared, number + 1))
                        else:
                            stop = stop or (
                                "classification_unavailable",
                                "Window attempt limit reached.",
                            )
                    else:
                        stop = ("classification_unavailable", "Jev could not complete the request.")
                else:
                    if not stop:
                        streak = 0
                    result = classify(response, prepared, bounds, config)
                    successful.append(result)
                    completed.append(bounds)
                    input_tokens += response["usage"]["input_tokens"]
                    output_tokens += response["usage"]["output_tokens"]
                    record.update(result=result, provider=response)
                store.save_window(obs, evaluation, record)
                records.append(record)
    except asyncio.CancelledError:
        interrupted = True
        usage_unknown |= bool(pending)
        stop = ("interrupted", "Inspection was interrupted.")
    except (SemcullError, OSError) as exc:
        failure_exit = exc.exit_code if isinstance(exc, SemcullError) else 4
        stop = (
            "inspection_failed",
            "Inspection or result persistence failed; saved records may remain recoverable.",
        )
        usage_unknown |= bool(pending)
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    unfinished = merge([piece for bounds in requested for piece in subtract(bounds, completed)])
    status = ("partial" if successful else "failed") if stop else "completed"
    result = {
        "schema_version": "1",
        "observation_id": obs,
        "evaluation_id": evaluation,
        "run_status": status,
        "results": sorted(successful, key=lambda x: x["examined_bytes"]),
        "coverage": coverage(total, previous + completed),
    }
    if stop:
        result["error"] = {"code": stop[0], "message": stop[1]}
        result["unfinished_bytes"] = [list(bounds) for bounds in unfinished]
    elif not requested:
        result["reason"] = "no_new_text"
    detailed = {
        **result,
        "request": request,
        "window_records": records,
        "usage": {
            "known_input_tokens": input_tokens,
            "known_output_tokens": output_tokens,
            "unknown": usage_unknown,
            "attempts": attempts,
            "estimated_tokens": tokens,
        },
    }
    if usage_report is not None:
        usage_report.update(detailed["usage"])
    try:
        store.save_result(obs, evaluation, detailed)
    except (OSError, SemcullError):
        error = {
            "code": "persistence_failed",
            "message": "Evaluation could not be saved; published window records may remain recoverable.",
        }
        result.update(run_status="partial" if successful else "failed", error=error)
        return result, 4
    return result, 130 if interrupted else (failure_exit if stop else 0)


def compact_result(value: dict, limit: int) -> dict:
    def size(item):
        return len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1

    if size(value) <= limit:
        return value
    result = dict(value)
    result["results"] = list(value["results"])
    result["details_omitted"] = True
    result["omitted_results"] = 0
    result["details_message"] = (
        "Use result with these observation/evaluation IDs before drawing conclusions from omitted windows."
    )
    while result["results"] and size(result) > limit:
        result["results"].pop()
        result["omitted_results"] += 1
    if size(result) > limit and "unfinished_bytes" in result:
        result["omitted_unfinished_ranges"] = len(result.pop("unfinished_bytes"))
    return result


def get_result(store: Store, obs: str, evaluation: str) -> dict:
    value = store.evaluation_result(obs, evaluation)
    if value["run_status"] != "incomplete":
        return value
    # Recovery is a snapshot, not a claim that the original writer has stopped.
    parent = value["request"].get("parent_evaluation_id")
    previous = inherited(store, obs, parent)[1] if parent else []
    current = [tuple(item["examined_bytes"]) for item in value["results"]]
    value["coverage"] = coverage(store.metadata(obs)["bytes"], previous + current)
    value["usage"] = {
        "known_input_tokens": sum(
            r.get("provider", {}).get("usage", {}).get("input_tokens", 0)
            for r in value["window_records"]
        ),
        "known_output_tokens": sum(
            r.get("provider", {}).get("usage", {}).get("output_tokens", 0)
            for r in value["window_records"]
        ),
        "unknown": True,
    }
    return value
