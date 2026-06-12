"""Central config for the lead engine. Edit the lists here to steer a run.

Nothing is hardcoded into the logic — discovery iterates PRACTICE_AREAS x CITIES,
the ICP gate uses ICP_PRACTICE_AREAS + HEADCOUNT bounds, and scoring uses WEIGHTS.
"""

from __future__ import annotations

import json
from pathlib import Path

# Model used for the AI gate + extraction. gpt-4o-mini = cheapest capable model
# with reliable JSON structured output. ~$0.15 / $0.60 per 1M tokens.
MODEL = "gpt-4o-mini"

# --- ICP definition -------------------------------------------------------
# 5-50 lawyer US firms in these practice areas.
ICP_PRACTICE_AREAS = [
    "personal injury",
    "immigration",
    "criminal defense",
    "family law",
    "employment",
]
HEADCOUNT_MIN = 2   # PI/criminal/family firms cluster at 2-4 attorneys; 1 = solo (drop)
HEADCOUNT_MAX = 50

# --- Discovery surface ----------------------------------------------------
# Each (practice_area, city) becomes a Google Places text query:
#   "<practice_area> lawyer in <city>"
# Start small to validate quality, then widen the city list.
PRACTICE_AREAS = ICP_PRACTICE_AREAS
CITIES = [
    "Austin, TX",
    "Phoenix, AZ",
    "Denver, CO",
    "Tampa, FL",
    "Charlotte, NC",
]

# --- Scoring model (V1 detectable subset of the original scope) -----------
# Signals we can detect for free from the website + Places data. Perplexity-based
# "invisible in AI search" and job-board "hiring" signals are out of V1.
WEIGHTS = {
    "google_ads_pixel": 2,   # ad spend = they care about acquisition
    "faq_missing": 1,        # AEO gap
    "schema_missing": 1,     # AEO gap
    "high_rating": 1,        # Google rating >= 4.5 (proxy for Avvo rating)
}
HIGH_RATING_THRESHOLD = 4.5

# Priority tiers from the original scope.
def priority_tier(score: int) -> str:
    if score >= 4:
        return "A"   # push first
    if score >= 2:
        return "B"   # push after A
    return "C"       # store, don't push yet

# --- Paths ----------------------------------------------------------------
ENV_FILE = Path(__file__).parent / ".env"      # real secrets, loaded regardless of cwd
OUT_DIR = Path(__file__).parent / "output"
LEADS_CSV = OUT_DIR / "leads.csv"
SETTINGS_FILE = OUT_DIR / "settings.json"       # UI-editable overrides for the values above


# --- UI-editable settings overlay -----------------------------------------
# The constants above are defaults. If output/settings.json exists (written by the
# UI), its values override them — so both the CLI and the UI read one source of truth.
def current_settings() -> dict:
    return {
        "practice_areas": ICP_PRACTICE_AREAS,
        "cities": CITIES,
        "headcount_min": HEADCOUNT_MIN,
        "headcount_max": HEADCOUNT_MAX,
        "weights": WEIGHTS,
        "high_rating_threshold": HIGH_RATING_THRESHOLD,
    }


def save_settings(d: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


def _apply_overrides() -> None:
    global ICP_PRACTICE_AREAS, PRACTICE_AREAS, HEADCOUNT_MIN, HEADCOUNT_MAX
    global CITIES, WEIGHTS, HIGH_RATING_THRESHOLD
    if not SETTINGS_FILE.exists():
        return
    try:
        d = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    ICP_PRACTICE_AREAS = d.get("practice_areas") or ICP_PRACTICE_AREAS
    PRACTICE_AREAS = ICP_PRACTICE_AREAS          # discovery searches the ICP areas
    CITIES = d.get("cities") or CITIES
    HEADCOUNT_MIN = d.get("headcount_min", HEADCOUNT_MIN)
    HEADCOUNT_MAX = d.get("headcount_max", HEADCOUNT_MAX)
    WEIGHTS = {**WEIGHTS, **(d.get("weights") or {})}
    HIGH_RATING_THRESHOLD = d.get("high_rating_threshold", HIGH_RATING_THRESHOLD)


_apply_overrides()
