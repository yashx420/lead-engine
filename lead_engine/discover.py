"""Discovery: Google Places API (New) text search -> candidate firms.

This is the half that decides lead quality. Places gives us name, website,
phone, rating and rating count reliably and legally. We filter to firms that
have a website (no site = can't enrich or run AEO offer).
"""

from __future__ import annotations

import os
import time
from urllib.parse import urlparse

import httpx

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.websiteUri",
        "places.nationalPhoneNumber",
        "places.rating",
        "places.userRatingCount",
        "places.formattedAddress",
        "places.businessStatus",
        "nextPageToken",
    ]
)


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc or None


def search(practice_area: str, city: str, max_results: int = 60) -> list[dict]:
    """Text-search one practice_area x city. Paginates up to ~60 results."""
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GOOGLE_PLACES_API_KEY is not set. On Streamlit Cloud add it under "
            "Settings -> Secrets and reboot; locally put it in lead_engine/.env."
        )
    query = f"{practice_area} lawyer in {city}"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    results: list[dict] = []
    page_token: str | None = None

    with httpx.Client(timeout=30) as client:
        while len(results) < max_results:
            body: dict = {"textQuery": query, "regionCode": "US"}
            if page_token:
                body["pageToken"] = page_token
            resp = client.post(PLACES_URL, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

            for p in data.get("places", []):
                if p.get("businessStatus") not in (None, "OPERATIONAL"):
                    continue
                website = p.get("websiteUri")
                if not website:  # no site -> cannot enrich or run AEO offer
                    continue
                results.append(
                    {
                        "place_id": p["id"],
                        "firm_name": p.get("displayName", {}).get("text", ""),
                        "website": website,
                        "domain": _domain(website),
                        "phone": p.get("nationalPhoneNumber", ""),
                        "google_rating": p.get("rating"),
                        "rating_count": p.get("userRatingCount", 0),
                        "address": p.get("formattedAddress", ""),
                        "query_practice_area": practice_area,
                        "query_city": city,
                        "source": "google_places",
                    }
                )

            page_token = data.get("nextPageToken")
            if not page_token:
                break
            time.sleep(2)  # nextPageToken needs a moment to become valid

    return results


def discover(practice_areas: list[str], cities: list[str]) -> list[dict]:
    """Sweep all practice_area x city pairs, dedup by place_id."""
    seen: set[str] = set()
    out: list[dict] = []
    for pa in practice_areas:
        for city in cities:
            try:
                rows = search(pa, city)
            except httpx.HTTPStatusError as e:
                print(f"  ! Places error for '{pa} / {city}': {e.response.status_code} {e.response.text[:200]}")
                continue
            new = [r for r in rows if r["place_id"] not in seen]
            seen.update(r["place_id"] for r in new)
            out.extend(new)
            print(f"  {pa:18s} | {city:16s} -> {len(rows):3d} hits ({len(new)} new)")
    return out
