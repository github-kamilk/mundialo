from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from app.agents import (
    AngleEditor,
    Copywriter,
    DataHunter,
    FixtureTranslator,
    LlmCopywriter,
    LlmMediaEditorial,
    LlmMediaTranslator,
    MatchResearcher,
    MediaEditorialCopy,
    MetricAnalyst,
    NarrativeScout,
    build_hashtags,
    build_media_package,
    collect_media,
)
from app.evaluation.judges import (
    FactChecker,
    MediaFactChecker,
    MediaQualityJudge,
    QualityJudge,
    final_status,
)
from app.memory import MemoryStore
from app.memory.episodes import OutletHealthStore, RunEpisode
from app.models import GenerationError, ModelError, ModelGateway
from app.observability import RunLogger, RunTelemetry
from app.schemas import (
    CountryMediaPanel,
    EditorialBrief,
    EvidenceItem,
    EvidenceStore,
    InstagramPackage,
    MatchFacts,
    MatchRequest,
    MetricSnapshot,
    PackageStatus,
    ScoreLine,
    SourceTier,
    ValidationReport,
    WorkflowRun,
)
from app.tools import (
    CorroboratedMediaFactsProvider,
    FactsProviderChain,
    LiveFactsProvider,
    MatchContext,
    MediaResearchProvider,
    RawMediaItem,
    ScheduledMatch,
    SourcePolicyError,
    ToolGateway,
    ToolGatewayError,
    find_scheduled_match,
)

FactsResearch = LiveFactsProvider | CorroboratedMediaFactsProvider | FactsProviderChain


# Marker w notes: fakty z lokalnego fixture'a podczas runu --research. Takie fakty
# moga byc nieaktualnym snapshotem (np. testowym), wiec run nigdy nie konczy sie
# 'ready' bez weryfikacji czlowieka.
FIXTURE_FALLBACK_NOTE = (
    "facts: live research bez potwierdzonego wyniku; uzyto lokalnego fixture "
    "(zweryfikuj wynik przed publikacja)"
)

# Marker w notes: wynik podany RECZNIE przez operatora (--score). Zewnetrzne zrodla
# go nie potwierdzily, wiec - jak fixture-fallback - run nigdy nie konczy sie 'ready'
# automatycznie; czlowiek zatwierdza posta przed publikacja.
OPERATOR_OVERRIDE_NOTE = (
    "facts: wynik zweryfikowany RECZNIE przez operatora (--score); brak potwierdzenia "
    "zewnetrznego (zatwierdz posta przed publikacja)"
)


