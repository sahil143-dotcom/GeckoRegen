# GeckoRegen — Product Specification

## Problem
Compliance teams cannot reliably keep up with regulatory changes. Regulators publish across fragmented channels, jurisdictions, formats, and websites. When a regulator redesigns their site, scrapers break silently — critical changes get missed.

## Solution
GeckoRegen is a self-healing regulatory intelligence monitor. It scrapes regulator websites via Bright Data Scraper Studio, detects when scrapers break, auto-heals them via BD's Self-Healing API, validates repairs against data-quality constraints, and builds persistent organizational memory that learns from every change.

## The 4 Unique Layers

### Layer 1: Self-Healing Pipeline (the engine)
- Scraper Studio builds scrapers for each regulator site
- Snapshot diffing detects when a scraper breaks (field returns null, type mismatch, record count drop)
- BD Self-Healing API regenerates scraper code with contextual prompts
- Re-run validates the fix

### Layer 2: Validation Gate (the differentiator)
- After healing, check: field population rate (>90%?), format consistency (all same type?), value range (regex match?), record count (no significant drop?)
- Accept or reject based on data — not blind trust
- If rejected, re-trigger heal with sharper prompt including the specific failure

### Layer 3: Persistent Organizational Memory (the brain)
- Every detected change stored with context: regulator, jurisdiction, type, severity, affected controls, timestamp
- Regulator behavior profiles: publish frequency, change types, seasonal patterns
- Impact correlation: "Similar to GDPR amendment 6 months ago that affected your data retention"
- Healing event log: what broke, what prompt fixed it, attempts, success rate

### Layer 4: Adaptive Monitoring (the evolution)
- Dynamic scan frequency: high-activity regulators scanned more often, quiet ones less
- Anomaly-triggered scans: sudden spike in regulator activity → immediate scan regardless of schedule
- Priority routing: changes from regulators that historically impact YOUR organization escalated faster

## Target Regulators (start with 5)
1. FCA (UK) — fca.org.uk ✅ scraper built, 751 records
2. ESMA (EU) — esma.europa.eu ⏳ generating
3. BaFin (Germany) — bundesanstalt-finanzdienstleistungsaufsicht.de
4. MAS (Singapore) — mas.gov.sg
5. FINMA (Switzerland) — finma.ch

## Bright Data Products Used
1. Scraper Studio — build and run scrapers (central)
2. Self-Healing API — regenerate broken scrapers (core)
3. Trigger/Dataset API — run scrapers programmatically
4. Collectors List API — manage scraper inventory

## Stack
- **Backend:** Python (stdlib + requests + sqlite3)
- **Frontend:** HTML/CSS/JS (vanilla or minimal framework)
- **DB:** SQLite (production-grade schema, not JSON files)
- **BD:** Scraper Studio + Self-Healing API
- **Deploy:** Vercel or local

## What We Don't Build
- No AI chat agent (dashboard-first)
- No audio/spoken channel
- No PDF/vision channel
- No multi-vendor LLM tiers
- No 3D globe
- No file-based storage
- No hallucinated fallback data
- No SSL disabling

## Judging Criteria Alignment
- **01 Impact:** Regulatory compliance = millions in fines if missed
- **02 Creativity:** Self-healing + memory + adaptation = nobody else has this
- **03 Technical excellence:** Real DB, validation gates, smoke tests, documented decisions
- **04 Scraper Studio:** Central — 4+ BD products, deepest integration
- **05 Self-healing:** This IS our product
- **06 Presentation:** Live break → heal → recover demo + judge guide page
