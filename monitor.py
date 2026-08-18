"""
GeckoRegen — Monitor (orchestrator).

Wires all modules into the full self-healing pipeline:
  run_scraper → check_health → monitor_regulator → monitor_all

Stdlib only.  Imports: bd_client, db, detector, healer, memory.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import bd_client
import db
import detector
import healer
import memory

_BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_BASE, "scrapers_config.json")

# Default expected schema — field → spec dict (see detector.detect_failures).
# A regulator entry in scrapers_config.json may override "expected_schema".
DEFAULT_SCHEMA: dict[str, dict[str, Any]] = {
    "title":        {"type": str, "required": True,  "regex": r"\S+"},
    "article_url":  {"type": str, "required": True,  "regex": r"https?://"},
    "publish_date": {"type": str, "required": True,  "regex": r"\d{4}-\d{2}-\d{2}"},
    "summary":      {"type": str, "required": False, "regex": r"\S+"},
}

# Per-regulator schemas from scrapers_config.json field lists.
# Test Shop price is optional — BD notes it as often MISSING (self-heal case).
SCHEMA_BY_NAME: dict[str, dict[str, dict[str, Any]]] = {
    "FCA": DEFAULT_SCHEMA,
    "FINMA": {
        "title":            {"type": str, "required": True,  "regex": r"\S+"},
        "date":             {"type": str, "required": True,  "regex": r"\S+"},
        "category":         {"type": str, "required": False},
        "summary":          {"type": str, "required": False, "regex": r"\S+"},
        "product_page_url": {"type": str, "required": True,  "regex": r"https?://"},
    },
    "BD Test Shop": {
        "product_name":     {"type": str, "required": True,  "regex": r"\S+"},
        "price":            {"type": str, "required": False},
        "image_url":        {"type": str, "required": False, "regex": r"https?://"},
        "availability":     {"type": str, "required": False},
        "product_page_url": {"type": str, "required": True,  "regex": r"https?://"},
    },
}


def _schema_for(regulator: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Prefer an explicit schema on the regulator, else name-based, else default."""
    if regulator.get("expected_schema"):
        return regulator["expected_schema"]
    return SCHEMA_BY_NAME.get(regulator.get("name", ""), DEFAULT_SCHEMA)


# ── 1. run_scraper ────────────────────────────────────────────────────

def run_scraper(regulator: dict[str, Any]) -> list[dict[str, Any]]:
    """Trigger BD scraper for *regulator* and download the dataset.

    *regulator* must have keys: collector_id, url.
    Returns the list of records from BD.
    """
    collector_id = regulator["collector_id"]
    url = regulator["url"]
    snapshot_id = bd_client.trigger_scraper(collector_id, [url])
    records = bd_client.get_dataset(snapshot_id)
    return records if records else []


# ── 2. check_health ───────────────────────────────────────────────────

def check_health(records: list[dict[str, Any]], expected_schema: dict[str, Any]) -> dict[str, Any]:
    """Run detector.detect_failures on *records* and return a health status dict.

    If *records* is empty, returns a 'broken' status immediately.
    For multi-record datasets, checks the first record against the schema
    and computes a population rate across all records.
    """
    if not records:
        return {
            "status": "broken",
            "healthy": False,
            "field_population_rate": 0.0,
            "record_count": 0,
            "missing_fields": list(expected_schema.keys()),
            "error_details": "no records returned from scraper",
            "severity": "critical",
            "broken_fields": [],
        }

    # Detect on the first record (representative row)
    report = detector.detect_failures(records[0], expected_schema)
    broken_fields = report["broken_fields"]

    # Population rate across all records
    total_fields = len(expected_schema) * len(records)
    populated = 0
    for rec in records:
        for field in expected_schema:
            v = rec.get(field)
            if v is not None and v != "":
                populated += 1
    pop_rate = round(populated / total_fields, 4) if total_fields else 0.0

    if report["healthy"]:
        status = "healthy"
    elif report["severity"] == "critical":
        status = "broken"
    else:
        status = "degraded"

    return {
        "status": status,
        "healthy": report["healthy"],
        "field_population_rate": pop_rate,
        "record_count": len(records),
        "missing_fields": [b["field"] for b in broken_fields],
        "error_details": json.dumps(broken_fields, default=str) if broken_fields else None,
        "severity": report["severity"],
        "broken_fields": broken_fields,
    }


