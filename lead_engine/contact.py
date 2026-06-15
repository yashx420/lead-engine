"""Contact discovery + verification.

Email finding is scrape-first (free): pull addresses off the firm's own pages.
Verification is layered, cheapest-first:
  1. syntax   - well-formed address
  2. MX       - the firm domain actually accepts mail (DNS, always works)
  3. SMTP     - best-effort mailbox probe + catch-all detection (often blocked
                by ISP port-25 filtering or defeated by accept-all mail hosts)
A paid verifier (MillionVerifier etc.) can later replace step 3 via verify_email's
`paid_check` hook for true mailbox-level confidence.
"""

from __future__ import annotations

import random
import re
import smtplib
import socket

import dns.resolver
import httpx
import openai
from pydantic import BaseModel

from . import config, enrich

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
CFEMAIL_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')
LINKEDIN_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+", re.I)

# Role/shared mailboxes — usable but not a named decision maker.
ROLE_PREFIXES = {
    "info", "contact", "office", "admin", "hello", "intake", "reception", "mail",
    "support", "team", "frontdesk", "help", "inquiries", "inquiry", "newclients",
    "casemanager", "legal", "law", "attorney", "attorneys", "service", "services",
}

# Third-party domains that show up in page source but aren't the firm's mail.
JUNK_EMAIL_DOMAINS = ("sentry.io", "wixpress.com", "example.com", "domain.com", "email.com")

CONTACT_PATHS = ("/contact", "/contact-us", "/contactus", "/attorneys", "/our-team", "/about")


def classify(email: str) -> str:
    """'personal' (named decision maker) vs 'role' (shared inbox)."""
    local = email.split("@")[0].lower()
    base = re.split(r"[._\-]", local)[0]
    return "role" if (local in ROLE_PREFIXES or base in ROLE_PREFIXES) else "personal"


def _decode_cfemail(hexstr: str) -> str | None:
    """Decode a Cloudflare-obfuscated email (data-cfemail hex blob)."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except (ValueError, IndexError):
        return None


def _deobfuscate(html: str) -> str:
    """Turn ' [at] ' / ' (dot) ' style obfuscation back into a real address."""
    s = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", html, flags=re.I)
    s = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", s, flags=re.I)
    return s


def _emails_from_html(html: str, domain: str) -> set[str]:
    out: set[str] = set()
    candidates = list(EMAIL_RE.findall(_deobfuscate(html)))
    for hexblob in CFEMAIL_RE.findall(html):  # Cloudflare-protected addresses
        decoded = _decode_cfemail(hexblob)
        if decoded and EMAIL_RE.fullmatch(decoded):
            candidates.append(decoded)
    for raw in candidates:
        email = raw.lower().rstrip(".")
        edom = email.split("@")[1]
        if edom.startswith("www."):
            edom = edom[4:]
            email = email.split("@")[0] + "@" + edom
        if any(j in edom for j in JUNK_EMAIL_DOMAINS):
            continue
        if domain and not edom.endswith(domain):  # keep only the firm's own domain
            continue
        out.add(email)
    return out


def _linkedin_from_html(html: str) -> set[str]:
    """Personal LinkedIn profile URLs (/in/...) found on the page."""
    return {m.split("?")[0].rstrip("/") for m in LINKEDIN_RE.findall(html)}


def _pick_linkedin(found: set[str], first: str, last: str, hunter_url: str = "") -> str:
    """Best LinkedIn for the decision maker: Hunter's > name-matched scrape > sole scrape."""
    if hunter_url:
        return hunter_url
    if not found:
        return ""
    for url in found:
        slug = url.rsplit("/in/", 1)[-1].lower()
        if (last and last in slug) or (first and len(first) > 2 and first in slug):
            return url
    return next(iter(found)) if len(found) == 1 else ""  # blank if ambiguous


