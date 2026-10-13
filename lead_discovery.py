"""Squatch Signal - keyless public-website lead discovery.

No API keys, no paid services. For every company website you submit the engine:

1. Checks robots.txt and only reads pages the site allows for automated clients.
2. Reads the home page, then follows the site's own links to its contact,
   about, careers and news pages (a small, polite, bounded crawl).
3. Parses structured data (JSON-LD), meta tags and *content* blocks only -
   navigation, footers and scripts are ignored for signals / industry.
4. Extracts: clean company name, industry, employee count, public emails
   (incl. Cloudflare-protected and "name [at] domain" forms), phone, LinkedIn,
   and sentence-level business signals (hiring, funding, launches, expansion...).
5. Checks that each email's domain can receive mail (DNS MX lookup - free).
6. Runs several companies in parallel and caches results for an hour.

It never bypasses logins, CAPTCHAs, rate limits or robots.txt. If a site blocks
automated access (HTTP 403/429) the engine can optionally fall back to the
public Internet Archive copy of the home page and labels the result as archived.
"""
from __future__ import annotations

import difflib
import html as htmllib
import ipaddress
import json
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import requests

try:  # optional: better MX lookups. Falls back to a plain A-record check.
    import dns.exception
    import dns.resolver
except ImportError:  # pragma: no cover
    dns = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
USER_AGENT = "SquatchSignalBot/3.0 (+public website lead research; honours robots.txt)"
MAX_BYTES = 1_500_000
TIMEOUT = (4, 10)
MAX_REDIRECTS = 4
MAX_EXTRA_PAGES = 4
DOMAIN_BUDGET_SECONDS = 40
CACHE_TTL_SECONDS = 3600
MAX_WORKERS = 4
REDIRECT_CODES = {301, 302, 303, 307, 308}
# Tests run against a local server; production never sets this.
ALLOW_PRIVATE_HOSTS = os.getenv("SQUATCH_ALLOW_PRIVATE") == "1"

SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe"}
CHROME_TAGS = {"nav", "footer", "aside", "form", "select", "button"}
BLOCK_TAGS = {
    "p", "div", "section", "article", "li", "ul", "ol", "tr", "td", "th", "dd", "dt",
    "h1", "h2", "h3", "h4", "h5", "blockquote", "figcaption", "main", "header", "br",
}
HEADING_TAGS = {"h1", "h2", "h3", "h4"}

EMAIL_PATTERN = re.compile(
    r"[A-Z0-9._%+\-]{1,64}@[A-Z0-9](?:[A-Z0-9\-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9\-]{0,61}[A-Z0-9])?)+",
    re.IGNORECASE,
)
OBFUSCATED_AT = re.compile(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", re.IGNORECASE)
OBFUSCATED_DOT = re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.IGNORECASE)
JUNK_EMAIL_DOMAINS = {
    "example.com", "example.org", "domain.com", "yourdomain.com", "email.com", "sentry.io",
    "sentry-next.wixpress.com", "wixpress.com", "godaddy.com", "test.com", "mysite.com",
}
JUNK_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "js", "css", "woff", "woff2", "ico", "map"}
LOW_VALUE_LOCALS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "privacy", "abuse", "legal", "dpo",
    "unsubscribe", "postmaster", "webmaster", "careers", "jobs", "hr", "recruiting", "press",
    "media", "support", "help", "billing", "security",
}
BEST_LOCALS = ("sales", "partnerships", "partners", "business", "hello", "contact", "enquiries", "inquiries")

PAGE_GROUPS = {
    "contact": re.compile(r"(^|/)(contact|contact-us|contactus|get-in-touch|reach-us)(/|$|\.)", re.I),
    "careers": re.compile(r"(^|/)(careers?|jobs|join-us|join-our-team|work-with-us|open-positions)(/|$|\.)", re.I),
    "about": re.compile(r"(^|/)(about|about-us|company|who-we-are|our-story)(/|$|\.)", re.I),
    "news": re.compile(r"(^|/)(news|newsroom|press|press-releases?|blog|media|announcements)(/|$|\.)", re.I),
}
CRAWL_ORDER = ["contact", "careers", "about", "news"]

