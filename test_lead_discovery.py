"""Offline tests for the keyless discovery engine.

A tiny local web server hosts several fake company sites (selected by Host
header), so the whole pipeline - robots.txt, multi-page crawl, JSON-LD, emails,
charset handling, blocked sites - is tested end-to-end without internet access.

Run:  python -m unittest -v
Hostnames below must resolve to 127.0.0.1. The test suite maps them itself via
a custom resolver, so no /etc/hosts edits are required.
"""
import os
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

os.environ["SQUATCH_ALLOW_PRIVATE"] = "1"  # local test server only; never set in production

import lead_discovery as ld  # noqa: E402

PORT = 8765
HOSTS = {"acme-saas.test", "globex-logistics.test", "blocked-site.test", "robots-site.test", "latin1-site.test"}


def _cfemail(address: str, key: int = 0x42) -> str:
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in address)


ACME_HOME = f"""<!doctype html><html><head>
<title>Workflow automation for revenue teams | Acme</title>
<meta name="description" content="Acme is a SaaS platform that automates sales workflows with an API for developers.">
</head><body>
<nav><a href="/pricing">Pricing</a> <a href="/careers">Careers</a> <a href="/contact">Contact Us</a>
<a href="/about-us">About Us</a> <a href="/blog">Blog</a> <a href="/news">News</a> Product Launch Hiring Funding Partners</nav>
<header><h1>Automate your sales workflow with Acme SaaS</h1></header>
<main>
<p>Our SaaS software platform helps teams automate workflow and connect any API in minutes.</p>
<p>We just launched Acme Copilot, our new AI assistant for revenue teams.</p>
<p>Our team of 120 employees works across four offices worldwide.</p>
</main>
<footer>Questions? Write to sales@acme-saas.test or partners [at] acme-saas [dot] test.
<a class="__cf_email__" data-cfemail="{_cfemail('hello@acme-saas.test')}">[email protected]</a>
<a href="https://www.linkedin.com/company/acme-saas/">LinkedIn</a>
<a href="mailto:noreply@acme-saas.test">noreply</a> logo@2x.png</footer>
</body></html>"""

ACME_CONTACT = """<html><head><title>Contact</title></head><body><main>
<h2>Get in touch</h2><p>Call us on <a href="tel:+1-415-555-0100">+1 415 555 0100</a>.</p>
<p>Reach our team at jane.doe@acme-saas.test for partnerships.</p></main></body></html>"""

ACME_CAREERS = """<html><head><title>Careers</title></head><body><main>
<h1>Careers at Acme</h1><p>We're hiring across engineering and sales as we grow our team.</p>
<ul><li>Senior Backend Engineer - Remote</li><li>Account Executive - Full-time</li>
<li>Product Designer - Remote</li><li>Sales Manager - Full-time</li></ul></main></body></html>"""

ACME_NEWS = """<html><head><title>News</title></head><body><main>
<article><h2>Acme raises $20M Series B</h2>
<p>Acme raised a $20M Series B led by Foo Ventures to expand into the European market.</p></article>
</main></body></html>"""

GLOBEX_HOME = """<html><head><title>Home \\ Globex</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[{"@type":"WebSite","name":"Globex site"},
{"@type":"Organization","name":"Globex Logistics","description":"Global freight and supply chain logistics provider.",
"telephone":"+1-212-555-0199","numberOfEmployees":{"@type":"QuantitativeValue","value":850},
"address":{"@type":"PostalAddress","addressLocality":"Rotterdam","addressCountry":"NL"},
"email":"mailto:info@globex-logistics.test","sameAs":["https://www.linkedin.com/company/globex-logistics"]}]}
</script></head><body><main><h1>Freight forwarding and warehouse solutions</h1>
<p>We move freight by sea, air and road with a modern fleet and last mile delivery.</p></main></body></html>"""

LATIN1_HOME = ("<html><head><title>Café Software – Ünïcode</title></head><body><main>"
               "<p>Notre plateforme logicielle SaaS aide les équipes à automatiser le travail à Montréal.</p>"
               "<p>日本語のテキスト and software cloud API platform for developers.</p></main></body></html>")


class Handler(BaseHTTPRequestHandler):
    hits: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        Handler.hits.append((host, self.path))
        path = self.path.split("?")[0]
        if host == "acme-saas.test":
            pages = {"/": ACME_HOME, "/contact": ACME_CONTACT, "/careers": ACME_CAREERS, "/news": ACME_NEWS,
                     "/about-us": "<html><body><main><p>Founded in 2019, we build workflow software.</p></main></body></html>"}
            if path == "/robots.txt":
                return self._send(200, "User-agent: *\nDisallow: /private\n", "text/plain")
            if path in pages:
                return self._send(200, pages[path])
        elif host == "globex-logistics.test":
            if path == "/":
                return self._send(200, GLOBEX_HOME)
        elif host == "blocked-site.test":
            return self._send(403, "Forbidden")
        elif host == "robots-site.test":
            if path == "/robots.txt":
                return self._send(200, "User-agent: *\nDisallow: /\n", "text/plain")
            return self._send(200, "<html><title>Hidden</title></html>")
        elif host == "latin1-site.test":
            if path == "/":
                return self._send(200, LATIN1_HOME.encode("utf-8"), "text/html")  # no charset header
        self._send(404, "Not found")


class LeadDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        real = socket.getaddrinfo

        def fake(host, *a, **k):
            return real("127.0.0.1" if host in HOSTS else host, *a, **k)

        cls._patch = patch("socket.getaddrinfo", fake)
        cls._patch.start()
        # requests resolves via urllib3 -> socket.create_connection -> getaddrinfo (patched)
        cls._mx = patch("lead_discovery._mx_status", lambda d: "ok")
        cls._mx.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls._patch.stop()
        cls._mx.stop()

    def setUp(self):
        ld.clear_cache()
        ld._robots_cache.clear()
        Handler.hits.clear()

    def url(self, host, path="/"):
        return f"http://{host}:{PORT}{path}"

    # ---- pure helpers --------------------------------------------------------
    def test_normalize_url(self):
        self.assertEqual(ld.normalize_url("example.com/about#team"), "https://example.com/about")

    def test_reject_unsupported_scheme_and_credentials(self):
        with self.assertRaises(ValueError):
            ld.normalize_url("file:///etc/passwd")
        with self.assertRaises(ValueError):
            ld.normalize_url("https://user:pw@example.com")

    def test_industry_inference(self):
        self.assertEqual(ld.infer_industry("SaaS cloud software platform"), "Software / SaaS")
        self.assertEqual(ld.infer_industry("nothing specific"), "")
        self.assertEqual(ld.infer_industry("global freight and warehouse logistics"), "Logistics & Supply Chain")

    def test_company_name_selection(self):
        self.assertEqual(ld.pick_company_name("postman.com", "", {}, "API engineering for your agents | Postman"), "Postman")
        self.assertEqual(ld.pick_company_name("anthropic.com", "", {}, "Home \\ Anthropic"), "Anthropic")
        self.assertEqual(ld.pick_company_name("hubspot.com", "", {"og:site_name": "HubSpot"}, "x"), "HubSpot")
        self.assertEqual(ld.pick_company_name("zoho.com", "", {}, "Welcome"), "Zoho")

    def test_employee_extraction(self):
        self.assertEqual(ld.extract_employees("Over 19 K+ Employees Worldwide")[0], 19000)
        self.assertEqual(ld.extract_employees("a team of 1,200 employees")[0], 1200)
        self.assertEqual(ld.extract_employees("no counts here")[0], 0)

    def test_cloudflare_email_decoding(self):
        self.assertEqual(ld._decode_cfemail(_cfemail("hello@x.com")), "hello@x.com")

    def test_email_ranking_prefers_company_domain_and_filters_junk(self):
        ranked = ld._rank_emails({"a@sentry.io", "logo@2x.png", "noreply@acme.com", "info@acme.com", "sales@acme.com",
                                  "x@other.org", "jane@acme.com"}, "acme.com")
        self.assertEqual(ranked[0], "jane@acme.com")
        self.assertNotIn("a@sentry.io", ranked)
        self.assertNotIn("logo@2x.png", ranked)
        self.assertLess(ranked.index("sales@acme.com"), ranked.index("noreply@acme.com"))

    def test_menu_text_is_not_a_signal(self):
        blocks = [("p", "Pricing Careers Contact Us About Us Blog News Product Launch Hiring Funding Partners")]
        self.assertEqual(ld.extract_signals(blocks), [])

    # ---- end-to-end -----------------------------------------------------------
    def test_full_crawl_extracts_everything(self):
        results, errors = ld.discover_urls([self.url("acme-saas.test")], delay_seconds=0)
        self.assertEqual(errors, [])
        r = results[0]
        self.assertEqual(r["company"], "Acme")
        self.assertEqual(r["industry"], "Software / SaaS")
        self.assertEqual(r["employees"], 120)
        # best email: a named person on the company domain beats role mailboxes
        self.assertEqual(r["email"], "jane.doe@acme-saas.test")
        self.assertEqual(r["email_status"], "Unverified")
        for expected in ("sales@acme-saas.test", "partners@acme-saas.test", "hello@acme-saas.test"):
            self.assertIn(expected, r["all_emails"])
        self.assertTrue(all("2x.png" not in e for e in r["all_emails"]))
        self.assertEqual(r["phone"], "+14155550100")
        self.assertIn("linkedin.com/company/acme-saas", r["linkedin"])
        tags = set(r["signal_tags"])
        self.assertTrue({"Funding", "Hiring", "Launch"} <= tags, tags)
        self.assertNotIn("Pricing Careers", r["signals"])  # nav text never leaks in
        self.assertGreaterEqual(len(r["pages_crawled"]), 4)
        self.assertEqual(r["discovery_method"], "multi_page_crawl")
        self.assertGreater(r["data_quality"], 70)
        self.assertIn("Source: ", r["evidence"])

    def test_private_page_is_not_fetched_and_robots_is_read(self):
        ld.discover_urls([self.url("acme-saas.test")], delay_seconds=0)
        self.assertIn(("acme-saas.test", "/robots.txt"), Handler.hits)

    def test_jsonld_organization_wins(self):
        results, errors = ld.discover_urls([self.url("globex-logistics.test")], delay_seconds=0)
        self.assertEqual(errors, [])
        r = results[0]
        self.assertEqual(r["company"], "Globex Logistics")
        self.assertEqual(r["employees"], 850)
        self.assertEqual(r["industry"], "Logistics & Supply Chain")
        self.assertEqual(r["email"], "info@globex-logistics.test")
        self.assertEqual(r["location"], "Rotterdam, NL")
        self.assertIn("linkedin.com/company/globex-logistics", r["linkedin"])

    def test_charset_is_not_garbled(self):
        results, _ = ld.discover_urls([self.url("latin1-site.test")], delay_seconds=0, deep=False)
        r = results[0]
        self.assertIn("Café", r["page_title"])
        self.assertNotIn("Ã", r["page_title"])

    def test_robots_disallow_is_respected(self):
        results, errors = ld.discover_urls([self.url("robots-site.test")], delay_seconds=0)
        self.assertEqual(results, [])
        self.assertIn("robots.txt", errors[0]["error"])
        self.assertNotIn(("robots-site.test", "/"), Handler.hits)

    def test_blocked_site_reports_error_without_bypass(self):
        with patch("lead_discovery._wayback_snapshot", return_value=None):
            results, errors = ld.discover_urls([self.url("blocked-site.test")], delay_seconds=0)
        self.assertEqual(results, [])
        self.assertIn("denied", errors[0]["error"])

    def test_blocked_site_uses_labelled_archive_copy_when_available(self):
        snap = (self.url("globex-logistics.test"), "text/html", GLOBEX_HOME, "2025-01-02")
        with patch("lead_discovery._wayback_snapshot", return_value=snap):
            results, errors = ld.discover_urls([self.url("blocked-site.test")], delay_seconds=0)
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["discovery_method"], "archived_snapshot")
        self.assertIn("ARCHIVED COPY dated 2025-01-02", results[0]["evidence"])

    def test_batch_dedupes_caches_and_isolates_failures(self):
        urls = [self.url("acme-saas.test"), self.url("acme-saas.test", "/about-us"), "ftp://bad.example",
                self.url("globex-logistics.test"), self.url("blocked-site.test")]
        with patch("lead_discovery._wayback_snapshot", return_value=None):
            results, errors = ld.discover_urls(urls, delay_seconds=0)
        self.assertEqual([r["company"] for r in results], ["Acme", "Globex Logistics"])
        self.assertEqual(len(errors), 3)
        self.assertTrue(any("Duplicate" in e["error"] for e in errors))
        hits_before = len(Handler.hits)
        again, _ = ld.discover_urls([self.url("acme-saas.test")], delay_seconds=0)
        self.assertTrue(again[0]["from_cache"])
        self.assertEqual(len(Handler.hits), hits_before)  # served from cache

    def test_ssrf_protection_blocks_private_targets(self):
        with patch.object(ld, "ALLOW_PRIVATE_HOSTS", False):
            results, errors = ld.discover_urls(["http://127.0.0.1:8765/", "http://localhost:8765/", "http://169.254.169.254/"],
                                               delay_seconds=0, archive_fallback=False)
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 3)
        self.assertTrue(all("private" in e["error"].lower() or "local" in e["error"].lower() for e in errors), errors)

    def test_no_mx_domain_email_is_dropped(self):
        with patch("lead_discovery._mx_status", lambda d: "none"):
            results, _ = ld.discover_urls([self.url("acme-saas.test")], delay_seconds=0)
        self.assertEqual(results[0]["email"], "")
        self.assertEqual(results[0]["email_status"], "Missing")

    def test_parallel_batch_is_faster_than_serial(self):
        orig = ld._fetch

        def slow(*a, **k):
            time.sleep(0.3)
            return orig(*a, **k)

        urls = [self.url("acme-saas.test"), self.url("globex-logistics.test"), self.url("latin1-site.test")]
        with patch("lead_discovery._fetch", slow):
            t0 = time.time()
            ld.discover_urls(urls, delay_seconds=0, deep=False, use_cache=False)
            parallel = time.time() - t0
        # serial would be >= 3 sites x 2 fetches (robots + page) x 0.3s = 1.8s
        self.assertLess(parallel, 1.6)


if __name__ == "__main__":
    unittest.main()
