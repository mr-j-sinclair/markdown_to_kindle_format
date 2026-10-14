"""Opt-in live smoke tests for daily_facts.py: real World Bank, Wikipedia,
flagcdn, Open-Meteo, TfL and OpenAI calls (a few cents per run). Skipped unless
DAILY_FACTS_LIVE=1, so the default test run and CI never make them. Run:

  OPENAI_API_KEY="$(security find-generic-password -s openai-api-key -w)" \\
    DAILY_FACTS_LIVE=1 .venv/bin/python3 -m unittest tests.test_daily_facts_live -v

DAILY_FACTS_LIVE_DATE (default 2026-10-11: Argentina, larger than the UK)
picks the day; DAILY_FACTS_LIVE_OUT (default: a new temporary directory,
kept) holds the full day's Markdown and EPUB. The full-day run is a
--preview, so nothing is sent and no real state is touched. Calls E
(weather) and F (Thameslink) are also checked on their own for tomorrow.
Open-Meteo also serves recent past dates, but a DAILY_FACTS_LIVE_DATE far
in the past may leave the full day's weather unavailable."""

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
HEADINGS = ("## Weather in Croydon", "## Trains", "### Windrush line", "### Thameslink", "## Flag of the Day", "## Research Fact", "## AI Engineering", "## UK vs Jamaica")


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



@unittest.skipUnless(LIVE, "live smoke tests: set DAILY_FACTS_LIVE=1")
class LiveCountryFactTests(unittest.TestCase):
    """One fact from each API adapter (IMF, WHO, World Bank) and one
    search-only fact through call G, each written up by the fact writer."""

    def fact(self, fact_id):
        data = df.load_data()
        sel = df.select(DATE, data)
        sel.country_fact = next(f for f in data["country_facts"] if f["id"] == fact_id)
        facts = df.Facts()
        facts.country_fact = df._gather_country_fact(sel, facts, client=df._openai_client())
        block, _ = df.fact_block(sel.country_fact, facts.country_fact)
        paragraph = df.write_country_fact(sel.country_fact, facts.country_fact, client=df._openai_client())
        print(f"\n{block}\n-> {paragraph}\n{facts.diagnostics}")
        return facts.country_fact

    def test_api_facts(self):
        for fact_id in ("gov-debt", "life-expectancy", "internet"):
            observations = self.fact(fact_id)["observations"]
            self.assertEqual(set(observations), {df.UK, df.JAMAICA}, fact_id)
            self.assertEqual({obs["via"] for obs in observations.values()}, {"api"}, fact_id)

    def test_search_only_fact(self):
        fact = self.fact("prison-rate")
        self.assertTrue(fact["search_actions"])
        self.assertEqual(set(fact["observations"]), {df.UK, df.JAMAICA})   # never vacuous
        for obs in fact["observations"].values():
            self.assertEqual(obs["via"], "search")
            self.assertTrue(df._allowed_host(obs["url"], ["prisonstudies.org"]))


@unittest.skipUnless(LIVE, "live smoke tests: set DAILY_FACTS_LIVE=1")
class LiveWeatherAndTrainsTests(unittest.TestCase):
    """Call E over a real Open-Meteo forecast and call F's real web search,
    both for tomorrow (forecasts and planned works only exist ahead)."""

    def setUp(self):
        self.date = df.target_date()

    def test_weather_paragraph(self):
        weather = df.croydon_weather(self.date)
        paragraph = df.write_weather(weather, client=df._openai_client())  # raises if guards fail
        print(f"\n{df.weather_block(weather)}\n-> {paragraph}")
        self.assertIn(weather["high"], paragraph)

    def test_thameslink_check(self):
        result = df.thameslink_disruptions(self.date, client=df._openai_client())  # raises if unvalidated
        print(f"\n{self.date}: {result}")
        self.assertIn(result["status"], ("disruption", "none_found"))
        self.assertTrue(result["search_actions"])
        if result["status"] == "disruption":
            self.assertTrue(result["summary"] and result["sources"])


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
        self.assertNotIn("not available today", markdown.split("## UK vs Jamaica", 1)[1])
        with open(state, "rb") as f:
            self.assertEqual(f.read(), before)


if __name__ == "__main__":
    unittest.main()