INDUSTRY_TERMS: dict[str, dict[str, int]] = {
    "Software / SaaS": {
        "saas": 3, "software": 2, "api": 1, "developer": 1, "crm": 2, "cloud platform": 2,
        "cloud": 1, "automation": 1, "analytics": 1, "machine learning": 1,
        "artificial intelligence": 1, "no-code": 2, "workflow": 1,
    },
    "Cybersecurity": {
        "cybersecurity": 3, "cyber security": 3, "threat": 2, "zero trust": 3, "endpoint": 2,
        "ransomware": 2, "vulnerability": 2, "identity security": 3, "security platform": 2,
    },
    "IT Services & Consulting": {
        "it services": 3, "digital transformation": 2, "consulting": 2, "systems integrator": 3,
        "managed services": 2, "outsourcing": 3, "technology services": 3, "engineering services": 2,
    },
    "Fintech & Financial Services": {
        "fintech": 3, "payments": 2, "payment gateway": 3, "banking": 2, "bank": 1, "insurance": 2,
        "lending": 2, "wealth": 1, "financial services": 3, "investment": 1,
    },
    "Healthcare & Life Sciences": {
        "healthcare": 3, "hospital": 2, "clinical": 2, "patient": 2, "pharmaceutical": 3,
        "biotech": 3, "medical": 2, "health": 1, "diagnostics": 2, "telehealth": 3,
    },
    "Manufacturing & Industrial": {
        "manufacturing": 3, "factory": 2, "industrial": 2, "production line": 3, "machinery": 2,
        "automotive": 2, "fabrication": 2, "supply chain": 1,
    },
    "Retail & E-commerce": {
        "e-commerce": 3, "ecommerce": 3, "retail": 2, "online store": 3, "consumer goods": 2,
        "shopify": 2, "marketplace": 1, "storefront": 2,
    },
    "Education & EdTech": {
        "edtech": 3, "university": 2, "school": 1, "learning platform": 3, "students": 2,
        "curriculum": 2, "education": 2, "courses": 1,
    },
    "Logistics & Supply Chain": {
        "logistics": 3, "freight": 3, "shipping": 2, "transportation": 2, "fleet": 2,
        "warehouse": 2, "supply chain": 2, "last mile": 3,
    },
    "Professional Services": {
        "staffing": 3, "recruitment": 3, "recruiting": 2, "advisory": 2, "law firm": 3,
        "accounting": 3, "professional services": 3, "agency": 1,
    },
    "Marketing & Media": {
        "marketing": 2, "advertising": 3, "seo": 2, "content marketing": 3, "media": 1,
        "branding": 2, "email marketing": 3, "social media": 1,
    },
    "Energy & Utilities": {
        "energy": 2, "renewable": 3, "solar": 3, "utilities": 3, "oil and gas": 3,
        "wind power": 3, "power generation": 3, "electric vehicle": 1,
    },
    "Real Estate & Construction": {
        "real estate": 3, "construction": 3, "property management": 3, "architecture": 2,
        "commercial property": 3, "homes": 1,
    },
    "Travel & Hospitality": {
        "hotel": 3, "hospitality": 3, "travel": 2, "booking": 1, "tourism": 3, "restaurant": 2,
    },
}
_INDUSTRY_RE = {
    label: [(re.compile(r"\b" + re.escape(term) + r"\b", re.I), weight) for term, weight in terms.items()]
    for label, terms in INDUSTRY_TERMS.items()
}

SIGNAL_PATTERNS = [
    ("Funding", re.compile(r"\b(raised|raises|funding|series [a-e]\b|seed round|backed by|investment from|ipo|valuation)\b", re.I)),
    ("Hiring", re.compile(r"\b(we(?:'re| are) hiring|now hiring|open (?:positions|roles)|current openings|join our team|join us|hiring)\b", re.I)),
    ("Expansion", re.compile(r"\b(expand(?:s|ed|ing)?|expansion|new office|opens? (?:a )?new|new market|new (?:data cent(?:er|re)|headquarters|region)|scal(?:e|ing) globally)\b", re.I)),
    ("Launch", re.compile(r"\b(launch(?:ed|es)|introducing|unveil(?:s|ed)?|announces?|announced|now available|new product|release[sd]?)\b", re.I)),
    ("Partnership", re.compile(r"\b(partner(?:s|ship|ed)? with|acquir(?:es|ed|ition)|merger|strategic alliance|joins? forces)\b", re.I)),
    ("Initiative", re.compile(r"\b(digital transformation|modernization|ai-powered|ai-driven|automation|transform(?:ing)? how)\b", re.I)),
]
EMPLOYEE_RE = re.compile(
    r"(\d{1,3}(?:[,\.]\d{3})+|\d+(?:\.\d+)?)\s*([kKmM])?\s*\+?\s*"
    r"(?:global\s+|full[- ]time\s+|talented\s+|dedicated\s+)?"
    r"(employees|team members|people worldwide|professionals|associates|colleagues|staff members)\b",
    re.IGNORECASE,
)
GENERIC_NAMES = {"home", "homepage", "welcome", "index", "official site", "official website", "main page"}

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_robots_cache: dict[str, robotparser.RobotFileParser | None] = {}
_robots_lock = threading.Lock()
_mx_cache: dict[str, str] = {}


# --------------------------------------------------------------------------
# URL helpers and safe fetching
# --------------------------------------------------------------------------
def _valid_email(email: str) -> bool:
    if not isinstance(email, str):
        return False
    email = email.strip()
    return 0 < len(email) <= 254 and EMAIL_PATTERN.fullmatch(email) is not None


def normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Enter a website URL.")
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        if not re.match(r"^https?://", raw, re.IGNORECASE):
            raise ValueError("Only public HTTP/HTTPS website URLs are supported.")
    else:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid website URL or port.") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise ValueError("Only public HTTP/HTTPS website URLs are supported.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing usernames or passwords are not allowed.")
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("Invalid website port.")
    return urldefrag(raw)[0]


def _domain_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _same_site(host_a: str, host_b: str) -> bool:
    a, b = host_a.lower().removeprefix("www."), host_b.lower().removeprefix("www.")
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _validate_public_host(url: str) -> None:
    """Reject local, private, reserved and non-global targets (SSRF protection)."""
    if ALLOW_PRIVATE_HOSTS:
        return
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise ValueError("Invalid website URL or port.") from exc
    if not host:
        raise ValueError("Website URL has no hostname.")
    if host.lower().rstrip(".") in {"localhost", "localhost.localdomain"}:
        raise ValueError("Local or private network URLs are not allowed.")
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve host: {host}") from exc
    if not records:
        raise ValueError("Host did not resolve to an address.")
    for record in records:
        address = record[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError("Could not validate the website IP address.") from exc
        if not ip.is_global:
            raise ValueError("Private, local, or reserved IP targets are not allowed.")


class FetchError(ValueError):
    """A fetch failure carrying the HTTP status (0 for network errors)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _charset(content_type: str, head: bytes) -> str:
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    if not match:
        match = re.search(rb"<meta[^>]+charset=[\"']?([\w\-]+)", head[:4096], re.I)
        if match:
            return match.group(1).decode("ascii", "ignore")
        return "utf-8"
    return match.group(1)


def _fetch(url: str, *, html_only: bool = True, max_bytes: int = MAX_BYTES) -> tuple[str, str, str]:
    """Return (final_url, content_type, text). Retries once on transient errors."""
    last: Exception | None = None
    for attempt in range(2):
        try:
            return _fetch_once(url, html_only=html_only, max_bytes=max_bytes)
        except FetchError as exc:
            last = exc
            transient = exc.status == 0 or exc.status >= 500
            if attempt == 0 and transient:
                time.sleep(0.8)
                continue
            raise
    raise last or FetchError("Could not fetch website.")


def _fetch_once(url: str, *, html_only: bool, max_bytes: int) -> tuple[str, str, str]:
    current = normalize_url(url)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1" if html_only else "*/*",
        "Accept-Language": "en;q=0.9",
    }
    with requests.Session() as session:
        for hop in range(MAX_REDIRECTS + 1):
            _validate_public_host(current)
            try:
                response = session.get(current, headers=headers, timeout=TIMEOUT, allow_redirects=False, stream=True)
            except requests.Timeout as exc:
                raise FetchError("Website request timed out.") from exc
            except requests.RequestException as exc:
                raise FetchError(f"Could not fetch website: {str(exc)[:160]}") from exc
            status = response.status_code
            if status in REDIRECT_CODES:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise FetchError("Website returned a redirect without a target.", status)
                if hop == MAX_REDIRECTS:
                    raise FetchError("Maximum redirect limit reached.", status)
                current = normalize_url(urljoin(current, location))
                continue
            if status == 429:
                response.close()
                raise FetchError("Website rate-limited the request (HTTP 429).", status)
            if status in {401, 403, 407}:
                response.close()
                raise FetchError(f"Website denied automated access (HTTP {status}).", status)
            if status >= 400:
                response.close()
                raise FetchError(f"Website returned HTTP {status}.", status)
            content_type = (response.headers.get("Content-Type") or "").lower()
            if html_only and "html" not in content_type:
                response.close()
                raise FetchError(f"URL did not return HTML (Content-Type: {content_type or 'unknown'}).", status)
            chunks, size = [], 0
            try:
                for chunk in response.iter_content(chunk_size=16384):
                    if not chunk:
                        continue
                    remaining = max_bytes - size
                    if len(chunk) >= remaining:
                        chunks.append(chunk[:remaining])
                        break
                    chunks.append(chunk)
                    size += len(chunk)
            except requests.RequestException as exc:
                raise FetchError("Failed while reading website response.") from exc
            finally:
                response.close()
            raw = b"".join(chunks)
            try:
                text = raw.decode(_charset(content_type, raw), errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            return current, content_type, text
    raise FetchError("Could not fetch website.")


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------
def _robots_for(url: str) -> robotparser.RobotFileParser | None:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    with _robots_lock:
        if origin in _robots_cache:
            return _robots_cache[origin]
    parser: robotparser.RobotFileParser | None
    try:
        _, _, body = _fetch(origin + "/robots.txt", html_only=False, max_bytes=200_000)
        parser = robotparser.RobotFileParser()
        parser.parse(body.splitlines())
    except FetchError:
        # 4xx / unreachable robots.txt = no published restrictions (RFC 9309).
        parser = None
    with _robots_lock:
        _robots_cache[origin] = parser
    return parser


def robots_allows(url: str) -> bool:
    parser = _robots_for(url)
    if parser is None:
        return True
    return parser.can_fetch(USER_AGENT, url)


# --------------------------------------------------------------------------
# HTML parsing
# --------------------------------------------------------------------------
def _decode_cfemail(encoded: str) -> str:
    try:
        key = int(encoded[:2], 16)
        return "".join(chr(int(encoded[i:i + 2], 16) ^ key) for i in range(2, len(encoded), 2))
    except ValueError:
        return ""


def _deobfuscate(text: str) -> str:
    text = OBFUSCATED_AT.sub("@", text)
    return OBFUSCATED_DOT.sub(".", text)


class _PageParser(HTMLParser):
    """Collects metadata, content blocks (no nav/footer), links, contacts, JSON-LD."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.blocks: list[tuple[str, str]] = []  # (kind 'h' | 'p', text) - content only
        self.contact_text: list[str] = []        # all visible text incl. footer, for emails
        self.links: list[tuple[str, str]] = []
        self.emails: set[str] = set()
        self.phones: set[str] = set()
        self.jsonld_raw: list[str] = []
        self._in_title = False
        self._in_jsonld = False
        self._skip = 0
        self._chrome = 0
        self._cur: list[str] = []
        self._cur_kind = "p"
        self._a_href: str | None = None
        self._a_text: list[str] = []

    # -- helpers -----------------------------------------------------------
    def _flush(self):
        text = " ".join(" ".join(self._cur).split())
        self._cur = []
        if text and self._chrome == 0 and self._skip == 0:
            self.blocks.append((self._cur_kind, text))
        self._cur_kind = "p"

    @property
    def text(self) -> str:
        return " ".join(t for _, t in self.blocks)

    # -- parser hooks --------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = (tag or "").lower()
        a = {str(k).lower(): (v or "").strip() for k, v in attrs if k is not None}
        if tag == "title":
            self._in_title = True
        if tag == "script":
            if "ld+json" in a.get("type", "").lower():
                self._in_jsonld = True
                self.jsonld_raw.append("")
        if tag in SKIP_TAGS:
            self._skip += 1
        if tag in CHROME_TAGS:
            self._flush()
            self._chrome += 1
        if tag in BLOCK_TAGS:
            self._flush()
            if tag in HEADING_TAGS:
                self._cur_kind = "h"
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content"):
                self.meta[key] = a["content"]
        if "data-cfemail" in a:
            email = _decode_cfemail(a["data-cfemail"]).lower()
            if _valid_email(email):
                self.emails.add(email)
        if tag == "a":
            href = a.get("href", "")
            low = href.lower()
            if low.startswith("mailto:"):
                email = htmllib.unescape(href[7:]).split("?", 1)[0].strip().lower()
                if _valid_email(email):
                    self.emails.add(email)
            elif low.startswith("tel:"):
                phone = re.sub(r"[^\d+]", "", href[4:])
                if 7 <= len(re.sub(r"\D", "", phone)) <= 15:
                    self.phones.add(phone)
            elif href and not low.startswith(("javascript:", "#", "data:")):
                self._a_href, self._a_text = href, []

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if tag == "title":
            self._in_title = False
        if tag == "script" and self._in_jsonld:
            self._in_jsonld = False
        if tag in SKIP_TAGS and self._skip > 0:
            self._skip -= 1
        if tag in BLOCK_TAGS or tag in HEADING_TAGS:
            self._flush()
        if tag in CHROME_TAGS and self._chrome > 0:
            self._flush()
            self._chrome -= 1
        if tag == "a" and self._a_href is not None:
            self.links.append((self._a_href, " ".join(" ".join(self._a_text).split())))
            self._a_href = None

    def handle_data(self, data):
        if self._in_jsonld:
            self.jsonld_raw[-1] += data
            return
        text = " ".join((data or "").split())
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        if self._skip:
            return
        self._cur.append(text)
        if self._a_href is not None:
            self._a_text.append(text)
        self.contact_text.append(text)

    def close(self):
        super().close()
        self._flush()
        blob = _deobfuscate(" ".join(self.contact_text))
        for found in EMAIL_PATTERN.findall(blob):
            email = found.strip(".,;:()[]{}<>\"'").lower()
            if _valid_email(email):
                self.emails.add(email)


def _parse_jsonld(raw_blocks: list[str]) -> list[dict]:
    """Flatten JSON-LD into a list of dict nodes."""
    nodes: list[dict] = []

    def walk(obj):
        if isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, dict):
            nodes.append(obj)
            for key in ("@graph", "mainEntity", "publisher", "provider", "author"):
                if key in obj:
                    walk(obj[key])

    for raw in raw_blocks:
        try:
            walk(json.loads(raw.strip()))
        except (ValueError, TypeError):
            continue
    return nodes


