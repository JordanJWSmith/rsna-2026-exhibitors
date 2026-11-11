import unittest
from decimal import Decimal

from scrape_rsna import (
    booth_measurements,
    clean_text,
    company_address,
    decimal_text,
    make_csv_rows,
    parse_exhibitor_payload,
    parse_hall_payload,
    website_url,
)


class ScraperTests(unittest.TestCase):
    def test_booth_measurements_regular(self):
        self.assertEqual(booth_measurements("10 x 20", 0), ("10x20", "200"))
        self.assertEqual(booth_measurements("8.333 x 10", False), ("8.333x10", "83.33"))

    def test_booth_measurements_irregular(self):
        self.assertEqual(booth_measurements("845.7", 1), ("", "845.7"))

    def test_decimal_text(self):
        self.assertEqual(decimal_text(Decimal("100.00")), "100")
        self.assertEqual(decimal_text(Decimal("83.330")), "83.33")

    def test_address_excludes_country_and_deduplicates(self):
        detail = {
            "address1": "101 Main St",
            "address2": "Suite 2",
            "address3": "",
            "city": "Chicago",
            "state": "IL",
            "zip": "60601",
            "country": "United States",
        }
        self.assertEqual(
            company_address(detail), "101 Main St, Suite 2, Chicago, IL, 60601"
        )
        self.assertNotIn("United States", company_address(detail))

    def test_hall_payload_removes_inventory_but_keeps_assignments(self):
        payload = [
            {
                "hallid": "A",
                "booth": "1000",
                "boothdisplay": "1000",
                "exhid": "42",
                "exhname": " Acme  Imaging ",
            },
            {
                "hallid": "A",
                "booth": "1001",
                "boothdisplay": "1001",
                "exhid": "",
                "exhname": "",
                "boothstatus": "Available",
            },
        ]
        records = parse_hall_payload(payload, "A")
        self.assertEqual(len(records), 1)
        self.assertEqual(clean_text(records[0]["exhname"]), "Acme Imaging")

    def test_exhibitor_payload_and_csv_row(self):
        detail = parse_exhibitor_payload(
            [
                {
                    "exhid": "42",
                    "exhname": "Acme Imaging",
                    "address1": "1 Main St",
                    "city": "Chicago",
                    "state": "IL",
                    "zip": "60601",
                    "country": "United States",
                    "url": "acme.example",
                }
            ],
            "42",
        )
        assignments = [
            {
                "hallid": "B",
                "boothdisplay": "6327",
                "exhid": "42",
                "exhname": "Acme Imaging",
                "boothdims": "10 x 10",
                "irregular": 0,
            }
        ]
        row = make_csv_rows(assignments, {"42": detail})[0]
        self.assertEqual(row["hall"], "North Hall")
        self.assertEqual(row["booth_size"], "10x10")
        self.assertEqual(row["booth_area_sq_ft"], "100")
        self.assertEqual(row["website_url"], "https://acme.example")

    def test_website_rejects_non_http_scheme(self):
        self.assertEqual(website_url("mailto:test@example.com"), "")


if __name__ == "__main__":
    unittest.main()
