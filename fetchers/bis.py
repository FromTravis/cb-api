"""
Fetches policy rate data from the BIS (Bank for International Settlements) API.
Endpoint: https://stats.bis.org/api/v1/data/{flow}/{key}/all

Requires Accept: application/vnd.sdmx.data+json header to receive JSON.
BIS wraps the SDMX response under {"meta": ..., "data": {"dataSets": [...], "structure": {...}}}.

Example:
  flow = "BIS,WS_CBPOL,1.0"
  key  = "D.JP"   → daily Japan central bank policy rate
"""

import logging
from datetime import date

import requests
import cache
from config import DEFAULT_START_DATE

logger = logging.getLogger(__name__)

BASE_URL = "https://stats.bis.org/api/v1/data"
HEADERS  = {
    "User-Agent": "Mozilla/5.0 (compatible; cb-api/1.0)",
    "Accept":     "application/vnd.sdmx.data+json",
}


def fetch(flow: str, key: str, start_date: str = DEFAULT_START_DATE) -> list[dict]:
    """
    Fetch a BIS SDMX series and return list of {"date": "YYYY-MM-DD", "value": float}.

    Args:
        flow:       SDMX flow identifier, e.g. "BIS,WS_CBPOL,1.0"
        key:        Series key, e.g. "D.JP"
        start_date: Earliest date to include
    """
    safe = flow.replace(",", "_").replace(".", "_") + "_" + key.replace(".", "_")
    cache_key = f"bis_{safe}_{start_date[:7]}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    url    = f"{BASE_URL}/{flow}/{key}/all"
    params = {"startPeriod": start_date[:10]}

    logger.info("Fetching BIS %s/%s from %s", flow, key, start_date[:10])
    resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    payload   = resp.json()
    data      = payload.get("data", payload)          # handle both wrapped and flat
    ds        = data["dataSets"][0]
    structure = data["structure"]

    # Time dimension → position index
    time_dim  = next(x for x in structure["dimensions"]["observation"] if x["id"] == "TIME_PERIOD")
    tv        = time_dim["values"]

    # First (and only) series
    sk        = next(iter(ds["series"]))
    obs       = ds["series"][sk]["observations"]

    results = []
    for idx_str, values in obs.items():
        period = tv[int(idx_str)]["id"]   # "YYYY-MM-DD" or "YYYY-MM"
        raw    = values[0]
        if raw is None:
            continue
        try:
            iso = period + "-01" if len(period) == 7 else period
            results.append({"date": iso, "value": float(raw)})
        except (ValueError, TypeError):
            pass

    results.sort(key=lambda r: r["date"])
    logger.debug("BIS %s/%s: %d observations", flow, key, len(results))
    cache.set(cache_key, results)
    return results
