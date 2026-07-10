"""
Central Bank Policy Dashboard — Flask API
GET /api/data/<cb_key>   Chart data — monthly or daily rows, depending on the bank's series config
GET /api/meta/<cb_key>   Static metadata
GET /api/banks           List all CBs
GET /api/health          Pre-flight check
GET /api/status          Cache info
DELETE /api/cache        Clear cache
"""

import logging
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import threading
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

import cache
from assembler import assemble
from config import CB_CONFIG, DEFAULT_START_DATE, FRED_API_KEY

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)


def _error(message, status=400):
    return jsonify({"error": message}), status


def _cb_or_404(cb_key):
    cfg = CB_CONFIG.get(cb_key)
    if cfg is None:
        return None, _error(f"Unknown CB: '{cb_key}'. Valid: {', '.join(CB_CONFIG)}", 404)
    return cfg, None


@app.get("/")
def index():
    from flask import send_from_directory
    return send_from_directory(".", "cb-policy-dashboard-live.html")


@app.get("/image.png")
def logo():
    from flask import send_from_directory
    return send_from_directory(".", "image.png")


@app.get("/api/banks")
def list_banks():
    return jsonify({"banks": [
        {"key": k, "name": v["name"], "currency": v["currency"],
         "country": v["country"], "cpi_target": v["cpi_target"]}
        for k, v in CB_CONFIG.items()
    ]})


@app.get("/api/meta/<cb_key>")
def get_meta(cb_key):
    cfg, err = _cb_or_404(cb_key)
    if err:
        return err
    return jsonify({
        "key": cb_key, "name": cfg["name"], "country": cfg["country"],
        "currency": cfg["currency"], "cpi_target": cfg["cpi_target"],
        "series": {
            sk: {"label": sv.get("label", sk), "source": sv["source"],
                 "series_id": sv.get("id", "")}
            for sk, sv in cfg["series"].items()
        },
    })


@app.get("/api/health")
def health():
    checks = {}

    # FRED key
    if FRED_API_KEY:
        checks["fred_key"] = {"ok": True, "message": "FRED API key is set"}
    else:
        checks["fred_key"] = {
            "ok": False,
            "message": "FRED_API_KEY is not set in .env",
            "fix": "Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html "
                   "then add FRED_API_KEY=your_key to your .env file and restart the server."
        }

    # Reachability
    for name, url in [("fred", "https://api.stlouisfed.org"),
                      ("ecb",  "https://data-api.ecb.europa.eu"),
                      ("eurostat", "https://ec.europa.eu/eurostat")]:
        try:
            r = requests.head(url, timeout=4, allow_redirects=True)
            checks[name] = {"ok": True, "message": f"{name.upper()} is reachable ({r.status_code})"}
        except requests.ConnectionError:
            checks[name] = {"ok": False, "message": f"{name.upper()} unreachable — check internet connection"}
        except Exception as e:
            checks[name] = {"ok": False, "message": str(e)}

    all_ok = all(c["ok"] for c in checks.values())
    return jsonify({"ok": all_ok, "checks": checks})


