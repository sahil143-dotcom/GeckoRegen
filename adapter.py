"""GeckoRegen adaptive scan frequency scheduler (Layer 4 — Adapter).

Adjusts per-regulator scan frequency based on learned publication patterns.
stdlib only (datetime, statistics). Imports from db.py.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any

import db


# --- helpers ---------------------------------------------------------------


def _parse_ts(s: str | None) -> datetime | None:
    """Parse SQLite datetime('now')-style 'YYYY-MM-DD HH:MM:SS' strings."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _recent_changes(regulator_id: int, limit: int = 50, db_path: str = db.DB_PATH) -> list[datetime]:
    """Return detected_at timestamps for the last N changes, oldest first."""
    with db.get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT detected_at FROM changes WHERE regulator_id = ? "
            "ORDER BY detected_at DESC LIMIT ?",
            (regulator_id, limit),
        ).fetchall()
    ts = [_parse_ts(r["detected_at"]) for r in rows]
    return [t for t in reversed(ts) if t is not None]


# --- core API --------------------------------------------------------------


def compute_scan_frequency(regulator_id: int, sample_size: int = 20, db_path: str = db.DB_PATH) -> int:
    """Query DB for last N changes, compute publications per week, and
    return a recommended scan interval in minutes.

    Heuristic: aim to scan roughly twice per expected publication interval.
    Floor of 30 min (rate-limit safety), ceiling of 1440 min (daily).
    Falls back to 360 min (6h, schema default) when insufficient data.
    """
    times = _recent_changes(regulator_id, limit=sample_size, db_path=db_path)
    if len(times) < 2:
        return 360  # ponytail: not enough data; default. upgrade path: per-reg profile lookup

    span_days = (times[-1] - times[0]).total_seconds() / 86400.0
    if span_days <= 0:
        # all changes share a timestamp — burst; treat as 1 day span
        span_days = 1.0
    pubs_per_week = (len(times) / span_days) * 7.0

    if pubs_per_week <= 0:
        return 360

    # scan twice per publication interval => interval = half the avg gap
    avg_gap_minutes = (span_days * 1440.0) / len(times)
    recommended = max(30, min(1440, int(avg_gap_minutes / 2)))
    return recommended


def adjust_frequency(regulator_id: int, db_path: str = db.DB_PATH) -> int:
    """Compute the recommended scan frequency and persist it to
    regulators.scan_frequency_minutes. Returns the new interval."""
    new_freq = compute_scan_frequency(regulator_id, db_path=db_path)
    with db.get_conn(db_path) as conn:
        conn.execute(
            "UPDATE regulators SET scan_frequency_minutes = ? WHERE id = ?",
            (new_freq, regulator_id),
        )
    return new_freq


