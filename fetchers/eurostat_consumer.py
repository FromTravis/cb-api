"""
Fetches consumer inflation expectations (balance score: "price trends next 12 months")
from the Eurostat REST API — dataset ei_bsco_m, indicator BS-PT-NY, unit BAL.

No zip download needed: each country is a small JSON response (~10KB vs 28MB zip).

Country → Eurostat geo code mapping:
  ECB  → EA21   (Euro area 21 countries, from 2026; same series as EA20 before)
  PL   → PL
  HU   → HU
  CZ   → CZ
  RO   → RO
"""

import logging

import requests
import cache
from config import DEFAULT_START_DATE

logger = logging.getLogger(__name__)

BASE_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
DATASET  = "ei_bsco_m"
HEADERS  = {"User-Agent": "Mozilla/5.0 (compatible; cb-api/1.0)"}

# Map from our 2-letter internal codes to Eurostat geo codes
GEO_MAP = {
    "EA": "EA21",   # Euro area (latest composition)
    "PL": "PL",
    "HU": "HU",
    "CZ": "CZ",
    "RO": "RO",
}


def fetch(country_code: str, start_date: str = DEFAULT_START_DATE) -> list[dict]:
    """
    Return [{date: 'YYYY-MM-01', value: float}] for the given country code.
    Balance score: % expecting higher prices minus % expecting lower prices.
    """
    cc      = country_code.upper()
    geo     = GEO_MAP.get(cc, cc)
    cache_key = f"eurostat_consumer_{cc}_{start_date[:7]}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    cutoff = start_date[:7]
    logger.info("Fetching Eurostat consumer survey for %s (geo=%s)", cc, geo)

    params = {
        "geo":              geo,
        "indic":            "BS-PT-NY",
        "s_adj":            "NSA",
        "unit":             "BAL",
        "format":           "JSON",
        "lang":             "EN",
        "sinceTimePeriod":  cutoff,
    }

    try:
        resp = requests.get(f"{BASE_URL}/{DATASET}", params=params,
                            headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Eurostat consumer fetch failed for %s: %s", cc, e)
        return []

    data = resp.json()
    time_index   = data["dimension"]["time"]["category"]["index"]
    pos_to_period = {v: k for k, v in time_index.items()}
    values = data.get("value", {})

    results = []
    for idx_str, val in values.items():
        period = pos_to_period.get(int(idx_str), "")
        if period and val is not None:
            try:
                results.append({"date": period + "-01", "value": round(float(val), 2)})
            except (ValueError, TypeError):
                pass

    results.sort(key=lambda r: r["date"])
    logger.debug("Eurostat consumer %s: %d monthly observations", cc, len(results))
    cache.set(cache_key, results)
    return results
