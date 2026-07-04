"""
Persistent key-value store for AI-generated summaries.
Keyed by SHA-256 of the source text so each statement is summarised once ever,
even across server restarts. New statements trigger a Claude call; existing ones
are served from the JSON file with zero API cost.
"""

import hashlib
import json
import logging
import os

logger = logging.getLogger(__name__)

# Lives in .data/ NOT .cache/ — the cache-clear endpoint deletes everything in
# .cache/, so the summary store must be kept separate to survive cache flushes.
STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), ".data", "fed_summaries.json"
)


def _key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_all() -> dict:
    try:
        with open(STORE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_all(db: dict) -> None:
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    with open(STORE_PATH, "w") as f:
        json.dump(db, f)
    logger.debug("Saved %d summaries to %s", len(db), STORE_PATH)


def key_for(text: str) -> str:
    return _key(text)
