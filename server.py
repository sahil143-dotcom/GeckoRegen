"""GeckoRegen — Flask API + SSE events.

Serves the dashboard, exposes JSON APIs for health/changes/heals/regulators,
triggers manual scans, and streams live events via Server-Sent Events.

Minimal: no auth, no CORS, no migrations. stdlib + Flask only.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
from typing import Any

from flask import Flask, Response, jsonify, request, send_from_directory

import db
import memory
import monitor
import seed_demo

# ── paths ────────────────────────────────────────────────────────────

_BASE = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = os.path.join(_BASE, "web")
_SANDBOX_DIR = os.path.join(_BASE, "sandbox")
_SANDBOX_INDEX = os.path.join(_SANDBOX_DIR, "index.html")
_SANDBOX_BROKEN = os.path.join(_SANDBOX_DIR, "broken-index.html")
_SANDBOX_WORKING = os.path.join(_SANDBOX_DIR, "working-index.html")

SCAN_COOLDOWN_SECONDS = 30
_scan_lock = threading.Lock()
_last_scan_at: dict[int, float] = {}
_scan_in_flight: set[int] = set()

# ── Flask app ────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=None)


# ── in-process event bus (ponytail: global list of queues, no Redis) ──

_subscribers: list[queue.Queue] = []
_sub_lock = threading.Lock()


def publish_event(event_type: str, data: dict[str, Any]) -> None:
    """Broadcast an event to all connected SSE clients."""
    payload = json.dumps({"type": event_type, "data": data}, default=str)
    with _sub_lock:
        for q in _subscribers:
            q.put(payload)


def _subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue()
    with _sub_lock:
        _subscribers.append(q)
    return q


def _unsubscribe(q: queue.Queue) -> None:
    with _sub_lock:
        if q in _subscribers:
            _subscribers.remove(q)


# ── routes: pages ────────────────────────────────────────────────────


@app.route("/")
def index():
    return send_from_directory(_WEB_DIR, "index.html")


@app.route("/guide")
def guide():
    return send_from_directory(_WEB_DIR, "guide.html")


@app.route("/style.css")
def style_css():
    return send_from_directory(_WEB_DIR, "style.css", mimetype="text/css")


@app.route("/app.js")
def app_js():
    return send_from_directory(_WEB_DIR, "app.js", mimetype="application/javascript")


@app.route("/sandbox")
def sandbox_page():
    """Local preview of the demo regulator page (working or broken)."""
    return send_from_directory(_SANDBOX_DIR, "index.html")


# ── routes: JSON API ─────────────────────────────────────────────────


@app.route("/api/health")
def api_health():
    """All scraper health scores — latest health row per regulator."""
    regulators = db.get_regulators()
    result = []
    for reg in regulators:
        rid = reg["id"]
        scores = db.get_health(rid)
        latest = scores[0] if scores else None
        result.append({
            "regulator_id": rid,
            "regulator_name": reg["name"],
            "jurisdiction": reg.get("jurisdiction"),
            "latest": latest,
            "history_count": len(scores),
        })
    return jsonify(result)


@app.route("/api/changes")
def api_changes():
    """Recent changes with severity, newest first."""
    limit = request.args.get("limit", 50, type=int)
    return jsonify(db.get_changes(limit))


@app.route("/api/heals")
def api_heals():
    """Healing event history, newest first."""
    limit = request.args.get("limit", 50, type=int)
    return jsonify(db.get_heals(limit))


@app.route("/api/regulators")
def api_regulators():
    """Regulator list + behavior profiles."""
    regulators = db.get_regulators()
    result = []
    for reg in regulators:
        rid = reg["id"]
        profile = db.get_profile(rid)
        result.append({**reg, "profile": profile})
    return jsonify(result)


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Trigger a manual scan of a regulator.

    JSON body: {"regulator_id": <int>}
    Runs monitor.monitor_regulator in a background thread so the HTTP
    request returns immediately. Events are published via publish_event.
    Cooldown: one in-flight scan per regulator, plus 30s between starts.
    """
    body = request.get_json(silent=True) or {}
    reg_id = body.get("regulator_id")
    if reg_id is None:
        return jsonify({"error": "regulator_id required"}), 400
    try:
        reg_id = int(reg_id)
    except (TypeError, ValueError):
        return jsonify({"error": "regulator_id must be an integer"}), 400

    regulators = {r["id"]: r for r in db.get_regulators()}
    if reg_id not in regulators:
        return jsonify({"error": f"regulator {reg_id} not found"}), 404

    reg = regulators[reg_id]
    now = time.time()
    with _scan_lock:
        if reg_id in _scan_in_flight:
            return jsonify({
                "error": "scan already running for this regulator",
                "regulator_id": reg_id,
            }), 429
        last = _last_scan_at.get(reg_id, 0)
        wait = SCAN_COOLDOWN_SECONDS - (now - last)
        if wait > 0:
            return jsonify({
                "error": "scan cooldown",
                "retry_after_seconds": int(wait) + 1,
                "regulator_id": reg_id,
            }), 429
        _scan_in_flight.add(reg_id)
        _last_scan_at[reg_id] = now

    def _run_scan():
        try:
            publish_event("scan_started", {"regulator_id": reg_id, "name": reg["name"]})
            result = monitor.monitor_regulator(reg)
            publish_event("scan_completed", {
                "regulator_id": reg_id,
                "name": reg["name"],
                "result": result,
            })
        except Exception as exc:
            publish_event("scan_failed", {
                "regulator_id": reg_id,
                "name": reg["name"],
                "error": str(exc),
            })
        finally:
            with _scan_lock:
                _scan_in_flight.discard(reg_id)

    thread = threading.Thread(target=_run_scan, daemon=True)
    thread.start()

    return jsonify({"status": "triggered", "regulator_id": reg_id, "name": reg["name"]})


