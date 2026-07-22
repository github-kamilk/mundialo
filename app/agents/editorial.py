from __future__ import annotations

from dataclasses import dataclass

from app.schemas import (
    AngleCandidate,
    AngleScore,
    Caption,
    Carousel,
    CarouselSlide,
    EditorialBrief,
    EvidenceStore,
    InstagramPackage,
    MatchFacts,
    MatchRequest,
    MetricSnapshot,
    MetricValue,
    PackageStatus,
    PublicNarrative,
    ReelScript,
    ReelSegment,
    StoryFrame,
)
from app.tools import ToolGateway


class MatchResearcher:
    def __init__(self, gateway: ToolGateway) -> None:
        self.gateway = gateway

    def resolve(self, request: MatchRequest) -> dict:
        return self.gateway.resolve_match(
            request.match_query,
            date_hint=request.date_hint,
            competition_hint=request.competition_hint,
        )

    def fetch_facts(self, match_id: str, evidence: EvidenceStore) -> MatchFacts:
        facts, items = self.gateway.fetch_match_facts(match_id)
        evidence.add_many(items)
        return facts


class DataHunter:
    def __init__(self, gateway: ToolGateway) -> None:
        self.gateway = gateway

    def fetch_metrics(self, match_id: str) -> MetricSnapshot | None:
        return self.gateway.fetch_team_stats(match_id)


class NarrativeScout:
    def __init__(self, gateway: ToolGateway) -> None:
        self.gateway = gateway

    def fetch_narratives(self, match_id: str) -> list[PublicNarrative]:
        return self.gateway.fetch_public_narratives(match_id)


@dataclass(frozen=True)
class TacticalInsight:
    kind: str
    summary: str
    claim_ids: list[str]
    confidence: str


class MetricAnalyst:
    def analyze(
        self,
        facts: MatchFacts,
        metrics: MetricSnapshot,
        narratives: list[PublicNarrative],
    ) -> list[TacticalInsight]:
        insights: list[TacticalInsight] = []
        home = facts.home_team
        away = facts.away_team
        home_possession = metrics.metric(home, "possession")
        away_possession = metrics.metric(away, "possession")
        home_shots = metrics.metric(home, "shots")
        away_shots = metrics.metric(away, "shots")

        if facts.score.penalties:
            insights.append(
                TacticalInsight(
                    kind="penalty_final",
                    summary="Mecz rozstrzygnely karne, wiec wynik po 120 minutach nie zamyka opowieci o przebiegu finalu.",
                    claim_ids=facts.source_ids,
                    confidence="high",
                )
            )

        if home_possession and away_possession:
            diff = int(home_possession.value) - int(away_possession.value)
            if abs(diff) >= 18:
                higher_team = home if diff > 0 else away
                insights.append(
                    TacticalInsight(
                        kind="possession_vs_finish",
                        summary=f"{higher_team} mial wyrazna przewage posiadania, ale material powinien sprawdzic, czy ta przewaga byla realna kontrola.",
                        claim_ids=[home_possession.evidence_id],
                        confidence="high",
                    )
                )

        if home_shots and away_shots:
            diff = int(home_shots.value) - int(away_shots.value)
            if abs(diff) >= 6:
                higher_team = home if diff > 0 else away
                insights.append(
                    TacticalInsight(
                        kind="shot_volume",
                        summary=f"{higher_team} oddal znacznie wiecej strzalow, ale sam wolumen nie wystarcza do mocnego wniosku bez jakosci okazji.",
                        claim_ids=[home_shots.evidence_id],
                        confidence="medium",
                    )
                )

        for narrative in narratives:
            insights.append(
                TacticalInsight(
                    kind="public_narrative",
                    summary=narrative.narrative,
                    claim_ids=[narrative.evidence_id],
                    confidence="medium",
                )
            )

        return insights


