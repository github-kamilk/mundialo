# Architektura warstwy graficznej (Instagram)

Stan dokumentu: 2026-06-12
Cel: zamienic `MediaReactionPackage` (glowny tor: reakcje mediow obu krajow) na gotowe pliki PNG karuzeli do publikacji na Instagramie, deterministycznie i autonomicznie.

## 1. Problem

Pipeline redakcyjny zwraca strukture (slajdy karuzeli z tlumaczeniami/streszczeniami prasy obu krajow), ale nie grafiki. Rozwazane opcje:

1. Generowanie obrazkow modelem AI (gpt-image, Nano Banana, Ideogram) na bazie szablonu + tekstu.
2. Programowe nanoszenie tekstu na grafike po pikselach (np. Pillow).
3. Rezygnacja z Instagrama na rzecz X/Twittera (sam tekst).
4. Szablony HTML/CSS renderowane do PNG headless przegladarka.

## 2. Ocena opcji

### Opcja 1: czysty AI image gen — ODRZUCONA jako glowna sciezka

- Modele 2026 (GPT Image 2/1.5, Nano Banana 2/Pro, Ideogram 4) renderuja tekst duzo lepiej niz rok temu, ale nadal nie gwarantuja poprawnosci typografii — a nasze slajdy to dlugie polskie streszczenia prasy (5-8 zdan, polskie znaki, nazwiska typu Quinones/Sithole). Jedna przekrecona litera w nazwisku albo cytacie podwaza caly pipeline fact-check i prawo cytatu.
- Cytat na slajdzie MUSI byc znak-w-znak tym, co zatwierdzil `MediaFactChecker`. Generator dyfuzyjny tego nie gwarantuje z definicji.
- Brak spojnosci wizualnej miedzy postami bez walki z promptami.

### Opcja 2: Pillow / rysowanie po pikselach — ODRZUCONA

- Zrodlo intuicji "programowy tekst na grafice = brzydki". Racja, ale dotyczy rastra: brak layoutu, lamanie linii i skalowanie fontu trzeba pisac recznie.

### Opcja 3: przejscie na X/Twittera — ODRZUCONA

- Format "obie strony w jednej karuzeli" (tytul -> media kraju A -> media kraju B -> zrodla) jest stworzony pod karuzele IG, nie pod thread.
- IG bardziej reprezentacyjny jako portfolio projektu (decyzja kierownika).
- X moze byc kanalem wtornym (repost tych samych grafik) — zerowy koszt dodatkowy.

### Opcja 4: HTML/CSS -> PNG (headless Chromium) — WYBRANA

Standard branzowy automatycznych grafik social media (tak pod spodem dzialaja Bannerbear, Placid, Templated, HTMLCSSToImage).

- Deterministyczne: ten sam pakiet = ten sam piksel. Tekst zatwierdzony przez sedziow trafia na slajd bez zadnej transformacji.
- Ladne: typografia webowa, flexbox/grid, gradienty. Jakosc ograniczona tylko jakoscia szablonu CSS, robionego RAZ.
- Darmowe i lokalne: Playwright w Pythonie, zero kosztow per-render.
- Wersjonowane: szablony to pliki w repo.

## 3. Architektura

```
runs/<run_id>/run.json  (media_package, status: ready | needs_human_review)
        |
        v
  app/render/specs.py     build_slide_specs(media_package) -> list[SlideSpec]
        |                 (czysta funkcja, testowalna offline; wzbogaca slajdy
        |                  karuzeli o dane z paneli: outlet, tier, kraj, flaga)
        v
  app/render/templates/   Jinja2: base.html + title / media_country / sources
        |
        v
  app/render/renderer.py  Playwright chromium, viewport 1080x1350,
        |                 device_scale_factor=2, czeka na fonts.ready + autofit
        v
  runs/<run_id>/slides/slide_01.png ... slide_NN.png
```

### Typy slajdow (zgodne z rolami `CarouselSlide` toru medialnego)