@app.get("/api/analysis/<cb_key>")
def get_analysis(cb_key):
    """Compare the last two CB statements using Claude Haiku."""
    from config import ANTHROPIC_API_KEY
    _, err = _cb_or_404(cb_key)
    if err:
        return err

    fetcher_map = {
        "fed": "fetchers.fed_events", "ecb": "fetchers.ecb_events",
        "boe": "fetchers.boe_events", "boj": "fetchers.boj_events",
        "pol": "fetchers.nbp_events", "hun": "fetchers.mnb_events",
        "cze": "fetchers.cnb_events", "rom": "fetchers.bnr_events",
    }
    if cb_key not in fetcher_map:
        return jsonify({"analysis": None})

    import importlib
    mod = importlib.import_module(fetcher_map[cb_key])
    events = mod.fetch(start_date=DEFAULT_START_DATE, force=False)

    # Need at least 2 events with real summaries
    real = [e for e in events if e.get("body") and not e["body"].startswith("[")]
    if len(real) < 2:
        return jsonify({"analysis": None, "reason": "Not enough summaries yet"})

    last, prev = real[-1], real[-2]

    # Cache key based on both summaries so it only regenerates when content changes
    import hashlib, cache as cache_mod
    cache_key = f"analysis_{cb_key}_" + hashlib.md5(
        (last["body"] + prev["body"]).encode()
    ).hexdigest()[:12]
    cached = cache_mod.get(cache_key)
    if cached:
        return jsonify({"analysis": cached})

    if not ANTHROPIC_API_KEY:
        return jsonify({"analysis": None, "reason": "API key not set"})

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = (
            f"You are analysing two consecutive central bank policy statements.\n\n"
            f"PREVIOUS statement ({prev['d']}):\n{prev['body']}\n\n"
            f"LATEST statement ({last['d']}):\n{last['body']}\n\n"
            f"In 2-3 sentences, analyse what changed between the two meetings: "
            f"rate action, policy stance shift, or key economic assessment differences. "
            f"Be concise and factual."
        )
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        analysis = resp.content[0].text.strip()
        cache_mod.set(cache_key, analysis)
        return jsonify({"analysis": analysis, "last_date": last["d"], "prev_date": prev["d"]})
    except Exception as e:
        logger.error("Analysis failed for %s: %s", cb_key, e)
        return jsonify({"analysis": None, "error": str(e)})


@app.get("/api/events/<cb_key>")
def get_events(cb_key):
    _, err = _cb_or_404(cb_key)
    if err:
        return err
    start_date = request.args.get("start", DEFAULT_START_DATE)
    fetcher_map = {
        "fed": "fetchers.fed_events",
        "ecb": "fetchers.ecb_events",
        "boe": "fetchers.boe_events",
        "boj": "fetchers.boj_events",
        "pol": "fetchers.nbp_events",
        "hun": "fetchers.mnb_events",
        "cze": "fetchers.cnb_events",
        "rom": "fetchers.bnr_events",
    }
    if cb_key in fetcher_map:
        import importlib
        mod = importlib.import_module(fetcher_map[cb_key])
        try:
            # Always return immediately from the summary store.
            # The startup warmup and daily refresh handle generating
            # summaries for new statements in the background.
            events = mod.fetch(start_date=start_date, force=False)
            return jsonify({"events": events, "count": len(events)})
        except Exception as e:
            # If fetch() blocks on new summaries and times out, kick it off
            # in background and return an empty list so the page still loads.
            logger.error("Failed to fetch %s events: %s", cb_key, e)
            threading.Thread(
                target=lambda: mod.fetch(start_date=DEFAULT_START_DATE, force=False),
                daemon=True
            ).start()
            return jsonify({"events": [], "count": 0, "generating": True})
    return jsonify({"events": [], "count": 0})


@app.get("/api/data/<cb_key>")
def get_data(cb_key):
    cfg, err = _cb_or_404(cb_key)
    if err:
        return err

    start_date    = request.args.get("start", DEFAULT_START_DATE)
    force_refresh = request.args.get("refresh", "0") == "1"

    if force_refresh:
        cache.invalidate(f"assembled_{cb_key}_{start_date[:7]}")
        logger.info("Cache invalidated for %s", cb_key)

    try:
        rows = assemble(cb_key, start_date=start_date)
    except ValueError as e:
        return _error(str(e), 400)
    except Exception as e:
        logger.exception("Error assembling %s", cb_key)
        return _error(f"Data fetch failed: {e}", 502)

    # Expose per-series labels and sources for the frontend
    SOURCE_DISPLAY = {
        "fred": "FRED", "ecb": "ECB", "bis": "BIS", "boe": "BoE",
        "ons": "ONS", "nbp": "NBP", "bundesbank": "Bundesbank",
        "eurostat": "Eurostat", "nbp_ecb": "ECB",
    }
    series_meta = {
        sk: {
            "label":  sv.get("label", sk),
            "source": SOURCE_DISPLAY.get(sv.get("source", ""), sv.get("source", "")),
        }
        for sk, sv in cfg["series"].items()
    }
    return jsonify({
        "cb": cb_key, "name": cfg["name"], "currency": cfg["currency"],
        "cpi_target": cfg["cpi_target"], "start_date": start_date,
        "series": series_meta,
        "data": rows, "count": len(rows),
        "partial": any(
            any(r.get(k) is None for k in ("rate", "cpi", "y2", "y10"))
            for r in rows
        ),
    })