class AngleEditor:
    def generate_candidates(
        self,
        facts: MatchFacts,
        metrics: MetricSnapshot,
        insights: list[TacticalInsight],
    ) -> list[AngleCandidate]:
        candidates: list[AngleCandidate] = []
        home = facts.home_team
        away = facts.away_team
        home_possession = metrics.metric(home, "possession")
        away_possession = metrics.metric(away, "possession")
        home_shots = metrics.metric(home, "shots")

        if home_possession and away_possession and facts.score.penalties:
            candidates.append(
                AngleCandidate(
                    archetype="possession_trap",
                    thesis=(
                        f"{home} mial przewage z pilka, ale final i tak musial przejsc przez karne."
                    ),
                    tension=(
                        "Przewaga w posiadaniu kontra brak zamkniecia meczu przed seria jedenastek."
                    ),
                    main_number=MetricValue(
                        label=f"{home}: posiadanie pilki",
                        value=f"{home_possession.value}{home_possession.unit}",
                        unit=home_possession.unit,
                        evidence_id=home_possession.evidence_id,
                    ),
                    supporting_claim_ids=[
                        *facts.source_ids,
                        home_possession.evidence_id,
                    ],
                    score=AngleScore(
                        surprise=2,
                        simplicity=2,
                        emotion=2,
                        evidence_strength=2,
                        comment_potential=2,
                    ),
                    risk="low",
                )
            )

        if home_shots and facts.score.penalties:
            candidates.append(
                AngleCandidate(
                    archetype="one_number",
                    thesis=(
                        f"{home} oddal {home_shots.value} strzalow, ale final rozstrzygnely dopiero karne."
                    ),
                    tension="Wolumen strzalow kontra brak rozstrzygniecia w grze.",
                    main_number=MetricValue(
                        label=f"{home}: strzaly",
                        value=home_shots.value,
                        unit=home_shots.unit,
                        evidence_id=home_shots.evidence_id,
                    ),
                    supporting_claim_ids=[
                        *facts.source_ids,
                        home_shots.evidence_id,
                    ],
                    score=AngleScore(
                        surprise=1,
                        simplicity=2,
                        emotion=2,
                        evidence_strength=2,
                        comment_potential=1,
                    ),
                    risk="medium",
                )
            )

        if not candidates:
            fallback_metric = self._first_available_metric(metrics)
            if fallback_metric:
                candidates.append(
                    AngleCandidate(
                        archetype="three_numbers",
                        thesis="Ten mecz najlepiej opisac trzema podstawowymi liczbami, bez udawania mocniejszego angle'u.",
                        tension="Brak jednej bardzo mocnej anomalii.",
                        main_number=fallback_metric,
                        supporting_claim_ids=[fallback_metric.evidence_id, *facts.source_ids],
                        score=AngleScore(
                            surprise=0,
                            simplicity=2,
                            emotion=1,
                            evidence_strength=1,
                            comment_potential=1,
                        ),
                        risk="low",
                    )
                )
        return candidates

    def select(self, candidates: list[AngleCandidate]) -> AngleCandidate:
        if not candidates:
            raise ValueError("no angle candidates")
        return sorted(candidates, key=lambda candidate: candidate.score.total, reverse=True)[0]

    def create_brief(self, selected: AngleCandidate, metrics: MetricSnapshot) -> EditorialBrief:
        forbidden = []
        if any("xG unavailable" in warning for warning in metrics.metric_warnings):
            forbidden.append("Nie uzywaj xG ani wnioskow opartych na jakosci okazji.")
        if any("PPDA unavailable" in warning for warning in metrics.metric_warnings):
            forbidden.append("Nie uzywaj PPDA ani angle'u o pressingu.")
        return EditorialBrief(
            selected_angle=selected,
            one_sentence_thesis=f"Wynik mowi, ze final byl rowny, ale dane pokazuja, ze {selected.thesis.lower()}",
            allowed_claim_ids=sorted(set([selected.main_number.evidence_id, *selected.supporting_claim_ids])),
            forbidden_claims=forbidden,
            tone="prosty, szybki, bez zargonu",
            cta_goal="comments",
            visual_options=["three_metric_table", "score_timeline"],
        )

    def _first_available_metric(self, metrics: MetricSnapshot) -> MetricValue | None:
        for team_metrics in metrics.team_metrics.values():
            for metric in team_metrics.values():
                return metric
        return None


