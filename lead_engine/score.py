"""ICP gate + weighted scoring. Pure functions, no I/O — easy to test/tune."""

from __future__ import annotations

from . import config


def passes_icp(lead: dict) -> tuple[bool, str]:
    """Hard gate. Returns (passes, reason_if_not)."""
    if lead.get("enrich_status") != "ok":
        return False, lead.get("enrich_status", "not_enriched")
    if not lead.get("is_law_firm"):
        return False, "not_a_law_firm"

    pas = {p.lower() for p in lead.get("practice_areas", [])}
    if not pas & set(config.ICP_PRACTICE_AREAS):
        return False, "practice_area_mismatch"

    count = lead.get("attorney_count")
    if count is not None and not (config.HEADCOUNT_MIN <= count <= config.HEADCOUNT_MAX):
        return False, f"headcount_out_of_range({count})"

    return True, ""


def score(lead: dict) -> int:
    w = config.WEIGHTS
    s = 0
    if lead.get("google_ads_pixel"):
        s += w["google_ads_pixel"]
    if not lead.get("faq_present"):
        s += w["faq_missing"]
    if not lead.get("schema_present"):
        s += w["schema_missing"]
    rating = lead.get("google_rating")
    if rating is not None and rating >= config.HIGH_RATING_THRESHOLD:
        s += w["high_rating"]
    return s


def apply(lead: dict) -> dict:
    ok, reason = passes_icp(lead)
    lead["passes_icp"] = ok
    lead["icp_reason"] = reason
    if ok:
        lead["score"] = score(lead)
        lead["priority_tier"] = config.priority_tier(lead["score"])
    else:
        lead["score"] = 0
        lead["priority_tier"] = ""
    return lead
