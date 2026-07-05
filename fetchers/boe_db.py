"""
Fetches daily series from the Bank of England statistics database.
Endpoint: https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp

Supported series (examples):
  IUDBEDR  Official Bank Rate
  IUDMNPY  10Y Nominal Par Yield
  IUDSNPY  Short-term (2Y) Nominal Par Yield
  XUDLERS  EUR/GBP Spot Rate (= GBP/EUR, i.e. 1 GBP = X EUR)
"""

import csv
import io
import logging
from datetime import datetime, date

import requests
import cache
from config import DEFAULT_START_DATE

logger = logging.getLogger(__name__)

BASE_URL = "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
HEADERS  = {"User-Agent": "Mozilla/5.0 (compatible; cb-api/1.0)"}


def fetch(series_code: str, start_date: str = DEFAULT_START_DATE) -> list[dict]:
    """
    Fetch a single BoE daily series.
    Returns list of {"date": "YYYY-MM-DD", "value": float}.
    """
    cache_key = f"boe_{series_code}_{start_date[:7]}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # BoE expects DD/Mon/YYYY format
    try:
        start_dt = datetime.strptime(start_date[:10], "%Y-%m-%d")
    except ValueError:
        start_dt = datetime(int(start_date[:4]), 1, 1)

    today = date.today()
    params = {
        "csv.x":       "yes",
        "Datefrom":    start_dt.strftime("%d/%b/%Y"),
        "Dateto":      today.strftime("%d/%b/%Y"),
        "SeriesCodes": series_code,
        "CSVF":        "TN",
        "UsingCodes":  "Y",
        "VPD":         "Y",
        "VFD":         "N",
    }

    logger.info("Fetching BoE %s from %s", series_code, params["Datefrom"])
    resp = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    results = []
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        raw_date  = (row.get("DATE") or "").strip()
        raw_value = (row.get(series_code) or "").strip()
        if not raw_date or not raw_value:
            continue
        try:
            dt    = datetime.strptime(raw_date, "%d %b %Y")
            value = float(raw_value)
            results.append({"date": dt.strftime("%Y-%m-%d"), "value": value})
        except (ValueError, KeyError):
            continue

    results.sort(key=lambda r: r["date"])
    logger.debug("BoE %s: %d daily observations", series_code, len(results))
    cache.set(cache_key, results)
    return results