class Copywriter:
    def create_package(
        self,
        package_id: str,
        facts: MatchFacts,
        metrics: MetricSnapshot,
        brief: EditorialBrief,
        evidence: EvidenceStore,
    ) -> InstagramPackage:
        selected = brief.selected_angle
        home = facts.home_team
        away = facts.away_team
        home_short = short_team(home)
        score_claim_ids = facts.source_ids
        first_goal = facts.goals[0] if facts.goals else None
        second_goal = facts.goals[1] if len(facts.goals) > 1 else None
        goal_claim_ids = [goal.evidence_id for goal in facts.goals[:2]]

        hook = f"Ten final wygladal jak kontrola {home_short}. Dane sa mniej wygodne."
        cta = f"Dla Ciebie to byla kontrola {home_short} czy final uratowany dopiero karnymi?"
        voiceover = [
            ReelSegment(
                time_range="0-3s",
                text=hook,
                claim_ids=[],
            ),
            ReelSegment(
                time_range="3-10s",
                text=(
                    f"{home} i {away} skonczyli po dogrywce wynikiem {facts.score.after_extra_time or facts.score.full_time}, "
                    f"a zwyciezce wybraly karne."
                ),
                claim_ids=score_claim_ids,
            ),
            ReelSegment(
                time_range="10-25s",
                text=(
                    f"Najwazniejsza liczba: {home} mial {selected.main_number.value} posiadania pilki."
                ),
                claim_ids=[selected.main_number.evidence_id],
            ),
            ReelSegment(
                time_range="25-45s",
                text=self._interpretation_text(home, facts, first_goal, second_goal),
                claim_ids=[*goal_claim_ids, *score_claim_ids],
            ),
            ReelSegment(
                time_range="45-60s",
                text=cta,
                claim_ids=[],
            ),
        ]

        slides = [
            CarouselSlide(
                slide_number=1,
                role="hook",
                headline=f"{home_short} mialo pilke. Rywal mial final na granicy.",
                body="Ten mecz nie jest tak prosty, jak brzmi: posiadanie kontra karne.",
                claim_ids=[],
                visual_brief="Duzy tytul, po bokach herby/nazwy druzyn, bez tabeli.",
            ),
            CarouselSlide(
                slide_number=2,
                role="context",
                headline=f"{facts.score.after_extra_time or facts.score.full_time} po 120 minutach",
                body=f"{facts.score.winner} wygral dopiero w karnych {facts.score.penalties}.",
                claim_ids=score_claim_ids,
                visual_brief="Osiowo: wynik po dogrywce i wynik karnych.",
            ),
            CarouselSlide(
                slide_number=3,
                role="number",
                headline=f"Liczba meczu: {selected.main_number.value}",
                body=f"Tyle posiadania mial {home}. To brzmi jak kontrola.",
                claim_ids=[selected.main_number.evidence_id],
                visual_brief="Progress bar 61/39.",
            ),
            CarouselSlide(
                slide_number=4,
                role="chart",
                headline="Przewaga byla widoczna, ale nie zamknela meczu",
                body=self._metric_table_text(metrics, home, away),
                claim_ids=["e_possession", "e_shots", "e_shots_on_target"],
                visual_brief="Mini tabela: posiadanie, strzaly, strzaly celne.",
            ),
            CarouselSlide(
                slide_number=5,
                role="interpretation",
                headline="To nie jest historia o samej dominacji",
                body=self._carousel_interpretation(home_short, away, facts, first_goal),
                claim_ids=[*(goal_claim_ids[:1]), *score_claim_ids],
                visual_brief="Timeline: 6' Arsenal, 64' PSG, karne.",
            ),
            CarouselSlide(
                slide_number=6,
                role="hero_problem",
                headline=f"Problem {home_short}: przewaga bez nokautu",
                body=(
                    "Przy takim posiadaniu i tylu strzalach oczekujesz zamkniecia meczu. Tutaj final nadal wisial na jednej serii."
                ),
                claim_ids=[selected.main_number.evidence_id, "e_shots", *score_claim_ids],
                visual_brief="Kontrast: kontrola pilki vs jedenastki.",
            ),
            CarouselSlide(
                slide_number=7,
                role="cta",
                headline="Wynik oddaje przebieg?",
                body=f"Czy to byla kontrola {home_short}, czy rywal skutecznie sprowadzil final do karnych?",
                claim_ids=[],
                visual_brief="Pytanie na calej planszy.",
            ),
        ]

        stories = [
            StoryFrame(
                frame_number=1,
                kind="poll",
                text=f"Czy {home_short} kontrolowalo ten final?",
                claim_ids=[],
            ),
            StoryFrame(
                frame_number=2,
                kind="quiz",
                text=f"Kto mial wieksze posiadanie? {home}: {selected.main_number.value}.",
                claim_ids=[selected.main_number.evidence_id],
            ),
            StoryFrame(
                frame_number=3,
                kind="question",
                text="Ktory final wziac pod dane nastepnym razem?",
                claim_ids=[],
            ),
        ]

        caption = Caption(
            text=(
                f"{home} wygral final, ale sama historia wyniku nie wystarcza. "
                f"{selected.main_number.value} posiadania, wiecej strzalow i dopiero karne: "
                "to jest material o przewadze, ktora nie zamknela meczu."
            ),
            hashtags=["#pilkawliczbach", "#championsleague", "#psg", "#arsenal", "#nietylkowynik"],
            source_note=f"Zrodla danych: {metrics.provider}, OfflineVerifiedFixture. Fixture lokalny MVP.",
            claim_ids=[*score_claim_ids, selected.main_number.evidence_id, "e_shots"],
        )

        package = InstagramPackage(
            package_id=package_id,
            match=facts,
            editorial_angle=selected,
            reel_script=ReelScript(
                hook=hook,
                voiceover=voiceover,
                on_screen_text=[
                    "Wynik: 1-1 po dogrywce",
                    f"Liczba meczu: {selected.main_number.value} posiadania {home_short}",
                    "Przewaga z pilka != automatyczna kontrola",
                ],
                cta=cta,
            ),
            carousel=Carousel(slides=slides),
            stories=stories,
            caption=caption,
            visual_brief={
                "safe_assets": ["wlasne plansze", "tabele", "timeline"],
                "recommended_chart": "three_metric_table",
                "notes": ["nie dodawaj nowych metryk poza evidence ledgerem"],
            },
            sources=evidence.ledger(),
            status=PackageStatus.READY,
        )
        evidence.mark_used(package.all_claim_ids())
        return package

    def _interpretation_text(
        self,
        home: str,
        facts: MatchFacts,
        first_goal: object | None,
        second_goal: object | None,
    ) -> str:
        if first_goal and second_goal:
            return (
                "To pokazuje przewage z pilka, ale nie pelna kontrole. "
                f"{first_goal.team} prowadzil od {first_goal.minute}. minuty, "
                f"{second_goal.team} wyrownal w {second_goal.minute}. minucie "
                "i finalu nie zamknieto przed jedenastkami."
            )
        return (
            "To pokazuje przewage z pilka, ale nie pelna kontrole. "
            f"{home} mial inicjatywe, a final nadal doszedl do serii jedenastek."
        )

    def _carousel_interpretation(
        self,
        home_short: str,
        away: str,
        facts: MatchFacts,
        first_goal: object | None,
    ) -> str:
        if first_goal:
            return (
                f"{home_short} mialo inicjatywe, ale {away} utrzymal final przy zyciu "
                f"od gola w {first_goal.minute}. minucie az do serii karnych."
            )
        return f"{home_short} mialo inicjatywe, ale final nadal dotrwal do serii karnych."

    def _metric_table_text(self, metrics: MetricSnapshot, home: str, away: str) -> str:
        home_possession = metrics.metric(home, "possession")
        away_possession = metrics.metric(away, "possession")
        home_shots = metrics.metric(home, "shots")
        away_shots = metrics.metric(away, "shots")
        home_sot = metrics.metric(home, "shots_on_target")
        away_sot = metrics.metric(away, "shots_on_target")
        return (
            f"Posiadanie: {home_possession.value}-{away_possession.value}%. "
            f"Strzaly: {home_shots.value}-{away_shots.value}. "
            f"Celne: {home_sot.value}-{away_sot.value}."
        )


def short_team(team_name: str) -> str:
    known = {
        "Paris Saint-Germain": "PSG",
    }
    return known.get(team_name, team_name)
