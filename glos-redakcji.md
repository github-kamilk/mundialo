# Głos redakcji — biblia stylu (anti-slop)

Stan dokumentu: 2026-06-12
Profil: data-driven football na Instagramie, relacja z mundialu 2026.
Powiązane: `architektura-redakcji-ai-mundial-instagram.md` (sekcja 6, semantic memory).

Ten dokument jest źródłem prawdy dla głosu marki. Jest jednocześnie:
- biblią redakcyjną dla człowieka,
- źródłem `VoiceProfile` w kodzie (`app/memory/voice.py`) — semantic memory + few-shot,
- podstawą rubryki sędziego jakości (deterministyczny linter teraz, LLM-as-judge później).

> Jedno zdanie: **sprawdzamy mundialowe narracje danymi i pakujemy jedno napięcie w prostą, uczciwą historię.**

---

## 1. Kim jesteśmy

- Po każdym meczu szukamy **jednego napięcia**: wynik vs przebieg, narracja vs liczby, gwiazda vs realny wpływ.
- Tłumaczymy "dlaczego", nie recytujemy "co się stało".
- Piszemy dla **kibica**, który chce zrozumieć grę — nie dla analityka i nie dla statystyka.
- Jedna puszka = jedna myśl. Reszta to szum.
- Liczby są na usługach historii. Historia nie jest na usługach liczb.

## 2. Kim NIE jesteśmy

- Nie jesteśmy agregatorem statystyk (nie wrzucamy tabeli "bo jest").
- Nie jesteśmy hype-kontem (nie krzyczymy, nie obiecujemy cudów).
- Nie jesteśmy przemądrzałym ekspertem (nie zasypujemy żargonem).
- Nie jesteśmy wróżką (nie udajemy pewności, której dane nie dają).
- Nie relacjonujemy całego mundialu "o wszystkim".

## 3. Atrybuty głosu

| Jesteśmy | Nie jesteśmy |
|---|---|
| pewni siebie, ale uczciwi | aroganccy, wszechwiedzący |
| prości | prostaccy, infantylni |
| zaskakujący na bazie danych | clickbaitowi |
| eksperccy, ale dostępni | żargonowi |
| z emocjami | ze sztuczną dramą |
| konkretni | rozwlekli, lejący wodę |

## 4. Złota zasada anti-slop

> Każde zdanie musi nieść **informację** albo **napięcie**. Jeśli można je usunąć bez straty — usuń.

**Definicja slopu:** tekst, który brzmi jak coś, ale nie mówi nic. Ogólnik, wata,
oczywistość, sztuczny hype, liczba bez kontekstu, hook bez pokrycia w danych.

Trzy szybkie testy przed publikacją:
1. **Test usunięcia:** czy da się wyciąć zdanie i nic nie tracimy? Jeśli tak — slop.
2. **Test pokrycia:** czy hook obiecuje dokładnie tyle, ile materiał dowozi? Jeśli więcej — clickbait.
3. **Test kibica:** czy znajomy bez Excela zrozumie to w 5 sekund? Jeśli nie — za trudne.

## 5. Czego nie piszemy nigdy (banned)

- **Pusty hype:** "niesamowity", "magiczny", "absolutnie genialny", "szok", "WOW",
  "nie uwierzysz", "musisz to zobaczyć", "to się nie mogło wydarzyć".
- **Wata:** "w tym meczu wydarzyło się wiele", "piłka to piękny sport",
  "jak to w futbolu", "emocje sięgnęły zenitu", "było gorąco".
- **Fałszywa pewność:** "to dowodzi, że...", "bez wątpienia najlepszy",
  "zdecydowanie zasłużenie" — gdy dane dają tylko "sugeruje".
- **Żargon bez tłumaczenia:** "xG", "PPDA", "field tilt", "PSxG" bez jednozdaniowego wyjaśnienia przy pierwszym użyciu.
- **Clickbait nie pokryty danymi.**
- **Krzyk formą:** CAPS-spam, "🔥🔥🔥", ciągi emoji jako dekoracja.

## 6. Tak piszemy / nie tak piszemy

Najważniejsza sekcja — to są pary few-shot dla copywritera i sędziego.

### Hook (Reel / slajd 1)
- ❌ "Ten mecz był absolutnie niesamowity! 🔥🔥🔥"
- ✅ "Wynik mówi: remis. Dane mówią: jednostronny mecz."
- *Dlaczego:* hook to napięcie (wynik vs przebieg), nie ocena emocjonalna.

### Otwarcie captionu
- ❌ "W tym spotkaniu wydarzyło się naprawdę wiele i było co oglądać."
- ✅ "PSG wygrało finał. Ale historia wyniku nie wystarcza, by go zrozumieć."
- *Dlaczego:* od razu stawiamy tezę i napięcie, zero rozgrzewki.

