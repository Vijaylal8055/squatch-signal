"""End-to-end UI test: drives the real Streamlit app headlessly in guest mode (no keys)."""
import os
import socket
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

os.environ["SQUATCH_ALLOW_PRIVATE"] = "1"
os.environ.pop("SUPABASE_URL", None)
os.environ.pop("SUPABASE_PUBLISHABLE_KEY", None)

from streamlit.testing.v1 import AppTest  # noqa: E402

import lead_discovery as ld  # noqa: E402
from test_lead_discovery import HOSTS, Handler  # noqa: E402

PORT = 8766


def _button(at, text):
    for b in at.button:
        if text.lower() in (b.label or "").lower():
            return b
    raise AssertionError(f"button containing {text!r} not found; have {[b.label for b in at.button]}")


class AppEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        real = socket.getaddrinfo
        cls.p1 = patch("socket.getaddrinfo", lambda h, *a, **k: real("127.0.0.1" if h in HOSTS else h, *a, **k))
        cls.p2 = patch("lead_discovery._mx_status", lambda d: "ok")
        cls.p1.start(); cls.p2.start()
        ld.clear_cache()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.p1.stop(); cls.p2.stop()

    def test_guest_flow(self):
        at = AppTest.from_file("app.py", default_timeout=90).run()
        self.assertFalse(at.exception, at.exception)
        # no Supabase configured -> guest login is offered, no password form
        _button(at, "Continue as guest").click(); at.run()
        self.assertFalse(at.exception, at.exception)
        self.assertTrue(at.session_state["authenticated"])

        # Overview renders with an empty workspace
        self.assertIn("No leads yet", " ".join(i.value for i in at.info))

        # Discover
        at.sidebar.radio[0].set_value("Discover Leads").run()
        urls = f"http://acme-saas.test:{PORT}/\nhttp://globex-logistics.test:{PORT}/\nhttp://blocked-site.test:{PORT}/"
        at.text_area[0].set_value(urls)
        with patch("lead_discovery._wayback_snapshot", return_value=None):
            _button(at, "Discover prospects").click(); at.run()
        self.assertFalse(at.exception, at.exception)
        found = at.session_state["discovery_results"]
        errors = at.session_state["discovery_errors"]
        self.assertEqual({r["company"] for r in found}, {"Acme", "Globex Logistics"})
        self.assertEqual(len(errors), 1)
        # fit score uses discovered employees (120 -> ICP size band) and the SaaS industry
        acme = next(r for r in found if r["company"] == "Acme")
        self.assertGreaterEqual(acme["fit_score"], 70, acme["score_reasons"])
        self.assertTrue(any("technology ICP" in r for r in acme["score_reasons"]))
        self.assertEqual(found[0]["company"], "Acme")  # sorted by score

        # Save both
        _button(at, "Save 2 selected").click(); at.run()
        self.assertFalse(at.exception, at.exception)
        store = at.session_state["db_client"].store
        self.assertEqual(len(store["leads"]), 2)
        saved = {l["company"]: l for l in store["leads"]}
        self.assertEqual(saved["Acme"]["employees"], 120)
        self.assertEqual(saved["Acme"]["user_id"], "guest")
        self.assertIn("Source URL:", saved["Acme"]["evidence"])
        self.assertEqual(at.session_state["page"], "Leads")

        # Re-discovering the same companies must not create duplicates
        at.sidebar.radio[0].set_value("Discover Leads").run()
        at.text_area[0].set_value(f"http://acme-saas.test:{PORT}/")
        _button(at, "Discover prospects").click(); at.run()
        _button(at, "Save 1 selected").click(); at.run()
        self.assertEqual(len(at.session_state["db_client"].store["leads"]), 2)

        # Leads page + filters render
        at.sidebar.radio[0].set_value("Leads").run()
        self.assertFalse(at.exception, at.exception)
        self.assertTrue(any("Acme" in e.label for e in at.expander))

        # Outreach draft from a lead
        at.sidebar.radio[0].set_value("Outreach Drafts").run()
        self.assertFalse(at.exception, at.exception)
        _button(at, "Generate draft").click(); at.run()
        self.assertFalse(at.exception, at.exception)
        self.assertTrue(at.session_state["draft_subject"].startswith("Idea for"))

        # Add a lead manually
        at.sidebar.radio[0].set_value("Add a Lead").run()
        at.text_input[0].set_value("Manual Co")
        _button(at, "Save lead").click(); at.run()
        self.assertFalse(at.exception, at.exception)
        self.assertEqual(len(at.session_state["db_client"].store["leads"]), 3)

        # Overview with data
        at.sidebar.radio[0].set_value("Overview").run()
        self.assertFalse(at.exception, at.exception)
        self.assertEqual([m.value for m in at.metric][0], "3")

    def test_runs_without_pandas_and_escapes_scraped_text(self):
        """Locked-down Windows policies can block pandas' compiled files: the app must not need it."""
        with patch.dict(sys.modules, {"pandas": None, "pyarrow": None}):
            at = AppTest.from_file("app.py", default_timeout=90).run()
            _button(at, "Continue as guest").click(); at.run()
            at.sidebar.radio[0].set_value("Discover Leads").run()
            at.text_area[0].set_value(f"http://acme-saas.test:{PORT}/\nhttp://blocked-site.test:{PORT}/")
            with patch("lead_discovery._wayback_snapshot", return_value=None):
                _button(at, "Discover prospects").click(); at.run()
            self.assertFalse(at.exception, at.exception)
            self.assertEqual(len(at.session_state["discovery_results"]), 1)
            _button(at, "Save 1 selected").click(); at.run()
            self.assertFalse(at.exception, at.exception)
            # hostile text from a scraped site must never reach the page as raw HTML
            at.sidebar.radio[0].set_value("Add a Lead").run()
            at.text_input[0].set_value('<img src=x onerror=alert(1)> $5 | **bold**')
            _button(at, "Save lead").click(); at.run()
            at.sidebar.radio[0].set_value("Overview").run()
            self.assertFalse(at.exception, at.exception)
            html = " ".join(m.value for m in at.markdown)
            self.assertIn("ss-table", html)          # table rendered by our own helper
            self.assertIn("ss-bar-fill", html)       # bar chart rendered by our own helper
            self.assertNotIn("<img src=x", html)
            self.assertIn("&lt;img src=x", html)


if __name__ == "__main__":
    unittest.main()
