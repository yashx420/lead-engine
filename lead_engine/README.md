# Lead Engine — V1

Local law-firm lead intelligence. Discovers ICP-matching US law firms via the
Google Places API, enriches each from its website, gates + scores with OpenAI
gpt-4o-mini, and writes `output/leads.csv`. No email finding in V1.

**ICP:** 5–50 lawyer US firms in personal injury, immigration, criminal defense,
family law, or employment.

## Pipeline

```
discover  Google Places text search  (PRACTICE_AREAS x CITIES)  -> candidate firms w/ website
dedup     skip anything already in leads.csv (by place_id + domain)
enrich    fetch site -> free signal regex (FAQ / schema / Google Ads pixel / CTA)
          + gpt-4o-mini reads homepage + team page: is_law_firm, practice_areas, attorney_count
score     hard ICP gate, then weighted score -> priority tier A/B/C
```

Why the LLM is called directly (not through ScrapeGraph's LLM wrapper): ScrapeGraph's
internal model-token registry lags newer model IDs, so we fetch + clean HTML
ourselves and call the `openai` SDK to stay on the cheapest current model.

## Setup

```powershell
cd lead_engine
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # then fill in both keys
```

Enable **"Places API (New)"** in Google Cloud and create an API key for `GOOGLE_PLACES_API_KEY`.

## Run

```powershell
python -m lead_engine.run --discover-only       # free: just list candidates
python -m lead_engine.run --limit 10            # cheap test: enrich 10 firms
python -m lead_engine.run                        # full run -> output/leads.csv
```

Re-running is safe — dedup means each firm is processed once. Run it on a 3-day
cron to mimic the original scope's cadence.

## Tuning

All in `config.py`: `CITIES`, `PRACTICE_AREAS`, `WEIGHTS`, headcount bounds, model.
Widen `CITIES` once you're happy with lead quality on the first few.

## Cost (≈2–3k leads/month)

Google Places: **$0** (within the $200/mo free credit). gpt-4o-mini enrichment:
**under $10**. Each firm is ~1 short LLM call. Drop cost further with the
OpenAI **Batch API** (50% off) — a natural V2 once volume is steady.

## Not in V1 (deliberate)

Email finding, MillionVerifier, Perplexity "AI visibility" signal, job-board
"hiring" intent, campaign push (ManyReach/Aimfox), the Karpathy learning loop.
The CSV schema already carries the columns those steps will fill.
