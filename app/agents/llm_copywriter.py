"""Copywriter oparty na LLM - z guardrailami evidence i anti-slop.

Model dostaje brief, dozwolone evidence i glos (few-shot), a zwraca structured
output (`CopyDraft`). Kazdy draft jest walidowany: claim_ids musza istniec w
EvidenceStore (zero halucynacji zrodel), brak banned phrases (anti-slop), struktura
zgodna z kontraktem puszki. Przy bledzie harness ponawia z feedbackiem; po porazce
koordynator robi fallback na deterministyczny copywriter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.memory import DEFAULT_VOICE_PROFILE, VoiceProfile, banned_hits, fold_ascii
from app.models.structured import ModelGateway, generate_structured
from app.schemas import (
    Caption,
    Carousel,
    CarouselSlide,
    EditorialBrief,
    EvidenceStore,
    InstagramPackage,
    MatchFacts,
    MetricSnapshot,
    PackageStatus,
    ReelScript,
    ReelSegment,
    StoryFrame,
)


@dataclass(frozen=True)
class CopyDraft:
    hook: str
    voiceover: list[ReelSegment]
    on_screen_text: list[str]
    cta: str
    slides: list[CarouselSlide]
    stories: list[StoryFrame]
    caption: Caption

    def all_claim_ids(self) -> list[str]:
        ids: list[str] = []
        for segment in self.voiceover:
            ids.extend(segment.claim_ids)
        for slide in self.slides:
            ids.extend(slide.claim_ids)
        for story in self.stories:
            ids.extend(story.claim_ids)
        ids.extend(self.caption.claim_ids)
        return ids

    def texts(self) -> list[str]:
        texts = [self.hook, self.cta, self.caption.text, self.caption.source_note]
        texts.extend(segment.text for segment in self.voiceover)
        texts.extend(slide.headline for slide in self.slides)
        texts.extend(slide.body for slide in self.slides)
        texts.extend(story.text for story in self.stories)
        return texts


def build_copy_draft(
    data: dict[str, Any],
    allowed_evidence_ids: set[str],
    banned_phrases: list[str],
) -> CopyDraft:
    """Parsuje slownik LLM w CopyDraft i waliduje. Rzuca ValueError z czytelnym feedbackiem."""
    try:
        draft = CopyDraft(
            hook=str(data["hook"]).strip(),
            voiceover=[
                ReelSegment(
                    time_range=str(seg["time_range"]),
                    text=str(seg["text"]).strip(),
                    claim_ids=list(seg.get("claim_ids", [])),
                )
                for seg in data["voiceover"]
            ],
            on_screen_text=[str(item) for item in data.get("on_screen_text", [])],
            cta=str(data["cta"]).strip(),
            slides=[
                CarouselSlide(
                    slide_number=int(slide["slide_number"]),
                    role=str(slide["role"]),
                    headline=str(slide["headline"]).strip(),
                    body=str(slide["body"]).strip(),
                    claim_ids=list(slide.get("claim_ids", [])),
                    visual_brief=str(slide.get("visual_brief", "")).strip(),
                )
                for slide in data["slides"]
            ],
            stories=[
                StoryFrame(
                    frame_number=int(story["frame_number"]),
                    kind=str(story["kind"]),
                    text=str(story["text"]).strip(),
                    claim_ids=list(story.get("claim_ids", [])),
                )
                for story in data.get("stories", [])
            ],
            caption=Caption(
                text=str(data["caption"]["text"]).strip(),
                hashtags=[str(tag) for tag in data["caption"].get("hashtags", [])],
                source_note=str(data["caption"].get("source_note", "")).strip(),
                claim_ids=list(data["caption"].get("claim_ids", [])),
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"niezgodny schemat CopyDraft: {error}") from error

    _validate_draft(draft, allowed_evidence_ids, banned_phrases)
    return draft


def _validate_draft(
    draft: CopyDraft,
    allowed_evidence_ids: set[str],
    banned_phrases: list[str],
) -> None:
    if not draft.hook:
        raise ValueError("hook nie moze byc pusty")
    if not draft.voiceover or not draft.voiceover[0].time_range.startswith("0-3"):
        raise ValueError("pierwszy segment voiceover musi miec hook w zakresie 0-3s")
    if not 5 <= len(draft.slides) <= 7:
        raise ValueError(f"karuzela musi miec 5-7 slajdow, ma {len(draft.slides)}")
    if draft.slides[0].role != "hook":
        raise ValueError("slajd 1 musi miec role 'hook' (napiecie, nie opis)")
    if draft.slides[-1].role != "cta":
        raise ValueError("ostatni slajd musi miec role 'cta'")
    if not draft.cta:
        raise ValueError("CTA nie moze byc puste")
    if not draft.caption.source_note:
        raise ValueError("caption musi miec source_note ze zrodlami")

    unknown = sorted(set(draft.all_claim_ids()) - allowed_evidence_ids)
    if unknown:
        raise ValueError(
            f"claim_ids bez pokrycia w evidence (halucynacja zrodla): {unknown}. "
            f"Dozwolone evidence_id: {sorted(allowed_evidence_ids)}"
        )

    hits = banned_hits(" ".join(draft.texts()), banned_phrases)
    if hits:
        raise ValueError(
            f"copy zawiera zakazane sformulowania (anti-slop): {hits}. "
            "Przepisz prosto, bez pustego hype'u."
        )


class LlmCopywriter:
    def __init__(self, gateway: ModelGateway, voice: VoiceProfile | None = None) -> None:
        self.gateway = gateway
        self.voice = voice or DEFAULT_VOICE_PROFILE

    def create_package(
        self,
        package_id: str,
        facts: MatchFacts,
        metrics: MetricSnapshot,
        brief: EditorialBrief,
        evidence: EvidenceStore,
    ) -> InstagramPackage:
        allowed_evidence_ids = {item.id for item in evidence.ledger()}
        system = self._system_prompt()
        user = self._user_prompt(facts, metrics, brief, evidence)

        draft = generate_structured(
            self.gateway,
            system=system,
            user=user,
            build=lambda data: build_copy_draft(
                data, allowed_evidence_ids, self.voice.banned_phrases
            ),
        )

        package = InstagramPackage(
            package_id=package_id,
            match=facts,
            editorial_angle=brief.selected_angle,
            reel_script=ReelScript(
                hook=draft.hook,
                voiceover=draft.voiceover,
                on_screen_text=draft.on_screen_text,
                cta=draft.cta,
            ),
            carousel=Carousel(slides=draft.slides),
            stories=draft.stories,
            caption=draft.caption,
            visual_brief={
                "safe_assets": ["wlasne plansze", "tabele", "timeline"],
                "recommended_chart": brief.visual_options[0] if brief.visual_options else "three_metric_table",
                "notes": ["nie dodawaj nowych metryk poza evidence ledgerem"],
            },
            sources=evidence.ledger(),
            status=PackageStatus.READY,
        )
        evidence.mark_used(package.all_claim_ids())
        return package

    def _system_prompt(self) -> str:
        voice = self.voice
        examples = "\n".join(
            f"- ({pair.context}) ZLE: {pair.slop} | DOBRZE: {pair.ours}"
            for pair in voice.few_shot()
        )
        return (
            "Jestes redaktorem profilu data-driven football na Instagramie.\n"
            f"Pozycjonowanie: {voice.positioning}\n"
            f"Ton TAK: {', '.join(voice.tone_do)}.\n"
            f"Ton NIE: {', '.join(voice.tone_dont)}.\n"
            f"Zakazane sformulowania: {', '.join(voice.banned_phrases)}.\n"
            "Zasady: jedna puszka = jedna mysl; kazda liczba ma evidence_id z listy; "
            "zaden nowy fakt spoza evidence; hook to napiecie, nie opis; prosty jezyk.\n"
            "Przyklady glosu:\n"
            f"{examples}\n"
            "Zwracasz WYLACZNIE obiekt JSON zgodny ze schematem podanym przez uzytkownika."
        )

    def _user_prompt(
        self,
        facts: MatchFacts,
        metrics: MetricSnapshot,
        brief: EditorialBrief,
        evidence: EvidenceStore,
    ) -> str:
        evidence_lines = "\n".join(
            f"- {item.id}: {item.claim}" for item in evidence.ledger()
        )
        forbidden = "; ".join(brief.forbidden_claims) or "brak"
        schema = {
            "hook": "string",
            "voiceover": [{"time_range": "0-3s", "text": "string", "claim_ids": ["evidence_id"]}],
            "on_screen_text": ["string"],
            "cta": "string",
            "slides": [
                {
                    "slide_number": 1,
                    "role": "hook|context|number|chart|interpretation|hero_problem|cta",
                    "headline": "string",
                    "body": "string",
                    "claim_ids": ["evidence_id"],
                    "visual_brief": "string",
                }
            ],
            "stories": [{"frame_number": 1, "kind": "poll|quiz|question", "text": "string", "claim_ids": []}],
            "caption": {
                "text": "string",
                "hashtags": ["#tag"],
                "source_note": "string",
                "claim_ids": ["evidence_id"],
            },
        }
        return (
            f"MECZ: {facts.home_team} vs {facts.away_team}, {facts.competition} ({facts.stage}).\n"
            f"WYNIK: {facts.score.full_time}"
            + (f", po karnych {facts.score.penalties}" if facts.score.penalties else "")
            + ".\n"
            f"TEZA (jedna mysl): {brief.one_sentence_thesis}\n"
            f"GLOWNA LICZBA: {brief.selected_angle.main_number.label} = "
            f"{brief.selected_angle.main_number.value} (evidence_id={brief.selected_angle.main_number.evidence_id}).\n"
            f"CEL CTA: {brief.cta_goal}.\n"
            f"ZAKAZANE CLAIMY: {forbidden}.\n"
            "DOZWOLONE EVIDENCE (mozesz cytowac tylko te id w claim_ids):\n"
            f"{evidence_lines}\n\n"
            "Wymagania struktury: karuzela 5-7 slajdow, slajd 1 role 'hook', ostatni role 'cta', "
            "pierwszy segment voiceover w 0-3s, caption z source_note.\n"
            "SCHEMAT JSON do zwrocenia:\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )
