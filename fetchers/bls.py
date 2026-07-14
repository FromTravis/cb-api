import logging
from datetime import datetime, timedelta

import requests

import cache
from config import DEFAULT_START_DATE

logger = logging.getLogger(__name__)
BASE_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data"


def _fetch_raw(series_id, start_date, frequency="m"):
    if frequency != "m":
        raise ValueError("BLS fetcher currently supports monthly data only")

    year = int(start_date[:4])
    current_year = datetime.today().year
    payload = {
        "seriesid": [series_id],
        "startyear": str(year),
        "endyear": str(current_year),
    }
    resp = requests.post(BASE_URL, json=payload, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("Status") != "REQUEST_SUCCEEDED":
        raise ValueError(payload.get("message") or "BLS request failed")

    results = []
    series = payload.get("Results", {}).get("series", [])
    if not series:
        return results

    for item in series[0].get("data", []):
        try:
            year_value = int(item["year"])
            period = item.get("period", "")
            month = int(period.replace("M", "")) if period.startswith("M") else 1
            value = item.get("value", ".")
            if value == ".":
                continue
            results.append({
                "date": f"{year_value:04d}-{month:02d}-01",
                "value": float(value),
            })
        except (KeyError, ValueError, TypeError):
            continue

    logger.debug("BLS %s: %d observations", series_id, len(results))
    return sorted(results, key=lambda row: row["date"])


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
        results.append({
            "date": d,
            "value": round((by_date[d] / prior_val - 1) * 100, 2),
        })
    return results


def fetch(series_id, transform=None, start_date=DEFAULT_START_DATE, frequency="m"):
    fetch_start = start_date
    if transform == "yoy_pct":
        fetch_start = f"{int(start_date[:4]) - 1}{start_date[4:]}"
    cache_key = f"bls_{series_id}_{frequency}_{fetch_start}"
    cached = cache.get(cache_key)
    if cached is None:
        logger.info("Fetching BLS %s (%s) from %s", series_id, frequency, fetch_start)
        cached = _fetch_raw(series_id, fetch_start, frequency)
        cache.set(cache_key, cached)
    return _to_yoy_pct(cached) if transform == "yoy_pct" else cached
