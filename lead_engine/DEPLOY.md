# Lead Engine UI — Run & Deploy

A Streamlit interface to configure the ICP, run the pipeline, browse leads, and
export CSV/Excel. Your client opens a URL, signs in, and works in the browser.

## Run locally (test it first)

```powershell
cd C:\Users\Yash\Documents\Scrapegraph-ai-main\Scrapegraph-ai-main
pip install -r lead_engine/requirements.txt
streamlit run lead_engine/app.py
```

Opens at http://localhost:8501. Keys are read from `lead_engine/.env`.

> **Sign-in is currently OFF** (no password prompt). Before exposing the hosted URL
> publicly, re-enable a login — easiest is to gate on a shared password in `app.py`,
> or add `streamlit-authenticator` for per-user logins. Until then, keep the URL private.

## What the client can do
- **Sidebar** — edit practice areas, cities, attorney-count bounds, scoring weights → **Save**.
- **Run** — *Discover + score* and *Find + verify contacts*, with a firm cap and a Hunter on/off toggle. Progress streams live; runs continue in the background.
- **Leads** — filter by tier / practice area / has-email / verified, see the key columns, and **Download CSV / Excel**.

## Deploy as a hosted link

You need **two secrets** set as environment variables (or Streamlit secrets): a login
password and the API keys. **Do not ship `.env` to a public host** — set keys as env vars instead.

Required env vars:
```
APP_PASSWORD=...            # the client's login
GOOGLE_PLACES_API_KEY=...
OPENAI_API_KEY=...
HUNTER_API=...
```

### Option A — Streamlit Community Cloud (free, fastest)
1. Push this repo to GitHub (ensure `.env` is git-ignored — it already is).
2. On https://share.streamlit.io → New app → point at `lead_engine/app.py`.
3. In **App settings → Secrets**, paste:
   ```toml
   APP_PASSWORD = "..."
   GOOGLE_PLACES_API_KEY = "..."
   OPENAI_API_KEY = "..."
   HUNTER_API = "..."
   ```
4. Deploy → share the URL. (Note: free tier sleeps when idle and has limited CPU; long runs are slower. Fine for moderate use.)

### Option B — Small VPS (more control, ~$5–7/mo)
On an Ubuntu box (DigitalOcean/Hetzner):
```bash
pip install -r lead_engine/requirements.txt
playwright install chromium
export APP_PASSWORD=... GOOGLE_PLACES_API_KEY=... OPENAI_API_KEY=... HUNTER_API=...
streamlit run lead_engine/app.py --server.port 8501 --server.address 0.0.0.0 --server.headless true
```
Put it behind a reverse proxy (Caddy/Nginx) with HTTPS, and keep it alive with
`systemd` or `tmux`. Browser fallback needs `playwright install chromium` on the server.

## Notes
- The password gate is a single shared password — fine for one client. For multiple
  users with separate logins, add `streamlit-authenticator` later.
- Output (`leads.csv`, `leads_verified.csv`, `settings.json`) lives in `lead_engine/output/`.
  On a host with ephemeral disk, mount a volume or sync exports off-box if you need them to persist.
