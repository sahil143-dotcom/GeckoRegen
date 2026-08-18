"""GeckoRegen — Flask API + SSE events.

Serves the dashboard, exposes JSON APIs for health/changes/heals/regulators,
triggers manual scans, and streams live events via Server-Sent Events.

Minimal: no auth, no CORS, no migrations. stdlib + Flask only.
"""
from __future__ import annotations

import json
import os
import queue
import threading
from typing import Any

from flask import Flask, Response, jsonify, request, send_from_directory

import db
import monitor

# ── paths ────────────────────────────────────────────────────────────

_BASE = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = os.path.join(_BASE, "web")

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
    """
    body = request.get_json(silent=True) or {}
    reg_id = body.get("regulator_id")
    if reg_id is None:
        return jsonify({"error": "regulator_id required"}), 400

    # validate regulator exists
    regulators = {r["id"]: r for r in db.get_regulators()}
    if reg_id not in regulators:
        return jsonify({"error": f"regulator {reg_id} not found"}), 404

    reg = regulators[reg_id]

    def _run_scan():
        try:
            publish_event("scan_started", {"regulator_id": reg_id, "name": reg["name"]})
            result = monitor.monitor_regulator(reg_id)
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

    thread = threading.Thread(target=_run_scan, daemon=True)
    thread.start()

    return jsonify({"status": "triggered", "regulator_id": reg_id, "name": reg["name"]})


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
        "/", "/guide", "/api/health", "/api/changes",
        "/api/heals", "/api/regulators", "/api/scan", "/api/events",
    ]
    for path in expected:
        assert path in rules, f"route {path} not registered"

    # event bus round-trip
    q = _subscribe()
    publish_event("test", {"v": 1})
    msg = q.get(timeout=2)
    assert json.loads(msg)["type"] == "test", "event bus broken"
    _unsubscribe(q)

    print("SELF-CHECK PASSED: 8 routes registered, event bus works")


if __name__ == "__main__":
    _self_check()
    db.init_db()
    app.run(host="0.0.0.0", port=8000, debug=True, threaded=True)