def detect_anomaly(regulator_id: int, window_hours: int = 24, db_path: str = db.DB_PATH) -> dict[str, Any] | None:
    """Count changes in the last `window_hours` and compare against the
    historical average over the same window size. If the recent count
    exceeds 2x the historical average, return an anomaly alert dict;
    otherwise return None."""
    now = datetime.utcnow()
    window_start = now - timedelta(hours=window_hours)

    with db.get_conn(db_path) as conn:
        recent = conn.execute(
            "SELECT COUNT(*) AS c FROM changes "
            "WHERE regulator_id = ? AND detected_at >= ?",
            (regulator_id, window_start.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()["c"]

        # historical: average changes per `window_hours` across all history
        total_row = conn.execute(
            "SELECT COUNT(*) AS c, MIN(detected_at) AS first, MAX(detected_at) AS last "
            "FROM changes WHERE regulator_id = ?",
            (regulator_id,),
        ).fetchone()
        total = total_row["c"]
        first = _parse_ts(total_row["first"])
        last_ts = _parse_ts(total_row["last"])

    if total < 2 or first is None or last_ts is None:
        # insufficient history; no baseline to call anomaly
        return None

    history_days = max(1.0, (last_ts - first).total_seconds() / 86400.0)
    windows_in_history = history_days * (24.0 / window_hours)
    historical_avg = total / windows_in_history

    if historical_avg <= 0:
        return None

    if recent > 2 * historical_avg:
        return {
            "regulator_id": regulator_id,
            "window_hours": window_hours,
            "recent_count": recent,
            "historical_avg": round(historical_avg, 3),
            "threshold": 2 * historical_avg,
            "alert": "anomaly: recent change rate exceeds 2x historical average",
        }
    return None


def get_scan_schedule(regulator_id: int, db_path: str = db.DB_PATH) -> dict[str, Any]:
    """Return the next scan time based on current scan_frequency_minutes
    and last_scanned_at. If already due (or never scanned), next_scan is now.
    """
    with db.get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT scan_frequency_minutes, last_scanned_at FROM regulators WHERE id = ?",
            (regulator_id,),
        ).fetchone()

    if row is None:
        raise ValueError(f"regulator {regulator_id} not found")

    freq = row["scan_frequency_minutes"] or 360
    last = _parse_ts(row["last_scanned_at"])
    now = datetime.utcnow()

    if last is None:
        next_scan = now  # never scanned -> due immediately
        due = True
    else:
        next_scan = last + timedelta(minutes=freq)
        due = now >= next_scan

    return {
        "regulator_id": regulator_id,
        "scan_frequency_minutes": freq,
        "last_scanned_at": last.strftime("%Y-%m-%d %H:%M:%S") if last else None,
        "next_scan_at": next_scan.strftime("%Y-%m-%d %H:%M:%S"),
        "due_now": due,
    }


def run_adaptation_cycle(db_path: str = db.DB_PATH) -> list[dict[str, Any]]:
    """For each active regulator: check if due for scan, and adjust scan
    frequency if enough change data exists. Returns a per-regulator summary.
    """
    results: list[dict[str, Any]] = []
    regs = db.get_regulators(db_path=db_path)
    for r in regs:
        if not r.get("active"):
            continue
        rid = r["id"]
        schedule = get_scan_schedule(rid, db_path=db_path)

        # count available change history to decide whether to adapt
        with db.get_conn(db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM changes WHERE regulator_id = ?",
                (rid,),
            ).fetchone()["c"]

        new_freq = None
        if count >= 2:  # need at least 2 changes to compute a rate
            new_freq = adjust_frequency(rid, db_path=db_path)

        anomaly = detect_anomaly(rid, db_path=db_path)

        results.append({
            "regulator_id": rid,
            "name": r["name"],
            "due_now": schedule["due_now"],
            "current_freq": schedule["scan_frequency_minutes"],
            "new_freq": new_freq,
            "change_count": count,
            "anomaly": anomaly,
        })
    return results


# --- self-check ------------------------------------------------------------


def _self_check() -> None:
    """Build a temp DB, seed regulators + changes across time, exercise all
    five public functions, assert sensible outputs. stdlib only."""
    import os
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    test_db = tmp.name

    try:
        db.init_db(test_db)

        # two regulators: one active with history, one active without
        rid_a = db.insert_regulator(
            name="FCA", url="https://fca.org.uk/news",
            scan_frequency_minutes=360, db_path=test_db,
        )
        rid_b = db.insert_regulator(
            name="ESMA", url="https://esma.europa.eu",
            scan_frequency_minutes=360, db_path=test_db,
        )

        # seed 10 changes for FCA spread across 14 days (UTC)
        # insert_change has no detected_at param, so insert raw to set timestamps
        base = datetime.utcnow() - timedelta(days=14)
        with db.get_conn(test_db) as conn:
            for i in range(10):
                when = base + timedelta(days=int(i * 1.4))
                conn.execute(
                    "INSERT INTO changes (regulator_id, title, detected_at) VALUES (?,?,?)",
                    (rid_a, f"change-{i}", when.strftime("%Y-%m-%d %H:%M:%S")),
                )

        # 1) compute_scan_frequency — should be between 30 and 1440, not default 360
        freq = compute_scan_frequency(rid_a, db_path=test_db)
        assert 30 <= freq <= 1440, f"freq out of bounds: {freq}"
        # 10 changes / 14 days ~ 5 pubs/week; avg gap ~1.4d=2016min; half ~1008
        # so expected around 1008; allow wide tolerance for the heuristic
        assert freq != 360, "should not return default when data exists"

        # regulator with no changes -> default 360
        freq_b = compute_scan_frequency(rid_b, db_path=test_db)
        assert freq_b == 360, f"no-data default expected, got {freq_b}"

        # 2) adjust_frequency — persists to DB
        new_freq = adjust_frequency(rid_a, db_path=test_db)
        assert new_freq == freq, "adjust_frequency should persist computed freq"
        with db.get_conn(test_db) as conn:
            stored = conn.execute(
                "SELECT scan_frequency_minutes FROM regulators WHERE id = ?",
                (rid_a,),
            ).fetchone()["scan_frequency_minutes"]
        assert stored == new_freq, f"DB not updated: {stored} != {new_freq}"

        # 3) detect_anomaly — no anomaly on evenly spread history
        anom = detect_anomaly(rid_a, window_hours=24, db_path=test_db)
        # evenly spread => recent ~1 change in 24h, historical avg ~0.7 => not >2x
        assert anom is None or anom["recent_count"] <= anom["threshold"], \
            "false anomaly on even data"

        # seed a burst: 5 changes in the last 12 hours -> should trip anomaly
        now = datetime.utcnow()
        with db.get_conn(test_db) as conn:
            for i in range(5):
                conn.execute(
                    "INSERT INTO changes (regulator_id, title, detected_at) VALUES (?,?,?)",
                    (rid_a, f"burst-{i}",
                     (now - timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S")),
                )
        anom2 = detect_anomaly(rid_a, window_hours=24, db_path=test_db)
        assert anom2 is not None, "burst should trigger anomaly"
        assert anom2["recent_count"] > anom2["threshold"]
        assert anom2["regulator_id"] == rid_a

        # regulator with no history -> no anomaly (None)
        assert detect_anomaly(rid_b, db_path=test_db) is None

        # 4) get_scan_schedule — never scanned regulator is due now
        sched = get_scan_schedule(rid_a, db_path=test_db)
        assert sched["regulator_id"] == rid_a
        assert "next_scan_at" in sched and "due_now" in sched

        sched_b = get_scan_schedule(rid_b, db_path=test_db)
        assert sched_b["due_now"] is True, "never-scanned regulator should be due"
        assert sched_b["last_scanned_at"] is None

        # set last_scanned_at in the past -> not due
        past = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        with db.get_conn(test_db) as conn:
            conn.execute(
                "UPDATE regulators SET last_scanned_at = ? WHERE id = ?",
                (past, rid_b),
            )
        sched_b2 = get_scan_schedule(rid_b, db_path=test_db)
        # default 360 min freq, scanned 1h ago => not due for ~5h
        assert sched_b2["due_now"] is False, "should not be due 1h into a 6h interval"
        assert sched_b2["last_scanned_at"] == past

        # 5) run_adaptation_cycle — iterates active regulators
        cycle = run_adaptation_cycle(db_path=test_db)
        assert len(cycle) == 2, f"expected 2 active regulators, got {len(cycle)}"
        by_id = {c["regulator_id"]: c for c in cycle}
        assert by_id[rid_a]["new_freq"] is not None, "FCA has data -> should adapt"
        assert by_id[rid_b]["new_freq"] is None, "ESMA has no changes -> no adapt"
        assert by_id[rid_a]["change_count"] == 15  # 10 + 5 burst
        assert by_id[rid_b]["change_count"] == 0

        # inactive regulator is skipped
        db.insert_regulator(name="INACTIVE", url="https://example.com", active=0, db_path=test_db)
        cycle2 = run_adaptation_cycle(db_path=test_db)
        assert len(cycle2) == 2, "inactive regulator should be skipped"

        # get_scan_schedule on missing regulator raises
        try:
            get_scan_schedule(99999, db_path=test_db)
            raise AssertionError("expected ValueError for missing regulator")
        except ValueError:
            pass

        print("SELF-CHECK PASSED: adapter functions work end-to-end")
    finally:
        if os.path.exists(test_db):
            os.remove(test_db)


if __name__ == "__main__":
    _self_check()