def gather_emails(website: str, domain: str, http: httpx.Client, browser=None) -> list[str]:
    """Collect on-domain emails from homepage + contact/attorney pages.

    Uses the browser fallback on every page (law firms are heavily Cloudflare'd),
    but short-circuits as soon as a named (personal) address is found.
    """
    found: set[str] = set()
    pages = [website] + [website.rstrip("/") + p for p in CONTACT_PATHS]
    for url in pages:
        html = enrich.robust_fetch(url, http, browser)
        if not html:
            continue
        found |= _emails_from_html(html, domain)
        if any(classify(e) == "personal" for e in found):
            break  # got a decision-maker address; stop fetching more pages
    return sorted(found)


def best_email(emails: list[str]) -> tuple[str | None, str]:
    """Prefer a named (personal) mailbox over a role inbox."""
    personal = [e for e in emails if classify(e) == "personal"]
    if personal:
        return personal[0], "personal"
    if emails:
        return emails[0], "role"
    return None, ""


# --- verification ---------------------------------------------------------
def _mx_hosts(domain: str) -> list[str]:
    try:
        ans = dns.resolver.resolve(domain, "MX", lifetime=8)
        ranked = sorted((r.preference, str(r.exchange).rstrip(".")) for r in ans)
        return [host for _, host in ranked]
    except Exception:
        return []


def _smtp_probe(email: str, mx_host: str) -> str:
    """'deliverable' | 'undeliverable' | 'catch_all' | 'unknown' (blocked/timeout)."""
    domain = email.split("@")[1]
    try:
        server = smtplib.SMTP(timeout=10)
        server.connect(mx_host)
        server.helo("mail.example.com")
        server.mail("verify@example.com")
        code, _ = server.rcpt(email)
        rand = f"no-such-user-{random.randint(100000, 999999)}@{domain}"
        rand_code, _ = server.rcpt(rand)
        server.quit()
        if rand_code in (250, 251):      # server accepts anything -> can't confirm
            return "catch_all"
        if code in (250, 251):
            return "deliverable"
        if code in (550, 551, 553, 554):
            return "undeliverable"
        return "unknown"
    except (socket.error, smtplib.SMTPException, OSError):
        return "unknown"  # most commonly: ISP blocks outbound port 25


def verify_email(email: str | None, paid_check=None) -> dict:
    """Return verification result with a 0-100 confidence and a status label."""
    res = {"email": email or "", "syntax": False, "mx": False,
           "smtp": "", "status": "no_email", "confidence": 0}
    if not email:
        return res
    if not EMAIL_RE.fullmatch(email):
        res["status"] = "bad_syntax"
        return res
    res["syntax"] = True

    if paid_check is not None:               # hook for MillionVerifier/ZeroBounce
        return paid_check(email)

    domain = email.split("@")[1]
    if not _mx_hosts(domain):
        res["status"] = "no_mx"              # domain can't receive mail
        return res
    res["mx"] = True

    smtp = _smtp_probe(email, _mx_hosts(domain)[0])
    res["smtp"] = smtp
    res["status"], res["confidence"] = {
        "deliverable": ("valid", 95),
        "catch_all": ("risky_catchall", 60),
        "undeliverable": ("invalid", 5),
        "unknown": ("mx_only", 50),          # syntax + MX good, mailbox unconfirmed
    }[smtp]
    return res


# --- finder: names -> pattern-guess -> verify -----------------------------
TITLE_SUFFIXES = {"jr", "sr", "esq", "esquire", "ii", "iii", "iv", "dr", "mr", "ms", "mrs", "attorney"}
DM_KEYWORDS = ("managing", "founding", "founder", "owner", "principal", "name partner", "senior partner", "president")


class Attorney(BaseModel):
    name: str
    title: str
    is_decision_maker: bool  # managing/founding/name partner/owner


class Roster(BaseModel):
    attorneys: list[Attorney]


