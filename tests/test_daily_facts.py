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


def default_routes(country_iso3="JAM"):
    def population(url):
        rows = [wb_row(country_iso3, 2024, 2_800_000), wb_row("GBR", 2024, 69_000_000)]
        if country_iso3 != "GBR":
            rows.append(wb_row("GBR", 2025, 69_500_000))  # UK has a newer year the country lacks
        return wb_page(rows)

    def latest(url):
        iso3 = re.search(r"country/([A-Z]{3})/", url).group(1)
        code = re.search(r"indicator/([^?]+)", url).group(1)
        return wb_page([wb_row(iso3, 2025, 4.2, code)])

    return {
        "SP.POP.TOTL?format=json&per_page=200&date=": population,
        "mrnev=1": latest,
        "flagcdn.com": PNG,
        "w/api.php": lead(),
        "wikipedia.org": lambda url: wiki(url.rsplit("/", 1)[1]),
    }


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
        return item


def good_client():
    return FakeClient({"grounded_prose": [response(GOOD_A, tool_calls=[])],
                       "ai_lesson": [response(GOOD_B)],
                       "run_check": [response(GOOD_C, tool_calls=[])]})


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

    def jamaica_selection(self):
        sel = df.select(DATE, self.data)
        sel.country = next(c for c in self.data["countries"] if c["cca3"] == "JAM")
        return sel

    def gather(self, routes=None, client=None, sel=None):
        sel = sel or self.jamaica_selection()
        with patch.object(df, "http_get", FakeHTTP(routes or default_routes())):
            return sel, df.gather(sel, self.data, [], client=client or good_client())


# ---------- selection ----------

class SelectionTests(Base):
    def test_same_date_same_picks(self):
        a, b = df.select(DATE, self.data), df.select(DATE, self.data)
        self.assertEqual((a.country, a.research, a.ai_theme, a.uk_indicators, a.jm_indicators),
                         (b.country, b.research, b.ai_theme, b.uk_indicators, b.jm_indicators))

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

    def test_indicator_fallbacks_are_distinct_next_slots(self):
        sel = df.select(DATE, self.data)
        codes = [i["code"] for i in sel.uk_indicators]
        self.assertEqual(len(codes), df.INDICATOR_TRIES)
        self.assertEqual(len(set(codes)), len(codes))

    def test_catalogues_are_well_formed(self):
        titles = [t["wikipedia_title"] for t in self.data["research"]]
        self.assertEqual(len(titles), len(set(titles)))
        for t in self.data["research"]:
            self.assertTrue(t["category"] and t["display_title"] and t["wikipedia_title"])
        for theme in self.data["ai_themes"]:
            self.assertTrue(theme["name"] and theme["description"] and theme["examples"])
        self.assertEqual(len(self.data["countries"]), 193)
        for ind in self.data["indicators"]:
            self.assertIn(ind["format"], ("usd", "pct", "count"))


# ---------- World Bank ----------

