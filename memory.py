"""
GeckoRegen — Memory layer.

Persistent organisational memory + regulator behaviour profiles + impact
correlation.  Learns over time by recording every detected change and every
healing event, then distilling those into a per-regulator profile.

Stdlib only.  Imports the DB helper from db.py.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

from db import DB_PATH, get_conn

# ── helpers ──────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _week_of(dt_str: str) -> Optional[str]:
    """Return ISO week key 'YYYY-Www' or None if unparseable."""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "")[:19])
    except (ValueError, TypeError):
        return None
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _season(month: int) -> str:
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "autumn"


def _text_similarity(a: str, b: str) -> float:
    """0..1 text similarity via SequenceMatcher on lowercased strings."""
    a = (a or "").lower().strip()
    b = (b or "").lower().strip()
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# ── 1. record_change ─────────────────────────────────────────────────


def record_change(reg_id: int, change_data: Dict[str, Any]) -> int:
    """
    Store a detected change with full context.

    change_data keys (all optional except title):
        title, category, publish_date, summary, article_url,
        severity, is_new, snapshot_id

    Returns the new change.id.
    """
    title = change_data.get("title")
    category = change_data.get("category")
    publish_date = change_data.get("publish_date")
    summary = change_data.get("summary")
    article_url = change_data.get("article_url")
    if isinstance(title, (dict, list)):
        title = json.dumps(title)
    if isinstance(category, (dict, list)):
        category = json.dumps(category)
    if isinstance(publish_date, (dict, list)):
        publish_date = json.dumps(publish_date)
    if isinstance(summary, (dict, list)):
        summary = json.dumps(summary)
    if isinstance(article_url, (dict, list)):
        article_url = json.dumps(article_url)

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO changes
               (regulator_id, detected_at, title, category, publish_date,
                summary, article_url, severity, is_new, snapshot_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                reg_id,
                _now(),
                title,
                category,
                publish_date,
                summary,
                article_url,
                change_data.get("severity", "info"),
                1 if change_data.get("is_new", True) else 0,
                change_data.get("snapshot_id"),
            ),
        )
        change_id = cur.lastrowid
    # keep the profile fresh
    try:
        update_regulator_profile(reg_id)
    except Exception:
        pass
    return change_id


# ── 2. record_heal ───────────────────────────────────────────────────


def record_heal(reg_id: int, heal_data: Dict[str, Any]) -> int:
    """
    Store a healing event.

    heal_data keys (all optional):
        broken_fields (list|str), heal_prompt, bd_job_id, status,
        attempts, validation_passed (bool), validation_details (dict|str),
        completed_at, duration_seconds

    Returns the new healing_events.id.
    """
    broken_fields = heal_data.get("broken_fields")
    if isinstance(broken_fields, (list, tuple)):
        broken_fields = json.dumps(broken_fields)

    val_details = heal_data.get("validation_details")
    if isinstance(val_details, dict):
        val_details = json.dumps(val_details)

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO healing_events
               (regulator_id, triggered_at, broken_fields, heal_prompt,
                bd_job_id, status, attempts, validation_passed,
                validation_details, completed_at, duration_seconds)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                reg_id,
                _now(),
                broken_fields,
                heal_data.get("heal_prompt"),
                heal_data.get("bd_job_id"),
                heal_data.get("status", "triggered"),
                heal_data.get("attempts", 1),
                1 if heal_data.get("validation_passed") else 0,
                val_details,
                heal_data.get("completed_at"),
                heal_data.get("duration_seconds"),
            ),
        )
        heal_id = cur.lastrowid
    try:
        update_regulator_profile(reg_id)
    except Exception:
        pass
    return heal_id


# ── 3. get_regulator_profile ─────────────────────────────────────────


def get_regulator_profile(reg_id: int) -> Optional[Dict[str, Any]]:
    """
    Return the stored behaviour profile for a regulator, or None.

    Keys: avg_publications_per_week, common_change_types (list),
    seasonal_patterns (dict), last_profiled_at, total_changes_detected,
    total_heals, successful_heals, success_rate (float 0..1).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM regulator_profiles WHERE regulator_id = ?",
            (reg_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["common_change_types"] = json.loads(d.get("common_change_types") or "[]")
    d["seasonal_patterns"] = json.loads(d.get("seasonal_patterns") or "{}")
    total = d.get("total_heals", 0) or 0
    success = d.get("successful_heals", 0) or 0
    d["success_rate"] = round(success / total, 3) if total else 0.0
    return d


# ── 4. update_regulator_profile ──────────────────────────────────────


def update_regulator_profile(reg_id: int) -> Dict[str, Any]:
    """
    Recompute the regulator profile from historical data in the DB and
    upsert into regulator_profiles.  Returns the new profile dict.
    """
    with get_conn() as conn:
        changes = conn.execute(
            "SELECT detected_at, category, publish_date FROM changes "
            "WHERE regulator_id = ? ORDER BY detected_at",
            (reg_id,),
        ).fetchall()

        heals = conn.execute(
            "SELECT validation_passed FROM healing_events "
            "WHERE regulator_id = ?",
            (reg_id,),
        ).fetchall()

        existing = conn.execute(
            "SELECT id FROM regulator_profiles WHERE regulator_id = ?",
            (reg_id,),
        ).fetchone()

    # ── publications per week ───────────────────────────────────────
    weeks: Counter = Counter()
    seasonal: Counter = Counter()
    change_types: Counter = Counter()
    for ch in changes:
        key = _week_of(ch["detected_at"] or ch["publish_date"] or "")
        if key:
            weeks[key] += 1
        dt_str = ch["publish_date"] or ch["detected_at"] or ""
        try:
            m = datetime.fromisoformat(dt_str[:19]).month
            seasonal[_season(m)] += 1
        except (ValueError, TypeError):
            pass
        if ch["category"]:
            change_types[ch["category"]] += 1

    avg_per_week = round(sum(weeks.values()) / len(weeks), 2) if weeks else 0.0

    # ── seasonal patterns: label each season by activity ───────────
    # ponytail: simple high/medium/low bucketing; good enough for
    # dozens of data points, revisit if we need statistical rigour.
    season_patterns: Dict[str, str] = {}
    if seasonal:
        counts = list(seasonal.values())
        hi = max(counts)
        lo = min(counts)
        span = hi - lo
        for season, cnt in seasonal.items():
            if span == 0:
                season_patterns[season] = "medium"
            elif cnt >= lo + span * 0.66:
                season_patterns[season] = "high"
            elif cnt >= lo + span * 0.33:
                season_patterns[season] = "medium"
            else:
                season_patterns[season] = "low"

    common_types = [c for c, _ in change_types.most_common(5)]

    total_changes = len(changes)
    total_heals = len(heals)
    successful_heals = sum(1 for h in heals if h["validation_passed"])

    with get_conn() as conn:
        if existing:
            conn.execute(
                """UPDATE regulator_profiles SET
                   avg_publications_per_week = ?,
                   common_change_types = ?,
                   seasonal_patterns = ?,
                   last_profiled_at = ?,
                   total_changes_detected = ?,
                   total_heals = ?,
                   successful_heals = ?
                   WHERE regulator_id = ?""",
                (
                    avg_per_week,
                    json.dumps(common_types),
                    json.dumps(season_patterns),
                    _now(),
                    total_changes,
                    total_heals,
                    successful_heals,
                    reg_id,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO regulator_profiles
                   (regulator_id, avg_publications_per_week,
                    common_change_types, seasonal_patterns,
                    last_profiled_at, total_changes_detected,
                    total_heals, successful_heals)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    reg_id,
                    avg_per_week,
                    json.dumps(common_types),
                    json.dumps(season_patterns),
                    _now(),
                    total_changes,
                    total_heals,
                    successful_heals,
                ),
            )

    return {
        "regulator_id": reg_id,
        "avg_publications_per_week": avg_per_week,
        "common_change_types": common_types,
        "seasonal_patterns": season_patterns,
        "last_profiled_at": _now(),
        "total_changes_detected": total_changes,
        "total_heals": total_heals,
        "successful_heals": successful_heals,
        "success_rate": round(successful_heals / total_heals, 3) if total_heals else 0.0,
    }


# ── 5. find_similar_changes ──────────────────────────────────────────


def find_similar_changes(
    new_change: Dict[str, Any], limit: int = 5
) -> List[Dict[str, Any]]:
    """
    Search historical changes by title/category similarity using simple
    text matching (SequenceMatcher, not embeddings).

    new_change keys used: title, category, summary.

    Returns a list of dicts sorted by similarity descending:
        {id, regulator_id, title, category, publish_date, detected_at,
         similarity, note}
    """
    new_title = (new_change.get("title") or "").lower()
    new_cat = (new_change.get("category") or "").lower()
    new_summary = (new_change.get("summary") or "").lower()

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, regulator_id, title, category, summary, "
            "publish_date, detected_at "
            "FROM changes ORDER BY detected_at DESC"
        ).fetchall()

    scored: List[Dict[str, Any]] = []
    for r in rows:
        title_sim = _text_similarity(new_title, r["title"] or "")
        cat_sim = _text_similarity(new_cat, r["category"] or "")
        sum_sim = _text_similarity(new_summary, r["summary"] or "")
        # ponytail: weighted average — title matters most, category is a
        # strong signal, summary is soft context.
        score = round(title_sim * 0.5 + cat_sim * 0.3 + sum_sim * 0.2, 3)
        if score <= 0:
            continue
        if title_sim > 0.8:
            note = "near-duplicate title"
        elif cat_sim > 0.8 and title_sim > 0.5:
            note = "same category, similar title"
        elif title_sim > 0.5:
            note = "similar title"
        elif cat_sim > 0.8:
            note = "same category"
        else:
            note = "weak match"
        scored.append(
            {
                "id": r["id"],
                "regulator_id": r["regulator_id"],
                "title": r["title"],
                "category": r["category"],
                "publish_date": r["publish_date"],
                "detected_at": r["detected_at"],
                "similarity": score,
                "note": note,
            }
        )

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:limit]


