# Runbook: generowanie posta po meczu

Krótka checklista na wieczór meczowy. Tor domyślny: **`media_reaction`** (reakcje mediów obu krajów).

> Stan systemu: dane wchodzą z lokalnego fixture'a (brak fetcherów live). Po meczu **człowiek wprowadza
> dane** (wynik + cytaty z whitelisty), a system je waliduje, tłumaczy (z `--llm`) i składa w karuzelę.

## 0. Jednorazowo (setup)

```powershell
pip install -e .
# opcjonalnie, jeśli chcesz, żeby system sam tłumaczył cytaty:
pip install ".[llm]"
$env:OPENAI_API_KEY = "sk-..."   # PowerShell (bieżąca sesja)

# warstwa graficzna (render karuzeli PNG):
pip install ".[render]"
python -m playwright install chromium
python scripts/fetch_render_assets.py   # fonty + flagi (jednorazowo, potem offline)
```

## 1. Po meczu: utwórz fixture

1. Skopiuj `data/fixtures/matches/_template.json` na `data/fixtures/matches/<match_id>.json`
   (`<match_id>` = nazwa pliku, np. `mexico_rpa_opener_2026`).
2. Wypełnij: `match_id` (= nazwa pliku), `aliases` (wstaw **wyróżniającą** frazę, nie samo „Kraj A - Kraj B"),
   `date`, `venue`, `teams`, `score.full_time`, `goals`, `key_events`, `evidence`.
3. Reguły, które łamią run (są w `_instrukcja` w szablonie):
   - każdy `evidence_id` w `goals`/`key_events`/`fact_source_ids` musi istnieć w `evidence[].id`;
   - fakty to Tier A: `provider: "OfflineVerifiedFixture"`, `source_tier: "A"`.

## 2. Wklej po 2 cytaty na kraj

- `media[].country` musi być **identyczne** jak `teams.home` / `teams.away`.
- `media[].outlet` musi istnieć dla tego kraju w `data/sources/country_media.json`
  (to whitelista — nieznany outlet jest po cichu odrzucany).
- `media[].url` musi być w domenie z whitelisty tego outletu.
- `translation_pl`: **wymagane bez `--llm`**. Z `--llm` wystarczy `original_text` (model przetłumaczy).

## 3. Preflight (zanim odpalisz pipeline)

```powershell
python scripts/validate_fixture.py <match_id>          # tryb deterministyczny (wymaga translation_pl)
python scripts/validate_fixture.py <match_id> --llm     # tryb LLM (translation_pl opcjonalne)
```

- `WERDYKT: GOTOWE` → możesz generować. `NIE GOTOWE` → popraw to, co ma `[FAIL]`.
- Preflight wypisze gotową komendę `Run:` z poprawnym aliasem.

## 4. Wygeneruj post

```powershell
# deterministycznie (z gold translation_pl w fixture):
python -m app --match "<wyróżniający alias>" --pretty --save-run

# z tłumaczeniem przez LLM:
python -m app --match "<wyróżniający alias>" --pretty --save-run --llm
```

Wynik to JSON pakietu: slajdy karuzeli (PL) + caption + źródła. Z `--save-run` pełny run ląduje w `runs/`.

Przy runie z LLM slajd tytułowy i caption ramuje krok redakcyjny (`media_editorial`):
hook = najmocniejsza atrybuowana teza prasy, caption = kontrast obu stron + CTA-pytanie.
Gdy krok zawiedzie, w `notes` zobaczysz `editorial: fallback szablonu ...`, a tytuł wraca do
neutralnego "X N-M Y: jak odebrały to media?" — run jest poprawny, tylko mniej charakterny.
Streszczenia slajdów prowadzą tezą outletu i nie powtarzają końcowego wyniku
(pilnuje tego sędzia `score_only_on_title_slide`).

## 5. Wyrenderuj grafiki karuzeli

Najprościej: dodaj `--render` do komendy z kroku 4 — run `ready` od razu wyrenderuje karuzelę
(implikuje `--save-run`; statusy inne niż `ready` są pomijane z podpowiedzią). Osobno:

```powershell
python -m app.render                                  # najnowszy run ze statusem ready
python -m app.render --run-dir runs/run_<id>          # konkretny run
python -m app.render --allow-review                   # podgląd także dla needs_human_review
```

PNG (1080×1350, gotowe pod IG) lądują w `runs/run_<id>/slides/`, a obok nich dwa pliki
tekstowe: `caption.txt` — gotowy opis posta IG (opis redakcyjny → pełne linki do źródeł →
hashtagi) oraz `x_post.txt` — gotowy DŁUGI post na X/Twittera, informacyjnie 1:1
z karuzelą (nagłówek i podtytuł slajdu tytułowego → pełne streszczenia każdego
cytowanego artykułu → CTA → źródła z linkami → komplet hashtagów; bez skracania —
X dopuszcza długie posty). **Obejrzyj każdy slajd przed publikacją** (to jest moment human review). Na slajdzie
źródłowym tylko outlety i domeny — pełne URL-e idą w `caption.txt`.
Szczegóły: `architektura-warstwy-graficznej.md`.

## Kiedy odpalać run live (timing ma znaczenie)

- **Nie odpalaj w trakcie ani tuż po gwizdku** — redakcje publikują reakcje 1–3 h po meczu.
  Run za wczesny → same zapowiedzi/live-blogi, które scout odrzuca → `insufficient_evidence`.
- **Optymalne okno: 2–3 h po meczu lub następny ranek.** System czyta najświeższe artykuły
  bezpośrednio ze stron sekcji redakcji (`sections` w `country_media.json`), więc świeżość
  nie zależy od indeksu wyszukiwarki.
- Wynik meczu potwierdzany jest krzyżowo (≥2 redakcje, media obu krajów). Jeżeli fakty
  wpadną z lokalnego fixture'a (fallback), run **celowo nigdy nie kończy się `ready`** —
  sprawdź wynik i zaakceptuj ręcznie albo odpal ponownie później.
- Sędzia porównuje wynik z faktów ze wzmiankami w cytatach/streszczeniach
  (`score_consistent_with_media`) — rozjazd blokuje `ready`.

## Modele i koszty

Rekomendowana komenda produkcyjna:

```powershell
python -m app --match "<mecz>" --research --save-run --date YYYY-MM-DD --model gpt-4o
```

- `--model` (jakościowy) obsługuje **tylko** treść widoczną dla odbiorcy: tłumaczenia
  cytatów i streszczenia na slajdy. Tu `gpt-4o` robi realną różnicę (polszczyzna).
- `--light-model` (domyślnie `gpt-4o-mini`) obsługuje scouty ekstrakcji: wybór cytatu
  z artykułu i odczyt wyniku. Te kroki są za twardymi walidacjami (verbatim,
  format X-Y, anty-fabrykacja, retry z feedbackiem), więc tańszy model nie obniża
  jakości — a to właśnie tam pali się większość tokenów (wiele artykułów × próby).

## Tryb live (`--research`) — opcjonalny, eksperymentalny

Zamiast ręcznego fixture'a model sam szuka faktów i cytatów w obrębie whitelisty
(search Tavily + fetch artykułu + ekstrakcja dosłownych fragmentów). Faza 1 (szkielet):
działa, ma guardraile (whitelist domen, sanityzacja, cytat = dosłowny podłańcuch),
ale realne trafianie w artykuł meczowy wymaga jeszcze hardeningu — traktuj jak beta.

```powershell
pip install ".[research,llm]"
$env:OPENAI_API_KEY = "sk-..."
$env:TAVILY_API_KEY = "tvly-..."
python -m app --match "Meksyk - RPA mundial 2026" --research --pretty --save-run
```

- `--research` implikuje `--llm`. Bez kluczy/sieci system **degraduje się** (nie crashuje):
  do fixture'a (media i fakty) lub `insufficient_evidence` (fakty), a powód jest w `notes` runu.
- Fakty live idą łańcuchem: (1) źródła oficjalne (fifa/uefa, Tier A, pojedyncze źródło,
  `confidence: medium`), (2) **korroboracja w mediach krajowych** — wynik przyjęty tylko, gdy
  ≥2 różne zaufane outlety obu krajów zgadzają się co do rezultatu, (3) lokalny fixture.
  Zawsze przejrzyj przed publikacją.
- `--time-range` (domyślnie `week`) odcina archiwalne relacje tych samych drużyn (np. mecz
  młodzieżówek sprzed lat). Dla starszych meczów zwiększ do `month`.
- `--date YYYY-MM-DD` zaostrza strażnika "inny mecz" — warto podawać, gdy drużyny grały
  ze sobą niedawno więcej niż raz.

## Statusy wyniku (`status`)

- `ready` — pakiet gotowy do dalszej obróbki graficznej.
- `needs_human_review` — coś wymaga decyzji człowieka (np. brak głosów jednego kraju, brak tłumaczenia).
- `insufficient_evidence` — za mało, by tworzyć (np. brak głosów obu krajów / mecz nierozpoznany).

## Szybki troubleshooting

| Objaw | Przyczyna | Fix |
|---|---|---|
| `media[Kraj]` → 0, `unknown_outlet` | outlet spoza whitelisty | użyj `provider_id` z `country_media.json` lub dopisz outlet |
| `media[Kraj]` → 0, `odrzucone: ...spoza whitelisty` | `url` w złej domenie | popraw `url` na domenę z whitelisty outletu |
| panel pusty / `needs_human_review` | brak `translation_pl` bez `--llm` | dodaj tłumaczenia albo odpal z `--llm` |
| resolver łapie inny mecz | alias za mało wyróżniający | dodaj dłuższą, unikalną frazę do `aliases` |
| `--llm` + brak `OPENAI_API_KEY` | brak klucza | ustaw `OPENAI_API_KEY` (inaczej fallback do gold) |

> Uwaga: tor `data_story` (post o danych) jest na razie zaszyty pod narrację PSG–Arsenal — na dziś
> trzymaj się `media_reaction`.
