import unittest

from app.render.specs import (
    build_caption_text,
    build_slide_specs,
    domain_of,
    format_date_pl,
    load_iso2_map,
)


def make_media_package() -> dict:
    quote_mx = {
        "outlet": "ElUniversalMX",
        "country": "Meksyk",
        "language": "es",
        "original_text": "La Seleccion...",
        "translation_pl": "Reprezentacja...",
        "url": "https://www.eluniversal.com.mx/deportes/cronica",
        "tier": "A",
        "retrieved_at": "2026-06-11T22:32:13+00:00",
        "evidence_id": "e_mx_1",
        "summary_pl": "Streszczenie MX.",
    }
    quote_za = {
        "outlet": "News24ZA",
        "country": "RPA",
        "language": "en",
        "original_text": "Mexico claimed...",
        "translation_pl": "Meksyk odniosl...",
        "url": "https://news24.com/sport/soccer/worldcup/first-take",
        "tier": "B",
        "retrieved_at": "2026-06-11T22:33:00+00:00",
        "evidence_id": "e_za_1",
        "summary_pl": "Streszczenie ZA.",
    }
    return {
        "package_id": "mpkg_test",
        "status": "ready",
        "match": {
            "home_team": "Meksyk",
            "away_team": "RPA",
            "date": "2026-06-11",
            "score": {"full_time": "2-0"},
        },
        "panels": [
            {"country": "Meksyk", "language": "es", "quotes": [quote_mx]},
            {"country": "RPA", "language": "en", "quotes": [quote_za]},
        ],
        "carousel": {
            "slides": [
                {
                    "slide_number": 1,
                    "role": "title",
                    "headline": "Meksyk 2-0 RPA: jak odebraly to media?",
                    "body": "Reakcje prasy w obu krajach.",
                    "claim_ids": ["e_result_1"],
                    "visual_brief": "",
                },
                {
                    "slide_number": 2,
                    "role": "media_country",
                    "headline": "Media Meksyk: ElUniversalMX",
                    "body": "Streszczenie MX.",
                    "claim_ids": ["e_mx_1"],
                    "visual_brief": "",
                },
                {
                    "slide_number": 3,
                    "role": "media_country",
                    "headline": "Media RPA: News24ZA",
                    "body": "Streszczenie ZA.",
                    "claim_ids": [],  # celowo: test fallbacku po outlecie w headline
                    "visual_brief": "",
                },
                {
                    "slide_number": 4,
                    "role": "sources",
                    "headline": "Zrodla",
                    "body": "...",
                    "claim_ids": ["e_mx_1", "e_za_1"],
                    "visual_brief": "",
                },
            ]
        },
    }


ISO2 = {"Meksyk": "mx", "RPA": "za"}
OUTLET_NAMES = {"ElUniversalMX": "El Universal", "News24ZA": "News24"}


