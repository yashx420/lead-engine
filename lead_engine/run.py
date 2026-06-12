"""Orchestrator: discover -> dedup -> enrich -> score -> append to leads.csv

Usage:
    python -m lead_engine.run                 # full run
    python -m lead_engine.run --limit 10       # enrich only first 10 (cheap test)
    python -m lead_engine.run --discover-only  # just list candidates, no LLM spend
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date

import httpx
import openai
from dotenv import load_dotenv

from . import config, discover, enrich, score

CSV_FIELDS = [
    "firm_name", "domain", "website", "phone", "address", "state",
    "google_rating", "rating_count", "attorney_count", "team_page_found", "practice_areas",
    "is_law_firm", "faq_present", "schema_present", "google_ads_pixel", "book_call_cta",
    "passes_icp", "icp_reason", "score", "priority_tier",
    "query_practice_area", "query_city", "source", "place_id", "first_seen", "enrich_status",
]


def _load_seen() -> tuple[set[str], set[str]]:
    """Existing place_ids + domains, so we never re-process or duplicate a firm."""
    place_ids: set[str] = set()
    domains: set[str] = set()
    if config.LEADS_CSV.exists():
        with config.LEADS_CSV.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                place_ids.add(row.get("place_id", ""))
                if row.get("domain"):
                    domains.add(row["domain"])
    return place_ids, domains


def _row_for_csv(r: dict) -> dict:
    if isinstance(r.get("practice_areas"), list):
        r = {**r, "practice_areas": "; ".join(r["practice_areas"])}
    return r


def main(argv: list[str] | None = None) -> int:
    # override=True so .env wins over any stale OPENAI_API_KEY in the system env.
    load_dotenv(config.ENV_FILE, override=True)
    ap = argparse.ArgumentParser(description="Local law-firm lead engine (V1).")
    ap.add_argument("--limit", type=int, default=None, help="cap firms enriched (cheap test run)")
    ap.add_argument("--discover-only", action="store_true", help="list candidates, skip enrichment/LLM")
    ap.add_argument("--no-browser", action="store_true", help="skip Playwright fallback (faster, more fetch_failed on Cloudflare/JS sites)")
    args = ap.parse_args(argv)

    print("== Discovery (Google Places) ==")
    candidates = discover.discover(config.PRACTICE_AREAS, config.CITIES)
    print(f"  -> {len(candidates)} candidate firms with a website\n")

    seen_ids, seen_domains = _load_seen()
    fresh = [c for c in candidates if c["place_id"] not in seen_ids and c["domain"] not in seen_domains]
    print(f"== Dedup ==\n  {len(candidates) - len(fresh)} already in leads.csv, {len(fresh)} new\n")

    if args.discover_only:
        for c in fresh[: args.limit or len(fresh)]:
            print(f"  {c['firm_name'][:40]:40s} {c['domain']}")
        return 0

    if args.limit:
        fresh = fresh[: args.limit]

    print(f"== Enrich + score ({len(fresh)} firms) ==")
    llm = openai.OpenAI()
    browser = None if args.no_browser else enrich.BrowserFetcher()
    today = date.today().isoformat()

    # Write each lead immediately (append + flush) so progress is durable: a crash
    # keeps everything done so far, and a restart resumes via dedup.
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not config.LEADS_CSV.exists()
    passed = processed = 0
    with config.LEADS_CSV.open("a", newline="", encoding="utf-8") as f, httpx.Client() as http:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        try:
            for i, lead in enumerate(fresh, 1):
                try:
                    enrich.enrich_one(lead, http, llm, browser)
                except Exception as e:  # one bad site must never kill the run
                    lead["enrich_status"] = f"error: {type(e).__name__}"
                score.apply(lead)
                lead["first_seen"] = today
                w.writerow(_row_for_csv(lead))
                f.flush()
                processed += 1
                passed += int(lead["passes_icp"])
                flag = "PASS" if lead["passes_icp"] else f"drop({lead['icp_reason']})"
                print(f"  [{i}/{len(fresh)}] {lead['firm_name'][:34]:34s} {flag:24s} score={lead['score']} {lead['priority_tier']}")
        finally:
            if browser is not None:
                browser.close()

    print(f"\n== Done ==\n  {passed}/{processed} passed ICP. Written to {config.LEADS_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
