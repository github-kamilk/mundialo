"""Model Gateway: jeden interfejs do wielu modeli (architektura pkt 2).

Logika biznesowa nie jest przywiazana do dostawcy. `ModelGateway` to waski
kontrakt tekst-in/tekst-out; warstwa structured output (app/models/structured.py)
zajmuje sie JSON-em, walidacja i retry. `FakeModelGateway` pozwala testowac caly
harness bez kluczy i sieci.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ModelError(RuntimeError):
    pass


@dataclass
class FakeModelGateway:
    """Deterministyczny model do testow. Zwraca zaskryptowane odpowiedzi po kolei."""

    responses: list[str] = field(default_factory=list)
    calls: list[dict[str, str]] = field(default_factory=list)

    def complete(self, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        if not self.responses:
            raise ModelError("FakeModelGateway: brak kolejnych zaskryptowanych odpowiedzi")
        return self.responses.pop(0)


@dataclass
class OpenAiModelGateway:
    """Realny adapter OpenAI/Anthropic-compatible. Import leniwy - brak twardej zaleznosci.

    Uruchamiany tylko, gdy jest klucz; w testach uzywamy FakeModelGateway.
    """

    model: str = "gpt-4o-mini"
    temperature: float = 0.7
    api_key: str | None = None
    max_tokens: int = 1500

    def complete(self, system: str, user: str) -> str:
        try:
            from openai import OpenAI
        except ImportError as error:  # pragma: no cover - zalezy od opcjonalnej instalacji
            raise ModelError(
                "pakiet 'openai' nie jest zainstalowany (pip install '.[llm]')"
            ) from error

        key = self.api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ModelError("brak OPENAI_API_KEY")

        client = OpenAI(api_key=key)
        response = client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = response.choices[0].message.content
        if not content:
            raise ModelError("pusta odpowiedz modelu")
        return content
