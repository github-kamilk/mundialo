from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from app.models import OpenAiModelGateway
from app.observability import RunLogger, RunTelemetry
from app.orchestration import EditorInChiefCoordinator
from app.schemas import MatchRequest, to_plain
from app.tools import ToolGateway


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mundial-redakcja-ai",
        description="Generuje evidence-first puszke instagramowa po meczu.",
    )
    parser.add_argument("--match", required=True, help="Opis meczu, np. 'PSG - Arsenal'.")
    parser.add_argument("--date", default=None, help="Opcjonalna data YYYY-MM-DD.")
    parser.add_argument("--competition", default=None, help="Opcjonalna podpowiedz rozgrywek.")
    parser.add_argument(
        "--post-type",
        default="media_reaction",
        choices=["media_reaction", "data_story"],
        help="Tor tresci: glowny (reakcje mediow) lub wtorny (post o danych).",
    )
    parser.add_argument(
        "--score",
        default=None,
        help=(
            "Wynik zweryfikowany RECZNIE przez operatora, format 'X-Y' (np. '1-1'). "
            "Furtka na mecze, gdzie zrodla sa nieosiagalne (FIFA JS-wall, prasa pisze "
            "slownie 'empate' bez cyfr): wstrzykuje wynik jako evidence o niskim zaufaniu "
            "i WYMUSZA needs_human_review (czlowiek zatwierdza posta przed publikacja)."
        ),
    )
    parser.add_argument("--pretty", action="store_true", help="Formatuj JSON.")
    parser.add_argument("--save-run", action="store_true", help="Zapisz run do katalogu runs/.")
    parser.add_argument("--runs-dir", default="runs", help="Katalog na replay runow.")
    parser.add_argument(
        "--llm",
        action="store_true",
        help=(
            "Wlacz sciezke LLM (tlumaczenie cytatow + copy) przez OpenAiModelGateway. "
            "Wymaga: pip install '.[llm]' oraz zmiennej OPENAI_API_KEY. Bez tej flagi tor "
            "medialny uzywa gold 'translation_pl' z fixture (sciezka deterministyczna)."
        ),
    )
    parser.add_argument(
        "--model",
        default="gpt-4o-mini",
        help=(
            "Model JAKOSCIOWY: tlumaczenia, streszczenia na slajdy, copy (domyslnie "
            "gpt-4o-mini; na produkcje rekomendowane gpt-4o - to jedyne miejsce, gdzie "
            "poziom polszczyzny jest widoczny dla odbiorcy)."
        ),
    )
    parser.add_argument(
        "--light-model",
        default="gpt-4o-mini",
        help=(
            "Model LEKKI: scouty ekstrakcji (wybor cytatu, wynik meczu ze strony). "
            "Te kroki maja twarde walidacje (verbatim, X-Y, anty-fabrykacja), wiec "
            "tanszy model nie obniza jakosci - bledy sa lapane i ponawiane. To tu "
            "pala sie tokeny (wiele artykulow x retry), wiec mini tnie koszty."
        ),
    )
    parser.add_argument(
        "--research",
        action="store_true",
        help=(
            "Tryb live: model szuka faktow i cytatow w obrebie whitelisty (search+fetch). "
            "Wymaga: pip install '.[research,llm]', OPENAI_API_KEY oraz TAVILY_API_KEY. "
            "Implikuje --llm. Bez sieci/kluczy degraduje sie do fixture."
        ),
    )
    parser.add_argument(
        "--search-provider",
        default="tavily",
        choices=["tavily"],
        help="Backend wyszukiwania dla --research (domyslnie tavily).",
    )
    parser.add_argument(
        "--time-range",
        default="week",
        choices=["day", "week", "month", "year"],
        help=(
            "Okno swiezosci wynikow search dla --research (domyslnie week). Chroni "
            "przed lapaniem archiwalnych relacji tych samych druzyn sprzed lat."
        ),
    )
    parser.add_argument(
        "--render",
        action="store_true",
        help=(
            "Po runie wyrenderuj karuzele PNG do runs/<run_id>/slides/ (wymaga "
            "pip install '.[render]'; implikuje --save-run). Automatycznie renderowany "
            "jest tylko status ready; dla needs_human_review uzyj: python -m app.render --allow-review."
        ),
    )
    parser.add_argument(
        "--no-episodes",
        action="store_true",
        help=(
            "Wylacz pamiec epizodyczna: bez advisories z runs/.outlet_health.json "
            "w notes i bez aktualizacji magazynu po runie (epizod w run.json zostaje). "
            "Raport magazynu: python -m app.health."
        ),
    )
    return parser


