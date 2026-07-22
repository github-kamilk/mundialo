"""Glos redakcji jako semantic memory (zrodlo: glos-redakcji.md).

To jest maszynowa wersja biblii stylu: atrybuty glosu, banned phrases, wzorce
hookow i pary few-shot. Konsumuja ja: QualityJudge (deterministyczny anti-slop
linter) oraz agenci LLM (copywriter, media-translator/editorial), ktorym glos
jest wstrzykiwany przez koordynatora.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


def fold_ascii(text: str) -> str:
    """Sprowadza tekst do ASCII (usuwa diakrytyki), zeby matchowanie banned phrases
    dzialalo niezaleznie od polskich znakow - a same stringi i output zostaja ASCII."""
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()


def banned_hits(text: str, banned_phrases: list[str]) -> list[str]:
    """Trafienia banned phrases w tekscie - od POCZATKU SLOWA, nie podlancuchowo.

    Dopasowanie podlancuchowe dawalo falszywe alarmy: 'wow' lapalo sie w srodku
    zwyklych slow (np. 'Lewandowowi'), a takiego trafienia model nie umie 'poprawic'
    przy retry, bo slowo bywa nieusuwalne (nazwisko w cytacie).

    Fraza musi zaczynac sie na granicy slowa, ale na koncu dopuszczamy krotka
    koncowke fleksyjna - lista zawiera rdzenie ('niesamowit', 'magiczn'), ktore
    inaczej nigdy nie zlapalyby form odmienionych ('niesamowite', 'magiczny').
    """
    folded = fold_ascii(text)
    hits = set()
    for phrase in banned_phrases:
        folded_phrase = fold_ascii(phrase).strip()
        if not folded_phrase:
            continue
        # po fold_ascii wszystko jest ASCII lowercase, wiec [a-z] pokrywa
        # tez sfoldowane polskie znaki ('niesamowitą' -> 'niesamowita')
        suffix = r"[a-z]{0,4}" if folded_phrase[-1].isalpha() else ""
        pattern = rf"(?<![a-z0-9]){re.escape(folded_phrase)}{suffix}(?![a-z0-9])"
        if re.search(pattern, folded):
            hits.add(phrase)
    return sorted(hits)


# Wzorzec wyniku w tekscie (2-0, 2:1); lookaround odcina fragmenty dat (2026-06-11).
_SCORE_PAIR_RE = re.compile(r"(?<![\d-])(\d{1,2})\s*[-:]\s*(\d{1,2})(?![\d-])")

# Markery wyniku DO PRZERWY / czastkowego: liczba w takim zdaniu nie jest wynikiem
# koncowym, wiec strazniki "nie powtarzaj wyniku" maja je pomijac. Bez tego
# streszczenie typu "pierwsza polowa zakonczyla sie 0-0" myli straznika
# (Francja-Senegal 3-1 vs 0-0 do przerwy).
_HALFTIME_MARKERS = (
    "do przerwy", "po przerwie", "przed przerwa", "przerwie", "pierwsza polow",
    "pierwszej polow",  # PL
    "mi-temps", "a la pause", "premiere periode", "premier acte",  # FR
    "primer tiempo", "al descanso", "primera parte", "primera mitad",  # ES
    "primeiro tempo", "ao intervalo", "intervalo", "primeira parte",  # PT
    "half-time", "halftime", "half time", "first half", "at the break",
    "at the interval",  # EN
    # DE: substring 'halbzeit' lapie 'zur Halbzeit', 'Halbzeitstand', 'erste Halbzeit'
    "halbzeit", "zur pause", "pausenstand",
    # NL: 'rust' bez kontekstu jest za krotkie ('frustratie'), wiec pelne frazy
    "eerste helft", "bij rust", "naar rust", "de rust in", "ruststand",
    # NO/DK ('forste' po zamianie o-slash -> o w _is_non_final_score_sentence)
    "til pause", "etter pause", "forste omgang", "halvleg",
    # SV: substring 'halvtid' lapie 'i halvtid', 'vid halvtid', 'halvtidsvila'
    "halvtid",
)

# Markery SERII RZUTOW KARNYCH: wynik karnych (np. "4-3 w karnych") to nie wynik
# w regulaminowym czasie, wiec strazniki "nie powtarzaj/konflikt wyniku" maja go
# pomijac - inaczej recap "wygrali 4-3 w karnych" przy koncowym 1-1 daje
# score_consistent_with_media false-positive (Niemcy-Paragwaj run_20260630100914).
# Celowo unikamy golego "penal"/"penalty"/"Elfmeter" (rzut karny w grze) - bierzemy
# tylko frazy specyficzne dla serii (liczba mnoga / "tanda" / "shootout" / "tirs au but").
_SHOOTOUT_MARKERS = (
    "rzutow karnych", "rzutach karnych", "rzuty karne", "rzutami karnymi",
    "serii rzutow", "konkursie rzutow", "konkurs rzutow",  # PL
    "tanda", "por penales", "los penales", "en penales", "de penales",
    "definicion por penal", "penaltis",  # ES (penaltis tez PT)
    "penais", "penalidades", "marcas da cal",  # PT
    "tirs au but", "seance de tirs", "aux tirs",  # FR
    "penalty shootout", "shootout", "on penalties", "spot-kick", "spot kick",
    "from the spot",  # EN
    "elfmeterschiess",  # DE (po ß->ss); golego "elfmeter" NIE bierzemy (karny w grze)
    "strafschoppen", "penaltyserie",  # NL (seria karnych; goly 'penalty' to karny w grze)
    "straffesparkkonkurranse", "straffesparkskonkurrence", "etter straffer",  # NO/DK
    "pa straffar", "strafflaggning",  # SV (po foldzie: 'på straffar', 'straffläggning')
)

# Markery DOGRYWKI / wyniku PO 90 MINUTACH (faza pucharowa): zdanie "po 90 minutach
# bylo 1-1, w dogrywce padl zwyciesk gol" niesie wynik CZASTKOWY - przy koncowym 2-1
# (po dogrywce) strazniki mialyby ten sam false-positive co przy wyniku do przerwy.
# Konwencja systemu: wynik koncowy OBEJMUJE dogrywke (karne osobno, patrz
# _SHOOTOUT_MARKERS), wiec celowo NIE markujemy "po 120 minutach" - to juz wynik
# koncowy, a jego pominiecie oslabiloby korroboracje (np. _resurrect_gate_rejected).
_EXTRA_TIME_MARKERS = (
    "dogryw", "po 90 minut", "regulaminowym czasie", "regulaminowego czasu",  # PL
    "extra time", "after 90 minutes", "normal time", "in regulation",  # EN
    "prolongation", "temps reglementaire",  # FR (po foldzie: 'réglementaire')
    # ES/PT: 'prorroga' lapie tez 'prorrogacao' (BR), 'suplementar' tez 'suplementario'
    "prorroga", "tiempo reglamentario", "los 90 minutos", "tiempo extra",
    "alargue", "suplementar",  # ES (alargue: AR/UY)
    "prolongamento", "tempo regulamentar",  # PT-PT
    "verlangerung", "nach 90 minuten",  # DE (po foldzie: 'Verlängerung')
    "verlenging",  # NL
    "ekstraomgang",  # NO/DK (lapie tez 'ekstraomgangene')
    "forlangning",  # SV (po foldzie: 'förlängning(en)')
)

# Zdania rozbijamy na '.;!?\n' - ale NIE na ',' (FR "mi-temps, 0-0" trzyma marker przy
# wyniku) ani ':' (to separator wyniku, 2:1). Marker do-przerwy i wynik zyja w jednym zdaniu.
_SENTENCE_SPLIT_RE = re.compile(r"[.;!?\n]")


def _is_non_final_score_sentence(sentence: str) -> bool:
    # Zdanie o wyniku DO PRZERWY, z SERII RZUTOW KARNYCH albo o wyniku PO 90 MINUTACH
    # przed DOGRYWKA - liczba w nim nie jest wynikiem koncowym, wiec strazniki
    # "nie powtarzaj/konflikt wyniku" maja je pomijac (np. "4-3 w karnych" przy
    # koncowym 1-1 albo "po 90 minutach 1-1" przy koncowym 2-1 po dogrywce).
    # fold_ascii (NFKD) nie rozklada liter z przekreslona kreska - polskiego 'l'
    # (U+0142), niem. 'ß' ani skand. 'o z ukosnikiem' (U+00F8) - mapujemy recznie
    # ('ß'->'ss' dla "Elfmeterschießen", 'ø'->'o' dla "første omgang").
    folded = fold_ascii(sentence).replace("ł", "l").replace("ß", "ss").replace("ø", "o")
    return (
        any(marker in folded for marker in _HALFTIME_MARKERS)
        or any(marker in folded for marker in _SHOOTOUT_MARKERS)
        or any(marker in folded for marker in _EXTRA_TIME_MARKERS)
    )


def mentioned_scores(text: str) -> set[tuple[str, str]]:
    """Pary wynikow wzmiankowane w tekscie, z pominieciem zdan o wyniku CZASTKOWYM.

    Wspolny prymityw dla wszystkich straznikow "nie powtarzaj wyniku koncowego"
    (per-artykul w torze medialnym i blokujacy QualityJudge): wynik DO PRZERWY
    ('1-0 do przerwy', fr. 'mi-temps'), wynik SERII RZUTOW KARNYCH ('4-3 w
    karnych', es. 'por penales') ani wynik PO 90 MINUTACH przed dogrywka
    ('po 90 minutach 1-1', de. 'nach 90 Minuten') to nie wynik koncowy -
    to tresc, nie powtorka/konflikt wyniku z naglowka, nawet gdy liczbowo rozni
    sie od wyniku koncowego. Cytaty doslowne wylacza wywolujacy (to slowa outletu,
    nie nasze).
    """
    found: set[tuple[str, str]] = set()
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if _is_non_final_score_sentence(sentence):
            continue
        for a, b in _SCORE_PAIR_RE.findall(sentence):
            if int(a) <= 15 and int(b) <= 15:
                found.add((a, b))
    return found


@dataclass(frozen=True)
class VoicePair:
    context: str
    slop: str
    ours: str
    why: str


@dataclass(frozen=True)
class VoiceProfile:
    positioning: str
    tone_do: list[str]
    tone_dont: list[str]
    banned_phrases: list[str]
    hook_archetypes: list[str]
    examples: list[VoicePair]
    judge_rubric: list[str]

    def few_shot(self, context: str | None = None) -> list[VoicePair]:
        if context is None:
            return list(self.examples)
        return [pair for pair in self.examples if pair.context == context]


DEFAULT_VOICE_PROFILE = VoiceProfile(
    positioning=(
        "Sprawdzamy mundialowe narracje danymi i pakujemy jedno napiecie "
        "w prosta, uczciwa historie dla kibica."
    ),
    tone_do=[
        "pewni siebie, ale uczciwi",
        "prosci",
        "zaskakujacy na bazie danych",
        "eksperccy, ale dostepni",
        "z emocjami, bez sztucznej dramy",
        "konkretni",
    ],
    tone_dont=[
        "aroganccy, wszechwiedzacy",
        "prostaccy, infantylni",
        "clickbaitowi",
        "zargonowi",
        "rozwlekli, lejacy wode",
    ],
    # ASCII-only; matchujemy po fold_ascii(), wiec lapie tez warianty z diakrytykami
    # (np. "zobaczyc" zlapie "zobaczyć"). Output i slad runu zostaja ASCII.
    banned_phrases=[
        "niesamowit",
        "magiczn",
        "absolutnie",
        "szok",
        "nie uwierzysz",
        "musisz to zobaczyc",
        "wydarzylo sie wiele",
        "to dowodzi",
        "bez watpienia",
        "wow",
        # nadmierne uogolnienia (tor medialny: nie oceniamy emocji calego narodu)
        "caly kraj",
        "cala polska",
        "caly narod",
        "wszyscy kibice",
        "wszyscy meksykanie",
    ],
    hook_archetypes=[
        "Wynik mowi X. Dane mowia Y.",
        "Wszyscy gadaja o [narracja]. Jedna liczba zmienia obraz.",
        "[Gwiazda] nie strzelil. I tak rozkrecil mecz - oto dowod.",
        "[Druzyna] miala [X]% pilki. I prawie nic z tego.",
        "Najglosniejsza historia to [narracja]. Problem zaczal sie wczesniej.",
        "[Liczba] - tyle wystarczylo, zeby wywrocic mecz.",
    ],
    examples=[
        VoicePair(
            context="hook",
            slop="Ten mecz byl absolutnie niesamowity! 🔥🔥🔥",
            ours="Wynik mowi: remis. Dane mowia: jednostronny mecz.",
            why="hook to napiecie, nie ocena emocjonalna",
        ),
        VoicePair(
            context="caption",
            slop="W tym spotkaniu wydarzylo sie naprawde wiele i bylo co ogladac.",
            ours="PSG wygralo final. Ale historia wyniku nie wystarcza, by go zrozumiec.",
            why="od razu teza i napiecie, zero rozgrzewki",
        ),
        VoicePair(
            context="number",
            slop="PSG mialo 61% posiadania pilki.",
            ours="61% pilki dla PSG - i mimo to final wisial na karnych.",
            why="liczba bez kontrastu to slop, kontrast robi historie",
        ),
        VoicePair(
            context="interpretation",
            slop="To pokazuje, ze PSG bylo zdecydowanie lepsze i zasluzenie wygralo.",
            ours="Wiecej pilki i strzalow sugeruje przewage. Ale przewaga to nie kontrola - meczu nie zamknieto przed karnymi.",
            why="'sugeruje' zamiast 'dowodzi', przewaga != kontrola",
        ),
        VoicePair(
            context="cta",
            slop="A co Wy o tym myslicie? Dajcie znac w komentarzu!",
            ours="To byla kontrola PSG, czy Arsenal sprytnie sprowadzil final do karnych?",
            why="CTA oparte na napieciu dzieli odbiorcow i napedza komentarze",
        ),
        VoicePair(
            context="media_title",
            slop="Cala Polska oszalala na punkcie tego meczu!",
            ours="Meksyk 2-0 RPA. News24: 'dar lojalnosci naszego trenera'.",
            why="hook to najmocniejsza teza prasy z atrybucja, nie formulka i nie ocena za narod",
        ),
        VoicePair(
            context="media_panel",
            slop="Wszyscy Meksykanie sa zalamani po tym wyniku.",
            ours="Wedlug El Universal: 'Tri znowu zaczyna z wiekszymi watpliwosciami niz pewnoscia'.",
            why="atrybuujemy konkretnemu medium, nie calemu narodowi",
        ),
        VoicePair(
            context="media_sources",
            slop="Zrodla: internet.",
            ours="Zrodla: El Universal, Record (Meksyk); News24, SuperSport (RPA).",
            why="konkretne, renomowane outlety z linkami zamiast 'internetu'",
        ),
    ],
    judge_rubric=[
        "jedno wyrazne napiecie (jedna mysl)",
        "hook obiecuje dokladnie to, co material dowozi",
        "glowna liczba ma kontekst i zrodlo",
        "brak banned phrases / pustego hype'u",
        "zargon wytlumaczony przy pierwszym uzyciu",
        "CTA konkretne i oparte na napieciu",
        "ton pasuje do formatu",
        "brak zdan do usuniecia bez straty (wata)",
    ],
)
