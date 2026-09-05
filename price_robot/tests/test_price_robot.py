import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import price_robot


class PriceRobotTests(unittest.TestCase):
    def test_eon_extracts_registered_prices_and_limits(self):
        text = """
        Registrovaný zákazník služby E.ON Drive CZ
        Denní Tarif Cena přes den (8:00-20:00)
        Skupina 1 (až 100kW) 7,85 9,5
        Skupina 2 (101 -200 kW) 10,74 13,00
        Skupina 3 (201- 400kW) 13,96 16,90
        Noční tarif Cena přes noc (20:00-8:00)
        Skupina 1 (až 100kW) 6,61 8,00
        Skupina 2 (101 -200 kW) 8,26 10,00
        Skupina 3 (201- 400kW) 9,92 12,00
        Objem volných minut
        Skupina 1 (až 100kW) 480 (AC konektory) /120 (DC konektory)
        Skupina 2 (101 -200 kW) 60
        Skupina 3 (201- 400kW) 30
        Poplatek za minuty po překročení volných minut
        Denní tarif 1,65 2,00
        Noční tarif 0,00 0,00
        Ceník platný od 18.6.2026.
        """
        with patch.object(price_robot, "pdf_content", return_value=(text, [])):
            parsed = price_robot.parse_eon(b"%PDF-test", "https://example.test/eon.pdf")

        self.assertEqual(parsed.effective_from.isoformat(), "2026-06-18")
        self.assertEqual(len(parsed.rules), 4)
        self.assertEqual(parsed.rules[1]["dayPriceCZKPerKWh"], 9.5)
        self.assertEqual(parsed.rules[1]["nightPriceCZKPerKWh"], 8.0)
        self.assertEqual(parsed.rules[1]["occupancyFreeMinutes"], 120)
        self.assertTrue(parsed.rules[1]["nightOccupancyFeeWaived"])

    def test_pre_extracts_only_start_tariff_table(self):
        text = "Ceník dobíjení v síti PRE POINT platný od 1. 12. 2025 " + "x" * 100
        table = [
            [
                "TRATS\nEGRAHC\nERP",
                "Typ konektoru",
                "Měsíční poplatek",
                "Cena za kWh",
                "Cena za minutu",
                "Volné minuty při dobíjení",
            ],
            [None, "AC", "0,00 Kč", "9,00 Kč (7,44 bez DPH)", "1,00 Kč", "180"],
            [None, "DC", None, "12,00 Kč (9,91 bez DPH)", "2,00 Kč", "60"],
            [None, "UFC", None, "14,00 Kč (11,57 bez DPH)", "2,00 Kč", "30"],
        ]
        with patch.object(price_robot, "pdf_content", return_value=(text, [table])):
            parsed = price_robot.parse_pre(b"%PDF-test", "https://example.test/pre.pdf")

        self.assertEqual([item["dayPriceCZKPerKWh"] for item in parsed.rules], [9.0, 12.0, 14.0])
        self.assertEqual([item["maximumPowerKW"] for item in parsed.rules], [49, 149, 1_000])

    def test_cez_extracts_basic_power_bands(self):
        text = """
        Ceník služby Dobíjení Účinnost: od 1. 6. 2026
        I. Ceník Vlastních a Partnerských DS
        ≤ 49 kW 09,90 od 481. min.* 02,00
        Basic ≤ 149 kW 0,00 12,90 od 91. min.* 02,00
        ≥ 150 kW 15,90 od 46. min.* 02,00
        Standard
        """ + "x" * 100
        with patch.object(price_robot, "pdf_content", return_value=(text, [])):
            parsed = price_robot.parse_cez(b"%PDF-test", "https://example.test/cez.pdf")

        self.assertEqual(len(parsed.rules), 4)
        self.assertEqual(parsed.rules[0]["dayPriceCZKPerKWh"], 9.9)
        self.assertEqual(parsed.rules[2]["dayPriceCZKPerKWh"], 12.9)
        self.assertEqual(parsed.rules[3]["occupancyFreeMinutes"], 45)

    def test_feed_validation_is_fail_closed(self):
        invalid_feed = {
            "schemaVersion": 2,
            "sources": [{"providerID": "eon"}],
            "tariffs": [],
        }
        with self.assertRaises(price_robot.PriceRobotError):
            price_robot.validate_feed(invalid_feed)


if __name__ == "__main__":
    unittest.main()
