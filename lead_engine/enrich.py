"""Enrichment: fetch the firm's site, detect AEO signals for free (regex), and
use Claude Haiku to read the page text for the things that need judgment
(is it really a law firm, which ICP practice areas, how many attorneys).

We fetch + clean the HTML ourselves and call the LLM directly rather than going
through ScrapeGraph's LLM wrapper, because ScrapeGraph's internal model-token
registry lags newer model IDs. This keeps us on the cheapest current model.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

import httpx
import openai
from bs4 import BeautifulSoup
from pydantic import BaseModel

from . import config

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Hints for locating an "Our Attorneys / Team" page from homepage links.
TEAM_HINTS = ("attorney", "lawyer", "our-team", "our-people", "professionals", "meet-the", "staff")

# Fallback paths to probe if no team link is found on the homepage.
COMMON_TEAM_PATHS = (
    "/attorneys", "/our-attorneys", "/our-team", "/team", "/lawyers",
    "/our-lawyers", "/attorneys-staff", "/meet-the-team", "/about/attorneys",
)


# --- free signal detection (no LLM) --------------------------------------
def detect_signals(html: str) -> dict:
    low = html.lower()
    return {
        "faq_present": bool(
            re.search(r'"@type"\s*:\s*"faqpage"', low) or re.search(r"\bfaq\b", low)
        ),
        "schema_present": "application/ld+json" in low or "schema.org" in low,
        "google_ads_pixel": bool(
            re.search(r"aw-\d{6,}", low)            # gtag AW- conversion id
            or "googleadservices.com" in low
            or "googleads.g.doubleclick.net" in low
        ),
        "book_call_cta": bool(
            re.search(r"book a call|schedule a (free )?consultation|free consultation|get started", low)
        ),
    }


def _clean_text(html: str, limit: int = 6000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return text[:limit]


def _find_team_url(html: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        raw = str(a.get("href") or "")
        if "@" in raw or raw.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
            continue  # email/phone/anchor links, not a team page
        href = raw.lower()
        label = a.get_text(" ").lower()
        if any(h in href or h in label for h in TEAM_HINTS):
            resolved = urljoin(base_url, raw)
            if resolved.lower().startswith(("http://", "https://")):
                return resolved
    return None


def fetch(url: str, client: httpx.Client) -> str | None:
    """Fast path: plain HTTP GET. Returns None on non-200 (e.g. Cloudflare 403)."""
    if not url.lower().startswith(("http://", "https://")):
        return None  # skip mailto:, tel:, scheme-less, javascript: etc.
    try:
        r = client.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=20)
        if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
            return r.text
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, UnicodeError):
        return None
    return None


class BrowserFetcher:
    """Lazy, reusable headless Chromium for sites that block httpx or render via JS.

    Launches one browser on first use; each get() runs in a fresh context.
    Many law-firm sites sit behind Cloudflare or are JS-rendered — a real browser
    clears most basic challenges and runs the JS that plain httpx can't.
    """

    def __init__(self) -> None:
        self._pw = None
        self._browser = None

    def _ensure(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        return self._browser

    def get(self, url: str) -> str | None:
        try:
            browser = self._ensure()
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 768})
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(2500)  # let Cloudflare / JS settle
                html = page.content()
            finally:
                ctx.close()
            return html
        except Exception:
            return None

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


def robust_fetch(url: str, http: httpx.Client, browser: "BrowserFetcher | None", min_len: int = 2000) -> str | None:
    """httpx first; fall back to a real browser if blocked (None) or thin (JS shell)."""
    html = fetch(url, http)
    if html and len(html) >= min_len:
        return html
    if browser is not None:
        rendered = browser.get(url)
        if rendered:
            return rendered
    return html  # may be None or thin; caller treats None as fetch_failed


# --- LLM extraction (Haiku) ----------------------------------------------
class FirmProfile(BaseModel):
    is_law_firm: bool
    practice_areas: list[str]   # canonical ICP labels or "other"
    attorney_count: int | None  # best estimate from the team page, null if unknown
    state: str | None


_EXTRACT_SYSTEM = (
    "You analyze US law firm websites for B2B lead qualification. "
    "Return strict structured data. For practice_areas, map to this canonical set "
    f"and use only these labels (plus 'other'): {config.ICP_PRACTICE_AREAS}. "
    "For attorney_count, count distinct attorneys/lawyers named on the team page; "
    "if you genuinely cannot tell, use null — do not guess a round number."
)


def extract_profile(client: openai.OpenAI, firm_name: str, home_text: str, team_text: str) -> FirmProfile:
    user = (
        f"Firm name: {firm_name}\n\n"
        f"--- HOMEPAGE TEXT ---\n{home_text}\n\n"
        f"--- TEAM/ATTORNEYS PAGE TEXT ---\n{team_text or '(not found)'}"
    )
    completion = client.beta.chat.completions.parse(
        model=config.MODEL,
        max_tokens=512,
        messages=[
            {"role": "system", "content": _EXTRACT_SYSTEM},
            {"role": "user", "content": user},
        ],
        response_format=FirmProfile,
    )
    profile = completion.choices[0].message.parsed
    if profile is None:  # refusal / no structured output
        raise RuntimeError("model returned no parsed output")
    return profile


def enrich_one(lead: dict, http: httpx.Client, llm: openai.OpenAI, browser: "BrowserFetcher | None" = None) -> dict:
    """Mutates `lead` in place with signals + extracted profile. Returns it."""
    home_html = robust_fetch(lead["website"], http, browser)
    if not home_html:
        lead["enrich_status"] = "fetch_failed"
        return lead

    lead.update(detect_signals(home_html))

    # Find the team/attorneys page: homepage link first, then probe common paths.
    team_html = None
    team_url = _find_team_url(home_html, lead["website"])
    if team_url:
        team_html = robust_fetch(team_url, http, browser)
    if not team_html:
        base = lead["website"].rstrip("/")
        for path in COMMON_TEAM_PATHS:
            team_html = fetch(base + path, http)  # httpx only — cheap probe of many paths
            if team_html:
                break
    lead["team_page_found"] = bool(team_html)

    try:
        profile = extract_profile(
            llm,
            lead["firm_name"],
            _clean_text(home_html),
            _clean_text(team_html) if team_html else "",
        )
        lead["is_law_firm"] = profile.is_law_firm
        lead["practice_areas"] = profile.practice_areas
        # Trust the count only if we actually read a team page; otherwise the
        # homepage names too few attorneys and would wrongly drop good firms.
        lead["attorney_count"] = profile.attorney_count if team_html else None
        lead["state"] = profile.state
        lead["enrich_status"] = "ok"
    except (openai.APIError, RuntimeError) as e:
        lead["enrich_status"] = f"llm_error: {type(e).__name__}"
    return lead
