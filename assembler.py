"""
Data assembler — fetches and merges series into monthly or daily rows.
Core series: rate, cpi, y2, y10 (always present).
Optional:    fx (currency pair) — present when CB_CONFIG has an "fx" entry.
"""

import logging
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)

import cache
from config import CB_CONFIG, DEFAULT_START_DATE
from fetchers.fred import fetch as fred_fetch
from fetchers.ecb  import fetch as ecb_fetch

logger = logging.getLogger(__name__)

CORE_SERIES = ("rate", "cpi", "y2", "y10")


def _to_ym_index(series_data):
    return {row["date"][:7]: row["value"] for row in series_data}


def _to_day_index(series_data):
    return {row["date"]: row["value"] for row in series_data}


def _forward_fill(index, periods):
    filled, last = {}, None
    for p in periods:
        if p in index:
            last = index[p]
        filled[p] = last
    return filled


def _all_months(start):
    from datetime import date
    sy, sm = int(start[:4]), int(start[5:7])
    today = date.today()
    months, y, m = [], sy, sm
    while (y, m) <= (today.year, today.month):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months


def _all_days(start):
    from datetime import date, timedelta
    d = date(int(start[:4]), int(start[5:7]), int(start[8:10]))
    today = date.today()
    days = []
    while d <= today:
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def _apply_transform(raw, transform):
    """Apply post-fetch transforms that don't need a full year of prior data."""
    if transform == "invert":
        return [
            {"date": r["date"], "value": round(1.0 / r["value"], 6)}
            for r in raw if r.get("value") and r["value"] != 0
        ]
    return raw


def _fetch_series(sc, start_date, frequency_override=None):
    """Fetch one series config entry; returns list of {date, value} dicts."""
    src     = sc["source"]
    sid     = sc["id"]
    transform = sc.get("transform")
    freq    = frequency_override or sc.get("frequency", "m")

    if src == "fred":
        # yoy_pct is handled inside fred_fetch; invert is handled here after
        fred_transform = transform if transform == "yoy_pct" else None
        raw = fred_fetch(sid, transform=fred_transform, start_date=start_date, frequency=freq)
        if transform == "invert":
            raw = _apply_transform(raw, "invert")
    elif src == "ecb":
        raw = ecb_fetch(sid, start_date=start_date)
        if transform in ("invert",):
            raw = _apply_transform(raw, transform)
    elif src == "eurostat":
        from fetchers.eurostat import fetch as eurostat_fetch
        # id format: "dataset|key=val|key=val", e.g. "irt_lt_mcby_m|geo=PL"
        parts  = sid.split("|")
        dataset = parts[0]
        params  = dict(p.split("=", 1) for p in parts[1:])
        raw = eurostat_fetch(dataset, params, start_date=start_date)
        if transform:
            raw = _apply_transform(raw, transform)
    elif src == "nbp":
        from fetchers.nbp import fetch as nbp_fetch
        raw = nbp_fetch(sid, start_date=start_date)
        if transform:
            raw = _apply_transform(raw, transform)
    elif src == "boe":
        from fetchers.boe_db import fetch as boe_fetch
        raw = boe_fetch(sid, start_date=start_date)
        if transform:
            raw = _apply_transform(raw, transform)
    elif src == "ons":
        from fetchers.ons import fetch as ons_fetch
        raw = ons_fetch(sid, start_date=start_date)
        # ONS D7G7 is already YoY % — no transform needed
    else:
        raw = []
    return raw


def assemble(cb_key, start_date=DEFAULT_START_DATE):
    cache_key = f"assembled_{cb_key}_{start_date[:7]}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    cfg = CB_CONFIG.get(cb_key)
    if cfg is None:
        raise ValueError(f"Unknown central bank: {cb_key!r}")

    series_cfg = cfg["series"]
    has_fx     = "fx" in series_cfg

    # Output granularity is determined by CORE series only — fx doesn't flip monthly
    # CBs to daily just because it has a daily source.
    daily = any(
        series_cfg[sk].get("frequency") == "d"
        for sk in CORE_SERIES
        if sk in series_cfg
    )
    to_index = _to_day_index if daily else _to_ym_index

    raw_indices, fetch_errors = {}, {}

    all_keys = list(CORE_SERIES) + (["fx"] if has_fx else [])

    for sk in all_keys:
        sc = series_cfg.get(sk)
        if sc is None:
            raw_indices[sk] = {}
            continue
        try:
            # For the fx series in a monthly-output CB, always fetch monthly
            freq_override = None if (sk != "fx" or daily) else "m"
            raw = _fetch_series(sc, start_date, frequency_override=freq_override)
            raw_indices[sk] = to_index(raw)
            logger.info("%s/%s: %d points fetched", cb_key, sk, len(raw_indices[sk]))
        except Exception as e:
            logger.error("Failed to fetch %s/%s: %s", cb_key, sk, e)
            fetch_errors[sk] = str(e)
            raw_indices[sk] = {}

    periods = _all_days(start_date) if daily else _all_months(start_date[:7])
    filled  = {sk: _forward_fill(raw_indices[sk], periods) for sk in all_keys}

    rows = []
    for p in periods:
        vals = {sk: filled[sk].get(p) for sk in all_keys}
        # Skip periods where all CORE series are null (ignore fx for this check)
        if all(vals.get(sk) is None for sk in CORE_SERIES):
            continue
        row = {"date": p}
        for sk in all_keys:
            v = vals[sk]
            # FX: more decimal places matter (e.g. EURUSD 1.1234)
            row[sk] = round(v, 4 if sk == "fx" else 2) if v is not None else None
        rows.append(row)

    if rows and not fetch_errors:
        cache.set(cache_key, rows)
    elif fetch_errors:
        logger.warning("Partial data for %s — not caching. Errors: %s", cb_key, fetch_errors)

    return rows
