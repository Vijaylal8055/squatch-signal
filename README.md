# Squatch Signal — keyless lead intelligence

Caprae Capital AI-readiness challenge · enhancement of [SaaSquatch](https://www.saasquatchleads.com/).

**Paste company websites → get scored, evidence-backed prospects** with a clean company name, industry,
headcount, public emails, phone, LinkedIn and sentence-level buying signals (hiring, funding, launches,
expansion) — then review, save, export to CSV and draft outreach. **No API keys, no paid data, no sign-up needed.**

## What I improved and why
SaaSquatch answers "who are the leads". The slow, manual part for a sales team is deciding *which ones are worth contacting and why*.
Squatch Signal adds an **evidence-first qualification layer** on top of lead discovery:

| Problem in a typical scraper | What this does |
|---|---|
| Garbage names, nav-menu text as "signals" | Reads only *content* blocks (nav/footer/scripts ignored); name from JSON-LD → `og:site_name` → best title segment |
| Homepage only → few emails | Follows the site's own contact / about / careers / news links (bounded crawl) |
| Emails missed or fake | `mailto`, plain text, `name [at] domain [dot] com`, Cloudflare-protected emails; junk filtered; ranked (named person > sales@ > info@ > noreply@) |
| No sense of fit | Transparent, explainable 0–100 ICP score (every point has a stated reason) + a separate *data-quality* score |
| Blocked sites kill the batch | Failures isolated per site; optional labelled Internet Archive fallback; never bypasses blocks |
| Trust / compliance | Honours `robots.txt`, SSRF-safe, evidence + source URL kept for every field |

## Architecture
```
Streamlit UI (app.py)
   ├─ Discover Leads ──► lead_discovery.py  (pure Python, requests + stdlib HTMLParser + dnspython)
   │      robots.txt → home page → contact/careers/about/news → JSON-LD + meta + content blocks
   │      → name · industry · employees · emails · signals · MX check → record + evidence
   │      ThreadPoolExecutor (4 workers, one thread per company) · 1-hour in-memory TTL cache
   ├─ Leads / Overview / Import CSV / Add Lead / Outreach Drafts
   └─ Storage (app.py → db_client)
        • Guest mode (default): per-session in-memory workspace (local_db.py) – zero config
        • Optional accounts: Supabase Postgres + Auth with row-level security (supabase_schema.sql)
             leads              ← leads you chose to keep ("Save selected prospects", Add a Lead, Import CSV)
             discovered_leads   ← every Discover run, stored automatically (discovery history)
             outreach_drafts    ← saved email drafts
```
* **Stack:** Python 3.12, Streamlit, requests, dnspython, (optional) supabase-py.
* **Data storage:** guest = session memory (export CSV to keep); accounts = Supabase PostgreSQL with RLS.
* **Caching / performance:** per-URL TTL cache (1 h), parallel companies, response-size cap (1.5 MB), connect/read timeouts, one retry on transient errors, per-domain time budget (40 s), per-host politeness delay, DNS/MX and robots.txt caches.
* **Hosting:** Streamlit Community Cloud (free, managed container — not static, not serverless functions). Deploy = push to GitHub → "New app".
* **Data quality:** dedupe by domain → email → company; MX check on every email domain; employee counts parsed from structured data / copy ("19K+ employees").
* **No pandas needed for the UI:** tables and charts are rendered by small built-in HTML helpers (all values escaped), so the app also runs on locked-down Windows PCs where Application Control blocks pandas' compiled files.
* **Safety:** only user-supplied URLs; public IPs only (blocks localhost/private/metadata IPs on every redirect hop); max 4 redirects; no login/CAPTCHA handling; robots.txt honoured.

## How data flows (important)
| Action in the app | Where it is stored |
|---|---|
| **Discover prospects** | Shown for review **and** logged to `discovered_leads` (signed-in users only; guests stay in memory) |
| **Save N selected prospects to my leads** | Copied into `leads` (duplicates by domain → email → company are skipped) |
| **Add a Lead / Import CSV** | `leads` |
| **Save outreach draft** | `outreach_drafts` |

`discovered_leads` is a history log: running the same URL twice adds rows twice. `leads` is your curated pipeline.
Every row carries your `user_id`, and row-level security means each account sees only its own rows.

## Project structure
```
app.py                  Streamlit UI: sign-in/guest, Discover Leads (+ discovery history), Leads, Import CSV, Add Lead, Outreach Drafts
lead_discovery.py       Keyless discovery engine (robots.txt, crawl, extraction, MX check, cache, threads)
local_db.py             In-memory guest workspace that mimics the Supabase calls the app uses
supabase_schema.sql     OPTIONAL schema (leads, outreach_drafts, discovered_leads) + row-level security; safe to re-run
check_supabase.py       Read-only checker: URL/key type, all 3 tables, columns, row-level security (prints no secrets)
test_lead_discovery.py  Offline engine tests against a local fake multi-site web
test_app.py             Headless end-to-end UI test (guest flow)
requirements.txt        Dependencies (supabase is imported only if you configure it)
.streamlit/config.toml  Dark/gold theme and server settings
.env.example            Optional Supabase variables (copy to .env; never commit .env)
run_app.bat             One-click Windows launcher
.gitignore              Keeps secrets, virtualenvs, caches, exports and zips out of Git
```

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: run_app.bat does all of this
pip install -r requirements.txt
streamlit run app.py                                   # http://localhost:8501 → "Continue as guest"
```

## Deploy free (Streamlit Community Cloud)
Repository: <https://github.com/Vijaylal8055/squatch-signal> (public, branch `main`). Status: deployed and verified. Sign-in works and
every discovery run is stored in Supabase `discovered_leads`.

**First deployment**
1. Push this folder to a **public GitHub repo**. `.gitignore` already excludes `.env`, `.venv`, caches, CSV exports and zips.
   Run `git status` first and confirm `.env` is **not** listed:
   ```bash
   git init && git add . && git status
   git commit -m "Squatch Signal"
   git branch -M main && git remote add origin https://github.com/<you>/squatch-signal.git && git push -u origin main
   ```
2. Go to <https://share.streamlit.io> → **Create app** → *Deploy a public app from GitHub* → repo `<you>/squatch-signal`,
   branch `main`, main file `app.py`.
3. **Advanced settings**: choose Python 3.12 and paste your Supabase values under **Secrets** (publishable key only, never the service-role key):
   ```toml
   SUPABASE_URL = "https://xxxx.supabase.co"
   SUPABASE_PUBLISHABLE_KEY = "sb_publishable_xxxx"
   ```
4. Click **Deploy** (first build takes a couple of minutes). Visitors can use **Continue as guest** with no login.
5. In Supabase → **Authentication → URL Configuration**, set **Site URL** to your `https://<subdomain>.streamlit.app` address so
   confirmation emails link back to the deployed app instead of localhost.
6. Test: sign in, run **Discover prospects** on a site, then confirm new rows in Supabase → Table Editor → `discovered_leads`.

**Updating the app** — edit locally, then `git add . && git commit -m "message" && git push`. Streamlit Cloud redeploys automatically.
Change secrets under the app's **Settings → Secrets** (no code change or push needed).

**Free-tier behaviour (can change; see Streamlit docs)**
* Apps with no traffic for about 12 hours go to sleep; the next visitor clicks a wake-up button. The in-memory 1-hour cache and guest data are lost on sleep.
* Roughly 1 GB memory shared limits, unlimited public apps, one private app, no custom domain, `*.streamlit.app` URL.
* Discovery runs from Streamlit's shared servers; a few sites may return HTTP 403 to cloud IP ranges (the Internet Archive fallback usually still returns a labelled copy).
* Free Supabase projects can pause when idle. If sign-in suddenly fails, restore the project from the Supabase dashboard.

## Supabase setup (optional) and verification
1. Create a free project at <https://supabase.com>. Under **Authentication → Sign In / Providers** keep **Email** enabled
   (for quick demos you may switch **Confirm email** off; otherwise new users must click a confirmation link first).
2. Copy `.env.example` to **`.env`** and fill in `SUPABASE_URL` (Settings → API → Project URL) and
   `SUPABASE_PUBLISHABLE_KEY` (the `sb_publishable_...` or legacy anon key). The app reads only `.env`.
   Never use the secret or `service_role` key, and never commit `.env`.
3. Open **SQL Editor**, paste all of `supabase_schema.sql` and run it. It is safe to re-run and creates `leads`,
   `outreach_drafts` and `discovered_leads` with row-level security.
4. Verify: `python check_supabase.py` — it prints OK / WARN / FAIL for URL, key, auth, and all three tables (never your key).
5. Start the app, **sign in or create an account** (do *not* use guest mode), run **Discover prospects**, then look at
   Supabase → **Table Editor → `discovered_leads`**: new rows appear with your `user_id`.
6. On Streamlit Cloud put the same two values in **Settings → Secrets** instead of a file.

To inspect your whole database layout at any time, run the one-query schema report in `WORKING_GUIDE.pdf` (Section 8).

## Tests (offline, no internet needed)
```bash
python -m unittest -v      # 21 tests, ~8 seconds
```
`test_lead_discovery.py` spins up a local multi-site fake web and checks crawling, JSON-LD, emails, robots.txt, charset handling,
blocked sites, caching, parallelism and SSRF protection. `test_app.py` drives the real UI headlessly:
guest start → discover → save → dedupe → leads → outreach draft → add lead → overview.

## Troubleshooting
* **`DLL load failed ... An Application Control policy has blocked this file`** — your PC's AppLocker/WDAC policy is blocking a compiled file in `.venv`. The app no longer needs pandas for its tables, so the pandas error is gone. If another package is blocked, install Python system-wide (e.g. under `C:\Program Files`), recreate `.venv` from that Python, or run from an allowed folder / ask your IT admin. Deploying to Streamlit Cloud avoids the problem entirely.
* **Discovered rows are not in Supabase** — you are in guest mode (guests never touch Supabase), the `.env` values are missing, or
  `discovered_leads` has no row-level-security policy. Sign in with a real account and re-run `supabase_schema.sql`.
  A yellow *"Could not store discovery history: …"* banner shows the exact database error.
* **`ERROR 42710: policy "..." already exists`** — you ran a `create policy` without dropping it first. Use `supabase_schema.sql`
  (it uses `drop policy if exists` before each `create policy`) or run `drop policy if exists "<name>" on public.<table>;` first.
* **`ERROR 55000: ... is already an identity column`** — harmless; that column is already set up. Remove that line and run the rest.
* **`new row violates row-level security policy` (42501)** — you are not signed in, or the table has RLS on but no policy. Re-run the schema.
* **`violates check constraint`** — `email_status` must be Verified / Unverified / Missing, `fit_score` 0–100, draft `status` draft / reviewed / sent / archived.
* **Account created but cannot sign in** — email confirmation is on: click the link in the email, or turn off *Confirm email* in Supabase.
* **Deployed app is slow to open / shows a "wake up" page** — it slept after about 12 idle hours. Click the button and wait for it to restart.
* **Confirmation email opens localhost** — set Supabase **Site URL** to your `.streamlit.app` address (Authentication → URL Configuration).
* **App works locally but fails on Streamlit Cloud** — open **Manage app → Logs**; check that Secrets are pasted as TOML with quotes and that Python 3.12 is selected.
* **Port 8501 busy** — `streamlit run app.py --server.port 8502`.
* **A site says HTTP 403** — it blocks automated clients; the app reports it instead of bypassing it.

## Honest limitations
* Reads server-rendered HTML. JavaScript-only sites (some SPAs) expose little; big brands that block bots (HTTP 403) return an error or, if enabled, a clearly labelled archived copy.
* Industry / signals / headcount are keyword heuristics, not AI predictions. Email checks prove the *domain* can receive mail, not that the mailbox exists.
* Not a people database: it finds what a company publishes about itself.
* Use responsibly: follow site terms, privacy law and anti-spam rules; a human should review every outreach message.