# ── 6. get_regulatory_timeline ───────────────────────────────────────


def get_regulatory_timeline(
    reg_id: int, limit: int = 50
) -> List[Dict[str, Any]]:
    """
    Chronological list (oldest first) of all changes for a regulator.
    Each entry: {id, detected_at, title, category, severity, publish_date,
                 article_url, is_new}
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, detected_at, title, category, severity, "
            "publish_date, article_url, is_new "
            "FROM changes WHERE regulator_id = ? "
            "ORDER BY detected_at ASC LIMIT ?",
            (reg_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── self-check ───────────────────────────────────────────────────────


def _self_check() -> None:
    """
    In-memory self-check: create a temp DB, init the schema via db.py,
    exercise every public function, assert sensible results.
    """
    import tempfile
    import os
    import sqlite3
    from contextlib import contextmanager
    import db as dbmod

    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")

    # get_conn's default arg was bound at import time, so we monkeypatch
    # both db module's attribute and our own imported reference.
    orig_get_conn = dbmod.get_conn
    orig_path = dbmod.DB_PATH
    dbmod.DB_PATH = db_path

    @contextmanager
    def _patched_conn(db_path_arg=None):
        c = sqlite3.connect(db_path_arg or db_path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    dbmod.get_conn = _patched_conn
    # memory.py imported get_conn by name — patch it in our own namespace.
    # When run as __main__, we are already in the module's global scope.
    import sys
    _self_mod = sys.modules[__name__]
    _self_mod.get_conn = _patched_conn

    dbmod.init_db(db_path)
    dbmod.insert_regulator("FCA", "https://example.com", db_path=db_path)
    dbmod.insert_regulator("ESMA", "https://example.com/esma", db_path=db_path)

    # record_change
    cid1 = record_change(1, {
        "title": "New guidance on crypto assets",
        "category": "guidance",
        "severity": "warning",
        "publish_date": "2025-06-15 10:00:00",
        "summary": "FCA publishes new guidance on crypto asset regulation.",
    })
    cid2 = record_change(1, {
        "title": "Enforcement action against firm X",
        "category": "enforcement",
        "severity": "critical",
        "publish_date": "2025-07-20 12:00:00",
        "summary": "FCA takes enforcement action against firm X.",
    })
    assert cid1 and cid2, "record_change returned no id"

    # record_heal
    hid1 = record_heal(1, {
        "broken_fields": ["title", "summary"],
        "heal_prompt": "Field title returns null...",
        "status": "done",
        "validation_passed": True,
        "duration_seconds": 42,
    })
    hid2 = record_heal(1, {
        "broken_fields": ["category"],
        "status": "failed",
        "validation_passed": False,
    })
    assert hid1 and hid2, "record_heal returned no id"

    # get_regulator_profile
    prof = get_regulator_profile(1)
    assert prof is not None, "profile missing after update"
    assert prof["total_changes_detected"] == 2, prof
    assert prof["total_heals"] == 2, prof
    assert prof["successful_heals"] == 1, prof
    assert 0 < prof["success_rate"] <= 0.5, prof
    assert isinstance(prof["common_change_types"], list), prof
    assert isinstance(prof["seasonal_patterns"], dict), prof

    # find_similar_changes
    similar = find_similar_changes({
        "title": "New guidance on crypto assets and stablecoins",
        "category": "guidance",
        "summary": "FCA publishes new guidance.",
    })
    assert len(similar) > 0, "no similar changes found"
    assert similar[0]["similarity"] > 0.5, similar[0]
    assert "note" in similar[0], similar[0]
    assert similar[0]["id"] == cid1, (similar[0], cid1)

    # get_regulatory_timeline
    tl = get_regulatory_timeline(1)
    assert len(tl) == 2, tl
    assert tl[0]["detected_at"] <= tl[1]["detected_at"], "timeline not sorted"

    # update_regulator_profile (idempotent)
    prof2 = update_regulator_profile(1)
    assert prof2["total_changes_detected"] == 2, prof2

    # profile for regulator with no data — should be None
    assert get_regulator_profile(2) is None

    # Restore
    dbmod.DB_PATH = orig_path
    dbmod.get_conn = orig_get_conn
    _self_mod.get_conn = orig_get_conn
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    print("memory.py self-check OK")


if __name__ == "__main__":
    _self_check()
