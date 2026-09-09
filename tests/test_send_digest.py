"""send_digest must name itself to Cloudflare.

Measured 2026-08-26: the first wheel-daily CI run completed but its digest
died with "HTTP Error 403: Forbidden". api.resend.com sits behind Cloudflare,
which rejects the default Python-urllib User-Agent (error 1010) — the same
trap build_earnings_exclusions already works around in the same file. A
direct curl with identical key/from/to succeeded, isolating the UA as the
only difference.
"""

import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import run_daily


class SendDigestNamesItself(unittest.TestCase):
    def _capture_request(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            return mock.MagicMock(__enter__=mock.MagicMock(), __exit__=mock.MagicMock())

        return captured, fake_urlopen

    def test_sends_a_named_user_agent(self):
        captured, fake_urlopen = self._capture_request()
        env = {
            "RESEND_API_KEY": "k",
            "RESEND_FROM": "Paper Wheel <hello@example.com>",
            "RESEND_TO": "someone@example.com",
        }
        with mock.patch.dict("os.environ", env), \
                mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            run_daily.send_digest("subject", "body")
        req = captured["req"]
        # urllib normalizes header names to Capitalized-With-Dashes.
        ua = req.headers.get("User-agent", "")
        self.assertTrue(ua.startswith("paper-wheel/"),
                        f"digest request must not use the default urllib UA, got {ua!r}")

    def test_skips_cleanly_without_config(self):
        captured, fake_urlopen = self._capture_request()
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            run_daily.send_digest("subject", "body")
        self.assertNotIn("req", captured, "no RESEND_* config must mean no request")


if __name__ == "__main__":
    unittest.main()


def test_a_green_day_writes_state_and_sends_no_mail():
    """The opener already reads daily_state.json and nav_history.jsonl off R2.

    A GREEN day's mail restated those two files and carried no decision, so it
    was the third copy of a number nobody acted on. The state write is NOT on
    this switch: if a quiet day also stopped writing, quiet would stop meaning
    "nothing to act on" and start meaning "did not run".
    """
    from scripts.run_daily import should_send_digest

    assert should_send_digest("GREEN") is False


def test_yellow_and_red_days_still_mail():
    from scripts.run_daily import should_send_digest

    assert should_send_digest("YELLOW") is True, "every name earnings-excluded is a finding"
    assert should_send_digest("RED") is True, "a breach or a failed run must never go quiet"


def test_an_unknown_status_mails_rather_than_going_quiet():
    from scripts.run_daily import should_send_digest

    assert should_send_digest("") is True
    assert should_send_digest("PURPLE") is True
