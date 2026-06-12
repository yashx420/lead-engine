"""Find + verify contact info for leads already in leads.csv.

Reads output/leads.csv, processes ICP-passers first, and for each lead:
  - scrapes the firm's own pages for an email, picks the best (named > role)
  - verifies it (syntax + MX + best-effort SMTP)  [contact.verify_email]
  - confirms phone present (Google-sourced, trusted) and website live
Writes output/leads_verified.csv incrementally (resumable via place_id).

Usage:
    python -m lead_engine.verify_contacts                 # all leads, passers first
    python -m lead_engine.verify_contacts --passed-only   # only ICP-passing leads
    python -m lead_engine.verify_contacts --limit 20      # cheap test
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

import httpx
import openai
from dotenv import load_dotenv

from . import config, contact, enrich

VERIFIED_CSV = config.OUT_DIR / "leads_verified.csv"
NEW_FIELDS = [
    "email", "email_type", "email_source", "decision_maker", "email_mx",
    "email_smtp", "email_status", "email_confidence", "candidates_tried",
    "phone_present", "website_live",
]


def _load_done() -> set[str]:
    done: set[str] = set()
    if VERIFIED_CSV.exists():
        with VERIFIED_CSV.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done.add(row.get("place_id", ""))
    return done


def main(argv: list[str] | None = None) -> int:
    load_dotenv(config.ENV_FILE, override=True)
    ap = argparse.ArgumentParser(description="Find + verify contact info for existing leads.")
    ap.add_argument("--passed-only", action="store_true", help="only ICP-passing leads")
    ap.add_argument("--limit", type=int, default=None, help="cap leads processed")
    ap.add_argument("--no-browser", action="store_true", help="skip Playwright fallback")
    ap.add_argument("--no-hunter", action="store_true", help="free sources only; never spend a Hunter credit")
    args = ap.parse_args(argv)

    if not config.LEADS_CSV.exists():
        print(f"No leads file at {config.LEADS_CSV}. Run the discovery pipeline first.")
        return 1

    with config.LEADS_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        in_fields = reader.fieldnames or []
        leads = list(reader)

    # ICP-passers first, so the best leads get contacts verified first.
    leads.sort(key=lambda r: r.get("passes_icp") != "True")
    if args.passed_only:
        leads = [r for r in leads if r.get("passes_icp") == "True"]

    done = _load_done()
    todo = [r for r in leads if r.get("place_id", "") not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(leads)} leads, {len(done)} already verified, processing {len(todo)}\n")

    out_fields = list(in_fields) + [c for c in NEW_FIELDS if c not in in_fields]
    VERIFIED_CSV.parent.mkdir(parents=True, exist_ok=True)
    new_file = not VERIFIED_CSV.exists()
    browser = None if args.no_browser else enrich.BrowserFetcher()
    llm = openai.OpenAI()
    hunter_key = None if args.no_hunter else os.environ.get("HUNTER_API")
    print(f"Hunter fallback: {'ON' if hunter_key else 'OFF'}\n")

    n_email = n_valid = n_hunter = 0
    with VERIFIED_CSV.open("a", newline="", encoding="utf-8") as f, httpx.Client() as http:
        w = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        try:
            for i, lead in enumerate(todo, 1):
                website = lead.get("website", "")
                lead["phone_present"] = bool(lead.get("phone"))

                if website:
                    r = contact.find_best_email(website, lead.get("domain", ""), http, llm, browser, hunter_key)
                else:
                    r = contact._result("", "", "", contact._EMPTY_VERIFY)
                lead.update(r)
                w.writerow(lead)
                f.flush()

                email, status = r["email"], r["email_status"]
                n_email += bool(email)
                n_valid += status in ("valid", "risky_catchall", "mx_only")
                n_hunter += r["email_source"] == "hunter"
                tag = f"{status}({r['email_confidence']}) {r['email_source']}" if email else "no_email"
                print(f"  [{i}/{len(todo)}] {lead.get('firm_name','')[:28]:28s} {(email or '-')[:32]:32s} {tag}")
        finally:
            if browser is not None:
                browser.close()

    print(f"\n== Done ==\n  emails found: {n_email}/{len(todo)} | usable (mx+): {n_valid} | "
          f"Hunter credits used: {n_hunter} | -> {VERIFIED_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