def extract_people(llm: openai.OpenAI, text: str) -> list[Attorney]:
    """LLM pulls attorney names + titles from team-page text; flags the decision maker."""
    if not text.strip():
        return []
    try:
        completion = llm.beta.chat.completions.parse(
            model=config.MODEL,
            max_tokens=800,
            messages=[
                {"role": "system", "content":
                 "Extract attorneys/lawyers named on this US law firm page. For each give name "
                 "and title. Set is_decision_maker=true for the managing/founding/name partner, "
                 "owner, or principal (the person who decides on marketing vendors). Names only — "
                 "no staff, paralegals, or generic text."},
                {"role": "user", "content": text[:8000]},
            ],
            response_format=Roster,
        )
        parsed = completion.choices[0].message.parsed
        return parsed.attorneys if parsed else []
    except (openai.APIError, RuntimeError):
        return []


def _name_parts(name: str) -> tuple[str, str]:
    toks = [t.lower() for t in re.split(r"[\s,]+", name.strip()) if t.isalpha()]
    toks = [t for t in toks if t not in TITLE_SUFFIXES]
    if len(toks) >= 2:
        return toks[0], toks[-1]
    return (toks[0], "") if toks else ("", "")


def _patterns(first: str, last: str) -> dict[str, str]:
    f, l = first, last
    fi, li = (f[:1] if f else ""), (l[:1] if l else "")
    return {
        "first.last": f"{f}.{l}", "flast": f"{fi}{l}", "first": f, "firstlast": f"{f}{l}",
        "f.last": f"{fi}.{l}", "first_last": f"{f}_{l}", "firstl": f"{f}{li}",
        "last": l, "last.first": f"{l}.{f}", "lastfirst": f"{l}{f}",
    }


def _infer_pattern(emails: set[str], people: list[Attorney]) -> str | None:
    """If a known email's local part matches a known attorney name, deduce the firm's pattern."""
    for email in emails:
        local = email.split("@")[0].lower()
        for person in people:
            first, last = _name_parts(person.name)
            if not (first and last):
                continue
            for pid, val in _patterns(first, last).items():
                if val and val == local:
                    return pid
    return None


def _candidates(first: str, last: str, domain: str, pattern: str | None) -> list[str]:
    if not (first and domain):
        return []
    pats = _patterns(first, last)
    order = [pattern] if pattern and pattern in pats else []
    order += ["first.last", "flast", "first", "firstlast", "f.last"]  # most common firm patterns
    seen, out = set(), []
    for pid in order:
        local = pats.get(pid, "")
        if local and local not in seen:
            seen.add(local)
            out.append(f"{local}@{domain}")
    return out[:6]  # cap SMTP probes per firm


def _pick_decision_maker(people: list[Attorney]) -> Attorney | None:
    for p in people:
        if p.is_decision_maker:
            return p
    for p in people:  # fall back to a title that smells senior
        if any(k in p.title.lower() for k in DM_KEYWORDS):
            return p
    return people[0] if people else None


def _result(email: str, etype: str, source: str, verify: dict, dm: str = "",
            cands: str = "", website_live: bool = False, linkedin: str = "") -> dict:
    """Flatten an email choice + its verification into one scalar dict for CSV."""
    return {
        "email": email, "email_type": etype, "email_source": source,
        "decision_maker": dm, "linkedin": linkedin, "candidates_tried": cands,
        "website_live": website_live,
        "email_mx": verify["mx"], "email_smtp": verify["smtp"],
        "email_status": verify["status"], "email_confidence": verify["confidence"],
    }


_EMPTY_VERIFY = {"mx": False, "smtp": "", "status": "no_email", "confidence": 0}


