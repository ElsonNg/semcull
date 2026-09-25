"""Four commands, JSON diagnostics, and raw-only source stdout."""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
from contextlib import nullcontext

from . import __version__
from .check import compact_result, get_result, plan, resolve_context, run_check
from .config import load_config
from .credentials import api_key
from .jev import Jev, fits, prepare
from .models import Intent, SemcullError, strict_json, validate_id
from .store import Store
from .windows import parse_range, parse_selector


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse messages may echo arbitrary user input, including secrets.
        raise SemcullError(
            "invalid_arguments", "Invalid command arguments. Use semcull --help or command --help."
        )


def parser() -> Parser:
    root = Parser(
        prog="semcull", description="Inspect noisy tool output against explicit expectations."
    )
    root.add_argument("--version", action="version", version=f"semcull {__version__}")
    root.add_argument(
        "--config", metavar="PATH", help="Use this TOML file instead of the default configuration"
    )
    commands = root.add_subparsers(dest="command", required=True, parser_class=Parser)
    check = commands.add_parser("check", help="Classify new or saved output")
    check.add_argument("observation", nargs="?")
    check.add_argument("--file")
    check.add_argument("--spec")
    check.add_argument("--question")
    check.add_argument("--expect", action="append", default=[])
    check.add_argument("--eval", dest="evaluation")
    check.add_argument("--only", help="tail, next, or lines:START-END")
    check.add_argument(
        "--window", type=int, help="Conservative source-token estimate budget (UTF-8 bytes)"
    )
    check.add_argument("--model")
    check.add_argument("--min-confidence", type=float)
    check.add_argument(
        "--verbose", action="store_true", help="Report this check's Jev token usage on stderr"
    )
    show = commands.add_parser("show", help="Retrieve raw saved source")
    show.add_argument("observation")
    selectors = show.add_mutually_exclusive_group()
    selectors.add_argument("--lines", help="One-based inclusive START:END")
    selectors.add_argument("--bytes", dest="byte_range", help="Zero-based half-open START:END")
    selectors.add_argument("--all", action="store_true", dest="all_source")
    result = commands.add_parser("result", help="Retrieve full saved evaluation JSON")
    result.add_argument("observation")
    result.add_argument("--eval", dest="evaluation")
    delete = commands.add_parser(
        "delete", help="Delete a saved observation or a snapshot of all observations"
    )
    delete.add_argument("observation", nargs="?")
    delete.add_argument("--all", action="store_true", dest="all_observations")
    return root


def emit(value, stream):
    stream.write(
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    )
    stream.flush()


def read_intent(args) -> Intent | None:
    if args.spec:
        if args.question is not None or args.expect:
            raise SemcullError(
                "invalid_arguments", "--spec and inline intent flags are mutually exclusive."
            )
        try:
            with open(args.spec, "r", encoding="utf-8") as stream:
                value = stream.read(1024 * 1024 + 1)
            if len(value) > 1024 * 1024:
                raise SemcullError(
                    "invalid_intent", "Intent specification is too large; shorten the criteria."
                )
            return Intent.parse(strict_json(value))
        except (ValueError, UnicodeError):
            raise SemcullError(
                "invalid_intent", "Intent must be valid JSON without duplicate keys."
            ) from None
    if args.question is not None or args.expect:
        return Intent.inline(args.question, args.expect)
    return None


async def _execute_check(
    store, obs, intent, config, args, provider_factory, parent, previous, usage_report
):
    requested, _ = plan(store, obs, intent, config, args.only, previous, parent)
    if not requested:
        return await run_check(
            store,
            obs,
            intent,
            config,
            None,
            selector=args.only,
            parent=parent,
            previous=previous,
            usage_report=usage_report,
        )
    key = api_key()
    if not key:
        raise SemcullError(
            "missing_credentials", "Set TYPESAFE_API_KEY before a check that calls Jev."
        )
    async with provider_factory(key, config.timeout) as provider:
        return await run_check(
            store,
            obs,
            intent,
            config,
            provider,
            selector=args.only,
            parent=parent,
            previous=previous,
            usage_report=usage_report,
        )


