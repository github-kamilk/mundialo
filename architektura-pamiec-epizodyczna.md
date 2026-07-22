# Architektura: pamięć epizodyczna runów (pętla `run → epizod → stan → bogatszy następny run`)

Status: zaakceptowany; **Etapy 1–3 wdrożone** (2026-07-10). Etap 1: `app/observability/telemetry.py`,
`app/memory/episodes.py` (`RunEpisode`), pole `episode` w run.json, `TelemetrySearchClient`,
`ResearchError.status_code`. Etap 2: `OutletHealthStore` (`runs/.outlet_health.json`,
okno 20, dedup po dniu, advisories od serii ≥3, transient nie buduje ani nie przerywa
serii), advisories w `notes` toru medialnego, `apply` tylko przy `save_run`,
flaga `--no-episodes`, raport `python -m app.health [--country X]`.
Etap 3: protokół `SourceHealth` (contracts), `_order_section_candidates` (demote martwych
sekcji + slot eksploracyjny), raw_content-first przy świeżym streaku botblock (bez
raw_content fetch idzie mimo streaka), re-probe po `RE_PROBE_HOURS=72`; cold-start
parity przypięte testami. Testy: `tests/test_episodes.py` (47).
Etap 4 (statystyki osądów, propozycje diffów do Mapy) pozostaje osobną decyzją.
Wzorzec: `compress_episode` → `apply_episode_to_state` → następny prompt jest
bogatszy. Domyka jedyny istotny brak zidentyfikowany w przeglądzie architektury
(2026-07-10).

## 1. Problem i cel

System dziś **uczy się przez operatora, nie sam**. Każda lekcja operacyjna — kicker
403-uje boty, record.pt trzyma crónicę za paywallem, sekcja NRK przeniosła się pod
`/fotballvm2026/`, sekcje Curaçao to JS-walle — jest odkrywana od nowa w diagnozie
runu, a konsolidowana ręcznie do `country_media.json` i heurystyk. To jest dokładnie
definicja agenta bez pamięci epizodycznej: *"uczy się na tych
samych błędach w kółko"*. Jedyna pamięć między runami to `runs/.search_cache`
(cache wyników wyszukiwania, TTL 1h).

Cel pętli:

1. **Epizod**: po każdym realnym runie system zapisuje ustrukturyzowany zapis
   operacyjny (co się udało fetchnąć, co padło i jak, które sekcje dały linki,
   co wybrał kurator, co odrzuciła bramka).
2. **Stan**: epizody agregują się do trwałego magazynu zdrowia źródeł
   (`outlet_health`), z wygaszaniem.
3. **Bogatszy następny run**: kolejny run (a) dostaje w `notes` gotową diagnozę
   zamiast odkrywania jej od zera, (b) mądrzej wybiera sekcje/outlety w ramach
   budżetu, (c) operator dostaje raport konsolidacyjny podpowiadający edycje
   `country_media.json`.

## 2. Zasady projektowe (twarde)

Kolejność ważności — przy konflikcie wygrywa wcześniejsza:

1. **Pamięć jest doradcza, nigdy blokująca.** Awaria zapisu/odczytu magazynu,
   uszkodzony plik, brak pliku → run przebiega identycznie jak dziś (cold start =
   zachowanie obecne). Epizodyka nie może stopić ani jednego runu.
2. **Pętla NIE dotyka Mapy Wiedzy.** `country_media.json` pozostaje wyłącznie
   ludzki: pętla nie dopisuje, nie usuwa i nie zmienia outletów, domen, tierów ani
   `confidence`. Whitelist i zaufanie to konstytucja (Core/Semantic — kurowana),
   `outlet_health` to obserwacje (Episodic — maszynowe, wygasające). Rozdzielenie
   warstw = operacja **Isolate** z DW; chroni też przed poisoningiem whitelisty.
3. **Epizod powstaje deterministycznie, bez LLM.** Zdarzenia mamy w kodzie w
   momencie ich zajścia — "kompresja" to agregacja pól, nie streszczanie tekstu.
   Zgodnie z DW: LLM w rygorze tylko tam, gdzie potrzebny osąd; tu nie jest.
   Bonus anti-injection: do magazynu idą wyłącznie URL-e, statusy i liczby —
   nigdy treść artykułów.
4. **Zdrowie wpływa tylko na KOLEJNOŚĆ i DIAGNOZĘ, nie na dostępność.** Outlet
   "martwy" wg zdrowia nadal jest na whitelistcie i nadal może wrócić (re-probe,
   pkt 7). Tier z rejestru zawsze bije zdrowie przy ocenie zaufania treści.
5. **Pamięć wygasa.** Okno ostatnich N zdarzeń per obiekt + polityka re-probe.
   Sygnał negatywny nie jest wyrokiem dożywotnim (odpowiednik `max_episodes`
   i priorytetyzacji recency z DW).

