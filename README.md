# GeckoRegen

**Self-healing regulatory intelligence monitor.** Compliance teams can't keep up with regulatory changes — regulators publish across fragmented sites, jurisdictions, and formats. When a regulator redesigns their website, scrapers break silently and critical changes get missed.

GeckoRegen never goes blind. It continuously scrapes regulator websites via Bright Data Scraper Studio, detects when scrapers break, auto-heals them via Bright Data's Self-Healing API, **independently validates** the repaired data before putting it back into production, and builds persistent organizational memory that learns from every change.

> GeckoRegen is not a scraper. It is an autonomous reliability layer for web-data pipelines. Bright Data scrapes; GeckoRegen governs. Bright Data heals; GeckoRegen validates before accepting.

---

## How it works

```
1. Monitor triggers scraper (BD Scraper Studio)
2. Detector diffs output vs last-known-good snapshot
3. If breakage detected → Healer generates contextual prompt
4. Healer calls BD Self-Healing API → BD regenerates scraper code
5. Validator re-runs scraper, checks output against schema constraints
6. If validation passes → accept heal, update health to "healthy"
7. If validation fails → reject, re-trigger with sharper prompt (max 3 attempts)
8. Memory records every change, every heal, learns regulator behavior patterns
9. Adapter adjusts scan frequency based on learned activity
```

### The 4 layers

| Layer | What it does | Why it's unique |
|---|---|---|
| **Self-Healing Pipeline** | Detects breakage → calls BD Self-Healing API → heals scraper | Nobody else automates the full heal loop |
| **Validation Gate** | After healing, re-runs scraper and checks: field population rate, format consistency, value range, record count | BD's heal is not trusted blindly. We verify independently. |
| **Persistent Memory** | Every change stored with context. Regulator behavior profiles learned over time. Impact correlation with historical changes. | The system gets smarter the longer it runs. |
| **Adaptive Monitoring** | Dynamic scan frequency — active regulators scanned more often, quiet ones less. Anomaly-triggered scans on activity spikes. | No competitor adapts. They all scan at fixed intervals. |

---

## Bright Data products used

| Product | Role |
|---|---|
| **Scraper Studio** | Builds and runs scrapers for each regulator site (central) |
| **Self-Healing API** | Regenerates broken scraper code when sites change (core) |
| **Trigger/Dataset API** | Runs scrapers programmatically, downloads structured JSON |
| **Collectors List API** | Manages scraper inventory, reads output schemas |

---

## Tech stack

- **Backend:** Python (stdlib + requests + sqlite3 + concurrent.futures)
- **Frontend:** Vanilla HTML/CSS/JS (no build step, no frameworks)
- **Database:** SQLite (5 tables — regulators, health_scores, changes, healing_events, regulator_profiles)
- **Server:** Flask (8 API routes + SSE for live dashboard updates)
- **BD:** Scraper Studio + Self-Healing API

---

## Setup

### Prerequisites

- Python 3.11+
- Bright Data account with Scraper Studio credits
- Bright Data API token

### Install

```bash
pip install requests flask python-dotenv
```

### Configure

```bash
cp .env.example .env
# Edit .env: BRIGHTDATA_API_KEY=your_token_here
```

### Run

```bash
# Initialize database
python db.py
python seed_demo.py   # fallback rows for ESMA/BaFin/MAS/FINMA if empty
python server.py

# Start the dashboard
python server.py
# Open http://localhost:8000
```

---

## Demo guide

Visit `/guide` in the dashboard for a step-by-step walkthrough.

### Quick demo flow

1. **Dashboard loads** — live FCA/FINMA/Test Shop plus fallback ESMA/BaFin/MAS (tagged `fallback:synthetic`, not live scrapes)
2. **Click "Simulate Break"** — `POST /api/break` swaps `sandbox/index.html` (preview `/sandbox`), then scans **BD Test Shop** at its public URL
3. **Local redesign is visible at `/sandbox`**. Bright Data cannot scrape localhost — the live BD hop is the Test Shop URL
4. **Detector** diffs JSON vs last-known-good; health grid pulses red/amber
5. **If fields break**, Healer sends a contextual prompt to BD Self-Healing API
6. **Validator** re-runs and checks population ≥ 90% before accepting
7. **If credits run out**, the timeline shows a pre-recorded FCA heal (labelled as such)
8. **Memory** `GET /api/similar` surfaces similar historical changes

---

## Engineering decisions

1. **SQLite, not Postgres** — zero config, single-file, portable for demo. Upgrade: migrate to Postgres via SQLAlchemy.
2. **Flask, not FastAPI** — minimal, no async complexity. SSE via Flask Response(generator).
3. **Vanilla JS frontend, not React** — no build step, no node_modules. Upgrade: migrate to React if needed.
4. **Snapshot diffing, not DOM diffing** — diff JSON output, not HTML structure. Detects what the scraper returns, not what the page looks like.
5. **Validation gate after every heal** — BD's heal is not trusted blindly. Re-run + schema check before accepting.
6. **Contextual heal prompts** — include field name, expected pattern, last-known-good value. Not generic "it broke."
7. **Max 3 heal attempts** — if validation fails 3 times, alert for manual intervention. Don't loop forever.
8. **Concurrent scraper runs with cap** — ThreadPoolExecutor(max_workers=3) to respect rate limits.
9. **Pre-recorded fallback data** — if BD credits run out or network fails, demo uses real recorded data.
10. **`.env` gitignored** — API tokens never committed. Public data only.

---

## Project structure

```
GeckoRegen/
├── bd_client.py            # Bright Data API client (6 endpoints)
├── db.py                   # SQLite schema + queries (5 tables)
├── detector.py              # Snapshot diff + failure detection + severity
├── healer.py               # Contextual prompts + BD heal API + validation gate
├── memory.py               # Regulator profiles + impact correlation + timeline
├── monitor.py              # Orchestrator — wires all modules, concurrent runner
├── adapter.py              # Adaptive scan frequency scheduler
├── server.py               # Flask API (8 routes + SSE)
├── sandbox/                 # Fake regulator site for demo
│   ├── index.html           # Original page (scraper works)
│   └── broken-index.html     # Redesigned page (breaks scraper)
├── web/                    # Dashboard frontend
│   ├── index.html           # Health grid + change feed + healing timeline
│   ├── guide.html           # Judge guide page
│   ├── app.js                # SSE client + dashboard logic
│   └── style.css             # Dark theme, professional
├── data/
│   ├── geckoregen.db         # SQLite database
│   ├── fca_news_raw.json     # 751 FCA regulatory records
│   └── snapshots/            # Per-regulator JSONL snapshot history
├── SPEC.md                   # Product specification
├── DESIGN.md                 # Architecture + schema + file structure
├── requirements.txt
└── .env                      # API keys (gitignored)
```

---

## Target regulators

| Regulator | Jurisdiction | Status |
|---|---|---|
| FCA (UK) | UK | Live BD collector — 751 records |
| FINMA | CH | Live BD collector |
| BD Test Shop | TEST | Live BD collector — public URL for heal demo |
| ESMA (EU) | EU | Fallback rows only (`active: 0`) |
| BaFin (Germany) | DE | Fallback rows only (`active: 0`) |
| MAS (Singapore) | SG | Fallback only — `mas.gov.sg` is BD-blocked |

---

Built for the **Scrape-Verse Hackathon** by WeMakeDevs + Bright Data.
