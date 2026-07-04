import logging
from datetime import datetime, timedelta
import requests
import cache
from config import FRED_API_KEY, DEFAULT_START_DATE

logger = logging.getLogger(__name__)
BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

def _fetch_raw(series_id, start_date, frequency="m"):
    if not FRED_API_KEY:
        raise ValueError("FRED_API_KEY is not set in .env")
    params = {
        "series_id": series_id, "api_key": FRED_API_KEY,
        "file_type": "json", "observation_start": start_date,
        "frequency": frequency,
    }
    if frequency == "m":
        params["aggregation_method"] = "avg"
    resp = requests.get(BASE_URL, params=params, timeout=10)
    resp.raise_for_status()
    results = []
    for obs in resp.json().get("observations", []):
        val = obs.get("value", ".")
        if val != ".":
            try:
                results.append({"date": obs["date"], "value": float(val)})
            except (ValueError, KeyError):
                pass
    logger.debug("FRED %s: %d observations", series_id, len(results))
    return results

def _to_yoy_pct(raw):
    if len(raw) < 13:
        return []
    by_date = {r["date"]: r["value"] for r in raw}
    results = []
    for d in sorted(by_date):
        dt = datetime.strptime(d, "%Y-%m-%d")
        prior = (dt - timedelta(days=365)).strftime("%Y-%m")
        matches = [k for k in by_date if k.startswith(prior)]
        if not matches:
            continue
        prior_val = by_date[matches[0]]
        if prior_val == 0:
            continue
        results.append({"date": d, "value": round((by_date[d] / prior_val - 1) * 100, 2)})
    return results

def fetch(series_id, transform=None, start_date=DEFAULT_START_DATE, frequency="m"):
    fetch_start = start_date
    if transform == "yoy_pct":
        fetch_start = f"{int(start_date[:4]) - 1}{start_date[4:]}"
    cache_key = f"fred_{series_id}_{frequency}_{fetch_start}"
    cached = cache.get(cache_key)
    if cached is None:
        logger.info("Fetching FRED %s (%s) from %s", series_id, frequency, fetch_start)
        cached = _fetch_raw(series_id, fetch_start, frequency)
        cache.set(cache_key, cached)
    return _to_yoy_pct(cached) if transform == "yoy_pct" else cached
