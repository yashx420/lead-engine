# Lead Engine

Self-serve lead intelligence for **US law firms** — discover firms, qualify them
against an ICP, find & verify the decision-maker's email, score, and export.
Streamlit UI + a Python pipeline (Google Places, OpenAI gpt-4o-mini, Playwright,
free SMTP verification, Hunter fallback).

## Quick start (local)

```bash
pip install -r requirements.txt
playwright install chromium
cp lead_engine/.env.example lead_engine/.env   # fill in your keys
streamlit run lead_engine/app.py
```

Opens at http://localhost:8501.

## What it does

| Stage | Tool | Output |
|---|---|---|
| Discover | Google Places API | candidate firms (name, site, phone, rating) |
| Enrich | Playwright + httpx | fetched site (handles Cloudflare/JS) |
| Qualify | OpenAI gpt-4o-mini | ICP gate, attorney count, decision-maker |
| Find email | scrape → pattern-guess → Hunter | decision-maker address |
| Verify | SMTP + MX (free) | deliverability status |
| Score | weighted signals | Tier A / B / C |

Output lands in `lead_engine/output/leads.csv` and `leads_verified.csv`, browsable
and downloadable (CSV/Excel) in the UI.

## Configuration

Editable from the UI sidebar (saved to `lead_engine/output/settings.json`), or in
`lead_engine/config.py`: practice areas, cities, attorney-count bounds, scoring weights.

## Deploy

See [`lead_engine/DEPLOY.md`](lead_engine/DEPLOY.md). Recommended host:
**Streamlit Community Cloud** (free, deploys from this repo) or a small VPS for
heavier scraping. API keys go in the host's secrets/env — never commit `.env`.

## CLI

```bash
python -m lead_engine.run                 # discover + qualify + score
python -m lead_engine.verify_contacts     # find + verify decision-maker emails
```
