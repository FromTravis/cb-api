"""
Fetches data from the National Bank of Poland (NBP).

Two data sources:
  id="ref"  → Interest rate XML archive (reference rate = main policy rate)
              https://static.nbp.pl/dane/stopy/stopy_procentowe_archiwum.xml
              Records change dates only; assembler forward-fills between decisions.

  id="eur"  → REST API for EUR/PLN daily exchange rate (mid rate)
              https://api.nbp.pl/api/exchangerates/rates/a/eur/{start}/{end}/
              Max 367 days per request; paginated by year.
"""

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta

import requests
import cache
from config import DEFAULT_START_DATE

logger = logging.getLogger(__name__)

RATE_URL = "https://static.nbp.pl/dane/stopy/stopy_procentowe_archiwum.xml"
FX_URL   = "https://api.nbp.pl/api/exchangerates/rates/a/{code}/{start}/{end}/?format=json"
HEADERS  = {"User-Agent": "Mozilla/5.0 (compatible; cb-api/1.0)"}


# ── reference rate (policy rate) ─────────────────────────────────────────────

def _fetch_reference_rate(start_date: str) -> list[dict]:
    cache_key = f"nbp_ref_{start_date[:7]}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    logger.info("Fetching NBP reference rate from XML archive")
    resp = requests.get(RATE_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    cutoff = start_date[:10]
    results = []

    for entry in root.findall("pozycje"):
        dt_str = entry.get("obowiazuje_od", "")
        if not dt_str or dt_str < cutoff:
            continue
        for pos in entry.findall("pozycja"):
            if pos.get("id") != "ref":
                continue
            raw = pos.get("oprocentowanie", "").replace(",", ".")
            try:
                results.append({"date": dt_str, "value": float(raw)})
            except ValueError:
                pass

    results.sort(key=lambda r: r["date"])
    logger.debug("NBP ref rate: %d change dates since %s", len(results), cutoff)
    cache.set(cache_key, results)
    return results


# ── exchange rate (EUR/PLN daily) ─────────────────────────────────────────────

def _fetch_exchange_rate(currency_code: str, start_date: str) -> list[dict]:
    cache_key = f"nbp_fx_{currency_code}_{start_date[:7]}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    logger.info("Fetching NBP %s/PLN daily rates", currency_code.upper())

    start_dt = datetime.strptime(start_date[:10], "%Y-%m-%d").date()
    today    = date.today()
    results  = []

    # NBP API max window = 367 days; iterate by year-long chunks
    chunk_start = start_dt
    while chunk_start <= today:
        chunk_end = min(chunk_start + timedelta(days=366), today)
        url = FX_URL.format(
            code=currency_code.lower(),
            start=chunk_start.strftime("%Y-%m-%d"),
            end=chunk_end.strftime("%Y-%m-%d"),
        )
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 404:
                # No data for this period (e.g. future range)
                break
            resp.raise_for_status()
            for rate in resp.json().get("rates", []):
                results.append({
                    "date":  rate["effectiveDate"],
                    "value": float(rate["mid"]),
                })
        except requests.RequestException as e:
            logger.warning("NBP FX chunk %s–%s failed: %s", chunk_start, chunk_end, e)

        chunk_start = chunk_end + timedelta(days=1)

    results.sort(key=lambda r: r["date"])
    logger.debug("NBP %s/PLN: %d daily observations", currency_code.upper(), len(results))
    cache.set(cache_key, results)
    return results


# ── dispatcher ────────────────────────────────────────────────────────────────

def fetch(series_id: str, start_date: str = DEFAULT_START_DATE) -> list[dict]:
    """
    Dispatch to the right NBP source based on series_id:
      "ref"           → NBP reference rate (policy rate)
      any other code  → NBP daily exchange rate against PLN (e.g. "eur")
    """
    if series_id.lower() == "ref":
        return _fetch_reference_rate(start_date)
    return _fetch_exchange_rate(series_id, start_date)
