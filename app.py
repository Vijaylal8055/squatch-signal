import copy
import csv
import html as _html
import io
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import streamlit as st
from dotenv import load_dotenv
from lead_discovery import clear_cache, discover_urls
from local_db import LocalClient

# ============================================================
# CONFIGURATION
# ============================================================
load_dotenv()
st.set_page_config(
    page_title="Squatch Signal | Lead Intelligence",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

def _setting(*names):
    """Read config from the environment, then Streamlit secrets (Streamlit Cloud)."""
    for name in names:
        value = os.getenv(name)
        if not value:
            try:
                value = st.secrets.get(name)
            except Exception:
                value = None
        if value:
            return str(value).strip()
    return ""


# Supabase is OPTIONAL. Without it the app runs in keyless guest mode.
SUPABASE_URL = _setting("SUPABASE_URL")
SUPABASE_KEY = _setting("SUPABASE_PUBLISHABLE_KEY", "SUPABASE_ANON_KEY", "SUPABASE_KEY")

EMAIL_STATUSES = ["Verified", "Unverified", "Missing"]
NAV_ITEMS = ["Overview", "Leads", "Discover Leads", "Import CSV", "Add a Lead", "Outreach Drafts"]
NOW = lambda: datetime.now(timezone.utc).isoformat()

st.markdown(
    """
    <style>
    :root { color-scheme: dark; --gold:#d6b36a; --gold-light:#f0d99b; }
    .stApp,[data-testid="stAppViewContainer"],[data-testid="stMain"] {
      background:#101114 !important; color:#f5f5f7 !important;
    }
    .stApp p,.stApp li,.stApp label,.stApp span,.stApp small,
    .stApp [data-testid="stMarkdownContainer"],.stApp [data-testid="stWidgetLabel"] { color:#f0f0f2; }
    .stApp h1,.stApp h2,.stApp h3,.stApp h4 { color:#f0d99b !important; }
    [data-testid="stSidebar"] { background:#17181c !important; border-right:1px solid #373027; }
    [data-testid="stSidebar"] h1,[data-testid="stSidebar"] h2,[data-testid="stSidebar"] h3 { color:#f0d99b !important; }
    .stApp input,.stApp textarea,[data-baseweb="input"] input,[data-baseweb="textarea"] textarea {
      color:#fff !important; background:#222329 !important; -webkit-text-fill-color:#fff !important;
    }
    .stApp input::placeholder,.stApp textarea::placeholder { color:#b9bac2 !important; -webkit-text-fill-color:#b9bac2 !important; }
    [data-baseweb="select"] > div,[data-baseweb="popover"],[data-baseweb="menu"],[role="listbox"],[role="option"] {
      color:#fff !important; background:#222329 !important;
    }
    .stApp .stButton > button,.stApp .stDownloadButton > button,.stApp [data-testid="stFormSubmitButton"] button {
      color:#fff !important; background:#292a30 !important; border:1px solid #75613a !important; border-radius:9px; font-weight:600;
    }
    .stApp .stButton > button[kind="primary"],.stApp [data-testid="stFormSubmitButton"] button[kind="primary"] {
      color:#171717 !important; background:#d6b36a !important; border:1px solid #d6b36a !important; font-weight:700;
    }
    [data-testid="stMetric"] { background:linear-gradient(145deg,#202126,#18191d); border:1px solid #413723; padding:16px; border-radius:13px; }
    [data-testid="stMetricLabel"],[data-testid="stMetricLabel"] * { color:#d0d0d5 !important; }
    [data-testid="stMetricValue"],[data-testid="stMetricValue"] * { color:#f0d99b !important; }
    .ss-card { background:#191a1f; border:1px solid #3b3326; padding:18px; border-radius:13px; margin-bottom:12px; }
    .ss-muted { color:#c5c5cc !important; font-size:.9rem; }
    div[data-testid="stForm"] { background:#191a1f !important; border:1px solid #3b3326; border-radius:12px; padding:18px; }
    [data-testid="stExpander"] { background:#191a1f !important; border:1px solid #39342a; border-radius:10px; }
    .stApp a { color:#f0d99b !important; }
    hr { border-color:#39342a !important; }
    .ss-table-wrap { overflow-x:auto; border:1px solid #3b3326; border-radius:10px; margin:8px 0 14px; }
    .ss-table { width:100%; border-collapse:collapse; font-size:.88rem; }
    .ss-table th { background:#202126; color:#f0d99b; text-align:left; padding:8px 10px; white-space:nowrap; }
    .ss-table td { padding:7px 10px; border-top:1px solid #2c2a25; color:#f0f0f2; max-width:340px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .ss-bar-row { display:flex; align-items:center; gap:10px; margin:7px 0; font-size:.9rem; }
    .ss-bar-label { width:110px; color:#c5c5cc; }
    .ss-bar-track { flex:1; background:#222329; border-radius:6px; height:14px; }
    .ss-bar-fill { background:#d6b36a; height:14px; border-radius:6px; }
    .ss-bar-val { width:30px; text-align:right; color:#f0d99b; font-weight:600; }
    </style>
    """,
    unsafe_allow_html=True,
)

DEFAULT_STATE = {
    "authenticated": False, "access_token": None, "user_id": None,
    "user_email": None, "db_client": None, "page": "Overview",
    "selected_lead_id": None, "guest": False, "draft_subject": "", "draft_body": "",
    "draft_lead_id": None, "discovery_results": [], "discovery_errors": [],
}
for key, value in DEFAULT_STATE.items():
    if key not in st.session_state:
        st.session_state[key] = copy.deepcopy(value)

# ============================================================
# AUTHENTICATION / DATABASE
# ============================================================
def new_supabase_client():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Missing SUPABASE_URL or Supabase publishable/anon key. Check your .env file.")
    from supabase import create_client  # imported lazily: guests never need it
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def get_db():
    client = st.session_state.get("db_client")
    if client is None:
        raise RuntimeError("Your database session has expired. Sign in again.")
    token = st.session_state.get("access_token")
    if token:
        client.postgrest.auth(token)
    return client


def establish_session(auth_response):
    session = getattr(auth_response, "session", None)
    user = getattr(auth_response, "user", None)
    if not session or not user:
        raise RuntimeError("Supabase did not return a valid session. If email confirmation is enabled, confirm your email first.")
    st.session_state["db_client"] = new_supabase_client()
    st.session_state["access_token"] = session.access_token
    st.session_state["user_id"] = str(user.id)
    st.session_state["user_email"] = user.email or ""
    st.session_state["authenticated"] = True


def clear_session():
    for key, value in DEFAULT_STATE.items():
        st.session_state[key] = copy.deepcopy(value)


def start_guest_session():
    st.session_state["db_client"] = LocalClient()
    st.session_state["access_token"] = None
    st.session_state["user_id"] = "guest"
    st.session_state["user_email"] = "Guest workspace"
    st.session_state["guest"] = True
    st.session_state["authenticated"] = True


def auth_screen():
    st.markdown("# 🟡 Squatch Signal")
    st.caption("Lead intelligence · Gold Edition")
    st.markdown("### Start prospecting")
    st.write("Turn any list of company websites into scored, evidence-backed leads with outreach drafts. No API keys required.")
    if st.button("Continue as guest — no account needed", type="primary", width="stretch"):
        start_guest_session(); st.rerun()
    st.caption("A guest workspace lives in this browser session only. Export to CSV to keep your leads, or sign in to save them permanently.")
    if not SUPABASE_URL or not SUPABASE_KEY:
        st.info("Account sign-in is optional and not configured on this deployment (add SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY to enable it).")
        return
    st.divider()
    st.markdown("### Welcome back")
    mode = st.radio("Account", ["Sign in", "Create account"], horizontal=True)
    with st.form("authentication_form"):
        email = st.text_input("Email address").strip().lower()
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in" if mode == "Sign in" else "Create account", type="primary", width="stretch")
    if submitted:
        if not email or not password:
            st.error("Enter both your email and password.")
            return
        try:
            client = new_supabase_client()
            if mode == "Sign in":
                response = client.auth.sign_in_with_password({"email": email, "password": password})
                establish_session(response)
                st.rerun()
            else:
                response = client.auth.sign_up({"email": email, "password": password})
                if getattr(response, "session", None):
                    establish_session(response)
                    st.success("Account created.")
                    st.rerun()
                else:
                    st.info("Account created. Check your email to confirm your account, then sign in.")
        except Exception as exc:
            st.error(f"Authentication failed: {exc}")

# ============================================================
# HELPERS
# ============================================================
def current_user_id():
    user_id = st.session_state.get("user_id")
    if not user_id:
        raise RuntimeError("You must sign in before accessing records.")
    return str(user_id)


def clean_text(value):
    return "" if value is None else str(value).strip()


def _cell(value):
    text = "" if value is None else " ".join(str(value).split())
    return _html.escape(text).replace("$", "&#36;")  # escape: scraped text must never become HTML / LaTeX


def show_table(rows, max_rows=200):
    """Render a list of dicts as a themed table without pandas.

    st.dataframe needs pandas, whose compiled files some locked-down Windows
    machines block. Every value is HTML-escaped.
    """
    rows = list(rows or [])[:max_rows]
    if not rows:
        return
    cols = list(dict.fromkeys(key for row in rows for key in row))
    head = "".join(f"<th>{_cell(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f'<td title="{_cell(row.get(c))}">{_cell(row.get(c))}</td>' for c in cols) + "</tr>" for row in rows)
    st.markdown(f'<div class="ss-table-wrap"><table class="ss-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>', unsafe_allow_html=True)


def show_bars(data):
    """Tiny horizontal bar chart without pandas. `data` maps label -> count."""
    peak = max(max(data.values(), default=0), 1)
    rows = "".join(f'<div class="ss-bar-row"><span class="ss-bar-label">{_cell(label)}</span><div class="ss-bar-track"><div class="ss-bar-fill" style="width:{value / peak * 100:.0f}%"></div></div><span class="ss-bar-val">{int(value)}</span></div>' for label, value in data.items())
    st.markdown(rows, unsafe_allow_html=True)


def safe_int(value, default=0):
    try:
        return default if value is None or str(value).strip() == "" else int(float(value))
    except (ValueError, TypeError):
        return default


def safe_float(value, default=0.0):
    try:
        cleaned = str(value).replace("$", "").replace(",", "").strip()
        return default if not cleaned else float(cleaned)
    except (ValueError, TypeError):
        return default


def valid_email(email):
    return bool(re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+", email or ""))


def lead_name(lead):
    return clean_text(lead.get("company")) or "Unnamed company"


def lead_contact(lead):
    return clean_text(lead.get("contact_name") or lead.get("contact"))


def lead_title(lead):
    return clean_text(lead.get("contact_title") or lead.get("title"))


def lead_revenue(lead):
    return safe_float(lead.get("estimated_revenue_usd_m", lead.get("revenue", 0)))


def lead_score(lead):
    return safe_int(lead.get("fit_score", lead.get("score", 0)))


def normalize_email_status(status, email):
    status = clean_text(status).title()
    if not email:
        return "Missing"
    return status if status in ("Verified", "Unverified") else "Unverified"


def normalize_lead_payload(data):
    company = clean_text(data.get("company"))
    email = clean_text(data.get("email")).lower()
    contact = clean_text(data.get("contact_name") or data.get("contact"))
    title = clean_text(data.get("contact_title") or data.get("title"))
    revenue = max(0.0, safe_float(data.get("estimated_revenue_usd_m", data.get("revenue", 0))))
    score = max(0, min(100, safe_int(data.get("fit_score", data.get("score", 0)))))
    domain = clean_text(data.get("domain")).lower()
    return {
        "company": company, "domain": domain or None,
        "contact_name": contact or None, "contact_title": title or None,
        "industry": clean_text(data.get("industry")) or None,
        "employees": max(0, safe_int(data.get("employees", 0))),
        "estimated_revenue_usd_m": revenue, "fit_score": score,
        "email": email or None, "email_status": normalize_email_status(data.get("email_status", data.get("status", "")), email),
        "signals": clean_text(data.get("signals")) or None,
        "evidence": clean_text(data.get("evidence")) or None,
        # Keep legacy columns synchronized with the existing schema.
        "contact": contact or None, "title": title or None,
        "revenue": revenue, "score": score,
    }


def calculate_lead_score(data):
    """Transparent example heuristic, not a conversion-probability prediction."""
    score, reasons = 0, []
    industry = clean_text(data.get("industry")).lower()
    employees = safe_int(data.get("employees"))
    email = clean_text(data.get("email"))
    status = clean_text(data.get("email_status")).title()
    if 50 <= employees <= 1000:
        score += 25; reasons.append("+25: company size is within the example ICP")
    elif employees > 0:
        score += 10; reasons.append("+10: company size is known")
    if any(k in industry for k in ("saas", "software", "technology", "cybersecurity", "it services")):
        score += 20; reasons.append("+20: industry matches the example technology ICP")
    elif industry:
        score += 10; reasons.append("+10: industry is known")
    if clean_text(data.get("signals")):
        score += 25; reasons.append("+25: a business signal has been recorded")
    if clean_text(data.get("evidence")):
        score += 15; reasons.append("+15: supporting evidence notes are present")
    if email and status == "Verified":
        score += 15; reasons.append("+15: email is marked verified")
    elif email:
        score += 7; reasons.append("+7: an email address is available but not verified")
    return min(100, score), reasons


def lead_dedupe_key(data):
    domain = clean_text(data.get("domain")).lower()
    email = clean_text(data.get("email")).lower()
    company = " ".join(clean_text(data.get("company")).lower().split())
    if domain:
        candidate = domain if "://" in domain else f"https://{domain}"
        try:
            host = (urlsplit(candidate).hostname or "").lower().rstrip(".")
            if host.startswith("www."):
                host = host[4:]
            if host:
                return f"domain:{host}"
        except ValueError:
            pass
    if email:
        return f"email:{email}"
    if company:
        return f"company:{company}"
    return ""

# ============================================================
# DATABASE OPERATIONS
# ============================================================
def load_leads():
    try:
        response = get_db().table("leads").select("*").eq("user_id", current_user_id()).order("created_at", desc=True).execute()
        return response.data or []
    except Exception as exc:
        st.error(f"Could not load leads: {exc}")
        return []


def load_drafts():
    try:
        response = get_db().table("outreach_drafts").select("*").eq("user_id", current_user_id()).order("updated_at", desc=True).execute()
        return response.data or []
    except Exception as exc:
        st.error(f"Could not load outreach drafts: {exc}")
        return []


def save_lead(data, lead_id=None):
    try:
        user_id = current_user_id()
        payload = normalize_lead_payload(data)
        payload["user_id"] = user_id
        payload["updated_at"] = NOW()
        if not payload["company"]:
            st.error("Company name is required."); return False
        if payload["email"] and not valid_email(payload["email"]):
            st.error("Enter a valid email address."); return False
        if lead_id:
            response = get_db().table("leads").update(payload).eq("id", str(lead_id)).eq("user_id", user_id).execute()
        else:
            payload["created_at"] = NOW()
            response = get_db().table("leads").insert(payload).execute()
        if not response.data:
            st.error("No record was changed. The lead may not exist or may not belong to your account.")
            return False
        return True
    except Exception as exc:
        st.error(f"Could not save lead: {exc}")
        return False


def save_discoveries(items):
    """Store every discovery result in public.discovered_leads (best effort)."""
    try:
        user_id = current_user_id()
        rows = []
        for i in items:
            all_emails = i.get("all_emails") or []
            if isinstance(all_emails, (list, tuple, set)):
                all_emails = ", ".join(str(e) for e in all_emails)
            rows.append({
                "user_id": user_id,
                "company": clean_text(i.get("company")) or None,
                "domain": clean_text(i.get("domain")).lower() or None,
                "email": clean_text(i.get("email")).lower() or None,
                "email_status": normalize_email_status(i.get("email_status"), i.get("email")),
                "industry": clean_text(i.get("industry")) or None,
                "signals": clean_text(i.get("signals")) or None,
                "evidence": clean_text(i.get("evidence")) or None,
                "source_url": clean_text(i.get("source_url")) or None,
                "page_title": clean_text(i.get("page_title")) or None,
                "description": clean_text(i.get("description")) or None,
                "all_emails": clean_text(all_emails) or None,
                "content_type": clean_text(i.get("content_type")) or None,
            })
        if rows:
            get_db().table("discovered_leads").insert(rows).execute()
        return True
    except Exception as exc:
        st.warning(f"Could not store discovery history: {exc}")
        return False


def delete_lead(lead_id):
    try:
        get_db().table("leads").delete().eq("id", str(lead_id)).eq("user_id", current_user_id()).execute()
        return True
    except Exception as exc:
        st.error(f"Could not delete lead: {exc}")
        return False


def save_outreach_draft(subject, body, lead_id=None):
    try:
        user_id = current_user_id()
        body = clean_text(body)
        if not body:
            st.error("Draft body cannot be empty."); return False
        payload = {
            "user_id": user_id, "lead_id": str(lead_id) if lead_id else None,
            "subject": clean_text(subject) or None, "body": body,
            "status": "draft", "updated_at": NOW(),
        }
        payload["created_at"] = NOW()
        response = get_db().table("outreach_drafts").insert(payload).execute()
        if not response.data:
            st.error("No draft was saved. Check your account permissions and table policies.")
            return False
        return True
    except Exception as exc:
        st.error(f"Could not save draft: {exc}")
        return False


def delete_draft(draft_id):
    try:
        get_db().table("outreach_drafts").delete().eq("id", str(draft_id)).eq("user_id", current_user_id()).execute()
        return True
    except Exception as exc:
        st.error(f"Could not delete draft: {exc}")
        return False

# ============================================================
# CSV AND OUTREACH HELPERS
# ============================================================
CSV_ALIASES = {
    "company": ["company", "company_name", "organization", "account"],
    "domain": ["domain", "website", "company_domain"],
    "contact_name": ["contact_name", "contact", "person", "full_name"],
    "contact_title": ["contact_title", "title", "job_title", "role"],
    "industry": ["industry", "sector"],
    "employees": ["employees", "employee_count", "company_size"],
    "estimated_revenue_usd_m": ["estimated_revenue_usd_m", "revenue", "revenue_m", "estimated_revenue"],
    "email": ["email", "work_email", "business_email"],
    "email_status": ["email_status", "status"],
    "fit_score": ["fit_score", "score", "lead_score"],
    "signals": ["signals", "growth_signals", "reason_to_contact"],
    "evidence": ["evidence", "source", "source_notes"],
}


def normalize_header(header):
    return re.sub(r"[^a-z0-9]+", "_", clean_text(header).lower()).strip("_")


def map_csv_row(row):
    normalized = {normalize_header(k): clean_text(v) for k, v in row.items() if k is not None}
    result = {}
    for target, aliases in CSV_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                result[target] = normalized[alias]; break
    return result


def make_csv_bytes(rows):
    fields = ["company", "domain", "contact_name", "contact_title", "industry", "employees", "estimated_revenue_usd_m", "fit_score", "email", "email_status", "signals", "evidence", "created_at", "updated_at"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({**row, "contact_name": row.get("contact_name") or row.get("contact"), "contact_title": row.get("contact_title") or row.get("title"), "estimated_revenue_usd_m": lead_revenue(row), "fit_score": lead_score(row), "email_status": row.get("email_status") or row.get("status")})
    return output.getvalue().encode("utf-8-sig")


def generate_email_draft(lead, value_proposition):
    company = lead_name(lead)
    contact = lead_contact(lead)
    first_name = contact.split()[0] if contact else "there"
    signals = clean_text(lead.get("signals"))
    context = f"I noticed your team may be focused on {signals[0].lower()}{signals[1:] if len(signals) > 1 else ''}." if signals else f"I came across {company} and wanted to learn more about your team's current priorities."
    subject = f"Idea for {company}"
    body = (f"Hi {first_name},\n\n{context}\n\nWe help teams with {value_proposition.strip()}.\n\nWould a brief conversation next week be useful to explore whether this could support your team's goals?\n\nBest,\n[Your name]")
    return subject, body

# ============================================================
# APPLICATION SHELL
# ============================================================
if not st.session_state["authenticated"]:
    auth_screen()
    st.stop()

try:
    get_db()
    current_user_id()
except Exception as exc:
    st.warning(f"Your session needs attention: {exc}")
    if st.button("Return to sign in", type="primary"):
        clear_session(); st.rerun()
    st.stop()

with st.sidebar:
    st.markdown("# 🟡 Squatch Signal")
    st.caption("LEAD INTELLIGENCE · GOLD EDITION")
    st.divider()
    # Stable widget key + callback: page changes made in code (e.g. after saving) stay in sync
    # with the sidebar, and every click registers (an index-based radio drops alternate clicks).
    st.session_state["nav_radio"] = st.session_state["page"] if st.session_state["page"] in NAV_ITEMS else NAV_ITEMS[0]
    st.radio("Navigation", NAV_ITEMS, key="nav_radio", on_change=lambda: st.session_state.update(page=st.session_state["nav_radio"]), label_visibility="collapsed")
    st.divider()
    st.caption("SIGNED IN AS")
    st.write(st.session_state.get("user_email") or "Authenticated user")
    if st.session_state.get("guest"):
        st.caption("Guest data lives in this browser session. Export CSV to keep it.")
    if st.button("Exit guest workspace" if st.session_state.get("guest") else "Sign out", width="stretch"):
        try:
            get_db().auth.sign_out()
        except Exception:
            pass
        clear_session(); st.rerun()
    st.markdown('<div class="ss-card"><b style="color:#f0d99b">Evidence-first prospecting</b><p class="ss-muted">Scores are transparent heuristics. Verify company information and contact permissions before outreach.</p></div>', unsafe_allow_html=True)

leads = load_leads()
drafts = load_drafts()
st.markdown('<div class="ss-muted">PROSPECTING WORKSPACE</div>', unsafe_allow_html=True)

# ============================================================
# DISCOVER LEADS
# ============================================================
if st.session_state["page"] == "Discover Leads":
    st.title("Discover leads from company websites")
    st.write("Paste company websites. Squatch Signal reads each site's own public pages (home, contact, about, careers, news) and turns them into scored, evidence-backed prospects. No API keys, no paid data.")
    st.info("Only the URLs you provide are visited, and each site's robots.txt is honoured. Logins, CAPTCHAs and rate limits are never bypassed. Emails are checked for syntax and a working mail domain (MX), not for mailbox existence.")
    with st.form("website_discovery_form"):
        urls_text = st.text_area("Company website URLs (one per line)", placeholder="hubspot.com\nzoho.com\nhttps://www.postman.com", height=150)
        o1, o2 = st.columns(2)
        deep = o1.checkbox("Also read contact, about, careers & news pages", value=True, help="Finds more emails, employee counts and hiring/funding signals. Adds a few seconds per company.")
        archive = o2.checkbox("If a site blocks bots, use its Internet Archive copy", value=True, help="Public archived copy of the home page, clearly labelled as possibly outdated.")
        st.caption("Up to 10 companies per batch. Companies are processed in parallel and cached for one hour.")
        discover_clicked = st.form_submit_button("Discover prospects", type="primary", width="stretch")
    if discover_clicked:
        raw_urls = [line.strip() for line in urls_text.splitlines() if line.strip()]
        if not raw_urls:
            st.warning("Enter at least one website URL.")
        elif len(raw_urls) > 10:
            st.warning("Please submit no more than 10 URLs per batch.")
        else:
            try:
                with st.spinner(f"Reading {len(raw_urls)} public website(s)…"):
                    found, errors = discover_urls(raw_urls, deep=deep, archive_fallback=archive)
                for item in found:
                    item["email_status"] = normalize_email_status(item.get("email_status"), item.get("email"))
                    score_data = {"industry": item.get("industry"), "employees": item.get("employees", 0), "email": item.get("email"), "email_status": item.get("email_status"), "signals": item.get("signals"), "evidence": item.get("evidence")}
                    item["fit_score"], item["score_reasons"] = calculate_lead_score(score_data)
                    item["selected"] = True
                found.sort(key=lambda x: x.get("fit_score", 0), reverse=True)
                st.session_state["discovery_results"] = found
                save_discoveries(found)
                st.session_state["discovery_errors"] = errors
            except Exception as exc:
                st.error(f"Website discovery failed: {exc}")
                st.session_state["discovery_results"] = []
                st.session_state["discovery_errors"] = [{"url": "", "error": str(exc)}]

    found = st.session_state.get("discovery_results", [])
    errors = st.session_state.get("discovery_errors", [])
    if found:
        st.divider(); st.subheader("Review discovered prospects")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Companies analysed", len(found))
        c2.metric("With a public email", sum(bool(x.get("email")) for x in found))
        c3.metric("With business signals", sum(bool(x.get("signals")) for x in found))
        c4.metric("Avg data completeness", f"{round(sum(x.get('data_quality', 0) for x in found) / len(found))}%")
        review_rows = []
        for idx, item in enumerate(found):
            with st.container(border=True):
                h1, h2, h3 = st.columns([4, 1, 1])
                with h1:
                    st.markdown(f"### {item.get('company') or 'Unknown company'}")
                    employees = item.get("employees") or 0
                    st.caption(f"{item.get('domain') or 'Unknown domain'} · {item.get('industry') or 'Industry not inferred'}" + (f" · ~{employees:,} employees" if employees else "") + (f" · {item['location']}" if item.get("location") else ""))
                    source_url = clean_text(item.get("source_url"))
                    if source_url:
                        st.markdown(f"**Source:** [{source_url}]({source_url})")
                    if item.get("discovery_method") == "archived_snapshot":
                        st.warning("This site blocks automated access, so this result comes from a public Internet Archive copy and may be outdated.")
                with h2:
                    st.metric("Fit score", f"{item.get('fit_score', 0)}/100")
                with h3:
                    st.metric("Data quality", f"{item.get('data_quality', 0)}%")
                if item.get("description"):
                    st.write(item["description"])
                e1, e2, e3 = st.columns([1.2, 1.6, 1])
                with e1:
                    st.write("**Best public email**")
                    st.write(item.get("email") or "No email found")
                    others = [x for x in item.get("all_emails", []) if x != item.get("email")]
                    if others:
                        st.caption("Also found: " + ", ".join(others[:4]))
                    if item.get("email_check"):
                        st.caption("Check: " + item["email_check"] + ". Not a mailbox verification.")
                with e2:
                    st.write("**Business signals**")
                    if item.get("signals"):
                        for part in item["signals"].split(" | "):
                            st.write(f"- {part}")
                    else:
                        st.write("No clear signal found on the pages read.")
                with e3:
                    st.write("**Contact**")
                    st.write(item.get("phone") or "No phone found")
                    if item.get("linkedin"):
                        st.markdown(f"[LinkedIn]({item['linkedin']})")
                with st.expander("View source evidence and score breakdown"):
                    st.write(item.get("evidence") or "No evidence text returned.")
                    st.caption("Pages read: " + ", ".join(item.get("pages_crawled", [])) + (" · served from cache" if item.get("from_cache") else ""))
                    for reason in item.get("score_reasons", []): st.write(f"- {reason}")
                item["selected"] = st.checkbox("Select to save to my leads", value=item.get("selected", True), key=f"discovery_select_{idx}")
                review_rows.append({"Save": item["selected"], "Company": item.get("company"), "Domain": item.get("domain"), "Industry": item.get("industry"), "Employees": item.get("employees"), "Email": item.get("email"), "Fit score": item.get("fit_score"), "Signals": item.get("signals"), "Source URL": item.get("source_url")})
        show_table(review_rows)
        selected = [x for x in found if x.get("selected")]
        if st.button(f"Save {len(selected)} selected prospect(s) to my leads", type="primary", disabled=not selected, width="stretch"):
            existing_keys = {lead_dedupe_key(x) for x in leads if lead_dedupe_key(x)}
            saved = skipped = failed = 0
            progress = st.progress(0)
            for idx, item in enumerate(selected):
                key = lead_dedupe_key(item)
                if key and key in existing_keys:
                    skipped += 1
                else:
                    payload = {"company": item.get("company"), "domain": item.get("domain"), "industry": item.get("industry"), "employees": item.get("employees", 0), "estimated_revenue_usd_m": 0, "fit_score": item.get("fit_score", 0), "email": item.get("email"), "email_status": normalize_email_status(item.get("email_status"), item.get("email")), "signals": item.get("signals"), "evidence": "Source URL: " + clean_text(item.get("source_url")) + ("\n" + clean_text(item.get("evidence")) if item.get("evidence") else "")}
                    if save_lead(payload):
                        saved += 1
                        if key: existing_keys.add(key)
                    else:
                        failed += 1
                progress.progress((idx + 1) / max(len(selected), 1))
            if saved: st.success(f"Saved {saved} lead(s).")
            if skipped: st.info(f"Skipped {skipped} duplicate(s) already in your leads.")
            if failed: st.warning(f"{failed} lead(s) could not be saved. See the error message above.")
            if saved:
                st.session_state["page"] = "Leads"; st.rerun()
        csv_buffer = io.StringIO()
        fields = ["company", "domain", "industry", "employees", "email", "email_status", "fit_score", "signals", "phone", "linkedin", "location", "data_quality", "source_url", "evidence"]
        writer = csv.DictWriter(csv_buffer, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(found)
        d1, d2 = st.columns([3, 1])
        d1.download_button("Download discovery results CSV", csv_buffer.getvalue().encode("utf-8-sig"), "squatch_signal_discovered_leads.csv", "text/csv")
        if d2.button("Clear 1-hour cache"):
            clear_cache(); st.toast("Cache cleared. The next run fetches fresh pages.")
    if errors:
        st.subheader("Websites that could not be read")
        show_table(errors)
        st.caption("Blocked or login-gated sites are reported, never bypassed. Use a company-provided source or the Import CSV page for those.")
    if not found and not errors:
        st.markdown('<div class="ss-card"><b style="color:#f0d99b">How it works</b><ol><li>Paste company websites, one per line.</li><li>Squatch Signal checks robots.txt, then reads the home page plus contact, about, careers and news pages.</li><li>It extracts a clean company name, industry, headcount, public emails, phone, LinkedIn and sentence-level business signals (hiring, funding, launches, expansion).</li><li>Review the evidence and transparent fit score, then save the prospects you want.</li></ol></div>', unsafe_allow_html=True)

# ============================================================
# OVERVIEW
# ============================================================
elif st.session_state["page"] == "Overview":
    st.title("Find your next best customer.")
    st.write("Manage prospects, understand why they qualify, and prepare personalized outreach.")
    high_fit = [x for x in leads if lead_score(x) >= 80]
    verified = [x for x in leads if clean_text(x.get("email")) and clean_text(x.get("email_status")).title() == "Verified"]
    total_revenue = sum(lead_revenue(x) for x in leads)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total leads", len(leads)); c2.metric("High-fit leads", len(high_fit)); c3.metric("Verified emails", len(verified)); c4.metric("Estimated revenue", f"${total_revenue:,.1f}M")
    st.caption("Revenue figures are user-provided estimates, not independently verified.")
    left, right = st.columns([1.6, 1])
    with left:
        st.subheader("Recently added leads")
        if leads:
            show_table([{"Company": lead_name(x), "Industry": x.get("industry") or "—", "Fit score": lead_score(x), "Email status": x.get("email_status") or "Missing"} for x in leads[:10]])
        else:
            st.info("No leads yet. Discover leads, add a lead, or import a CSV to get started.")
    with right:
        st.subheader("Lead score distribution")
        show_bars({"High (80–100)": sum(lead_score(x) >= 80 for x in leads), "Medium (50–79)": sum(50 <= lead_score(x) < 80 for x in leads), "Low (0–49)": sum(lead_score(x) < 50 for x in leads)})
        st.metric("Saved outreach drafts", len(drafts))
        if st.button("Go to leads", type="primary", width="stretch"):
            st.session_state["page"] = "Leads"; st.rerun()

# ============================================================
# LEADS
# ============================================================
elif st.session_state["page"] == "Leads":
    st.title("Lead discovery")
    st.write("Search, filter, inspect evidence, and manage your prospects.")
    search = st.text_input("Search leads", placeholder="Company, domain, contact, email, or industry").lower().strip()
    industries = sorted({clean_text(x.get("industry")) for x in leads if clean_text(x.get("industry"))})
    f1, f2, f3 = st.columns(3)
    industry_filter = f1.selectbox("Industry", ["All"] + industries)
    min_score = f2.selectbox("Minimum fit score", [0, 50, 70, 80, 90])
    status_filter = f3.selectbox("Email status", ["All", "Verified", "Unverified", "Missing"])
    filtered = []
    for lead in leads:
        searchable = " ".join([lead_name(lead), clean_text(lead.get("domain")), lead_contact(lead), clean_text(lead.get("email")), clean_text(lead.get("industry")), lead_title(lead)]).lower()
        if search and search not in searchable: continue
        if industry_filter != "All" and lead.get("industry") != industry_filter: continue
        if lead_score(lead) < min_score: continue
        if status_filter != "All" and clean_text(lead.get("email_status")).title() != status_filter: continue
        filtered.append(lead)
    st.caption(f"{len(filtered)} matching leads")
    if filtered:
        st.download_button("Export filtered leads to CSV", data=make_csv_bytes(filtered), file_name="squatch-signal-leads.csv", mime="text/csv")
    for lead in filtered:
        lead_id = lead.get("id"); score = lead_score(lead)
        with st.expander(f"{lead_name(lead)} · Score {score}/100 · {lead.get('email_status') or 'Missing email'}"):
            a, b = st.columns([2, 1])
            with a:
                st.write(f"**Domain:** {lead.get('domain') or '—'}")
                st.write(f"**Industry:** {lead.get('industry') or '—'}")
                st.write(f"**Contact:** {lead_contact(lead) or '—'}")
                st.write(f"**Title:** {lead_title(lead) or '—'}")
                st.write(f"**Email:** {lead.get('email') or '—'}")
                st.write(f"**Employees:** {lead.get('employees') or '—'}")
                st.write(f"**Estimated revenue:** ${lead_revenue(lead):,.2f}M")
            with b:
                st.progress(max(0, min(100, score)) / 100); st.markdown(f"### {score}/100"); st.caption("Stored fit score")
                if st.button("Edit lead", key=f"edit_{lead_id}"):
                    st.session_state["selected_lead_id"] = str(lead_id); st.session_state["page"] = "Add a Lead"; st.rerun()
                if st.button("Create outreach draft", key=f"draft_{lead_id}"):
                    subject, body = generate_email_draft(lead, "qualifying leads faster and improving outbound relevance")
                    st.session_state["draft_subject"] = subject; st.session_state["draft_body"] = body; st.session_state["draft_lead_id"] = str(lead_id); st.session_state["page"] = "Outreach Drafts"; st.rerun()
            st.markdown("**Signals**"); st.write(lead.get("signals") or "No signals recorded.")
            st.markdown("**Evidence / source notes**"); st.write(lead.get("evidence") or "No evidence recorded.")
            with st.expander("How to interpret this score"):
                calculated, reasons = calculate_lead_score(lead)
                st.write(f"Stored score: **{score}/100**. Current heuristic score: **{calculated}/100**.")
                for reason in reasons: st.write(f"- {reason}")
                st.caption("This is an example ICP heuristic, not a prediction of conversion probability.")
            with st.form(f"delete_form_{lead_id}"):
                confirm = st.checkbox("I confirm that I want to delete this lead.", key=f"confirm_delete_{lead_id}")
                delete_clicked = st.form_submit_button("Delete lead")
            if delete_clicked:
                if not confirm: st.warning("Confirm deletion first.")
                elif delete_lead(lead_id): st.success("Lead deleted."); st.rerun()
    if not filtered:
        st.info("No leads match your filters. Try changing the search or filters.")

# ============================================================
# CSV IMPORT
# ============================================================
elif st.session_state["page"] == "Import CSV":
    st.title("Import leads from CSV")
    st.write("Upload a prospect list, preview the mapping, and skip records that appear to be duplicates.")
    st.caption("Supported columns include company, domain, contact_name, contact_title, industry, employees, estimated_revenue_usd_m, fit_score, email, email_status, signals, and evidence.")
    uploaded_file = st.file_uploader("Choose a CSV file", type=["csv"])
    if uploaded_file:
        try:
            raw = uploaded_file.getvalue().decode("utf-8-sig")
            parsed = [map_csv_row(row) for row in csv.DictReader(io.StringIO(raw))]
            valid_rows = [row for row in parsed if clean_text(row.get("company"))]
            invalid_count = len(parsed) - len(valid_rows)
            existing_keys = {lead_dedupe_key(x) for x in leads if lead_dedupe_key(x)}
            import_rows, seen, duplicate_count = [], set(), 0
            for row in valid_rows:
                key = lead_dedupe_key(row)
                if key and (key in existing_keys or key in seen): duplicate_count += 1; continue
                if key: seen.add(key)
                import_rows.append(row)
            st.metric("Rows in file", len(parsed))
            c1, c2, c3 = st.columns(3)
            c1.metric("Ready to import", len(import_rows)); c2.metric("Potential duplicates skipped", duplicate_count); c3.metric("Missing company skipped", invalid_count)
            if import_rows:
                st.subheader("Preview"); show_table(import_rows[:25])
                st.caption("Duplicate detection checks normalized domain first, then email, then company name. Review the preview before importing.")
                if st.button(f"Import {len(import_rows)} leads", type="primary", width="stretch"):
                    imported = failed = 0; progress = st.progress(0)
                    for index, row in enumerate(import_rows):
                        email = clean_text(row.get("email")).lower()
                        row["email"] = email
                        row["email_status"] = normalize_email_status(row.get("email_status"), email)
                        if save_lead(row): imported += 1
                        else: failed += 1
                        progress.progress((index + 1) / len(import_rows))
                    if imported: st.success(f"Imported {imported} lead(s).")
                    if failed: st.warning(f"{failed} row(s) could not be imported. Review the error messages above.")
                    st.rerun()
            else:
                st.info("There are no new valid leads to import.")
        except UnicodeDecodeError:
            st.error("Could not read the CSV encoding. Save the file as UTF-8.")
        except Exception as exc:
            st.error(f"Could not parse this CSV file: {exc}")

# ============================================================
# ADD / EDIT LEAD
# ============================================================
elif st.session_state["page"] == "Add a Lead":
    editing_id = st.session_state.get("selected_lead_id")
    editing = next((x for x in leads if str(x.get("id")) == str(editing_id)), None) if editing_id else None
    def field_value(field, legacy=None, default=""):
        if not editing: return default
        value = editing.get(field)
        if value is None and legacy: value = editing.get(legacy)
        return value if value is not None else default
    st.title("Edit lead" if editing else "Add a lead")
    st.write("Enter prospect details and document the evidence behind the fit.")
    with st.form("lead_form"):
        company = st.text_input("Company name *", value=field_value("company"))
        domain = st.text_input("Company domain / website", value=field_value("domain"))
        c1, c2 = st.columns(2)
        with c1:
            industry = st.text_input("Industry", value=field_value("industry"))
            employees = st.number_input("Employee count", min_value=0, value=max(0, safe_int(field_value("employees", default=0))), step=10)
            revenue = st.number_input("Estimated revenue (USD millions)", min_value=0.0, value=max(0.0, safe_float(field_value("estimated_revenue_usd_m", "revenue", 0))), step=0.1)
        with c2:
            contact = st.text_input("Contact name", value=field_value("contact_name", "contact"))
            title = st.text_input("Contact title", value=field_value("contact_title", "title"))
            email = st.text_input("Business email", value=field_value("email"))
            status_default = clean_text(field_value("email_status", default="Unverified")).title()
            if status_default not in EMAIL_STATUSES: status_default = "Unverified"
            email_status = st.selectbox("Email status", EMAIL_STATUSES, index=EMAIL_STATUSES.index(status_default))
        score = st.slider("Fit score", min_value=0, max_value=100, value=max(0, min(100, safe_int(field_value("fit_score", "score", 50)))))
        signals = st.text_area("Signals / reason to contact", value=field_value("signals"), placeholder="e.g. Expanding into a new market; hiring sales staff")
        evidence = st.text_area("Evidence / source notes", value=field_value("evidence"), placeholder="Record source URL, announcement, date checked, or other supporting details.")
        preview_clicked = st.form_submit_button("Calculate example score from current fields")
        submitted = st.form_submit_button("Update lead" if editing else "Save lead", type="primary")
    if preview_clicked:
        calculated, reasons = calculate_lead_score({"industry": industry, "employees": employees, "email": email, "email_status": email_status, "signals": signals, "evidence": evidence})
        st.info(f"Example heuristic score: {calculated}/100")
        for reason in reasons: st.write(f"- {reason}")
        st.caption("This calculates an example score from your current inputs; it does not save the record or change the slider automatically.")
    if submitted:
        if not clean_text(company): st.error("Company name is required.")
        elif email and not valid_email(email): st.error("Please enter a valid email address.")
        elif email_status == "Verified" and not email: st.error("A verified email status requires an email address.")
        else:
            payload = {"company": company, "domain": domain, "industry": industry, "employees": employees, "estimated_revenue_usd_m": revenue, "contact_name": contact, "contact_title": title, "email": email, "email_status": email_status, "fit_score": score, "signals": signals, "evidence": evidence}
            if save_lead(payload, editing_id):
                st.success("Lead saved successfully."); st.session_state["selected_lead_id"] = None; st.session_state["page"] = "Leads"; st.rerun()
    if editing and st.button("Cancel editing"):
        st.session_state["selected_lead_id"] = None; st.session_state["page"] = "Leads"; st.rerun()

# ============================================================
# OUTREACH DRAFTS
# ============================================================
elif st.session_state["page"] == "Outreach Drafts":
    st.title("Outreach drafts")
    st.write("Generate, edit, and save human-reviewed first-touch messages.")
    lead_options = {str(x["id"]): f"{lead_name(x)} — {lead_contact(x) or 'No contact'}" for x in leads if x.get("id")}
    if not leads:
        st.info("Add or import a lead before creating a personalized draft.")
    else:
        lead_ids = list(lead_options.keys())
        default_lead_id = st.session_state.get("draft_lead_id")
        default_index = lead_ids.index(default_lead_id) if default_lead_id in lead_ids else 0
        chosen_lead_id = st.selectbox("Select a lead", lead_ids, index=default_index, format_func=lambda x: lead_options[x])
        chosen_lead = next((x for x in leads if str(x.get("id")) == chosen_lead_id), None)
        value_prop = st.text_input("Value proposition", value="qualifying leads faster and improving outbound relevance")
        if st.button("Generate draft from lead", type="primary"):
            subject, body = generate_email_draft(chosen_lead, value_prop)
            st.session_state["draft_subject"] = subject; st.session_state["draft_body"] = body; st.session_state["draft_lead_id"] = chosen_lead_id; st.rerun()
        with st.form("draft_editor"):
            subject = st.text_input("Subject", value=st.session_state.get("draft_subject", ""))
            body = st.text_area("Email body", value=st.session_state.get("draft_body", ""), height=260)
            save_new = st.form_submit_button("Save as new draft", type="primary")
        if save_new:
            if save_outreach_draft(subject, body, chosen_lead_id):
                st.success("Outreach draft saved."); st.session_state["draft_subject"] = subject; st.session_state["draft_body"] = body; st.rerun()
        st.caption("The draft generator uses a local template, not an LLM. Review all claims, recipient details, and outreach permissions.")
    st.divider(); st.subheader("Saved drafts")
    if not drafts:
        st.info("No saved drafts yet.")
    else:
        for draft in drafts:
            draft_id = draft.get("id")
            linked_lead = next((x for x in leads if str(x.get("id")) == str(draft.get("lead_id"))), None)
            label = clean_text(draft.get("subject")) or "Untitled draft"
            with st.expander(f"{label} · {lead_name(linked_lead) if linked_lead else 'Lead unavailable'}"):
                st.caption(f"Status: {draft.get('status', 'draft')} · Updated: {draft.get('updated_at', 'Unknown')}")
                st.text_area("Saved message", value=draft.get("body") or "", height=150, key=f"view_draft_{draft_id}", disabled=True)
                if st.button("Load into editor", key=f"load_{draft_id}"):
                    st.session_state["draft_subject"] = draft.get("subject") or ""
                    st.session_state["draft_body"] = draft.get("body") or ""
                    st.session_state["draft_lead_id"] = str(draft.get("lead_id")) if draft.get("lead_id") else None
                    st.info("Draft loaded into the editor state. You can save a revised version as a new draft.")
                confirm_delete = st.checkbox("Confirm delete", key=f"confirm_draft_{draft_id}")
                if st.button("Delete draft", key=f"delete_draft_{draft_id}"):
                    if confirm_delete:
                        if delete_draft(draft_id): st.success("Draft deleted."); st.rerun()
                    else: st.warning("Confirm deletion first.")

st.divider()
st.caption("Squatch Signal · Lead Intelligence · Gold Edition · Evidence-first prospecting · Outreach drafts require human review")