class BuildSlideSpecsTest(unittest.TestCase):
    def test_roles_and_numbering(self) -> None:
        specs = build_slide_specs(make_media_package(), ISO2)
        self.assertEqual([s["role"] for s in specs], ["title", "media_country", "media_country", "sources"])
        self.assertTrue(all(s["total"] == 4 for s in specs))

    def test_title_spec_splits_score_from_question(self) -> None:
        title = build_slide_specs(make_media_package(), ISO2)[0]
        self.assertEqual(title["score"], "2–0")
        self.assertEqual(title["question"], "Jak odebraly to media?")
        self.assertEqual(title["home_iso2"], "mx")
        self.assertEqual(title["date"], "11.06.2026")

    def test_title_without_score_prefix_keeps_headline(self) -> None:
        package = make_media_package()
        package["carousel"]["slides"][0]["headline"] = "Wielki wieczor w Meksyku"
        title = build_slide_specs(package, ISO2)[0]
        self.assertEqual(title["question"], "Wielki wieczor w Meksyku")

    def test_title_attribution_byline_passed_through_separately(self) -> None:
        # hook to czysta teza, a byline (sama nazwa dziennika, bez 'wg') idzie osobnym polem
        package = make_media_package()
        package["carousel"]["slides"][0]["headline"] = "Meksyk 2-0 RPA. Bafana z podniesiona glowa"
        package["carousel"]["slides"][0]["attribution"] = "News24"
        title = build_slide_specs(package, ISO2)[0]
        self.assertEqual(title["attribution"], "News24")
        self.assertEqual(title["question"], "Bafana z podniesiona glowa")

    def test_title_without_attribution_yields_empty_byline(self) -> None:
        title = build_slide_specs(make_media_package(), ISO2)[0]
        self.assertEqual(title["attribution"], "")

    def test_editorial_hook_keeps_attribution_colon(self) -> None:
        # nowy format ramy: "Home N-M Away. Hook z atrybucja" - prefiks z wynikiem
        # odpada, ale atrybucja z ':' w hooku musi zostac
        package = make_media_package()
        package["carousel"]["slides"][0]["headline"] = (
            "Meksyk 2-0 RPA. Czeska prasa: dar lojalnosci trenera"
        )
        title = build_slide_specs(package, ISO2)[0]
        self.assertEqual(title["question"], "Czeska prasa: dar lojalnosci trenera")

    def test_editorial_hook_preserves_brand_casing(self) -> None:
        # 'iSport' na poczatku hooka nie moze zostac 'ISport'
        package = make_media_package()
        package["carousel"]["slides"][0]["headline"] = (
            "Meksyk 2-0 RPA. iSport: Krejci wzbudzil emocje"
        )
        title = build_slide_specs(package, ISO2)[0]
        self.assertEqual(title["question"], "iSport: Krejci wzbudzil emocje")

    def test_editorial_hook_without_colon_drops_score_prefix(self) -> None:
        package = make_media_package()
        package["carousel"]["slides"][0]["headline"] = (
            "Meksyk 2-0 RPA. Sen Bafana stal sie koszmarem - News24ZA"
        )
        title = build_slide_specs(package, ISO2)[0]
        self.assertEqual(title["question"], "Sen Bafana stal sie koszmarem - News24ZA")

    def test_media_slide_enriched_from_panel_by_evidence_id(self) -> None:
        spec = build_slide_specs(make_media_package(), ISO2, outlet_names=OUTLET_NAMES)[1]
        # na grafice ludzka nazwa redakcji, nie techniczny provider_id
        self.assertEqual(spec["outlet"], "El Universal")
        self.assertEqual(spec["tier"], "A")
        self.assertEqual(spec["domain"], "eluniversal.com.mx")
        self.assertEqual(spec["accent"], "home")
        self.assertEqual(spec["iso2"], "mx")
        # tresc slajdu bez transformacji
        self.assertEqual(spec["body"], "Streszczenie MX.")

    def test_media_slide_fallback_match_by_outlet_in_headline(self) -> None:
        spec = build_slide_specs(make_media_package(), ISO2, outlet_names=OUTLET_NAMES)[2]
        self.assertEqual(spec["outlet"], "News24")
        self.assertEqual(spec["accent"], "away")

    def test_outlet_without_display_name_falls_back_to_provider_id(self) -> None:
        spec = build_slide_specs(make_media_package(), ISO2, outlet_names={})[1]
        self.assertEqual(spec["outlet"], "ElUniversalMX")

    def test_sources_slide_lists_all_quotes(self) -> None:
        spec = build_slide_specs(make_media_package(), ISO2, outlet_names=OUTLET_NAMES)[3]
        outlets = [item["outlet"] for item in spec["items"]]
        self.assertEqual(outlets, ["El Universal", "News24"])
        self.assertEqual(spec["items"][1]["domain"], "news24.com")

    def test_caption_file_has_intro_links_and_hashtags(self) -> None:
        package = make_media_package()
        package["caption"] = {
            "text": "El Universal widzi szare zwyciestwo. News24 pisze o koszmarze. Kto ma racje?",
            "hashtags": ["#mundial2026", "#reakcjemediow"],
        }
        text = build_caption_text(package, outlet_names=OUTLET_NAMES, display_map={})
        lines = text.splitlines()
        # opis redakcyjny na poczatku, potem linki, hashtagi na koncu
        self.assertEqual(lines[0], package["caption"]["text"])
        self.assertIn("Źródła:", lines)
        self.assertIn(
            "- El Universal (Meksyk): https://www.eluniversal.com.mx/deportes/cronica", lines
        )
        self.assertIn(
            "- News24 (RPA): https://news24.com/sport/soccer/worldcup/first-take", lines
        )
        self.assertEqual(lines[-1], "#mundial2026 #reakcjemediow")

    def test_caption_file_dedupes_repeated_urls(self) -> None:
        package = make_media_package()
        package["caption"] = {"text": "Opis.", "hashtags": []}
        duplicate = dict(package["panels"][0]["quotes"][0])
        package["panels"][0]["quotes"].append(duplicate)
        text = build_caption_text(package, outlet_names={}, display_map={})
        self.assertEqual(text.count("eluniversal.com.mx"), 1)

    def test_x_post_mirrors_full_carousel_content(self) -> None:
        from app.render.specs import build_x_post_text

        package = make_media_package()
        package["title_slide"] = {
            "headline": "Meksyk 2-0 RPA. News24: 'dar lojalnosci trenera'",
            "body": "Meksykanska prasa swietuje, w RPA krytyka trenera.",
        }
        package["caption"] = {
            "text": "El Universal widzi szare zwyciestwo. Czy Broos powinien odejsc?",
            "hashtags": ["#mundial2026", "#worldcup2026", "#meksyk", "#rpa"],
        }
        long_summary = " ".join(
            f"Zdanie numer {i} o tym, jak redakcja ocenia mecz i jego konsekwencje."
            for i in range(12)
        )
        package["panels"][0]["quotes"][0]["summary_pl"] = long_summary
        text = build_x_post_text(package, outlet_names=OUTLET_NAMES, display_map={})
        # naglowek + podtytul ze slajdu tytulowego
        self.assertIn("News24: 'dar lojalnosci trenera'", text)
        self.assertIn("Meksykanska prasa swietuje", text)
        # PELNE streszczenie bez skracania (wszystkie 12 zdan)
        self.assertIn("Zdanie numer 0", text)
        self.assertIn("Zdanie numer 11", text)
        # drugi panel tez w komplecie
        self.assertIn("Streszczenie ZA.", text)
        # CTA, zrodla z linkami i KOMPLET hashtagow
        self.assertIn("Czy Broos powinien odejsc?", text)
        self.assertIn("Źródła:", text)
        self.assertIn("- News24 (RPA): https://news24.com/sport/soccer/worldcup/first-take", text)
        self.assertIn("#mundial2026 #worldcup2026 #meksyk #rpa", text)

    def test_x_post_humanizes_outlets_without_duplication(self) -> None:
        from app.render.specs import build_x_post_text

        package = make_media_package()
        package["title_slide"] = {"headline": "Meksyk 2-0 RPA: jak odebraly to media?"}
        package["caption"] = {"text": "Opis.", "hashtags": []}
        package["panels"][0]["quotes"][0]["summary_pl"] = (
            "ElUniversalMX relacjonuje gola w 8. minucie spotkania. Redakcja chwali zespol."
        )
        text = build_x_post_text(package, outlet_names=OUTLET_NAMES, display_map={})
        # techniczny provider_id zamieniony na ludzka nazwe...
        self.assertIn("El Universal relacjonuje", text)
        self.assertNotIn("ElUniversalMX relacjonuje", text)
        # ...naglowek bloku nie dubluje nazwy outletu (jest juz w streszczeniu)
        self.assertIn("🗞️ Meksyk:\nEl Universal relacjonuje", text)
        # goly cytat bez atrybucji w tresci dostaje outlet w naglowku bloku
        self.assertIn("🗞️ News24 (RPA):\nStreszczenie ZA.", text)

    def test_unknown_country_gives_empty_iso2(self) -> None:
        package = make_media_package()
        package["panels"][0]["country"] = "Atlantyda"
        package["panels"][0]["quotes"][0]["country"] = "Atlantyda"
        spec = build_slide_specs(package, ISO2)[1]
        self.assertEqual(spec["iso2"], "")
        self.assertEqual(spec["accent"], "away")  # nie-home => away (neutralny drugi akcent)