## 3. Artefakty i przepływ

```
                       RUN (coordinator)
                            │
   MediaResearchProvider / collect_section_hits / SearchClient / fetcher
                            │  emit() w miejscach dzisiejszych diag.append()
                            ▼
                  RunTelemetry  (in-memory, per run)          ← WRITE
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
     run.json["episode"]        RunEpisode.from_telemetry()   ← COMPRESS
     (audyt, maszynowo-                   │                     (deterministyczny)
      czytelny zapis runu)                ▼
                             OutletHealthStore.apply(episode) ← WRITE-BACK
                             runs/.outlet_health.json           (apply_episode_to_state)
                                          │
              ┌───────────────────────────┼───────────────────────────┐
              ▼                           ▼                           ▼
   advisories() → notes         kolejność sekcji/outletów    raport konsolidacyjny
   następnego runu              w ramach budżetu (Select)    dla operatora (CLI)
   ("kicker bot-block 7/7")     + slot eksploracyjny         → ręczne edycje
                                                               country_media.json
```

Lokalizacja magazynu: **`runs/.outlet_health.json`** — obok `.search_cache`, bo to
stan operacyjny (maszynowy, wygasający), a nie kurowana konfiguracja. Wersjonowana
w gicie pozostaje Mapa Wiedzy; zdrowie zmienia się co run i commitowane byłoby szumem.

## 4. Model danych

### 4.1 Zdarzenia (nowe dataclassy, `app/tools/contracts.py` lub `app/observability/telemetry.py`)

```python
@dataclass(frozen=True)
class OutletFetchEvent:
    provider_id: str          # np. "KickerDE"
    country: str
    url: str
    outcome: str              # "ok" | "botblock" | "stale_path" | "transient" | "thin" | "error"
    body_len: int             # 0 gdy brak treści
    had_raw_content: bool     # czy indeks search uratował treść mimo padłego fetchu

@dataclass(frozen=True)
class SectionProbeEvent:
    provider_id: str
    country: str
    section_url: str
    outcome: str              # "links" | "no_links" | "botblock" | "stale_path" | "transient"
    links_found: int

@dataclass(frozen=True)
class SearchEvent:
    query: str
    hits: int
    error: str | None         # np. "432" (budżet Tavily)

@dataclass(frozen=True)
class JudgmentEvent:          # dla statystyk wariancji (etap 4, opcjonalny)
    kind: str                 # "curator_pick" | "curator_empty" | "gate_reject" | "gate_bypass" | "salvage"
    country: str
    url: str | None
    detail: str
```

`RunTelemetry` (w `app/observability/telemetry.py`): kolektor append-only z
`emit(event)` i `as_dicts()`. Wstrzykiwany tam, gdzie dziś trafia `notes`/`diag`
(te zostają bez zmian — telemetria je uzupełnia, nie zastępuje).

### 4.2 Epizod (`app/memory/episodes.py`)

```python
@dataclass(frozen=True)
class RunEpisode:
    run_id: str
    at: str                    # ISO UTC
    match_query: str
    status: str                # PackageStatus runu
    blocking: list[str]        # np. ["one_country_media_missing"]
    outlet_events: list[OutletFetchEvent]
    section_events: list[SectionProbeEvent]
    search_events: list[SearchEvent]

    @classmethod
    def from_telemetry(cls, run, telemetry) -> "RunEpisode": ...
```

Epizod w całości trafia do `run.json` (sekcja `"episode"`) — run.json staje się
maszynowo czytelny, co samo w sobie jest wartością (dziś diagnoza = parsowanie
wolnotekstowych `notes`).

### 4.3 Magazyn (`runs/.outlet_health.json`)

```json
{
  "version": 1,
  "updated_at": "2026-07-10T18:00:00Z",
  "outlets": {
    "KickerDE": {
      "events": [
        {"day": "2026-07-09", "outcome": "botblock", "body_len": 0, "run_id": "run_..."},
        {"day": "2026-07-10", "outcome": "botblock", "body_len": 0, "run_id": "run_..."}
      ]
    }
  },
  "sections": {
    "https://www.vg.no/spesial/...": {
      "events": [{"day": "2026-07-10", "outcome": "no_links", "links": 0, "run_id": "run_..."}]
    }
  },
  "search": {
    "last_432_at": "2026-07-09T23:12:00Z"
  }
}
```

Zasady magazynu:

- **Okno**: max 20 zdarzeń per outlet/sekcja (`events = events[-20:]` — dokładnie
  wzorzec `EpisodicMemory.add()` z DW). Podsumowania (streaki, last_ok) liczone
  przy odczycie, nie przechowywane — jedno źródło prawdy.
- **Dedup**: klucz `(url_kanoniczny, outcome, dzień)` — re-rolle tego samego meczu
  (wariancja LLM) nie liczą tej samej porażki wielokrotnie.
- **Zapis atomowy**: tmp + rename; błąd zapisu → warning na stderr, run niezakłócony.
- **Uszkodzony plik**: reset do pustego + nota w notes (pamięć doradcza — pkt 2.1).

## 5. Klasyfikacja wyników (mapowanie na istniejący runbook)

Deterministyczne mapowanie tego, co kod już wie, na klasy z ustaloną akcją
operatorską (spójne z runbookiem utrzymania sekcji: 404/400 = zła ścieżka →
człowiek szuka nowego URL; 403/202 = bot-block → NIE ruszać, search nadrabia;
522/timeout = transient → ignorować):

| Sygnał w kodzie | Klasa | Co robi pętla |
|---|---|---|
| fetch HTTP 403/401/202, "enable JavaScript" w treści | `botblock` | advisory "licz na raw_content/search"; demote fetch-kolejności |
| fetch HTTP 404/400 na sekcji | `stale_path` | advisory "sekcja wymaga nowego URL" → raport konsolidacyjny |
| timeout / 5xx / 522 | `transient` | licz, ale nie demotuj (pojedyncze = szum) |
| fetch ok, `body_len < _MIN_ARTICLE_BODY_FOR_SLIDE` | `thin` | advisory przy powtarzalności ("outlet daje stuby") |
| sekcja zwraca 200 i 0 linków artykułów | `no_links` | streak ≥3 → advisory "JS-wall?"; demote w budżecie sekcji |
| Tavily HTTP 432 | `search.432` | advisory na start: "sprawdź kredyty/klucz przed runem" |

## 6. Zachowanie przy następnym runie (Select)

### 6.1 Advisories → `notes` (etap 2; zero zmiany zachowania retrievalu)

Na starcie runu koordynator pobiera `store.advisories(countries)` i dokleja do
`facts_notes` (obok istniejącej noty `modele:`):

```
outlet_health[Niemcy]: KickerDE botblock 7/7 od 2026-06-28 (fetch martwy; raw_content ratuje) 
outlet_health[Norwegia]: sekcja vg.no/spesial 5x zero linkow (JS-wall) 
outlet_health: Tavily 432 widziane 2026-07-09 23:12 - sprawdz kredyty
```

Wartość natychmiastowa: operator (i skill debug-match-content) dostaje diagnozę
w run.json zamiast wyprowadzać ją z surowych logów. Progi: advisory dopiero od
≥3 zgodnych zdarzeń w oknie (pojedynczy fail to szum).

### 6.2 Kolejność sekcji i outletów (etap 3; pierwsza realna zmiana zachowania)

- **Sekcje**: dziś budżet `max_sections_per_country=4` tnie listę w kolejności
  rejestru. Po zmianie: sekcje sortowane wg "dawały linki ostatnio" (streak
  `no_links`/`botblock` spycha w dół), **ale ostatni slot budżetu jest
  eksploracyjny** — dostaje go sekcja najdawniej próbowana/nieznana. Bez slotu
  eksploracyjnego zdrowie stałoby się samospełniającą się śmiercią sekcji
  (nigdy nie próbujemy → nigdy nie ozdrowieje).
- **Outlety (fetch)**: kolejność kandydatów do fetchu w `_extract_items` bez
  zmian (o niej decyduje kurator), ale outlet z długim streakiem `botblock`
  może iść od razu ścieżką `raw_content`-first (oszczędza timeouty), z probą
  fetchu wg polityki re-probe.
- **Czego NIE zmieniamy**: tier, whitelist domen, `max_quotes`, decyzje kuratora
  i bramki. Zdrowie nie widzi treści — steruje tylko I/O.

### 6.3 Raport konsolidacyjny (domknięcie na poziomie Semantic)

`python -m app.health [--country X]` — czytelny raport z magazynu:
streaki, last_ok, klasy problemów + sugerowana akcja z runbooku (np. "stale_path:
znajdź nowy URL sekcji i zaktualizuj country_media.json + verified_at"). Świadomie
**człowiek** przenosi wnioski do Mapy Wiedzy — konsolidacja semantyczna pozostaje
kurowana (zasada 2.2). To odpowiednik DW-owego "operator patrzy na Last Session
Report".

## 7. Wygaszanie i re-probe

- Zdarzenia starsze niż okno (20) wypadają automatycznie.
- **Re-probe**: jeżeli ostatnie zdarzenie dla "martwego" obiektu jest starsze niż
  72h, następny run wykonuje normalną próbę (i nadpisuje obraz zdrowia). Czyli:
  demote działa tylko na świeżych obserwacjach; stare same tracą moc.
- `transient` nigdy nie buduje streaka (filtrowane przy liczeniu podsumowań).

## 8. Zmiany w kodzie (per plik)

| Plik | Zmiana | Etap |
|---|---|---|
| `app/observability/telemetry.py` (nowy) | `RunTelemetry` + dataclassy zdarzeń | 1 |
| `app/tools/research.py` | `emit()` obok istniejących `diag.append()` (fetch, thin, gate, sekcje); klasyfikacja HTTP → klasa zdarzenia w `_fetch`/`facts_text_from_hit` | 1 |
| `app/tools/search.py` | `SearchEvent` (hits, 432) w `CachingSearchClient`/`TavilySearchClient` | 1 |
| `app/tools/control.py` | `ResearchError` niesie kod HTTP (dziś tylko string) — potrzebny do klasyfikacji | 1 |
| `app/schemas/domain.py` | `WorkflowRun.episode: dict \| None` (serializacja do run.json) | 1 |
| `app/orchestration/coordinator.py` | budowa `RunEpisode` na końcu obu torów; `store.apply()` gdy `save_run=True`; advisories → `facts_notes` na starcie | 1–2 |
| `app/memory/episodes.py` (nowy) | `RunEpisode`, `OutletHealthStore` (load/apply/advisories/ordering/save atomowy) | 2 |
| `app/cli.py` | flaga `--no-episodes`; podkomenda `health`; wpięcie store'a | 2–3 |
| `app/tools/research.py` | kolejność sekcji wg zdrowia + slot eksploracyjny; `raw_content`-first dla botblock-streak | 3 |

## 9. Etapy i testy

**Etap 1 — telemetria (zero zmiany zachowania).** Zdarzenia + `episode` w run.json.
Testy: `EpisodeCaptureTests` — fake fetcher zwraca 403/thin/ok → epizod zawiera
poprawnie sklasyfikowane zdarzenia; run bez telemetrii (testy istniejące) przechodzi
bez zmian.

**Etap 2 — magazyn + advisories (zmiana tylko w notes).** Testy:
`OutletHealthApplyTests` (apply, okno, dedup po dniu, wygasanie),
`AdvisoryThresholdTests` (advisory od ≥3 zdarzeń; transient nie buduje streaka),
`HealthNeverBlocksTests` (uszkodzony JSON → run przechodzi, nota o resecie),
`HealthIsolationTests` (store NIGDY nie modyfikuje rejestru/whitelisty).

**Etap 3 — Select (pierwsza zmiana retrievalu).** Testy:
`SectionOrderingTests` (streak no_links spycha sekcję; slot eksploracyjny bierze
najstarszą), `ReprobeTests` (po 72h martwy outlet dostaje próbę),
`ColdStartParityTests` (brak pliku zdrowia → kolejność identyczna jak dziś).

**Etap 4 (później, osobna decyzja).** Statystyki osądów (`JudgmentEvent`): częstość
pustych selekcji kuratora per model, częstość salvage per kraj — dane pod decyzje
"co przenieść na mocniejszy model". Ewentualnie: propozycje diffów do
`country_media.json` generowane do review (nigdy auto-apply).

## 10. Świadome NIE (zakres odrzucony)

- **Nie** uczymy się treścią (żadnych zapisów tekstów artykułów do pamięci) —
  tylko metadane operacyjne. Anti-injection + prawo cytatu.
- **Nie** automatyzujemy edycji Mapy Wiedzy — konsolidacja semantyczna zostaje
  ludzka (zasada 2.2).
- **Nie** budujemy pamięci "wszystkiego" (7 warstw na siłę) — zakres to zdrowie
  źródeł dla retrievalu, bo tam jest udokumentowany, powtarzalny ból
  (kicker/record/NRK/Curaçao/Tavily 432). "Projektujesz, nie instalujesz".
- **Nie** używamy LLM do kompresji epizodu — agregacja jest deterministyczna.

## 11. Mapowanie na słownik pamięci (dla spójności dokumentacji)

| Pojęcie | Realizacja tutaj |
|---|---|
| Write | `RunTelemetry.emit()` w punktach I/O |
| Compress | `RunEpisode.from_telemetry()` (formularz, deterministyczny) |
| `apply_episode_to_state` | `OutletHealthStore.apply(episode)` |
| Select / bogatszy prompt | advisories w `notes` + kolejność sekcji w budżecie |
| Isolate | `runs/.outlet_health.json` oddzielony od `data/sources/country_media.json` |
| Wygaszanie (`max_episodes`) | okno 20 zdarzeń + re-probe 72h |
| KK | zdrowie steruje I/O, nigdy nie rozszerza tego, co widzi model |
| Core/Semantic vs Episodic | Mapa Wiedzy (ludzka, wersjonowana) vs zdrowie (maszynowe, wygasające) |