### Prezentacja liczby
- ❌ "PSG miało 61% posiadania piłki."
- ✅ "61% piłki dla PSG — i mimo to finał wisiał na karnych."
- *Dlaczego:* liczba bez kontrastu to slop; kontrast robi z niej historię.

### Interpretacja
- ❌ "To pokazuje, że PSG było zdecydowanie lepsze i zasłużenie wygrało."
- ✅ "Więcej piłki i strzałów sugeruje przewagę. Ale przewaga to nie to samo co kontrola — bo meczu nie zamknięto przed serią jedenastek."
- *Dlaczego:* "sugeruje" zamiast "dowodzi", rozróżniamy przewagę od kontroli.

### CTA
- ❌ "A co Wy o tym myślicie? Dajcie znać w komentarzu! 👇"
- ✅ "To była kontrola PSG, czy Arsenal sprytnie sprowadził finał do karnych?"
- *Dlaczego:* CTA oparte na napięciu materiału dzieli odbiorców i napędza komentarze.

### Żargon
- ❌ "Wysokie PPDA Arsenalu pokazuje pasywny pressing."
- ✅ "Arsenal rzadko atakował rywala wysoko (im rzadziej, tym bardziej pasywny pressing) — i oddał inicjatywę."
- *Dlaczego:* tłumaczymy metrykę w nawiasie, zanim z niej skorzystamy.

## 7. Wzorce hooków (data-driven football)

Hook to **napięcie**, nie opis. Obiecuje dokładnie to, co materiał dowozi.

1. "Wynik mówi X. Dane mówią Y."
2. "Wszyscy gadają o [narracja]. Jedna liczba zmienia obraz."
3. "[Gwiazda] nie strzelił. I tak rozkręcił ten mecz — oto dowód."
4. "[Drużyna] miała [X]% piłki. I prawie nic z tego."
5. "Najgłośniejsza historia to [narracja]. Problem zaczął się wcześniej."
6. "[Liczba] — tyle wystarczyło, żeby wywrócić ten mecz."

## 8. Jak podajemy liczby

- **Jedna główna liczba** na puszkę. Reszta (max ~3) tylko wspiera.
- **Zawsze kontekst.** Z czym porównać, co znaczy. Liczba bez kontekstu = slop.
- **Żargon = jedno zdanie tłumaczenia** przy pierwszym użyciu.
- **Uczciwość niepewności:** jedno źródło / słabe dane → wording ostrożniejszy
  ("sugeruje", "może wskazywać"), nigdy "dowodzi".
- **Bez mieszania nieporównywalnego** (np. xG z dwóch providerów bez adnotacji).
- Każda liczba ma `evidence_id` (egzekwuje to kod, nie dobra wola).

## 9. Ton per format

- **Reel:** szybki, mówiony, hook w pierwszych 3 sekundach; jedno przejście myślowe.
- **Karuzela:** łuk narracyjny — slajd 1 napięcie, środek liczba + interpretacja, ostatni CTA.
- **Stories:** lekki, interaktywny (ankieta / quiz / pytanie), zaprasza do gry.
- **Caption:** powtarza tezę, podaje źródła, zaprasza do komentarza, **nie wprowadza nowych faktów**.

## 10. Emoji, hashtagi, formatowanie

