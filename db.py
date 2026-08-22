"""GeckoRegen SQLite schema + query functions.

Schema per DESIGN.md. stdlib sqlite3 only — no ORM.
All queries parameterized; no string interpolation into SQL.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def _data_dir() -> str:
    override = os.environ.get("DATA_DIR")
    if override:
        return override
    # Vercel filesystem is read-only except /tmp
    if os.environ.get("VERCEL"):
        return "/tmp/geckoregen-data"
    return os.path.join(PROJECT_DIR, "data")


DATA_DIR = _data_dir()
DB_PATH = os.environ.get("DATABASE_PATH") or os.path.join(DATA_DIR, "geckoregen.db")
os.makedirs(DATA_DIR, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS regulators (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    full_name TEXT,
    jurisdiction TEXT,
    url TEXT NOT NULL,
    collector_id TEXT,
    active INTEGER DEFAULT 1,
    scan_frequency_minutes INTEGER DEFAULT 360,
    created_at TEXT DEFAULT (datetime('now')),
    last_scanned_at TEXT
);

CREATE TABLE IF NOT EXISTS health_scores (
    id INTEGER PRIMARY KEY,
    regulator_id INTEGER REFERENCES regulators(id),
    timestamp TEXT DEFAULT (datetime('now')),
    status TEXT NOT NULL,
    field_population_rate REAL,
    record_count INTEGER,
    missing_fields TEXT,
    error_details TEXT
);

CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY,
    regulator_id INTEGER REFERENCES regulators(id),
    detected_at TEXT DEFAULT (datetime('now')),
    title TEXT,
    category TEXT,
    publish_date TEXT,
    summary TEXT,
    article_url TEXT,
    severity TEXT,
    is_new INTEGER DEFAULT 1,
    snapshot_id TEXT
);

CREATE TABLE IF NOT EXISTS healing_events (
    id INTEGER PRIMARY KEY,
    regulator_id INTEGER REFERENCES regulators(id),
    triggered_at TEXT DEFAULT (datetime('now')),
    broken_fields TEXT,
    heal_prompt TEXT,
    bd_job_id TEXT,
    status TEXT,
    attempts INTEGER DEFAULT 1,
    validation_passed INTEGER,
    validation_details TEXT,
    completed_at TEXT,
    duration_seconds INTEGER
);

CREATE TABLE IF NOT EXISTS regulator_profiles (
    id INTEGER PRIMARY KEY,
    regulator_id INTEGER REFERENCES regulators(id),
    avg_publications_per_week REAL,
    common_change_types TEXT,
    seasonal_patterns TEXT,
    last_profiled_at TEXT,
    total_changes_detected INTEGER DEFAULT 0,
    total_heals INTEGER DEFAULT 0,
    successful_heals INTEGER DEFAULT 0
);
"""


@contextmanager
def get_conn(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a sqlite3 connection; commit on success, rollback on error."""
    db_path = db_path or DB_PATH
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str = DB_PATH) -> None:
    """Create all 5 tables from DESIGN.md schema (regulators, health_scores,
    changes, healing_events, regulator_profiles). Idempotent."""
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)


# --- insert functions -----------------------------------------------------


def insert_regulator(
    name: str,
    url: str,
    full_name: str | None = None,
    jurisdiction: str | None = None,
    collector_id: str | None = None,
    active: int = 1,
    scan_frequency_minutes: int = 360,
    last_scanned_at: str | None = None,
    db_path: str = DB_PATH,
) -> int:
    """Insert a regulator row; return new row id."""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO regulators
               (name, full_name, jurisdiction, url, collector_id, active,
                scan_frequency_minutes, last_scanned_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (name, full_name, jurisdiction, url, collector_id, active,
             scan_frequency_minutes, last_scanned_at),
        )
        return cur.lastrowid


def insert_health(
    regulator_id: int,
    status: str,
    field_population_rate: float | None = None,
    record_count: int | None = None,
    missing_fields: list[str] | None = None,
    error_details: str | None = None,
    db_path: str = DB_PATH,
) -> int:
    """Insert a health_scores row; missing_fields stored as JSON array."""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO health_scores
               (regulator_id, status, field_population_rate, record_count,
                missing_fields, error_details)
               VALUES (?,?,?,?,?,?)""",
            (regulator_id, status, field_population_rate, record_count,
             json.dumps(missing_fields) if missing_fields else None,
             error_details),
        )
        return cur.lastrowid


def insert_change(
    regulator_id: int,
    title: str | None = None,
    category: str | None = None,
    publish_date: str | None = None,
    summary: str | None = None,
    article_url: str | None = None,
    severity: str | None = None,
    is_new: int = 1,
    snapshot_id: str | None = None,
    db_path: str = DB_PATH,
) -> int:
    """Insert a change detection row; return new row id."""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO changes
               (regulator_id, title, category, publish_date, summary,
                article_url, severity, is_new, snapshot_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (regulator_id, title, category, publish_date, summary,
             article_url, severity, is_new, snapshot_id),
        )
        return cur.lastrowid


