# Mundial Redakcja AI (archiwalny README PL)

> **Uwaga:** ten dokument opisuje stan projektu z fazy MVP (czerwiec 2026,
> fixture'y zamiast żywego retrievalu) i został zachowany jako zapis historyczny.
> Aktualny opis systemu: [README.md](README.md) (EN). Szczegóły architektury po
> polsku: `docs/design/architektura-*.md`.

Uruchamialny MVP systemu opisanego w `docs/design/architektura-redakcji-ai-mundial-instagram.md`.

System ma **dwa tory tresci**, wybierane przez `MatchRequest.post_type`:

- **`media_reaction` (glowny, domyslny)** — „jak mecz odebraly media obu krajow?".
  Karuzela: slajd tytulowy -> max 2 slajdy kraj A -> max 2 slajdy kraj B -> slajd ze
  zrodlami. Szczegoly: `docs/design/architektura-relacje-medialne.md`.
- **`data_story` (wtorny)** — pelny pipeline danych:

```text
Mecz -> fakty -> dane -> narracje -> napiecie -> angle -> copy -> fact-check -> puszka
```

## Szybki start

Glowny tor (reakcje mediow):

```powershell
python -m app --match "Meksyk - RPA mecz otwarcia mundialu 2026" --pretty
```

Wtorny tor (post o danych):

```powershell
python -m app --match "PSG - Arsenal, final Ligi Mistrzow 2026" --post-type data_story --pretty
```

Zapis runu:

```powershell
python -m app --match "Meksyk - RPA" --pretty --save-run
```

Testy:

```powershell
python -m unittest discover -s tests
```

## Co jest w MVP

- kontrakty danych przez dataclasses i walidatory domenowe;
- lokalny `ToolGateway` z fixture-providerem, ktory egzekwuje `SourceRegistry`
  (integralnosc provider/tier, regula "Tier C != fakt");
- `EvidenceStore` pilnujacy, zeby claimy mialy zrodla, i wykrywajacy konflikty zrodel;
- role redakcyjne: researcher, data hunter, narrative scout, metric analyst, angle editor, copywriter;
- glos redakcji jako semantic memory (`VoiceProfile` z `docs/design/glos-redakcji.md`):
  atrybuty, banned phrases (w tym nadmierne uogolnienia typu "caly kraj"), wzorce
  hookow, pary few-shot (osobne dla toru medialnego); wstrzykiwany bezposrednio do
  agentow LLM (`copy` / `media_translate` / `media_editorial`);
- **tor medialny**: rejestr outletow per kraj z `data/sources/country_media.json` (48 reprezentacji,
  min. 2 outlety/kraj, `confidence` + `verified_at`; whitelist domen + tier, `MEDIA_REACTION`),
  `fetch_media_reactions` (sanitize + walidacja domeny), tlumaczenie PL dwiema sciezkami
  (`FixtureTranslator` offline / `LlmMediaTranslator` za `ModelGateway` z anty-fabrykacja,
  anti-slop i regula >=2 zrodla dla "nastroju"), asembler karuzeli i `validate_media`;
  oryginal + URL zostaja w `EvidenceStore`, na slajdzie tylko tlumaczenie;
- fact-check i quality judge z deterministycznym anti-slop linterem (`no_banned_phrases`);
- copywriter na LLM za `ModelGateway` (provider-agnostyczny): structured output +
  walidacja + retry z feedbackiem, guardraile (claim_ids tylko z EvidenceStore = zero
  halucynacji zrodel, anti-slop, struktura puszki) i **deterministyczny fallback**;
  domyslnie bez modelu = sciezka deterministyczna (testy bez kluczy, `FakeModelGateway`);
- harness ewaluacji z **asercjami** (`expected_status`, `must_have_checks`,
  `must_fail_checks`, `expected_blocking`, `forbidden_terms_in_copy`) + raport pass/fail i pass_rate;
- CLI zwracajace JSON;
- testy regresji dla resolvera, angle scoringu, fact-checku, integralnosci zrodel,
  graceful degradation i kontraktu puszki;
- zapis pelnego runu do `runs/` (request, evidence, tool_calls, notes, status) do podgladu i debugowania.

Uruchomienie harnessu ewaluacji:

```powershell
python -m app.evaluation.reports
```

## Pokrycie scenariuszy

Harness obsluguje oba tory (po `post_type` w scenariuszu). Scenariusze pokrywaja realne,
rozne sciezki kodu (a nie atrapy):

Tor danych (`data_story`):

| Scenariusz | Sprawdza | Status |
|---|---|---|
| `psg_arsenal_ucl_final_2026` | pelna sciezka -> `ready` | pokryte |
| `synthetic_possession_trap` | slaby angle (<7/10) -> `needs_human_review` | pokryte |
| `unknown_match` | brak meczu -> `insufficient_evidence` | pokryte |
| `conflicting_sources` | konflikt zrodel blokuje `ready` | pokryte |
| `missing_metrics` | brak metryk degraduje sie zamiast crashowac | pokryte |

Tor medialny (`media_reaction`):

| Scenariusz | Sprawdza | Status |
|---|---|---|
| `media_mexico_rpa_happy` | 2 kraje, >=2 zrodla, whitelist -> `ready` | pokryte |
| `media_one_country_missing` | brak glosow jednego kraju -> `needs_human_review` | pokryte |
| `media_offwhitelist_dropped` | URL spoza whitelisty odrzucony -> kraj pusty | pokryte |
| `media_no_sources` | brak glosow obu krajow -> `insufficient_evidence` | pokryte |
| `media_injection_sanitized` | injection w artykule zneutralizowany -> `ready` | pokryte |

Pozostale typy z dokumentu (czerwona kartka, kontrowersja sedziowska, kontekst
awansu z grupy, gwiazda bez gola, dywersyfikacja sciezki `ready`) sa **zablokowane
na de-hardkodowaniu warstwy copy/angle** i zostana dodane razem z wpieciem LLM.
Tworzenie 20 zielonych fixture'ow na zaszytych szablonach byloby tym samym teatrem,
ktorego unikamy.

## Aktualne ograniczenia (stan: czerwiec 2026, MVP)

- Fetchery FIFA/UEFA/FotMob oraz pozyskiwanie reakcji mediow sa zastapione lokalnymi
  fixture'ami; realna warstwa zbierania (search w obrebie whitelisty + fetch URL za
  bramka) jest odlozona (wymaga kluczy). *(Pozniej wdrozone: `--research` + Tavily.)*
- Tor medialny w produkcji wymaga modelu do tlumaczenia (`LlmMediaTranslator`); offline
  korzysta z `translation_pl` z fixture (gold). Bez modelu i bez gold -> `needs_human_review`.
- Nie ma automatycznej publikacji na Instagramie.
- Copywriter LLM jest gotowy za bramka (`ModelGateway`), ale wlaczasz go opcjonalnie:
  `pip install '.[llm]'`, ustaw `OPENAI_API_KEY` i podaj `OpenAiModelGateway` do
  `EditorInChiefCoordinator(model_gateway=...)`. Angle scoring jest wciaz
  deterministyczny (LLM angle = kolejny plaster, ta sama infrastruktura).
- Wiedza systemu pochodzi z prasy (retriever mediow pod `--research`: filtry whitelisty +
  selekcja LLM); nie ma osobnej wewnetrznej bazy wiedzy ani embeddingow.
- Fixture PSG-Arsenal jest lokalnym snapshotem testowym, a nie live scrapingiem.
