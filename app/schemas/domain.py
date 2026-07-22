from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any


class SourceTier(str, Enum):
    A = "A"
    B = "B"
    C = "C"


class PackageStatus(str, Enum):
    READY = "ready"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


def to_plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: to_plain(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: to_plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    return value


def require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} cannot be empty")


@dataclass(frozen=True)
class MatchRequest:
    match_query: str
    date_hint: str | None = None
    competition_hint: str | None = None
    post_type: str = "media_reaction"
    # wynik zweryfikowany RECZNIE przez operatora ('X-Y'). Furtka na mecze, gdzie
    # zewnetrzne zrodla sa nieosiagalne (FIFA JS-wall, prasa pisze slownie 'empate'
    # bez cyfr) - wstrzykuje wynik jako evidence o niskim zaufaniu i WYMUSZA
    # needs_human_review (czlowiek ostatecznie zatwierdza posta).
    score_override: str | None = None

    def validate(self) -> None:
        require_non_empty(self.match_query, "match_query")
        if self.post_type not in {"media_reaction", "data_story"}:
            raise ValueError("post_type must be media_reaction or data_story")
        if self.score_override is not None and not re.fullmatch(r"\d{1,2}-\d{1,2}", self.score_override):
            raise ValueError("score_override musi wygladac jak 'X-Y' (np. '1-1')")


@dataclass
class EvidenceItem:
    id: str
    claim: str
    value: Any
    source_url: str
    source_tier: SourceTier
    provider: str
    retrieved_at: str
    confidence: str = "high"
    used_in_output: bool = False

    def validate(self) -> None:
        require_non_empty(self.id, "evidence.id")
        require_non_empty(self.claim, "evidence.claim")
        require_non_empty(self.provider, "evidence.provider")
        require_non_empty(self.source_url, "evidence.source_url")
        if self.confidence not in {"high", "medium", "low"}:
            raise ValueError("evidence.confidence must be high, medium or low")


@dataclass
class EvidenceStore:
    items: dict[str, EvidenceItem] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)

    def add(self, item: EvidenceItem) -> None:
        item.validate()
        if item.id in self.items and self.items[item.id].value != item.value:
            self.conflicts.append(item.id)
        self.items[item.id] = item

    def add_many(self, items: list[EvidenceItem]) -> None:
        for item in items:
            self.add(item)

    def get(self, evidence_id: str) -> EvidenceItem | None:
        return self.items.get(evidence_id)

    def mark_used(self, evidence_ids: list[str]) -> None:
        for evidence_id in evidence_ids:
            item = self.items.get(evidence_id)
            if item:
                item.used_in_output = True

    def missing(self, evidence_ids: list[str]) -> list[str]:
        return sorted({evidence_id for evidence_id in evidence_ids if evidence_id not in self.items})

    def ledger(self) -> list[EvidenceItem]:
        return sorted(self.items.values(), key=lambda item: item.id)


@dataclass(frozen=True)
class ScoreLine:
    full_time: str
    after_extra_time: str | None = None
    penalties: str | None = None
    winner: str | None = None


@dataclass(frozen=True)
class GoalEvent:
    team: str
    player: str
    minute: int
    detail: str
    evidence_id: str


@dataclass(frozen=True)
class MatchFacts:
    match_id: str
    competition: str
    stage: str
    date: str
    venue: str
    home_team: str
    away_team: str
    score: ScoreLine
    goals: list[GoalEvent]
    key_events: list[dict[str, Any]]
    source_ids: list[str]
    status: str = "resolved"
    resolution_confidence: str = "high"
    ambiguities: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MetricValue:
    label: str
    value: Any
    unit: str
    evidence_id: str


@dataclass(frozen=True)
class MetricSnapshot:
    provider: str
    retrieved_at: str
    team_metrics: dict[str, dict[str, MetricValue]]
    player_metrics: list[dict[str, Any]]
    metric_warnings: list[str]
    source_ids: list[str]

    def metric(self, team: str, metric_key: str) -> MetricValue | None:
        return self.team_metrics.get(team, {}).get(metric_key)


@dataclass(frozen=True)
class PublicNarrative:
    narrative: str
    source_type: str
    evidence_id: str
    verification_status: str = "narrative_only"


@dataclass(frozen=True)
class AngleScore:
    surprise: int
    simplicity: int
    emotion: int
    evidence_strength: int
    comment_potential: int

    @property
    def total(self) -> int:
        return (
            self.surprise
            + self.simplicity
            + self.emotion
            + self.evidence_strength
            + self.comment_potential
        )


@dataclass(frozen=True)
class AngleCandidate:
    archetype: str
    thesis: str
    tension: str
    main_number: MetricValue
    supporting_claim_ids: list[str]
    score: AngleScore
    risk: str = "low"
    missing_evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EditorialBrief:
    selected_angle: AngleCandidate
    one_sentence_thesis: str
    allowed_claim_ids: list[str]
    forbidden_claims: list[str]
    tone: str
    cta_goal: str
    visual_options: list[str]


