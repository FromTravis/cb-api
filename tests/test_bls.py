import unittest

from config import CB_CONFIG
from fetchers.bls import _to_yoy_pct


class BLSFetcherTests(unittest.TestCase):
    def test_fed_cpi_uses_bls_series(self):
        fed_cpi = CB_CONFIG["fed"]["series"]["cpi"]
        self.assertEqual(fed_cpi["source"], "bls")
        self.assertEqual(fed_cpi["id"], "CUUR0000SA0")
        self.assertEqual(fed_cpi["transform"], "yoy_pct")

    def test_yoy_pct_transformation_uses_prior_year_value(self):
        raw = [
            {"date": "2022-01-01", "value": 100.0},
            {"date": "2022-02-01", "value": 100.2},
            {"date": "2022-03-01", "value": 100.4},
            {"date": "2022-04-01", "value": 100.6},
            {"date": "2022-05-01", "value": 100.8},
            {"date": "2022-06-01", "value": 101.0},
            {"date": "2022-07-01", "value": 101.2},
            {"date": "2022-08-01", "value": 101.4},
            {"date": "2022-09-01", "value": 101.6},
            {"date": "2022-10-01", "value": 101.8},
            {"date": "2022-11-01", "value": 102.0},
            {"date": "2022-12-01", "value": 102.2},
            {"date": "2023-01-01", "value": 103.0},
            {"date": "2023-02-01", "value": 103.5},
        ]
        transformed = _to_yoy_pct(raw)
        self.assertEqual(transformed[0]["date"], "2023-01-01")
        self.assertEqual(transformed[0]["value"], 3.0)
        self.assertEqual(len(transformed), 2)


if __name__ == "__main__":
    unittest.main()
