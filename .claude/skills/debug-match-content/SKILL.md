---
name: debug-match-content
description: >-
  Diagnozuje i naprawia sytuacje, gdy post z meczu nie chce się wygenerować w projekcie mundialo
  (tor media_reaction). Użyj ZAWSZE, gdy run kończy się statusem needs_human_review lub
  insufficient_evidence, render mówi "brak media_package - nic do renderu", wynik na grafice
  jest zły, post dotyczy innego meczu, brakuje głosów jednego kraju, albo użytkownik mówi
  "nie wygenerowało się", "post się nie zrobił", "coś nie tak z meczem X", "render nic nie
  wrzucił". Prowadzi od objawu, przez notatki runu (notes), do konkretnej naprawy — i rozróżnia
  problem czasowy (za wcześnie po meczu, czekaj i powtórz) od błędu kodu/selekcji (napraw + test).
---

# Debug: post z meczu się nie generuje

Ten skill jest mapą diagnostyczną toru `media_reaction`. Pipeline jest evidence-first i celowo
woli ODMÓWIĆ publikacji niż wypuścić śmieci — więc „nie wygenerowało się" prawie zawsze znaczy,
że któryś strażnik zadziałał. Twoim zadaniem jest odczytać, KTÓRY, i zdecydować, czy to problem
**danych/czasu** (czekaj i powtórz), czy **kodu/selekcji** (napraw i dodaj test regresyjny).

Projekt: `C:\Users\kaami\Desktop\mundial\mundialo`. Powłoka: PowerShell. Testy: 190+,
`python -m unittest discover -s tests -q` musi zostać zielone po każdej zmianie kodu.

## Krok 0 — złota zasada: czas czy błąd?

To najważniejsza decyzja i najczęstsze nieporozumienie. Prasa publikuje reakcje pomeczowe
**1–3 h po gwizdku**. Run odpalony w trakcie albo tuż po meczu znajdzie tylko zapowiedzi i
live-blogi — a scout je SŁUSZNIE odrzuca (to nie są reakcje na zakończony mecz). To nie jest błąd.

- Objawy problemu **czasowego**: w `notes` widać `scout: 0 cytatow` przy URL-ach z `live`,
  `online-prenos`, `preview`, `zagrijavanje`, `set-for`, `opens-world-cup`; brak relacji
  pomeczowych w sekcjach. **Naprawa: nie ruszaj kodu. Powtórz run 2–3 h po meczu lub rano.**
- Objawy problemu **kodu/selekcji**: właściwy artykuł istnieje (widać go w `notes` lub łatwo go
  znaleźć), a mimo to wypadł z puli, dostał zły wynik, albo trafił w inny mecz. **Naprawa: popraw
  filtr w kodzie i dodaj test regresyjny odtwarzający ten URL/przypadek.**

Jeśli nie masz pewności która to sytuacja — przejdź przez Krok 1–2, notes rozstrzygną.

## Krok 1 — znajdź i przeczytaj ostatni run

Każdy run zapisuje pełną diagnostykę do `runs/<id>/run.json`. Zacznij stąd, zawsze.

```powershell
cd C:\Users\kaami\Desktop\mundial\mundialo
$d = (Get-ChildItem runs | Sort-Object Name -Descending | Select-Object -First 1).Name
$r = Get-Content "runs\$d\run.json" -Raw -Encoding utf8 | ConvertFrom-Json
"RUN: $d"
"query:  $($r.request.match_query)  | date: $($r.request.date_hint)"
"status: $($r.status)  | media_package: $([bool]$r.media_package)"
"fact_check blocking:  $($r.fact_check.blocking_issues -join ', ')"
"quality blocking:     $($r.quality_report.blocking_issues -join ', ')"
if ($r.media_package) { "FAKTY: $($r.media_package.match.home_team) $($r.media_package.match.score.full_time) $($r.media_package.match.away_team)" }
```

Jeśli debugujesz konkretny, starszy run — podmień `Select-Object -First 1` na filtr po nazwie
meczu albo wskaż katalog ręcznie.

## Krok 2 — czytaj `notes` (to jest złoto diagnostyczne)

`notes` zawiera ślad researchu per kraj: jakie sekcje pobrano, jakie zapytania poszły, ile hitów
przeszło filtry, które URL-e scout odrzucił i dlaczego. To stąd dowiesz się WSZYSTKIEGO.

```powershell
$r.notes | ForEach-Object { $_ -split '; ' } | ForEach-Object { "  $_" }
# panele, które się złożyły (jeśli media_package istnieje):
$r.media_package.panels | ForEach-Object { $c=$_.country; $_.quotes | ForEach-Object { "[$c/$($_.outlet)] $($_.url)" } }
# źródła wyniku (przy złym wyniku — sprawdź, z jakiej strony przyszedł):
$r.evidence | Where-Object { $_.id -like 'e_result*' } | ForEach-Object { "RESULT: $($_.provider) $($_.value) <- $($_.source_url)" }
```

Czytając `notes` szukaj wzorców:
- `media[Kraj]: N cytatow` — ile głosów zebrał kraj. `0 cytatow` = ten kraj jest przyczyną.
- `scout: 0 cytatow (kandydatow tresci: K)` — scout dostał K kandydatów i wszystkie odrzucił
  (nie na temat / przed meczem / inny mecz). Jeśli K=0, problem jest WCZEŚNIEJ (selekcja/sekcje).
- `sekcja ... nieudana (... 403/404/...)` — strona sekcji nie odpowiedziała. Pojedyncza porażka
  jest OK (search nadrabia); jeśli padły WSZYSTKIE sekcje danego kraju, patrz awaria D.
- `-> N hitow po filtrach` — ile artykułów weszło do puli. Małe N przy realnie istniejących
  relacjach = filtr za ostry albo zła selekcja (awaria C).
- `FIXTURE_FALLBACK_NOTE` / „uzyto lokalnego fixture" — fakty z lokalnego fixture, nie z sieci
  (awaria E) — run nigdy nie kończy się `ready` automatycznie.

## Mapa: status / blocking → przyczyna → naprawa

| objaw | przyczyna | gdzie |
|---|---|---|
| `insufficient_evidence` + `media_unavailable` | OBA kraje 0 cytatów — zwykle za wcześnie po meczu | Krok 0 (czas); albo awaria C/D |
| `needs_human_review` + `one_country_media_missing` | JEDEN kraj 0 cytatów | notes tego kraju → awaria A/C/D |
| `needs_human_review` + `score_consistent_with_media` | wynik faktów ≠ wynik w cytatach (zły mecz/strona) | awaria B |
| `insufficient_evidence` + `match_not_found_live` | nie znaleziono potwierdzonego wyniku | awaria B/E; sprawdź datę i terminarz |
| `needs_human_review` + `live_facts_unavailable` / `source_integrity_failed` | infra: brak kluczy, sieć, budżet | awaria F |
| `needs_human_review` + `translation_unavailable` | LLM nie zwrócił paneli (i fixture nie ma golda) | awaria F/G; sprawdź klucz/limit modelu |
| ready, ale `FIXTURE_FALLBACK_NOTE` w notes | fakty z fixture, nie z sieci | awaria E |
| quality: `no_banned_phrases` / `score_only_on_title_slide` / `mood_requires_two_sources` | LLM złamał kontrakt głosu | awaria G |
| render: „brak media_package - nic do renderu" | run zhaltował wcześniej → nie ma pakietu | wróć do Kroku 1, to objaw, nie przyczyna |

## Katalog znanych awarii

### A. Za wcześnie po meczu (najczęstsze, NIE błąd)
notes: same `live`/`online-prenos`/`preview`, `scout: 0 cytatow`, jeden lub oba kraje 0.
→ Powtórz run 2–3 h po meczu lub rano. Patrz `runbook-mecz.md`, sekcja o timingu.

### B. Zły wynik / post o innym meczu
Wynik na grafice się nie zgadza, albo treść dotyczy innego spotkania.
- Sprawdź `e_result*` w evidence — z jakiego URL przyszedł wynik. Strona-LISTA wyników
  (`scores-fixtures`, `results`, `standings`, `fixtures`) podaje wynik CUDZEGO meczu.
- Artykuł o sparingu/innym przeciwniku sprzed dni przeszedł scout.
Naprawa w `app/tools/research.py`: `is_article_url` (odsiewa strony-listy), `date_from_url` +
`url_date_too_old` (odsiew archiwaliów; margines 2 dni na live-blogi), prompt scouta w
`app/agents/media_scout.py` (sparing/inny przeciwnik = pusta lista). Strażnik
`score_consistent_with_media` w `judges.py` jest tu Twoim sprzymierzeńcem — to on zatrzymał zły
wynik przed publikacją.

### C. Dobra relacja istnieje, ale wypadła z puli
Felieton/relacja jest w sieci, lecz `-> N hitow` jej nie zawiera albo scout jej nie dostał.
Typowe pułapki (wszystkie już raz naprawiane — szukaj wzorca):
- nazwa wielowyrazowa nie matchuje slugu z myślnikami (`bafana bafana` vs `bafana-bafana`) →
  `match_blob` normalizuje myślniki na spacje.
