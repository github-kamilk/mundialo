import json
import tempfile
import unittest
from pathlib import Path

from app.schemas import SourceTier
from app.tools import (
    CountryMediaProfile,
    SourcePolicyError,
    SourceRegistry,
    load_country_media,
)

MVP_OUTLETS = {
    "ElUniversalMX": ("Meksyk", "A", ("eluniversal.com.mx",), "es"),
    "ESPNDeportesMX": ("Meksyk", "A", ("espn.com.mx",), "es"),
    "RecordMX": ("Meksyk", "B", ("record.com.mx",), "es"),
    "News24ZA": ("RPA", "A", ("news24.com",), "en"),
    "SuperSportZA": ("RPA", "A", ("supersport.com",), "en"),
    "IOLZA": ("RPA", "B", ("iol.co.za",), "en"),
}


class CountryMediaLoaderTests(unittest.TestCase):
    def test_loads_48_countries_with_min_two_outlets(self) -> None:
        _, profiles = load_country_media()
        self.assertEqual(len(profiles), 48)
        for country, profile in profiles.items():
            self.assertEqual(profile.country, country)
            self.assertGreaterEqual(len(profile.outlets), 2)
            self.assertGreaterEqual(len(profile.team_names), 1)
            self.assertGreaterEqual(len(profile.query_templates), 1)

    def test_every_country_has_local_world_cup_term(self) -> None:
        # lokalny termin MS (WK/WM/MS/Mundial...) zasila bezprzeciwnikowe zapytanie
        # '{team} {world_cup}' w MediaResearchProvider - kazdy kraj musi go miec
        _, profiles = load_country_media()
        for country, profile in profiles.items():
            with self.subTest(country=country):
                self.assertTrue(
                    profile.world_cup and profile.world_cup.strip(),
                    f"{country}: brak terminu world_cup",
                )

    def test_rejects_blank_world_cup_term(self) -> None:
        bad = {
            "schema_version": "1.0",
            "countries": [
                {
                    "country": "Testland",
                    "iso2": "XX",
                    "language": "en",
                    "confederation": "TEST",
                    "role": "qualified",
                    "search_hints": {
                        "team_names": ["Testland"],
                        "query_templates": ["{team} vs {opponent}"],
                        "world_cup": "   ",
                    },
                    "outlets": [
                        {
                            "provider_id": "BlankWcXX",
                            "name": "Blank",
                            "tier": "A",
                            "domains": ["example.com"],
                            "sections": [],
                            "confidence": "high",
                            "verified_at": None,
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(SourcePolicyError):
                load_country_media(path)

    def test_no_duplicate_provider_ids(self) -> None:
        descriptors, _ = load_country_media()
        self.assertEqual(len(descriptors), len(set(descriptors)))

    def test_mvp_outlets_preserved(self) -> None:
        registry = SourceRegistry()
        for provider_id, (country, tier, domains, language) in MVP_OUTLETS.items():
            descriptor = registry.get(provider_id)
            self.assertIsNotNone(descriptor, provider_id)
            assert descriptor is not None
            self.assertEqual(descriptor.country, country)
            self.assertEqual(descriptor.tier, SourceTier(tier))
            self.assertEqual(descriptor.domains, domains)
            self.assertEqual(descriptor.language, language)

    def test_registry_media_countries(self) -> None:
        registry = SourceRegistry()
        self.assertEqual(len(registry.media_countries()), 48)

    def test_country_profile_mexico(self) -> None:
        registry = SourceRegistry()
        profile = registry.country_profile("Meksyk")
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertIsInstance(profile, CountryMediaProfile)
        self.assertIn("El Tri", profile.team_names)
        self.assertTrue(any("{team}" in template for template in profile.query_templates))
        self.assertEqual(len(profile.outlets), 3)

    def test_qatar_panel_uses_local_arabic_press(self) -> None:
        # Regresja: panel Kataru skladal sie WYLACZNIE z miedzynarodowych anglojezycznych
        # edycji (Al Jazeera English .com, beIN en-us/en-nz) - kurator nie mial zadnego
        # lokalnego zrodla, wiec drugi cytat zawsze byl 'miedzynarodowy, nie z Kataru'.
        # Whitelist Kataru musi zawierac wlasna prase katarska (arabska), nie edycje EN.
        registry = SourceRegistry()
        profile = registry.country_profile("Katar")
        self.assertIsNotNone(profile)
        assert profile is not None
        domains = {d for outlet in profile.outlets for d in outlet.descriptor.domains}
        self.assertTrue(
            {"raya.com", "al-sharq.com", "al-watan.com"}.issubset(domains),
            f"Katar bez lokalnej prasy arabskiej: {sorted(domains)}",
        )
        # miedzynarodowe edycje EN nie moga byc jedynym/glownym glosem kraju arabskiego
        self.assertNotIn("aljazeera.com", domains)
        self.assertNotIn("beinsports.com", domains)
        self.assertIn("العنابي", profile.team_names)

    def test_portugal_has_free_fetchable_outlet_alongside_paywalled_record(self) -> None:
        # Regresja (Kolumbia-Portugalia, run_20260628075308): panel PT wyszedl suchy
        # (sama statystyka 'o duelo em 5 factos'), bo record.pt trzyma prozaiczne
        # crónica/notas za paywallem + sciana JS -> fetch widzi sama nawigacje (~955 zn.),
        # bramka temporalna SLUSZNIE je odrzuca (brak tresci meczowej), a A Bola dala
        # tylko 2 linki tuz po nocnym meczu. Portugalia musi miec DRUGI darmowy,
        # fetchowalny outlet (ojogo.pt - crónica 'um descafeinado' wyciaga sie czysto)
        # obok dwoch glownych, by panel nie zalezal od paywallowego record.
        registry = SourceRegistry()
        profile = registry.country_profile("Portugalia")
        self.assertIsNotNone(profile)
        assert profile is not None
        domains = {d for outlet in profile.outlets for d in outlet.descriptor.domains}
        self.assertIn("ojogo.pt", domains)
        # record.pt zostaje jako zapas (widzety statystyczne sa fetchowalne), ale nie
        # moze byc jedynym dostawca prozy
        self.assertTrue({"abola.pt", "record.pt"}.issubset(domains))
        # A Bola wzmocniona: druga sekcja (Mundial) obok sekcji Selecao
        abola = next(o for o in profile.outlets if "abola.pt" in o.descriptor.domains)
        self.assertGreaterEqual(len(abola.sections), 2)

    def test_canada_has_fetchable_cbc_section(self) -> None:
        # Regresja (RPA-Kanada, run_20260629080021): panel Kanady wyszedl pusty
        # (one_country_media_missing), bo Tavily search 432-owal (wyczerpany budzet
        # planu), a OBA dotychczasowe outlety Kanady scrawlowaly sie cienko - TSN
        # /soccer statycznie wystawia strony '...-on-tsn-schedule' + promo hokejowe
        # (linki hockey-canada przeszly filtr nazwy bo zawieraja 'canada', bramka
        # temporalna SLUSZNIE je odrzucila), Sportsnet to JS-wall (0 linkow). Kanada
        # byla wiec w 100% zalezna od Tavily i awaria planu zostawila ja bez glosu.
        # CBC Sports (cbc.ca/sports/soccer) jest fetchowalny (200) i wystawia recap
        # meczu statycznie ('fifa-world-cup-canada-south-africa-recap-...') - musi byc
        # outletem Kanady, by kraj przetrwal awarie Tavily na samych sekcjach.
        registry = SourceRegistry()
        profile = registry.country_profile("Kanada")
        self.assertIsNotNone(profile)
        assert profile is not None
        domains = {d for outlet in profile.outlets for d in outlet.descriptor.domains}
        self.assertIn("cbc.ca", domains)
        cbc = next(o for o in profile.outlets if "cbc.ca" in o.descriptor.domains)
        self.assertTrue(any("soccer" in s for s in cbc.sections))

    def test_germany_has_fetchable_outlet_alongside_botblocked_kicker(self) -> None:
        # Regresja (Niemcy-Paragwaj, run_20260630100914/212809): niemiecki slajd wyszedl
        # jako goly cytat bez streszczenia ("krotkie gowno"). kicker - jedyny outlet z
        # pelnym Spielberichtem - 403-uje boty NA POZIOMIE ARTYKULU tez (Tavily zwraca
        # URL ale raw_content=0, wlasny fetch 403), wiec search NIE nadrabia; zostawal
        # sam Bild (tabloid: krotkie kawalki, strony wideo, oceny z innego meczu) - za
        # cienki, by LLM zlozyl kontraktowe streszczenie (salvage -> sam cytat). Niemcy
        # musza miec DRUGI osiagalny outlet z pelna relacja: sportschau.de (ARD, darmowy,
        # Spielbericht ~7,6 tys. zn. fetchuje sie czysto, sekcja fifa-wm-2026 listuje go).
        registry = SourceRegistry()
        profile = registry.country_profile("Niemcy")
        self.assertIsNotNone(profile)
        assert profile is not None
        domains = {d for outlet in profile.outlets for d in outlet.descriptor.domains}
        self.assertIn("sportschau.de", domains)
        sportschau = next(o for o in profile.outlets if "sportschau.de" in o.descriptor.domains)
        self.assertEqual(sportschau.descriptor.tier, SourceTier("A"))
        self.assertTrue(any("fifa-wm-2026" in s for s in sportschau.sections))
        # kicker zostaje jako zapas, ale nie moze byc jedynym dostawca pelnej relacji
        self.assertTrue({"kicker.de", "bild.de"}.issubset(domains))

    def test_argentina_has_reaction_rich_outlets_beyond_ole(self) -> None:
        # Regresja (Argentyna-Szwajcaria, run_20260713060703): panel Argentyny stanal
        # na artykule o FREKWENCJI i hubie 'arma tu equipo', bo pula byla plytka:
        # sekcja TyC to JS-wall (200, puste body, 0 linkow), a run dzien po meczu
        # zastal sekcje Ole /seleccion zalana zapowiedziami polfinalu z Anglia.
        # Infobae/Clarin/La Nacion fetchuja sie statycznie (zweryfikowane 2026-07-13)
        # i wystawialy wtedy wlasnie te reakcje, ktorych zabraklo (Messi kontra arbiter,
        # 'Embolo devastado', cronica Juliana Alvareza) - musza byc w whiteliscie,
        # zeby kurator MIAL z czego wybierac.
        registry = SourceRegistry()
        profile = registry.country_profile("Argentyna")
        self.assertIsNotNone(profile)
        assert profile is not None
        domains = {d for outlet in profile.outlets for d in outlet.descriptor.domains}
        self.assertTrue(
            {"infobae.com", "clarin.com", "lanacion.com.ar"}.issubset(domains), domains
        )
        # TyC zostaje w whiteliscie searchu (Tavily znajduje tam artykuly), ale jego
        # martwa sekcja nie moze zjadac slotu crawla (cap sekcji na kraj)
        tyc = next(o for o in profile.outlets if "tycsports.com" in o.descriptor.domains)
        self.assertEqual(len(tyc.sections), 0)
        # kazdy z nowych outletow ma fetchowalna sekcje deportes
        for domain in ("infobae.com", "clarin.com", "lanacion.com.ar"):
            outlet = next(o for o in profile.outlets if domain in o.descriptor.domains)
            self.assertTrue(any("deportes" in s for s in outlet.sections), domain)

    def test_configured_sections_fit_within_crawl_cap(self) -> None:
        # Niezmiennik laczacy config z kodem: _section_hits przerywa po
        # max_sections_per_country LACZNIE na kraj (iteracja outletow po kolei), wiec
        # sekcje skonfigurowane PONAD ten pulap nigdy sie nie scrawluja (cicha strata
        # swiezosci). Pilnujemy, by suma sekcji kazdego kraju miescila sie w budzecie -
        # inaczej dodanie outletu/sekcji (jak ojogo.pt dla PT) jest pozorne.
        from app.tools import MediaResearchProvider

        cap = MediaResearchProvider.__dataclass_fields__["max_sections_per_country"].default
        _, profiles = load_country_media()
        for country, profile in profiles.items():
            total = sum(len(outlet.sections) for outlet in profile.outlets)
            with self.subTest(country=country):
                self.assertLessEqual(
                    total,
                    cap,
                    f"{country}: {total} sekcji > pulap crawla {cap} - nadmiar sie nie scrawluje",
                )

    def test_rejects_duplicate_provider_id(self) -> None:
        outlet = {
            "provider_id": "DupOutletXX",
            "name": "Dup",
            "tier": "A",
            "domains": ["example.com"],
            "sections": [],
            "confidence": "high",
            "verified_at": None,
        }
        bad = {
            "schema_version": "1.0",
            "countries": [
                {
                    "country": "TestlandA",
                    "iso2": "XA",
                    "language": "en",
                    "confederation": "TEST",
                    "role": "qualified",
                    "search_hints": {
                        "team_names": ["TestlandA"],
                        "query_templates": ["{team} vs {opponent}"],
                    },
                    "outlets": [outlet, dict(outlet)],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(SourcePolicyError):
                load_country_media(path)

    def test_rejects_invalid_tier(self) -> None:
        bad = {
            "schema_version": "1.0",
            "countries": [
                {
                    "country": "Testland",
                    "iso2": "XX",
                    "language": "en",
                    "confederation": "TEST",
                    "role": "qualified",
                    "search_hints": {
                        "team_names": ["Testland"],
                        "query_templates": ["{team} vs {opponent}"],
                    },
                    "outlets": [
                        {
                            "provider_id": "BadTierXX",
                            "name": "Bad",
                            "tier": "Z",
                            "domains": ["example.com"],
                            "sections": [],
                            "confidence": "high",
                            "verified_at": None,
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(SourcePolicyError):
                load_country_media(path)

    def test_rejects_country_without_outlets(self) -> None:
        bad = {
            "schema_version": "1.0",
            "countries": [
                {
                    "country": "Testland",
                    "iso2": "XX",
                    "language": "en",
                    "confederation": "TEST",
                    "role": "qualified",
                    "search_hints": {
                        "team_names": ["Testland"],
                        "query_templates": ["{team} vs {opponent}"],
                    },
                    "outlets": [],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(SourcePolicyError):
                load_country_media(path)


if __name__ == "__main__":
    unittest.main()
