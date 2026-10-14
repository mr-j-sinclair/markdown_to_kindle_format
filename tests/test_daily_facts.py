import contextlib
import datetime
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import daily_facts as df

DATE = datetime.date(2026, 10, 10)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def wb_page(rows):
    return json.dumps([{"page": 1, "pages": 1, "per_page": 200, "total": len(rows)}, rows]).encode()


def wb_row(iso3, year, value, code="SP.POP.TOTL"):
    return {"indicator": {"id": code}, "countryiso3code": iso3, "date": str(year), "value": value}


def lead(text="Some encyclopedic text. More lead paragraphs."):
    return json.dumps({"query": {"pages": [{"title": "T", "extract": text}]}}).encode()


def wiki(title, extract="Some encyclopedic text."):
    return json.dumps({"type": "standard", "title": title, "titles": {"normalized": title},
                       "extract": extract,
                       "content_urls": {"desktop": {"page": f"https://en.wikipedia.org/wiki/{title}"}}}).encode()


def forecast(url, chances=None, amounts=None, codes=None, offset=3600, **daily):
    """An Open-Meteo answer for the URL's date and the next, shaped like the
    real one: 48 hourly labels at ONE fixed offset (utc_offset_seconds), and
    each label's values covering the PRECEDING hour. chances/amounts/codes are
    per hour of the date (index 13 = 13:00-14:00 when offset matches London),
    so they sit on labels 1..24. Defaults: light rain, 20.0/13.4 °C and light
    showers 13:00-16:00."""
    day = re.search(r"start_date=([\d-]+)", url).group(1)
    next_day = re.search(r"end_date=([\d-]+)", url).group(1)
    chances = [0] * 13 + [55, 64, 50, 40] + [0] * 7 if chances is None else chances
    amounts = [0.0] * 13 + [0.2, 0.3, 0.1] + [0.0] * 8 if amounts is None else amounts
    codes = [3] * 13 + [80, 80, 80] + [3] * 8 if codes is None else codes

    def labels(values, before):
        return [before] + list(values) + [before] * (47 - len(values))
    values = {"time": [day, next_day], "weather_code": [61, 3], "temperature_2m_max": [20.0, 18.0],
              "temperature_2m_min": [13.4, 11.0], "precipitation_probability_max": [max(chances or [0]), 0],
              "sunrise": [f"{day}T07:20", f"{next_day}T07:22"], "sunset": [f"{day}T18:11", f"{next_day}T18:09"]}
    values.update({k: [v, values[k][1]] for k, v in daily.items()})
    times = [f"{d}T{h:02d}:00" for d in (day, next_day) for h in range(24)]
    return json.dumps({"utc_offset_seconds": offset, "daily": values,
                       "hourly": {"time": times, "precipitation_probability": labels(chances, 0),
                                  "precipitation": labels(amounts, 0.0),
                                  "weather_code": labels(codes, 3)}}).encode()


def tfl_status(*statuses):
    """A TfL Line/Status answer; no statuses means Good Service."""
    statuses = statuses or ({"statusSeverityDescription": "Good Service", "validityPeriods": []},)
    return json.dumps([{"id": "windrush", "lineStatuses": list(statuses)}]).encode()


def closure(reason, start, end):
    return {"statusSeverityDescription": "Part Closure", "reason": reason,
            "validityPeriods": [{"fromDate": start, "toDate": end, "isNow": False}]}


class FakeHTTP:
    """Routes http_get URLs to canned bytes or exceptions by substring."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value(url) if callable(value) else value
        raise AssertionError(f"unexpected URL {url}")


# Trimmed live responses (2026-10-14). The IMF DataMapper returns every
# country whatever the path, with projections years ahead; WHO's GHO rows
# carry a sex dimension the request must pin.
IMF_VALUES = {"GBR": {"2023": 98.3, "2024": 99.4, "2025": 102.1, "2026": 102, "2027": 102.5},
              "JAM": {"2023": 66.5, "2024": 62.5, "2025": 66, "2026": 64.3, "2027": 64},
              "SDN": {"2023": 286.9, "2024": 282.3, "2025": 204.4, "2026": 334.4, "2027": 274.1}}
WHO_ROWS = [{"SpatialDim": iso3, "TimeDim": year, "Dim1": sex, "Dim2": None, "NumericValue": value}
            for iso3, year, sex, value in (
                ("GBR", 2022, "SEX_BTSX", 79.4), ("GBR", 2022, "SEX_FMLE", 80.9), ("GBR", 2022, "SEX_MLE", 77.8),
                ("GBR", 2023, "SEX_BTSX", 80.7), ("GBR", 2023, "SEX_FMLE", 82.6), ("GBR", 2023, "SEX_MLE", 78.9),
                ("JAM", 2022, "SEX_BTSX", 72.1), ("JAM", 2022, "SEX_FMLE", 74.9), ("JAM", 2022, "SEX_MLE", 69.5),
                ("JAM", 2023, "SEX_BTSX", 72.6), ("JAM", 2023, "SEX_FMLE", 75.1), ("JAM", 2023, "SEX_MLE", 70.2))]


def imf_page(url, values=None):
    code = re.search(r"api/v1/([^/]+)/", url).group(1)
    return json.dumps({"values": {code: IMF_VALUES if values is None else values},
                       "api": {"version": "1", "output-method": "json"}}).encode()


def who_page(url, rows=None):
    """Applies the request's pinned Dim1, as the real filter does."""
    rows = WHO_ROWS if rows is None else rows
    sex = re.search(r"Dim1%20eq%20'(\w+)'", url)
    return json.dumps({"value": [r for r in rows if not sex or r["Dim1"] == sex.group(1)]}).encode()


def fact_series(url):
    """World Bank rows for today's fact, both countries in 2024 (UK 2025 too)."""
    code = re.search(r"indicator/([^?]+)", url).group(1)
    return wb_page([wb_row("GBR", 2025, 3.1, code), wb_row("GBR", 2024, 2.9, code), wb_row("JAM", 2024, 4.2, code)])


def default_routes(country_iso3="JAM"):
    def population(url):
        rows = [wb_row(country_iso3, 2024, 2_800_000), wb_row("GBR", 2024, 69_000_000)]
        if country_iso3 != "GBR":
            rows.append(wb_row("GBR", 2025, 69_500_000))  # UK has a newer year the country lacks
        return wb_page(rows)

    def land(url):
        return wb_page([wb_row(country_iso3, 2024, 10_830, df.LAND_AREA), wb_row("GBR", 2024, 241_930, df.LAND_AREA)])

    return {
        "SP.POP.TOTL?format=json&per_page=200&date=": population,
        "AG.LND.TOTL.K2?format=json&per_page=200&date=": land,
        "country/GBR;JAM/indicator/": fact_series,
        "imf.org/external/datamapper/api": imf_page,
        "ghoapi.azureedge.net": who_page,
        "flagcdn.com": PNG,
        "api.open-meteo.com": forecast,
        "api.tfl.gov.uk": tfl_status(),
        "w/api.php": lead(),
        "wikipedia.org": lambda url: wiki(url.rsplit("/", 1)[1]),
    }


def world_bank_down_routes():
    routes = default_routes()
    for key in [k for k in routes if "date=" in k or "/indicator/" in k]:
        del routes[key]
    routes["worldbank"] = TimeoutError()
    return routes


TOOL_URL = "https://Example.com/Guide/Evals?utm_source=openai"


def search_call(*urls):
    return SimpleNamespace(type="web_search_call", action=SimpleNamespace(
        type="search", query="q", sources=[SimpleNamespace(type="url", url=u) for u in urls]))


def open_page_call(url):
    return SimpleNamespace(type="web_search_call", action=SimpleNamespace(type="open_page", url=url))