@dataclass(frozen=True)
class ReelSegment:
    time_range: str
    text: str
    claim_ids: list[str]


@dataclass(frozen=True)
class ReelScript:
    hook: str
    voiceover: list[ReelSegment]
    on_screen_text: list[str]
    cta: str


@dataclass(frozen=True)
class CarouselSlide:
    slide_number: int
    role: str
    headline: str
    body: str
    claim_ids: list[str]
    visual_brief: str
    # Byline atrybucji (slajd tytulowy): sama nazwa dziennika pod tytulem - osobny, spojny
    # element zamiast nazwy redakcji wcisnietej w naglowek. Domyslnie None (pozostale slajdy go nie maja).
    attribution: str | None = None


@dataclass(frozen=True)
class Carousel:
    slides: list[CarouselSlide]


@dataclass(frozen=True)
class StoryFrame:
    frame_number: int
    kind: str
    text: str
    claim_ids: list[str]


@dataclass(frozen=True)
class Caption:
    text: str
    hashtags: list[str]
    source_note: str
    claim_ids: list[str]


@dataclass(frozen=True)
class InstagramPackage:
    package_id: str
    match: MatchFacts
    editorial_angle: AngleCandidate
    reel_script: ReelScript
    carousel: Carousel
    stories: list[StoryFrame]
    caption: Caption
    visual_brief: dict[str, Any]
    sources: list[EvidenceItem]
    status: PackageStatus

    def all_claim_ids(self) -> list[str]:
        claim_ids: list[str] = []
        for segment in self.reel_script.voiceover:
            claim_ids.extend(segment.claim_ids)
        for slide in self.carousel.slides:
            claim_ids.extend(slide.claim_ids)
        for story in self.stories:
            claim_ids.extend(story.claim_ids)
        claim_ids.extend(self.caption.claim_ids)
        claim_ids.extend(self.editorial_angle.supporting_claim_ids)
        claim_ids.append(self.editorial_angle.main_number.evidence_id)
        return sorted(set(claim_ids))


@dataclass(frozen=True)
class MediaQuote:
    """Jeden zacytowany glos medialny. Na slajd idzie summary_pl (streszczenie
    artykulu z wplecionym cytatem, >=5 zdan) albo - gdy brak - translation_pl;
    original_text zostaje w EvidenceStore (audyt, prawo cytatu, weryfikacja)."""

    outlet: str
    country: str
    language: str
    original_text: str
    translation_pl: str
    url: str
    tier: SourceTier
    retrieved_at: str
    evidence_id: str
    confidence: str = "high"
    summary_pl: str | None = None


@dataclass(frozen=True)
class CountryMediaPanel:
    country: str
    language: str
    quotes: list[MediaQuote]
    mood_summary: str | None = None
    source_count: int = 0


@dataclass(frozen=True)
class MediaReactionPackage:
    package_id: str
    match: MatchFacts
    title_slide: CarouselSlide
    panels: list[CountryMediaPanel]
    carousel: Carousel
    caption: Caption
    sources: list[EvidenceItem]
    status: PackageStatus

    def quote_evidence_ids(self) -> list[str]:
        ids: list[str] = []
        for panel in self.panels:
            ids.extend(quote.evidence_id for quote in panel.quotes)
        return ids

    def all_claim_ids(self) -> list[str]:
        claim_ids: list[str] = list(self.quote_evidence_ids())
        for slide in self.carousel.slides:
            claim_ids.extend(slide.claim_ids)
        claim_ids.extend(self.caption.claim_ids)
        return sorted(set(claim_ids))

    def countries(self) -> list[str]:
        return [panel.country for panel in self.panels]


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    result: str
    details: str = ""


@dataclass(frozen=True)
class ValidationReport:
    status: str
    checks: list[ValidationCheck]
    blocking_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "pass" and not self.blocking_issues


@dataclass(frozen=True)
class WorkflowRun:
    run_id: str
    request: MatchRequest
    package: InstagramPackage | None
    fact_check: ValidationReport
    quality_report: ValidationReport
    tool_calls: list[dict[str, Any]]
    evidence: list[EvidenceItem]
    status: PackageStatus
    notes: list[str] = field(default_factory=list)
    media_package: MediaReactionPackage | None = None
    # Epizod runu (pamiec epizodyczna, etap 1): maszynowy zapis zdarzen operacyjnych
    # (fetch/sekcje/search) jako plaski dict - patrz architektura-pamiec-epizodyczna.md.
    # Dict zamiast typu z app.memory, zeby schemas pozostalo warstwa bazowa bez cyklu.
    episode: dict[str, Any] | None = None

