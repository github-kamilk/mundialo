"""Structured output: validate -> feedback -> retry -> fallback.

Model nigdy nie oddaje wolnego tekstu do systemu. Kazda odpowiedz jest parsowana
do struktury i walidowana; przy bledzie wstrzykujemy komunikat z powrotem do
modelu i ponawiamy. Po wyczerpaniu prob rzucamy GenerationError - wtedy warstwa
wyzej robi fallback (np. deterministyczny copywriter) albo needs_human_review.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, TypeVar, runtime_checkable

T = TypeVar("T")


@runtime_checkable
class ModelGateway(Protocol):
    def complete(self, system: str, user: str) -> str: ...


@dataclass
class GenerationAttempt:
    raw: str
    error: str | None


class GenerationError(RuntimeError):
    def __init__(self, message: str, attempts: list[GenerationAttempt]) -> None:
        super().__init__(message)
        self.attempts = attempts


def parse_json_object(raw: str) -> dict[str, Any]:
    """Toleruje czesty przypadek modelu owijajacego JSON w ```json ... ```."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("oczekiwano obiektu JSON na najwyzszym poziomie")
    return data


def generate_structured(
    gateway: ModelGateway,
    system: str,
    user: str,
    build: Callable[[dict[str, Any]], T],
    max_retries: int = 2,
) -> T:
    """`build` parsuje slownik w obiekt domenowy i waliduje (rzuca ValueError przy bledzie)."""
    attempts: list[GenerationAttempt] = []
    feedback = ""
    for _ in range(max_retries + 1):
        prompt = user
        if feedback:
            prompt = (
                f"{user}\n\nTWOJA POPRZEDNIA ODPOWIEDZ BYLA NIEPOPRAWNA:\n{feedback}\n"
                "Zwroc wylacznie poprawny JSON zgodny ze schematem."
            )
        raw = gateway.complete(system, prompt)
        try:
            data = parse_json_object(raw)
            obj = build(data)
            return obj
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            feedback = str(error)
            attempts.append(GenerationAttempt(raw=raw, error=feedback))
    raise GenerationError(
        f"nie udalo sie wygenerowac poprawnej struktury po {max_retries + 1} probach: {feedback}",
        attempts,
    )
