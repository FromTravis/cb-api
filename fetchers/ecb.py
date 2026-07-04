import logging
import requests
import cache
from config import DEFAULT_START_DATE

logger = logging.getLogger(__name__)
BASE_URL = "https://data-api.ecb.europa.eu/service/data"

def fetch(series_key, start_date=DEFAULT_START_DATE):
    cache_key = f"ecb_{series_key}_{start_date[:7]}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    url = f"{BASE_URL}/{series_key}"
    params = {"startPeriod": start_date[:7], "format": "jsondata", "detail": "dataonly"}
    headers = {"Accept": "application/vnd.sdmx.data+json;version=1.0.0-wd"}
    logger.info("Fetching ECB %s", series_key)
    resp = requests.get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    try:
        dataset = payload["dataSets"][0]
        time_vals = next(d for d in payload["structure"]["dimensions"]["observation"] if d["id"] == "TIME_PERIOD")["values"]
        series_key_inner = next(iter(dataset["series"]))
        observations = dataset["series"][series_key_inner]["observations"]
        results = []
        for idx_str, obs in observations.items():
            if obs[0] is None:
                continue
            period = time_vals[int(idx_str)]["id"]
            date = period + "-01" if len(period) == 7 else period
            results.append({"date": date, "value": round(float(obs[0]), 4)})
        results.sort(key=lambda x: x["date"])
    except (KeyError, IndexError, TypeError) as e:
        logger.error("ECB parse error: %s", e)
        results = []
    cache.set(cache_key, results)
    return results
