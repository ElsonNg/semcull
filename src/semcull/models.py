"""Small shared types and strict intent parsing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

# Default error routing groups that are always included
RESERVED = {
    "insufficient_evidence": "The examined text does not provide enough evidence to answer.",
    "ambiguous": "The examined text supports competing interpretations or outcomes.",
    "other": "The examined text supports an outcome outside the supplied expectations.",
}
ID_PATTERN = re.compile(r"[a-z][a-z0-9_]*\Z")


class SemcullError(Exception):

    def __init__(self, code: str, message: str, exit_code: int = 2, **details: Any):
        super().__init__(message)
        self.code, self.message, self.exit_code = code, message, exit_code
        self.details = details

    def diagnostic(self, level: str = "error", **refs: str) -> dict:
        result = {
            "schema_version": "1",
            "level": level,
            "code": self.code,
            "message": self.message,
            **refs,
        }
        if self.details:
            result["details"] = self.details
        return result


def strict_json(text: str) -> Any:

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Non-finite JSON number")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


@dataclass(frozen=True)
class Intent:
    question: str
    expectations: dict[str, str]

    @classmethod
    def parse(cls, value: Any) -> Intent:
        if not isinstance(value, dict) or set(value) != {"question", "expectations"}:
            raise SemcullError("invalid_intent", "Intent requires only question and expectations.")
        
        question, expectations = value["question"], value["expectations"]

        if not isinstance(question, str) or not question.strip():
            raise SemcullError("invalid_intent", "Question must be a nonempty string.")
        
        if not isinstance(expectations, dict) or not 1 <= len(expectations) <= 252:
            raise SemcullError("invalid_intent", "Supply between 1 and 252 expectations.")
        
        for key, description in expectations.items():

            if not isinstance(key, str) or not ID_PATTERN.fullmatch(key) or key in RESERVED:
                raise SemcullError(
                    "invalid_intent",
                    "Expectation IDs must be unique, nonreserved lowercase identifiers.",
                )
            if not isinstance(description, str) or not description.strip():
                raise SemcullError(
                    "invalid_intent", "Expectation descriptions must be nonempty strings."
                )

            
        return cls(question, dict(expectations))

    @classmethod
    def inline(cls, question: str | None, values: list[str]) -> Intent:
        expectations = {}

        for item in values:
            name, sep, description = item.partition("=")
            if not sep or name in expectations:
                raise SemcullError(
                    "invalid_intent", "Each --expect needs a unique name=description."
                )
            expectations[name] = description
        return cls.parse({"question": question, "expectations": expectations})

    def as_dict(self) -> dict:
        return {"question": self.question, "expectations": dict(self.expectations)}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def validate_id(value: str, prefix: str) -> str:
    if not re.fullmatch(rf"{prefix}_[0-9a-f]{{32}}", value):
        raise SemcullError("invalid_id", f"Expected a {prefix}_ identifier, not a path.")
    return value