def _node_types(node: dict) -> set[str]:
    t = node.get("@type", [])
    return {x.lower() for x in ([t] if isinstance(t, str) else [str(i) for i in t])}


ORG_TYPES = {"organization", "corporation", "localbusiness", "ngo", "educationalorganization",
             "onlinebusiness", "professionalservice", "softwareapplication", "store", "medicalorganization"}


def _org_from_jsonld(nodes: list[dict]) -> dict:
    for node in nodes:
        if _node_types(node) & ORG_TYPES and (node.get("name") or node.get("legalName")):
            return node
    return {}


def _jsonld_employees(org: dict) -> int:
    value = org.get("numberOfEmployees")
    if isinstance(value, dict):
        value = value.get("value") or value.get("maxValue") or value.get("minValue")
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# Extraction logic
# --------------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _company_guess(domain: str) -> str:
    name = domain.split(":", 1)[0].split(".")[0]
    return re.sub(r"[-_]+", " ", name).strip().title() or "Unknown company"


def pick_company_name(domain: str, org_name: str, meta: dict, title: str) -> str:
    """Choose the cleanest company name: JSON-LD > og:site_name > best title segment."""
    label = _norm(domain.split(".")[0])
    candidates = []
    for value in (org_name, meta.get("og:site_name"), meta.get("application-name")):
        value = htmllib.unescape(value or "").strip()
        if value and value.lower() not in GENERIC_NAMES and len(value) <= 60:
            candidates.append(value)
    if candidates:
        return candidates[0][:80]
    segments = [s.strip() for s in re.split(r"\s*[|\-\u2013\u2014:\u00b7\u2022\u00bb\\/]+\s*", htmllib.unescape(title or "")) if s.strip()]
    segments = [s for s in segments if s.lower() not in GENERIC_NAMES and len(s) <= 60]
    best, best_score = "", 0.0
    for seg in segments:
        n = _norm(seg)
        if not n or not label:
            continue
        score = 1.0 if (label in n or n in label) and len(n) >= 3 else difflib.SequenceMatcher(None, n, label).ratio()
        if score > best_score:
            best, best_score = seg, score
    if best and best_score >= 0.6:
        return best[:80]
    return _company_guess(domain)


