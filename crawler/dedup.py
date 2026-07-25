"""
dedup.py — Deduplication store using a local JSON file.

Tracks which opportunity URLs have already been sent to Telegram.
Automatically cleans up entries older than retention_days.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

SEEN_FILE = Path("seen_urls.json")


def _load() -> dict:
    """Load the seen-URLs store from disk."""
    if SEEN_FILE.exists():
        try:
            with open(SEEN_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def _save(data: dict) -> None:
    """Persist the seen-URLs store to disk — compact one-line-per-entry format."""
    with open(SEEN_FILE, "w") as f:
        f.write("{\n")
        items = list(data.items())
        for i, (url, entry) in enumerate(items):
            comma = "," if i < len(items) - 1 else ""
            f.write(f'  {json.dumps(url)}: {json.dumps(entry)}{comma}\n')
        f.write("}\n")


def is_seen(url: str) -> bool:
    """Return True if this URL has already been sent."""
    data = _load()
    return url in data


def _get_timestamp(entry) -> str:
    """Extract timestamp from old (str), verbose dict, or new compact dict format."""
    if isinstance(entry, dict):
        return entry.get("t") or entry.get("seen_at", "")
    return entry


def mark_seen(url: str) -> None:
    """Mark a URL as seen with today's timestamp."""
    data = _load()
    data[url] = datetime.utcnow().isoformat()
    _save(data)


def mark_seen_bulk(urls: list[str], opportunities: list[dict] = None) -> None:
    """Mark multiple URLs as seen in one write.
    
    If opportunities list is provided (same order as urls), stores
    ai_score and title alongside the timestamp for easier auditing.
    """
    data = _load()
    now = datetime.utcnow().isoformat()
    opp_map = {}
    if opportunities:
        for opp in opportunities:
            url = opp.get("url", "")
            if url:
                opp_map[url] = opp

    for url in urls:
        opp = opp_map.get(url)
        if opp and opp.get("ai_score") is not None:
            data[url] = {"t": now[:19], "s": opp.get("ai_score", 0)}
        else:
            data[url] = {"t": now[:19], "s": 0}
    _save(data)


def cleanup(retention_days: int = 90) -> int:
    """
    Remove entries older than retention_days.
    Returns the number of entries removed.
    """
    data = _load()
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    to_delete = []

    for url, entry in data.items():
        try:
            ts = _get_timestamp(entry)
            seen_at = datetime.fromisoformat(ts)
            if seen_at < cutoff:
                to_delete.append(url)
        except (ValueError, TypeError):
            to_delete.append(url)

    for url in to_delete:
        del data[url]

    if to_delete:
        _save(data)

    return len(to_delete)


def stats() -> dict:
    """Return simple stats about the dedup store."""
    data = _load()
    return {
        "total_seen": len(data),
        "file": str(SEEN_FILE.absolute()),
    }