def _as_text(value: Any) -> str | None:
    """Coerce scraper values to a SQLite-safe string (price may be a dict)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for k in ("amount", "value", "text", "url", "name", "price"):
            if value.get(k) not in (None, ""):
                return _as_text(value[k])
        return json.dumps(value)
    if isinstance(value, list):
        parts = [_as_text(x) for x in value]
        return ", ".join(p for p in parts if p)
    return str(value)


# ── 3. monitor_regulator ──────────────────────────────────────────────

def monitor_regulator(
    regulator: dict[str, Any],
    on_stage: Any = None,
) -> dict[str, Any]:
    """Full pipeline for a single regulator:
    run scraper → save snapshot → detect failures → heal if broken → record.

    *regulator* keys: id, name, url, collector_id, (optional) expected_schema.
    *on_stage* optional callback(stage: str) for live dashboard (monitor/detect/heal/validate/memory).
    Returns a result dict summarising the run.
    """
    def _stage(name: str) -> None:
        if on_stage:
            on_stage(name)

    reg_id = regulator["id"]
    schema = _schema_for(regulator)

    # 1. Run scraper
    _stage("monitor")
    try:
        records = run_scraper(regulator)
    except Exception as e:
        # Record broken status and return
        db.update_health(reg_id, "broken", error_details=str(e),
                         record_count=0, missing_fields=list(schema.keys()))
        return {"regulator_id": reg_id, "status": "broken", "error": str(e),
                "healed": False}

    # 2. Save snapshot
    _stage("detect")
    snapshot_path = detector.save_snapshot(reg_id, records) if records else None

    # 3. Check health
    health = check_health(records, schema)
    db.update_health(
        reg_id, health["status"],
        field_population_rate=health["field_population_rate"],
        record_count=health["record_count"],
        missing_fields=health["missing_fields"],
        error_details=health["error_details"],
    )

    # 4. Record changes (each record as a potential change entry)
    for rec in records:
        memory.record_change(reg_id, {
            "title": _as_text(rec.get("title") or rec.get("product_name")),
            "category": _as_text(rec.get("category") or rec.get("availability")),
            "publish_date": _as_text(rec.get("publish_date") or rec.get("date")),
            "summary": _as_text(rec.get("summary") or rec.get("price")),
            "article_url": _as_text(rec.get("article_url") or rec.get("product_page_url")),
            "severity": health["severity"],
            "snapshot_id": snapshot_path,
        })

    # 5. If failures detected → heal (only if we have broken fields to fix)
    healed = False
    if not health["healthy"] and health["broken_fields"]:
        # Get last-known-good from snapshot history
        last_good = detector.load_last_snapshot(reg_id)
        if last_good and isinstance(last_good, list) and last_good:
            last_good = last_good[0]
        if not last_good:
            last_good = records[0] if records else {}

        broken_field_names = [b["field"] for b in health["broken_fields"]]
        # healer.validate_heal expects {field: "str"|"int"|...}, not detector specs
        heal_schema = {
            field: ("str" if spec.get("type") is str else
                    "int" if spec.get("type") is int else
                    "float" if spec.get("type") is float else
                    "bool" if spec.get("type") is bool else None)
            for field, spec in schema.items()
        }
        _stage("heal")
        heal_result = healer.heal_pipeline(
            regulator["collector_id"],
            regulator["name"],
            broken_field_names,
            last_good,
            regulator["url"],
            heal_schema,
            on_stage=on_stage,
        )

        # Record the heal event
        memory.record_heal(reg_id, {
            "broken_fields": broken_field_names,
            "heal_prompt": healer.generate_heal_prompt(
                regulator["name"], broken_field_names, last_good),
            "status": "done" if heal_result.get("success") else "failed",
            "validation_passed": heal_result.get("success", False),
            "attempts": heal_result.get("attempts", 1),
            "validation_details": heal_result.get("validation"),
        })

        # Update health after heal
        if heal_result.get("success"):
            db.update_health(reg_id, "healthy",
                             field_population_rate=health["field_population_rate"],
                             record_count=health["record_count"])
            health["status"] = "healthy"
            healed = True
        else:
            db.update_health(reg_id, "broken",
                             error_details="heal failed — manual intervention needed",
                             missing_fields=broken_field_names)
            health["status"] = "broken"

    # 6. Update last_scanned_at
    _stage("memory")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE regulators SET last_scanned_at = datetime('now') WHERE id = ?",
            (reg_id,),
        )

    return {
        "regulator_id": reg_id,
        "status": health["status"],
        "record_count": health["record_count"],
        "field_population_rate": health["field_population_rate"],
        "healed": healed,
        "snapshot_path": snapshot_path,
    }


# ── 4. monitor_all ────────────────────────────────────────────────────

def monitor_all(max_workers: int = 3) -> list[dict[str, Any]]:
    """Run monitor_regulator on all active regulators concurrently.

    Uses ThreadPoolExecutor(max_workers=3) per DESIGN.md §8 to respect
    BD API rate limits.
    """
    regulators = db.get_regulators()
    active = [r for r in regulators if r.get("active")]

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(monitor_regulator, reg): reg for reg in active}
        for fut in as_completed(futures):
            reg = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({
                    "regulator_id": reg["id"],
                    "status": "broken",
                    "error": str(e),
                    "healed": False,
                })
    return results


# ── 5. init_from_config ───────────────────────────────────────────────

def init_from_config(config_path: str = CONFIG_PATH) -> list[int]:
    """Read scrapers_config.json and insert regulators into DB if they don't
    already exist (matched by name).

    Config format: a list of objects with keys:
        name, full_name, jurisdiction, url, collector_id,
        active, scan_frequency_minutes, (optional) expected_schema

    Returns the list of regulator DB IDs (existing + newly inserted).
    """
    with open(config_path, "r", encoding="utf-8") as fh:
        config = json.load(fh)

    # Config may be a list of scrapers, or an object with a "scrapers" key
    # (plus blocked_domains / notes). Dict-keyed-by-collector-id is also accepted.
    if isinstance(config, dict):
        raw = config.get("scrapers", config)
    else:
        raw = config

    if isinstance(raw, dict):
        entries = []
        for collector_id, meta in raw.items():
            if not isinstance(meta, dict):
                continue
            entry = dict(meta)
            entry.setdefault("collector_id", collector_id)
            entries.append(entry)
    else:
        entries = list(raw)

    db.init_db()
    existing = {r["name"]: r for r in db.get_regulators()}
    ids: list[int] = []

    for entry in entries:
        name = entry["name"]
        if name in existing:
            rid = existing[name]["id"]
            with db.get_conn() as conn:
                conn.execute(
                    """UPDATE regulators SET url = ?, full_name = ?,
                       jurisdiction = ?, collector_id = ?, active = ?
                       WHERE id = ?""",
                    (entry["url"], entry.get("full_name"),
                     entry.get("jurisdiction"), entry.get("collector_id"),
                     entry.get("active", 1), rid),
                )
            ids.append(rid)
            continue

        rid = db.insert_regulator(
            name=name,
            url=entry["url"],
            full_name=entry.get("full_name"),
            jurisdiction=entry.get("jurisdiction"),
            collector_id=entry.get("collector_id"),
            active=entry.get("active", 1),
            scan_frequency_minutes=entry.get("scan_frequency_minutes", 360),
        )
        ids.append(rid)

    return ids


# ── __main__ self-check ───────────────────────────────────────────────

def _self_check() -> None:
    """Inits DB, creates a test regulator config, runs monitor_regulator
    on it (mocking the BD API call), verifies health status is recorded.
    """
    import tempfile
    import shutil

    tmpdir = tempfile.mkdtemp()
    test_db = os.path.join(tmpdir, "test_gecko.db")
    test_snapshot_dir = os.path.join(tmpdir, "snapshots")

    # Patch db module to use the temp DB
    orig_db_path = db.DB_PATH
    db.DB_PATH = test_db

    # Patch detector snapshot dir
    orig_snapshot_dir = detector._SNAPSHOT_DIR
    detector._SNAPSHOT_DIR = test_snapshot_dir

    # Patch get_conn on both db and memory (memory imported the name, not the module).
    import memory as mem_mod
    orig_db_get_conn = db.get_conn
    orig_mem_get_conn = mem_mod.get_conn

    import sqlite3
    from contextlib import contextmanager

    @contextmanager
    def _patched_get_conn(db_path_arg=None):
        # ponytail: ignore db_path_arg — default args in db.py were bound
        # at import time to the original DB_PATH, so always use test_db.
        os.makedirs(os.path.dirname(test_db), exist_ok=True)
        conn = sqlite3.connect(test_db)
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

    db.get_conn = _patched_get_conn
    mem_mod.get_conn = _patched_get_conn

    # Mock BD API calls
    orig_trigger = bd_client.trigger_scraper
    orig_get_dataset = bd_client.get_dataset

    mock_records = [
        {"title": "FCA fines XYZ Bank £2M", "article_url": "https://fca.org.uk/xyz",
         "publish_date": "2025-08-18", "summary": "The FCA has imposed a penalty."},
        {"title": "New crypto guidance", "article_url": "https://fca.org.uk/crypto",
         "publish_date": "2025-08-17", "summary": "New guidance on crypto assets."},
    ]

    bd_client.trigger_scraper = lambda *a, **k: "mock_snapshot_123"
    bd_client.get_dataset = lambda *a, **k: mock_records

    try:
        # 1. Init DB
        db.init_db(test_db)
        assert os.path.exists(test_db), "DB file not created"

        # 2. Create test regulator config
        rid = db.insert_regulator(
            name="TESTREG",
            url="https://test.example.com/news",
            full_name="Test Regulator",
            jurisdiction="ZZ",
            collector_id="c_test_001",
            db_path=test_db,
        )
        assert rid > 0, "insert_regulator failed"

        # 3. Run monitor_regulator
        regulator = {
            "id": rid,
            "name": "TESTREG",
            "url": "https://test.example.com/news",
            "collector_id": "c_test_001",
        }
        result = monitor_regulator(regulator)

        # 4. Verify health status is recorded
        assert result["status"] == "healthy", f"Expected healthy, got {result['status']}"
        assert result["record_count"] == 2, f"Expected 2 records, got {result['record_count']}"

        health_rows = db.get_health(rid, db_path=test_db)
        assert len(health_rows) >= 1, "No health rows recorded"
        assert health_rows[0]["status"] == "healthy", \
            f"Health status not recorded as healthy: {health_rows[0]['status']}"

        # 5. Verify changes were recorded via memory
        changes = db.get_changes(50, db_path=test_db)
        assert len(changes) == 2, f"Expected 2 changes, got {len(changes)}"

        # 6. Test failure path: mock broken scraper
        bd_client.get_dataset = lambda *a, **k: [
            {"title": None, "article_url": "https://test.example.com/1",
             "publish_date": "2025-08-18", "summary": ""},
        ]
        # Mock healer to avoid network calls
        orig_heal_pipeline = healer.heal_pipeline
        healer.heal_pipeline = lambda *a, **k: {
            "success": True, "attempts": 1,
            "validation": {"valid": True, "record_count": 1, "avg_population_rate": 1.0},
        }

        result2 = monitor_regulator(regulator)
        assert result2["healed"] is True, "Heal should have been triggered"
        assert result2["status"] == "healthy", "Post-heal status should be healthy"

        health_rows2 = db.get_health(rid, db_path=test_db)
        assert len(health_rows2) >= 3, "Heal events should add health rows"
        assert any(h["status"] == "broken" for h in health_rows2), \
            "Should have recorded broken status before heal"

        # 7. Test empty dataset (heal_pipeline still mocked)
        bd_client.get_dataset = lambda *a, **k: []
        result3 = monitor_regulator(regulator)
        assert result3["status"] == "broken", "Empty dataset should be broken"

        healer.heal_pipeline = orig_heal_pipeline

        print("monitor.py self-check: ALL PASSED")

    finally:
        # Restore everything
        db.DB_PATH = orig_db_path
        db.get_conn = orig_db_get_conn
        detector._SNAPSHOT_DIR = orig_snapshot_dir
        mem_mod.get_conn = orig_mem_get_conn
        bd_client.trigger_scraper = orig_trigger
        bd_client.get_dataset = orig_get_dataset
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    _self_check()