def _industry_scores(text: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    for label, patterns in _INDUSTRY_RE.items():
        total = 0.0
        for pattern, weight in patterns:
            count = min(len(pattern.findall(text)), 3)
            total += count * weight
        if total:
            scores[label] = total
    return scores


def infer_industry(text: str, *, min_score: float = 2) -> str:
    """Broad industry from text, or '' when evidence is too weak. Heuristic only."""
    scores = _industry_scores(text or "")
    if not scores:
        return ""
    label, top = max(scores.items(), key=lambda kv: kv[1])
    return label if top >= min_score else ""


def _classify_page_industry(primary: str, body: str) -> str:
    """Weighted: title/description/headings count 3x vs. body copy."""
    combined: dict[str, float] = {}
    for label, score in _industry_scores(primary).items():
        combined[label] = combined.get(label, 0) + score * 3
    for label, score in _industry_scores(body[:8000]).items():
        combined[label] = combined.get(label, 0) + score
    if not combined:
        return ""
    ranked = sorted(combined.items(), key=lambda kv: kv[1], reverse=True)
    label, top = ranked[0]
    if top < 4:
        return ""
    if len(ranked) > 1 and ranked[1][1] > 0 and top < ranked[1][1] * 1.15:
        # near tie: prefer the label that appears in the primary text
        primary_scores = _industry_scores(primary)
        label = max((r[0] for r in ranked[:2]), key=lambda l: primary_scores.get(l, 0))
    return label


def _parse_count(num: str, suffix: str) -> int:
    try:
        if "," in num or (num.count(".") == 1 and len(num.split(".")[1]) == 3):
            value = float(num.replace(",", "").replace(".", ""))
        else:
            value = float(num)
    except ValueError:
        return 0
    suffix = (suffix or "").lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(value)


def extract_employees(text: str) -> tuple[int, str]:
    for match in EMPLOYEE_RE.finditer(text or ""):
        count = _parse_count(match.group(1), match.group(2))
        if 2 <= count <= 2_500_000:
            start = max(0, match.start() - 20)
            return count, text[start: match.end() + 20].strip()
    return 0, ""


def _looks_like_menu(text: str) -> bool:
    words = text.split()
    if len(words) < 6:
        return True
    capitalised = sum(1 for w in words if w[:1].isupper())
    return capitalised / len(words) > 0.75 and not text.rstrip().endswith((".", "!", "?"))


_YEAR_RE = re.compile(r"\b(19\d\d|20\d\d)\b")


def _is_stale(sentence: str) -> bool:
    """History ('founded in 2014', 'acquired in 2020') is not a fresh buying signal."""
    years = [int(y) for y in _YEAR_RE.findall(sentence)]
    return bool(years) and max(years) < time.gmtime().tm_year - 1


def extract_signals(blocks: list[tuple[str, str]], page_label: str = "") -> list[tuple[str, str, str]]:
    """Return [(tag, snippet, page_label)] - one sentence-level snippet per signal type."""
    found: dict[str, tuple[str, str, str]] = {}
    for _, text in blocks:
        if len(text) < 30 or _looks_like_menu(text):
            continue
        sentences = re.split(r"(?<=[.!?])\s+", text)
        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < 30 or _looks_like_menu(sentence) or _is_stale(sentence):
                continue
            for tag, pattern in SIGNAL_PATTERNS:
                if tag not in found and pattern.search(sentence):
                    found[tag] = (tag, sentence[:220], page_label)
    return list(found.values())


def _careers_signal(blocks: list[tuple[str, str]], url: str) -> tuple[str, str, str] | None:
    jobish = re.compile(r"\b(apply|remote|full[- ]time|part[- ]time|engineer|developer|manager|designer|analyst|sales|intern)\b", re.I)
    roles = [t for _, t in blocks if 4 <= len(t) <= 110 and jobish.search(t)]
    if len(roles) >= 3:
        return ("Hiring", f"Careers page lists about {len(roles)} role-related entries", url)
    return None


def _rank_emails(emails: set[str], domain: str) -> list[str]:
    cleaned = []
    for email in emails:
        local, _, host = email.partition("@")
        tld = host.rsplit(".", 1)[-1]
        if host in JUNK_EMAIL_DOMAINS or tld in JUNK_TLDS or len(local) > 40:
            continue
        if re.search(r"\d{6,}", local):
            continue
        cleaned.append(email)

    def rank(email: str):
        local, _, host = email.partition("@")
        base = local.split("+")[0].lower()
        same = _same_site(host, domain)
        if base in LOW_VALUE_LOCALS:
            quality = 3
        elif base in BEST_LOCALS:
            quality = 1
        elif base in {"info", "admin", "office", "team"}:
            quality = 2
        else:
            quality = 0  # a named person
        return (0 if same else 1, quality, email)

    return sorted(set(cleaned), key=rank)


def _mx_status(domain: str) -> str:
    """'ok' (domain can receive mail), 'none' (definitely cannot), 'unknown'."""
    domain = (domain or "").lower()
    if domain in _mx_cache:
        return _mx_cache[domain]
    status = "unknown"
    try:
        if dns is not None:
            resolver = dns.resolver.Resolver()
            resolver.lifetime = 3.0
            try:
                answers = resolver.resolve(domain, "MX")
                status = "ok" if len(answers) else "none"
            except dns.resolver.NoAnswer:
                try:  # RFC 5321: fall back to the implicit A/AAAA record
                    resolver.resolve(domain, "A")
                    status = "ok"
                except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                    status = "none"
            except dns.resolver.NXDOMAIN:
                status = "none"
        else:  # pragma: no cover
            socket.getaddrinfo(domain, None)
            status = "ok"
    except socket.gaierror:  # pragma: no cover
        status = "none"
    except Exception:  # network down, timeouts, SERVFAIL: do not penalise the lead
        status = "unknown"
    _mx_cache[domain] = status
    return status


def _extract_links(links: list[tuple[str, str]], base_url: str, domain: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Return ({group: [candidate urls]}, socials) from page links."""
    internal: dict[str, list[str]] = {}
    socials: dict[str, str] = {}
    for href, _label in links:
        try:
            absolute = urldefrag(urljoin(base_url, htmllib.unescape(href)))[0]
            parsed = urlparse(absolute)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        host = parsed.hostname.lower()
        if "linkedin.com" in host and "/company/" in parsed.path and "linkedin" not in socials:
            socials["linkedin"] = absolute.split("?")[0]
        elif ("twitter.com" in host or host == "x.com") and "twitter" not in socials and len(parsed.path.strip("/").split("/")) == 1 and parsed.path.strip("/"):
            socials["twitter"] = absolute.split("?")[0]
        if not _same_site(host, domain):
            continue
        path = parsed.path or "/"
        if path.count("/") > 3:
            continue
        for group, pattern in PAGE_GROUPS.items():
            if pattern.search(path):
                bucket = internal.setdefault(group, [])
                if absolute not in bucket and len(bucket) < 3:
                    bucket.append(absolute)
                break
    return internal, socials


def _parse_html(html: str) -> _PageParser:
    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # malformed markup should not kill the batch
        raise ValueError(f"Could not parse website HTML: {str(exc)[:150]}") from exc
    return parser


def _wayback_snapshot(domain: str) -> tuple[str, str, str, str] | None:
    """Latest public Internet Archive copy of the home page, or None."""
    try:
        _, _, body = _fetch(f"https://archive.org/wayback/available?url={domain}", html_only=False, max_bytes=100_000)
        closest = (json.loads(body).get("archived_snapshots") or {}).get("closest") or {}
        if not closest.get("available") or not closest.get("url"):
            return None
        snap_url = closest["url"].replace("http://", "https://", 1)
        final, ctype, html = _fetch(snap_url)
        stamp = str(closest.get("timestamp", ""))
        date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(stamp) >= 8 else "unknown date"
        return final, ctype, html, date
    except (FetchError, ValueError):
        return None


# --------------------------------------------------------------------------
# Per-company extraction
# --------------------------------------------------------------------------
def _short(path_url: str) -> str:
    p = urlparse(path_url)
    return (p.path or "/") if p.path not in ("", "/") else "home"


def extract_company(raw_url: str, *, deep: bool = True, delay_seconds: float = 0.4, archive_fallback: bool = True) -> dict:
    """Build one lead record from a company website. Raises ValueError on failure."""
    started = time.monotonic()
    normalized = normalize_url(raw_url)
    domain = _domain_from_url(normalized)
    archived_date = ""
    notes: list[str] = []

    if not robots_allows(normalized):
        raise ValueError("robots.txt asks automated clients not to read this page, so it was skipped.")
    try:
        final_url, content_type, html = _fetch(normalized)
    except FetchError as exc:
        if archive_fallback and (exc.status in {403, 429} or exc.status >= 500 or exc.status == 0):
            snap = _wayback_snapshot(domain)
            if snap is None:
                raise ValueError(f"{exc} No public archived copy was found. Use a company-provided source or CSV import.") from exc
            final_url, content_type, html, archived_date = snap
            notes.append(f"Direct access failed ({exc}); used the public Internet Archive copy from {archived_date} (may be outdated).")
        else:
            raise

    home = _parse_html(html)
    pages = [("home", final_url, home)]
    internal, socials = _extract_links(home.links, final_url, domain)

    if deep and not archived_date:
        fetched_groups = 0
        for group in CRAWL_ORDER:
            if fetched_groups >= MAX_EXTRA_PAGES:
                break
            candidates = list(internal.get(group, []))
            if group in {"contact", "about"}:
                candidates.append(urljoin(final_url, f"/{group}"))  # conventional path as a last resort
            for url in dict.fromkeys(candidates):
                if url == final_url or url in [u for _, u, _ in pages]:
                    continue
                if time.monotonic() - started > DOMAIN_BUDGET_SECONDS:
                    notes.append("Time budget reached; remaining pages skipped.")
                    break
                try:
                    if not robots_allows(url):
                        notes.append(f"{group} page skipped (robots.txt).")
                        continue
                    if delay_seconds:
                        time.sleep(min(float(delay_seconds), 5.0))
                    page_url, _, page_html = _fetch(url)
                    pages.append((group, page_url, _parse_html(page_html)))
                    fetched_groups += 1
                    break  # got this group; move on
                except (FetchError, ValueError):
                    continue  # optional pages: a 404 here is normal; try next candidate

    # ---- merge --------------------------------------------------------------
    nodes = [n for _, _, p in pages for n in _parse_jsonld(p.jsonld_raw)]
    org = _org_from_jsonld(nodes)
    title = " ".join(home.title_parts).strip()
    meta = home.meta
    description = (org.get("description") or meta.get("description") or meta.get("og:description")
                   or meta.get("twitter:description") or "").strip()
    if not isinstance(description, str):
        description = ""
    company = pick_company_name(domain, str(org.get("name") or org.get("legalName") or ""), meta, title)

    headings = " ".join(t for k, t in home.blocks if k == "h")
    primary = " ".join([title, description, headings, str(org.get("description") or "")])
    body = " ".join(p.text for _, _, p in pages)
    industry = _classify_page_industry(primary, body)

    employees = _jsonld_employees(org)
    employees_note = "structured data on the site" if employees else ""
    if not employees:
        for label, _, page in pages:
            employees, snippet = extract_employees(page.text)
            if employees:
                employees_note = f'{label} page: "{snippet}"'
                break

    signals: list[tuple[str, str, str]] = []
    seen_tags: set[str] = set()
    for label, url, page in pages:
        candidates = extract_signals(page.blocks, _short(url))
        if label == "careers":
            careers = _careers_signal(page.blocks, _short(url))
            if careers:
                candidates.insert(0, careers)
        for item in candidates:
            if item[0] not in seen_tags:
                seen_tags.add(item[0])
                signals.append(item)
    priority = {tag: i for i, (tag, _) in enumerate(SIGNAL_PATTERNS)}
    signals.sort(key=lambda s: priority.get(s[0], 99))
    signals = signals[:4]

    all_emails: set[str] = set()
    email_pages: dict[str, str] = {}
    for label, url, page in pages:
        for email in page.emails:
            all_emails.add(email)
            email_pages.setdefault(email, _short(url))
    if org.get("email"):
        em = str(org["email"]).replace("mailto:", "").strip().lower()
        if _valid_email(em):
            all_emails.add(em)
            email_pages.setdefault(em, "structured data")
    ranked = _rank_emails(all_emails, domain)

    email, email_check, email_status = "", "", "Missing"
    dropped = []
    for candidate in ranked:
        status = _mx_status(candidate.partition("@")[2])
        if status == "none":
            dropped.append(candidate)
            continue
        email = candidate
        email_status = "Unverified"
        email_check = {"ok": "domain has mail servers (MX found)", "unknown": "domain mail check unavailable"}[status]
        break
    if dropped:
        notes.append("Ignored email(s) whose domain cannot receive mail: " + ", ".join(dropped[:3]))

    phones = sorted({p for _, _, page in pages for p in page.phones})
    phone = phones[0] if phones else str(org.get("telephone") or "").strip()
    address = org.get("address")
    location = ""
    if isinstance(address, dict):
        location = ", ".join(str(address.get(k)) for k in ("addressLocality", "addressRegion", "addressCountry") if address.get(k))
    linkedin = socials.get("linkedin", "")
    if not linkedin:
        for same in (org.get("sameAs") or []) if isinstance(org.get("sameAs"), list) else []:
            if "linkedin.com/company" in str(same):
                linkedin = str(same)
                break

    # ---- evidence & quality ---------------------------------------------------
    pages_read = [_short(u) for _, u, _ in pages]
    evidence_parts = [f"Source: {final_url}", "Pages read: " + ", ".join(pages_read)]
    if archived_date:
        evidence_parts.append(f"ARCHIVED COPY dated {archived_date}")
    if title:
        evidence_parts.append(f"Page title: {title[:120]}")
    if description:
        evidence_parts.append(f"Description: {description[:300]}")
    if employees:
        evidence_parts.append(f"Employees ({employees:,}) from {employees_note}")
    if email:
        evidence_parts.append(f"Email {email} published on {email_pages.get(email, 'site')}; {email_check}")
    for tag, snippet, where in signals:
        evidence_parts.append(f"Signal [{tag}] on {where}: {snippet}")
    if phone:
        evidence_parts.append(f"Phone: {phone}")
    if linkedin:
        evidence_parts.append(f"LinkedIn: {linkedin}")
    evidence_parts.extend(notes)

    quality = sum([
        25 if (org.get("name") or meta.get("og:site_name")) else 10,
        15 if description else 0,
        15 if industry else 0,
        20 if email else 0,
        10 if signals else 0,
        10 if employees else 0,
        5 if len(pages) > 1 else 0,
    ])
    return {
        "company": company,
        "domain": _domain_from_url(final_url) if not archived_date else domain,
        "email": email,
        "email_status": email_status,
        "industry": industry,
        "employees": employees,
        "signals": " | ".join(f"{tag}: {snippet}" for tag, snippet, _ in signals),
        "signal_tags": [tag for tag, _, _ in signals],
        "evidence": " | ".join(evidence_parts)[:4000],
        "source_url": normalized if archived_date else final_url,
        "page_title": title,
        "description": description[:1000],
        "all_emails": ranked[:10],
        "email_check": email_check,
        "phone": phone,
        "linkedin": linkedin,
        "location": location,
        "pages_crawled": pages_read,
        "content_type": content_type,
        "discovery_method": "archived_snapshot" if archived_date else ("multi_page_crawl" if len(pages) > 1 else "home_page"),
        "data_quality": min(100, quality),
        "notes": notes,
    }


# --------------------------------------------------------------------------
# Batch API used by the app
# --------------------------------------------------------------------------
def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def discover_urls(
    raw_urls: list[str],
    delay_seconds: float = 0.4,
    *,
    deep: bool = True,
    use_cache: bool = True,
    archive_fallback: bool = True,
    max_workers: int = MAX_WORKERS,
) -> tuple[list[dict], list[dict]]:
    """Discover lead details from user-supplied company websites. No API keys.

    Returns (results, errors). Order of results follows the input order.
    Different companies run in parallel; pages of one company are fetched
    sequentially with a polite delay.
    """
    jobs: list[tuple[str, str, str]] = []
    errors: list[dict] = []
    seen: set[str] = set()
    for raw in raw_urls:
        if not isinstance(raw, str) or not raw.strip():
            continue
        original = raw.strip()
        try:
            normalized = normalize_url(original)
            domain = _domain_from_url(normalized)
        except ValueError as exc:
            errors.append({"url": original, "error": str(exc)[:300]})
            continue
        if domain in seen:
            errors.append({"url": original, "error": "Duplicate domain in the submitted list."})
            continue
        seen.add(domain)
        jobs.append((original, normalized, domain))

    def work(job):
        original, normalized, domain = job
        key = f"{normalized}|{deep}|{archive_fallback}"
        if use_cache:
            with _cache_lock:
                hit = _cache.get(key)
            if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
                return original, dict(hit[1], from_cache=True), None
        try:
            record = extract_company(normalized, deep=deep, delay_seconds=delay_seconds, archive_fallback=archive_fallback)
        except Exception as exc:  # one bad site must not abort the batch
            return original, None, str(exc)[:500]
        if use_cache:
            with _cache_lock:
                _cache[key] = (time.time(), record)
        return original, dict(record, from_cache=False), None

    results: list[dict] = []
    if jobs:
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(jobs)))) as pool:
            for original, record, error in pool.map(work, jobs):
                if record is not None:
                    results.append(record)
                else:
                    errors.append({"url": original, "error": error})
    return results, errors