class HelpersTest(unittest.TestCase):
    def test_domain_of(self) -> None:
        self.assertEqual(domain_of("https://www.news24.com/sport"), "news24.com")
        self.assertEqual(domain_of("https://supersport.com/x?y=1"), "supersport.com")
        self.assertEqual(domain_of(""), "")

    def test_format_date_pl(self) -> None:
        self.assertEqual(format_date_pl("2026-06-11"), "11.06.2026")
        self.assertEqual(format_date_pl("bez daty"), "bez daty")

    def test_load_iso2_map_from_repo_registry(self) -> None:
        mapping = load_iso2_map()
        self.assertEqual(mapping.get("Meksyk"), "mx")
        self.assertEqual(mapping.get("RPA"), "za")

    def test_home_nations_use_subdivision_flags_not_union_jack(self) -> None:
        # narody brytyjskie maja wlasne flagi (Saltire / krzyz sw. Jerzego),
        # nie Union Jack ("gb") - regresja: Szkocja/Anglia mialy iso2 "GB"
        mapping = load_iso2_map()
        self.assertEqual(mapping.get("Szkocja"), "gb-sct")
        self.assertEqual(mapping.get("Anglia"), "gb-eng")
        self.assertNotEqual(mapping.get("Szkocja"), "gb")

    def test_iso2_map_accepts_subdivision_codes(self) -> None:
        import tempfile
        from pathlib import Path

        registry = (
            '{"countries": ['
            '{"country": "Szkocja", "iso2": "GB-SCT"},'
            '{"country": "Meksyk", "iso2": "MX"},'
            '{"country": "Smieci", "iso2": "GB-SUBDIV-TOOLONG"}'
            ']}'
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            path.write_text(registry, encoding="utf-8")
            mapping = load_iso2_map(path)
        self.assertEqual(mapping.get("Szkocja"), "gb-sct")
        self.assertEqual(mapping.get("Meksyk"), "mx")
        self.assertNotIn("Smieci", mapping)  # nieprawidlowy format odrzucony


class RenderAfterRunTest(unittest.TestCase):
    """Gating automatycznego renderu w CLI (bez dotykania Playwrighta)."""

    def test_no_media_package(self) -> None:
        from pathlib import Path

        from app.cli import render_after_run

        self.assertEqual(render_after_run({}, "run_x", Path("runs")), "no_package")

    def test_skips_non_ready_status(self) -> None:
        from pathlib import Path

        from app.cli import render_after_run

        plain = {"media_package": {"status": "needs_human_review"}}
        self.assertEqual(render_after_run(plain, "run_x", Path("runs")), "skipped_status")

    def test_renders_ready_package(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from app.cli import render_after_run

        package = make_media_package()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("app.render.render_slides", return_value=[Path(tmp) / "slide_01.png"]) as mocked:
                result = render_after_run({"media_package": package}, "run_x", Path(tmp))
        self.assertEqual(result, "rendered")
        specs, out_dir = mocked.call_args.args
        self.assertEqual(len(specs), 4)
        self.assertTrue(str(out_dir).endswith("slides"))


class DisplayNameTests(unittest.TestCase):
    def test_title_and_panel_use_display_names_with_diacritics(self) -> None:
        from app.render.specs import load_display_map

        display_map = load_display_map()
        self.assertEqual(display_map.get("Korea Poludniowa"), "Korea Południowa")
        package = {
            "match": {
                "home_team": "Korea Poludniowa",
                "away_team": "Czechy",
                "score": {"full_time": "2-1"},
                "date": "2026-06-12",
            },
            "panels": [
                {
                    "country": "Korea Poludniowa",
                    "quotes": [
                        {
                            "evidence_id": "e1",
                            "outlet": "YonhapKR",
                            "country": "Korea Poludniowa",
                            "url": "https://en.yna.co.kr/x",
                            "tier": "A",
                        }
                    ],
                }
            ],
            "carousel": {
                "slides": [
                    {
                        "slide_number": 1,
                        "role": "title",
                        "headline": "Korea Poludniowa 2-1 Czechy: jak odebraly to media?",
                        "body": "",
                        "claim_ids": [],
                    },
                    {
                        "slide_number": 2,
                        "role": "media_country",
                        "headline": "Media Korea Poludniowa: YonhapKR",
                        "body": "tresc",
                        "claim_ids": ["e1"],
                    },
                ]
            },
        }
        specs = build_slide_specs(package)
        title = specs[0]
        self.assertEqual(title["home_team"], "Korea Południowa")
        panel = specs[1]
        self.assertEqual(panel["country"], "Korea Południowa")


class CityPaletteTests(unittest.TestCase):
    def test_resolves_city_from_stadium_alias(self) -> None:
        from app.render.specs import resolve_palette

        city, palette = resolve_palette("Estadio Guadalajara (Akron), Guadalajara")
        self.assertEqual(city, "Guadalajara")
        self.assertNotEqual(palette["accent"], "#e9c46a")

    def test_unknown_venue_falls_back_to_default(self) -> None:
        from app.render.specs import resolve_palette

        city, palette = resolve_palette("nieznany stadion")
        self.assertEqual(city, "")
        self.assertEqual(palette["accent"], "#e9c46a")

    def test_every_palette_keeps_text_readable(self) -> None:
        """Kontrast WCAG: jasny tekst (--ink) na tlach kazdej palety >= 7:1."""
        from app.render.specs import load_palettes

        def luminance(hex_color: str) -> float:
            rgb = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
            lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
            return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

        def contrast(a: str, b: str) -> float:
            la, lb = luminance(a), luminance(b)
            lighter, darker = max(la, lb), min(la, lb)
            return (lighter + 0.05) / (darker + 0.05)

        ink = "#f2f5fb"
        data = load_palettes()
        palettes = [("default", data["default"])] + [
            (entry["city"], entry["palette"]) for entry in data["cities"]
        ]
        self.assertGreaterEqual(len(palettes), 17)  # default + 16 miast
        for name, palette in palettes:
            for key in ("bg_top", "bg_bottom", "glow_a", "glow_b"):
                ratio = contrast(ink, palette[key])
                self.assertGreaterEqual(
                    ratio, 7.0, f"paleta {name}: kontrast tekstu na {key} = {ratio:.1f}"
                )
            # akcent musi byc czytelny na tle (duzy tekst: >= 3:1)
            self.assertGreaterEqual(
                contrast(palette["accent"], palette["bg_top"]), 3.0, f"paleta {name}: accent"
            )

    def test_specs_carry_palette_and_host_city(self) -> None:
        package = {
            "match": {
                "home_team": "Korea Poludniowa",
                "away_team": "Czechy",
                "score": {"full_time": "2-1"},
                "date": "2026-06-12",
                "venue": "Estadio Guadalajara (Akron), Guadalajara",
            },
            "panels": [],
            "carousel": {
                "slides": [
                    {
                        "slide_number": 1,
                        "role": "title",
                        "headline": "x: y?",
                        "body": "",
                        "claim_ids": [],
                    }
                ]
            },
        }
        specs = build_slide_specs(package)
        self.assertEqual(specs[0]["host_city"], "Guadalajara")
        self.assertIn("accent", specs[0]["palette"])


if __name__ == "__main__":
    unittest.main()
