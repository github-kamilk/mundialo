# Pomysly / backlog

Lista rzeczy do zrobienia poza MVP. Nie roadmapa operacyjna (od tego jest
`scripts/roadmap.py`, ktory pokazuje stan renderow per mecz), tylko pomysly na
rozwoj systemu. Format wpisu: tytul, status, problem, propozycja, kryteria
akceptacji, powiazane pliki.

## Leaderboard z historia i diffem (persisted eval)

Status: pomysl

Problem: dzis "leaderboard" to tylko funkcja punktujaca (`Leaderboard.score_run`:
40 fact-check + 40 quality + 20 angle >=7). Wyniki nie sa nigdzie zapisywane -
`python -m app.evaluation.reports` drukuje JSON na stdout i tyle. `evaluate_scenario`
bierze z `LeaderboardRow` tylko `.score` i wyrzuca rozbicie na osie
(status / fact_check / quality). Brak historii i porownania wersji = nie da sie
odpowiedziec "czy zmiana promptu/modelu poprawila, czy pogorszyla jakosc", a to
jest istota leaderboardu (metryki zamiast zgadywania).

Propozycja:
- zapisywac kazdy przebieg harnessu do `runs/eval_<timestamp>.json` (pelny raport:
  summary + per-scenariusz status/checki/score/blocking);
- prosty diff wzgledem poprzedniego przebiegu: scenariusze, ktore zmienily
  passed/score (regresje na czerwono, poprawy na zielono);
- zachowac w raporcie pelny `LeaderboardRow` (status + fact_check + quality + score),
  a nie sam `.score`, zeby widac bylo, na ktorej osi spadlo;
- opcjonalnie: etykieta wersji (prompt/model) przy przebiegu, zeby zestawiac
  "wariant A vs wariant B".

Kryteria akceptacji:
- po uruchomieniu harnessu powstaje plik raportu na dysku;
- drugie uruchomienie pokazuje diff wzgledem pierwszego (co sie zmienilo);
- regresja w dowolnym scenariuszu jest widoczna jako jawny wpis, nie tylko spadek
  zbiorczego pass_rate.

Powiazane: `app/evaluation/leaderboard.py`, `app/evaluation/reports.py`,
`tests/test_hardening.py`, katalog `runs/`.

## Tryby publikacji: pre_match / daily_roundup

Status: pomysl (knob usuniety jako martwy 2026-06-17)

Problem: `MatchRequest.mode` (pre_match / post_match / daily_roundup) istnial jako
pole + walidacja + argument CLI, ale zadna logika sie na nim nie rozgalezala -
pipeline zawsze szedl sciezka pomeczowa. Usuniete, zeby CLI nie udawalo opcji,
ktorych nie ma.

Propozycja (gdyby wracac): osobny tor zapowiedzi (pre_match: sklady, forma, stawka
bez wyniku) i dzienne podsumowanie (daily_roundup: kilka meczow). To realne typy
postow z architektury, nie wariant istniejacego toru - wymagaja wlasnych agentow
i kontraktu wyjscia, wiec wchodza jako nowy `post_type`, a nie flaga.

## Tryb szybki vs jakosciowy (speed_mode)

Status: pomysl (knob usuniety jako martwy 2026-06-17)

Problem: `MatchRequest.speed_mode` (fast_45_min / quality_90_min) byl knobem bez
zadnego efektu na pipeline.

Propozycja: realna dzwignia kosztu/latency - fast wymusza lekki model wszedzie,
mniejszy `max_select` w kuratorze i mniej retry; quality zostawia obecne ustawienia.
Wpiac dopiero, gdy pojawi sie presja czasu/kosztu (post tuz po gwizdku).
