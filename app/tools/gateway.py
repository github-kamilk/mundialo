from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.schemas import (
    EvidenceItem,
    GoalEvent,
    MatchFacts,
    MetricSnapshot,
    MetricValue,
    PublicNarrative,
    ScoreLine,
    SourceTier,
)
from app.tools.contracts import ProviderCapability, RawMediaItem
from app.tools.control import BudgetTracker, sanitize_external_text
from app.tools.registry import SourcePolicyError, SourceRegistry


class ToolGatewayError(RuntimeError):
    pass


@dataclass
class ToolCall:
    tool: str
    args: dict[str, Any]
    status: str
    observation: str


@dataclass
class FixtureRepository:
    root: Path
    _cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.root.exists():
            raise ToolGatewayError(f"fixture root does not exist: {self.root}")

    def all_matches(self) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.json")):
            if path.name.startswith("_"):
                continue
            matches.append(self.load(path.stem))
        return matches

    def load(self, fixture_id: str) -> dict[str, Any]:
        if fixture_id in self._cache:
            return self._cache[fixture_id]
        path = self.root / f"{fixture_id}.json"
        if not path.exists():
            raise ToolGatewayError(f"fixture not found: {fixture_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        self._cache[fixture_id] = data
        return data

    def resolve(self, query: str, competition_hint: str | None = None) -> dict[str, Any] | None:
        normalized = normalize(query)
        best: tuple[int, dict[str, Any]] | None = None
        for match in self.all_matches():
            aliases = [match["match_id"], *match.get("aliases", [])]
            haystacks = [normalize(alias) for alias in aliases]
            if competition_hint:
                haystacks.append(normalize(competition_hint))
            score = max(token_overlap(normalized, haystack) for haystack in haystacks)
            if score > 0 and (best is None or score > best[0]):
                best = (score, match)
        if best and best[0] >= 2:
            return best[1]
        return None


class ToolGateway:
    def __init__(
        self,
        fixture_root: Path | None = None,
        registry: SourceRegistry | None = None,
        budget: BudgetTracker | None = None,
    ) -> None:
        root = fixture_root or Path(__file__).resolve().parents[2] / "data" / "fixtures" / "matches"
        self.fixtures = FixtureRepository(root)
        self.registry = registry or SourceRegistry()
        self.budget = budget or BudgetTracker()
        self.calls: list[ToolCall] = []

    def resolve_match(
        self,
        query: str,
        date_hint: str | None = None,
        competition_hint: str | None = None,
    ) -> dict[str, Any]:
        match = self.fixtures.resolve(query, competition_hint)
        if not match:
            return self._log(
                "resolve_match",
                {"query": query, "date_hint": date_hint, "competition_hint": competition_hint},
                "miss",
                {"status": "insufficient_evidence", "ambiguities": ["match_not_found"]},
            )
        if date_hint and date_hint != match.get("date"):
            return self._log(
                "resolve_match",
                {"query": query, "date_hint": date_hint, "competition_hint": competition_hint},
                "needs_review",
                {
                    "status": "needs_human_review",
                    "match_id": match["match_id"],
                    "ambiguities": ["date_hint_differs_from_fixture"],
                },
            )
        return self._log(
            "resolve_match",
            {"query": query, "date_hint": date_hint, "competition_hint": competition_hint},
            "ok",
            {
                "status": "resolved",
                "match_id": match["match_id"],
                "resolution_confidence": "high",
                "ambiguities": [],
            },
        )

    def fetch_match_facts(self, match_id: str) -> tuple[MatchFacts, list[EvidenceItem]]:
        data = self.fixtures.load(match_id)
        evidence = [parse_evidence(item) for item in data.get("evidence", [])]
        for item in evidence:
            try:
                self.registry.validate_evidence(item)
            except SourcePolicyError as error:
                raise ToolGatewayError(str(error)) from error
        score = data["score"]
        match = MatchFacts(
            match_id=data["match_id"],
            competition=data["competition"],
            stage=data["stage"],
            date=data["date"],
            venue=data["venue"],
            home_team=data["teams"]["home"],
            away_team=data["teams"]["away"],
            score=ScoreLine(
                full_time=score["full_time"],
                after_extra_time=score.get("after_extra_time"),
                penalties=score.get("penalties"),
                winner=score.get("winner"),
            ),
            goals=[
                GoalEvent(
                    team=goal["team"],
                    player=goal["player"],
                    minute=int(goal["minute"]),
                    detail=goal["detail"],
                    evidence_id=goal["evidence_id"],
                )
                for goal in data.get("goals", [])
            ],
            key_events=data.get("key_events", []),
            source_ids=data.get("fact_source_ids", []),
        )
        self._log("fetch_match_facts", {"match_id": match_id}, "ok", {"goals": len(match.goals)})
        return match, evidence

    def fetch_team_stats(self, match_id: str) -> MetricSnapshot | None:
        data = self.fixtures.load(match_id)
        metrics_data = data.get("metrics")
        if not metrics_data:
            self._log(
                "fetch_team_stats",
                {"match_id": match_id},
                "miss",
                {"metrics": "unavailable"},
            )
            return None
        team_metrics: dict[str, dict[str, MetricValue]] = {}
        for team_name, values in metrics_data["team_metrics"].items():
            team_metrics[team_name] = {
                metric_key: MetricValue(
                    label=metric["label"],
                    value=metric["value"],
                    unit=metric.get("unit", ""),
                    evidence_id=metric["evidence_id"],
                )
                for metric_key, metric in values.items()
            }
        snapshot = MetricSnapshot(
            provider=metrics_data["provider"],
            retrieved_at=metrics_data["retrieved_at"],
            team_metrics=team_metrics,
            player_metrics=metrics_data.get("player_metrics", []),
            metric_warnings=metrics_data.get("metric_warnings", []),
            source_ids=metrics_data.get("source_ids", []),
        )
        self._log("fetch_team_stats", {"match_id": match_id}, "ok", {"provider": snapshot.provider})
        return snapshot

    def fetch_public_narratives(self, match_id: str) -> list[PublicNarrative]:
        data = self.fixtures.load(match_id)
        narratives = [
            PublicNarrative(
                narrative=sanitize_external_text(item["narrative"]),
                source_type=item["source_type"],
                evidence_id=item["evidence_id"],
                verification_status=item.get("verification_status", "narrative_only"),
            )
            for item in data.get("narratives", [])
        ]
        self._log("fetch_public_narratives", {"match_id": match_id}, "ok", {"count": len(narratives)})
        return narratives

    def fetch_media_reactions(self, match_id: str, country: str) -> list[RawMediaItem]:
        """Zwraca glosy mediow danego kraju tylko z zaufanych outletow.

        Outlet musi byc w rejestrze (MEDIA_REACTION) i miec zgodna domene; tier
        pochodzi z rejestru (zrodlo prawdy), tekst jest sanityzowany (anti-injection).
        Wpisy spoza whitelisty / z nieznanego outletu sa odrzucane (nie blokuja runu).
        """
        data = self.fixtures.load(match_id)
        items: list[RawMediaItem] = []
        dropped: list[str] = []
        for entry in data.get("media", []):
            if entry.get("country") != country:
                continue
            outlet = entry["outlet"]
            descriptor = self.registry.get(outlet)
            if descriptor is None or not descriptor.can(ProviderCapability.MEDIA_REACTION):
                dropped.append(f"{outlet}:unknown_outlet")
                continue
            original = sanitize_external_text(entry["original_text"])
            retrieved_at = entry.get("retrieved_at", "1970-01-01T00:00:00+00:00")
            candidate = EvidenceItem(
                id=entry["evidence_id"],
                claim=original[:140] or f"glos medialny: {outlet}",
                value={
                    "original": original,
                    "outlet": outlet,
                    "country": country,
                    "language": descriptor.language or entry.get("language", ""),
                },
                source_url=entry["url"],
                source_tier=descriptor.tier,
                provider=outlet,
                retrieved_at=retrieved_at,
                confidence=entry.get("confidence", "high"),
            )
            try:
                self.registry.validate_evidence(candidate)
            except SourcePolicyError as error:
                dropped.append(f"{outlet}:{error}")
                continue
            items.append(
                RawMediaItem(
                    evidence_id=entry["evidence_id"],
                    outlet=outlet,
                    country=country,
                    language=descriptor.language or entry.get("language", ""),
                    url=entry["url"],
                    original_text=original,
                    tier=descriptor.tier,
                    retrieved_at=retrieved_at,
                    translation_pl=entry.get("translation_pl"),
                    confidence=entry.get("confidence", "high"),
                )
            )
        self._log(
            "fetch_media_reactions",
            {"match_id": match_id, "country": country},
            "ok",
            {"count": len(items), "dropped": dropped},
        )
        return items

    def reset(self) -> None:
        """Czysci stan per-run (log wywolan + budzet). Budzet jest na run."""
        self.calls = []
        self.budget.reset()

    def as_dicts(self) -> list[dict[str, Any]]:
        return [call.__dict__ for call in self.calls]

    def _log(self, tool: str, args: dict[str, Any], status: str, observation: Any) -> Any:
        self.budget.charge()
        self.calls.append(
            ToolCall(tool=tool, args=args, status=status, observation=json.dumps(observation, ensure_ascii=False))
        )
        return observation


def parse_evidence(item: dict[str, Any]) -> EvidenceItem:
    return EvidenceItem(
        id=item["id"],
        claim=item["claim"],
        value=item.get("value"),
        source_url=item["source_url"],
        source_tier=SourceTier(item["source_tier"]),
        provider=item["provider"],
        retrieved_at=item["retrieved_at"],
        confidence=item.get("confidence", "high"),
    )


def normalize(value: str) -> str:
    return (
        value.lower()
        .replace("-", " ")
        .replace(":", " ")
        .replace(",", " ")
        .replace(".", " ")
        .replace("liga mistrzow", "champions league")
        .replace("lm", "champions league")
    )


def token_overlap(left: str, right: str) -> int:
    left_tokens = {token for token in left.split() if len(token) > 1}
    right_tokens = {token for token in right.split() if len(token) > 1}
    return len(left_tokens & right_tokens)