@app.get("/api/status")
def get_status():
    return jsonify({
        "status": "ok", "fred_key_set": bool(FRED_API_KEY),
        "available_banks": list(CB_CONFIG.keys()),
        "cache": cache.status(),
    })


@app.delete("/api/cache")
def clear_cache():
    count = cache.invalidate_all()
    return jsonify({"cleared": count})


EVENT_FETCHERS = [
    ("Fed", "fetchers.fed_events"),
    ("ECB", "fetchers.ecb_events"),
    ("BoE", "fetchers.boe_events"),
    ("BoJ", "fetchers.boj_events"),
    ("NBP", "fetchers.nbp_events"),
    ("MNB", "fetchers.mnb_events"),
    ("CNB", "fetchers.cnb_events"),
    ("BNR", "fetchers.bnr_events"),
]


def _run_event_fetchers(force=False):
    """Fetch/summarise all CB events in parallel background threads."""
    import importlib

    def _fetch_one(name, mod_path):
        try:
            mod = importlib.import_module(mod_path)
            events = mod.fetch(start_date=DEFAULT_START_DATE, force=force)
            logger.info("%s events: %d items", name, len(events))
        except Exception as e:
            logger.error("%s events fetch failed: %s", name, e)

    # Run all fetchers concurrently — each may take 1-3 min on first run
    workers = [
        threading.Thread(target=_fetch_one, args=(name, path), daemon=True)
        for name, path in EVENT_FETCHERS
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join()


def _events_startup_warmup():
    """On startup: pre-generate summaries AND pre-download slow data sources."""
    time.sleep(5)   # let the server finish initialising
    logger.info("Startup warmup beginning…")

    # Pre-download the Eurostat consumer survey zip (28MB) so the first
    # user request for HUN/POL/CZE/ROM/ECB doesn't block for 90 seconds.
    try:
        from fetchers.eurostat_consumer import _download_all
        _download_all(start_date=DEFAULT_START_DATE)
        logger.info("Eurostat consumer survey pre-cached")
    except Exception as e:
        logger.warning("Eurostat consumer pre-cache failed: %s", e)

    _run_event_fetchers(force=False)
    logger.info("Startup warmup complete")


def _events_daily_refresh():
    """Background thread: refresh CB events once every 24 hours."""
    while True:
        time.sleep(24 * 60 * 60)
        logger.info("Daily events refresh starting…")
        _run_event_fetchers(force=True)


if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    logger.info("Starting CB Policy API on http://localhost:%d", port)
    if not FRED_API_KEY:
        logger.warning("FRED_API_KEY not set — data fetches will fail")
    # Clear any stale cache from previous runs with missing API keys
    cleared = cache.invalidate_all()
    if cleared:
        logger.info("Cleared %d stale cache entries on startup", cleared)
    # Pre-generate any missing event summaries 5s after startup (non-blocking)
    threading.Thread(target=_events_startup_warmup, daemon=True).start()
    # Also schedule daily refresh
    threading.Thread(target=_events_daily_refresh, daemon=True).start()
    logger.info("Event warmup and daily refresh threads started")
    app.run(host="0.0.0.0", port=port, debug=debug)
