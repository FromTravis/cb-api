"""
Simple file-based JSON cache with TTL.

Central bank data updates monthly, so a 12-hour cache is more than adequate.
Each cache entry is stored as a JSON file: .cache/<key>.json
The file contains { "fetched_at": <unix timestamp>, "data": <payload> }
"""

import json
import os
import time
import logging

from config import CACHE_DIR, CACHE_TTL_SECONDS

logger = logging.getLogger(__name__)


def _path(key: str) -> str:
    """Return the file path for a given cache key."""
    # sanitise key so it's safe as a filename
    safe = key.replace("/", "_").replace(":", "_")
    return os.path.join(CACHE_DIR, f"{safe}.json")


def _ensure_dir() -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)


def get(key: str):
    """
    Return cached data for key if it exists and is not expired.
    Returns None on miss or expiry.
    """
    fp = _path(key)
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, "r") as f:
            entry = json.load(f)
        age = time.time() - entry["fetched_at"]
        if age > CACHE_TTL_SECONDS:
            logger.debug("Cache expired for %s (age %.0fs)", key, age)
            return None
        logger.debug("Cache hit for %s (age %.0fs)", key, age)
        return entry["data"]
    except (json.JSONDecodeError, KeyError, OSError) as e:
        logger.warning("Cache read error for %s: %s", key, e)
        return None


def set(key: str, data) -> None:
    """Write data to cache with current timestamp."""
    _ensure_dir()
    fp = _path(key)
    try:
        with open(fp, "w") as f:
            json.dump({"fetched_at": time.time(), "data": data}, f)
        logger.debug("Cache written for %s", key)
    except OSError as e:
        logger.warning("Cache write error for %s: %s", key, e)


def invalidate(key: str) -> bool:
    """Delete a cache entry. Returns True if it existed."""
    fp = _path(key)
    if os.path.exists(fp):
        os.remove(fp)
        logger.info("Cache invalidated for %s", key)
        return True
    return False


def invalidate_all() -> int:
    """Delete all cache entries. Returns count of deleted files."""
    if not os.path.exists(CACHE_DIR):
        return 0
    count = 0
    for fname in os.listdir(CACHE_DIR):
        if fname.endswith(".json"):
            os.remove(os.path.join(CACHE_DIR, fname))
            count += 1
    logger.info("Cache cleared (%d entries)", count)
    return count


def status() -> dict:
    """Return a summary of current cache state (for the /status endpoint)."""
    if not os.path.exists(CACHE_DIR):
        return {"entries": 0, "files": []}
    files = []
    for fname in sorted(os.listdir(CACHE_DIR)):
        if not fname.endswith(".json"):
            continue
        fp = os.path.join(CACHE_DIR, fname)
        try:
            with open(fp) as f:
                entry = json.load(f)
            age = time.time() - entry["fetched_at"]
            files.append({
                "key": fname[:-5],
                "age_seconds": int(age),
                "expires_in_seconds": max(0, int(CACHE_TTL_SECONDS - age)),
                "expired": age > CACHE_TTL_SECONDS
            })
        except Exception:
            files.append({"key": fname[:-5], "error": "unreadable"})
    return {"entries": len(files), "ttl_seconds": CACHE_TTL_SECONDS, "files": files}