def render_after_run(plain_run: dict, run_id: str, runs_dir: Path) -> str:
    """Renderuje karuzele po zakonczonym runie. Zwraca kod wyniku (do testow).

    Blad renderu nie moze zabic wyniku runu — pakiet tekstowy jest juz policzony
    i zapisany, render zawsze mozna powtorzyc przez `python -m app.render`.
    """
    media_package = plain_run.get("media_package")
    if not media_package:
        print("[render] brak media_package - nic do renderu.", file=sys.stderr)
        return "no_package"
    status = media_package.get("status", "")
    if status != "ready":
        print(
            f"[render] status '{status}' - pomijam automatyczny render; podglad: "
            f"python -m app.render --run-dir {runs_dir / run_id} --allow-review",
            file=sys.stderr,
        )
        return "skipped_status"
    try:
        import app.render as render_layer

        out_dir = runs_dir / run_id / "slides"
        specs = render_layer.build_slide_specs(media_package)
        paths = render_layer.render_slides(specs, out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        caption_path = out_dir / "caption.txt"
        caption_path.write_text(
            render_layer.build_caption_text(media_package), encoding="utf-8"
        )
        x_post_path = out_dir / "x_post.txt"
        x_post_path.write_text(
            render_layer.build_x_post_text(media_package), encoding="utf-8"
        )
    except Exception as error:  # noqa: BLE001 - render jest best-effort po zapisanym runie
        print(f"[render] blad renderu (run zapisany, render mozna powtorzyc): {error}", file=sys.stderr)
        return "error"
    for path in paths:
        print(f"[render] {path}", file=sys.stderr)
    print(f"[render] {caption_path}", file=sys.stderr)
    print(f"[render] {x_post_path}", file=sys.stderr)
    print(f"[render] zapisano {len(paths)} slajdow + caption.txt + x_post.txt.", file=sys.stderr)
    return "rendered"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    request = MatchRequest(
        match_query=args.match,
        date_hint=args.date,
        competition_hint=args.competition,
        post_type=args.post_type,
        score_override=args.score,
    )
    try:
        request.validate()
    except ValueError as error:
        print(f"[blad] niepoprawne zapytanie: {error}", file=sys.stderr)
        return 2
    use_llm = args.llm or args.research
    model_gateway = None
    light_gateway = None
    if use_llm:
        if not os.environ.get("OPENAI_API_KEY"):
            print(
                "[ostrzezenie] tryb LLM wlaczony, ale brak OPENAI_API_KEY; system sprobuje "
                "LLM i przy bledzie zrobi fallback (gold z fixture / human review).",
                file=sys.stderr,
            )
        # dwa poziomy: jakosciowy (tresc widoczna dla odbiorcy) i lekki (ekstrakcja
        # za twardymi walidacjami) - patrz help --model / --light-model
        model_gateway = OpenAiModelGateway(model=args.model)
        light_gateway = (
            model_gateway
            if args.light_model == args.model
            else OpenAiModelGateway(model=args.light_model)
        )

    gateway = ToolGateway()
    # telemetria epizodow (etap 1): jedna instancja wspoldzielona przez search,
    # provider medialny i koordynatora (jak gateway.budget); epizod idzie do run.json
    telemetry = RunTelemetry()
    # magazyn zdrowia zrodel (etap 2): advisories do notes + apply po realnym runie.
    # Doradczy, nigdy blokujacy - dlatego wlaczany domyslnie; --no-episodes wylacza.
    health = None
    if not args.no_episodes:
        from app.memory.episodes import OutletHealthStore

        health = OutletHealthStore(path=Path(args.runs_dir) / ".outlet_health.json")
    media_research = None
    facts_research = None
    if args.research:
        from app.agents import (
            LlmFactsScout,
            LlmMediaCurator,
            LlmMediaScout,
            LlmPostMatchGate,
        )
        from app.tools import (
            CachingSearchClient,
            CorroboratedMediaFactsProvider,
            DiskTtlCache,
            FactsProviderChain,
            HttpPageFetcher,
            LiveFactsProvider,
            MediaResearchProvider,
            TavilySearchClient,
            TelemetrySearchClient,
            TtlCache,
        )

        if not os.environ.get("TAVILY_API_KEY"):
            print(
                "[ostrzezenie] --research wlaczone, ale brak TAVILY_API_KEY; search live nie "
                "zadziala i system zdegraduje sie do fixture (jesli istnieje).",
                file=sys.stderr,
            )
        # time_range odcina archiwalne relacje; advanced = lepszy ranking, mniej smieci.
        # DiskTtlCache wspoldzielony miedzy procesami: re-roll tego samego meczu
        # (wariancja LLM) nie pali budzetu Tavily od zera - wyczerpane kredyty (432)
        # topily kolejne kraje haltem one_country_media_missing. Puste wyniki nie sa
        # cache'owane (re-roll po zwloce indeksacji dostaje swieza probe).
        # TelemetrySearchClient najbardziej zewnetrznie (nad cachem): zdarzenie
        # opisuje 'co run widzial' - 432 i puste wyniki to sygnaly zdrowia
        # niezaleznie od tego, czy odpowiedz przyszla z dysku
        search_client = TelemetrySearchClient(
            inner=CachingSearchClient(
                inner=TavilySearchClient(time_range=args.time_range, search_depth="advanced"),
                cache=DiskTtlCache(root=Path(args.runs_dir) / ".search_cache"),
            ),
            telemetry=telemetry,
        )
        fetcher = HttpPageFetcher()
        cache = TtlCache()
        facts_scout = LlmFactsScout(light_gateway)
        media_research = MediaResearchProvider(
            registry=gateway.registry,
            search_client=search_client,
            fetcher=fetcher,
            # ekstrakcja co do zasady na lekkim modelu (twardy walidator verbatim),
            # ale dla pisma NIELACINSKIEGO (arabskie/CJK/cyrylica) kierujemy na model
            # jakosciowy: mini gubi tam slowa i guard anti-fabrication topi caly felieton
            scout=LlmMediaScout(light_gateway, strong_gateway=model_gateway),
            budget=gateway.budget,
            cache=cache,
            # kurator na lekkim modelu (jak scout): wybor reakcji wlasnej prasy z puli
            curator=LlmMediaCurator(light_gateway) if light_gateway else None,
            # bramka temporalna na modelu JAKOSCIOWYM (nie lekkim): decyzja
            # 'pomeczowe czy przedmeczowe' NIE ma walidatora, a mini myli ja nawet
            # przy waskim pytaniu binarnym (zmierzone: mini 3/3 zle na przedmeczowej
            # analizie 126130, 4o 3/3 dobrze). To jedyny krok osadu bez backstopu,
            # wiec idzie na 4o; ekstrakcja verbatim (z walidatorem) zostaje na mini.
            recency_gate=LlmPostMatchGate(model_gateway) if model_gateway else None,
            telemetry=telemetry,
            # etap 3: kolejnosc sekcji wg zdrowia + pomijanie martwego fetchu;
            # --no-episodes (health=None) = zachowanie jak bez pamieci
            health=health,
        )
        # fakty: najpierw zrodla oficjalne, potem korroboracja w mediach obu krajow
        facts_research = FactsProviderChain(
            providers=(
                LiveFactsProvider(
                    registry=gateway.registry,
                    search_client=search_client,
                    fetcher=fetcher,
                    scout=facts_scout,
                    budget=gateway.budget,
                    cache=cache,
                ),
                CorroboratedMediaFactsProvider(
                    registry=gateway.registry,
                    search_client=search_client,
                    fetcher=fetcher,
                    scout=facts_scout,
                    budget=gateway.budget,
                    cache=cache,
                ),
            )
        )

    from app.tools import load_schedule

    # zapis konfiguracji modeli do notes runu: przy diagnozie wariancji (bramka
    # temporalna, kurator, scout) trzeba odroznic 'model sie pomylil' od
    # 'operator zapomnial --model gpt-4o' - bez tego run.json tego nie mowi
    config_notes: list[str] = []
    if use_llm:
        config_notes.append(f"modele: jakosciowy={args.model}, lekki={args.light_model}")

    coordinator = EditorInChiefCoordinator(
        gateway=gateway,
        logger=RunLogger(Path(args.runs_dir)),
        model_gateway=model_gateway,
        media_research=media_research,
        facts_research=facts_research,
        # terminarz (data/schedule/world_cup_2026.json): autorytatywna data,
        # miasto i stadion meczu; brak pliku = pusta lista = zachowanie jak dotad
        schedule=load_schedule(),
        config_notes=config_notes,
        telemetry=telemetry,
        health=health,
    )
    run = coordinator.run(request, save_run=args.save_run or args.render)
    indent = 2 if args.pretty else None
    plain_run = to_plain(run)
    # ensure_ascii=True: bezpieczny output na konsoli Windows (cp1252) - ewentualne
    # znaki spoza ASCII ida jako \uXXXX (wciaz poprawny JSON). Zapis do runs/ jest UTF-8.
    print(json.dumps(plain_run, ensure_ascii=True, indent=indent))
    if args.render:
        render_after_run(plain_run, run.run_id, Path(args.runs_dir))
    return 0


# `python -m app.cli` bez tego guardu importowal modul i wychodzil z kodem 0
# NIC nie robiac - cicha pulapka na operatora (kanoniczne wejscie to `python -m app`,
# ale obie formy maja dzialac tak samo).
if __name__ == "__main__":
    raise SystemExit(main())