def response(payload, *, tool_calls=None, status="completed"):
    """A fake Responses API result. tool_calls defaults to one search whose
    results contain TOOL_URL; pass [] for a response that never searched."""
    tool_calls = [search_call(TOOL_URL)] if tool_calls is None else tool_calls
    output = list(tool_calls) + [SimpleNamespace(type="message", content=[SimpleNamespace(annotations=[])])]
    return SimpleNamespace(output_text=json.dumps(payload), output=output, status=status,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


GOOD_A = {"country_blurb": "An island in the Caribbean. It is mountainous.",
          "research_headline": "A striking fact", "research_body": "First paragraph.\n\nSecond paragraph."}
GOOD_B = {"topic_title": "Context rot", "concept": "Long contexts degrade recall.",
          "why_it_matters": ["It limits agent reliability."],
          "sources": [{"url": "https://example.com/Guide/Evals", "supports": "Recall degrades with length."}]}
GOOD_C = {"status": "ok", "notes": [], "source_disagreements": []}
GOOD_E = {"paragraph": "A grey day with light showers from 1pm until 4pm, so take an umbrella for the "
                       "afternoon. Expect a high of 20°C and a low of 13°C."}
RAIL_URL = "https://www.thameslinkrailway.com/travel-information/engineering-works"
GOOD_F = {"status": "none_found", "summary": "", "sources": []}
DISRUPTION_F = {"status": "disruption",
                "summary": "No Thameslink trains run between Blackfriars and Elephant & Castle all day.",
                "sources": [{"url": RAIL_URL, "supports": "Lists the closure on 10 October."}]}


def echo_fact(request):
    """Fact writer stand-in: repeats the block's relation and any differing years."""
    relation = re.search(r"^RELATION: (.+)$", request["input"], re.MULTILINE)
    years = re.search(r"different years \(Jamaica (\d{4}), UK (\d{4})\)", request["input"])
    text = (f"Jamaica's figure is {relation.group(1)}." if relation
            else "Only one country's figure is available today, so there is no comparison.")
    if years:
        text += f" Jamaica's is from {years.group(1)} and the UK's from {years.group(2)}."
    return response({"paragraph": text}, tool_calls=[])


def echo_comparison(request):
    """Call D stand-in: one sentence repeating the block's RELATION line."""
    relation = re.search(r"^RELATION: (.+)$", request["input"], re.MULTILINE)
    sentence = (f"It is {relation.group(1)}." if relation
                else "No UK figure is available, so no comparison is possible.")
    return response({"sentence": sentence}, tool_calls=[])
WB_PAGE = "https://data.worldbank.org/indicator/IT.NET.USER.ZS?locations=JM&view=chart"
PRISON_UK = "https://www.prisonstudies.org/country/united-kingdom-england-wales"
PRISON_JM = "https://www.prisonstudies.org/country/jamaica"


def found(country, value, year, url, **extra):
    return dict({"country": country, "status": "found", "value": value, "year": year,
                 "unit_note": "Prisoners per 100,000 of the national population.", "url": url,
                 "supports": "The page lists the prison population rate."}, **extra)


def fact_search(*items, urls=(PRISON_UK, PRISON_JM)):
    """A call G answer whose search returned urls."""
    return response({"countries": list(items)}, tool_calls=[search_call(*urls)])


REVIEWER_URL = "https://history.example/Golay-1949"   # a page the source check's search returned


def with_disagreements(client, *disagreements, url=REVIEWER_URL, tool_calls=None, **extra):
    """Make client's call C (no tools) record these (section, note)
    disagreements, and its source check echo each one citing url; by
    default the source check's search returned REVIEWER_URL."""
    client.answers["run_check"] = [response(dict(GOOD_C, source_disagreements=[
        {"section": s, "note": n} for s, n in disagreements], **extra), tool_calls=[])]
    client.answers["source_check"] = [response({"source_disagreements": [
        {"section": s, "note": n, "url": url} for s, n in disagreements]},
        tool_calls=[search_call(REVIEWER_URL)] if tool_calls is None else tool_calls)]
    return client


class FakeClient:
    """Stands in for openai.OpenAI; answers by the structured-output schema name."""

    def __init__(self, answers):
        self.answers = answers   # name -> list of responses/exceptions, consumed in order
        self.requests = []
        self.responses = self

    def create(self, **kwargs):
        name = kwargs["text"]["format"]["name"]
        self.requests.append(kwargs)
        queue = self.answers[name]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item(kwargs) if callable(item) else item


def good_client():
    return FakeClient({"grounded_prose": [response(GOOD_A, tool_calls=[])],
                       "ai_lesson": [response(GOOD_B)],
                       "run_check": [response(GOOD_C, tool_calls=[])],
                       "comparison": [echo_comparison],
                       "weather": [response(GOOD_E, tool_calls=[])],
                       "country_fact": [echo_fact],
                       # Call G only runs when an API lacks a figure; by default it finds nothing.
                       "country_fact_search": [response({"countries": []}, tool_calls=[search_call(WB_PAGE)])],
                       "thameslink": [response(GOOD_F, tool_calls=[search_call(RAIL_URL)])]})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = df.load_data()
        for target in ("INPUT_DIR", "OUTPUT_DIR"):
            p = patch.object(df, target, os.path.join(self.tmp, target.lower()))
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(df, "_sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp)

    def jamaica_selection(self, fact="internet"):
        sel = df.select(DATE, self.data)
        sel.country = next(c for c in self.data["countries"] if c["cca3"] == "JAM")
        sel.country_fact = self.fact(fact)
        return sel

    def fact(self, fact_id):
        return next(f for f in self.data["country_facts"] if f["id"] == fact_id)

    def gather(self, routes=None, client=None, sel=None):
        sel = sel or self.jamaica_selection()
        with patch.object(df, "http_get", FakeHTTP(routes or default_routes())):
            return sel, df.gather(sel, self.data, [], client=client or good_client())


# ---------- selection ----------

class SelectionTests(Base):
    def test_same_date_same_picks(self):
        a, b = df.select(DATE, self.data), df.select(DATE, self.data)
        self.assertEqual((a.country, a.research, a.ai_theme, a.country_fact),
                         (b.country, b.research, b.ai_theme, b.country_fact))

    def test_no_repeats_within_a_cycle(self):
        n = len(self.data["countries"])
        picks = {df.seeded_index(n, "country", day) for day in range(n)}
        self.assertEqual(len(picks), n)

    def test_cycle_boundary_reshuffles(self):
        n = 50
        first = [df.seeded_index(n, "research", d) for d in range(n)]
        second = [df.seeded_index(n, "research", d) for d in range(n, 2 * n)]
        self.assertEqual(sorted(second), list(range(n)))
        self.assertNotEqual(first, second)

    def test_categories_use_independent_seeds(self):
        n = 15
        uk = [df.seeded_index(n, "uk-economy", d) for d in range(n)]
        jm = [df.seeded_index(n, "jm-economy", d) for d in range(n)]
        self.assertNotEqual(uk, jm)

    def test_no_repeat_across_cycle_boundaries(self):
        for n in (15, 12, 193):
            picks = [df.seeded_index(n, "ai-theme", d) for d in range(n * 40)]
            for cycle in range(40):
                self.assertEqual(sorted(picks[cycle * n:(cycle + 1) * n]), list(range(n)))
            gap = min(df.BOUNDARY_GAP, n // 3)
            for d in range(gap, len(picks)):
                self.assertNotIn(picks[d], picks[d - gap:d], f"n={n} day={d}")

    def test_one_fact_a_day_rotates_through_the_catalogue(self):
        facts = self.data["country_facts"]
        day = (DATE - df.EPOCH).days
        self.assertEqual(df.select(DATE, self.data).country_fact,
                         facts[df.seeded_index(len(facts), "country-fact", day)])   # one fact for both countries
        cycle = [df.seeded_index(len(facts), "country-fact", d) for d in range(len(facts))]
        self.assertEqual(sorted(cycle), list(range(len(facts))))                    # each used once per cycle

    def test_catalogues_are_well_formed(self):
        titles = [t["wikipedia_title"] for t in self.data["research"]]
        self.assertEqual(len(titles), len(set(titles)))
        for t in self.data["research"]:
            self.assertTrue(t["category"] and t["display_title"] and t["wikipedia_title"])
        for theme in self.data["ai_themes"]:
            self.assertTrue(theme["name"] and theme["description"] and theme["examples"])
        self.assertEqual(len(self.data["countries"]), 193)
        ids = [f["id"] for f in self.data["country_facts"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 15)
        for f in self.data["country_facts"]:
            self.assertIn(f["format"], ("usd", "pct", "count", "dec", "year"), f["id"])
            self.assertTrue(f["label"] and f["meaning"] and f["org"], f["id"])
            self.assertIn(f["source"], ("worldbank", "imf", "who", "search"), f["id"])
            if f["source"] == "search":
                self.assertTrue(f["domains"], f["id"])
            else:
                self.assertTrue(f["code"], f["id"])
            if f["source"] == "who":
                self.assertTrue(f["page"].startswith("https://www.who.int/"), f["id"])
        self.assertEqual({f["source"] for f in self.data["country_facts"]}, {"worldbank", "imf", "who", "search"})


# ---------- World Bank ----------

class WorldBankTests(Base):
    def test_population_uses_newest_common_year(self):
        sel, facts = self.gather()
        self.assertEqual(facts.population, {"value": 2_800_000, "uk_value": 69_000_000, "year": 2024})
        self.assertTrue(any("uses 2024" in d for d in facts.diagnostics))

    def test_stale_observation_is_flagged(self):
        routes = default_routes()
        routes["country/GBR;JAM/indicator/"] = wb_page([wb_row("GBR", 2021, 97.9), wb_row("JAM", 2021, 90.0)])
        _, facts = self.gather(routes)
        self.assertEqual(facts.country_fact["observations"]["JAM"]["year"], 2021)
        self.assertTrue(any("is from 2021" in d for d in facts.diagnostics))

    def test_malformed_response_raises_fetch_error(self):
        with patch.object(df, "http_get", FakeHTTP({"worldbank": b'[{"message": "bad"}]'})):
            with self.assertRaises(df.FetchError):
                df.world_bank_series(["GBR"], "X", 2020, 2026)

    def test_malformed_rows_become_fetch_errors_and_degrade(self):
        for bad in ({"value": "not-a-number"}, {"date": "20x5"}, {"value": float("nan")}):
            routes = default_routes()
            routes["SP.POP.TOTL?format=json&per_page=200&date="] = wb_page(
                [dict(wb_row("JAM", 2024, 1.0), **bad), wb_row("GBR", 2024, 69_000_000)])
            with patch.object(df, "http_get", FakeHTTP(routes)):
                with self.assertRaises(df.FetchError, msg=bad):
                    df.world_bank_series(["JAM", "GBR"], df.POPULATION, 2020, 2026)
            sel, facts = self.gather(routes)
            self.assertIsNone(facts.population)
            self.assertTrue(any(d.startswith("Population unavailable") for d in facts.diagnostics))
            self.assertIn("| Population | unavailable today |", df.render_body(sel, facts))

    def test_non_positive_population_is_no_data_and_stale_year_flagged(self):
        routes = default_routes()
        routes["SP.POP.TOTL?format=json&per_page=200&date="] = wb_page(
            [wb_row("JAM", 2023, 2_790_000), wb_row("JAM", 2024, 0),
             wb_row("GBR", 2023, 68_000_000), wb_row("GBR", 2024, 69_000_000)])
        _, facts = self.gather(routes)
        self.assertEqual(facts.population["year"], 2023)
        self.assertIn("Population latest World Bank year shared with the UK is 2023.", facts.diagnostics)

    def test_recent_population_year_not_flagged_stale(self):
        _, facts = self.gather()
        self.assertFalse(any("Population latest" in d for d in facts.diagnostics))

    def test_lead_fallback_is_a_diagnostic(self):
        routes = default_routes()
        routes["w/api.php"] = TimeoutError()
        _, facts = self.gather(routes)
        self.assertIsNotNone(facts.research_wiki)
        self.assertIn("Research fact source: full lead unavailable (Wikipedia lead "
                      f"'{facts.research_wiki.title}': TimeoutError); used the summary paragraph only.",
                      facts.diagnostics)

    def test_lead_section_widens_extract_and_is_capped(self):
        long_lead = " ".join(["word"] * (df.MAX_SOURCE_WORDS + 50))
        routes = default_routes()
        routes["w/api.php"] = lead(long_lead)
        with patch.object(df, "http_get", FakeHTTP(routes)):
            summary = df.wikipedia_lead("Transistor")
        self.assertEqual(len(summary.extract.split()), df.MAX_SOURCE_WORDS)
        self.assertEqual(summary.url, "https://en.wikipedia.org/wiki/Transistor")

    def test_lead_failure_falls_back_to_summary(self):
        routes = default_routes()
        routes["w/api.php"] = TimeoutError()
        with patch.object(df, "http_get", FakeHTTP(routes)):
            summary = df.wikipedia_lead("Transistor")
        self.assertEqual(summary.extract, "Some encyclopedic text.")

    def test_retries_then_succeeds(self):
        calls = []

        def flaky(url):
            calls.append(url)
            if len(calls) < 3:
                raise TimeoutError()
            return wb_page([wb_row("GBR", 2025, 1.0)])
        with patch.object(df, "http_get", flaky):
            self.assertEqual(df.world_bank_series(["GBR"], "X", 2020, 2026), {"GBR": {2025: 1.0}})
        self.assertEqual(len(calls), 3)


# ---------- rendering ----------

class RenderTests(Base):
    def test_compare_to_uk(self):
        self.assertEqual(df.compare_to_uk(2_840_000, 69_490_000), "4.1% of the UK's")
        self.assertEqual(df.compare_to_uk(17_850_000, 69_490_000), "26% of the UK's")
        self.assertEqual(df.compare_to_uk(1_450_000_000, 69_490_000), "20.9× the UK's")
        self.assertEqual(df.compare_to_uk(10_000, 69_490_000), "0.014% of the UK's")

    def test_format_value(self):
        self.assertEqual(df.format_value(4.003e12, "usd"), "$4 trillion")
        self.assertEqual(df.format_value(22_704_903_217, "usd"), "$22.7 billion")
        self.assertEqual(df.format_value(8003.4, "usd"), "$8,003")
        self.assertEqual(df.format_value(-1.234, "pct"), "-1.2%")
        self.assertEqual(df.format_value(2_837_077, "count"), "2.84 million")
        self.assertEqual(df.format_value(286.37, "dec"), "286.4")

    def test_full_document(self):
        sel, facts = self.gather()
        markdown, warnings, _ = df.build_document(sel, facts, client=good_client())
        self.assertTrue(markdown.startswith("# Daily Facts — Saturday 10 October 2026\n"))
        for heading in ("## Flag of the Day: Jamaica", "## Research Fact: A striking fact",
                        "## AI Engineering: Context rot", "## UK vs Jamaica",
                        "### People using the internet (% of population)"):
            self.assertIn(heading, markdown)
        self.assertIn("![Flag of Jamaica](daily_facts_2026-10-10_flag.png)", markdown)
        self.assertIn("| Capital | Kingston |", markdown)
        self.assertIn("| Currency | Jamaican dollar |", markdown)
        self.assertIn("| Population | 2.8 million (2024) |", markdown)
        self.assertIn("| vs UK | 4.1% of the UK's (2024) |", markdown)
        # Every flag table row has exactly two cells.
        for line in markdown.split("## Flag of the Day", 1)[1].split("\n## ", 1)[0].splitlines():
            if line.startswith("|"):
                self.assertEqual(line.count("|"), 3, line)
        # The fact: one code-formatted row for both countries (the latest year both have), each
        # country's World Bank link, and no web-search notice.
        fact = markdown.split("## UK vs Jamaica", 1)[1]
        self.assertIn("| | UK | Jamaica |\n|---|---|---|\n"
                      "| People using the internet (% of population) | 2.9% (2024) | 4.2% (2024) |", fact)
        self.assertIn("Jamaica's figure is 1.3 percentage points higher than the UK's.", fact)
        self.assertIn("Sources: [World Bank — IT.NET.USER.ZS (UK)](https://data.worldbank.org/indicator/"
                      "IT.NET.USER.ZS?locations=GB) · [World Bank — IT.NET.USER.ZS (Jamaica)]("
                      "https://data.worldbank.org/indicator/IT.NET.USER.ZS?locations=JM)", fact)
        self.assertNotIn("web search", fact)
        # The AI source is rendered from the tool's own metadata URL.
        self.assertIn(f"- [example.com/Guide/Evals]({TOOL_URL})", markdown)

    def test_full_document_links_are_all_code_owned(self):
        sel, facts = self.gather()
        markdown, _, _ = df.build_document(sel, facts, client=good_client())
        links = df.rendered_links(df.converter_view(markdown)[0])
        self.assertEqual(set(links), df.allowed_links(sel, facts))
        self.assertIn(df.world_bank_url("SP.POP.TOTL", "JM"), links)
        self.assertIn("daily_facts_2026-10-10_flag.png", links)
        self.assertIn(TOOL_URL, links)

    def test_dollar_values_pass_invariant(self):
        sel, facts = self.gather()
        sel.country_fact = dict(sel.country_fact, format="usd", label="GDP (current US$)")
        for iso3, value in (("GBR", 3.1e12), ("JAM", 22_704_903_217)):
            facts.country_fact["observations"][iso3]["value"] = value
        facts.fact_text = None
        markdown, _, _ = df.build_document(sel, facts, client=good_client())
        self.assertIn("| $3.1 trillion (2024) | $22.7 billion (2024) |", markdown)
        self.assertIn("Jamaica's figure is 0.73% of the UK's.", markdown)   # the code-built paragraph

    def test_wikipedia_url_with_parentheses_passes_invariant(self):
        url = "https://en.wikipedia.org/wiki/Georgia_(country)"
        df.check_links(f"Sources: [Wikipedia]({url})", {url})

    def test_multiple_capitals_and_currencies(self):
        sel = df.select(DATE, self.data)
        sel.country = next(c for c in self.data["countries"] if c["cca3"] == "ZAF")
        _, facts = self.gather(default_routes("ZAF"), sel=sel)
        body = df.render_body(sel, facts)
        self.assertIn("| Capital | Pretoria, Bloemfontein, Cape Town |", body)

    def test_country_wikipedia_title_override(self):
        sel = df.select(DATE, self.data)
        for iso3, title in (("GEO", "Georgia_(country)"), ("COG", "Republic_of_the_Congo"), ("IRL", "Republic_of_Ireland"),
                            ("FSM", "Federated_States_of_Micronesia")):
            sel.country = next(c for c in self.data["countries"] if c["cca3"] == iso3)
            http = FakeHTTP(default_routes(iso3))
            with patch.object(df, "http_get", http):
                facts = df.gather(sel, self.data, [], client=good_client())
            self.assertTrue(any(f"summary/{urllib.parse.quote(title, safe='')}" in u for u in http.calls), iso3)
            self.assertIsNotNone(facts.country_wiki)

    def test_uk_as_country_of_the_day(self):
        sel = df.select(DATE, self.data)
        sel.country = next(c for c in self.data["countries"] if c["cca3"] == "GBR")
        _, facts = self.gather(default_routes("GBR"), sel=sel)
        self.assertIn("| vs UK | — (this is the UK) |", df.render_body(sel, facts))

    def test_filenames(self):
        self.assertEqual(df.slug(DATE), "daily_facts_2026-10-10")
        path = df.write_input(DATE, "# x\n")
        self.assertEqual(os.path.basename(path), "daily_facts_2026-10-10.md")

    def test_no_box_when_all_ok(self):
        sel, facts = self.gather()
        facts.diagnostics.clear()
        markdown, warnings, _ = df.build_document(sel, facts, client=good_client())
        self.assertNotIn("Run check", markdown)
        self.assertEqual(warnings, [])

    def test_review_warning_box_sits_under_title(self):
        sel, facts = self.gather()
        client = good_client()
        client.answers["run_check"] = [response(dict(GOOD_C, status="warning", notes=["UK value looks odd."]),
                                                tool_calls=[])]
        markdown, warnings, _ = df.build_document(sel, facts, client=client)
        lines = markdown.splitlines()
        self.assertTrue(lines[0].startswith("# Daily Facts"))
        self.assertEqual(lines[1:4], ["", "Today is Saturday 10th October 2026.", ""])
        self.assertTrue(lines[4].startswith("> **Run check — warning:** UK value looks odd."))
        self.assertIn("UK value looks odd.", warnings)

    def test_warning_status_without_notes_still_shows_box(self):
        sel, facts = self.gather()
        for diagnostics, headline in ((["Something failed."], "see diagnostics below."),
                                      ([], "the reviewer flagged a problem but gave no details.")):
            facts.diagnostics[:] = diagnostics
            client = good_client()
            client.answers["run_check"] = [response(dict(GOOD_C, status="warning"), tool_calls=[])]
            markdown, _, _ = df.build_document(sel, facts, client=client)
            self.assertEqual(markdown.splitlines()[4], f"> **Run check — warning:** {headline}")


# ---------- degradation ----------

class DegradationTests(Base):
    def test_flag_failure_is_soft(self):
        routes = default_routes()
        routes["flagcdn.com"] = OSError("down")
        sel, facts = self.gather(routes)
        body = df.render_body(sel, facts)
        self.assertIn("*Flag image unavailable today.*", body)
        self.assertIn("| Capital | Kingston |", body)

    def test_country_source_failure_shows_unavailable_blurb(self):
        routes = {"summary/Jamaica": TimeoutError(), **default_routes()}  # first match wins
        sel, facts = self.gather(routes)
        self.assertIsNone(facts.country_wiki)
        markdown, _, disagreements = df.build_document(sel, facts, client=self.flag_note_client())
        flag = markdown.split("## Flag of the Day", 1)[1].split("\n## ", 1)[0]
        self.assertIn("| Capital | Kingston |", flag)
        self.assertIn("*Unavailable today: the country description. See the run check above.*", flag)
        self.assertNotIn("[Wikipedia]", flag)
        self.assertNotIn("AI reviewer note", flag)
        self.assertEqual(disagreements, [])
        self.assertTrue(any(d.startswith("Country description source unavailable") for d in facts.diagnostics))

    def test_call_a_failure_shows_unavailable_blurb(self):
        for answer in (RuntimeError("down"), response(dict(GOOD_A, country_blurb=""), tool_calls=[])):
            client = good_client()
            client.answers["grounded_prose"] = [answer]
            sel, facts = self.gather(client=client)
            self.assertIsNotNone(facts.country_wiki)
            body = df.render_body(sel, facts)
            flag = body.split("## Flag of the Day", 1)[1].split("\n## ", 1)[0]
            self.assertIn("| Population | 2.8 million (2024) |", flag)
            self.assertIn("*Unavailable today: the country description.", flag)
            self.assertNotIn("[Wikipedia]", flag)
            self.assertIn("[World Bank population]", flag)
            self.assertTrue(any("Prose writer (call A) failed twice" in d for d in facts.diagnostics))

    def flag_note_client(self):
        return with_disagreements(good_client(), ("flag", "Disputed."))

    def test_world_bank_down(self):
        sel, facts = self.gather(world_bank_down_routes())
        markdown, warnings, _ = df.build_document(sel, facts, client=good_client())
        self.assertIn("| Population | unavailable today |", markdown)
        self.assertIn("| Land area | unavailable today |", markdown)
        self.assertNotIn("Size vs UK", markdown)
        self.assertIn("> - Population unavailable (World Bank SP.POP.TOTL: TimeoutError).", markdown)
        self.assertIn("> - UK vs Jamaica: World Bank API unavailable (World Bank IT.NET.USER.ZS: TimeoutError); "
                      "tried a web search instead.", markdown)
        # The default fake search finds nothing on worldbank.org, so the fact is unavailable, not invented.
        self.assertIn("Unavailable today: the UK vs Jamaica fact", markdown)
        self.assertNotRegex(markdown.split("## UK vs Jamaica", 1)[1], r"\d+\.\d%")

    def test_openai_down_keeps_code_facts_and_shows_raw_diagnostics(self):
        down = FakeClient({name: [RuntimeError("down")] for name in ("grounded_prose", "ai_lesson", "run_check",
                                                                  "comparison", "weather", "thameslink",
                                                                  "country_fact")})
        sel, facts = self.gather(client=down)
        markdown, warnings, _ = df.build_document(sel, facts, client=down)
        self.assertIn("Run check unavailable; raw diagnostics below.", markdown)
        self.assertIn("Prose writer (call A) failed twice (RuntimeError).", markdown)
        self.assertIn("Unavailable today: the research fact", markdown)
        self.assertIn("Unavailable today: the AI Engineering topic", markdown)
        self.assertIn("| Capital | Kingston |", markdown)
        fact = markdown.split("## UK vs Jamaica", 1)[1]
        self.assertIn("| 2.9% (2024) | 4.2% (2024) |", fact)       # code figures survive the outage
        self.assertIn("Jamaica's figure is 1.3 percentage points higher than the UK's.", fact)  # code sentence
        self.assertNotIn("Sources: [Wikipedia]", markdown)


# ---------- LLM boundary ----------

class URLMatchingTests(unittest.TestCase):
    def test_conservative_normalisation(self):
        n = df.normalise_url
        self.assertEqual(n("HTTPS://Example.COM/Path?utm_source=x&gclid=1#frag"), "https://example.com/Path")
        self.assertEqual(n("https://example.com/a?id=7&utm_medium=y"), "https://example.com/a?id=7")
        # Path case, www., trailing slash and meaningful query params all still distinguish URLs.
        self.assertNotEqual(n("https://example.com/Path"), n("https://example.com/path"))
        self.assertNotEqual(n("https://www.example.com/a"), n("https://example.com/a"))
        self.assertNotEqual(n("https://example.com/a/"), n("https://example.com/a"))
        self.assertNotEqual(n("https://example.com/a?id=1"), n("https://example.com/a?id=2"))

    def test_link_label(self):
        self.assertEqual(df.link_label("https://www.anthropic.com/engineering/x/"), "anthropic.com/engineering/x")


class LLMTests(Base):
    def test_call_b_requires_search_with_bounded_calls(self):
        client = good_client()
        df.write_ai_lesson(self.data["ai_themes"][0], "summary", ["Old topic"], client=client)
        req = client.requests[0]
        self.assertEqual(req["tools"], [{"type": "web_search"}])
        self.assertEqual(req["tool_choice"], "required")
        self.assertEqual(req["max_tool_calls"], 2)
        self.assertIn("- Old topic", req["input"])

    def lesson(self, payload, tool_calls=None):
        client = FakeClient({"ai_lesson": [response(payload, tool_calls=tool_calls)]})
        return df.write_ai_lesson(self.data["ai_themes"][0], "s", [], client=client)

    def test_call_b_requests_tool_source_metadata(self):
        client = good_client()
        df.write_ai_lesson(self.data["ai_themes"][0], "s", [], client=client)
        self.assertEqual(client.requests[0]["include"], ["web_search_call.action.sources"])
        props = client.requests[0]["text"]["format"]["schema"]["properties"]
        self.assertEqual(props["sources"]["items"]["required"], ["url", "supports"])

    def test_search_actions_counted(self):
        calls = [search_call(TOOL_URL), open_page_call("https://a.example/x"),
                 SimpleNamespace(type="web_search_call", action=SimpleNamespace(type="find_in_page"))]
        self.assertEqual(self.lesson(GOOD_B, tool_calls=calls)["search_actions"],
                         {"search": 1, "open_page": 1, "find_in_page": 1})

    def test_call_b_without_search_fails(self):
        with self.assertRaises(df.LLMError):
            self.lesson(GOOD_B, tool_calls=[])

    def test_matched_source_rendered_from_tool_metadata(self):
        lesson = self.lesson(GOOD_B)
        self.assertEqual(lesson["sources"], [{"url": TOOL_URL, "supports": "Recall degrades with length."}])

    def test_fabricated_source_rejected_and_no_fallback(self):
        payload = dict(GOOD_B, sources=[{"url": "https://made-up.example/paper", "supports": "x"}])
        with self.assertRaises(df.LLMError):
            self.lesson(payload, tool_calls=[search_call("https://real.example/a", "https://real.example/b")])

    def test_only_matching_sources_kept(self):
        payload = dict(GOOD_B, sources=[{"url": "https://made-up.example/x", "supports": "a"},
                                        {"url": "https://example.com/Guide/Evals", "supports": "b"}])
        self.assertEqual([s["url"] for s in self.lesson(payload)["sources"]], [TOOL_URL])

    def test_same_domain_pages_kept_and_duplicates_dropped(self):
        a, b = "https://docs.example.com/evals", "https://docs.example.com/tracing"
        payload = dict(GOOD_B, sources=[{"url": a, "supports": "1"}, {"url": b, "supports": "2"},
                                        {"url": a + "#intro", "supports": "3"}])
        lesson = self.lesson(payload, tool_calls=[search_call(a, b)])
        self.assertEqual([s["url"] for s in lesson["sources"]], [a, b])

    def test_open_page_url_counts_as_tool_owned(self):
        page = "https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents"
        payload = dict(GOOD_B, sources=[{"url": page, "supports": "x"}])
        lesson = self.lesson(payload, tool_calls=[search_call("https://other.example/"), open_page_call(page)])
        self.assertEqual(lesson["sources"][0]["url"], page)

    def test_sources_capped_at_three(self):
        urls = [f"https://example.org/p{i}" for i in range(5)]
        payload = dict(GOOD_B, sources=[{"url": u, "supports": "x"} for u in urls])
        self.assertEqual(len(self.lesson(payload, tool_calls=[search_call(*urls)])["sources"]), 3)

    def test_source_note_with_url_fails(self):
        payload = dict(GOOD_B, sources=[{"url": "https://example.com/Guide/Evals",
                                         "supports": "see https://evil.example"}])
        with self.assertRaises(df.LLMError):
            self.lesson(payload)

    def test_inline_citation_links_are_stripped(self):
        payload = dict(GOOD_B, concept="Long contexts degrade recall ([example.com](https://example.com/x)).")
        self.assertEqual(self.lesson(payload)["concept"], "Long contexts degrade recall.")

    def test_bare_url_in_text_fails(self):
        payload = dict(GOOD_A, research_body="See https://evil.example for more.")
        client = FakeClient({"grounded_prose": [response(payload, tool_calls=[])]})
        with self.assertRaises(df.LLMError):
            df.write_grounded_prose("Jamaica", "x", "Topic", "y", client=client)

    def test_overlong_field_fails(self):
        payload = dict(GOOD_A, country_blurb="word " * 200)
        client = FakeClient({"grounded_prose": [response(payload, tool_calls=[])]})
        with self.assertRaises(df.LLMError):
            df.write_grounded_prose("Jamaica", "x", "Topic", "y", client=client)

    def test_length_caps(self):
        # Code caps sit deliberately above the prompt targets (70 / 100-140 / 120-170 words).
        for field, cap in (("country_blurb", 95), ("research_body", 190)):
            for words, ok in ((cap, True), (cap + 1, False)):
                client = FakeClient({"grounded_prose": [response(dict(GOOD_A, **{field: "word " * words}),
                                                                 tool_calls=[])]})
                if ok:
                    df.write_grounded_prose("Jamaica", "x", "Topic", "y", client=client)
                else:
                    with self.assertRaises(df.LLMError, msg=field):
                        df.write_grounded_prose("Jamaica", "x", "Topic", "y", client=client)
        self.assertEqual(len(self.lesson(dict(GOOD_B, concept="word " * 220))["concept"].split()), 220)
        with self.assertRaises(df.LLMError):
            self.lesson(dict(GOOD_B, concept="word " * 221))
        for target in ("at most 70 words", "100-140 words"):
            self.assertIn(target, df.GROUNDED_INSTRUCTIONS)
        self.assertIn("concept: 120-170 words", df.AI_INSTRUCTIONS)

    def test_markup_rejected_in_every_llm_field(self):
        bad_values = ('<a href="//evil.example">x</a>', "See [a [b]](/path) here.", "![img](x.png)",
                      "Visit //evil.example today.", "<img src=x onerror=alert(1)>", "< script>",
                      "Read [source][ref].\n\n[ref]: ftp://evil.example/article",
                      "Read [source][ref].\n\n[ref]: /path", "Read [source] [ref] now.",
                      "Read [source][] now.", "Intro.\n  [source]: /relative/page",
                      "Write to mailto:someone@example.com today.", "Fetch it over ftp://example.com/x.",
                      "See <ftp://example.com/x> or <mailto:a@example.com>.",
                      "[r]\n\n> [r]: javascript:alert(1)", "[r]\n\n- [r]: /evil", "[r]\n\n1. [r]: /evil",
                      "Write to user@example.org for details.", "$$ `` x $$ user@example.org ``",
                      "Inline $x^2$ maths.", "Paren \\(a+b\\) maths.", "Code:\n\n```\nascii art\n```")
        for bad in bad_values:
            client = FakeClient({"grounded_prose": [response(dict(GOOD_A, research_body=bad), tool_calls=[])]})
            with self.assertRaises(df.LLMError, msg=bad):
                df.write_grounded_prose("Jamaica", "x", "Topic", "y", client=client)
            for payload in (dict(GOOD_B, why_it_matters=[bad]),
                            dict(GOOD_B, sources=[{"url": "https://example.com/Guide/Evals", "supports": bad}])):
                with self.assertRaises(df.LLMError, msg=bad):
                    self.lesson(payload)
            for payload in (dict(GOOD_C, status="warning", notes=[bad]),
                            dict(GOOD_C, source_disagreements=[{"section": "flag", "note": bad}])):
                client = FakeClient({"run_check": [response(payload, tool_calls=[])]})
                with self.assertRaises(df.LLMError, msg=bad):
                    df.review_document("# doc", [], client=client)

    def test_cross_field_reference_link_is_caught(self):
        # A [r] in one field is harmless alone; the definition in another field is rejected...
        self.assertEqual(df._safe_text("The [r] marker.", "x", 50), "The [r] marker.")
        with self.assertRaises(df.LLMError):
            self.lesson(dict(GOOD_B, concept="Text.\n\n> [r]: /evil"))
        # ...and the whole-document invariant catches the combination anyway.
        with self.assertRaises(df.UnsafeDocumentError):
            df.check_links("The [r] marker.\n\n> [r]: /evil\n", {"x.png"})
        # The converter's preprocessing (math protection) is part of the check.
        with self.assertRaises(df.UnsafeDocumentError):
            df.check_links("$$ `` x $$ user@example.org ``\n", set())

    def test_currency_amounts_pass(self):
        text = "GDP was $5 billion in 2020 and $3 billion in 2021; reserves rose to $2.5 billion."
        self.assertEqual(df._safe_text(text, "x", 50), text)

    def test_ordinary_brackets_pass(self):
        for text in ("He wrote it [sic] in 1950.", "As noted [1], recall drops.",
                     "Arrays [a, b] and ratios 3:2 are fine."):
            payload = dict(GOOD_B, concept=text)
            self.assertEqual(self.lesson(payload)["concept"], text)

    def test_relative_markdown_link_is_stripped(self):
        payload = dict(GOOD_B, concept="Recall degrades [x](/path) with length.")
        self.assertEqual(self.lesson(payload)["concept"], "Recall degrades with length.")

    def test_plain_prose_with_slashes_and_comparisons_passes(self):
        payload = dict(GOOD_B, concept="Use input/output pairs; latency < 2 s and recall > 90%.")
        self.assertEqual(self.lesson(payload)["concept"], payload["concept"])

    def test_empty_required_fields_fail(self):
        for payload in (dict(GOOD_B, topic_title="  "), dict(GOOD_B, concept=""),
                        dict(GOOD_B, why_it_matters=[" "])):
            with self.assertRaises(df.LLMError):
                self.lesson(payload)
        for field in ("country_blurb", "research_headline", "research_body"):
            client = FakeClient({"grounded_prose": [response(dict(GOOD_A, **{field: " \n "}), tool_calls=[])]})
            with self.assertRaises(df.LLMError, msg=field):
                df.write_grounded_prose("Jamaica", "x", "Topic", "y", client=client)

    def test_empty_blurb_allowed_when_country_source_empty(self):
        client = FakeClient({"grounded_prose": [response(dict(GOOD_A, country_blurb=""), tool_calls=[])]})
        prose = df.write_grounded_prose("Jamaica", "", "Topic", "y", client=client)
        self.assertEqual(prose["country_blurb"], "")

    def test_empty_field_retries_then_degrades(self):
        client = good_client()
        client.answers["ai_lesson"] = [response(dict(GOOD_B, concept=""))]
        _, facts = self.gather(client=client)
        self.assertIsNone(facts.ai)
        self.assertEqual(sum(r["text"]["format"]["name"] == "ai_lesson" for r in client.requests), 2)

    def test_client_retries_disabled_and_call_b_timeout(self):
        with patch("openai.OpenAI") as openai_cls:
            df._openai_client()
        self.assertEqual(openai_cls.call_args.kwargs, {"timeout": 60, "max_retries": 0})
        client = good_client()
        df.write_ai_lesson(self.data["ai_themes"][0], "s", [], client=client)
        self.assertEqual(client.requests[0]["timeout"], 120)

    def test_retry_once_then_succeed(self):
        client = good_client()
        client.answers["ai_lesson"] = [RuntimeError("blip"), response(GOOD_B)]
        _, facts = self.gather(client=client)
        self.assertEqual(facts.ai["topic_title"], "Context rot")
        self.assertFalse(any("call B" in d for d in facts.diagnostics))

    def test_llm_cannot_override_code_owned_values(self):
        payload = dict(GOOD_A, country_blurb="It has 99 million people and its capital is Paris.")
        client = good_client()
        client.answers["grounded_prose"] = [response(payload, tool_calls=[])]
        sel, facts = self.gather(client=client)
        body = df.render_body(sel, facts)
        self.assertIn("| Capital | Kingston |", body)
        self.assertIn("| Population | 2.8 million (2024) |", body)


# ---------- reviewer disagreements with sourced claims ----------

HAMMING = "Golay published an error-correcting code in 1949, before Hamming's 1950 code."
NOTE_SUFFIX = (f"Reviewer's source: [history.example/Golay-1949]({REVIEWER_URL}). "
               "The text above follows its cited source; "
               "this note is the AI reviewer's own view and may be wrong.")


class DisagreementTests(Base):
    def review_client(self, *disagreements, **kwargs):
        return with_disagreements(good_client(), *disagreements, **kwargs)

    def test_note_rendered_after_its_section_with_prose_unchanged(self):
        sel, facts = self.gather()
        facts.diagnostics.clear()
        plain = df.render_body(sel, facts)
        markdown, warnings, disagreements = df.build_document(
            sel, facts, client=self.review_client(("research", HAMMING)))
        research = markdown.split("## Research Fact", 1)[1].split("\n## ", 1)[0]
        self.assertTrue(research.rstrip().endswith(f"> **AI reviewer note:** {HAMMING} {NOTE_SUFFIX}"))
        self.assertRegex(research, r"Source: \[[^\n]*Wikipedia\]\([^)]*\)\n\n> \*\*AI reviewer note:\*\*")
        # The sourced prose is untouched: removing the note gives back the plain body.
        self.assertIn(plain.split("## AI Engineering", 1)[0].split("## Research Fact", 1)[1].rstrip(), research)
        self.assertEqual(markdown.count("AI reviewer note"), 1)
        self.assertEqual(disagreements, [{"section": "research", "note": HAMMING, "url": REVIEWER_URL}])
        self.assertEqual(warnings, [])
        self.assertIn(REVIEWER_URL, df.allowed_links(sel, facts, disagreements))
        df.check_links(markdown, df.allowed_links(sel, facts, disagreements))  # no exception
        with self.assertRaises(df.UnsafeDocumentError):
            df.check_links(markdown, df.allowed_links(sel, facts))

    def test_box_points_at_disagreements_without_counting_them(self):
        sel, facts = self.gather()
        facts.diagnostics.clear()
        markdown, warnings, _ = df.build_document(
            sel, facts, client=self.review_client(("research", HAMMING), ("uk_vs_jamaica", "Looks low.")))
        lines = markdown.splitlines()
        self.assertEqual(lines[4], "> **Run check — warning:** AI reviewer disagrees with a sourced claim in: "
                                   "Research Fact, UK vs Jamaica.")
        self.assertEqual(warnings, [])

    def test_pointer_follows_notes_and_diagnostics(self):
        sel, facts = self.gather()
        client = self.review_client(("research", HAMMING), status="warning", notes=["UK value looks odd."])
        markdown, warnings, _ = df.build_document(sel, facts, client=client)
        box = markdown.split("\n\n## ", 1)[0]
        self.assertIn("> **Run check — warning:** UK value looks odd.\n>\n"
                      "> AI reviewer disagrees with a sourced claim in: Research Fact.\n>\n> - ", box)
        self.assertEqual(warnings, ["UK value looks odd."] + facts.diagnostics)

    def test_no_note_under_unavailable_section(self):
        client = self.review_client(("ai", "That claim is disputed."), ("research", HAMMING))
        client.answers["ai_lesson"] = [RuntimeError("down")]
        sel, facts = self.gather(client=client)
        markdown, _, disagreements = df.build_document(sel, facts, client=client)
        ai = markdown.split("## AI Engineering", 1)[1].split("\n## ", 1)[0]
        self.assertIn("Unavailable today: the AI Engineering topic", ai)
        self.assertNotIn("AI reviewer note", ai)
        self.assertNotIn("AI Engineering.", markdown.split("\n\n## ", 1)[0])
        self.assertEqual([d["section"] for d in disagreements], ["research"])

    def test_duplicates_dropped_and_capped(self):
        client = self.review_client(("research", HAMMING), ("research", HAMMING), ("flag", "a."),
                                    ("ai", "b."), ("uk_vs_jamaica", "c."))
        review = df.review_document("# doc", [], client=client)
        self.assertEqual([d["section"] for d in review["source_disagreements"]], ["research", "flag", "ai"])

    def test_invalid_disagreements_fail_validation_and_fall_back(self):
        for bad in (("economy", "Wrong section."), ("research", "See https://example.com for the 1949 code."),
                    ("research", "word " * 60)):
            client = self.review_client(bad)
            with self.assertRaises(df.LLMError, msg=bad[0]):
                df.review_document("# doc", [], client=client)
            sel, facts = self.gather()
            markdown, _, disagreements = df.build_document(sel, facts, client=client)
            self.assertIn("Run check unavailable; raw diagnostics below.", markdown)
            self.assertNotIn("AI reviewer note", markdown)
            self.assertEqual(disagreements, [])

    def test_no_tools_and_no_source_check_without_disagreements(self):
        client = good_client()
        review = df.review_document("# doc", [], client=client)
        self.assertEqual(review["source_disagreements"], [])
        self.assertEqual(len(client.requests), 1)
        for key in ("tools", "tool_choice", "max_tool_calls", "include", "timeout"):
            self.assertNotIn(key, client.requests[0])

    def test_source_check_runs_once_with_one_search(self):
        client = self.review_client(("research", HAMMING), ("flag", "Disputed."))
        review = df.review_document("# doc", [], client=client)
        self.assertEqual([r["text"]["format"]["name"] for r in client.requests], ["run_check", "source_check"])
        self.assertNotIn("tools", client.requests[0])
        req = client.requests[1]
        self.assertEqual(req["tools"], [{"type": "web_search"}])
        self.assertEqual(req["tool_choice"], "required")
        self.assertEqual(req["max_tool_calls"], 1)
        self.assertEqual(req["include"], ["web_search_call.action.sources"])
        self.assertEqual(req["timeout"], 120)
        self.assertIn(HAMMING, req["input"])
        self.assertIn("Disputed.", req["input"])
        self.assertEqual(len(review["source_disagreements"]), 2)

    def test_source_check_sees_disputed_section_with_its_sources(self):
        client = self.review_client(("research", HAMMING), ("research", "Also disputed."))
        sel, facts = self.gather(client=client)
        df.build_document(sel, facts, client=client)
        req = client.requests[-1]
        self.assertEqual(req["text"]["format"]["name"], "source_check")
        research = df.render_sections(sel, facts)["research"]
        self.assertIn(f"SECTION: research\n{research}\n\nDISAGREEMENTS:\n- {HAMMING}\n- Also disputed.",
                      req["input"])
        self.assertIn(facts.research_wiki.url, req["input"])   # the section's own source link
        self.assertEqual(req["input"].count("SECTION:"), 1)    # only disputed sections, each once
        self.assertIn("justified in context", req["instructions"])
        self.assertNotIn("supports the reviewer's view", req["instructions"])

    def test_source_check_failure_drops_disagreements(self):
        client = self.review_client(("research", HAMMING))
        client.answers["source_check"] = [RuntimeError("down")]
        sel, facts = self.gather()
        facts.diagnostics.clear()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            markdown, warnings, disagreements = df.build_document(sel, facts, client=client)
        self.assertIn("source check failed", err.getvalue())
        self.assertNotIn("AI reviewer note", markdown)
        self.assertNotIn("Run check", markdown)  # not a diagnostic
        self.assertEqual((warnings, disagreements, facts.diagnostics), ([], [], []))

    def test_disagreement_only_warning_becomes_ok_when_all_dropped(self):
        for answer in (RuntimeError("down"),                                   # source check fails
                       response({"source_disagreements": []},                  # drops everything
                                tool_calls=[search_call(REVIEWER_URL)])):
            client = self.review_client(("research", HAMMING), status="warning")
            client.answers["source_check"] = [answer]
            sel, facts = self.gather()
            facts.diagnostics.clear()
            with contextlib.redirect_stderr(io.StringIO()):
                markdown, warnings, disagreements = df.build_document(sel, facts, client=client)
            self.assertNotIn("Run check", markdown)
            self.assertEqual((warnings, disagreements), ([], []))

    def test_real_notes_survive_dropped_disagreements(self):
        client = self.review_client(("research", HAMMING), status="warning", notes=["UK value looks odd."])
        client.answers["source_check"] = [RuntimeError("down")]
        sel, facts = self.gather()
        facts.diagnostics.clear()
        with contextlib.redirect_stderr(io.StringIO()):
            markdown, warnings, disagreements = df.build_document(sel, facts, client=client)
        self.assertEqual(markdown.splitlines()[4], "> **Run check — warning:** UK value looks odd.")
        self.assertEqual((warnings, disagreements), (["UK value looks odd."], []))

    def test_empty_warning_without_disagreements_keeps_generic_warning(self):
        client = good_client()
        client.answers["run_check"] = [response(dict(GOOD_C, status="warning"), tool_calls=[])]
        sel, facts = self.gather()
        facts.diagnostics.clear()
        markdown, warnings, _ = df.build_document(sel, facts, client=client)
        self.assertEqual(markdown.splitlines()[4],
                         "> **Run check — warning:** the reviewer flagged a problem but gave no details.")
        self.assertEqual(warnings, ["Run check flagged a problem without details."])
        self.assertEqual([r["text"]["format"]["name"] for r in client.requests], ["run_check"])  # no source check

    def test_source_check_cannot_add_new_disagreements(self):
        client = self.review_client(("research", HAMMING))
        client.answers["source_check"] = [response({"source_disagreements": [
            {"section": "research", "note": HAMMING, "url": REVIEWER_URL},
            {"section": "ai", "note": "Something call C never said.", "url": REVIEWER_URL}]},
            tool_calls=[search_call(REVIEWER_URL)])]
        with contextlib.redirect_stderr(io.StringIO()):
            review = df.review_document("# doc", [], client=client)
        self.assertEqual([d["section"] for d in review["source_disagreements"]], ["research"])

    def test_unsourced_disagreements_dropped(self):
        for url, tool_calls in (("https://elsewhere.example/golay", None),  # not a tool URL
                                ("", None),                                   # no url given
                                (REVIEWER_URL, [])):                           # never searched
            client = self.review_client(("research", HAMMING), url=url, tool_calls=tool_calls)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                review = df.review_document("# doc", [], client=client)
            self.assertEqual(review["source_disagreements"], [], msg=url)
            self.assertIn("dropped reviewer disagreement", err.getvalue())
            sel, facts = self.gather()
            facts.diagnostics.clear()
            with contextlib.redirect_stderr(io.StringIO()):
                markdown, warnings, disagreements = df.build_document(sel, facts, client=client)
            self.assertNotIn("AI reviewer note", markdown)
            self.assertNotIn("Run check", markdown)  # dropped silently: not a diagnostic or failure
            self.assertEqual((warnings, disagreements), ([], []))

    def test_matched_url_is_rendered_from_tool_metadata(self):
        client = self.review_client(("research", HAMMING), url="HTTPS://EXAMPLE.COM/Guide/Evals#x",
                                    tool_calls=[search_call(TOOL_URL)])
        review = df.review_document("# doc", [], client=client)
        self.assertEqual(review["source_disagreements"][0]["url"], TOOL_URL)

    def test_schema_is_strict(self):
        client = good_client()
        df.review_document("# doc", [], client=client)
        items = client.requests[0]["text"]["format"]["schema"]["properties"]["source_disagreements"]["items"]
        self.assertFalse(items["additionalProperties"])
        self.assertEqual(items["required"], ["section", "note"])
        self.assertEqual(items["properties"]["section"]["enum"], list(df.SECTION_NAMES))
        client = self.review_client(("research", HAMMING))
        df.review_document("# doc", [], client=client)
        items = client.requests[1]["text"]["format"]["schema"]["properties"]["source_disagreements"]["items"]
        self.assertFalse(items["additionalProperties"])
        self.assertEqual(items["required"], ["section", "note", "url"])

    def test_disagreements_stored_in_state(self):
        state_path = os.path.join(self.tmp, "state.json")
        with patch.object(df, "http_get", FakeHTTP(default_routes())), \
                patch("daily_facts.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            df.main(["--date", "2026-10-10", "--state", state_path],
                    client=self.review_client(("research", HAMMING)))
        entry = df.load_state(state_path)["sent"]["2026-10-10"]
        self.assertEqual(entry["reviewer_disagreements"],
                         [{"section": "research", "note": HAMMING, "url": REVIEWER_URL}])
        self.assertFalse(any(HAMMING in w for w in entry["warnings"]))


# ---------- UK comparisons (call D) ----------

def comparison_client(*sentences):
    return FakeClient({"comparison": [response({"sentence": x}, tool_calls=[]) for x in sentences]})


class ComparisonTests(Base):
    def compare(self, sentence, block="JAMAICA: 4.2% (2025)\nUK: 3.1% (2025)\nRELATION: 1.1 percentage "
                                      "points higher than the UK's",
                relation="1.1 percentage points higher than the UK's"):
        return df.write_comparison(block, relation, client=comparison_client(sentence))

    def test_land_relation(self):
        self.assertEqual(df.land_relation(10_830, 241_930), "96% smaller than the UK")    # Jamaica
        self.assertEqual(df.land_relation(2_736_690, 241_930), "1,031% larger than the UK")  # Argentina
        self.assertEqual(df.land_relation(241_930, 241_930), "the same size as the UK")
        self.assertEqual(df.land_relation(244_000, 241_930), "0.86% larger than the UK")
        self.assertEqual(df.land_relation(2.084, 241_930), "99.999% smaller than the UK")  # Monaco: never 100%
        self.assertEqual(df.format_area(2.084), "2.08 km²")
        self.assertEqual(df.format_area(241_930), "241,930 km²")

    def test_land_block_carries_values_years_and_relation(self):
        sel = self.jamaica_selection()
        facts = df.Facts(land={"value": 2_736_690, "uk_value": 241_930, "year": 2024})
        block, relation = df.land_block(sel, facts)
        self.assertEqual(relation, "1,031% larger than the UK")
        for text in ("LAND AREA: 2,736,690 km² (2024)", "UK LAND AREA: 241,930 km² (2024)",
                     "SIZE RATIO: 11.3× the UK's", "RELATION: 1,031% larger than the UK"):
            self.assertIn(text, block)

    def test_economy_relation(self):
        self.assertEqual(df.economy_relation(4.24, 3.06, "pct"), "1.1 percentage points higher than the UK's")
        self.assertEqual(df.economy_relation(-1.0, 2.3, "pct"), "3.3 percentage points lower than the UK's")
        self.assertEqual(df.economy_relation(2.04, 1.96, "pct"), "the same as the UK's")  # equal as displayed
        self.assertEqual(df.economy_relation(6_000, 50_000, "usd"), "12% of the UK's")

    def test_valid_sentence_passes_with_decimals_and_curly_apostrophe(self):
        sentence = "Jamaica's 4.2% in 2025 was 1.1 percentage points higher than the UK’s 3.1%."
        self.assertEqual(self.compare(sentence), sentence)

    def test_relation_phrase_required(self):
        for bad in ("Jamaica's 4.2% was above the UK's 3.1% in 2025.",                   # paraphrased
                    "Jamaica's 4.2% was 1.1 percentage points lower than the UK's."):     # wrong direction
            with self.assertRaises(df.LLMError, msg=bad):
                self.compare(bad)

    def test_numeric_guard(self):
        with self.assertRaises(df.LLMError):   # invented number
            self.compare("At 4.2%, Jamaica was 1.1 percentage points higher than the UK's, up from 3.9%.")
        with self.assertRaises(df.LLMError):   # re-rounded number
            self.compare("Jamaica's 4% was 1.1 percentage points higher than the UK's.")
        block = "LAND AREA: 2,736,690 km² (2024)\nRELATION: 1,031% larger than the UK"
        self.assertTrue(df.write_comparison(block, "1,031% larger than the UK", client=comparison_client(
            "At 2736690 km² in 2024 it is 1,031% larger than the UK.")))  # commas ignored

    def test_numeric_guard_keeps_sign_and_scale(self):
        block = ("JAMAICA: $6 billion (2025)\nUK: -1.0% (2025)\n"
                 "UK: no figure available in World Bank data for 2019-2026\nRELATION: 12% of the UK's")
        ok = df.write_comparison(block, "12% of the UK's", client=comparison_client(
            "At $6 billion it is 12% of the UK's, with no UK figure for 2019-2026 and the UK at -1.0%."))
        self.assertTrue(ok)
        for bad in ("At $6 million it is 12% of the UK's.",          # scale swapped
                    "It is 12% of the UK's, against the UK's 1.0%.",  # sign dropped
                    "It is 12% of the UK's, against the UK's \u22121.5%."):
            with self.assertRaises(df.LLMError, msg=bad):
                df.write_comparison(block, "12% of the UK's", client=comparison_client(bad))
        self.assertEqual(df._numbers("\u22121.0% and -1.0%"), {"-1.0"})

    def test_differing_years_must_both_appear(self):
        block = ("JAMAICA: 4.2% (2025)\nUK: 2.0% (2023)\nRELATION: 2.2 percentage points higher than the UK's\n"
                 "NOTE: the figures are from different years (Jamaica 2025, UK 2023)")
        relation = "2.2 percentage points higher than the UK's"
        with self.assertRaises(df.LLMError):
            df.write_comparison(block, relation, client=comparison_client(
                "Jamaica's 4.2% in 2025 was 2.2 percentage points higher than the UK's."))
        self.assertTrue(df.write_comparison(block, relation, client=comparison_client(
            "Jamaica's 4.2% in 2025 was 2.2 percentage points higher than the UK's 2.0% in 2023.")))

    def test_one_sentence_guard(self):
        for bad in ("It is 1.1 percentage points higher than the UK's. That is notable.",
                    "It is 1.1 percentage points higher than the UK's. and that is notable",
                    "It is 1.1 percentage points higher than the UK's\nin 2025.",
                    "", "  "):
            with self.assertRaises(df.LLMError, msg=bad):
                self.compare(bad)
        with self.assertRaises(df.LLMError):
            self.compare("It is 1.1 percentage points higher than the UK's " + "word " * 40)
        with self.assertRaises(df.LLMError):   # markup goes through _safe_text
            self.compare("It is [1.1 percentage points higher than the UK's](https://evil.example).")

    def test_call_d_request_shape(self):
        client = comparison_client("It is 1.1 percentage points higher than the UK's.")
        df.write_comparison("RELATION: 1.1 percentage points higher than the UK's",
                            "1.1 percentage points higher than the UK's", client=client)
        req = client.requests[0]
        self.assertEqual(req["reasoning"], {"effort": "low"})
        for key in ("tools", "tool_choice", "include"):
            self.assertNotIn(key, req)
        schema = req["text"]["format"]["schema"]
        self.assertTrue(req["text"]["format"]["strict"])
        self.assertEqual(schema["required"], ["sentence"])
        self.assertFalse(schema["additionalProperties"])

    def comparison_requests(self, client):
        return [r for r in client.requests if r["text"]["format"]["name"] == "comparison"]

    def test_land_comparison_rendered(self):
        client = good_client()
        sel, facts = self.gather(client=client)
        markdown, _, _ = df.build_document(sel, facts, client=client)
        flag = markdown.split("## Flag of the Day", 1)[1].split("\n## ", 1)[0]
        self.assertIn("| Land area | 10,830 km² (2024) |", flag)
        self.assertIn("|\n\n**Size vs UK:** It is 96% smaller than the UK.\n\nAn island", flag)
        self.assertEqual(len(self.comparison_requests(client)), 1)

    def test_land_comparison_failure_keeps_section_and_reviewer_notes(self):
        client = with_disagreements(good_client(), ("flag", "Disputed."))
        client.answers["comparison"] = [RuntimeError("down")]
        sel, facts = self.gather(client=client)
        markdown, _, disagreements = df.build_document(sel, facts, client=client)
        flag = markdown.split("## Flag of the Day", 1)[1].split("\n## ", 1)[0]
        self.assertIn("*UK comparison unavailable today.*", flag)
        self.assertIn("AI reviewer note", flag)     # not suppressed like an unavailable section
        self.assertTrue(any("Land area comparison (call D) failed twice" in d for d in facts.diagnostics))
        self.assertEqual([d["section"] for d in disagreements], ["flag"])

    def test_links_remain_code_owned(self):
        client = good_client()
        client.answers["comparison"] = [response({"sentence": "It is 96% smaller than the UK, see "
                                                               "[here](https://evil.example)."}, tool_calls=[])]
        sel, facts = self.gather(client=client)
        markdown, _, _ = df.build_document(sel, facts, client=client)
        self.assertNotIn("evil.example", markdown)
        links = set(df.rendered_links(df.converter_view(markdown)[0]))
        self.assertEqual(links, df.allowed_links(sel, facts))
        self.assertIn(df.world_bank_url(df.LAND_AREA, "JM"), links)

    def test_uk_as_country_makes_no_land_call(self):
        sel = df.select(DATE, self.data)
        sel.country = next(c for c in self.data["countries"] if c["cca3"] == "GBR")
        client = good_client()
        _, facts = self.gather(default_routes("GBR"), client=client, sel=sel)
        self.assertIsNotNone(facts.land)
        self.assertIsNone(facts.land_comparison)
        self.assertEqual(self.comparison_requests(client), [])
        flag = df.render_body(sel, facts).split("## Flag of the Day", 1)[1].split("\n## ", 1)[0]
        self.assertIn("| Land area | 241,930 km² (2024) |", flag)
        self.assertNotIn("vs UK:**", flag)
        self.assertNotIn("UK comparison unavailable", flag)

    def test_land_area_uses_latest_common_year(self):
        routes = default_routes()
        routes["AG.LND.TOTL.K2?format=json&per_page=200&date="] = wb_page([
            wb_row("JAM", 2023, 10_830, df.LAND_AREA), wb_row("GBR", 2023, 241_930, df.LAND_AREA),
            wb_row("GBR", 2024, 241_930, df.LAND_AREA)])
        _, facts = self.gather(routes)
        self.assertEqual(facts.land, {"value": 10_830, "uk_value": 241_930, "year": 2023})
        self.assertIn("Land area comparison uses 2023, the latest year both countries have.", facts.diagnostics)


# ---------- UK vs Jamaica fact (APIs, call G, writer) ----------

class CountryFactTests(Base):
    def observations(self, fact_id, routes=None, year=2026):
        with patch.object(df, "http_get", FakeHTTP(routes or default_routes())):
            return df.api_observations(self.fact(fact_id), year)

    def test_imf_drops_other_countries_and_projections(self):
        obs = self.observations("gov-debt")
        url = "https://www.imf.org/external/datamapper/GGXWDG_NGDP@WEO/GBR/JAM"
        self.assertEqual(obs, {"GBR": {"value": 102.1, "year": 2025, "url": url, "estimate": True, "via": "api"},
                               "JAM": {"value": 66.0, "year": 2025, "url": url, "estimate": True, "via": "api"}})

    def test_imf_missing_country_and_malformed(self):
        routes = default_routes()
        routes["imf.org/external/datamapper/api"] = lambda url: imf_page(url, {"GBR": IMF_VALUES["GBR"]})
        self.assertEqual(list(self.observations("gov-debt", routes)), ["GBR"])
        for bad in ({"GBR": {"2025": "102.1"}}, {"GBR": {"20x5": 1.0}}, {"GBR": {"2025": None}}):
            routes["imf.org/external/datamapper/api"] = lambda url, bad=bad: imf_page(url, bad)
            with self.assertRaises(df.FetchError, msg=bad):
                self.observations("gov-debt", routes)
        routes["imf.org/external/datamapper/api"] = b'{"values": {}}'
        with self.assertRaises(df.FetchError):
            self.observations("gov-debt", routes)

    def test_who_pins_dimensions(self):
        http = FakeHTTP(default_routes())
        with patch.object(df, "http_get", http):
            obs = df.api_observations(self.fact("life-expectancy"), 2026)
        self.assertIn("$filter=(SpatialDim%20eq%20'GBR'%20or%20SpatialDim%20eq%20'JAM')%20and%20Dim1%20eq%20"
                      "'SEX_BTSX'", http.calls[0])
        self.assertEqual({k: (v["value"], v["year"]) for k, v in obs.items()},
                         {"GBR": (80.7, 2023), "JAM": (72.6, 2023)})
        self.assertEqual(obs["GBR"]["url"], self.fact("life-expectancy")["page"])

    def test_who_ambiguous_rows_and_future_years(self):
        unpinned = dict(self.fact("life-expectancy"), filter={})
        with patch.object(df, "http_get", FakeHTTP(default_routes())), self.assertRaises(df.FetchError):
            df.api_observations(unpinned, 2026)   # three sexes per country and year
        routes = default_routes()
        routes["ghoapi.azureedge.net"] = lambda url: who_page(url, WHO_ROWS + [
            {"SpatialDim": "JAM", "TimeDim": 2030, "Dim1": "SEX_BTSX", "Dim2": None, "NumericValue": 1.0}])
        self.assertEqual(self.observations("life-expectancy", routes)["JAM"]["year"], 2023)

    def test_years_differ_when_no_common_year(self):
        routes = default_routes()
        routes["country/GBR;JAM/indicator/"] = wb_page([wb_row("GBR", 2025, 3.1), wb_row("JAM", 2023, 4.2)])
        client = good_client()
        sel, facts = self.gather(routes, client=client)
        obs = facts.country_fact["observations"]
        self.assertEqual((obs["GBR"]["year"], obs["JAM"]["year"]), (2025, 2023))
        block = next(r["input"] for r in client.requests if r["text"]["format"]["name"] == "country_fact")
        self.assertIn("NOTE: the figures are from different years (Jamaica 2023, UK 2025)", block)
        self.assertNotIn("country_fact_search", [r["text"]["format"]["name"] for r in client.requests])

    def search_requests(self, client):
        return [r for r in client.requests if r["text"]["format"]["name"] == "country_fact_search"]

    def test_one_missing_country_is_searched_on_the_source_site(self):
        routes = default_routes()
        routes["country/GBR;JAM/indicator/"] = wb_page([wb_row("GBR", 2024, 95.5)])
        client = good_client()
        client.answers["country_fact_search"] = [fact_search(found("Jamaica", 90.1, 2024, WB_PAGE), urls=[WB_PAGE])]
        sel, facts = self.gather(routes, client=client)
        req = self.search_requests(client)[0]
        self.assertEqual(req["tools"], [{"type": "web_search", "filters": {"allowed_domains": ["worldbank.org"]}}])
        self.assertEqual((req["tool_choice"], req["max_tool_calls"], req["timeout"]), ("required", 2, 120))
        self.assertEqual(req["include"], ["web_search_call.action.sources"])
        self.assertIn("COUNTRIES: Jamaica\n", req["input"])
        self.assertEqual(facts.country_fact["searched"], ["JAM"])
        markdown, _, _ = df.build_document(sel, facts, client=client)
        fact = markdown.split("## UK vs Jamaica", 1)[1]
        self.assertIn("| 95.5% (2024) | 90.1% (2024) |", fact)
        self.assertIn("> **Note:** Jamaica's figure was found by an AI (LLM) web search fallback of "
                      "data.worldbank.org, not from a data API.", fact)
        self.assertIn(f"[World Bank — IT.NET.USER.ZS (UK)]({df.world_bank_url('IT.NET.USER.ZS', 'GB')}) · "
                      f"[data.worldbank.org/indicator/IT.NET.USER.ZS (Jamaica)]({WB_PAGE})", fact)
        self.assertIn(WB_PAGE, df.allowed_links(sel, facts))
        self.assertTrue(any("no recent World Bank figure for Jamaica" in d for d in facts.diagnostics))

    def test_api_failure_searches_both_and_search_failure_is_unavailable(self):
        routes = default_routes()
        routes["imf.org/external/datamapper/api"] = TimeoutError()
        imf_page_url = "https://www.imf.org/external/datamapper/GGXWDG_NGDP@WEO/GBR"
        client = good_client()
        client.answers["country_fact_search"] = [fact_search(found("UK", 101.0, 2025, imf_page_url),
                                                             found("Jamaica", 66.0, 2025, imf_page_url),
                                                             urls=[imf_page_url])]
        sel, facts = self.gather(routes, client=client, sel=self.jamaica_selection("gov-debt"))
        self.assertIn("COUNTRIES: UK, Jamaica", self.search_requests(client)[0]["input"])
        section = df.render_sections(sel, facts)["uk_vs_jamaica"]
        self.assertIn("> **Note:** Both figures were found by an AI (LLM) web search fallback of imf.org", section)
        self.assertEqual(section.count(imf_page_url), 1)   # one link for both countries' shared page
        # The search itself failing (here: no search made) leaves the section unavailable, not invented.
        client = good_client()
        client.answers["country_fact_search"] = [response({"countries": [found("UK", 101.0, 2025, imf_page_url)]},
                                                          tool_calls=[])]
        sel, facts = self.gather(routes, client=client, sel=self.jamaica_selection("gov-debt"))
        self.assertEqual(len(self.search_requests(client)), 2)   # retried once
        self.assertIn("Unavailable today: the UK vs Jamaica fact", df.render_sections(sel, facts)["uk_vs_jamaica"])
        self.assertTrue(any("(call G) failed twice (no web search was made)" in d for d in facts.diagnostics))

    def test_search_only_fact(self):
        client = good_client()
        client.answers["country_fact_search"] = [fact_search(found("UK", 133.0, 2025, PRISON_UK),
                                                             found("Jamaica", 129.0, 2024, PRISON_JM))]
        sel, facts = self.gather(client=client, sel=self.jamaica_selection("prison-rate"))
        req = self.search_requests(client)[0]
        self.assertEqual(req["tools"][0]["filters"], {"allowed_domains": ["prisonstudies.org"]})
        self.assertIn("UNIT: per 100,000 people", req["input"])
        block = next(r["input"] for r in client.requests if r["text"]["format"]["name"] == "country_fact")
        self.assertIn("JAMAICA: 129.0 per 100,000 people (2024) (found by an AI web search of prisonstudies.org; "
                      "it counts: Prisoners per 100,000 of the national population.)", block)
        markdown, _, _ = df.build_document(sel, facts, client=client)
        fact = markdown.split("## UK vs Jamaica", 1)[1]
        self.assertIn("| Prison population rate (UK: England and Wales) | 133.0 per 100,000 people (2025) | 129.0 per 100,000 people "
                      "(2024) |", fact)
        self.assertIn("> **Note:** These figures were found by an AI (LLM) web search of prisonstudies.org; "
                      "no data API covers this fact.", fact)
        self.assertIn(f"[prisonstudies.org/country/jamaica (Jamaica)]({PRISON_JM})", fact)
        df.check_links(markdown, df.allowed_links(sel, facts))

    def test_search_validation_is_per_country(self):
        cases = (found("Jamaica", 129.0, 2024, "https://www.prisonstudies.org/country/made-up"),   # not a tool URL
                 found("Jamaica", 129.0, 2024, "https://evil.example/jamaica"),                     # off the domain
                 found("Jamaica", 129.0, 2024, "https://notprisonstudies.org/jamaica"),            # lookalike host
                 found("Jamaica", 129.0, 2005, PRISON_JM),                                          # too old
                 found("Jamaica", 129.0, 2031, PRISON_JM),                                          # future
                 found("Jamaica", float("nan"), 2024, PRISON_JM),
                 found("Jamaica", 129.0, 2024, PRISON_JM, unit_note="See https://evil.example"),
                 dict(found("Jamaica", 0, 0, ""), status="not_found"))
        for bad in cases:
            client = FakeClient({"country_fact_search": [fact_search(
                found("UK", 133.0, 2025, PRISON_UK), bad,
                urls=(PRISON_UK, PRISON_JM, "https://evil.example/jamaica", "https://notprisonstudies.org/jamaica"))]})
            if bad["value"] != bad["value"]:   # NaN cannot travel as JSON: check the parser directly
                with self.assertRaises(ValueError):
                    df._number(bad["value"])
                continue
            result = df.search_country_fact(self.fact("prison-rate"), ["GBR", "JAM"], DATE, client=client)
            self.assertEqual(list(result["observations"]), ["GBR"], bad)
            self.assertIn("JAM", result["rejected"], bad)
            self.assertEqual(result["observations"]["GBR"]["url"], PRISON_UK)
        sel = self.jamaica_selection("prison-rate")
        facts = df.Facts(country_fact={"observations": {"GBR": result["observations"]["GBR"]},
                                       "searched": ["GBR", "JAM"], "search_actions": {}})
        section = df.render_sections(sel, facts)["uk_vs_jamaica"]
        self.assertIn("| 133.0 per 100,000 people (2025) | not available today |", section)
        self.assertIn("> **Note:** UK's figure was found by an AI (LLM) web search of prisonstudies.org", section)

    def test_search_url_rendered_from_tool_metadata(self):
        client = FakeClient({"country_fact_search": [fact_search(
            found("Jamaica", 129.0, 2024, "HTTPS://WWW.PRISONSTUDIES.ORG/country/jamaica#rate"))]})
        result = df.search_country_fact(self.fact("prison-rate"), ["JAM"], DATE, client=client)
        self.assertEqual(result["observations"]["JAM"]["url"], PRISON_JM)

    def test_year_fact_has_no_relation(self):
        client = good_client()
        client.answers["country_fact_search"] = [fact_search(
            found("UK", 1801, 1801, "https://www.britannica.com/place/United-Kingdom"),
            found("Jamaica", 1962, 1962, "https://jis.gov.jm/information/independence"),
            urls=("https://www.britannica.com/place/United-Kingdom", "https://jis.gov.jm/information/independence"))]
        sel, facts = self.gather(client=client, sel=self.jamaica_selection("statehood"))
        block, relation = df.fact_block(sel.country_fact, facts.country_fact)
        self.assertEqual(relation, "")
        self.assertNotIn("different years", block)
        self.assertIn("| Year the present-day state was formed | 1801 | 1962 |",
                      df.render_sections(sel, facts)["uk_vs_jamaica"])

    def write(self, paragraph, fact_id="internet", observations=None):
        observations = observations or {"GBR": {"value": 2.9, "year": 2024, "url": "u", "estimate": False, "via": "api"},
                                        "JAM": {"value": 4.2, "year": 2024, "url": "v", "estimate": False, "via": "api"}}
        fact = {"observations": observations, "searched": [], "search_actions": {}}
        client = FakeClient({"country_fact": [response({"paragraph": paragraph}, tool_calls=[])]})
        return df.write_country_fact(self.fact(fact_id), fact, client=client), client

    def test_writer_accepts_free_paragraph_and_has_no_tools(self):
        text = ("Most people in both countries go online. Jamaica's 4.2% is 1.3 percentage points higher than "
                "the UK’s 2.9%, partly because figures are modelled.")
        paragraph, client = self.write(text)
        self.assertEqual(paragraph, text)
        for key in ("tools", "tool_choice", "include"):
            self.assertNotIn(key, client.requests[0])
        self.assertIn("RELATION: 1.3 percentage points higher than the UK's", client.requests[0]["input"])

    def test_writer_guards(self):
        for bad in ("Jamaica's figure is higher than the UK's.",                                  # no relation
                    "Jamaica's figure is 1.3 percentage points lower than the UK's.",              # wrong direction
                    "Jamaica's figure is 1.3 percentage points higher than the UK's, up from 3.5%.",  # invented
                    "Jamaica's figure is 1.3 percentage points higher than the UK's. " + "word " * 100,
                    "Jamaica's figure is [1.3 percentage points higher than the UK's](https://evil.example).",
                    ""):
            with self.assertRaises(df.LLMError, msg=bad):
                self.write(bad)
        differ = {"GBR": {"value": 2.9, "year": 2025, "url": "u", "estimate": False, "via": "api"},
                  "JAM": {"value": 4.2, "year": 2023, "url": "v", "estimate": False, "via": "api"}}
        with self.assertRaises(df.LLMError):
            self.write("Jamaica's figure is 1.3 percentage points higher than the UK's.", observations=differ)
        self.assertTrue(self.write("In 2023 Jamaica's figure is 1.3 percentage points higher than the UK's "
                                   "in 2025.", observations=differ)[0])

    def test_writer_accepts_ordinary_prose_numbers(self):
        ok = "In 2024, at 4.2% against 2.9%, Jamaica's figure is 1.3 percentage points higher than the UK's."
        self.assertEqual(self.write(ok)[0], ok)
        with self.assertRaises(df.LLMError):   # 4.2 is supplied only as a percentage
            self.write("Jamaica's figure is 1.3 percentage points higher than the UK's, about 4.2 in every 100.")

    def test_writer_numbers_need_their_unit_and_never_come_from_unit_notes(self):
        with self.assertRaises(df.LLMError):   # 4.2 is supplied, but as a %, not years
            self.write("Jamaica's figure is 1.3 percentage points higher than the UK's, at 4.2 years.")
        searched = {"GBR": {"value": 133.0, "year": 2025, "url": PRISON_UK, "estimate": False, "via": "search",
                            "unit_note": "Prisoners per 100,000 people on 31 March 2025.", "supports": "x"},
                    "JAM": {"value": 129.0, "year": 2024, "url": PRISON_JM, "estimate": False, "via": "search",
                            "unit_note": "Prisoners per 100,000 people.", "supports": "x"}}
        relation = "97% of the UK's"
        ok = f"Jamaica's rate is {relation}, with the UK's from 2025 and Jamaica's from 2024."
        self.assertEqual(self.write(ok, "prison-rate", searched)[0], ok)
        with self.assertRaises(df.LLMError):   # "31" appears only in the model-written unit note
            self.write(ok + " The UK count was taken on 31 March.", "prison-rate", searched)
        _, client = self.write(ok, "prison-rate", searched)
        self.assertIn("it counts: Prisoners per 100,000 people on 31 March 2025.", client.requests[0]["input"])

    def test_incomparable_fact_has_no_relation_and_shows_what_each_counts(self):
        uk_url = "https://www.gov.uk/government/statistics/statutory-homelessness-in-england"
        jm_url = "https://jis.gov.jm/homeless-count"
        client = good_client()
        client.answers["country_fact_search"] = [fact_search(
            found("UK", 135580, 2026, uk_url, unit_note="Households in temporary accommodation in England."),
            found("Jamaica", 2261, 2025, jm_url, unit_note="People found to be homeless in a street count."),
            urls=(uk_url, jm_url))]
        sel, facts = self.gather(client=client, sel=self.jamaica_selection("homelessness"))
        block, relation = df.fact_block(sel.country_fact, facts.country_fact)
        self.assertEqual(relation, "")
        self.assertNotIn("RELATION", block)
        facts.fact_text = None
        section = df.render_sections(sel, facts)["uk_vs_jamaica"]
        self.assertIn("| 135,580 (2026) | 2,261 (2025) |", section)
        self.assertIn("**What the figures count:** UK: Households in temporary accommodation in England. "
                      "Jamaica: People found to be homeless in a street count.", section)
        self.assertIn("The UK's figure is 135,580 (2026) and Jamaica's is 2,261 (2025).\n", section)  # no comparison
        self.assertFalse([f["id"] for f in self.data["country_facts"]
                          if f["id"] in ("homelessness", "statehood") and f.get("comparable", True)])

    def test_imf_search_fallback_keeps_the_api_year_policy_and_estimates(self):
        page = "https://www.imf.org/external/datamapper/GGXWDG_NGDP@WEO/JAM"
        client = FakeClient({"country_fact_search": [fact_search(found("UK", 102.0, 2026, page),
                                                                 found("Jamaica", 66.0, 2025, page), urls=[page])]})
        result = df.search_country_fact(self.fact("gov-debt"), ["GBR", "JAM"], DATE, client=client)
        self.assertEqual(result["rejected"], {"GBR": "implausible year"})   # 2026 is still a projection
        self.assertTrue(result["observations"]["JAM"]["estimate"])
        self.assertIn("may be an IMF estimate", df.fact_block(self.fact("gov-debt"), {
            "observations": result["observations"], "searched": ["JAM"], "search_actions": {}})[0])

    def test_who_staleness_threshold(self):
        _, facts = self.gather(sel=self.jamaica_selection("life-expectancy"))   # 2023 figures in 2026
        self.assertFalse(any("figure is from" in d for d in facts.diagnostics))
        routes = default_routes()
        routes["ghoapi.azureedge.net"] = lambda url: who_page(url, [r for r in WHO_ROWS if r["TimeDim"] == 2022])
        _, facts = self.gather(routes, sel=self.jamaica_selection("life-expectancy"))
        self.assertTrue(any("figure is from 2022" in d for d in facts.diagnostics))

    def test_writer_failure_uses_code_sentence(self):
        client = good_client()
        client.answers["country_fact"] = [RuntimeError("down")]
        sel, facts = self.gather(client=client)
        section = df.render_sections(sel, facts)["uk_vs_jamaica"]
        self.assertIn(f"{sel.country_fact['meaning']} The UK's figure is 2.9% (2024) and Jamaica's is 4.2% (2024). "
                      "Jamaica's figure is 1.3 percentage points higher than the UK's.", section)
        self.assertTrue(any("UK vs Jamaica writer failed twice" in d for d in facts.diagnostics))

    def test_imf_estimates_are_described_to_the_writer(self):
        client = good_client()
        sel, facts = self.gather(client=client, sel=self.jamaica_selection("gov-debt"))
        block = next(r["input"] for r in client.requests if r["text"]["format"]["name"] == "country_fact")
        self.assertIn("UK: 102.1% (2025) (IMF World Economic Outlook data, may be an IMF estimate)", block)
        self.assertIn("RELATION: 36.1 percentage points lower than the UK's", block)

    def test_fact_recorded_in_state(self):
        state_path = os.path.join(self.tmp, "state.json")
        with patch.object(df, "http_get", FakeHTTP(default_routes())), \
                patch("daily_facts.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            df.main(["--date", "2026-10-10", "--state", state_path], client=good_client())
        entry = df.load_state(state_path)["sent"]["2026-10-10"]
        self.assertEqual(entry["country_fact"], df.select(DATE, self.data).country_fact["id"])
        self.assertIn("country_fact_sources", entry)


# ---------- tomorrow's date + date line ----------

class TargetDateTests(Base):
    def test_target_is_tomorrow_across_month_and_year(self):
        for today, tomorrow in ((datetime.date(2026, 10, 12), datetime.date(2026, 10, 13)),
                                (datetime.date(2026, 10, 31), datetime.date(2026, 11, 1)),
                                (datetime.date(2026, 12, 31), datetime.date(2027, 1, 1))):
            with patch.object(df, "today_london", return_value=today):
                self.assertEqual(df.target_date(), tomorrow)

    def test_main_without_date_builds_and_records_tomorrow(self):
        state_path = os.path.join(self.tmp, "state.json")
        df.save_state(state_path, {"sent": {"2026-10-12": {"ai_topic": "x"}}})
        with patch.object(df, "today_london", return_value=datetime.date(2026, 10, 12)), \
                patch.object(df, "http_get", FakeHTTP(default_routes())), \
                patch("daily_facts.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(df.main(["--state", state_path], client=good_client()), df.EXIT_SENT)
        self.assertIn("2026-10-13", df.load_state(state_path)["sent"])
        self.assertTrue(run.call_args.args[0][2].endswith("daily_facts_2026-10-13.md"))
        with open(os.path.join(df.INPUT_DIR, "daily_facts_2026-10-13.md"), encoding="utf-8") as f:
            self.assertTrue(f.read().startswith("# Daily Facts — Tuesday 13 October 2026\n\n"
                                                "Today is Tuesday 13th October 2026.\n"))

    def test_date_line_ordinals(self):
        for day, text in ((1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"),
                          (13, "13th"), (21, "21st"), (22, "22nd"), (23, "23rd"), (31, "31st")):
            self.assertIn(f" {text} ", df.date_line(datetime.date(2026, 10, day)))
        self.assertEqual(df.date_line(datetime.date(2026, 10, 13)), "Today is Tuesday 13th October 2026.")


# ---------- weather (Open-Meteo + call E) ----------

UTC = datetime.timezone.utc


def utc(*args):
    return datetime.datetime(*args, tzinfo=UTC)


WEATHER = {"description": "light rain", "high": "20°C", "low": "13°C", "rain_chance": "64%",
           # 13:00-16:00 London (BST) on DATE
           "rain_periods": [{"start": utc(2026, 10, 10, 12), "end": utc(2026, 10, 10, 15), "kind": "showers",
                             "intensity": "light"}],
           "sunrise": "07:20 (7:20am)", "sunset": "18:11 (6:11pm)"}


def weather_client(*paragraphs):
    return FakeClient({"weather": [response({"paragraph": p}, tool_calls=[]) for p in paragraphs]})


def periods(first_end, chances, amounts=None, codes=None):
    """rain_periods() over consecutive hourly labels from first_end, as
    (London start, London end, kind, intensity)."""
    instants = [first_end + datetime.timedelta(hours=i) for i in range(len(chances))]
    amounts = amounts or [0.0] * len(chances)
    codes = codes or [3] * len(chances)
    return [(df._clock(p["start"]), df._clock(p["end"]), p["kind"], p["intensity"])
            for p in df.rain_periods(instants, chances, amounts, codes)]


class WeatherTests(Base):
    def fetch(self, route=forecast, date=DATE):
        with patch.object(df, "http_get", FakeHTTP({"api.open-meteo.com": route})):
            return df.croydon_weather(date)

    def test_forecast_becomes_code_strings(self):
        self.assertEqual(self.fetch(), WEATHER)

    def test_requests_the_date_and_the_next(self):
        http = FakeHTTP({"api.open-meteo.com": forecast})
        with patch.object(df, "http_get", http):
            df.croydon_weather(DATE)
        self.assertIn("start_date=2026-10-10&end_date=2026-10-11", http.calls[0])

    def test_clock_gives_both_forms_in_london_time(self):
        self.assertEqual([df._clock(utc(2026, 10, 10, h)) for h in (23, 8, 11, 14)],
                         ["midnight", "09:00 (9am)", "12:00 (midday)", "15:00 (3pm)"])
        self.assertEqual(df._clock(utc(2026, 10, 27, 6, 44)), "06:44 (6:44am)")   # GMT
        self.assertEqual(df._forms("15:00 (3pm)"), ["15:00", "3pm"])

    def test_each_label_covers_the_preceding_hour(self):
        # Labels 10:00..13:00 BST; a 60% chance on the 11:00 label is rain from 10:00 to 11:00.
        self.assertEqual(periods(utc(2026, 10, 10, 9), [0, 60, 0, 0]),
                         [("10:00 (10am)", "11:00 (11am)", "rain", "light")])
        self.assertEqual(periods(utc(2026, 10, 10, 9), [10, 0, 0.0, 0], amounts=[0, 0, 0.1, 0]),
                         [("11:00 (11am)", "12:00 (midday)", "rain", "light")])
        chances = [0] * 24
        for h in (6, 7, 13, 14, 15, 23):
            chances[h] = 50                     # 50% counts as likely; 49% does not
        chances[9] = 49
        got = periods(utc(2026, 10, 10, 0), chances)   # labels 01:00..24:00 BST
        self.assertEqual([p[:2] for p in got], [("06:00 (6am)", "08:00 (8am)"), ("13:00 (1pm)", "16:00 (4pm)"),
                                                ("23:00 (11pm)", "midnight")])

    def test_spring_forward_day(self):
        # 29 March 2026: 00:00 GMT to 23:00 UTC; 01:00 London never happens.
        day = datetime.date(2026, 3, 29)
        self.assertEqual(df.london_day(day), (utc(2026, 3, 29, 0), utc(2026, 3, 29, 23)))
        # Wet 00:00-01:00 GMT and the next real hour (02:00-03:00 BST): one spell, ending at a real time.
        self.assertEqual(periods(utc(2026, 3, 29, 1), [60, 60, 0]), [("midnight", "03:00 (3am)", "rain", "light")])
        # The whole day through the fetch: 23 labels inside it (offset GMT at the start).
        w = self.fetch(lambda url: forecast(url, chances=[0] * 22 + [70], amounts=[], offset=0), date=day)
        self.assertEqual([(df._clock(p["start"]), df._clock(p["end"])) for p in w["rain_periods"]],
                         [("23:00 (11pm)", "midnight")])

    def test_fall_back_day(self):
        # 25 October 2026: 23:00 UTC on the 24th to 00:00 UTC on the 26th, 25 real hours.
        day = datetime.date(2026, 10, 25)
        self.assertEqual(df.london_day(day), (utc(2026, 10, 24, 23), utc(2026, 10, 26, 0)))
        # Wet from London midnight through both 01:00 hours: three real hours, one spell, ending 02:00 GMT.
        self.assertEqual(periods(utc(2026, 10, 25, 0), [60, 60, 60, 0]),
                         [("midnight", "02:00 (2am)", "rain", "light")])
        # Through the fetch (fixed +1 offset): the 25th real hour exists and can be wet.
        w = self.fetch(lambda url: forecast(url, chances=[0] * 24 + [70], amounts=[]), date=day)
        self.assertEqual([(df._clock(p["start"]), df._clock(p["end"])) for p in w["rain_periods"]],
                         [("23:00 (11pm)", "midnight")])

    def test_gmt_date_with_bst_offset_labels(self):
        # Open-Meteo kept the +1 offset for 27 October (GMT): its 07:44 sunrise label is 06:44 London.
        w = self.fetch(lambda url: forecast(url, chances=[0] * 24, sunrise="2026-10-27T07:44"),
                       date=datetime.date(2026, 10, 27))
        self.assertEqual(w["sunrise"], "06:44 (6:44am)")

    def test_intensity_bands_and_kinds(self):
        start = utc(2026, 10, 10, 9)
        for peak, intensity in ((0.0, "light"), (0.49, "light"), (0.5, "moderate"), (4.0, "moderate"),
                                (4.1, "heavy")):
            self.assertEqual(periods(start, [60, 60, 0], [0.0, peak, 0.0])[0][3], intensity, peak)
        for codes, kind in (([51, 3, 3], "drizzle"), ([63, 3, 3], "rain"), ([81, 61, 3], "showers"),
                            ([73, 3, 3], "snow"), ([95, 81, 3], "thunderstorms"), ([3, 3, 3], "rain")):
            self.assertEqual(periods(start, [60, 60, 0], codes=codes)[0][2], kind, codes)

    def test_hourly_coverage_required(self):
        def edited(url, edit):
            data = json.loads(forecast(url))
            edit(data["hourly"])
            return json.dumps(data).encode()
        for edit in (lambda h: [h[k].pop(10) for k in h],                     # an hour of the day missing
                     lambda h: h["time"].__setitem__(10, h["time"][9]),       # a duplicate hides a missing hour
                     lambda h: h["precipitation_probability"].__setitem__(10, None),
                     lambda h: h["precipitation"].__setitem__(24, None),       # the day's last hour
                     lambda h: h["precipitation_probability"].__setitem__(5, "10"),
                     lambda h: h["precipitation"].pop(),                       # lengths differ
                     lambda h: [h[k].clear() for k in h]):
            with self.assertRaises(df.FetchError):
                self.fetch(lambda url, edit=edit: edited(url, edit))
        # The previous day's last hour (the date's 00:00 label) is not part of the day.
        self.assertEqual(self.fetch(lambda url: edited(url, lambda h: h["precipitation_probability"].__setitem__(
            0, 90)))["rain_periods"], WEATHER["rain_periods"])
        # Values outside the London day are not needed.
        self.assertEqual(self.fetch(lambda url: edited(url, lambda h: h["precipitation"].__setitem__(30, None)))
                         ["rain_chance"], "64%")

    def test_temperatures_rounded_half_up(self):
        w = self.fetch(lambda url: forecast(url, temperature_2m_max=12.5, temperature_2m_min=-0.4))
        self.assertEqual((w["high"], w["low"]), ("13°C", "0°C"))

    def test_no_rain_hours(self):
        self.assertEqual(self.fetch(lambda url: forecast(url, chances=[10] * 24, amounts=[0.0] * 24))["rain_periods"],
                         [])


    def test_malformed_forecast_is_fetch_error(self):
        for bad in (lambda url: forecast(url, temperature_2m_max=None), lambda url: forecast(url, weather_code=7),
                    lambda url: forecast(url, sunrise="2026-10-11T07:20"),
                    lambda url: forecast(url, chances=[None] * 24),
                    lambda url: json.dumps({"daily": {}}).encode(),
                    lambda url: forecast(url, time="2026-10-11")):
            with self.assertRaises(df.FetchError):
                self.fetch(bad)

    def test_call_e_accepts_free_wording(self):
        client = weather_client(GOOD_E["paragraph"])
        self.assertEqual(df.write_weather(WEATHER, client=client), GOOD_E["paragraph"])
        req = client.requests[0]
        for key in ("tools", "tool_choice", "include"):
            self.assertNotIn(key, req)
        self.assertIn("RAIN PERIODS: \n- light showers from 13:00 (1pm) to 16:00 (4pm)", req["input"])
        self.assertIn("SUNRISE: 07:20 (7:20am)", req["input"])
        # Either form of the start time will do; sunrise and the exact shape are optional.
        self.assertTrue(df.write_weather(WEATHER, client=weather_client(
            "Showers are likely from 13:00, light ones. High 20°C, low 13°C.")))
        self.assertIn("ONE form", req["instructions"])

    def test_call_e_guards(self):
        good = GOOD_E["paragraph"]
        for bad in (good.replace("20°C", "21°C"),                     # high changed
                    good.replace("a high of 20°C and a low of 13°C", "a high of 13°C"),  # low missing
                    good.replace("from 1pm", "from the afternoon"),     # rain start missing
                    good.replace("from 1pm", "from 11pm"),              # "1pm" inside "11pm" does not count
                    good.replace("from 1pm", "from 13:00 (1pm)"),       # a time in both forms
                    good + " Sunrise is at 07:20, that is 7:20am.",     # likewise
                    good + " Otherwise no rain is expected.",           # dry claim on a wet day
                    good + " Expect 5 mm.",                            # invented number
                    good + " Rain is 13% likely.",                     # a supplied number with another unit
                    good + " Showers until 20pm.",                     # likewise
                    # Swapped high and low (both strings present, roles reversed).
                    good.replace("a high of 20°C and a low of 13°C", "a high of 13°C and a low of 20°C"),
                    good.replace("a high of 20°C and a low of 13°C", "temperatures dropping to 20°C, peaking at "
                                                                     "13°C"),
                    good.replace("a high of 20°C and a low of 13°C", "20°C and 13°C"),   # no roles at all
                    good + " Stay dry" * 40,                           # over the length cap
                    good + " See https://evil.example"):
            with self.assertRaises(df.LLMError, msg=bad):
                df.write_weather(WEATHER, client=weather_client(bad))
        dry = dict(WEATHER, rain_periods=[])
        self.assertIn("RAIN PERIODS: none", df.weather_block(dry))
        self.assertTrue(df.write_weather(dry, client=weather_client(
            "A dry, grey day with no rain expected: a high of 20°C and a low of 13°C.")))
        # Whole values only: a low of 3°C is not found inside "13°C".
        cold = dict(dry, high="13°C", low="3°C")
        with self.assertRaises(df.LLMError):
            df.write_weather(cold, client=weather_client("A high of 13°C and a low of 13°C."))
        self.assertTrue(df.write_weather(cold, client=weather_client("A high of 13°C and a low of 3°C.")))
        # A supplied number cannot reappear with another unit, recognised or not.
        with self.assertRaises(df.LLMError):
            df.write_weather(dry, client=weather_client("A high of 20°C and a low of 13°C, with winds of 13 knots."))
        for ok in ("Warmest at 20°C, cooling to an overnight 13°C.",
                   "Up to 20°C this afternoon, down to 13°C tonight. Rain is unlikely, about a 64 per cent chance."):
            self.assertEqual(df.write_weather(dry, client=weather_client(ok)), ok)

    def test_rendered_with_source(self):
        sel, facts = self.gather()
        weather = df.render_sections(sel, facts)["weather"]
        self.assertEqual(weather, f"## Weather in Croydon\n\n{GOOD_E['paragraph']}\n\n"
                                  f"Source: [Open-Meteo]({df.OPEN_METEO_URL})")

    def test_call_e_failure_uses_code_sentence(self):
        client = good_client()
        client.answers["weather"] = [RuntimeError("down")]
        sel, facts = self.gather(client=client)
        weather = df.render_sections(sel, facts)["weather"]
        self.assertIn(df.weather_sentence(WEATHER), weather)
        self.assertEqual(df.weather_sentence(WEATHER),
                         "Today's weather is light rain, with a high of 20°C and a low of 13°C. Expect light "
                         "showers from 1pm to 4pm. Sunrise is at 7:20am and sunset at 6:11pm.")
        self.assertTrue(any("Weather writer (call E) failed twice" in d for d in facts.diagnostics))
        two = dict(WEATHER, rain_periods=[{"start": utc(2026, 10, 10, 5), "end": utc(2026, 10, 10, 7),
                                           "kind": "rain", "intensity": "heavy"}] + WEATHER["rain_periods"])
        self.assertIn("Expect heavy rain from 6am to 8am and light showers from 1pm to 4pm.",
                      df.weather_sentence(two))
        self.assertIn("Rain is unlikely, with no more than a 64% chance in any hour.",
                      df.weather_sentence(dict(WEATHER, rain_periods=[])))

    def test_fetch_failure_is_unavailable_and_others_render(self):
        routes = default_routes()
        routes["api.open-meteo.com"] = TimeoutError()
        client = good_client()
        sel, facts = self.gather(routes, client=client)
        markdown, _, _ = df.build_document(sel, facts, client=client)
        self.assertIn("*Unavailable today: the Croydon weather forecast.", markdown)
        self.assertNotIn(df.OPEN_METEO_URL, markdown)
        self.assertIn("Croydon weather unavailable (Open-Meteo forecast: TimeoutError).", facts.diagnostics)
        self.assertFalse(any(r["text"]["format"]["name"] == "weather" for r in client.requests))
        self.assertIn("No planned disruption listed by TfL.", markdown)
        self.assertIn("## AI Engineering: Context rot", markdown)


# ---------- trains (TfL Windrush + call F Thameslink) ----------

FIRST = "WINDRUSH LINE: Sunday 18 October, from 2200, no service between Surrey Quays and New Cross."


class WindrushTests(Base):
    def status(self, date, *statuses):
        with patch.object(df, "http_get", FakeHTTP({"api.tfl.gov.uk": tfl_status(*statuses)})):
            return df.windrush_status(date)

    def on(self, date, start, end):
        return self.status(date, closure(FIRST, start, end))["notices"] == [FIRST]

    def test_requests_a_range_around_the_date(self):
        http = FakeHTTP({"api.tfl.gov.uk": tfl_status()})
        with patch.object(df, "http_get", http):
            df.windrush_status(datetime.date(2026, 10, 13))
        self.assertIn("/Line/windrush/Status/2026-10-12T00:00:00/to/2026-10-14T23:59:59", http.calls[0])

    def test_bst_day_and_overnight_period(self):
        start, end = "2026-10-18T21:00:00Z", "2026-10-19T00:29:00Z"   # 22:00 to 01:29 London time
        self.assertFalse(self.on(datetime.date(2026, 10, 17), start, end))
        self.assertTrue(self.on(datetime.date(2026, 10, 18), start, end))
        self.assertTrue(self.on(datetime.date(2026, 10, 19), start, end))
        self.assertFalse(self.on(datetime.date(2026, 10, 20), start, end))

    def test_exact_london_midnight_is_a_half_open_boundary(self):
        # BST: London midnight starting 14 October is 23:00 UTC on the 13th.
        self.assertTrue(self.on(datetime.date(2026, 10, 13), "2026-10-13T22:00:00Z", "2026-10-13T23:00:00Z"))
        self.assertFalse(self.on(datetime.date(2026, 10, 14), "2026-10-13T22:00:00Z", "2026-10-13T23:00:00Z"))
        self.assertTrue(self.on(datetime.date(2026, 10, 14), "2026-10-13T23:00:00Z", "2026-10-14T01:00:00Z"))
        # GMT: London midnight is UTC midnight.
        self.assertTrue(self.on(datetime.date(2026, 11, 9), "2026-11-09T23:30:00Z", "2026-11-10T00:00:00Z"))
        self.assertFalse(self.on(datetime.date(2026, 11, 10), "2026-11-09T23:30:00Z", "2026-11-10T00:00:00Z"))

    def test_dst_transition_days(self):
        # 29 March 2026 (23 hours): 00:00 UTC to 23:00 UTC.
        spring = datetime.date(2026, 3, 29)
        self.assertTrue(self.on(spring, "2026-03-28T23:30:00Z", "2026-03-29T00:30:00Z"))
        self.assertTrue(self.on(spring, "2026-03-29T22:30:00Z", "2026-03-29T23:00:00Z"))
        self.assertFalse(self.on(spring, "2026-03-29T23:00:00Z", "2026-03-30T01:00:00Z"))
        # 25 October 2026 (25 hours): 23:00 UTC on the 24th to 00:00 UTC on the 26th.
        autumn = datetime.date(2026, 10, 25)
        self.assertFalse(self.on(autumn, "2026-10-24T22:30:00Z", "2026-10-24T23:00:00Z"))
        self.assertTrue(self.on(autumn, "2026-10-24T23:00:00Z", "2026-10-24T23:30:00Z"))
        self.assertTrue(self.on(autumn, "2026-10-25T23:30:00Z", "2026-10-26T00:00:00Z"))
        self.assertFalse(self.on(autumn, "2026-10-26T00:00:00Z", "2026-10-26T01:00:00Z"))

    def test_naive_timestamps_are_utc(self):
        self.assertFalse(self.on(datetime.date(2026, 10, 14), "2026-10-13T22:00:00", "2026-10-13T23:00:00"))
        self.assertTrue(self.on(datetime.date(2026, 10, 13), "2026-10-13T22:00:00", "2026-10-13T23:00:00"))

    def test_good_service_dedupe_and_no_period_statuses(self):
        day = datetime.date(2026, 10, 18)
        result = self.status(day, {"statusSeverityDescription": "Good Service", "validityPeriods": []},
                             closure(FIRST, "2026-10-18T21:00:00Z", "2026-10-19T00:29:00Z"),
                             closure("  WINDRUSH LINE: Sunday 18 October,\n from 2200, no service between "
                                     "Surrey Quays and New Cross. ", "2026-10-18T21:00:00Z", "2026-10-19T00:29:00Z"),
                             {"statusSeverityDescription": "Minor Delays", "reason": "Signal failure at Dalston.",
                              "validityPeriods": []})
        self.assertEqual(result, {"notices": [FIRST, "Signal failure at Dalston."], "dropped": 0})
        self.assertEqual(self.status(day), {"notices": [], "dropped": 0})

    def test_line_break_notice_kept_verbatim_in_one_bullet(self):
        # Real TfL shape (26-29 October 2026): a "\r\n" between sentences.
        multi = ("WINDRUSH LINE: Monday 26 October, after 2330, no service between New Cross Gate and West "
                 "Croydon. Alternatively, use local buses. \r\nThe 2232 train will divert to Crystal Palace.")
        period = ("2026-10-26T23:30:00Z", "2026-10-27T01:00:00Z")
        result = self.status(datetime.date(2026, 10, 27), closure(multi, *period), closure(FIRST, *period))
        self.assertEqual(result, {"notices": [multi, FIRST], "dropped": 0})
        sel = df.select(datetime.date(2026, 10, 27), self.data)
        facts = df.Facts(windrush=result)
        soup, _ = df.converter_view(df.render_sections(sel, facts)["trains"])
        items = [li.get_text() for li in soup.find_all("li")]
        self.assertEqual(len(items), 2)
        self.assertIn("Alternatively, use local buses.", items[0])
        self.assertIn("The 2232 train will divert to Crystal Palace.", items[0])

    def test_line_breaks_that_break_the_bullet_are_dropped(self):
        period = ("2026-10-18T21:00:00Z", "2026-10-19T00:29:00Z")
        bad = ["Closed.\r\n\r\nUse buses.", "Closed.\r\n# Heading", "Closed.\r\n- Another item",
               "Closed.\r\n> Quoted"]
        result = self.status(datetime.date(2026, 10, 18), *(closure(b, *period) for b in bad))
        self.assertEqual(result, {"notices": [], "dropped": len(bad)})

    def test_bad_notices_are_counted_never_rewritten(self):
        day = datetime.date(2026, 10, 18)
        period = ("2026-10-18T21:00:00Z", "2026-10-19T00:29:00Z")
        bad = [closure("Details at https://tfl.gov.uk/x", *period), closure("See [here](/x).", *period),
               closure("Fares cost $x^2$ today.", *period), closure("<b>Closed</b>", *period),
               {"statusSeverityDescription": "Part Closure", "validityPeriods": []},          # no reason
               closure(FIRST, "not a date", period[1]),                                         # malformed period
               {"statusSeverityDescription": "Part Closure", "reason": FIRST,
                "validityPeriods": [{"fromDate": period[0]}]}]                                  # missing toDate
        result = self.status(day, closure(FIRST, *period), *bad)
        self.assertEqual(result, {"notices": [FIRST], "dropped": len(bad)})

    def test_shape_error_is_fetch_error(self):
        with patch.object(df, "http_get", FakeHTTP({"api.tfl.gov.uk": b'{"message": "bad request"}'})):
            with self.assertRaises(df.FetchError):
                df.windrush_status(DATE)

    def windrush_text(self, routes):
        sel, facts = self.gather(routes)
        return facts, df.render_sections(sel, facts)["trains"].split("### Thameslink", 1)[0]

    def test_rendering(self):
        routes = default_routes()
        routes["api.tfl.gov.uk"] = tfl_status(closure(FIRST, "2026-10-10T10:00:00Z", "2026-10-10T12:00:00Z"))
        _, text = self.windrush_text(routes)
        self.assertIn(f"\n- {FIRST}\n", text)
        self.assertNotIn("No planned disruption", text)
        self.assertIn(f"Source: [TfL line status]({df.TFL_STATUS_URL})", text)

    def test_dropped_notice_is_never_no_disruption(self):
        routes = default_routes()
        routes["api.tfl.gov.uk"] = tfl_status(closure("See https://x.example", "2026-10-10T10:00:00Z",
                                                      "2026-10-10T12:00:00Z"))
        facts, text = self.windrush_text(routes)
        self.assertIn("*Some TfL notices could not be shown; check the TfL status page.*", text)
        self.assertNotIn("No planned disruption", text)
        self.assertNotIn("x.example", text)
        self.assertTrue(any("TfL Windrush notice(s) left out" in d for d in facts.diagnostics))

    def test_any_failure_degrades_only_windrush(self):
        for routes_patch, reason in ((TimeoutError(), "TfL Windrush status: TimeoutError"),
                                     (b"[]", "TfL Windrush status: unexpected response shape")):
            routes = default_routes()
            routes["api.tfl.gov.uk"] = routes_patch
            client = good_client()
            sel, facts = self.gather(routes, client=client)
            markdown, _, _ = df.build_document(sel, facts, client=client)
            self.assertIn("*Unavailable today: the Windrush line status.", markdown)
            self.assertIn(f"Windrush line status unavailable ({reason}).", facts.diagnostics)
            self.assertIn("An AI web search found no notice", markdown)
            self.assertIn(GOOD_E["paragraph"], markdown)
        with patch.object(df, "windrush_status", side_effect=KeyError("x")):   # not a FetchError
            _, facts = self.gather()
        self.assertIsNone(facts.windrush)
        self.assertIn("Windrush line status unavailable (KeyError).", facts.diagnostics)


class ThameslinkTests(Base):
    def check(self, payload, tool_calls=None):
        client = FakeClient({"thameslink": [response(payload, tool_calls=[search_call(RAIL_URL)]
                                                     if tool_calls is None else tool_calls)]})
        return df.thameslink_disruptions(DATE, client=client), client

    def test_request_shape(self):
        _, client = self.check(GOOD_F)
        req = client.requests[0]
        self.assertEqual(req["tools"], [{"type": "web_search"}])
        self.assertEqual(req["tool_choice"], "required")
        self.assertEqual(req["max_tool_calls"], 2)
        self.assertEqual(req["include"], ["web_search_call.action.sources"])
        self.assertEqual(req["timeout"], 120)
        self.assertIn("Saturday 10 October 2026 (2026-10-10)", req["input"])

    def test_none_found_discards_summary_and_sources(self):
        result, _ = self.check(dict(GOOD_F, summary="Ignored.", sources=[{"url": RAIL_URL, "supports": "x"}]))
        self.assertEqual(result, {"status": "none_found", "summary": "", "sources": [],
                                  "search_actions": {"search": 1}})

    def test_disruption_needs_a_validated_source(self):
        result, _ = self.check(DISRUPTION_F)
        self.assertEqual(result["sources"], [{"url": RAIL_URL, "supports": "Lists the closure on 10 October."}])
        for payload, tool_calls in ((dict(DISRUPTION_F, sources=[{"url": "https://made-up.example/x",
                                                                  "supports": "x"}]), None),
                                    (dict(DISRUPTION_F, sources=[]), None),
                                    (dict(DISRUPTION_F, summary=""), None),
                                    (dict(DISRUPTION_F, summary="See https://evil.example"), None),
                                    (dict(DISRUPTION_F, summary="word " * 71), None),
                                    (DISRUPTION_F, [])):                                   # never searched
            with self.assertRaises(df.LLMError), contextlib.redirect_stderr(io.StringIO()):
                self.check(payload, tool_calls)

    def trains(self, client):
        sel, facts = self.gather(client=client)
        markdown, _, _ = df.build_document(sel, facts, client=client)
        return sel, facts, markdown, markdown.split("### Thameslink", 1)[1].split("\n## ", 1)[0]

    def test_none_found_rendering(self):
        _, _, _, text = self.trains(good_client())
        self.assertIn("*An AI web search found no notice of planned Thameslink disruption for Saturday 10 "
                      "October. This is not a guarantee; check before travelling.*", text)
        self.assertNotIn("No planned disruption", text)
        self.assertIn(f"Check: [National Rail status and disruptions]({df.NATIONAL_RAIL_URL})", text)

    def test_disruption_rendering_and_links(self):
        client = good_client()
        client.answers["thameslink"] = [response(DISRUPTION_F, tool_calls=[search_call(RAIL_URL)])]
        sel, facts, markdown, text = self.trains(client)
        self.assertIn(DISRUPTION_F["summary"] + "\n\n*From an AI web search; may be incomplete or wrong. "
                      "Check before travelling.*", text)
        self.assertIn(f"- [thameslinkrailway.com/travel-information/engineering-works]({RAIL_URL})", text)
        links = set(df.rendered_links(df.converter_view(markdown)[0]))
        self.assertEqual(links, df.allowed_links(sel, facts))
        for url in (RAIL_URL, df.NATIONAL_RAIL_URL, df.TFL_STATUS_URL, df.OPEN_METEO_URL):
            self.assertIn(url, links)

    def test_failure_degrades_only_thameslink(self):
        client = good_client()
        client.answers["thameslink"] = [RuntimeError("down")]
        _, facts, markdown, text = self.trains(client)
        self.assertIn("*Unavailable today: the Thameslink check.", text)
        self.assertIn(f"Check: [National Rail status and disruptions]({df.NATIONAL_RAIL_URL})", text)
        self.assertIn("No planned disruption listed by TfL.", markdown)
        self.assertTrue(any("Thameslink check (call F) failed twice" in d for d in facts.diagnostics))

    def test_search_actions_recorded_in_state(self):
        state_path = os.path.join(self.tmp, "state.json")
        with patch.object(df, "http_get", FakeHTTP(default_routes())), \
                patch("daily_facts.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            df.main(["--date", "2026-10-10", "--state", state_path], client=good_client())
        entry = df.load_state(state_path)["sent"]["2026-10-10"]
        self.assertEqual((entry["thameslink_status"], entry["thameslink_search_actions"]),
                         ("none_found", {"search": 1}))

    def test_reviewer_may_dispute_new_sections(self):
        client = with_disagreements(good_client(), ("weather", "Disputed forecast."))
        sel, facts = self.gather(client=client)
        markdown, _, disagreements = df.build_document(sel, facts, client=client)
        weather = markdown.split("## Weather in Croydon", 1)[1].split("\n## ", 1)[0]
        self.assertIn("AI reviewer note:** Disputed forecast.", weather)
        self.assertIn("Weather in Croydon.", markdown.split("\n\n## ", 1)[0])
        self.assertIn("trains", client.requests[-2]["text"]["format"]["schema"]["properties"]
                      ["source_disagreements"]["items"]["properties"]["section"]["enum"])


# ---------- orchestration ----------

class MainTests(Base):
    def setUp(self):
        super().setUp()
        self.state_path = os.path.join(self.tmp, "state.json")
        p = patch.object(df, "http_get", FakeHTTP(default_routes()))
        p.start()
        self.addCleanup(p.stop)

    def run_main(self, *args, rc=0, client=None):
        with patch("daily_facts.subprocess.run", return_value=SimpleNamespace(returncode=rc)) as run:
            code = df.main(["--date", "2026-10-10", "--state", self.state_path, *args],
                           client=client or good_client())
        return code, run

    def test_send_records_state(self):
        code, run = self.run_main()
        self.assertEqual(code, df.EXIT_SENT)
        self.assertNotIn("--no-send-to-kindle", run.call_args.args[0])
        entry = df.load_state(self.state_path)["sent"]["2026-10-10"]
        self.assertEqual(entry["ai_topic"], "Context rot")
        self.assertEqual(entry["ai_sources"], [{"url": TOOL_URL,
                                                "model_attribution": "Recall degrades with length."}])
        self.assertIn("warnings", entry)
        self.assertEqual(entry["reviewer_disagreements"], [])
        self.assertEqual(entry["ai_search_actions"], {"search": 1})

    def test_converter_failures_do_not_record(self):
        for rc in (1, 2, 3):
            code, _ = self.run_main(rc=rc)
            self.assertEqual(code, rc)
            self.assertEqual(df.load_state(self.state_path)["sent"], {})

    def test_already_sent_makes_no_calls(self):
        df.save_state(self.state_path, {"sent": {"2026-10-10": {"ai_topic": "x"}}})
        client = good_client()
        with patch.object(df, "http_get", side_effect=AssertionError("network")):
            code, run = self.run_main(client=client)
        self.assertEqual(code, df.EXIT_ALREADY_SENT)
        run.assert_not_called()
        self.assertEqual(client.requests, [])

    def test_degraded_run_still_sends_and_records_warnings(self):
        with patch.object(df, "http_get", FakeHTTP(world_bank_down_routes())):
            code, run = self.run_main()
        self.assertEqual(code, df.EXIT_SENT)
        run.assert_called_once()
        self.assertNotIn("--no-send-to-kindle", run.call_args.args[0])
        warnings = df.load_state(self.state_path)["sent"]["2026-10-10"]["warnings"]
        self.assertIn("Population unavailable (World Bank SP.POP.TOTL: TimeoutError).", warnings)
        if df.select(DATE, df.load_data()).country_fact["source"] == "worldbank":
            self.assertTrue(any(w.startswith("UK vs Jamaica: World Bank API unavailable") for w in warnings))

    def test_warning_without_details_is_recorded_and_annotated(self):
        client = good_client()
        client.answers["run_check"] = [response(dict(GOOD_C, status="warning"), tool_calls=[])]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code, _ = self.run_main(client=client)
        self.assertEqual(code, df.EXIT_SENT)
        warnings = df.load_state(self.state_path)["sent"]["2026-10-10"]["warnings"]
        self.assertEqual(warnings[0], "Run check flagged a problem without details.")
        self.assertIn(f"::warning::Daily Facts 2026-10-10 was sent with {len(warnings)} warning(s)",
                      out.getvalue())

    def test_off_list_link_aborts_before_writing_or_sending(self):
        df.save_state(self.state_path, {"sent": {"2026-10-09": {"ai_topic": "Reranking"}}})
        with open(self.state_path, "rb") as f:
            before = f.read()
        evil = lambda sel, facts: "## Research Fact: x\n\n[click](https://evil.example/x)"
        err = io.StringIO()
        with patch.object(df, "_research_section", evil), contextlib.redirect_stderr(err):
            code, run = self.run_main()
        self.assertEqual(code, 1)
        run.assert_not_called()
        self.assertIn("https://evil.example/x", err.getvalue())
        self.assertFalse(os.path.exists(os.path.join(df.INPUT_DIR, "daily_facts_2026-10-10.md")))
        with open(self.state_path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_preview_leaves_existing_state_byte_identical(self):
        df.save_state(self.state_path, {"sent": {"2026-10-09": {"ai_topic": "Reranking"}}})
        with open(self.state_path, "rb") as f:
            before = f.read()
        code, _ = self.run_main("--preview")
        self.assertEqual(code, 0)
        with open(self.state_path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_preview_never_sends_or_records(self):
        code, run = self.run_main("--preview")
        self.assertEqual(code, 0)
        self.assertIn("--no-send-to-kindle", run.call_args.args[0])
        self.assertFalse(os.path.exists(self.state_path))

    def test_dry_run_writes_markdown_only(self):
        code, run = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertTrue(os.path.exists(os.path.join(df.INPUT_DIR, "daily_facts_2026-10-10.md")))

    def test_recent_topics_reach_call_b(self):
        df.save_state(self.state_path, {"sent": {"2026-10-09": {"ai_topic": "Reranking"}}})
        client = good_client()
        with patch("daily_facts.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            df.main(["--date", "2026-10-10", "--state", self.state_path], client=client)
        ai_req = next(r for r in client.requests if r["text"]["format"]["name"] == "ai_lesson")
        self.assertIn("- Reranking", ai_req["input"])


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        workflow = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                ".github", "workflows", "daily_facts.yml")
        with open(workflow, encoding="utf-8") as f:
            self.text = f.read()

    def test_schedule_has_backup_slots_within_one_london_day(self):
        # The single slot was missed on 2026-10-12: GitHub schedules are best-effort.
        slot = re.search(r'^\s+- cron: "50 ([\d,]+) \* \* \*"\n\s+timezone: "Europe/London"$',
                         self.text, re.M)
        self.assertIsNotNone(slot)
        hours = {int(h) for h in slot.group(1).split(",")}
        self.assertGreaterEqual(len(hours), 4)
        self.assertLessEqual(max(hours), 18)

    def test_workflow_success_code_matches_already_sent_constant(self):
        self.assertIn(f'"$rc" -eq {df.EXIT_ALREADY_SENT} ]', self.text)


if __name__ == "__main__":
    unittest.main()
