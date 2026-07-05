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
            "y2":   {"source": "ecb",  "id": "FM/M.U2.EUR.4F.BB.U2_2Y.YLD", "label": "EA 2Y yield"},
            "y10":  {"source": "ecb",  "id": "FM/M.U2.EUR.4F.BB.U2_10Y.YLD", "label": "EA 10Y yield"},
            "fx":   {"source": "fred", "id": "DEXUSEU", "label": "EUR/USD"},
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
            "y2":   {"source": "boe", "id": "IUDSNPY", "label": "2Y Gilt nominal yield", "frequency": "d"},
            "y10":  {"source": "boe", "id": "IUDMNPY", "label": "10Y Gilt nominal yield","frequency": "d"},
            "fx":   {"source": "boe", "id": "XUDLERS", "label": "GBP/EUR",               "frequency": "d"},
        }
    },

    # ── Bank of Japan ────────────────────────────────────────────────────────
    "boj": {
        "name": "Bank of Japan (BoJ)",
        "country": "JP", "currency": "JPY", "cpi_target": 2.0,
        "series": {
            "rate": {"source": "fred", "id": "IRSTCB01JPM156N", "label": "Policy rate"},
            "cpi":  {"source": "fred", "id": "JPNCPIALLMINMEI", "label": "CPI YoY", "transform": "yoy_pct"},
            "y2":   {"source": "fred", "id": "IR3TIB01JPM156N", "label": "3M interbank rate"},
            "y10":  {"source": "fred", "id": "IRLTLT01JPM156N", "label": "10Y JGB yield"},
            "fx":   {"source": "fred", "id": "DEXJPUS", "label": "USD/JPY"},
        }
    },

    # ── Poland (NBP) ─────────────────────────────────────────────────────────
    "pol": {
        "name": "National Bank of Poland (NBP)",
        "country": "PL", "currency": "PLN", "cpi_target": 2.5,
        "series": {
            "rate": {"source": "nbp",  "id": "ref", "label": "NBP Reference Rate", "frequency": "d"},
            "cpi":  {"source": "fred", "id": "POLCPIALLMINMEI", "label": "CPI YoY", "transform": "yoy_pct"},
            "y2":   {"source": "fred", "id": "IR3TIB01PLM156N", "label": "3M interbank rate"},
            "y10":  {"source": "fred", "id": "IRLTLT01PLM156N", "label": "10Y bond yield"},
            "fx":   {"source": "nbp",  "id": "eur", "label": "EUR/PLN", "frequency": "d"},
        }
    },

    # ── Hungary (MNB) ────────────────────────────────────────────────────────
    "hun": {
        "name": "National Bank of Hungary (MNB)",
        "country": "HU", "currency": "HUF", "cpi_target": 3.0,
        "series": {
            "rate": {"source": "fred", "id": "IRSTCB01HUM156N", "label": "Base rate"},
            "cpi":  {"source": "fred", "id": "HUNCPIALLMINMEI", "label": "CPI YoY", "transform": "yoy_pct"},
            "y2":   {"source": "fred", "id": "IR3TIB01HUM156N", "label": "3M interbank rate"},
            "y10":  {"source": "fred", "id": "IRLTLT01HUM156N", "label": "10Y bond yield"},
            "fx":   {"source": "ecb",  "id": "EXR/M.HUF.EUR.SP00.A", "label": "EUR/HUF"},
        }
    },

    # ── Romania (BNR) ────────────────────────────────────────────────────────
    "rom": {
        "name": "National Bank of Romania (BNR)",
        "country": "RO", "currency": "RON", "cpi_target": 2.5,
        "series": {
            "rate": {"source": "fred", "id": "IRSTCB01ROM156N", "label": "Policy rate"},
            "cpi":  {"source": "fred", "id": "ROMCPIALLMINMEI", "label": "CPI YoY", "transform": "yoy_pct"},
            "y2":   {"source": "fred", "id": "IR3TIB01ROM156N", "label": "3M interbank rate"},
            "y10":  {"source": "fred", "id": "IRLTLT01ROM156N", "label": "10Y bond yield"},
            "fx":   {"source": "ecb",  "id": "EXR/M.RON.EUR.SP00.A", "label": "EUR/RON"},
        }
    },

    # ── Czech Republic (CNB) ─────────────────────────────────────────────────
    "cze": {
        "name": "Czech National Bank (CNB)",
        "country": "CZ", "currency": "CZK", "cpi_target": 2.0,
        "series": {
            "rate": {"source": "fred", "id": "IRSTCB01CZM156N", "label": "2W repo rate"},
            "cpi":  {"source": "fred", "id": "CZECPIALLMINMEI", "label": "CPI YoY", "transform": "yoy_pct"},
            "y2":   {"source": "fred", "id": "IR3TIB01CZM156N", "label": "3M interbank rate"},
            "y10":  {"source": "fred", "id": "IRLTLT01CZM156N", "label": "10Y bond yield"},
            "fx":   {"source": "ecb",  "id": "EXR/M.CZK.EUR.SP00.A", "label": "EUR/CZK"},
        }
    },
}

DEFAULT_START_DATE = "2021-01-01"
