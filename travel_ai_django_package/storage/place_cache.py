"""SQLite-backed Places cache (TTL-based).

This cache is meant to reduce:
  - Google Places Text Search calls (query -> place_id mapping)
  - Re-resolution of English display name for non-KR locations
  - Re-fetching photo_reference for Places Photo

Policy note
  - `place_id` can be stored long-term.
  - Other details (name, address, lat/lon, photo_reference) are cached with TTL.

The cache is used as a performance layer and can be safely deleted.
"""

from __future__ import annotations

import os
import sqlite3
import time
import hashlib
from typing import Optional, Dict, Any, Tuple


def _now() -> int:
    return int(time.time())


def _instance_dir(base_dir: Optional[str] = None) -> str:
    # base_dir is the project root where app.py lives.
    if base_dir is None:
        base_dir = os.path.join(os.path.dirname(__file__), "..")
    inst = os.path.join(base_dir, "instance")
    os.makedirs(inst, exist_ok=True)
    return inst


def _db_path(base_dir: Optional[str] = None) -> str:
    return os.path.join(_instance_dir(base_dir), "places_cache.db")


def _connect(base_dir: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(base_dir), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=3000;")
    except Exception:
        pass
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS places_cache (
            place_id TEXT PRIMARY KEY,
            name_en TEXT,
            photo_reference TEXT,
            lat REAL,
            lon REAL,
            rating REAL,
            user_ratings_total INTEGER,
            updated_at INTEGER,
            expires_at INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS query_cache (
            query_key TEXT PRIMARY KEY,
            place_id TEXT,
            updated_at INTEGER,
            expires_at INTEGER
        )
        """
    )

    # Ensure forward-compatible columns (safe for existing DBs)
    try:
        cur2 = conn.cursor()
        cols = [row[1] for row in cur2.execute("PRAGMA table_info(places_cache)").fetchall()]
        if "rating" not in cols:
            cur2.execute("ALTER TABLE places_cache ADD COLUMN rating REAL")
        if "user_ratings_total" not in cols:
            cur2.execute("ALTER TABLE places_cache ADD COLUMN user_ratings_total INTEGER")
    except Exception:
        pass
    conn.commit()


def _normalize_query_key(query: str, lat: float = None, lon: float = None, radius_m: int = None) -> str:
    q = " ".join((query or "").strip().lower().split())
    # Round lat/lon to reduce cardinality but keep locality.
    if lat is not None and lon is not None:
        try:
            lat_r = round(float(lat), 3)
            lon_r = round(float(lon), 3)
        except Exception:
            lat_r, lon_r = 0.0, 0.0
        r = int(radius_m or 0)
        q = f"{q}|{lat_r},{lon_r}|r{r}"
    else:
        q = f"{q}|noloc"

    # Keep key short + consistent
    h = hashlib.sha1(q.encode("utf-8", errors="ignore")).hexdigest()
    return f"q:{h}"


class PlacesCache:
    """Thin wrapper around SQLite cache."""

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = base_dir
        self._conn = _connect(base_dir)
        _ensure_schema(self._conn)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def get_place_id_for_query(self, query: str, lat: float = None, lon: float = None, radius_m: int = None) -> Optional[str]:
        key = _normalize_query_key(query, lat, lon, radius_m)
        cur = self._conn.cursor()
        cur.execute("SELECT place_id, expires_at FROM query_cache WHERE query_key=?", (key,))
        row = cur.fetchone()
        if not row:
            return None
        exp = int(row["expires_at"] or 0)
        if exp and exp < _now():
            return None
        pid = (row["place_id"] or "").strip()
        return pid or None

    def set_query_mapping(self, query: str, place_id: str, ttl_seconds: int, lat: float = None, lon: float = None, radius_m: int = None) -> None:
        if not query or not place_id:
            return
        key = _normalize_query_key(query, lat, lon, radius_m)
        ts = _now()
        exp = ts + int(ttl_seconds or 0)
        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO query_cache(query_key, place_id, updated_at, expires_at) VALUES(?,?,?,?)",
            (key, place_id, ts, exp),
        )
        self._conn.commit()

    def get_place(self, place_id: str) -> Optional[Dict[str, Any]]:
        pid = (place_id or "").strip()
        if not pid:
            return None
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM places_cache WHERE place_id=?", (pid,))
        row = cur.fetchone()
        if not row:
            return None
        exp = int(row["expires_at"] or 0)
        if exp and exp < _now():
            return None
        # rating fields are optional (older DBs may not have columns)
        try:
            rating = row["rating"]
        except Exception:
            rating = None
        try:
            urt = row["user_ratings_total"]
        except Exception:
            urt = None
        return {
            "place_id": pid,
            "name": (row["name_en"] or "").strip(),
            "photo_reference": (row["photo_reference"] or "").strip(),
            "lat": row["lat"],
            "lon": row["lon"],
            "rating": rating,
            "user_ratings_total": urt,
        }

    def set_place(self, place_id: str, name_en: str = "", photo_reference: str = "", lat: float = None, lon: float = None, rating: float = None, user_ratings_total: int = None, ttl_seconds: int = 0) -> None:
        pid = (place_id or "").strip()
        if not pid:
            return
        ts = _now()
        exp = ts + int(ttl_seconds or 0)
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO places_cache(place_id, name_en, photo_reference, lat, lon, rating, user_ratings_total, updated_at, expires_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (pid, (name_en or "").strip(), (photo_reference or "").strip(), lat, lon, rating, user_ratings_total, ts, exp),
        )
        self._conn.commit()