@app.route("/api/break", methods=["POST"])
def api_break():
    """Swap sandbox HTML to simulate a site redesign (or restore it).

    JSON body: {"broken": true}  → copy broken-index.html over index.html
               {"broken": false} → restore working-index.html
    Judges can view the live page at GET /sandbox.
    """
    body = request.get_json(silent=True) or {}
    broken = body.get("broken", True)
    if not os.path.exists(_SANDBOX_BROKEN):
        return jsonify({"error": "sandbox/broken-index.html missing"}), 500

    if not os.path.exists(_SANDBOX_WORKING) and os.path.exists(_SANDBOX_INDEX):
        shutil.copy2(_SANDBOX_INDEX, _SANDBOX_WORKING)

    if broken:
        shutil.copy2(_SANDBOX_BROKEN, _SANDBOX_INDEX)
        state = "broken"
    else:
        src = _SANDBOX_WORKING if os.path.exists(_SANDBOX_WORKING) else _SANDBOX_INDEX
        shutil.copy2(src, _SANDBOX_INDEX)
        state = "working"

    publish_event("sandbox_toggled", {"state": state, "preview": "/sandbox"})
    return jsonify({
        "status": state,
        "preview": "/sandbox",
        "note": "Local HTML swapped. Bright Data scrapes public URLs — BD Test Shop is https://ecommerce-shop-brd.vercel.app. The NFRA sandbox at /sandbox is the visible redesign for judges.",
    })


@app.route("/api/similar")
def api_similar():
    """Impact correlation — historical changes similar to a title/category."""
    title = request.args.get("title") or ""
    category = request.args.get("category") or ""
    summary = request.args.get("summary") or ""
    limit = request.args.get("limit", 5, type=int)
    if not title and not category:
        return jsonify({"error": "title or category required"}), 400
    matches = memory.find_similar_changes(
        {"title": title, "category": category, "summary": summary},
        limit=limit,
    )
    return jsonify(matches)


# ── SSE event stream ─────────────────────────────────────────────────


@app.route("/api/events")
def api_events():
    """SSE stream — live events: health changes, heals, new changes, scans.

    Uses Flask Response with a generator that reads from a per-client queue.
    """
    q = _subscribe()

    def stream():
        try:
            # initial heartbeat so client knows stream is alive
            yield ": connected\n\n"
            while True:
                try:
                    payload = q.get(timeout=15)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    # keep-alive comment (SSE spec: lines starting with :)
                    yield ": keepalive\n\n"
        finally:
            _unsubscribe(q)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering
        },
    )


# ── self-check ───────────────────────────────────────────────────────


def _self_check() -> None:
    """Verify all routes are registered and event bus works."""
    rules = {r.rule: r.methods for r in app.url_map.iter_rules()}

    expected = [
        "/", "/guide", "/sandbox", "/api/health", "/api/changes",
        "/api/heals", "/api/regulators", "/api/scan", "/api/break",
        "/api/similar", "/api/events",
    ]
    for path in expected:
        assert path in rules, f"route {path} not registered"

    # event bus round-trip
    q = _subscribe()
    publish_event("test", {"v": 1})
    msg = q.get(timeout=2)
    assert json.loads(msg)["type"] == "test", "event bus broken"
    _unsubscribe(q)

    print("SELF-CHECK PASSED: 11 routes registered, event bus works")


if __name__ == "__main__":
    _self_check()
    db.init_db()
    monitor.init_from_config()
    seed_demo.seed_if_needed()
    app.run(host="0.0.0.0", port=8000, debug=True, threaded=True)
