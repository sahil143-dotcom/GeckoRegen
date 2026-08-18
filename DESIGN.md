# GeckoRegen — Design Document

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      GeckoRegen                              │
│                                                              │
│  ┌─────────────┐    ┌──────────────┐    ┌─────────────┐     │
│  │  Monitor     │───▶│  Detector    │───▶│  Healer     │     │
│  │  (cron/sched)│    │  (snapshot   │    │  (BD Self-  │     │
│  │  runs N      │    │   diff +     │    │   Healing   │     │
│  │  scrapers)   │    │   health     │    │   API)      │     │
│  └─────────────┘    └──────────────┘    └──────┬──────┘     │
│                                                │            │
│                                          ┌─────▼─────┐     │
│                                          │ Validator  │     │
│                                          │ (re-run +  │     │
│                                          │  schema    │     │
│                                          │  check)    │     │
│                                          └─────┬─────┘     │
│                                                │            │
│              ┌─────────────────────────────────┘            │
│              │                                               │
│              ▼                                               │
│  ┌─────────────────────┐    ┌────────────────────────┐      │
│  │  Memory (SQLite)    │    │  Dashboard (frontend)  │      │
│  │                     │    │                        │      │
│  │  - regulators       │    │  - health grid         │      │
│  │  - scrapers         │    │  - change feed         │      │
│  │  - snapshots        │    │  - healing timeline    │      │
│  │  - changes          │    │  - judge guide         │      │
│  │  - healing_events   │    │                        │      │
│  │  - health_scores    │    └────────────────────────┘      │
│  │  - regulator_profiles│                                     │
│  └─────────────────────┘                                     │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

## Database Schema (SQLite)

```sql
-- Regulator registry
CREATE TABLE regulators (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,           -- "FCA", "ESMA", "BaFin"
    full_name TEXT,                 -- "Financial Conduct Authority"
    jurisdiction TEXT,              -- "UK", "EU", "DE", "SG", "CH"
    url TEXT NOT NULL,              -- regulator news/press page URL
    collector_id TEXT,              -- BD Scraper Studio collector c_* ID
    active INTEGER DEFAULT 1,
    scan_frequency_minutes INTEGER DEFAULT 360,  -- adaptive
    created_at TEXT DEFAULT (datetime('now')),
    last_scanned_at TEXT
);

-- Scraper health log
CREATE TABLE health_scores (
    id INTEGER PRIMARY KEY,
    regulator_id INTEGER REFERENCES regulators(id),
    timestamp TEXT DEFAULT (datetime('now')),
    status TEXT NOT NULL,           -- "healthy", "degraded", "broken", "healing"
    field_population_rate REAL,     -- 0.0 to 1.0
    record_count INTEGER,
    missing_fields TEXT,            -- JSON array of field names that failed
    error_details TEXT
);

-- Change detection log
CREATE TABLE changes (
    id INTEGER PRIMARY KEY,
    regulator_id INTEGER REFERENCES regulators(id),
    detected_at TEXT DEFAULT (datetime('now')),
    title TEXT,
    category TEXT,
    publish_date TEXT,
    summary TEXT,
    article_url TEXT,
    severity TEXT,                  -- "critical", "warning", "info"
    is_new INTEGER DEFAULT 1,       -- 1 = first detection, 0 = already seen
    snapshot_id TEXT                -- BD snapshot that captured this
);

-- Self-healing event log
CREATE TABLE healing_events (
    id INTEGER PRIMARY KEY,
    regulator_id INTEGER REFERENCES regulators(id),
    triggered_at TEXT DEFAULT (datetime('now')),
    broken_fields TEXT,             -- JSON: which fields failed
    heal_prompt TEXT,               -- the contextual prompt sent to BD
    bd_job_id TEXT,                 -- BD refactor_template job ID
    status TEXT,                    -- "triggered", "pending_answer", "approved", "rejected", "done", "failed"
    attempts INTEGER DEFAULT 1,
    validation_passed INTEGER,      -- 1 = fix validated, 0 = failed validation
    validation_details TEXT,       -- JSON: population rate, format check, etc.
    completed_at TEXT,
    duration_seconds INTEGER
);

-- Regulator behavior profiles (learned over time)
CREATE TABLE regulator_profiles (
    id INTEGER PRIMARY KEY,
    regulator_id INTEGER REFERENCES regulators(id),
    avg_publications_per_week REAL,
    common_change_types TEXT,       -- JSON: ["enforcement", "guidance", "rule_change"]
    seasonal_patterns TEXT,         -- JSON: {"summer": "low", "q4": "high"}
    last_profiled_at TEXT,
    total_changes_detected INTEGER DEFAULT 0,
    total_heals INTEGER DEFAULT 0,
    successful_heals INTEGER DEFAULT 0
);
```