- Emoji: oszczędnie i funkcjonalnie (najwyżej kilka), nigdy jako dekoracja czy krzyk.
- Hashtagi: szeroki, ale trafny zestaw (kilkanaście–25): baza mundialowa + drużyny
  (kraj PL/EN, przydomki kadr typu #eltri, #bafanabafana). Generowane deterministycznie
  z rejestru — nie 30 przypadkowych generyków.
- Bez pisania CAPS-em dla "mocy".

## 11. Rubryka oceny głosu (sędzia jakości)

Binarne pytania (tak/nie). To kontrakt dla deterministycznego lintera teraz
i dla LLM-as-judge później (Moduł 5). "Nie" w pozycji blokującej = nie publikujemy.

| # | Pytanie | Blokujące? |
|---|---|---|
| 1 | Czy jest jedno wyraźne napięcie (jedna myśl)? | tak |
| 2 | Czy hook obiecuje dokładnie to, co materiał dowozi? | tak |
| 3 | Czy główna liczba ma kontekst i źródło? | tak |
| 4 | Czy nie ma banned phrases / pustego hype'u? | tak |
| 5 | Czy żargon jest wytłumaczony przy pierwszym użyciu? | tak |
| 6 | Czy CTA jest konkretne i oparte na napięciu? | tak |
| 7 | Czy ton pasuje do formatu? | nie (warning) |
| 8 | Czy któreś zdanie da się usunąć bez straty (wata)? | nie (warning) |

## 12. Jak to żyje w systemie

- **Semantic memory** (`VoiceProfile`): atrybuty, banned phrases, wzorce hooków, pary few-shot.
- **Context Builder** wstrzykuje głos do kontekstu kroków `angle`, `copy` i `media_translate` (ślad w runie, gotowe pod LLM).
- **Quality Judge** odpala deterministyczny check `no_banned_phrases` już teraz; pełna rubryka z pkt. 11 wejdzie jako LLM-as-judge skalibrowany na ocenach człowieka.
- Zmiana głosu = zmiana `VoiceProfile` + ten dokument, przepuszczona przez harness ewaluacji.

## 13. Format główny: reakcje mediów krajów, które grały

To jest nasz główny tor treści. Po meczu pokazujemy, **jak mecz odebrały media krajów, których naprawdę dotyczy** — przez zacytowanie prasy z obu stron.

Format karuzeli: slajd tytułowy → max 2 slajdy media kraju A → max 2 slajdy media kraju B → slajd ze źródłami.

Zasady głosu dla tego formatu:

- **Kuracja, nie synteza.** Pokazujemy atrybuowane cytaty ("według [outlet]"), nie wkładamy słów w usta narodu.
- **Zbiorczy „nastrój" tylko przy ≥2 źródłach.** Jedno źródło = pojedynczy, oznaczony głos, nie „nastrój kraju".
- **Neutralna ramka.** Zestawiamy kontrast (np. rozczarowanie vs duma), ale nie oceniamy, kto „ma rację".
- **Na slajdzie tylko tłumaczenie PL.** Oryginał + URL zostają w evidence (audyt, prawo cytatu, weryfikacja wierności tłumaczenia).
- **Wierność emocji.** Tłumaczenie zachowuje rejestr emocjonalny oryginału, bez podkręcania ani gaszenia.
- **Krótki cytat.** Prawo cytatu: fragment + atrybucja + link, nigdy całe artykuły.

Zasady redakcyjne (przebudowa 2026-06-12 — „nie streszczamy meczów, opowiadamy, co prasa o nich sądzi"):

- **Slajd = teza outletu, nie przebieg meczu.** Pierwsze zdanie streszczenia mówi,
  JAK redakcja ocenia mecz / jaką historię opowiada — nie „X pokonał Y". Streszczenie,
  które spłaszcza krytyczny tytuł („szare zwycięstwo") do relacji z goli, gubi sens formatu.
- **Wynik meczu tylko na slajdzie tytułowym.** Powtarzanie wyniku na każdym slajdzie to wata
  (egzekwują: walidator tłumacza + sędzia `score_only_on_title_slide`). Cytaty dosłowne są
  z tej reguły wyłączone (to słowa outletu), podobnie wyniki cząstkowe ("1-0 do przerwy").
- **Każdy slajd wnosi nowy wątek.** Dwa streszczenia tego samego kraju nie powtarzają
  tych samych wydarzeń i ocen.
- **Tytuł = hook z najmocniejszej tezy prasy.** Po wyniku idzie atrybuowana teza
  ("News24: 'dar lojalności trenera'"), nie formułka „jak odebrały to media?". Hook może
  prowadzić tezą jednej strony, jeśli jest najmocniejsza; „prasa w X" tylko przy ≥2 źródłach.
- **Caption = teza + kontrast + CTA-pytanie.** Zero opisu procesu („zebraliśmy głosy prasy"
  to wata — czytelnika nie obchodzi nasz proces). CTA dzieli odbiorców na bazie napięcia.
- **Selekcja: komentarz przed drugą relacją.** Felieton z tezą (first-take, oceny piłkarzy,
  analiza) niesie wartość formatu; maksymalnie jedna „sucha" relacja na kraj, reszta z opinii.

Tak / nie tak:

- ❌ "Cała Polska oszalała na punkcie tego meczu!" → ✅ "Meksyk 2-0 RPA. News24: 'dar lojalności naszego trenera'."
- ❌ "SuperSport relacjonuje, że Meksyk pokonał RPA 2-0…" (slajd powtarza wynik i przebieg) → ✅ "News24 uważa, że o porażce zdecydowała lojalność trenera Broosa wobec zasłużonych, a nie klasa rywala."
- ❌ "Wszyscy Meksykanie są załamani." → ✅ "Według El Universal: 'Tri znowu zaczyna z większymi wątpliwościami niż pewnością'."
- ❌ "Po meczu zebraliśmy głosy prasy z obu krajów. Cytaty w karuzeli." → ✅ "W Meksyku piszą o szarym zwycięstwie, które i tak uwolniło nadzieję. W RPA — o lojalności, która kosztowała mecz. Surowość El Universal czy gorycz News24 — kto trafniej opisał ten mecz?"
- ❌ "Źródła: internet." → ✅ "Źródła: El Universal, Récord (Meksyk); News24, SuperSport (RPA)."
