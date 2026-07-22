from __future__ import annotations

import re

from app.memory import DEFAULT_VOICE_PROFILE, banned_hits, mentioned_scores
from app.schemas import (
    EvidenceStore,
    InstagramPackage,
    MediaReactionPackage,
    PackageStatus,
    ValidationCheck,
    ValidationReport,
)

FAIR_USE_QUOTE_LEN = 420


class FactChecker:
    def validate(self, package: InstagramPackage | None, evidence: EvidenceStore) -> ValidationReport:
        checks: list[ValidationCheck] = []
        blocking: list[str] = []
        warnings: list[str] = []

        if package is None:
            return ValidationReport(
                status="needs_human_review",
                checks=[ValidationCheck("package_exists", "fail", "No package generated.")],
                blocking_issues=["package_missing"],
            )

        claim_ids = package.all_claim_ids()
        missing = evidence.missing(claim_ids)
        if missing:
            checks.append(
                ValidationCheck(
                    name="all_claims_have_evidence",
                    result="fail",
                    details=", ".join(missing),
                )
            )
            blocking.append("missing_evidence")
        else:
            checks.append(
                ValidationCheck(
                    name="all_claims_have_evidence",
                    result="pass",
                    details=f"{len(claim_ids)} claim ids checked.",
                )
            )

        if evidence.conflicts:
            checks.append(
                ValidationCheck(
                    name="no_source_conflicts",
                    result="fail",
                    details=", ".join(evidence.conflicts),
                )
            )
            blocking.append("source_conflicts")
        else:
            checks.append(ValidationCheck("no_source_conflicts", "pass"))

        low_confidence = [
            item.id
            for item in evidence.ledger()
            if item.id in claim_ids and item.confidence == "low"
        ]
        if low_confidence:
            checks.append(
                ValidationCheck(
                    name="no_low_confidence_claims",
                    result="fail",
                    details=", ".join(low_confidence),
                )
            )
            blocking.append("low_confidence_claims")
        else:
            checks.append(ValidationCheck("no_low_confidence_claims", "pass"))

        if any("xG" in text for text in package_texts(package)):
            warnings.append("Output mentions xG; verify source availability.")

        status = "pass" if not blocking else "needs_human_review"
        return ValidationReport(status=status, checks=checks, blocking_issues=blocking, warnings=warnings)


class QualityJudge:
    def __init__(self, banned_phrases: list[str] | None = None) -> None:
        self.banned_phrases = (
            banned_phrases
            if banned_phrases is not None
            else DEFAULT_VOICE_PROFILE.banned_phrases
        )

    def validate(self, package: InstagramPackage | None) -> ValidationReport:
        if package is None:
            return ValidationReport(
                status="needs_human_review",
                checks=[ValidationCheck("package_exists", "fail")],
                blocking_issues=["package_missing"],
            )

        checks: list[ValidationCheck] = []
        blocking: list[str] = []
        warnings: list[str] = []

        self._check(
            checks,
            blocking,
            name="single_angle",
            condition=bool(package.editorial_angle.thesis),
            details=package.editorial_angle.thesis,
        )
        self._check(
            checks,
            blocking,
            name="hook_first_three_seconds",
            condition=bool(package.reel_script.hook)
            and bool(package.reel_script.voiceover)
            and package.reel_script.voiceover[0].time_range.startswith("0-3"),
        )
        self._check(
            checks,
            blocking,
            name="carousel_has_5_to_7_slides",
            condition=5 <= len(package.carousel.slides) <= 7,
            details=str(len(package.carousel.slides)),
        )
        self._check(
            checks,
            blocking,
            name="slide_one_is_tension",
            condition=package.carousel.slides[0].role == "hook"
            and "?" not in package.carousel.slides[0].headline[:3],
        )
        self._check(
            checks,
            blocking,
            name="main_number_present",
            condition=bool(package.editorial_angle.main_number.evidence_id),
        )
        self._check(
            checks,
            blocking,
            name="cta_present",
            condition=bool(package.reel_script.cta)
            and package.carousel.slides[-1].role == "cta",
        )
        self._check(
            checks,
            blocking,
            name="source_note_present",
            condition=bool(package.caption.source_note),
        )
        self._check(
            checks,
            blocking,
            name="no_broadcast_clips",
            condition="fragment transmisji" not in " ".join(package.visual_brief.get("safe_assets", [])).lower(),
            details="visual brief lists safe asset types only",
        )
        banned_hits = find_banned_phrases(package, self.banned_phrases)
        self._check(
            checks,
            blocking,
            name="no_banned_phrases",
            condition=not banned_hits,
            details=", ".join(banned_hits),
        )
        if any(len(slide.headline) > 80 for slide in package.carousel.slides):
            warnings.append("Some slide headlines may be too long for mobile.")

        status = "pass" if not blocking else "needs_human_review"
        return ValidationReport(status=status, checks=checks, blocking_issues=blocking, warnings=warnings)

    def _check(
        self,
        checks: list[ValidationCheck],
        blocking: list[str],
        name: str,
        condition: bool,
        details: str = "",
    ) -> None:
        checks.append(ValidationCheck(name=name, result="pass" if condition else "fail", details=details))
        if not condition:
            blocking.append(name)