def hunter_find(domain: str, first: str, last: str, api_key: str) -> dict | None:
    """Hunter Email Finder: domain + name -> verified email. Costs 1 search credit.

    Returns a verify-shaped dict {email, mx, smtp, status, confidence} or None.
    """
    try:
        r = httpx.get(
            "https://api.hunter.io/v2/email-finder",
            params={"domain": domain, "first_name": first, "last_name": last, "api_key": api_key},
            timeout=30,
        )
        if r.status_code != 200:
            return None
        data = r.json().get("data") or {}
    except (httpx.HTTPError, ValueError):
        return None
    email = data.get("email")
    if not email:
        return None
    score = data.get("score") or 0
    vstat = ((data.get("verification") or {}).get("status")) or ""
    if vstat == "valid" or score >= 90:
        status = "valid"
    elif vstat == "accept_all":
        status = "risky_catchall"
    else:
        status = "mx_only"
    return {"email": email.lower(), "mx": True, "smtp": f"hunter:{vstat or score}",
            "status": status, "confidence": score,
            "linkedin": data.get("linkedin_url") or data.get("linkedin") or ""}


def find_best_email(website: str, domain: str, http: httpx.Client, llm: openai.OpenAI,
                    browser=None, hunter_key: str | None = None) -> dict:
    """Best email for a firm, cheapest-source-first.

    Order: published named (free) > pattern-guess+SMTP (free) > Hunter (1 credit, only if
    nothing free confirmed) > best unconfirmed fallback > published role inbox.
    Returns a flat dict (see _result) ready to merge into a CSV row.
    """
    # Scrape homepage + contact/attorney pages: collect emails, LinkedIn URLs, page text.
    emails: set[str] = set()
    linkedins: set[str] = set()
    texts: list[str] = []
    reachable = False
    for url in [website] + [website.rstrip("/") + p for p in CONTACT_PATHS]:
        html = enrich.robust_fetch(url, http, browser)
        if not html:
            continue
        reachable = True
        emails |= _emails_from_html(html, domain)
        linkedins |= _linkedin_from_html(html)
        texts.append(enrich._clean_text(html))
        if any(classify(e) == "personal" for e in emails):
            break

    def done(email="", etype="", source="", verify=_EMPTY_VERIFY, dm="", cands=""):
        li = _pick_linkedin(linkedins, first, last, str(verify.get("linkedin", "") or ""))
        return _result(email, etype, source, verify, dm, cands, website_live=reachable, linkedin=li)

    pub, ptype = best_email(sorted(emails))
    people = extract_people(llm, " ".join(texts))
    dm = _pick_decision_maker(people)
    dm_label = f"{dm.name} ({dm.title})" if dm else ""
    first, last = _name_parts(dm.name) if dm else ("", "")

    # best non-confirmed free option seen so far: (email, type, source, verify)
    fallback: tuple | None = None

    # 1. Published named email — free, verify it.
    if pub and ptype == "personal":
        v = verify_email(pub)
        if v["status"] == "valid":
            return done(pub, "personal", "published", v, dm_label)
        fallback = (pub, "personal", "published", v)

    # 2. Pattern-guess the decision maker, SMTP-verify — free.
    cands_str = ""
    if dm and first:
        pattern = _infer_pattern(emails, people)
        cands = _candidates(first, last, domain, pattern)
        cands_str = "; ".join(cands)
        for cand in cands:
            v = verify_email(cand)
            if v["status"] == "valid":
                return done(cand, "personal", f"pattern:{pattern or 'common'}", v, dm_label, cands_str)
            if fallback is None and v["status"] in ("risky_catchall", "mx_only"):
                fallback = (cand, "personal", "guess_unconfirmed", v)

    # 3. Hunter — paid, only reached when nothing free was confirmed valid.
    if hunter_key and domain and (first or last):
        h = hunter_find(domain, first, last, hunter_key)
        if h:
            if h["status"] == "valid":
                return done(h["email"], "personal", "hunter", h, dm_label, cands_str)
            # keep Hunter's result if it beats our free fallback's confidence
            if fallback is None or h["confidence"] > fallback[3]["confidence"]:
                fallback = (h["email"], "personal", "hunter", h)

    # 4. Settle: best unconfirmed option, else a published role inbox, else nothing.
    if fallback:
        return done(fallback[0], fallback[1], fallback[2], fallback[3], dm_label, cands_str)
    if pub:
        return done(pub, "role", "published", verify_email(pub), dm_label, cands_str)
    return done(dm=dm_label, cands=cands_str)