def main(argv=None, *, stdin=None, stdout=None, stderr=None, provider_factory=Jev) -> int:
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    references = {}
    try:
        args = parser().parse_args(argv)
        overrides = {
            name: getattr(args, name, None) for name in ("window", "model", "min_confidence")
        }
        config = load_config(args.config, overrides)
        intent = None
        if getattr(args, "observation", None):
            validate_id(args.observation, "obs")
        if getattr(args, "evaluation", None):
            validate_id(args.evaluation, "eval")
        if args.command == "check":
            intent = read_intent(args)
            kind, _ = parse_selector(args.only)
            if args.observation and args.file:
                raise SemcullError(
                    "invalid_arguments", "Choose an observation ID or --file, not both."
                )
            if args.evaluation and (not args.observation or intent is not None):
                raise SemcullError(
                    "invalid_arguments",
                    "--eval requires saved input and cannot accompany a fresh intent.",
                )
            if kind == "next" and (not args.observation or intent is not None):
                raise SemcullError(
                    "invalid_selector",
                    "--only next requires saved input and an existing evaluation.",
                )
            if not args.observation:
                if intent is None:
                    raise SemcullError(
                        "intent_required", "New input requires --spec or --question with --expect."
                    )
                if not args.file and stdin.isatty():
                    raise SemcullError(
                        "input_required", "Pipe finite UTF-8 input or specify --file."
                    )
                if not api_key():
                    raise SemcullError(
                        "missing_credentials", "Set TYPESAFE_API_KEY before a check that calls Jev."
                    )
            if intent is not None and not fits(prepare(intent, "x", 0, config.model), config):
                raise SemcullError(
                    "request_too_large", "Intent exceeds the request budget; shorten the criteria."
                )
        if args.command == "delete" and bool(args.observation) == args.all_observations:
            raise SemcullError("invalid_arguments", "Supply one observation ID or --all.")
        lines = (
            parse_range(args.lines, lines=True) if args.command == "show" and args.lines else None
        )
        byte_range = (
            parse_range(args.byte_range) if args.command == "show" and args.byte_range else None
        )
        with Store(
            config.root,
            config.max_observations,
            session_scoped=config.session_id is not None,
        ) as store:
            if args.command == "show":
                warning = store.show(
                    args.observation,
                    getattr(stdout, "buffer", stdout),
                    lines=lines,
                    byte_range=byte_range,
                    all_source=args.all_source,
                    limit=config.show_max_bytes,
                    command_prefix="semcull"
                    + (f" --config {shlex.quote(args.config)}" if args.config else ""),
                )
                if warning:
                    emit(warning, stderr)
                return 0
            if args.command == "result":
                selected = store.select_evaluation(args.observation, args.evaluation)
                emit(get_result(store, args.observation, selected), stdout)
                return 0
            if args.command == "delete":
                if args.all_observations:
                    result = store.delete_all()
                    emit(result, stdout)
                    if result["failed"]:
                        emit(
                            SemcullError(
                                "deletion_failed", "Some observations could not be deleted.", 4
                            ).diagnostic(),
                            stderr,
                        )
                        return 4
                else:
                    emit(store.delete(args.observation), stdout)
                return 0
            parent, previous = None, []
            obs = args.observation
            if obs:
                references["observation_id"] = obs
                intent, config, parent, previous = resolve_context(
                    store,
                    obs,
                    intent,
                    args.evaluation,
                    config,
                    model_override=args.model is not None,
                    policy_override=args.min_confidence is not None,
                )
            else:
                source = (
                    open(args.file, "rb")
                    if args.file
                    else nullcontext(getattr(stdin, "buffer", stdin))
                )
                with source as stream:
                    obs = store.capture(stream)
                references["observation_id"] = obs
            usage_report = {}
            result, code = asyncio.run(
                _execute_check(
                    store,
                    obs,
                    intent,
                    config,
                    args,
                    provider_factory,
                    parent,
                    previous,
                    usage_report,
                )
            )
            emit(compact_result(result, config.check_max_bytes), stdout)
            if args.verbose and usage_report:
                emit(
                    {
                        "schema_version": "1",
                        "level": "info",
                        "code": "token_usage",
                        "message": (
                            "Known Jev token usage; total usage may be higher."
                            if usage_report["unknown"]
                            else "Jev token usage for this check."
                        ),
                        "observation_id": obs,
                        "evaluation_id": result["evaluation_id"],
                        "details": {
                            key: usage_report[key]
                            for key in (
                                "known_input_tokens",
                                "known_output_tokens",
                                "unknown",
                                "attempts",
                            )
                        },
                    },
                    stderr,
                )
            if code:
                error = result["error"]
                emit(
                    SemcullError(error["code"], error["message"], code).diagnostic(
                        observation_id=obs, evaluation_id=result["evaluation_id"]
                    ),
                    stderr,
                )
            return code
    except SemcullError as exc:
        emit(exc.diagnostic(**references), stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        emit(SemcullError("interrupted", "Operation interrupted.", 130).diagnostic(), stderr)
        return 130
    except BrokenPipeError:
        return 4
    except FileNotFoundError:
        emit(
            SemcullError("not_found", "Requested input or artifact was not found.").diagnostic(),
            stderr,
        )
        return 2
    except OSError:
        emit(SemcullError("storage_error", "Filesystem operation failed.", 4).diagnostic(), stderr)
        return 4
    except Exception:
        emit(SemcullError("internal_error", "Unexpected internal error.", 1).diagnostic(), stderr)
        return 1