def package_texts(package: InstagramPackage) -> list[str]:
    texts: list[str] = [
        package.reel_script.hook,
        package.reel_script.cta,
        package.caption.text,
        package.caption.source_note,
    ]
    texts.extend(segment.text for segment in package.reel_script.voiceover)
    texts.extend(slide.headline for slide in package.carousel.slides)
    texts.extend(slide.body for slide in package.carousel.slides)
    texts.extend(story.text for story in package.stories)
    return texts


def find_banned_in_texts(texts: list[str], banned_phrases: list[str]) -> list[str]:
    """Deterministyczny anti-slop linter (kontrakt z glos-redakcji.md, sek. 5).

    Dopasowanie po granicach slow (banned_hits) - 'wow' nie moze lapac sie
    w srodku 'Lewandowowi'.
    """
    return banned_hits(" ".join(texts), banned_phrases)


def find_banned_phrases(package: InstagramPackage, banned_phrases: list[str]) -> list[str]:
    return find_banned_in_texts(package_texts(package), banned_phrases)


def media_texts(package: MediaReactionPackage) -> list[str]:
    """Tekst polski widoczny dla odbiorcy (do anti-slop). Oryginaly obce pomijamy."""
    texts: list[str] = [
        package.title_slide.headline,
        package.title_slide.body,
        package.caption.text,
        package.caption.source_note,
    ]
    for panel in package.panels:
        if panel.mood_summary:
            texts.append(panel.mood_summary)
        texts.extend(quote.translation_pl for quote in panel.quotes)
    for slide in package.carousel.slides:
        if slide.role == "media_country":
            texts.append(slide.headline)
            texts.append(slide.body)
    return texts


class MediaFactChecker:
    """Integralnosc dowodow toru medialnego: kazdy cytat ma zrodlo i oryginal."""

    def validate(
        self, package: MediaReactionPackage | None, evidence: EvidenceStore
    ) -> ValidationReport:
        if package is None:
            return ValidationReport(
                status="needs_human_review",
                checks=[ValidationCheck("package_exists", "fail", "No media package.")],
                blocking_issues=["package_missing"],
            )

        checks: list[ValidationCheck] = []
        blocking: list[str] = []

        missing = evidence.missing(package.all_claim_ids())
        _record(checks, blocking, "all_claims_have_evidence", not missing, ", ".join(missing))
        _record(
            checks,
            blocking,
            "no_source_conflicts",
            not evidence.conflicts,
            ", ".join(evidence.conflicts),
        )

        missing_originals: list[str] = []
        for quote in (q for panel in package.panels for q in panel.quotes):
            item = evidence.get(quote.evidence_id)
            original = item.value.get("original") if item and isinstance(item.value, dict) else None
            if not original:
                missing_originals.append(quote.evidence_id)
        _record(
            checks,
            blocking,
            "original_retained_in_evidence",
            not missing_originals,
            ", ".join(missing_originals),
        )

        mismatch = _score_mismatch_details(package)
        _record(checks, blocking, "score_consistent_with_media", mismatch is None, mismatch or "")

        status = "pass" if not blocking else "needs_human_review"
        return ValidationReport(status=status, checks=checks, blocking_issues=blocking)


# Wzorzec wyniku w tekscie: 1-2, 2:1; lookaround odcina fragmenty dat (2026-06-11).
# Sluzy do PARSOWANIA wyniku z faktow (fullmatch); skanowanie tekstu paneli (z
# pominieciem zdan o wyniku do przerwy) robi wspolny mentioned_scores z app.memory.
_SCORE_MENTION_RE = re.compile(r"(?<![\d-])(\d{1,2})\s*[-:]\s*(\d{1,2})(?![\d-])")


def _score_mismatch_details(package: MediaReactionPackage) -> str | None:
    """Cross-check: wynik z faktow vs wyniki wzmiankowane w cytatach/streszczeniach.

    Bezpiecznik na nieaktualne fakty (np. stary fixture przy meczu rozegranym
    naprawde): jezeli media wymieniaja jakies wyniki, a wsrod nich NIE MA wyniku
    z faktow (w zadnej orientacji), material idzie do czlowieka. Brak wzmianek
    o wyniku = brak sygnalu = pass. Wyniki DO PRZERWY sa pomijane (mentioned_scores)
    - to nie sa konkurencyjne wyniki koncowe.
    """
    facts_score = package.match.score.full_time
    match = _SCORE_MENTION_RE.fullmatch(facts_score.strip())
    if match is None:
        return None
    facts_pair = (match.group(1), match.group(2))

    mentioned: set[tuple[str, str]] = set()
    for panel in package.panels:
        for quote in panel.quotes:
            for text in (quote.original_text, quote.translation_pl, quote.summary_pl or ""):
                mentioned |= mentioned_scores(text)
    if not mentioned:
        return None
    if facts_pair in mentioned or (facts_pair[1], facts_pair[0]) in mentioned:
        return None
    scores = ", ".join(f"{a}-{b}" for a, b in sorted(mentioned))
    return (
        f"fakty mowia {facts_score}, ale media wymieniaja inne wyniki: {scores}; "
        "zweryfikuj wynik meczu"
    )


