"""GeckoRegen Detector — snapshot diff + failure detection + severity classification.

Stdlib only (json, os). The Detector sits between Monitor and Healer:
it identifies *what* broke so the Healer can generate a targeted fix prompt.
"""

import json
import os

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_BASE = os.path.dirname(os.path.abspath(__file__))
_SNAPSHOT_DIR = os.path.join(
    os.environ.get("DATA_DIR")
    or ("/tmp/geckoregen-data" if os.environ.get("VERCEL") else os.path.join(_BASE, "data")),
    "snapshots",
)

# Fields whose breakage is always 'critical' regardless of other damage.
CORE_FIELDS = frozenset({"title", "article_url", "publish_date", "summary"})


# ---------------------------------------------------------------------------
# 1. Snapshot persistence
# ---------------------------------------------------------------------------

def save_snapshot(reg_id, data):
    """Append *data* as one JSONL line to data/snapshots/<reg_id>.jsonl.

    Creates the directory on first call.  Returns the path written to.
    """
    os.makedirs(_SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(_SNAPSHOT_DIR, f"{reg_id}.jsonl")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
    return path


def load_last_snapshot(reg_id):
    """Return the last JSONL record for *reg_id*, or ``None`` if no history."""
    path = os.path.join(_SNAPSHOT_DIR, f"{reg_id}.jsonl")
    if not os.path.exists(path):
        return None
    last = None
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                last = json.loads(line)
    return last


# ---------------------------------------------------------------------------
# 2. Field-level diff
# ---------------------------------------------------------------------------

def diff_snapshots(current_data, last_known_good):
    """Compare *current_data* against *last_known_good* field-by-field.

    Returns a list of dicts, one per broken field::

        {"field", "current", "last_good", "issue"}

    Only fields present in *last_known_good* are checked — new fields are
    additions, not breakage.
    """
    broken = []
    if not last_known_good:
        return broken
    for field, good_val in last_known_good.items():
        if field not in current_data:
            broken.append({
                "field": field,
                "current": None,
                "last_good": good_val,
                "issue": "missing",
            })
        elif current_data[field] != good_val and _is_breakage(current_data[field], good_val):
            broken.append({
                "field": field,
                "current": current_data[field],
                "last_good": good_val,
                "issue": "changed",
            })
    return broken


def _is_breakage(current_val, good_val):
    """Heuristic: a change is *breakage* (not a content update) when the
    current value is null/empty or its type differs from the good value.

    ponytail: naive type+null check; upgrade to per-field comparators if
    false positives appear on content-heavy fields like ``summary``.
    """
    if current_val is None:
        return True
    if isinstance(current_val, str) and current_val.strip() == "":
        return True
    if type(current_val) is not type(good_val):
        return True
    return False


# ---------------------------------------------------------------------------
# 3. Severity classification
# ---------------------------------------------------------------------------

def classify_severity(broken_fields, record_count_drop):
    """Classify the severity of a set of broken fields.

    *broken_fields*   — list of field names (strings) or dicts with ``field``.
    *record_count_drop* — float 0.0–1.0 representing fraction of records lost.

    Returns ``'critical'``, ``'warning'``, or ``'info'``.
    """
    fields = [
        (f["field"] if isinstance(f, dict) else f) for f in (broken_fields or [])
    ]

    if record_count_drop and record_count_drop > 0.5:
        return "critical"

    core_broken = [f for f in fields if f in CORE_FIELDS]
    if core_broken:
        return "critical"

    if fields:
        return "warning"

    return "info"


# ---------------------------------------------------------------------------
# 4. Schema-driven failure detection
# ---------------------------------------------------------------------------

def detect_failures(current_data, expected_schema):
    """Validate *current_data* against *expected_schema*.

    *expected_schema* is a dict mapping field name → spec dict::

        {
            "title": {"type": str,  "required": True,  "regex": r"\\S+"},
            "count": {"type": int,  "required": True},
        }

    Returns a **FailureReport** dict::

        {
            "broken_fields":   [ {field, failure_type, detail, last_good} ],
            "severity":        "critical" | "warning" | "info",
            "record_count_drop": float | None,
            "healthy":         bool,
        }
    """
    broken = []
    for field, spec in expected_schema.items():
        val = current_data.get(field) if isinstance(current_data, dict) else None

        # --- null / missing -------------------------------------------------
        if val is None or (isinstance(val, str) and val.strip() == ""):
            if spec.get("required", False):
                broken.append({
                    "field": field,
                    "failure_type": "null" if val is None else "empty",
                    "detail": "required field is null/empty",
                    "last_good": None,
                })
            continue

        # --- type check -----------------------------------------------------
        exp_type = spec.get("type")
        if exp_type is not None and not isinstance(val, exp_type):
            broken.append({
                "field": field,
                "failure_type": "type_mismatch",
                "detail": f"expected {exp_type.__name__}, got {type(val).__name__}",
                "last_good": None,
            })
            continue

        # --- format / regex check -------------------------------------------
        import re
        pattern = spec.get("regex")
        if pattern and isinstance(val, str):
            if not re.search(pattern, val):
                broken.append({
                    "field": field,
                    "failure_type": "format_mismatch",
                    "detail": f"value does not match pattern {pattern!r}",
                    "last_good": None,
                })

    severity = classify_severity(broken, record_count_drop=0.0)
    return {
        "broken_fields": broken,
        "severity": severity,
        "record_count_drop": None,
        "healthy": len(broken) == 0,
    }


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import re  # noqa: F811  — already imported in detect_failures scope

    # Schema used across all test cases
    schema = {
        "title":         {"type": str, "required": True,  "regex": r"\S+"},
        "article_url":   {"type": str, "required": True,  "regex": r"https?://"},
        "publish_date":  {"type": str, "required": True,  "regex": r"\d{4}-\d{2}-\d{2}"},
        "record_count":  {"type": int, "required": True},
    }

    ok = True

    # --- Test 1: all fields healthy -----------------------------------------
    healthy = {
        "title": "FCA fines XYZ Bank",
        "article_url": "https://fca.org.uk/news/xyz",
        "publish_date": "2025-03-15",
        "record_count": 42,
    }
    report = detect_failures(healthy, schema)
    assert report["healthy"] is True, "healthy data should report healthy=True"
    assert report["severity"] == "info", f"healthy → info, got {report['severity']}"
    print("[PASS] all fields healthy")

    # --- Test 2: one field null (core field) --------------------------------
    null_title = dict(healthy, title=None)
    report = detect_failures(null_title, schema)
    assert not report["healthy"], "null title should be detected"
    assert report["severity"] == "critical", "title is core → critical"
    assert any(b["field"] == "title" and b["failure_type"] == "null"
               for b in report["broken_fields"]), "title null failure expected"
    print("[PASS] one field null → critical")

    # --- Test 3: one field wrong type ---------------------------------------
    wrong_type = dict(healthy, record_count="forty-two")
    report = detect_failures(wrong_type, schema)
    assert not report["healthy"]
    assert report["severity"] == "warning", "record_count not core → warning"
    assert any(b["field"] == "record_count" and b["failure_type"] == "type_mismatch"
               for b in report["broken_fields"])
    print("[PASS] wrong type → warning")

    # --- Test 4: record count drop >50% -------------------------------------
    broken = ["record_count"]
    sev = classify_severity(broken, record_count_drop=0.6)
    assert sev == "critical", f"60% drop → critical, got {sev}"
    print("[PASS] record count drop >50% → critical")

    # --- Test 5: snapshot save / load round-trip ----------------------------
    test_reg = "_selftest"
    test_path = os.path.join(_SNAPSHOT_DIR, f"{test_reg}.jsonl")
    if os.path.exists(test_path):
        os.remove(test_path)
    save_snapshot(test_reg, healthy)
    save_snapshot(test_reg, null_title)
    last = load_last_snapshot(test_reg)
    assert last is not None and last["title"] is None, "snapshot round-trip failed"
    os.remove(test_path)
    print("[PASS] snapshot save/load round-trip")

    # --- Test 6: diff_snapshots detects breakage ----------------------------
    diffs = diff_snapshots(null_title, healthy)
    assert any(d["field"] == "title" and d["issue"] in ("changed", "missing")
               for d in diffs), "diff should flag title"
    print("[PASS] diff_snapshots detects null breakage")

    print("\nAll self-checks passed.")
