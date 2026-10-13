"""Check that your Supabase project matches what Squatch Signal expects.

Run from the project folder:   python check_supabase.py

Reads SUPABASE_URL / SUPABASE_PUBLISHABLE_KEY from .env (or the environment),
makes a few read-only requests and prints OK / WARN / FAIL lines.
It never prints your key.
"""
import base64
import json
import os
import re
import sys
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

HERE = Path(__file__).resolve().parent
LEAD_COLUMNS = ("id,user_id,company,domain,contact_name,contact_title,contact,title,industry,employees,"
                "estimated_revenue_usd_m,revenue,fit_score,score,email,email_status,signals,evidence,created_at,updated_at")
DRAFT_COLUMNS = "id,user_id,lead_id,subject,body,status,created_at,updated_at"
DISCOVERED_COLUMNS = ("id,user_id,company,domain,email,email_status,industry,signals,evidence,source_url,"
                      "page_title,description,all_emails,content_type,discovered_at,created_at")
failures = 0


def say(level: str, message: str) -> None:
    global failures
    failures += level == "FAIL"
    print(f"[{level:<4}] {message}")


def jwt_role(token: str) -> str:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("role", "")
    except Exception:
        return ""


def check_table(base: str, key: str, table: str, columns: str) -> None:
    try:
        r = requests.get(f"{base}/rest/v1/{table}", params={"select": columns, "limit": 1},
                         headers={"apikey": key}, timeout=15)
    except requests.RequestException as exc:
        say("FAIL", f"{table}: could not reach Supabase ({type(exc).__name__}).")
        return
    try:
        body = r.json()
    except ValueError:
        body = {}
    code = body.get("code", "") if isinstance(body, dict) else ""
    message = (body.get("message", "") if isinstance(body, dict) else "")[:140]
    if r.status_code == 200:
        say("OK", f"table '{table}' exists and has all columns the app uses.")
        if isinstance(body, list) and body:
            say("FAIL", f"table '{table}' returns rows to an anonymous visitor: row-level security is OFF or too open. "
                        "Run supabase_schema.sql (it enables RLS and per-user policies).")
        else:
            say("OK", f"table '{table}' hides data from anonymous visitors (row-level security is working).")
    elif r.status_code == 404 or code == "PGRST205":
        say("FAIL", f"table '{table}' does not exist. Run supabase_schema.sql in Supabase > SQL Editor.")
    elif code == "42703" or "does not exist" in message:
        say("FAIL", f"table '{table}' is missing a column the app writes: {message}. Add it, or run supabase_schema.sql on a fresh project.")
    elif r.status_code in (401, 403):
        say("FAIL", f"table '{table}': the key was rejected (HTTP {r.status_code}). Check you copied the publishable/anon key.")
    else:
        say("WARN", f"table '{table}': unexpected response HTTP {r.status_code} {code} {message}")


def main() -> int:
    env_file = HERE / ".env"
    if load_dotenv:
        load_dotenv(env_file)
    if not env_file.exists():
        say("WARN", "no .env file next to this script. The app only reads '.env' (NOT '.env.example'). "
                    "Copy .env.example to .env and put your values there.")
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    key = next((os.getenv(n).strip() for n in ("SUPABASE_PUBLISHABLE_KEY", "SUPABASE_ANON_KEY", "SUPABASE_KEY") if os.getenv(n)), "")

    if not url or "YOUR_PROJECT" in url:
        say("FAIL", "SUPABASE_URL is missing or still the placeholder.")
        return 1
    if not re.fullmatch(r"https://[a-z0-9]{20}\.supabase\.co", url):
        say("WARN", "SUPABASE_URL does not look like https://<20-character-ref>.supabase.co "
                    "(use the 'Project URL' from Settings > API; no trailing path).")
    else:
        say("OK", "SUPABASE_URL format looks right.")

    if not key or "YOUR_" in key:
        say("FAIL", "no publishable/anon key found (SUPABASE_PUBLISHABLE_KEY) or it is still the placeholder.")
        return 1
    if key.startswith("sb_secret_"):
        say("FAIL", "that is a SECRET key. Never use it in this app or commit it. Use the publishable key (sb_publishable_...) "
                    "and rotate the secret key in Supabase.")
        return 1
    if key.startswith("sb_publishable_"):
        say("OK", "key type: publishable (correct).")
    elif key.startswith("eyJ"):
        role = jwt_role(key)
        if role == "service_role":
            say("FAIL", "that is the service_role key. Never use it here. Use the anon/publishable key and rotate service_role.")
            return 1
        say("OK" if role == "anon" else "WARN", f"key type: legacy JWT (role={role or 'unknown'}).")
    else:
        say("WARN", "key format not recognised; expected sb_publishable_... or a legacy anon JWT.")

    try:
        r = requests.get(f"{url}/auth/v1/settings", headers={"apikey": key}, timeout=15)
    except requests.RequestException as exc:
        say("FAIL", f"cannot reach {url} ({type(exc).__name__}). Check the URL, your internet and the project is not paused.")
        return 1
    if r.status_code in (401, 403):
        say("FAIL", f"Supabase rejected the key (HTTP {r.status_code}). The key does not belong to this project URL.")
        return 1
    if r.status_code != 200:
        say("FAIL", f"auth endpoint returned HTTP {r.status_code}. A free project that was idle may be paused: open the dashboard and restore it.")
        return 1
    say("OK", "project reachable and key accepted.")
    settings = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if settings.get("disable_signup"):
        say("WARN", "sign-ups are disabled: 'Create account' in the app will fail. Enable in Authentication > Sign In / Providers.")
    if not (settings.get("external") or {}).get("email", True):
        say("FAIL", "the Email provider is disabled. Enable it in Authentication > Sign In / Providers.")
    if settings.get("mailer_autoconfirm") is False:
        say("WARN", "email confirmation is ON: new users must click a confirmation email before signing in "
                    "(the free tier limits how many emails Supabase sends per hour). For demos, turn 'Confirm email' off.")

    check_table(url, key, "leads", LEAD_COLUMNS)
    check_table(url, key, "outreach_drafts", DRAFT_COLUMNS)
    check_table(url, key, "discovered_leads", DISCOVERED_COLUMNS)
    print("\nAll checks passed." if not failures else f"\n{failures} problem(s) found. Fix the FAIL lines above.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
    