class EditorInChiefCoordinator:
    def __init__(
        self,
        gateway: ToolGateway | None = None,
        memory: MemoryStore | None = None,
        logger: RunLogger | None = None,
        model_gateway: ModelGateway | None = None,
        media_research: MediaResearchProvider | None = None,
        facts_research: FactsResearch | None = None,
        schedule: list[ScheduledMatch] | None = None,
        config_notes: list[str] | None = None,
        telemetry: RunTelemetry | None = None,
        health: OutletHealthStore | None = None,
    ) -> None:
        self.gateway = gateway or ToolGateway()
        self.memory = memory or MemoryStore()
        self.logger = logger or RunLogger()
        # telemetria epizodow: ta sama instancja, ktora dostaly providery research
        # (wspoldzielona jak gateway.budget); resetowana per run w run()
        self.telemetry = telemetry or RunTelemetry()
        # magazyn zdrowia zrodel (etap 2): None = bez pamieci miedzy runami
        # (testy/fixture bez zmian). Doradczy, nigdy blokujacy.
        self.health = health
        self.media_research = media_research
        self.facts_research = facts_research
        self.schedule = schedule or []
        # Konfiguracja runu do zapisu w notes (np. ktore modele LLM jechaly ktore
        # etapy): bez tego run.json nie pozwala po fakcie odroznic 'model sie
        # pomylil' od 'operator zapomnial flagi --model' przy diagnozie wariancji.
        self.config_notes = tuple(config_notes or ())
        self.match_researcher = MatchResearcher(self.gateway)
        self.data_hunter = DataHunter(self.gateway)
        self.narrative_scout = NarrativeScout(self.gateway)
        self.metric_analyst = MetricAnalyst()
        self.angle_editor = AngleEditor()
        self.copywriter = Copywriter()
        self.model_gateway = model_gateway
        self.llm_copywriter = (
            LlmCopywriter(model_gateway, voice=self.memory.voice) if model_gateway else None
        )
        self.media_translator = (
            LlmMediaTranslator(model_gateway, voice=self.memory.voice) if model_gateway else None
        )
        self.media_editorial = (
            LlmMediaEditorial(
                model_gateway,
                voice=self.memory.voice,
                # hook/caption to tresc dla odbiorcy: ludzkie nazwy redakcji
                # z katalogu zamiast technicznych provider_id
                outlet_names=self.gateway.registry.outlet_display_names(),
            )
            if model_gateway
            else None
        )
        self.fact_checker = FactChecker()
        self.quality_judge = QualityJudge(banned_phrases=self.memory.banned_phrases())
        self.media_fact_checker = MediaFactChecker()
        self.media_quality_judge = MediaQualityJudge(banned_phrases=self.memory.banned_phrases())

    def run(self, request: MatchRequest, save_run: bool = False) -> WorkflowRun:
        request.validate()
        self.gateway.reset()
        self.telemetry.reset()
        if request.post_type == "media_reaction":
            return self._run_media_reaction(request, save_run)
        return self._run_data_story(request, save_run)

    def _episode(
        self, run_id: str, request: MatchRequest, status: PackageStatus, blocking: list[str]
    ) -> dict:
        """Epizod runu do run.json (pamiec epizodyczna, etap 1): deterministyczna
        kompresja telemetrii. Halty tez dostaja epizod - to najciekawsze runy."""
        return RunEpisode.from_telemetry(
            run_id=run_id,
            match_query=request.match_query,
            status=status,
            blocking=blocking,
            telemetry=self.telemetry,
        ).as_dict()

    def _health_advisories(self, countries: list[str]) -> list[str]:
        """Gotowa diagnoza z magazynu zdrowia do notes (etap 2: tylko tekst).

        Operator (i skill debugu) widzi 'kicker botblock 7x' od razu, zamiast
        wyprowadzac to z surowych logow. Awaria magazynu nie moze topic runu."""
        if self.health is None:
            return []
        try:
            return self.health.advisories(countries)
        except Exception as error:  # noqa: BLE001 - pamiec doradcza, nigdy blokujaca
            return [f"outlet_health: advisories nieudane ({error})"]

    def _persist(self, run: WorkflowRun, save_run: bool) -> None:
        """Zapis runu + domkniecie petli epizodycznej (apply do magazynu zdrowia).

        Epizod aplikujemy TYLKO przy save_run (realne runy operatora) - runy
        testowe/ad-hoc nie zatruwaja obrazu zdrowia. apply() jest best-effort."""
        if not save_run:
            return
        self.logger.save(run)
        if self.health is not None and run.episode:
            try:
                self.health.apply(run.episode)
            except Exception:  # noqa: BLE001 - pamiec doradcza, nigdy blokujaca
                pass

    def _acquire_facts(
        self,
        request: MatchRequest,
        evidence: EvidenceStore,
        notes: list[str] | None = None,
    ) -> tuple[MatchFacts | None, tuple[PackageStatus, list[str], str] | None]:
        """Pozyskuje fakty: live (facts_research), z fallbackiem do fixture. Zwraca (facts, halt_spec)."""
        acquired_notes = notes if notes is not None else []
        # operator podal wynik recznie (--score): pomijamy live/fixture i budujemy fakty
        # z tego wyniku + metadanych terminarza. Zewnetrzne zrodla go nie potwierdzily,
        # wiec OPERATOR_OVERRIDE_NOTE wymusi pozniej needs_human_review.
        if request.score_override:
            return self._facts_from_operator_score(request, evidence, acquired_notes)
        if self.facts_research is not None:
            diagnostics: list[str] = []
            try:
                result = self.facts_research.acquire(
                    request.match_query, request.date_hint, notes=diagnostics
                )
            except Exception as error:  # noqa: BLE001 - degradacja zamiast crashu
                detail = f"; {'; '.join(diagnostics)}" if diagnostics else ""
                return None, (
                    PackageStatus.NEEDS_HUMAN_REVIEW,
                    ["live_facts_unavailable"],
                    f"live facts: {error}{detail}",
                )
            if result is not None:
                facts, evidence_items = result
                for item in evidence_items:
                    try:
                        self.gateway.registry.validate_evidence(item)
                    except SourcePolicyError as error:
                        return None, (
                            PackageStatus.NEEDS_HUMAN_REVIEW,
                            ["source_integrity_failed"],
                            str(error),
                        )
                    evidence.add(item)
                return facts, None
            # live nie znalazl potwierdzonego wyniku -> fixture (jesli mecz wpisany lokalnie)
            facts, _fixture_halt = self._facts_from_fixture(request, evidence)
            if facts is not None:
                acquired_notes.append(FIXTURE_FALLBACK_NOTE)
                # diagnoza live NIE moze zginac - operator musi widziec, czemu
                # zaden zewnetrzny provider nie potwierdzil wyniku
                acquired_notes.extend(diagnostics)
                return facts, None
            detail = f" ({'; '.join(diagnostics)})" if diagnostics else ""
            return None, (
                PackageStatus.INSUFFICIENT_EVIDENCE,
                ["match_not_found_live"],
                f"live facts: nie znaleziono potwierdzonego wyniku meczu{detail}",
            )

        return self._facts_from_fixture(request, evidence)

    def _enrich_facts_from_schedule(
        self, facts: MatchFacts, scheduled: ScheduledMatch
    ) -> MatchFacts:
        """Terminarz jako zrodlo prawdy dla metadanych: venue/data/etap/rozgrywki.

        Wynik i strzelcy POZOSTAJA z researchu (terminarz ich nie zna); nadpisujemy
        tylko to, co operator podal z gory, a scout czesto zgaduje ('nieznany stadion').
        """
        updates: dict[str, str] = {}
        if scheduled.venue:
            updates["venue"] = scheduled.venue
        if scheduled.date:
            updates["date"] = scheduled.date
        if scheduled.stage and (not facts.stage or facts.stage.startswith("nieznan")):
            updates["stage"] = scheduled.stage
        if scheduled.competition and (
            not facts.competition or facts.competition.startswith("nieznan")
        ):
            updates["competition"] = scheduled.competition
        return replace(facts, **updates) if updates else facts

    def _facts_from_fixture(
        self, request: MatchRequest, evidence: EvidenceStore
    ) -> tuple[MatchFacts | None, tuple[PackageStatus, list[str], str] | None]:
        resolution = self.match_researcher.resolve(request)
        if resolution["status"] != "resolved":
            status = (
                PackageStatus.NEEDS_HUMAN_REVIEW
                if resolution["status"] == "needs_human_review"
                else PackageStatus.INSUFFICIENT_EVIDENCE
            )
            return None, (
                status,
                resolution.get("ambiguities", ["match_unresolved"]),
                f"resolve_match returned status={resolution['status']}",
            )
        try:
            facts = self.match_researcher.fetch_facts(resolution["match_id"], evidence)
        except ToolGatewayError as error:
            return None, (
                PackageStatus.NEEDS_HUMAN_REVIEW,
                ["source_integrity_failed"],
                str(error),
            )
        return facts, None

    def _facts_from_operator_score(
        self, request: MatchRequest, evidence: EvidenceStore, notes: list[str]
    ) -> tuple[MatchFacts | None, tuple[PackageStatus, list[str], str] | None]:
        """Fakty z wyniku podanego recznie przez operatora (--score).

        Kraje bierzemy z zapytania (aliasy z rejestru), wynik z operatora, a venue/data/
        etap/rozgrywki uzupelni potem _enrich_facts_from_schedule. Wynik to evidence o
        NISKIM zaufaniu (tier C, provider OperatorOverride) - dodajemy go wprost do magazynu
        (jak sciezka fixture, bez walidacji rejestru), a OPERATOR_OVERRIDE_NOTE wymusi
        needs_human_review.
        """
        profiles = self.gateway.registry.countries_in_text(request.match_query)
        if len(profiles) < 2:
            return None, (
                PackageStatus.INSUFFICIENT_EVIDENCE,
                ["match_not_found"],
                f"--score podany, ale nie wykryto dwoch krajow w '{request.match_query}'",
            )
        home, away = profiles[0].country, profiles[1].country
        score = request.score_override or ""
        date = request.date_hint or ""
        # kanoniczny match_id, gdy mecz daje sie rozwiazac (gold media w trybie
        # deterministycznym jest kluczowane match_id); inaczej syntetyczny - w trybie
        # --research media i tak ida per kraj, wiec id jest nieistotne
        match_id = f"override_{uuid4().hex[:12]}"
        try:
            resolution = self.match_researcher.resolve(request)
            if resolution.get("status") == "resolved" and resolution.get("match_id"):
                match_id = resolution["match_id"]
        except ToolGatewayError:
            pass
        result_id = f"e_result_override_{uuid4().hex[:10]}"
        evidence.add(
            EvidenceItem(
                id=result_id,
                claim=f"{home} - {away}: {score} (wynik zweryfikowany recznie przez operatora).",
                value=score,
                source_url="operator://verified",
                source_tier=SourceTier.C,
                provider="OperatorOverride",
                retrieved_at=datetime.now(timezone.utc).isoformat(),
                confidence="low",
            )
        )
        facts = MatchFacts(
            match_id=match_id,
            competition="",  # uzupelni terminarz (_enrich_facts_from_schedule)
            stage="",
            date=date,
            venue="",
            home_team=home,
            away_team=away,
            score=ScoreLine(full_time=score),
            goals=[],
            key_events=[],
            source_ids=[result_id],
            resolution_confidence="low",
        )
        notes.append(OPERATOR_OVERRIDE_NOTE)
        return facts, None

    def _run_data_story(self, request: MatchRequest, save_run: bool = False) -> WorkflowRun:
        run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
        evidence = EvidenceStore()
        facts_notes: list[str] = list(self.config_notes)
        facts, halt_spec = self._acquire_facts(request, evidence, notes=facts_notes)
        if halt_spec is not None:
            status, blocking, note = halt_spec
            return self._halt(
                run_id=run_id,
                request=request,
                status=status,
                blocking=blocking,
                note=note,
                evidence=evidence,
                save_run=save_run,
            )
        match_id = facts.match_id
        self.memory.write("facts", facts)

        metrics = self.data_hunter.fetch_metrics(match_id)
        if metrics is None:
            return self._halt(
                run_id=run_id,
                request=request,
                status=PackageStatus.NEEDS_HUMAN_REVIEW,
                blocking=["metrics_unavailable"],
                note="brak metryk w zaufanym zrodle; nie tworzymy angle'u opartego na danych",
                evidence=evidence,
                save_run=save_run,
            )
        self.memory.write("metrics", metrics)

        narratives = self.narrative_scout.fetch_narratives(match_id)
        self.memory.write("narratives", narratives)

        insights = self.metric_analyst.analyze(facts, metrics, narratives)
        self.memory.write("insights", insights)

        candidates = self.angle_editor.generate_candidates(facts, metrics, insights)
        selected = self.angle_editor.select(candidates)
        brief = self.angle_editor.create_brief(selected, metrics)
        self.memory.write("selected_angle", selected)

        package: InstagramPackage | None = None
        notes: list[str] = list(facts_notes)
        if selected.score.total >= 7:
            package, copy_note = self._write_copy(facts, metrics, brief, evidence)
            notes.append(copy_note)
        else:
            notes.append(
                f"brak angle'u 7/10 (najlepszy={selected.score.total}); "
                "material idzie do human review zamiast slabej puszki"
            )

        fact_report = self.fact_checker.validate(package, evidence)
        quality_report = self.quality_judge.validate(package)
        status = final_status(fact_report, quality_report)
        if package and status != package.status:
            package = InstagramPackage(
                package_id=package.package_id,
                match=package.match,
                editorial_angle=package.editorial_angle,
                reel_script=package.reel_script,
                carousel=package.carousel,
                stories=package.stories,
                caption=package.caption,
                visual_brief=package.visual_brief,
                sources=package.sources,
                status=status,
            )

        run = WorkflowRun(
            run_id=run_id,
            request=request,
            package=package,
            fact_check=fact_report,
            quality_report=quality_report,
            tool_calls=self.gateway.as_dicts(),
            evidence=evidence.ledger(),
            status=status,
            notes=notes,
            episode=self._episode(
                run_id,
                request,
                status,
                [*fact_report.blocking_issues, *quality_report.blocking_issues],
            ),
        )
        self._persist(run, save_run)
        return run

    def _run_media_reaction(self, request: MatchRequest, save_run: bool = False) -> WorkflowRun:
        run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
        evidence = EvidenceStore()
        facts_notes: list[str] = list(self.config_notes)
        # terminarz: autorytatywne metadane meczu (data, miasto, stadion) podane z gory -
        # zero ekstrakcji lokalizacji z tekstu i automatyczny date_hint
        scheduled = find_scheduled_match(
            self.gateway.registry, self.schedule, request.match_query, request.date_hint
        )
        if scheduled is not None:
            if not request.date_hint:
                # MatchRequest jest frozen - nowa instancja zamiast mutacji; dalsze
                # kroki (i zapis runu) widza efektywny date_hint z terminarza
                request = replace(request, date_hint=scheduled.date)
            facts_notes.append(
                f"terminarz: {scheduled.home} vs {scheduled.away}, {scheduled.date}, "
                f"{scheduled.venue}"
            )
        facts, halt_spec = self._acquire_facts(request, evidence, notes=facts_notes)
        if halt_spec is not None:
            status, blocking, note = halt_spec
            return self._halt(
                run_id=run_id,
                request=request,
                status=status,
                blocking=blocking,
                note=note,
                evidence=evidence,
                save_run=save_run,
            )
        if scheduled is not None:
            facts = self._enrich_facts_from_schedule(facts, scheduled)
        match_id = facts.match_id
        self.memory.write("facts", facts)

        countries = [facts.home_team, facts.away_team]
        # advisories z magazynu zdrowia (etap 2): diagnoza z POPRZEDNICH runow
        # ("kicker botblock 7x - licz na raw_content") laduje w notes, ZANIM tor
        # medialny zacznie zbierac; trafia tez do notki haltu (media_diag)
        facts_notes.extend(self._health_advisories(countries))

        # wynik z fixture-fallbacku jest NIEPOTWIERDZONY - nie podajemy go scoutowi,
        # zeby zle fakty nie odrzucaly prawdziwych relacji pomeczowych
        confirmed_score = (
            None if FIXTURE_FALLBACK_NOTE in facts_notes else facts.score.full_time
        )
        context = MatchContext(
            home_team=facts.home_team,
            away_team=facts.away_team,
            date=facts.date,
            competition=facts.competition,
            stage=facts.stage,
            match_id=facts.match_id,
            score=confirmed_score,
        )
        raw_by_country = collect_media(
            self.gateway,
            match_id,
            countries,
            evidence,
            context=context,
            research=self.media_research,
            notes=facts_notes,
        )
        media_diag = f" ({'; '.join(facts_notes)})" if facts_notes else ""
        available = [country for country in countries if raw_by_country.get(country)]
        if not available:
            return self._halt(
                run_id=run_id,
                request=request,
                status=PackageStatus.INSUFFICIENT_EVIDENCE,
                blocking=["media_unavailable"],
                note=f"brak glosow z zaufanych outletow dla obu krajow{media_diag}",
                evidence=evidence,
                save_run=save_run,
            )
        if len(available) < len(countries):
            missing = [country for country in countries if country not in available]
            return self._halt(
                run_id=run_id,
                request=request,
                status=PackageStatus.NEEDS_HUMAN_REVIEW,
                blocking=["one_country_media_missing"],
                note=f"brak glosow z zaufanych outletow: {missing}{media_diag}",
                evidence=evidence,
                save_run=save_run,
            )

        panels, copy_note, blocking = self._translate_panels(
            countries, raw_by_country, final_score=confirmed_score
        )
        if panels is None:
            return self._halt(
                run_id=run_id,
                request=request,
                status=PackageStatus.NEEDS_HUMAN_REVIEW,
                blocking=[blocking or "translation_unavailable"],
                note=copy_note,
                evidence=evidence,
                save_run=save_run,
            )

        editorial, editorial_note = self._media_editorial_frame(facts, panels)

        package = build_media_package(
            f"mpkg_{facts.match_id}_{uuid4().hex[:8]}",
            facts,
            panels,
            evidence,
            editorial=editorial,
            hashtags=build_hashtags(facts, self._hashtag_names(countries)),
        )

        fact_report = self.media_fact_checker.validate(package, evidence)
        quality_report = self.media_quality_judge.validate(package)
        status = final_status(fact_report, quality_report)
        unconfirmed_score = (
            FIXTURE_FALLBACK_NOTE in facts_notes or OPERATOR_OVERRIDE_NOTE in facts_notes
        )
        if unconfirmed_score and status == PackageStatus.READY:
            # wynik z fixture'a albo z recznego override operatora: zadne zrodlo
            # zewnetrzne go nie potwierdzilo, wiec nie publikujemy automatycznie -
            # czlowiek zatwierdza posta
            status = PackageStatus.NEEDS_HUMAN_REVIEW
        if status != package.status:
            package = replace(package, status=status)

        run = WorkflowRun(
            run_id=run_id,
            request=request,
            package=None,
            fact_check=fact_report,
            quality_report=quality_report,
            tool_calls=self.gateway.as_dicts(),
            evidence=evidence.ledger(),
            status=status,
            notes=[*facts_notes, copy_note, editorial_note],
            media_package=package,
            episode=self._episode(
                run_id,
                request,
                status,
                [*fact_report.blocking_issues, *quality_report.blocking_issues],
            ),
        )
        self._persist(run, save_run)
        return run

    def _hashtag_names(self, countries: list[str]) -> dict[str, list[str]]:
        """Nazwy do hashtagow per kraj: english_name + przydomki kadr z rejestru.

        Konwencja team_names: [0]=PL kanoniczna, [1]=lokalna pelna, [2+]=przydomki.
        Lokalna pelna odpada (bywa nielacinska/przydluga), przydomki to realne
        tagi kibicowskie (#eltri, #bafanabafana).
        """
        names: dict[str, list[str]] = {}
        for country in countries:
            profile = self.gateway.registry.country_profile(country)
            if profile is None:
                continue
            extra = list(profile.team_names[2:])
            if profile.english_name:
                extra.insert(0, profile.english_name)
            names[country] = extra
        return names

    def _media_editorial_frame(
        self,
        facts: MatchFacts,
        panels: list[CountryMediaPanel],
    ) -> tuple[MediaEditorialCopy | None, str]:
        """Rama redakcyjna (hook + kontrast + caption) z paneli; szablon przy braku/bledzie.

        Blad LLM nie psuje runu - karuzela bez ramy jest poprawna (neutralny tytul),
        tylko mniej charakterna; operator widzi przyczyne w notes.
        """
        if self.media_editorial is None:
            return None, "editorial: szablon (brak modelu)"
        try:
            editorial = self.media_editorial.write(facts, panels)
        except (GenerationError, ModelError, ValueError) as error:
            return None, f"editorial: fallback szablonu po bledzie LLM ({error})"
        return editorial, "editorial: llm (hook + kontrast z paneli prasy)"

    def _translate_panels(
        self,
        countries: list[str],
        raw_by_country: dict[str, list[RawMediaItem]],
        final_score: str | None = None,
    ) -> tuple[list[CountryMediaPanel] | None, str, str | None]:
        if self.media_translator is not None:
            suffix = ""
            try:
                panels = [
                    self.media_translator.write_panel(
                        country, raw_by_country[country], final_score=final_score
                    )
                    for country in countries
                ]
                if all(panel.quotes for panel in panels):
                    return panels, "media: llm", None
                suffix = " (LLM zwrocil pusty panel)"
            except (GenerationError, ModelError) as error:
                suffix = f" po bledzie LLM ({error})"
            panels = [
                FixtureTranslator().write_panel(country, raw_by_country[country])
                for country in countries
            ]
            if all(panel.quotes for panel in panels):
                return panels, f"media: fallback fixture{suffix}", None
            return None, f"media: brak tlumaczenia{suffix}", "translation_unavailable"

        panels = [
            FixtureTranslator().write_panel(country, raw_by_country[country])
            for country in countries
        ]
        if all(panel.quotes for panel in panels):
            return panels, "media: deterministyczny (fixture)", None
        return None, "media: brak tlumaczenia (brak modelu i gold w fixture)", "translation_unavailable"

    def _write_copy(
        self,
        facts: MatchFacts,
        metrics: MetricSnapshot,
        brief: EditorialBrief,
        evidence: EvidenceStore,
    ) -> tuple[InstagramPackage | None, str]:
        package_id = f"pkg_{facts.match_id}_{uuid4().hex[:8]}"
        if self.llm_copywriter is not None:
            try:
                package = self.llm_copywriter.create_package(
                    package_id, facts, metrics, brief, evidence
                )
                return package, "copy: llm"
            except (GenerationError, ModelError) as error:
                try:
                    package = self.copywriter.create_package(
                        package_id=package_id,
                        facts=facts,
                        metrics=metrics,
                        brief=brief,
                        evidence=evidence,
                    )
                    return package, f"copy: fallback deterministyczny po bledzie LLM ({error})"
                except Exception as fallback_error:  # noqa: BLE001 - twardy floor: review zamiast slabej puszki
                    return None, f"copy: generacja nieudana, do review ({fallback_error})"

        package = self.copywriter.create_package(
            package_id=package_id,
            facts=facts,
            metrics=metrics,
            brief=brief,
            evidence=evidence,
        )
        return package, "copy: deterministyczny"

    def _halt(
        self,
        run_id: str,
        request: MatchRequest,
        status: PackageStatus,
        blocking: list[str],
        note: str,
        evidence: EvidenceStore | None = None,
        save_run: bool = False,
    ) -> WorkflowRun:
        run = WorkflowRun(
            run_id=run_id,
            request=request,
            package=None,
            fact_check=ValidationReport(
                status="needs_human_review",
                checks=[],
                blocking_issues=blocking,
            ),
            quality_report=ValidationReport(
                status="needs_human_review",
                checks=[],
                blocking_issues=["package_missing"],
            ),
            tool_calls=self.gateway.as_dicts(),
            evidence=evidence.ledger() if evidence else [],
            status=status,
            notes=[note],
            episode=self._episode(run_id, request, status, blocking),
        )
        self._persist(run, save_run)
        return run
