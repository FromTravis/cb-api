"""
Central Bank API — configuration
All series codes, CB metadata, and environment settings live here.

CPI strategy:
  - Fed/ECB: FRED native series (CPIAUCSL, ECB SDMX)
  - All others: FRED OECD MEI series (e.g. POLCPIALLMINMEI)
    These are monthly index values → need yoy_pct transform
    More reliable than Eurostat SDMX which has had endpoint changes
"""

import os
from dotenv import load_dotenv

load_dotenv()

FRED_API_KEY      = os.getenv("FRED_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

CACHE_DIR = os.path.join(os.path.dirname(__file__), ".cache")
CACHE_TTL_SECONDS = 60 * 60 * 12   # 12 hours

CB_CONFIG = {

    # ── Federal Reserve ──────────────────────────────────────────────────────
    "fed": {
        "name": "Federal Reserve (Fed)",
        "country": "US", "currency": "USD", "cpi_target": 2.0,
        "series": {
            "rate": {"source": "fred", "id": "DFF",         "label": "Fed funds rate (effective, daily)", "frequency": "d"},
            "cpi":  {"source": "fred", "id": "CPIAUCSL",    "label": "CPI All Urban Consumers", "transform": "yoy_pct"},
            "y2":   {"source": "fred", "id": "DGS2",        "label": "2Y Treasury yield", "frequency": "d"},
            "y10":  {"source": "fred", "id": "DGS10",       "label": "10Y Treasury yield", "frequency": "d"},
            "fx":   {"source": "fred", "id": "DEXUSEU",     "label": "EUR/USD", "frequency": "d"},
        }
    },

    # ── European Central Bank ────────────────────────────────────────────────
    "ecb": {
        "name": "European Central Bank (ECB)",
        "country": "XM", "currency": "EUR", "cpi_target": 2.0,
        "series": {
            "rate": {"source": "ecb",  "id": "FM/B.U2.EUR.4F.KR.MRR_FR.LEV",  "label": "ECB Main refinancing rate"},
            "cpi":  {"source": "ecb",  "id": "HICP/M.U2.N.000000.4D0.ANR", "label": "HICP YoY"},
            "y2":   {"source": "bundesbank", "id": "D.REN.EUR.A610.000000WT0202.A", "label": "2Y Schatz yield",  "frequency": "d"},
            "y10":  {"source": "bundesbank", "id": "D.REN.EUR.A630.000000WT1010.A", "label": "10Y Bund yield",    "frequency": "d"},
            "fx":   {"source": "fred", "id": "DEXUSEU", "label": "EUR/USD", "frequency": "d"},
        }
    },
    # EU HICP https://data.ecb.europa.eu/data/datasets/HICP/HICP.M.U2.N.000000.4D0.ANR
    # ── Bank of England ──────────────────────────────────────────────────────
    "boe": {
        "name": "Bank of England (BoE)",
        "country": "GB", "currency": "GBP", "cpi_target": 2.0,
        "series": {
            "rate": {"source": "boe", "id": "IUDBEDR", "label": "Bank Rate",             "frequency": "d"},
            "cpi":  {"source": "ons", "id": "D7G7",   "label": "CPI YoY"},
            "y2":   {"source": "boe", "id": "IUDSNPY", "label": "5Y Gilt nominal yield", "frequency": "d"},
            "y10":  {"source": "boe", "id": "IUDMNPY", "label": "10Y Gilt nominal yield","frequency": "d"},
            "fx":   {"source": "boe", "id": "XUDLERS", "label": "GBP/EUR",               "frequency": "d"},
        }
    },

    # ── Bank of Japan ────────────────────────────────────────────────────────
    "boj": {
        "name": "Bank of Japan (BoJ)",
        "country": "JP", "currency": "JPY", "cpi_target": 2.0,
        "series": {
            "rate": {"source": "bis",  "id": "BIS,WS_CBPOL,1.0|D.JP", "label": "Policy rate", "frequency": "d"},
            "cpi":  {"source": "bis",  "id": "BIS,WS_LONG_CPI,1.0|M.JP.771", "label": "CPI YoY"},
            "y2":   {"source": "fred", "id": "IR3TIB01JPM156N", "label": "3M interbank rate"},
            "y10":  {"source": "fred", "id": "IRLTLT01JPM156N", "label": "10Y JGB yield"},
            "fx":   {"source": "fred", "id": "DEXJPUS", "label": "USD/JPY", "frequency": "d"},
        }
    },

    # ── Poland (NBP) ─────────────────────────────────────────────────────────
    "pol": {
        "name": "National Bank of Poland (NBP)",
        "country": "PL", "currency": "PLN", "cpi_target": 2.5,
        "series": {
            "rate": {"source": "nbp",  "id": "ref", "label": "NBP Reference Rate", "frequency": "d"},
            "cpi":  {"source": "ecb",  "id": "ICP/M.PL.N.000000.4.ANR", "label": "HICP YoY"},
            "y2":   {"source": "fred",  "id": "IR3TIB01PLM156N",  "label": "3M interbank rate"},
            "y10":  {"source": "fred", "id": "IRLTLT01PLM156N",  "label": "10Y bond yield"},
            "fx":   {"source": "nbp",  "id": "eur", "label": "EUR/PLN", "frequency": "d"},
        }
    },

    # ── Hungary (MNB) ────────────────────────────────────────────────────────
    "hun": {
        "name": "National Bank of Hungary (MNB)",
        "country": "HU", "currency": "HUF", "cpi_target": 3.0,
        "series": {
            "rate": {"source": "bis",  "id": "BIS,WS_CBPOL,1.0|D.HU",    "label": "Base rate",  "frequency": "d"},
            "cpi":  {"source": "bis",  "id": "BIS,WS_LONG_CPI,1.0|M.HU.771", "label": "CPI YoY"},
            "y2":   {"source": "fred", "id": "IR3TIB01HUM156N", "label": "3M interbank rate"},
            "y10":  {"source": "fred", "id": "IRLTLT01HUM156N", "label": "10Y bond yield"},
            "fx":   {"source": "ecb",  "id": "EXR/D.HUF.EUR.SP00.A", "label": "EUR/HUF", "frequency": "d"},
        }
    },

    # ── Romania (BNR) ────────────────────────────────────────────────────────
    "rom": {
        "name": "National Bank of Romania (BNR)",
        "country": "RO", "currency": "RON", "cpi_target": 2.5,
        "series": {
            "rate": {"source": "bis",  "id": "BIS,WS_CBPOL,1.0|D.RO",       "label": "Policy rate", "frequency": "d"},
            "cpi":  {"source": "bis",  "id": "BIS,WS_LONG_CPI,1.0|M.RO.771", "label": "CPI YoY"},
            "y2":   {"source": "fred", "id": "IR3TIB01ROM156N", "label": "3M interbank rate"},
            "y10":  {"source": "ecb",  "id": "IRS/M.RO.L.L40.CI.0000.RON.N.Z", "label": "10Y bond yield"},
            "fx":   {"source": "ecb",  "id": "EXR/D.RON.EUR.SP00.A", "label": "EUR/RON", "frequency": "d"},
        }
    },

    # ── Czech Republic (CNB) ─────────────────────────────────────────────────
    "cze": {
        "name": "Czech National Bank (CNB)",
        "country": "CZ", "currency": "CZK", "cpi_target": 2.0,
        "series": {
            "rate": {"source": "bis",  "id": "BIS,WS_CBPOL,1.0|M.CZ",       "label": "2W repo rate"},
            "cpi":  {"source": "bis",  "id": "BIS,WS_LONG_CPI,1.0|M.CZ.771", "label": "CPI YoY"},
            "y2":   {"source": "fred", "id": "IR3TIB01CZM156N", "label": "3M interbank rate"},
            "y10":  {"source": "fred", "id": "IRLTLT01CZM156N", "label": "10Y bond yield"},
            "fx":   {"source": "ecb",  "id": "EXR/D.CZK.EUR.SP00.A", "label": "EUR/CZK", "frequency": "d"},
        }
    },
}

DEFAULT_START_DATE = "2021-01-01"