## File Structure

```
GeckoRegen/
├── .env                    # API keys (gitignored)
├── .gitignore
├── SPEC.md                 # Product spec
├── DESIGN.md               # This file
├── README.md               # For judges — engineering decisions, setup, demo
├── requirements.txt
├── bd_client.py            # BD API client (DONE — verified)
├── db.py                   # SQLite schema + queries
├── monitor.py              # Scraper runner + health checker
├── detector.py             # Snapshot diff + failure detection + severity
├── healer.py               # Contextual prompt gen + BD heal API + validation gate
├── memory.py               # Regulator profiles + impact correlation + behavior learning
├── adapter.py              # Adaptive scan frequency scheduler
├── server.py               # Flask API — serves dashboard + SSE events
├── scrapers_config.json    # Scraper registry
├── data/
│   ├── geckoregen.db       # SQLite database
│   ├── fca_news_raw.json   # FCA data (Day 1)
│   └── snapshots/          # Per-regulator JSONL snapshot history
└── web/
    ├── index.html          # Dashboard — health grid + change feed
    ├── guide.html          # Judge guide page
    ├── style.css
    └── app.js              # SSE client + dashboard logic
```

## Data Flow

```
1. Monitor triggers scraper (POST /dca/trigger with collector_id)
2. Poll for data (GET /dca/dataset)
3. Save snapshot to data/snapshots/{regulator}.jsonl
4. Detector diffs current vs last-known-good
   → If no breakage: store changes in DB, update health score
   → If breakage detected: identify broken fields, classify severity
5. Healer generates contextual prompt:
   "Field {field} returns null on {regulator}. Expected pattern: {regex}.
    Last-known-good value: {example}. Fix to extract from new page structure."
6. Healer calls BD Self-Healing API (refactor_template → poll → approve)
7. Validator re-runs scraper on same URL
   → Checks: population rate, format consistency, value range, record count
   → If passes: accept heal, update health score to "healthy"
   → If fails: reject, re-trigger with sharper prompt (max 3 attempts)
8. Memory updates: regulator profile, heal event log, change history
9. Adapter adjusts scan frequency based on regulator activity
10. Dashboard shows all of this live via SSE
```

## API Endpoints (server.py — Flask)

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Dashboard (health grid + change feed) |
| GET | `/guide` | Judge guide page |
| GET | `/api/health` | JSON — all scraper health scores |
| GET | `/api/changes` | JSON — recent changes with severity |
| GET | `/api/heals` | JSON — healing event history |
| GET | `/api/regulators` | JSON — regulator list + profiles |
| POST | `/api/scan` | Trigger a manual scan (for demo) |
| POST | `/api/break` | Simulate a break (for demo — modifies sandbox site) |
| GET | `/api/events` | SSE — live events stream (health changes, heals, new changes) |

## Demo Sandbox Site

We need a simple HTML page that looks like a regulator news page, hosted locally or on Vercel. For the demo, we modify its HTML structure to simulate a "site redesign" that breaks the scraper.

```
sandbox/
└── index.html    # Fake regulator news page with product listings
                     We change CSS classes/structure live to trigger breakage
```

## Engineering Decisions (for README — Spider-Sense track)

1. **SQLite, not Postgres** — zero config, single-file, portable for demo. Upgrade path: migrate to Postgres via SQLAlchemy if scaling.
2. **Flask, not FastAPI** — minimal, no async complexity needed. SSE via Flask's `Response(generator)`.
3. **Vanilla JS frontend, not React** — no build step, no node_modules, fastest path to a working dashboard. Upgrade path: migrate to React/Next.js if needed.
4. **Snapshot diffing, not DOM diffing** — simpler, more reliable. Diff JSON output, not HTML structure. Detects what the scraper returns, not what the page looks like.
5. **Validation gate after every heal** — BD's heal is not trusted blindly. Re-run + schema check before accepting.
6. **Contextual heal prompts** — include field name, expected pattern, last-known-good value. Not generic "it broke."
7. **Max 3 heal attempts** — if validation fails 3 times, alert for manual intervention. Don't loop forever.
8. **Concurrent scraper runs with cap** — `ThreadPoolExecutor(max_workers=3)` to respect rate limits.
9. **Pre-recorded fallback data** — if BD credits run out or network fails, demo uses real recorded data, not hallucinated.
10. **`.env` gitignored** — API tokens never committed. Public data only.
