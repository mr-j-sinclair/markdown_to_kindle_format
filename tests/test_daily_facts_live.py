"""Opt-in live smoke tests for daily_facts.py: real World Bank, Wikipedia,
flagcdn and OpenAI calls (a few cents per run). Skipped unless
DAILY_FACTS_LIVE=1, so the default test run and CI never make them. Run:

  OPENAI_API_KEY="$(security find-generic-password -s openai-api-key -w)" \\
    DAILY_FACTS_LIVE=1 .venv/bin/python3 -m unittest tests.test_daily_facts_live -v

DAILY_FACTS_LIVE_DATE (default 2026-10-11: Argentina, larger than the UK)
picks the day; DAILY_FACTS_LIVE_OUT (default: a new temporary directory,
kept) holds the full day's Markdown and EPUB. The full-day run is a
--preview, so nothing is sent and no real state is touched."""

import datetime
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import daily_facts as df

LIVE = os.environ.get("DAILY_FACTS_LIVE") == "1"
# Parsed only when live, so a bad value cannot break ordinary discovery.
DATE = datetime.date.fromisoformat(os.environ.get("DAILY_FACTS_LIVE_DATE", "2026-10-11")) if LIVE else None
HEADINGS = ("## Flag of the Day", "## Research Fact", "## AI Engineering", "## UK Economy", "## Jamaica Economy")


def setUpModule():
    # Asked for live tests but cannot make them: fail loudly, never skip.
    if LIVE and not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("DAILY_FACTS_LIVE=1 but OPENAI_API_KEY is not set")


@unittest.skipUnless(LIVE, "live smoke tests: set DAILY_FACTS_LIVE=1")
class LiveComparisonTests(unittest.TestCase):
    """Call D on its own, over real World Bank figures."""

    def setUp(self):
        self.data = df.load_data()
        self.sel = df.select(DATE, self.data)

    def sentence(self, block, relation):
        sentence = df.write_comparison(block, relation, client=df._openai_client())  # raises if guards fail
        print(f"\n{block}\n-> {sentence}")
        self.assertTrue(sentence)
        return sentence

    def land(self, iso3):
        self.sel.country = next(c for c in self.data["countries"] if c["cca3"] == iso3)
        facts = df.Facts()
        facts.land = df._gather_vs_uk(self.sel, facts, df.LAND_AREA, "Land area")
        self.assertIsNotNone(facts.land, facts.diagnostics)
        return self.sentence(*df.land_block(self.sel, facts))

    def test_land_larger_country(self):
        self.assertIn("larger than the uk", self.land("ARG").lower())

    def test_land_smaller_country(self):
        self.assertIn("smaller than the uk", self.land("JAM").lower())

    def economy_facts(self):
        facts = df.Facts()
        facts.jm_econ = df._gather_economy(self.sel, facts, df.JAMAICA, "Jamaica", self.sel.jm_indicators)
        self.assertIsNotNone(facts.jm_econ, facts.diagnostics)
        return facts

    def test_economy_pair(self):
        facts = self.economy_facts()
        facts.jm_uk = df._gather_uk_equivalent(self.sel, facts, facts.jm_econ)
        self.assertIsInstance(facts.jm_uk, dict, facts.diagnostics)
        self.sentence(*df.economy_block(self.sel, facts))

    def test_economy_no_uk_data(self):
        facts = self.economy_facts()
        facts.jm_uk = "no_data"
        block, relation = df.economy_block(self.sel, facts)
        self.assertEqual(relation, "")
        self.sentence(block, relation)


@unittest.skipUnless(LIVE, "live smoke tests: set DAILY_FACTS_LIVE=1")
class LiveFullDayTests(unittest.TestCase):
    """A whole day's document end to end (--preview: converts, never sends)."""

    def test_full_day(self):
        out = os.environ.get("DAILY_FACTS_LIVE_OUT") or tempfile.mkdtemp(prefix="daily_facts_live_")
        os.makedirs(out, exist_ok=True)
        fd, state = tempfile.mkstemp(prefix="state_", suffix=".json", dir=out)  # never an existing file
        os.close(fd)
        df.save_state(state, {"sent": {}})
        with open(state, "rb") as f:
            before = f.read()
        with patch.object(df, "INPUT_DIR", os.path.join(out, "inputs")), \
                patch.object(df, "OUTPUT_DIR", os.path.join(out, "outputs")):
            rc = df.main(["--preview", "--date", DATE.isoformat(), "--state", state])
        markdown_path = os.path.join(out, "inputs", f"{df.slug(DATE)}.md")
        with open(markdown_path, encoding="utf-8") as f:
            markdown = f.read()
        print(f"\nLive output kept in {out}\n--- {markdown_path} ---\n{markdown}")

        self.assertEqual(rc, df.EXIT_SENT)
        self.assertTrue(os.path.exists(os.path.join(out, "outputs", f"{df.slug(DATE)}.epub")))
        for heading in HEADINGS:
            self.assertIn(heading, markdown)
        self.assertNotIn("Unavailable today", markdown)
        self.assertNotIn("UK comparison unavailable", markdown)
        if df.select(DATE, df.load_data()).country["cca3"] != df.UK:
            self.assertIn("**Size vs UK:**", markdown)
        self.assertIn("**vs UK:**", markdown.split("## Jamaica Economy", 1)[1])
        with open(state, "rb") as f:
            self.assertEqual(f.read(), before)


if __name__ == "__main__":
    unittest.main()
