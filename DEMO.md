# GeckoRegen — Judge demo (what you say + what they see)

**Sound bite:** *Bright Data scrapes. GeckoRegen governs. BD heals; we validate before we trust it.*

Do **not** open Unlocker / SERP / Browser. This is **Scraper Studio + Self-Healing**.

---

## Setup (2 minutes before they walk up)

1. `python server.py` — http://localhost:8000
2. Dashboard already shows FCA (751 live), Test Shop, FINMA, plus fallback ESMA/BaFin/MAS
3. Second tab: http://localhost:8000/guide
4. Third tab (optional): http://localhost:8000/sandbox

---

## 90-second live path

| Time | You do | You say | They should see |
|---|---|---|---|
| 0:00 | `/guide` | “We’re not a scraper. We’re the reliability layer so compliance teams don’t miss a regulator rewrite.” | Pitch + architecture strip |
| 0:20 | `/` | “Runtime is the real Python path: monitor → detect → heal → validate → memory.” | Pipeline idle, amber UI |
| 0:30 | Point at FCA card | “751 live FCA records from Scraper Studio. Not a mock.” | Health grid, change feed |
| 0:45 | **Simulate Break** | “This swaps the local sandbox (you can open /sandbox) and fires BD Test Shop — a public URL BD can actually crawl.” | Runtime lights Monitor; card goes HEALING |
| 1:00–1:30 | Wait / talk over poll | “Trigger + dataset poll. Detector diffs JSON. If fields fail, healer calls refactor_template. Validator re-runs. We reject a heal under 90% population.” | Stages walk; anomaly stream; timeline |

If the live heal is slow or credits are thin: open the **pre-recorded FCA heal** on the timeline and say it is labelled as a prior BD run.

---

## Architecture (one breath)

```
Browser  →  server.py (Flask + SSE)
         →  monitor.py
         →  bd_client.py   POST /dca/trigger   GET /dca/dataset
         →  detector.py    snapshot JSONL + schema
         →  healer.py      POST .../refactor_template  (only if broken)
         →  validate       re-run + pop ≥ 90%
         →  memory.py/db   SQLite
         →  /api/events    dashboard
```

`adapter.py` sits beside this: it changes `scan_frequency_minutes` from how noisy a regulator is.

---

## Honest lines (say them; judges punish the opposite)

- ESMA / BaFin / MAS = fallback rows, `active: 0`. MAS is BD-blocked.
- Simulate Break does **not** make Bright Data scrape `localhost`.
- Live BD hop = `ecommerce-shop-brd.vercel.app`.

---

## If they ask “why not Unlocker?”

Unlocker returns HTML. We need **stable JSON fields** per regulator, plus **self-heal of the scraper template**. That’s Studio + Self-Healing only.