def _summaries_repeating_score(package: MediaReactionPackage) -> list[str]:
    """Evidence_id streszczen, ktore powtarzaja KONCOWY wynik meczu.

    Wynik mieszka na slajdzie tytulowym; powtarzanie go w kazdym streszczeniu to
    wata (kontrakt z glos-redakcji.md, sek. 13). Cytaty doslowne (translation_pl)
    sa wylaczone - to slowa outletu, nie nasze. Inne wyniki ('1-0 do przerwy')
    nie sa lapane: mentioned_scores pomija zdania o wyniku do przerwy, wiec nawet
    gdy wynik koncowy jest liczbowo rowny czastkowemu, legalna wzmianka przechodzi.
    """
    match = _SCORE_MENTION_RE.fullmatch(package.match.score.full_time.strip())
    if match is None:
        return []
    pair = (match.group(1), match.group(2))
    reversed_pair = (pair[1], pair[0])
    repeated: list[str] = []
    for panel in package.panels:
        for quote in panel.quotes:
            if not quote.summary_pl:
                continue
            mentioned = mentioned_scores(quote.summary_pl)
            if pair in mentioned or reversed_pair in mentioned:
                repeated.append(quote.evidence_id)
    return repeated


class MediaQualityJudge:
    """Kontrakt formatu karuzeli medialnej (kuracja, atrybucja, anti-slop)."""

    def __init__(self, banned_phrases: list[str] | None = None) -> None:
        self.banned_phrases = (
            banned_phrases
            if banned_phrases is not None
            else DEFAULT_VOICE_PROFILE.banned_phrases
        )

    def validate(self, package: MediaReactionPackage | None) -> ValidationReport:
        if package is None:
            return ValidationReport(
                status="needs_human_review",
                checks=[ValidationCheck("package_exists", "fail")],
                blocking_issues=["package_missing"],
            )

        checks: list[ValidationCheck] = []
        blocking: list[str] = []

        _record(
            checks,
            blocking,
            "both_countries_present",
            len(package.panels) >= 2 and all(panel.quotes for panel in package.panels),
            details=", ".join(f"{p.country}:{len(p.quotes)}" for p in package.panels),
        )

        quotes = [quote for panel in package.panels for quote in panel.quotes]
        _record(
            checks,
            blocking,
            "every_quote_has_outlet_and_url",
            bool(quotes) and all(quote.outlet and quote.url for quote in quotes),
        )
        long_quotes = [q.evidence_id for q in quotes if len(q.translation_pl) > FAIR_USE_QUOTE_LEN]
        _record(
            checks,
            blocking,
            "quote_within_fair_use_length",
            not long_quotes,
            ", ".join(long_quotes),
        )
        bad_mood = [
            panel.country
            for panel in package.panels
            if panel.mood_summary and panel.source_count < 2
        ]
        _record(checks, blocking, "mood_requires_two_sources", not bad_mood, ", ".join(bad_mood))
        repeated_score = _summaries_repeating_score(package)
        _record(
            checks,
            blocking,
            "score_only_on_title_slide",
            not repeated_score,
            ", ".join(repeated_score),
        )
        _record(
            checks,
            blocking,
            "title_slide_present",
            bool(package.carousel.slides) and package.carousel.slides[0].role == "title",
        )
        _record(
            checks,
            blocking,
            "sources_slide_present",
            bool(package.carousel.slides) and package.carousel.slides[-1].role == "sources",
        )
        banned_hits = find_banned_in_texts(media_texts(package), self.banned_phrases)
        _record(checks, blocking, "no_banned_phrases", not banned_hits, ", ".join(banned_hits))

        status = "pass" if not blocking else "needs_human_review"
        return ValidationReport(status=status, checks=checks, blocking_issues=blocking)


def _record(
    checks: list[ValidationCheck],
    blocking: list[str],
    name: str,
    condition: bool,
    details: str = "",
) -> None:
    checks.append(ValidationCheck(name=name, result="pass" if condition else "fail", details=details))
    if not condition:
        blocking.append(name)


def final_status(fact_check: ValidationReport, quality_report: ValidationReport) -> PackageStatus:
    if fact_check.passed and quality_report.passed:
        return PackageStatus.READY
    return PackageStatus.NEEDS_HUMAN_REVIEW
