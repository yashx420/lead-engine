"""Backfill the `linkedin` column for leads already in leads_verified.csv.

Cheapest-first: free site scrape (httpx only — fast, no browser) to grab a /in/
URL, then Hunter Email Finder (1 credit) only for the misses. Writes after each
row so it's safe to stop/resume.

Usage:
    python -m lead_engine.backfill_linkedin              # scrape + Hunter fallback
    python -m lead_engine.backfill_linkedin --no-hunter  # free scrape only
    python -m lead_engine.backfill_linkedin --limit 10   # cheap test
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys

import httpx
from dotenv import load_dotenv

from . import config, contact, enrich

VERIFIED = config.OUT_DIR / "leads_verified.csv"


def _write(rows: list[dict], fields: list[str]) -> None:
    with VERIFIED.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def main(argv: list[str] | None = None) -> int:
    load_dotenv(config.ENV_FILE, override=True)
    ap = argparse.ArgumentParser(description="Backfill LinkedIn for existing leads.")
    ap.add_argument("--no-hunter", action="store_true", help="free site scrape only")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)
    hunter_key = None if args.no_hunter else os.environ.get("HUNTER_API")

    if not VERIFIED.exists():
        print(f"No file at {VERIFIED}")
        return 1
    with VERIFIED.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("empty file")
        return 0
    fields = list(rows[0].keys())
    if "linkedin" not in fields:                 # add the column if this file predates it
        idx = fields.index("decision_maker") + 1 if "decision_maker" in fields else len(fields)
        fields[idx:idx] = ["linkedin"]
        for r in rows:
            r.setdefault("linkedin", "")

    todo = [r for r in rows if not r.get("linkedin") and r.get("decision_maker")]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(rows)} leads · {len(todo)} need LinkedIn (have a decision maker) · "
          f"Hunter {'ON' if hunter_key else 'OFF'}\n")

    found = hcredits = 0
    with httpx.Client() as http:
        for i, r in enumerate(todo, 1):
            name = re.sub(r"\s*\([^)]*\)\s*$", "", r.get("decision_maker", "")).strip()
            first, last = contact._name_parts(name)
            domain, website = r.get("domain", ""), r.get("website", "")

            li, src = "", ""
            if website:                          # free pass: httpx-only scrape for /in/ links
                hits: set[str] = set()
                for url in [website] + [website.rstrip("/") + p for p in contact.CONTACT_PATHS]:
                    html = enrich.fetch(url, http)
                    if html:
                        hits |= contact._linkedin_from_html(html)
                    if hits:
                        break
                li = contact._pick_linkedin(hits, first, last)
                src = "scrape" if li else ""
            if not li and hunter_key and domain and (first or last):
                h = contact.hunter_find(domain, first, last, hunter_key)
                hcredits += 1
                if h and h.get("linkedin"):
                    li, src = h["linkedin"], "hunter"

            if li:
                r["linkedin"] = li
                found += 1
                _write(rows, fields)             # save progress after each hit
            print(f"  [{i}/{len(todo)}] {r.get('firm_name','')[:26]:26s} {(li or '-')[:48]:48s} {src}")

    _write(rows, fields)
    print(f"\n== Done ==\n  LinkedIn found: {found}/{len(todo)} | Hunter credits used: {hcredits} | -> {VERIFIED}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