- śmieci w puli (betting/odds/podcast/lifestyle, strony-sekcje jednosegmentowe) → segmenty
  blokowane w `is_article_url`.
- ten sam artykuł pod wieloma slugami zjada miejsca → `canonical_article_key` (ID ≥6 cyfr jako
  klucz).
- pula za mała → `max_articles_per_country` (obecnie 8).
Naprawa w `app/tools/research.py`. Po każdej zmianie DODAJ test w `tests/test_research.py`
odtwarzający feralny URL.

### D. Wszystkie sekcje kraju padają (403/404/JS)
notes: każda `sekcja ... nieudana` dla danego kraju, a search też nie nadrabia.
→ Najpierw sprawdź, czy to nie chwilowy 403 (powtórz). Jeśli outlet trwale blokuje boty lub jest
aplikacją JS, dodaj/zmień outlety albo `sections` tego kraju w `data/sources/country_media.json`.

### E. Fixture fallback (fakty z lokalnego pliku, nie z sieci)
notes zawiera `FIXTURE_FALLBACK_NOTE`. To świadomy bezpiecznik: żaden zewnętrzny provider nie
potwierdził wyniku, więc run NIGDY nie da `ready` sam. Zwykle pochodna awarii A (za wcześnie) lub
F (infra). Zweryfikuj wynik ręcznie albo powtórz później — nie obchodź tego bezpiecznika.

### F. Infra: klucze / sieć / budżet
notes wspomina brak `TAVILY_API_KEY`/`OPENAI_API_KEY`, błąd sieci albo budżet.
Klucze są w User env; wstrzyknij do sesji przed runem:
```powershell
$env:OPENAI_API_KEY = [Environment]::GetEnvironmentVariable('OPENAI_API_KEY','User')
$env:TAVILY_API_KEY = [Environment]::GetEnvironmentVariable('TAVILY_API_KEY','User')
```

### G. Strażnik jakości/głosu zablokował
quality blocking: `no_banned_phrases`, `score_only_on_title_slide`, `mood_requires_two_sources`,
`both_countries_present`. To kontrakt głosu z `glos-redakcji.md`. Zwykle samonaprawialne przy
ponownym runie (LLM dostaje feedback i ponawia). Jeśli powtarzalne — popraw prompt w
`app/agents/media_reaction.py` (translator/editorial) lub regułę w `app/evaluation/judges.py`,
zgodnie z biblią stylu.

## Mapa plików (gdzie co naprawiać)

- `app/tools/research.py` — selekcja artykułów: `is_article_url`, `url_date_too_old`,
  `date_from_url`, `match_blob`, `looks_like_opinion`, `prefer_opinion_hits`,
  `canonical_article_key`, `collect_section_hits`, `max_articles_per_country`.
- `app/agents/media_scout.py` — prompt scouta: co jest „na temat" i „po meczu".
- `app/orchestration/coordinator.py` — halty, pozyskiwanie faktów, `FIXTURE_FALLBACK_NOTE`.
- `app/evaluation/judges.py` — strażniki: `MediaFactChecker` (m.in. `score_consistent_with_media`),
  `MediaQualityJudge`.
- `data/sources/country_media.json` — outlety, domeny, `sections` per kraj.
- `data/schedule/world_cup_2026.json` — terminarz (data/miasto/stadion meczu).
- `runbook-mecz.md` — wiedza operacyjna o timingu i oknach.

## Re-run i weryfikacja

```powershell
$env:OPENAI_API_KEY = [Environment]::GetEnvironmentVariable('OPENAI_API_KEY','User')
$env:TAVILY_API_KEY = [Environment]::GetEnvironmentVariable('TAVILY_API_KEY','User')
python -m app --match "Kraj A - Kraj B" --date YYYY-MM-DD --research --save-run --render --model gpt-4o
```
Potem powtórz Krok 1 na nowym runie i potwierdź `status: ready` + poprawny wynik. Podgląd
zhaltowanego runu bez czekania na `ready`: `python -m app.render --run-dir runs\<id> --allow-review`.

## Norma projektu: każdy fix kodu = test regresyjny

W tym projekcie każda naprawa selekcji/faktów dostała test odtwarzający feralny przypadek (zwykle
sam URL) w `tests/test_research.py`. Trzymaj się tego: dodaj test, uruchom
`python -m unittest discover -s tests -q`, utrzymaj zielono. Inaczej ta sama awaria wróci za
kilka meczów.
