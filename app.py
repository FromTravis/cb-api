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
    }
    if cb_key in fetcher_map:
        import importlib
        mod = importlib.import_module(fetcher_map[cb_key])
        try:
            events = mod.fetch(start_date=start_date)
            return jsonify({"events": events, "count": len(events)})
        except Exception as e:
            logger.error("Failed to fetch %s events: %s", cb_key, e)
            return jsonify({"events": [], "error": str(e)})
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

    # Expose per-series labels so the frontend can label the FX axis correctly
    series_meta = {
        sk: {"label": sv.get("label", sk)}
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


def _events_daily_refresh():
    """Background thread: check for new CB events once every 24 hours."""
    while True:
        time.sleep(24 * 60 * 60)
        for name, mod_path in [("Fed", "fetchers.fed_events"), ("ECB", "fetchers.ecb_events"),
                               ("BoE", "fetchers.boe_events"), ("BoJ", "fetchers.boj_events")]:
            logger.info("Daily %s events refresh starting…", name)
            try:
                import importlib
                mod = importlib.import_module(mod_path)
                events = mod.fetch(start_date=DEFAULT_START_DATE, force=True)
                logger.info("Daily %s refresh complete: %d events", name, len(events))
            except Exception as e:
                logger.error("Daily %s refresh failed: %s", name, e)


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
    # Start daily background refresh for FOMC events (daemon = stops with server)
    threading.Thread(target=_events_daily_refresh, daemon=True).start()
    logger.info("Daily events refresh scheduled (Fed + ECB, runs every 24h)")
    app.run(host="0.0.0.0", port=port, debug=debug)
