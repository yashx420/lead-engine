"""Client-facing Streamlit UI for the lead engine.

Run locally:   streamlit run lead_engine/app.py
Deploy:        see lead_engine/DEPLOY.md

Configure the ICP + scoring, launch runs (discover / find+verify), browse leads
with the key columns, and download CSV / Excel. Sign-in is currently OFF.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lead_engine import config  # noqa: E402

REQUIRED_KEYS = ("GOOGLE_PLACES_API_KEY", "OPENAI_API_KEY")
OPTIONAL_KEYS = ("HUNTER_API",)
ALL_KEYS = REQUIRED_KEYS + OPTIONAL_KEYS + ("APP_PASSWORD",)


def get_secret(k: str) -> str:
    """Key from env (local .env) or Streamlit secrets (Cloud). Empty string if unset."""
    v = os.environ.get(k, "")
    if v:
        return v
    try:
        if k in st.secrets:                 # type: ignore[operator]
            return str(st.secrets[k])
    except Exception:
        pass
    return ""


# Mirror secrets into os.environ at startup (belt-and-suspenders; launch() also
# injects them explicitly into the subprocess env below).
for _k in ALL_KEYS:
    _v = get_secret(_k)
    if _v and _k not in os.environ:
        os.environ[_k] = _v

RUN_LOG = config.OUT_DIR / "ui_run.log"
ALL_PRACTICE_AREAS = [
    "personal injury", "immigration", "criminal defense", "family law",
    "employment", "medical malpractice", "workers compensation", "bankruptcy",
    "estate planning", "business law", "real estate", "dui",
]
ACCENT = "#5b3df5"

st.set_page_config(page_title="Lead Engine", page_icon="\U0001F4CA", layout="wide",
                   initial_sidebar_state="expanded")

st.markdown(f"""
<style>
  .block-container {{ padding-top: 1.6rem; padding-bottom: 2rem; max-width: 1300px; }}
  #MainMenu, footer {{ visibility: hidden; }}
  .hero {{ background: linear-gradient(110deg, {ACCENT} 0%, #7b5cff 60%, #9b7bff 100%);
           color: #fff; border-radius: 16px; padding: 20px 26px; margin-bottom: 18px;
           box-shadow: 0 6px 24px rgba(91,61,245,.22); }}
  .hero h1 {{ margin: 0; font-size: 26px; font-weight: 800; letter-spacing: -.5px; }}
  .hero p  {{ margin: 4px 0 0; opacity: .92; font-size: 13px; }}
  [data-testid="stMetric"] {{ background:#fff; border:1px solid #ece8ff; border-radius:14px;
           padding:14px 16px 10px; box-shadow:0 1px 4px rgba(20,20,60,.05); }}
  [data-testid="stMetricValue"] {{ font-size:26px; font-weight:800; color:{ACCENT}; }}
  [data-testid="stMetricLabel"] {{ color:#6b7280; font-weight:600; }}
  .stTabs [data-baseweb="tab-list"] {{ gap:4px; }}
  .stTabs [data-baseweb="tab"] {{ font-weight:600; padding:8px 18px; }}
  .stTabs [aria-selected="true"] {{ color:{ACCENT}; }}
  .stButton>button {{ border-radius:10px; font-weight:600; border:1px solid #e6e3f2; }}
  .stDownloadButton>button {{ border-radius:10px; font-weight:600; }}
  div[data-testid="stSidebarUserContent"] h2 {{ color:{ACCENT}; }}
</style>
""", unsafe_allow_html=True)


# --------------------------------------------------------------------------- runs
def run_active() -> bool:
    p = st.session_state.get("proc")
    return p is not None and p.poll() is None


def launch(cmd: list[str], label: str) -> None:
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    for k in ALL_KEYS:                       # ensure the subprocess gets the keys
        v = get_secret(k)
        if v:
            env[k] = v
    log = open(RUN_LOG, "w", encoding="utf-8")
    st.session_state["proc"] = subprocess.Popen(
        [sys.executable, "-m", *cmd], cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env)
    st.session_state["run_label"] = label


def log_tail(n: int = 200) -> str:
    if not RUN_LOG.exists():
        return ""
    return "\n".join(RUN_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])


def run_progress() -> tuple[int, int] | None:
    m = re.findall(r"\[(\d+)/(\d+)\]", log_tail())
    return (int(m[-1][0]), int(m[-1][1])) if m else None


# --------------------------------------------------------------------------- data
@st.cache_data(show_spinner=False)
def load_leads(mtime_key: float, path: str) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str).fillna("")


def leads_path() -> Path | None:
    v = config.OUT_DIR / "leads_verified.csv"
    if v.exists():
        return v
    return config.LEADS_CSV if config.LEADS_CSV.exists() else None


def to_excel(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        df.to_excel(xl, index=False, sheet_name="Leads")
    return buf.getvalue()


# Words that mark where a firm's brand ends and the descriptor begins, e.g.
# "Sandoval & James Car Accident Lawyers" -> brand "Sandoval & James".
FIRM_DESC_KEYWORDS = {
    "law", "lawyer", "lawyers", "attorney", "attorneys", "legal", "firm", "group",
    "office", "offices", "associates", "personal", "injury", "injuries", "accident",
    "accidents", "car", "truck", "criminal", "defense", "family", "immigration",
    "employment", "workers", "compensation", "dui", "dwi", "trial", "justice",
    "advocates", "counsel", "litigation", "disability", "malpractice", "divorce",
    "bankruptcy", "estate", "pllc", "llp",
}
NAME_SUFFIXES = {"jr", "sr", "esq", "esquire", "ii", "iii", "iv", "dr", "mr", "ms", "mrs"}


def split_firm(name: str) -> tuple[str, str]:
    """('XYZ Car Accident Lawyers Austin') -> ('XYZ', 'Car Accident Lawyers Austin')."""
    toks = str(name).split()
    for i, t in enumerate(toks):
        if i > 0 and re.sub(r"[^a-z&]", "", t.lower()) in FIRM_DESC_KEYWORDS:
            return " ".join(toks[:i]).rstrip(",;:- "), " ".join(toks[i:])
    return str(name), ""


def parse_dm(dm: str) -> tuple[str, str, str]:
    """'Jane Smith (Managing Partner)' -> ('Jane', 'Smith', 'Managing Partner')."""
    dm = str(dm)
    mt = re.search(r"\(([^)]*)\)\s*$", dm)
    title = mt.group(1).strip() if mt else ""
    name = re.sub(r"\s*\([^)]*\)\s*$", "", dm).strip()
    parts = [p for p in re.split(r"[\s,]+", name)
             if p and p.strip(".").isalpha() and p.lower().strip(".") not in NAME_SUFFIXES]
    if len(parts) >= 2:
        return parts[0], parts[-1], title
    return (parts[0], "", title) if parts else ("", "", title)


TIER_BADGE = {"A": "🟢 A", "B": "🟡 B", "C": "⚪ C"}
STATUS_BADGE = {
    "valid": "✅ Valid", "risky_catchall": "⚠️ Catch-all", "mx_only": "🔵 MX-only",
    "invalid": "❌ Invalid", "no_email": "— none", "no_mx": "❌ No MX",
    "bad_syntax": "❌ Bad", "no_website": "— no site",
}


# --------------------------------------------------------------------------- panels
def sidebar_settings() -> None:
    st.sidebar.markdown("## ⚙️ ICP & Scoring")
    s = config.current_settings()
    pas = st.sidebar.multiselect("Practice areas", ALL_PRACTICE_AREAS, default=s["practice_areas"])
    cities = st.sidebar.text_area("Cities (one per line)", value="\n".join(s["cities"]), height=130)
    c1, c2 = st.sidebar.columns(2)
    hmin = c1.number_input("Min attorneys", 1, 200, int(s["headcount_min"]))
    hmax = c2.number_input("Max attorneys", 1, 500, int(s["headcount_max"]))

    with st.sidebar.expander("Scoring weights", expanded=False):
        w = s["weights"]
        weights = {
            "google_ads_pixel": st.number_input("Google Ads pixel", 0, 5, int(w.get("google_ads_pixel", 2))),
            "faq_missing": st.number_input("FAQ missing", 0, 5, int(w.get("faq_missing", 1))),
            "schema_missing": st.number_input("Schema missing", 0, 5, int(w.get("schema_missing", 1))),
            "high_rating": st.number_input("High rating", 0, 5, int(w.get("high_rating", 1))),
        }
        rating = st.number_input("High-rating threshold", 1.0, 5.0, float(s["high_rating_threshold"]), 0.1)

    if st.sidebar.button("\U0001F4BE Save settings", use_container_width=True, type="primary"):
        config.save_settings({
            "practice_areas": pas,
            "cities": [c.strip() for c in cities.splitlines() if c.strip()],
            "headcount_min": int(hmin), "headcount_max": int(hmax),
            "weights": weights, "high_rating_threshold": float(rating),
        })
        st.sidebar.success("Saved — next run uses these.")
    st.sidebar.caption(f"{len(pas)} practice areas · {len([c for c in cities.splitlines() if c.strip()])} cities · {hmin}–{hmax} attorneys")


def kpi_row(df: pd.DataFrame | None) -> None:
    cols = st.columns(5)
    if df is None:
        for c, lbl in zip(cols, ["Leads", "ICP-passed", "With email", "Verified", "Tier A"]):
            c.metric(lbl, "—")
        return
    passed = df[df.get("passes_icp", "") == "True"] if "passes_icp" in df else df
    has_email = "email" in df.columns
    cols[0].metric("Leads", len(df))
    cols[1].metric("ICP-passed", len(passed))
    cols[2].metric("With email", int((passed["email"] != "").sum()) if has_email else 0)
    cols[3].metric("Verified", int((passed["email_status"] == "valid").sum()) if has_email else 0)
    cols[4].metric("Tier A", int((passed.get("priority_tier", pd.Series()) == "A").sum()))


def leads_tab(df: pd.DataFrame | None, path: Path | None) -> None:
    if df is None:
        st.info("No leads yet — open the **Run pipeline** tab and click *Discover + score* to build the list.")
        return
    has_contacts = "email" in df.columns

    f1, f2, f3, f4 = st.columns([1.2, 1.4, 1, 1])
    tiers = f1.multiselect("Tier", ["A", "B", "C"], default=["A", "B"])
    pa_opts = sorted({p for cell in df.get("practice_areas", pd.Series([], dtype=str))
                      for p in str(cell).split("; ") if p})
    pa_sel = f2.multiselect("Practice area", pa_opts)
    only_email = f3.checkbox("Has email", value=False)
    only_valid = f4.checkbox("Verified only", value=False)

    view = df.copy()
    if "passes_icp" in view.columns:
        view = view[view["passes_icp"] == "True"]
    if tiers and "priority_tier" in view.columns:
        view = view[view["priority_tier"].isin(tiers)]
    if pa_sel:
        view = view[view["practice_areas"].apply(lambda c: any(p in str(c) for p in pa_sel))]
    if only_email and has_contacts:
        view = view[view["email"] != ""]
    if only_valid and has_contacts:
        view = view[view["email_status"] == "valid"]

    st.caption(f"Showing **{len(view)}** of {len(df)} leads"
               + (f" · updated {time.strftime('%b %d %H:%M', time.localtime(path.stat().st_mtime))}" if path else ""))

    # derive split columns on the raw frame so they appear in the table AND downloads
    view = view.copy()
    fb = view.get("firm_name", pd.Series([""] * len(view))).apply(split_firm)
    view["firm_brand"] = fb.apply(lambda x: x[0])
    view["firm_detail"] = fb.apply(lambda x: x[1])
    if "decision_maker" in view.columns:
        nm = view["decision_maker"].apply(parse_dm)
        view["first_name"] = nm.apply(lambda x: x[0])
        view["last_name"] = nm.apply(lambda x: x[1])
        view["title"] = nm.apply(lambda x: x[2])

    # build a display frame with friendly badges
    disp = pd.DataFrame()
    disp["Firm"] = view["firm_brand"]
    disp["Firm detail"] = view["firm_detail"]
    disp["City"] = view.get("query_city", view.get("state", ""))
    disp["Practice"] = view.get("practice_areas", "")
    disp["Attys"] = pd.to_numeric(view.get("attorney_count", ""), errors="coerce")
    disp["Rating"] = pd.to_numeric(view.get("google_rating", ""), errors="coerce")
    disp["Score"] = pd.to_numeric(view.get("score", ""), errors="coerce")
    disp["Tier"] = (view["priority_tier"].map(lambda t: TIER_BADGE.get(t, t))
                    if "priority_tier" in view.columns else "")
    if has_contacts:
        disp["First"] = view.get("first_name", "")
        disp["Last"] = view.get("last_name", "")
        disp["Title"] = view.get("title", "")
        disp["Email"] = view.get("email", "")
        disp["Status"] = (view["email_status"].map(lambda s: STATUS_BADGE.get(s, s))
                          if "email_status" in view.columns else "")
        disp["Conf"] = pd.to_numeric(view.get("email_confidence", ""), errors="coerce")
        disp["LinkedIn"] = view.get("linkedin", "")
    disp["Phone"] = view.get("phone", "")
    disp["Website"] = view.get("website", "")

    colcfg = {
        "Firm": st.column_config.TextColumn(width="medium"),
        "Attys": st.column_config.NumberColumn(format="%d", help="Attorneys counted on the team page"),
        "Rating": st.column_config.NumberColumn(format="%.1f ⭐"),
        "Score": st.column_config.NumberColumn(format="%d"),
        "Email": st.column_config.TextColumn(width="medium"),
        "Website": st.column_config.LinkColumn(display_text="↗ visit"),
    }
    if has_contacts:
        colcfg["Conf"] = st.column_config.ProgressColumn("Conf", min_value=0, max_value=100, format="%d")
        colcfg["LinkedIn"] = st.column_config.LinkColumn(display_text="in ↗")
    st.dataframe(disp, use_container_width=True, hide_index=True, height=470, column_config=colcfg)

    d1, d2, _ = st.columns([1, 1, 3])
    d1.download_button("⬇️  CSV", view.to_csv(index=False).encode("utf-8"),
                       "leads.csv", "text/csv", use_container_width=True)
    d2.download_button("⬇️  Excel", to_excel(view), "leads.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       use_container_width=True)


def run_tab() -> None:
    missing = [k for k in REQUIRED_KEYS if not get_secret(k)]
    if missing:
        st.error(
            "⚠️ Missing API key(s): **" + ", ".join(missing) + "** — runs are disabled.\n\n"
            "**Streamlit Cloud:** app menu (top-right) → **Settings → Secrets** → add:\n"
            "```toml\nGOOGLE_PLACES_API_KEY = \"…\"\nOPENAI_API_KEY = \"…\"\nHUNTER_API = \"…\"\n```\n"
            "then **Reboot app**.  **Local:** put them in `lead_engine/.env`."
        )
    else:
        detected = [k.replace("_API_KEY", "").replace("_API", "").title() for k in REQUIRED_KEYS + OPTIONAL_KEYS if get_secret(k)]
        st.caption("🔑 Keys detected: " + ", ".join(detected))

    st.markdown("##### Configure this run")
    c1, c2, c3 = st.columns(3)
    limit = c1.number_input("Max firms this run", 5, 5000, 75, step=5)
    use_hunter = c2.checkbox("Use Hunter (spends credits)", value=True)
    passed_only = c3.checkbox("Contacts: ICP-passers only", value=True)

    st.markdown("##### Launch")
    b1, b2, b3 = st.columns(3)
    busy = run_active()
    blocked = busy or bool(missing)
    if b1.button("🔍  Discover + score", disabled=blocked, use_container_width=True, type="primary"):
        launch(["lead_engine.run", "--limit", str(int(limit))], "Discovery + scoring")
        st.rerun()
    if b2.button("📧  Find + verify contacts", disabled=blocked, use_container_width=True, type="primary"):
        cmd = ["lead_engine.verify_contacts", "--limit", str(int(limit))]
        if passed_only:
            cmd.append("--passed-only")
        if not use_hunter:
            cmd.append("--no-hunter")
        launch(cmd, "Find + verify contacts")
        st.rerun()
    if b3.button("⏹  Stop", disabled=not busy, use_container_width=True):
        st.session_state["proc"].terminate()
        st.rerun()

    st.divider()
    if busy:
        prog = run_progress()
        if prog:
            st.progress(prog[0] / prog[1], text=f"{st.session_state.get('run_label','')} — {prog[0]}/{prog[1]}")
        else:
            st.progress(0.0, text=f"{st.session_state.get('run_label','')} — starting…")
        st.code(log_tail(16) or "starting…", language="text")
        time.sleep(2)
        st.rerun()
    elif st.session_state.get("proc") is not None:
        st.success(f"✓ Finished: {st.session_state.get('run_label','')}")
        st.code(log_tail(16), language="text")
    else:
        st.caption("Pick a size, choose whether to use Hunter, then launch. Runs continue in the background — you can browse the Leads tab while one runs.")


# --------------------------------------------------------------------------- main
def main() -> None:
    st.markdown(
        '<div class="hero"><h1>📊 Lead Engine</h1>'
        '<p>US law-firm leads · discover → qualify → find &amp; verify the decision-maker → score → export</p></div>',
        unsafe_allow_html=True)

    sidebar_settings()
    path = leads_path()
    df = load_leads(path.stat().st_mtime, str(path)) if path else None

    kpi_row(df)
    st.write("")
    tab_leads, tab_run = st.tabs(["📋  Leads", "▶️  Run pipeline"])
    with tab_leads:
        leads_tab(df, path)
    with tab_run:
        run_tab()


main()