def insert_heal(
    regulator_id: int,
    broken_fields: list[str] | None = None,
    heal_prompt: str | None = None,
    bd_job_id: str | None = None,
    status: str | None = None,
    attempts: int = 1,
    validation_passed: int | None = None,
    validation_details: dict[str, Any] | None = None,
    completed_at: str | None = None,
    duration_seconds: int | None = None,
    db_path: str = DB_PATH,
) -> int:
    """Insert a healing_events row; broken_fields/validation_details as JSON."""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO healing_events
               (regulator_id, broken_fields, heal_prompt, bd_job_id, status,
                attempts, validation_passed, validation_details,
                completed_at, duration_seconds)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (regulator_id,
             json.dumps(broken_fields) if broken_fields else None,
             heal_prompt, bd_job_id, status, attempts, validation_passed,
             json.dumps(validation_details) if validation_details else None,
             completed_at, duration_seconds),
        )
        return cur.lastrowid


def insert_profile(
    regulator_id: int,
    avg_publications_per_week: float | None = None,
    common_change_types: list[str] | None = None,
    seasonal_patterns: dict[str, str] | None = None,
    last_profiled_at: str | None = None,
    total_changes_detected: int = 0,
    total_heals: int = 0,
    successful_heals: int = 0,
    db_path: str = DB_PATH,
) -> int:
    """Insert a regulator_profiles row; common_change_types/seasonal_patterns
    stored as JSON."""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO regulator_profiles
               (regulator_id, avg_publications_per_week, common_change_types,
                seasonal_patterns, last_profiled_at, total_changes_detected,
                total_heals, successful_heals)
               VALUES (?,?,?,?,?,?,?,?)""",
            (regulator_id, avg_publications_per_week,
             json.dumps(common_change_types) if common_change_types else None,
             json.dumps(seasonal_patterns) if seasonal_patterns else None,
             last_profiled_at, total_changes_detected, total_heals,
             successful_heals),
        )
        return cur.lastrowid


# --- query functions ------------------------------------------------------


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a sqlite3.Row to a dict, decoding known JSON columns."""
    d = dict(row)
    json_cols = {
        "missing_fields", "broken_fields", "validation_details",
        "common_change_types", "seasonal_patterns",
    }
    for col in json_cols:
        if col in d and isinstance(d[col], str):
            try:
                d[col] = json.loads(d[col])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


