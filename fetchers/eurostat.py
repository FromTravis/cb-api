"""
Fetches data from the Eurostat JSON API.
Documentation: https://ec.europa.eu/eurostat/api/dissemination/

Currently used for:
  irt_lt_mcby_m  — Long-term interest rates (EMU convergence criterion, 10Y bond yield)
                   geo=PL → Poland 10Y government bond yield (monthly, %)
"""

import logging
import requests
import cache
from config import DEFAULT_START_DATE

logger = logging.getLogger(__name__)

BASE_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
HEADERS  = {"User-Agent": "Mozilla/5.0 (compatible; cb-api/1.0)"}


def fetch(dataset: str, params: dict, start_date: str = DEFAULT_START_DATE) -> list[dict]:
    """
    Fetch a Eurostat JSON dataset and return list of {"date": "YYYY-MM-01", "value": float}.

    Args:
        dataset:    Eurostat dataset code, e.g. "irt_lt_mcby_m"
        params:     Filter parameters, e.g. {"geo": "PL"}
        start_date: Earliest date to include (YYYY-MM-DD)

    The Eurostat JSON response uses a flat value array indexed by position across all
    dimensions.  Since we filter to a single geo/rate, only the time dimension varies
    and the flat index maps directly to the ordered time periods.
    """
    cutoff = start_date[:7]   # YYYY-MM
    cache_key = f"eurostat_{dataset}_{'_'.join(f'{k}{v}' for k,v in sorted(params.items()))}_{cutoff}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    query = dict(params)
    query["format"] = "JSON"
    query["lang"]   = "EN"
    query["sinceTimePeriod"] = cutoff

    url = f"{BASE_URL}/{dataset}"
    logger.info("Fetching Eurostat %s %s", dataset, params)

    resp = requests.get(url, params=query, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    # Build position → period_string mapping from the time dimension
    time_index = (
        data.get("dimension", {})
            .get("time", {})
            .get("category", {})
            .get("index", {})
    )
    pos_to_period = {v: k for k, v in time_index.items()}
    values = data.get("value", {})

    results = []
    for idx_str, val in values.items():
        period = pos_to_period.get(int(idx_str), "")
        if period and val is not None:
            results.append({"date": period + "-01", "value": round(float(val), 4)})

    results.sort(key=lambda r: r["date"])
    logger.debug("Eurostat %s: %d observations", dataset, len(results))
    cache.set(cache_key, results)
    return results