class WorldBankTests(Base):
    def test_population_uses_newest_common_year(self):
        sel, facts = self.gather()
        self.assertEqual(facts.population, {"value": 2_800_000, "uk_value": 69_000_000, "year": 2024})
        self.assertTrue(any("uses 2024" in d for d in facts.diagnostics))

    def test_latest_value_and_year(self):
        with patch.object(df, "http_get", FakeHTTP(default_routes())):
            self.assertEqual(df.world_bank_latest("GBR", "FP.CPI.TOTL.ZG"), (4.2, 2025))

    def test_empty_value_falls_back_to_next_indicator(self):
        sel = self.jamaica_selection()
        first, second = sel.jm_indicators[0]["code"], sel.jm_indicators[1]["code"]
        routes = default_routes()
        base = routes["mrnev=1"]
        routes["mrnev=1"] = lambda url: (wb_page([wb_row("JAM", 2025, None, first)])
                                         if f"JAM/indicator/{first}" in url else base(url))
        _, facts = self.gather(routes, sel=sel)
        self.assertEqual(facts.jm_econ["indicator"]["code"], second)
        self.assertTrue(any("tried the next indicator" in d for d in facts.diagnostics))

    def test_stale_observation_is_flagged(self):
        routes = default_routes()
        routes["mrnev=1"] = lambda url: wb_page([wb_row(re.search(r"country/([A-Z]{3})/", url).group(1),
                                                        2020, 97.9)])
        _, facts = self.gather(routes)
        self.assertEqual(facts.jm_econ["year"], 2020)
        self.assertTrue(any("from 2020" in d for d in facts.diagnostics))

    def test_malformed_response_raises_fetch_error(self):
        with patch.object(df, "http_get", FakeHTTP({"worldbank": b'[{"message": "bad"}]'})):
            with self.assertRaises(df.FetchError):
                df.world_bank_latest("GBR", "X")

    def test_malformed_rows_become_fetch_errors_and_degrade(self):
        for bad in ({"value": "not-a-number"}, {"date": "20x5"}, {"value": float("nan")}):
            routes = default_routes()
            routes["mrnev=1"] = wb_page([dict(wb_row("JAM", 2025, 1.0, "X"), **bad)])
            with patch.object(df, "http_get", FakeHTTP(routes)):
                with self.assertRaises(df.FetchError, msg=bad):
                    df.world_bank_latest("JAM", "X")
            routes["SP.POP.TOTL?format=json&per_page=200&date="] = wb_page(
                [dict(wb_row("JAM", 2024, 1.0), **bad), wb_row("GBR", 2024, 69_000_000)])
            sel, facts = self.gather(routes)
            self.assertIsNone(facts.population)
            self.assertIsNone(facts.uk_econ)
            self.assertIsNone(facts.jm_econ)
            self.assertTrue(any(d.startswith("Population unavailable") for d in facts.diagnostics))
            self.assertIn("Unavailable today: uk economy fact", df.render_body(sel, facts))

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
            self.assertEqual(df.world_bank_latest("GBR", "X"), (1.0, 2025))
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

    def test_full_document(self):
        sel, facts = self.gather()
        markdown, warnings, _ = df.build_document(sel, facts, client=good_client())
        self.assertTrue(markdown.startswith("# Daily Facts — Saturday 10 October 2026\n"))
        for heading in ("## Flag of the Day: Jamaica", "## Research Fact: A striking fact",
                        "## AI Engineering: Context rot", "## UK Economy", "## Jamaica Economy"):
            self.assertIn(heading, markdown)
        self.assertIn("![Flag of Jamaica](daily_facts_2026-10-10_flag.png)", markdown)
        self.assertIn("| Capital | Kingston |", markdown)
        self.assertIn("| Currency | Jamaican dollar |", markdown)
        self.assertIn("| Population | 2.8 million (2024) |", markdown)
        self.assertIn("| vs UK | 4.1% of the UK's (2024) |", markdown)
        # Every table row has exactly two cells.
        for line in markdown.splitlines():
            if line.startswith("|"):
                self.assertEqual(line.count("|"), 3, line)
        # Every economy fact carries its year and a World Bank source.
        for section in ("## UK Economy", "## Jamaica Economy"):
            part = markdown.split(section, 1)[1]
            self.assertRegex(part, r"in 2025\.")
            self.assertIn("https://data.worldbank.org/indicator/", part)
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

    def test_dollar_economy_values_pass_invariant(self):
        sel, facts = self.gather()
        for econ, value in ((facts.uk_econ, 3.1e12), (facts.jm_econ, 22_704_903_217)):
            econ["indicator"] = dict(econ["indicator"], format="usd", label="GDP (current US$)")
            econ["value"] = value
        markdown, _, _ = df.build_document(sel, facts, client=good_client())
        self.assertIn("$3.1 trillion", markdown)
        self.assertIn("$22.7 billion", markdown)

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
        self.assertEqual(lines[1], "")
        self.assertTrue(lines[2].startswith("> **Run check — warning:** UK value looks odd."))
        self.assertIn("UK value looks odd.", warnings)

    def test_warning_status_without_notes_still_shows_box(self):
        sel, facts = self.gather()
        for diagnostics, headline in ((["Something failed."], "see diagnostics below."),
                                      ([], "the reviewer flagged a problem but gave no details.")):
            facts.diagnostics[:] = diagnostics
            client = good_client()
            client.answers["run_check"] = [response(dict(GOOD_C, status="warning"), tool_calls=[])]
            markdown, _, _ = df.build_document(sel, facts, client=client)
            self.assertEqual(markdown.splitlines()[2], f"> **Run check — warning:** {headline}")


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
            flag = body.split("\n## ", 1)[0]
            self.assertIn("| Population | 2.8 million (2024) |", flag)
            self.assertIn("*Unavailable today: the country description.", flag)
            self.assertNotIn("[Wikipedia]", flag)
            self.assertIn("[World Bank population]", flag)
            self.assertTrue(any("Prose writer (call A) failed twice" in d for d in facts.diagnostics))

    def flag_note_client(self):
        return with_disagreements(good_client(), ("flag", "Disputed."))

    def test_world_bank_down(self):
        routes = default_routes()
        routes["worldbank"] = TimeoutError()
        for key in [k for k in routes if "mrnev" in k or "SP.POP" in k]:
            del routes[key]
        sel, facts = self.gather(routes)
        markdown, warnings, _ = df.build_document(sel, facts, client=good_client())
        self.assertIn("| Population | unavailable today |", markdown)
        self.assertIn("Unavailable today: uk economy fact", markdown)
        self.assertIn("Unavailable today: jamaica economy fact", markdown)
        self.assertIn("> - Population unavailable (World Bank SP.POP.TOTL: TimeoutError).", markdown)
        # No fabricated numbers anywhere in the economy sections.
        self.assertNotRegex(markdown.split("## UK Economy", 1)[1], r"\d+\.\d%")

    def test_openai_down_keeps_code_facts_and_shows_raw_diagnostics(self):
        down = FakeClient({name: [RuntimeError("down")] for name in ("grounded_prose", "ai_lesson", "run_check")})
        sel, facts = self.gather(client=down)
        markdown, warnings, _ = df.build_document(sel, facts, client=down)
        self.assertIn("Run check unavailable; raw diagnostics below.", markdown)
        self.assertIn("Prose writer (call A) failed twice (RuntimeError).", markdown)
        self.assertIn("Unavailable today: the research fact", markdown)
        self.assertIn("Unavailable today: the AI Engineering topic", markdown)
        self.assertIn("| Capital | Kingston |", markdown)
        self.assertIn("**", markdown.split("## UK Economy", 1)[1])
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
            sel, facts, client=self.review_client(("research", HAMMING), ("uk_economy", "Looks low.")))
        lines = markdown.splitlines()
        self.assertEqual(lines[2], "> **Run check — warning:** AI reviewer disagrees with a sourced claim in: "
                                   "Research Fact, UK Economy.")
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
                                    ("ai", "b."), ("uk_economy", "c."))
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
        self.assertEqual(markdown.splitlines()[2], "> **Run check — warning:** UK value looks odd.")
        self.assertEqual((warnings, disagreements), (["UK value looks odd."], []))

    def test_empty_warning_without_disagreements_keeps_generic_warning(self):
        client = good_client()
        client.answers["run_check"] = [response(dict(GOOD_C, status="warning"), tool_calls=[])]
        sel, facts = self.gather()
        facts.diagnostics.clear()
        markdown, warnings, _ = df.build_document(sel, facts, client=client)
        self.assertEqual(markdown.splitlines()[2],
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
        routes = default_routes()
        routes["worldbank"] = TimeoutError()
        for key in [k for k in routes if "mrnev" in k or "SP.POP" in k]:
            del routes[key]
        with patch.object(df, "http_get", FakeHTTP(routes)):
            code, run = self.run_main()
        self.assertEqual(code, df.EXIT_SENT)
        run.assert_called_once()
        self.assertNotIn("--no-send-to-kindle", run.call_args.args[0])
        warnings = df.load_state(self.state_path)["sent"]["2026-10-10"]["warnings"]
        self.assertIn("Population unavailable (World Bank SP.POP.TOTL: TimeoutError).", warnings)
        self.assertTrue(any(w.startswith("UK economy fact unavailable") for w in warnings))

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


if __name__ == "__main__":
    unittest.main()