def get_regulators(db_path: str = DB_PATH) -> list[dict[str, Any]]:
    """Return all regulators (active first)."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM regulators ORDER BY active DESC, name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_health(reg_id: int, db_path: str = DB_PATH) -> list[dict[str, Any]]:
    """Return all health_scores rows for a regulator, newest first."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM health_scores WHERE regulator_id = ? "
            "ORDER BY timestamp DESC",
            (reg_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_changes(limit: int = 50, db_path: str = DB_PATH) -> list[dict[str, Any]]:
    """Return recent changes, newest first."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM changes ORDER BY detected_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_heals(limit: int = 50, db_path: str = DB_PATH) -> list[dict[str, Any]]:
    """Return recent healing_events, newest first."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM healing_events ORDER BY triggered_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_profile(reg_id: int, db_path: str = DB_PATH) -> dict[str, Any] | None:
    """Return the regulator_profiles row for a regulator, or None."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM regulator_profiles WHERE regulator_id = ?",
            (reg_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


# --- update functions -----------------------------------------------------


def update_health(
    reg_id: int,
    status: str,
    field_population_rate: float | None = None,
    record_count: int | None = None,
    missing_fields: list[str] | None = None,
    error_details: str | None = None,
    db_path: str = DB_PATH,
) -> int:
    """Insert a new health_scores row for reg_id (health is append-only log).
    Returns the new row id."""
    return insert_health(
        reg_id, status, field_population_rate, record_count,
        missing_fields, error_details, db_path,
    )


def update_profile(
    reg_id: int,
    avg_publications_per_week: float | None = None,
    common_change_types: list[str] | None = None,
    seasonal_patterns: dict[str, str] | None = None,
    total_changes_detected: int | None = None,
    total_heals: int | None = None,
    successful_heals: int | None = None,
    db_path: str = DB_PATH,
) -> int:
    """Upsert regulator_profiles row for reg_id. Updates last_profiled_at to
    now. Only provided fields are updated (None = leave unchanged). Returns
    the profile row id."""
    with get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM regulator_profiles WHERE regulator_id = ?",
            (reg_id,),
        ).fetchone()

        if existing:
            sets: list[str] = ["last_profiled_at = datetime('now')"]
            params: list[Any] = []
            mapping = {
                "avg_publications_per_week": avg_publications_per_week,
                "total_changes_detected": total_changes_detected,
                "total_heals": total_heals,
                "successful_heals": successful_heals,
            }
            for col, val in mapping.items():
                if val is not None:
                    sets.append(f"{col} = ?")
                    params.append(val)
            if common_change_types is not None:
                sets.append("common_change_types = ?")
                params.append(json.dumps(common_change_types))
            if seasonal_patterns is not None:
                sets.append("seasonal_patterns = ?")
                params.append(json.dumps(seasonal_patterns))
            params.append(reg_id)
            conn.execute(
                f"UPDATE regulator_profiles SET {', '.join(sets)} "
                "WHERE regulator_id = ?",
                params,
            )
            return existing["id"]
        else:
            cur = conn.execute(
                """INSERT INTO regulator_profiles
                   (regulator_id, avg_publications_per_week,
                    common_change_types, seasonal_patterns,
                    last_profiled_at, total_changes_detected,
                    total_heals, successful_heals)
                   VALUES (?,?,?,?,datetime('now'),?,?,?)""",
                (reg_id, avg_publications_per_week,
                 json.dumps(common_change_types) if common_change_types else None,
                 json.dumps(seasonal_patterns) if seasonal_patterns else None,
                 total_changes_detected or 0, total_heals or 0,
                 successful_heals or 0),
            )
            return cur.lastrowid


def mark_change_seen(change_id: int, db_path: str = DB_PATH) -> int:
    """Mark a change as no longer new (is_new = 0). Returns rows affected."""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE changes SET is_new = 0 WHERE id = ?",
            (change_id,),
        )
        return cur.rowcount


# --- self-check -----------------------------------------------------------


def _self_check() -> None:
    """Create temp DB, insert test data, query it back, assert it matches."""
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    test_db = tmp.name

    try:
        init_db(test_db)

        # insert regulator
        rid = insert_regulator(
            name="FCA",
            url="https://www.fca.org.uk/news",
            full_name="Financial Conduct Authority",
            jurisdiction="UK",
            collector_id="c_fca_001",
            db_path=test_db,
        )
        assert rid > 0, "insert_regulator returned non-positive id"

        # query back
        regs = get_regulators(test_db)
        assert len(regs) == 1, f"expected 1 regulator, got {len(regs)}"
        r = regs[0]
        assert r["name"] == "FCA", f"name mismatch: {r['name']}"
        assert r["full_name"] == "Financial Conduct Authority"
        assert r["jurisdiction"] == "UK"
        assert r["url"] == "https://www.fca.org.uk/news"
        assert r["collector_id"] == "c_fca_001"
        assert r["active"] == 1

        # insert + query health
        hid = insert_health(
            rid, "healthy", field_population_rate=0.95, record_count=42,
            missing_fields=["summary"], db_path=test_db,
        )
        h = get_health(rid, test_db)
        assert len(h) == 1, f"expected 1 health row, got {len(h)}"
        assert h[0]["status"] == "healthy"
        assert h[0]["field_population_rate"] == 0.95
        assert h[0]["record_count"] == 42
        assert h[0]["missing_fields"] == ["summary"], \
            f"JSON decode failed: {h[0]['missing_fields']}"

        # insert + query change, mark seen
        cid = insert_change(
            rid, title="New guidance on crypto", severity="warning",
            db_path=test_db,
        )
        ch = get_changes(10, test_db)
        assert len(ch) == 1
        assert ch[0]["title"] == "New guidance on crypto"
        assert ch[0]["is_new"] == 1
        affected = mark_change_seen(cid, db_path=test_db)
        assert affected == 1, f"mark_change_seen affected {affected} rows"
        ch2 = get_changes(10, test_db)
        assert ch2[0]["is_new"] == 0, "is_new not updated to 0"

        # insert + query heal
        insert_heal(
            rid, broken_fields=["summary"], heal_prompt="fix summary",
            status="done", validation_passed=1,
            validation_details={"rate": 0.95}, db_path=test_db,
        )
        heals = get_heals(10, test_db)
        assert len(heals) == 1
        assert heals[0]["status"] == "done"
        assert heals[0]["broken_fields"] == ["summary"]
        assert heals[0]["validation_details"] == {"rate": 0.95}

        # insert + query + update profile
        pid = insert_profile(
            rid, avg_publications_per_week=3.2,
            common_change_types=["enforcement", "guidance"],
            seasonal_patterns={"summer": "low"}, db_path=test_db,
        )
        p = get_profile(rid, test_db)
        assert p is not None
        assert p["avg_publications_per_week"] == 3.2
        assert p["common_change_types"] == ["enforcement", "guidance"]
        assert p["seasonal_patterns"] == {"summer": "low"}

        # update profile (upsert existing)
        update_profile(rid, total_changes_detected=5, total_heals=2,
                       successful_heals=2, db_path=test_db)
        p2 = get_profile(rid, test_db)
        assert p2["total_changes_detected"] == 5
        assert p2["total_heals"] == 2
        assert p2["successful_heals"] == 2
        assert p2["avg_publications_per_week"] == 3.2  # unchanged

        # update_profile on new regulator (insert path) — use a real reg_id
        rid2 = insert_regulator(name="ESMA", url="https://www.esma.europa.eu",
                                db_path=test_db)
        update_profile(rid2, avg_publications_per_week=1.0, db_path=test_db)
        p3 = get_profile(rid2, test_db)
        assert p3 is not None
        assert p3["avg_publications_per_week"] == 1.0

        print("SELF-CHECK PASSED: all assertions hold")
    finally:
        if os.path.exists(test_db):
            os.remove(test_db)


if __name__ == "__main__":
    _self_check()
