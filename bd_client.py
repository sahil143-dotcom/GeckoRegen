"""
GeckoRegen — Bright Data API client.

Wraps the 4 BD API surfaces we use:
  - Scraper Studio: trigger scrapers, download results
  - Self-Healing: submit heal prompt, poll progress, approve/reject
  - Collectors list: discover scrapers + their output schemas

Every function returns parsed JSON or raises. No silent failures.
"""
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

API_TOKEN = os.environ["BRIGHTDATA_API_KEY"]
BASE = "https://api.brightdata.com"
HEADERS = {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"}


# ── Scraper Studio: trigger + download ──────────────────────────────

def trigger_scraper(collector_id, urls, queue_next=1):
    """POST /dca/trigger — start a scrape job, return snapshot_id."""
    r = requests.post(
        f"{BASE}/dca/trigger",
        params={"collector": collector_id, "queue_next": queue_next},
        headers=HEADERS,
        json=[{"url": u} for u in urls],
    )
    r.raise_for_status()
    return r.json()["collection_id"]


def get_dataset(snapshot_id):
    """GET /dca/dataset — download results. Polls until ready."""
    for _ in range(180):  # 180 x 5s = 15min — BD crawls can take 3–12 min
        r = requests.get(
            f"{BASE}/dca/dataset",
            params={"id": snapshot_id},
            headers=HEADERS,
        )
        if r.status_code == 200:
            return r.json()
        if r.status_code == 202:
            time.sleep(5)
            continue
        r.raise_for_status()
    raise TimeoutError(f"Snapshot {snapshot_id} not ready after 10min")


# ── Collectors list ─────────────────────────────────────────────────

def list_scrapers(search=None):
    """GET /dca/collectors_list — list all scrapers in account."""
    params = {}
    if search:
        params["search"] = search
    r = requests.get(f"{BASE}/dca/collectors_list", headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()


# ── Self-Healing: submit → poll → approve/reject ────────────────────

def trigger_heal(collector_id, prompt, url=None):
    """POST /dca/collectors/{id}/refactor_template — start self-healing."""
    body = {"prompt": prompt}
    if url:
        body["url"] = url
    r = requests.post(
        f"{BASE}/dca/collectors/{collector_id}/refactor_template",
        headers=HEADERS,
        json=body,
    )
    r.raise_for_status()
    return r.json()


def poll_heal(collector_id, interval=5, timeout=900):
    """GET /dca/collectors/{id}/refactor_template/progress — poll until done."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(
            f"{BASE}/dca/collectors/{collector_id}/refactor_template/progress",
            headers=HEADERS,
        )
        r.raise_for_status()
        data = r.json()
        status = data.get("status", "")
        if status == "pending_answer":
            return data  # ready for approval
        if status in ("done", "failed"):
            return data
        time.sleep(interval)
    raise TimeoutError(f"Heal for {collector_id} timed out after {timeout}s")


def approve_heal(collector_id, approve=True):
    """POST /dca/collectors/{id}/resume_automation_job — approve or reject.
    Note: BD API uses {"message": true/false}, not {"approve": bool}.
    """
    r = requests.post(
        f"{BASE}/dca/collectors/{collector_id}/resume_automation_job",
        headers=HEADERS,
        json={"message": approve},
    )
    r.raise_for_status()
    return r.json()


# ── Smoke test ──────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== GeckoRegen BD Client Smoke Test ===\n")

    # 1. List scrapers
    print("1. Listing scrapers...")
    scrapers = list_scrapers()
    print(f"   Found {scrapers['total']} scrapers")
    for s in scrapers.get("data", []):
        print(f"   {s['id']} | {s['name']} | active={s.get('active')}")

    # 2. If we have scrapers, try triggering one
    if scrapers["data"]:
        first = scrapers["data"][0]
        print(f"\n2. Triggering {first['id']}...")
        try:
            snap = trigger_scraper(first["id"], ["https://www.esma.europa.eu/news-events/news"])
            print(f"   Snapshot: {snap}")
            print("   Waiting for data (this takes ~3min)...")
            data = get_dataset(snap)
            print(f"   Got {len(data)} records")
            if data:
                print(f"   First record keys: {list(data[0].keys())}")
        except Exception as e:
            print(f"   Error: {e}")
    else:
        print("\n2. No scrapers yet — create one with: bdata scraper create <url> <desc>")

    print("\n=== Smoke test complete ===")