1. `title` — okladka: etykieta "REAKCJE MEDIOW", wynik (druzyny + score z `MatchFacts`), flagi obu krajow, data; headline/body z `title_slide`.
2. `media_country` — serce formatu: chip kraju (flaga + nazwa), nazwa outletu + badge tieru, streszczenie `summary_pl`/`body` (5-8 zdan, autofit fontu), stopka z domena zrodla i numeracja slajdu.
3. `sources` — lista outletow z domenami i kraje; pelne URL-e zostaja w caption/EvidenceStore (na grafice nieklikalne i nieczytelne).

Tor `data_story` (wtorny): poza zakresem tego etapu; dojdzie jako osobne szablony (wykres SVG), gdy bedzie potrzebny.

### Wzbogacanie speca (specs.py)

- Slajd `media_country` jest dopasowywany do `MediaQuote` z paneli po `evidence_id` w `claim_ids` (fallback: outlet w headline) -> stad outlet, tier, url->domena, kraj, jezyk oryginalu.
- Flagi: `data/sources/country_media.json` ma `iso2` per kraj -> emoji flagi (regional indicators). Brak kraju w rejestrze -> brak flagi (nie zgadujemy).
- Render domyslnie tylko dla statusu `ready`; `--allow-review` pozwala renderowac `needs_human_review` (czlowiek i tak oglada przed publikacja).

### Kluczowe ustawienia techniczne

- Format IG: 1080x1350 (4:5); pierwszy slajd ustala ratio calej karuzeli w API.
- `device_scale_factor=2` -> PNG 2160x2700, ostra typografia po kompresji IG.
- Autofit: skrypt w szablonie zmniejsza font-size streszczenia az tekst miesci sie w kontenerze, potem ustawia flage gotowosci; renderer czeka na flage + `document.fonts.ready`.
- Fonty: lokalne pliki w `app/render/assets/fonts/` (render offline, deterministyczny); fallback na fonty systemowe.

## 4. Publikacja (etap pozniejszy)

Instagram Content Publishing API (Graph API):

- Wymagania: konto Business/Creator + strona FB, aplikacja Meta Developer, `instagram_basic` + `instagram_content_publish`; bez App Review dziala do 25 testerow (wystarczy dla projektu jednoosobowego).
- Karuzela: do 10 slajdow; kontener-dziecko per slajd -> kontener-rodzic -> publish; obrazy musza byc pod publicznym URL.
- Limit 100 postow API/24h — bez znaczenia przy naszej skali.
- Do tego czasu: publikacja reczna z `runs/<run_id>/slides/` (spojne z runbookiem: human review przed publikacja).

## 5. Kolejnosc prac

1. [x] `build_slide_specs` + szablony `title`/`media_country`/`sources` + renderer Playwright.
2. [x] CLI: `python -m app.render --run-dir runs/<run_id>` (najnowszy run domyslnie).
3. [x] Render proof na runie wzorcowym `run_20260611223055` (Meksyk 2-0 RPA).
4. [ ] Szlif brandu: paleta, logo, ewentualne tla (assety AI generowane raz, recznie zatwierdzone, bez tekstu w obrazie).
5. [x] Spiecie z pipeline'em: flaga `--render` w `python -m app` (implikuje --save-run; renderuje tylko `ready`, blad renderu nie psuje runu; autofit rosnie i kurczy tekst).
6. [ ] Szablony toru `data_story` (wykresy SVG).
7. [ ] (Pozniej) publikacja przez Graph API albo Buffer/Later.

## 6. Alternatywy odrzucone z notatka "kiedy wrocic"

- Bannerbear / Placid / Templated / Orshot: SaaS edytor + API, od ~$19-49/mies. Wrocic, gdyby utrzymanie wlasnych szablonow CSS bylo uciazliwe.
- HTMLCSSToImage / APITemplate: nasz renderer jako API. Wrocic przy problemach z deploymentem lokalnego Chromium.
- AI image gen (Ideogram/Nano Banana Pro): tylko assety brandowe generowane jednorazowo, nigdy tekst redakcyjny ani sciezka krytyczna.
