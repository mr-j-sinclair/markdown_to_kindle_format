#!/usr/bin/env python3
"""Build and deliver a short "Daily Facts" morning document to Kindle.

Runs unattended (see .github/workflows/daily_facts.yml): picks today's
country, research topic, AI Engineering theme and UK/Jamaica economic
indicators from date-seeded permutations over the committed lists in
daily_facts_data/, fetches the facts from public keyless APIs (World Bank,
Wikipedia, flagcdn), asks an OpenAI model to write the narrative prose,
renders inputs/daily_facts_YYYY-MM-DD.md and hands it to the existing
md_to_kindle.py CLI, which converts to EPUB and emails it via
kindle_delivery.py.

Grounding contract: table cells, economy values, years and every URL come
from code/APIs. The model only writes prose -- call A restates supplied
Wikipedia extracts, call B researches today's AI topic with web search and
selects its sources, which are kept only when they match a URL the search
tool itself returned and are rendered from that tool metadata -- and
call C reviews the rendered document plus the run's diagnostics and writes a
short warning box when something looks wrong. Where call C disagrees with a
sourced claim, the sourced prose stays as written and the reviewer's view is
shown as a labelled note under that section -- but only when a follow-up
"source check" call (made only on days call C disputes something) cites a
page its own web search returned (validated like call B's sources).

A failed source or model call degrades only its own section ("Unavailable
today: ...") and is listed in the warning box; nothing is fabricated. The
date is recorded in the state file only after md_to_kindle.py exits 0, so
delivery is at-least-once, exactly like ntiy_feed.py.

Exit codes: 0 = sent (or preview/dry-run done), 10 = this date was already
sent, otherwise failure (md_to_kindle.py's 1/2/3 are passed through).
"""

import argparse
import datetime
import functools
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "daily_facts_data")
INPUT_DIR = os.path.join(SCRIPT_DIR, "inputs")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "outputs")
CONVERTER = os.path.join(SCRIPT_DIR, "md_to_kindle.py")
DEFAULT_STATE = os.path.join(SCRIPT_DIR, "state", "daily_facts_state.json")

MODEL = "gpt-6-luna"
LONDON = ZoneInfo("Europe/London")
EPOCH = datetime.date(2026, 1, 1)
USER_AGENT = "markdown_to_kindle_format/daily_facts (https://github.com/mr-j-sinclair/markdown_to_kindle_format)"
HTTP_TIMEOUT = 20
RETRY_DELAYS = (2, 5)          # seconds between the 3 attempts of a read-only GET
POPULATION_YEARS = 6           # look back this far for a population year both countries have
BOUNDARY_GAP = 3               # no pick repeats within this many days across a rotation cycle boundary
INDICATOR_TRIES = 3            # indicators tried (next permutation slot) before an economy section degrades
STALE_YEARS = 3                # an observation this many years old is flagged in the run check
RECENT_AI_TOPICS = 60
MAX_AI_SOURCES = 3
MAX_DISAGREEMENTS = 3
# Call C's section keys -> the human section names used in the run-check box.
SECTION_NAMES = {"flag": "Flag of the Day", "research": "Research Fact", "ai": "AI Engineering",
                 "uk_economy": "UK Economy", "jamaica_economy": "Jamaica Economy"}

EXIT_SENT = 0
EXIT_ALREADY_SENT = 10
EXIT_UNSAFE_DOCUMENT = 1

UK = "GBR"
JAMAICA = "JAM"
WORLD_BANK_API = "https://api.worldbank.org/v2/country/{countries}/indicator/{code}?format=json&per_page=200&{query}"
WIKIPEDIA_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKIPEDIA_INTRO = ("https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro=1"
                   "&explaintext=1&redirects=1&format=json&formatversion=2&titles={title}")
MAX_SOURCE_WORDS = 600         # cap on the Wikipedia lead text sent to the model
FLAG_URL = "https://flagcdn.com/w640/{cca2}.png"

_sleep = time.sleep  # patched out in tests


class FetchError(Exception):
    """A public-API fetch failed after retries, or returned something unusable."""


class LLMError(Exception):
    """A model response was unusable (schema, length, links, missing search)."""


class UnsafeDocumentError(Exception):
    """The assembled document links somewhere code did not put a link."""


# ---------- data + deterministic selection ----------

def load_data(data_dir: str = DATA_DIR) -> dict:
    def read(name):
        with open(os.path.join(data_dir, name), encoding="utf-8") as f:
            return json.load(f)
    themes = read("ai_themes.json")
    return {
        "countries": read("countries.json"),
        "research": read("research_topics.json")["topics"],
        "ai_summary": themes["summary"],
        "ai_themes": themes["themes"],
        "indicators": read("economic_indicators.json")["indicators"],
    }


def _shuffled(n: int, category: str, cycle: int) -> list:
    order = list(range(n))
    seed = int.from_bytes(hashlib.sha256(f"{category}:{cycle}".encode()).digest(), "big")
    random.Random(seed).shuffle(order)
    return order


def cycle_order(n: int, category: str, cycle: int) -> list:
    """This cycle's shuffled order of range(n). The first BOUNDARY_GAP slots
    never hold an item from the previous cycle's last BOUNDARY_GAP slots, so
    nothing repeats within BOUNDARY_GAP days across a cycle boundary. Only
    the first 2*gap slots are reordered, so (with n >= 3*gap) every cycle's
    tail is its raw shuffle's tail and no recursion into older cycles is needed."""
    order = _shuffled(n, category, cycle)
    gap = min(BOUNDARY_GAP, n // 3)
    if cycle == 0 or gap == 0:
        return order
    recent = set(_shuffled(n, category, cycle - 1)[-gap:])
    head = order[:2 * gap]
    head = [i for i in head if i not in recent] + [i for i in head if i in recent]
    return head + order[2 * gap:]


def seeded_index(n: int, category: str, day_number: int, offset: int = 0) -> int:
    """Index for day_number in a per-cycle shuffled order of range(n): every
    item is used once before any repeats, and each category has its own
    seed. offset steps to later slots of the same order (indicator fallback)."""
    cycle, pos = divmod(day_number, n)
    return cycle_order(n, category, cycle)[(pos + offset) % n]


@dataclass
class Selection:
    date: datetime.date
    country: dict
    research: dict
    ai_theme: dict
    uk_indicators: list     # candidates in fallback order
    jm_indicators: list


def select(date: datetime.date, data: dict) -> Selection:
    day = (date - EPOCH).days

    def pick(items, category):
        return items[seeded_index(len(items), category, day)]

    def indicators(category):
        items = data["indicators"]
        return [items[seeded_index(len(items), category, day, k)] for k in range(INDICATOR_TRIES)]

    return Selection(
        date=date,
        country=pick(data["countries"], "country"),
        research=pick(data["research"], "research"),
        ai_theme=pick(data["ai_themes"], "ai-theme"),
        uk_indicators=indicators("uk-economy"),
        jm_indicators=indicators("jm-economy"),
    )


# ---------- HTTP (read-only GETs, bounded retries) ----------

def _describe(e: Exception) -> str:
    """Safe one-line error description: class + HTTP status, never the raw
    message (which could echo request details)."""
    status = getattr(e, "status_code", None) or getattr(e, "code", None)
    return f"{type(e).__name__}{f' {status}' if isinstance(status, int) else ''}"


def _with_retries(fn, what: str):
    last = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return fn()
        except Exception as e:  # network errors, timeouts, HTTP errors, bad JSON
            last = e
            if attempt < len(RETRY_DELAYS):
                _sleep(RETRY_DELAYS[attempt])
    raise FetchError(f"{what}: {_describe(last)}")


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def get_json(url: str, what: str):
    return _with_retries(lambda: json.loads(http_get(url)), what)


# ---------- World Bank ----------

def world_bank_rows(iso3s, code: str, query: str) -> list:
    """[(iso3, year, value or None)], parsed inside the retried fetch so a
    malformed payload (non-numeric value, bad year) becomes a FetchError."""
    countries = ";".join(dict.fromkeys(iso3s))
    url = WORLD_BANK_API.format(countries=countries, code=code, query=query)

    def fetch():
        data = json.loads(http_get(url))
        # Success is [meta, rows]; errors come back as [{"message": ...}].
        if not (isinstance(data, list) and len(data) == 2 and isinstance(data[0], dict)
                and "page" in data[0] and isinstance(data[1], (list, type(None)))):
            raise ValueError("unexpected World Bank response shape")
        rows = []
        for row in data[1] or []:
            value = row.get("value")
            if value is not None:
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError("non-finite World Bank value")
            rows.append((row.get("countryiso3code"), int(row["date"]), value))
        return rows
    return _with_retries(fetch, f"World Bank {code}")


def world_bank_series(iso3s, code: str, first_year: int, last_year: int) -> dict:
    """{iso3: {year: value}} for non-null observations."""
    series = {iso3: {} for iso3 in iso3s}
    for iso3, year, value in world_bank_rows(iso3s, code, f"date={first_year}:{last_year}"):
        if iso3 in series and value is not None:
            series[iso3][year] = value
    return series


def latest_common_year(series: dict):
    common = set.intersection(*(set(years) for years in series.values()))
    return max(common) if common else None


def world_bank_latest(iso3: str, code: str):
    """(value, year) of the most recent non-empty observation, or None."""
    for row_iso3, year, value in world_bank_rows([iso3], code, "mrnev=1"):
        if row_iso3 == iso3 and value is not None:
            return value, year
    return None


def world_bank_url(code: str, cca2: str) -> str:
    return f"https://data.worldbank.org/indicator/{code}?locations={cca2}"


# ---------- Wikipedia + flag ----------

@dataclass
class WikiSummary:
    title: str
    extract: str
    url: str


def wikipedia_summary(title: str) -> WikiSummary:
    url = WIKIPEDIA_SUMMARY.format(title=urllib.parse.quote(title.replace(" ", "_"), safe=""))

    def fetch():
        data = json.loads(http_get(url))
        extract = (data.get("extract") or "").strip()
        if data.get("type") == "disambiguation" or not extract:
            raise ValueError("no usable extract")
        return WikiSummary(title=data.get("titles", {}).get("normalized") or data["title"],
                           extract=extract, url=data["content_urls"]["desktop"]["page"])
    return _with_retries(fetch, f"Wikipedia '{title}'")


def wikipedia_lead(title: str, on_fallback=None) -> WikiSummary:
    """The article's summary (title, URL) with its extract widened to the
    whole lead section, so longer prose still has source text behind it.
    Falls back to the summary's first paragraph if the lead fetch fails,
    calling on_fallback(reason) when given."""
    summary = wikipedia_summary(title)
    url = WIKIPEDIA_INTRO.format(title=urllib.parse.quote(summary.title.replace(" ", "_"), safe=""))

    def fetch():
        lead = json.loads(http_get(url))["query"]["pages"][0]["extract"].strip()
        if not lead:
            raise ValueError("empty lead")
        return lead
    try:
        lead = _with_retries(fetch, f"Wikipedia lead '{summary.title}'")
    except FetchError as e:
        if on_fallback:
            on_fallback(str(e))
        else:
            print(f"note: {e}; using the summary paragraph only", file=sys.stderr)
        return summary
    words = lead.split()
    summary.extract = lead if len(words) <= MAX_SOURCE_WORDS else " ".join(words[:MAX_SOURCE_WORDS])
    return summary


def fetch_flag(cca2: str, path: str) -> None:
    def fetch():
        data = http_get(FLAG_URL.format(cca2=cca2.lower()))
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("not a PNG")
        return data
    data = _with_retries(fetch, "flag image")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


# ---------- OpenAI ----------

_LINK_RE = re.compile(r"(?<!!)\(?\[([^\]]*)\]\([^)]*\)\)?")  # not images: those are rejected
_URL_RE = re.compile(r"https?://|www\.|\b[a-z][a-z0-9+.-]*://|\bmailto:", re.IGNORECASE)
# Markup that must never reach the Markdown: raw HTML tags and autolinks
# (the converter passes HTML through), inline/image link remnants,
# protocol-relative //host, reference links ([text][ref], [text] [ref],
# [text][]) and link definitions at any nesting level (in a blockquote or
# list too). A definition renders to nothing on its own but would activate
# a [ref] in another field, so the per-field render check cannot see it.
_MARKUP_RE = re.compile(r"<\s*[A-Za-z/!]|\]\(|!\[|(?<![:/])//\w|\][ ]?\[|^[ \t>*+\-\d.)]*\[[^\]]+\]:",
                        re.MULTILINE)


@functools.cache
def _md_to_kindle():
    import md_to_kindle  # heavy; imported once, only when a check needs it
    return md_to_kindle


def converter_view(text: str):
    """(soup, math placeholders) exactly as md_to_kindle.load_markdown() --
    normalizers, math protection and Markdown extensions -- sees text."""
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "check.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return _md_to_kindle().load_markdown(path)


def rendered_links(soup) -> list:
    """Every href/src (HTML-unescaped) in a converter soup."""
    return ([tag["href"] for tag in soup.find_all(href=True)]
            + [tag["src"] for tag in soup.find_all(src=True)])


def _openai_client():
    from openai import OpenAI  # reads OPENAI_API_KEY from the environment
    return OpenAI(timeout=60, max_retries=0)  # _llm retries once itself


def _json_format(name: str, properties: dict) -> dict:
    return {"format": {"type": "json_schema", "name": name, "strict": True, "schema": {
        "type": "object", "properties": properties,
        "required": list(properties), "additionalProperties": False}}}


def _clean_text(text: str) -> str:
    """Drop inline citation links the model may add despite instructions;
    links in the document only ever come from code."""
    text = _LINK_RE.sub("", text)
    return re.sub(r"[ \t]+([.,;:])", r"\1", re.sub(r"[ \t]{2,}", " ", text)).strip()


def _safe_text(value: str, what: str, cap: int) -> str:
    """Clean one model-written field and reject links, markup or overlength."""
    value = _clean_text(value)
    if _URL_RE.search(value):
        raise LLMError(f"field {what} contains a URL")
    if _MARKUP_RE.search(value):
        raise LLMError(f"field {what} contains markup")
    # Authoritative check through the converter's own pipeline: anything it
    # would turn into a link or image (magiclink emails, nested constructs,
    # math rendered as images, code blocks that may become diagram images)
    # is rejected.
    soup, math = converter_view(value)
    if math:
        raise LLMError(f"field {what} contains math")
    if soup.find(["a", "img", "pre"]):
        raise LLMError(f"field {what} renders a link, image or code block")
    if len(value.split()) > cap:
        raise LLMError(f"field {what} too long")
    return value


def _parse_output(resp, caps: dict) -> dict:
    if getattr(resp, "status", "completed") != "completed":
        raise LLMError(f"response {resp.status}")
    try:
        data = json.loads(resp.output_text)
    except (json.JSONDecodeError, TypeError) as e:
        raise LLMError("response was not valid JSON") from e
    for key, cap in caps.items():
        values = data.get(key)
        items = values if isinstance(values, list) else [values]
        cleaned = []
        for value in items:
            if not isinstance(value, str):
                raise LLMError(f"field {key} missing")
            cleaned.append(_safe_text(value, key, cap))
        data[key] = cleaned if isinstance(values, list) else cleaned[0]
    return data


def _log_usage(label: str, resp) -> None:
    usage = getattr(resp, "usage", None)
    if usage is not None:
        print(f"{label}: {usage.input_tokens} input / {usage.output_tokens} output tokens", flush=True)


GROUNDED_INSTRUCTIONS = """\
You write short passages for a morning reading digest read on a Kindle.
Use ONLY facts stated in the source text supplied for each field. Never add
facts, numbers, names or dates that are not in that text. British English,
plain prose: no markdown, no links, no URLs.

- country_blurb: two or three sentences (at most 70 words) on the country's
  geography, from COUNTRY SOURCE.
- research_headline: a short, intriguing headline (at most 12 words) for one
  interesting fact from RESEARCH SOURCE.
- research_body: 100-140 words in two or three short paragraphs (separated
  by a blank line) explaining that fact, its background and why it is
  interesting, from RESEARCH SOURCE. If the source supports less, write less;
  never pad with outside knowledge.

If a source text is empty, return an empty string for the fields that depend on it."""


def write_grounded_prose(country: str, country_extract: str, research_title: str,
                         research_extract: str, client=None) -> dict:
    """Call A: prose restating the supplied Wikipedia extracts. No tools."""
    client = client or _openai_client()
    resp = client.responses.create(
        model=MODEL,
        instructions=GROUNDED_INSTRUCTIONS,
        input=(f"COUNTRY: {country}\nCOUNTRY SOURCE:\n{country_extract}\n\n"
               f"RESEARCH TOPIC: {research_title}\nRESEARCH SOURCE:\n{research_extract}"),
        reasoning={"effort": "low"},
        max_output_tokens=1600,
        text=_json_format("grounded_prose", {
            "country_blurb": {"type": "string"},
            "research_headline": {"type": "string"},
            "research_body": {"type": "string"}}),
    )
    _log_usage("call A", resp)
    prose = _parse_output(resp, {"country_blurb": 95, "research_headline": 20, "research_body": 190})
    required = ((["country_blurb"] if country_extract.strip() else [])
                + (["research_headline", "research_body"] if research_extract.strip() else []))
    for key in required:
        if not prose[key]:
            raise LLMError(f"field {key} empty")
    return prose


AI_INSTRUCTIONS = """\
You write the "AI Engineering topic of the day" for a machine-learning
engineer who is deepening their AI Engineering skills. It is read on a
Kindle first thing in the morning, so it must be short and educational --
explain the idea and why it matters, not just a definition.

1. Choose ONE specific, concrete topic within or adjacent to today's theme.
   Prefer something practitioners are actively discussing now; terms newer
   than the examples are welcome. Do not repeat a recently covered topic.
2. Research it with web search before writing. Prefer primary or
   authoritative technical sources: official documentation, vendor
   engineering blogs, research papers, and the people or organisations that
   coined the term.
3. Write using only what your sources support.

Fields (British English, plain prose, no markdown, no links, no URLs, no
citation markers in the first three):
- topic_title: at most 8 words.
- concept: 120-170 words; two or three short paragraphs separated by a blank
  line. Explain how it works and include a concrete example.
- why_it_matters: one or two items, each a single sentence of at most 30 words.
- sources: one to three web pages from your search results that support the
  lesson. For each give its exact url and, in "supports", the specific claim
  from concept or why_it_matters that the page supports. Links belong only
  here, never in the other fields."""


def ai_prompt(theme: dict, summary: str, recent_topics: list) -> str:
    recent = "\n".join(f"- {t}" for t in recent_topics) or "- (none yet)"
    return (f"TODAY'S THEME: {theme['name']} -- {theme['description']}\n"
            f"Example subtopics (examples, not limits): {'; '.join(theme['examples'])}\n\n"
            f"THE WIDER FIELD (for direction of travel): {summary}\n\n"
            f"RECENTLY COVERED -- do not repeat:\n{recent}")


# Query parameters that only track clicks; anything else may select content.
_TRACKING_PARAMS = {"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid"}


def normalise_url(url: str) -> str:
    """Conservative match key: lowercase scheme and host, drop the fragment
    and known tracking parameters. Path case, other query parameters, www.
    and trailing slashes are kept, so only genuinely equal URLs match."""
    parts = urllib.parse.urlsplit(url.strip())
    query = urllib.parse.urlencode(
        [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
         if not (k.lower().startswith("utm_") or k.lower() in _TRACKING_PARAMS)])
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))


def search_actions(resp) -> dict:
    """{action type: count} of the web_search tool's calls (search,
    open_page, find_in_page), for cost tracking."""
    counts = {}
    for item in getattr(resp, "output", []) or []:
        if getattr(item, "type", None) == "web_search_call":
            kind = getattr(getattr(item, "action", None), "type", None) or "unknown"
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def tool_urls(resp) -> dict:
    """{match key: original URL} for every page the web_search tool returned
    in its results (action.sources) or opened (open_page)."""
    urls = {}
    for item in getattr(resp, "output", []) or []:
        if getattr(item, "type", None) != "web_search_call":
            continue
        action = getattr(item, "action", None)
        candidates = [getattr(s, "url", None) for s in (getattr(action, "sources", None) or [])]
        if getattr(action, "type", None) == "open_page":
            candidates.append(getattr(action, "url", None))
        for url in candidates:
            if url:
                urls.setdefault(normalise_url(url), url)
    return urls


def validated_sources(selected: list, known: dict) -> list:
    """The model's selected sources that match a tool-owned URL, deduped by
    URL and capped. Each keeps the tool's original URL; "supports" is the
    model's own attribution note, not independently verified evidence."""
    out, seen = [], set()
    for source in selected:
        key = normalise_url(source["url"])
        if key in known and key not in seen:
            seen.add(key)
            out.append({"url": known[key], "supports": source["supports"]})
        elif key not in known:
            print(f"note: rejected source not returned by web search: {source['url']}", file=sys.stderr)
    return out[:MAX_AI_SOURCES]


def link_label(url: str) -> str:
    """Readable link text built from the URL itself (host + path)."""
    parts = urllib.parse.urlsplit(url)
    label = parts.netloc.lower().removeprefix("www.") + parts.path.rstrip("/")
    return label if len(label) <= 70 else label[:67] + "..."


def write_ai_lesson(theme: dict, summary: str, recent_topics: list, client=None) -> dict:
    """Call B: the model picks a topic in today's theme, must search the web
    (at most 2 tool calls), and selects its supporting sources, which are
    kept only if they match URLs the search tool itself returned."""
    client = client or _openai_client()
    resp = client.responses.create(
        model=MODEL,
        instructions=AI_INSTRUCTIONS,
        input=ai_prompt(theme, summary, recent_topics),
        tools=[{"type": "web_search"}],
        tool_choice="required",
        max_tool_calls=2,
        include=["web_search_call.action.sources"],
        reasoning={"effort": "low"},
        max_output_tokens=3000,
        timeout=120,  # web search can take longer than the client's 60s default
        text=_json_format("ai_lesson", {
            "topic_title": {"type": "string"},
            "concept": {"type": "string"},
            "why_it_matters": {"type": "array", "items": {"type": "string"}},
            "sources": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["url", "supports"],
                "properties": {"url": {"type": "string"}, "supports": {"type": "string"}}}}}),
    )
    _log_usage("call B", resp)
    actions = search_actions(resp)
    print(f"call B: web_search actions {sum(actions.values())} "
          f"({', '.join(f'{k}={v}' for k, v in sorted(actions.items())) or 'none'})", flush=True)
    if not actions:
        raise LLMError("no web search was made")
    lesson = _parse_output(resp, {"topic_title": 14, "concept": 220, "why_it_matters": 50})
    if not 1 <= len(lesson["why_it_matters"]) <= 2:
        raise LLMError("why_it_matters must have 1-2 items")
    if not (lesson["topic_title"] and lesson["concept"] and all(lesson["why_it_matters"])):
        raise LLMError("empty lesson field")
    selected = lesson.get("sources")
    if not isinstance(selected, list) or not all(
            isinstance(s, dict) and isinstance(s.get("url"), str) and isinstance(s.get("supports"), str)
            for s in selected):
        raise LLMError("sources malformed")
    for source in selected:
        source["supports"] = " ".join(_safe_text(source["supports"], "supports", 60).split())
    lesson["sources"] = validated_sources(selected, tool_urls(resp))
    lesson["search_actions"] = actions
    if not lesson["sources"]:
        raise LLMError("no selected source matched the search tool's URLs")
    return lesson


REVIEW_INSTRUCTIONS = """\
You check an automatically generated morning digest before it is emailed.
You are given the rendered Markdown and the run's diagnostics list.
Flag only concrete problems: failed or unavailable sections, stale data
years, implausible values (e.g. unemployment of 45%), prose that contradicts
the table, empty or truncated sections, and anything in the diagnostics that
the reader should know. Do not comment on style or writing quality.

If you believe a statement is wrong or disputed even though it may
faithfully follow its cited source, do NOT put it in notes. Instead add a
source_disagreements entry naming the section (flag, research, ai,
uk_economy or jamaica_economy) and briefly saying what you believe and why,
in at most 40 words.

Return status "ok" with no notes and no source_disagreements when nothing is
wrong; otherwise "warning" with at most 3 notes of at most 25 words each.
No links or URLs anywhere."""


def _disagreement_schema(*fields) -> dict:
    return {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["section", "note", *fields],
        "properties": {"section": {"type": "string", "enum": list(SECTION_NAMES)},
                       "note": {"type": "string"}, **{f: {"type": "string"} for f in fields}}}}


def review_document(document: str, diagnostics: list, client=None) -> dict:
    """Call C: a short "run check" over the whole rendered document. No
    tools; any disagreements it records are then sourced by source_check()
    and kept only if their url matches a URL that search tool returned."""
    client = client or _openai_client()
    diag = "\n".join(f"- {d}" for d in diagnostics) or "- (none)"
    resp = client.responses.create(
        model=MODEL,
        instructions=REVIEW_INSTRUCTIONS,
        input=f"DIAGNOSTICS:\n{diag}\n\nDOCUMENT:\n{document}",
        reasoning={"effort": "low"},
        max_output_tokens=1200,
        text=_json_format("run_check", {
            "status": {"type": "string", "enum": ["ok", "warning"]},
            "notes": {"type": "array", "items": {"type": "string"}},
            "source_disagreements": _disagreement_schema()}),
    )
    _log_usage("call C", resp)
    review = _parse_output(resp, {"notes": 40})
    review["notes"] = review["notes"][:3]
    disagreements = validated_disagreements(review.get("source_disagreements"))
    if disagreements:
        try:
            sourced = source_check(disagreements, client=client)
        except Exception as e:
            print(f"note: dropped {len(disagreements)} reviewer disagreement(s); source check failed "
                  f"({_describe(e)})", file=sys.stderr)
            sourced = []
        if not sourced and not review["notes"]:
            # The warning was only about now-dropped disagreements, so it is
            # not a problem the reader needs flagged.
            review["status"] = "ok"
        disagreements = sourced
    review["source_disagreements"] = disagreements
    return review


SOURCE_CHECK_INSTRUCTIONS = """\
A reviewer disputes the statements below in a morning digest. For each
disagreement, find a web page that supports the reviewer's view, using one
web search for all of them. Return each disagreement with its section and
note copied exactly as given, plus in "url" the exact url of a page from
your search results that supports it. Prefer authoritative sources:
official or government statistics, reference works, primary sources and
established news organisations; avoid forums and user-generated sites.
Leave out any disagreement your search does not support. No other text."""


def source_check(disagreements: list, client=None) -> list:
    """Follow-up to call C, made only when it disputes something: one web
    search (at most one tool call for all notes) to source the
    disagreements. Each kept item must repeat one of call C's notes and cite
    a URL the search tool returned; the rest are dropped."""
    client = client or _openai_client()
    notes = "\n".join(f"- section: {d['section']}; note: {d['note']}" for d in disagreements)
    resp = client.responses.create(
        model=MODEL,
        instructions=SOURCE_CHECK_INSTRUCTIONS,
        input=f"DISAGREEMENTS:\n{notes}",
        tools=[{"type": "web_search"}],
        tool_choice="required",
        max_tool_calls=1,
        include=["web_search_call.action.sources"],
        reasoning={"effort": "low"},
        max_output_tokens=1200,
        timeout=120,  # web search can take longer than the client's 60s default
        text=_json_format("source_check", {"source_disagreements": _disagreement_schema("url")}),
    )
    _log_usage("source check", resp)
    actions = search_actions(resp)
    print(f"source check: web_search actions {sum(actions.values())} "
          f"({', '.join(f'{k}={v}' for k, v in sorted(actions.items())) or 'none'})", flush=True)
    sourced = validated_disagreements(_parse_output(resp, {}).get("source_disagreements"), tool_urls(resp))
    asked = {(d["section"], d["note"].lower()) for d in disagreements}
    for d in sourced:
        if (d["section"], d["note"].lower()) not in asked:
            print(f"note: dropped source-check item that is not one of call C's notes ({d['section']})",
                  file=sys.stderr)
    return [d for d in sourced if (d["section"], d["note"].lower()) in asked]


def validated_disagreements(items, known: dict = None) -> list:
    """Disagreements with sourced claims: known section, cleaned note
    without links, at most 50 words; duplicates dropped, capped. With known
    (the search tool's URLs, from source_check) each item also needs a url
    matching one of them, rendered as the tool's original URL; unsourced
    ones are dropped with a note on stderr."""
    if not isinstance(items, list):
        raise LLMError("source_disagreements missing")
    out, seen = [], set()
    for item in items:
        if not (isinstance(item, dict) and item.get("section") in SECTION_NAMES
                and isinstance(item.get("note"), str)
                and (known is None or isinstance(item.get("url"), str))):
            raise LLMError("source disagreement malformed")
        note = _safe_text(item["note"], "source disagreement note", 50)
        key = (item["section"], note.lower())
        if not note or key in seen:
            continue
        if known is None:
            seen.add(key)
            out.append({"section": item["section"], "note": note})
        elif normalise_url(item["url"]) in known:
            seen.add(key)
            out.append({"section": item["section"], "note": note, "url": known[normalise_url(item["url"])]})
        else:
            print(f"note: dropped reviewer disagreement without a web-search source ({item['section']}): "
                  f"{item['url'] or '(no url)'}", file=sys.stderr)
    return out[:MAX_DISAGREEMENTS]


# ---------- gathering ----------

@dataclass
class Facts:
    population: dict = None      # {"value", "uk_value", "year"}
    flag_file: str = None        # relative to inputs/
    country_wiki: WikiSummary = None
    research_wiki: WikiSummary = None
    uk_econ: dict = None         # {"indicator", "value", "year"}
    jm_econ: dict = None
    prose: dict = None           # call A
    ai: dict = None              # call B
    diagnostics: list = field(default_factory=list)

    def warn(self, message: str) -> None:
        print(f"warning: {message}", file=sys.stderr, flush=True)
        self.diagnostics.append(message)


def _gather_population(sel: Selection, facts: Facts) -> None:
    iso3 = sel.country["cca3"]
    year = sel.date.year
    try:
        series = world_bank_series([iso3, UK], "SP.POP.TOTL", year - POPULATION_YEARS, year)
    except FetchError as e:
        facts.warn(f"Population unavailable ({e}).")
        return
    # A zero or negative population is no data for that year.
    series = {iso3: {y: v for y, v in years.items() if v > 0} for iso3, years in series.items()}
    common = latest_common_year(series)
    if common is None:
        facts.warn(f"No population year shared by {sel.country['name']} and the UK in World Bank data.")
        return
    if series[UK] and common != max(series[UK]):
        facts.warn(f"Population comparison uses {common}, the latest year both countries have.")
    if year - common >= STALE_YEARS:
        facts.warn(f"Population latest World Bank year shared with the UK is {common}.")
    facts.population = {"value": series[iso3][common], "uk_value": series[UK][common], "year": common}


def _gather_economy(sel: Selection, facts: Facts, iso3: str, place: str, candidates: list):
    for i, indicator in enumerate(candidates):
        try:
            latest = world_bank_latest(iso3, indicator["code"])
        except FetchError as e:
            facts.warn(f"{place} economy fact unavailable ({e}).")
            return None
        if latest is None:
            facts.warn(f"{place}: no World Bank value for '{indicator['label']}'; tried the next indicator.")
            continue
        value, obs_year = latest
        if sel.date.year - obs_year >= STALE_YEARS:
            facts.warn(f"{place} '{indicator['label']}' latest World Bank value is from {obs_year}.")
        return {"indicator": indicator, "value": value, "year": obs_year}
    facts.warn(f"{place} economy fact unavailable (no data for {len(candidates)} indicators).")
    return None


def _wiki(title: str, facts: Facts, label: str):
    try:
        return wikipedia_lead(title, on_fallback=lambda reason: facts.warn(
            f"{label}: full lead unavailable ({reason}); used the summary paragraph only."))
    except FetchError as e:
        facts.warn(f"{label} unavailable ({e}).")
        return None


def _llm(call, facts: Facts, label: str):
    """Run a model call with one explicit retry; None (and a diagnostic) on failure."""
    for attempt in (1, 2):
        try:
            return call()
        except Exception as e:  # SDK errors, missing key, LLMError
            reason = str(e) if isinstance(e, LLMError) else _describe(e)
            if attempt == 1:
                print(f"note: {label} failed ({reason}); retrying once", file=sys.stderr, flush=True)
            else:
                facts.warn(f"{label} failed twice ({reason}).")
    return None


def gather(sel: Selection, data: dict, recent_topics: list, client=None) -> Facts:
    facts = Facts()
    _gather_population(sel, facts)
    flag_name = f"{slug(sel.date)}_flag.png"
    try:
        fetch_flag(sel.country["cca2"], os.path.join(INPUT_DIR, flag_name))
        facts.flag_file = flag_name
    except FetchError as e:
        facts.warn(f"Flag image unavailable ({e}).")
    facts.country_wiki = _wiki(sel.country.get("wikipedia_title", sel.country["name"]), facts,
                               "Country description source")
    facts.research_wiki = _wiki(sel.research["wikipedia_title"], facts, "Research fact source")
    facts.uk_econ = _gather_economy(sel, facts, UK, "UK", sel.uk_indicators)
    facts.jm_econ = _gather_economy(sel, facts, JAMAICA, "Jamaica", sel.jm_indicators)

    if facts.country_wiki or facts.research_wiki:
        facts.prose = _llm(lambda: write_grounded_prose(
            sel.country["name"], facts.country_wiki.extract if facts.country_wiki else "",
            sel.research["display_title"], facts.research_wiki.extract if facts.research_wiki else "",
            client=client), facts, "Prose writer (call A)")
    facts.ai = _llm(lambda: write_ai_lesson(sel.ai_theme, data["ai_summary"], recent_topics, client=client),
                    facts, "AI Engineering writer (call B)")
    if facts.ai:
        print(f"AI topic: {facts.ai['topic_title']} ({len(facts.ai['sources'])} sources)", flush=True)
    return facts


# ---------- rendering ----------

def slug(date: datetime.date) -> str:
    return f"daily_facts_{date.isoformat()}"


def display_title(date: datetime.date) -> str:
    return f"Daily Facts — {date:%A} {date.day} {date:%B %Y}"


def _big(value: float) -> str:
    for divisor, word in ((1e12, "trillion"), (1e9, "billion"), (1e6, "million")):
        if abs(value) >= divisor:
            return f"{value / divisor:.2f}".rstrip("0").rstrip(".") + f" {word}"
    return f"{value:,.0f}"


def format_value(value: float, fmt: str) -> str:
    if fmt == "pct":
        return f"{value:.1f}%"
    if fmt == "usd":
        return f"${_big(value)}"
    return _big(value)


def compare_to_uk(value: float, uk_value: float) -> str:
    ratio = value / uk_value
    if ratio >= 1:
        return f"{ratio:.1f}× the UK's"
    pct = ratio * 100
    if pct >= 10:
        return f"{pct:.0f}% of the UK's"
    if pct >= 1:
        return f"{pct:.1f}% of the UK's"
    return f"{pct:.2g}% of the UK's"


_UNAVAILABLE = "*Unavailable today:"


def _unavailable(what: str) -> str:
    return f"{_UNAVAILABLE} {what}. See the run check above.*"


def _reviewer_note(note: str, url: str) -> str:
    if not note.endswith((".", "!", "?")):
        note += "."
    return (f"> **AI reviewer note:** {note} Reviewer's source: [{link_label(url)}]({url}). "
            f"The text above follows its cited source; "
            f"this note is the AI reviewer's own view and may be wrong.")


def _paragraphs(text: str) -> str:
    return "\n\n".join(p.strip() for p in re.split(r"\n\s*\n", text) if p.strip())


def _flag_section(sel: Selection, facts: Facts) -> str:
    c = sel.country
    lines = [f"## Flag of the Day: {c['name']}", ""]
    if facts.flag_file:
        lines += [f"![Flag of {c['name']}]({facts.flag_file})", ""]
    else:
        lines += ["*Flag image unavailable today.*", ""]
    region = " · ".join(x for x in (c["region"], c["subregion"]) if x)
    if facts.population:
        p = facts.population
        population = f"{_big(p['value'])} ({p['year']})"
        vs_uk = "— (this is the UK)" if c["cca3"] == UK else f"{compare_to_uk(p['value'], p['uk_value'])} ({p['year']})"
    else:
        population = vs_uk = "unavailable today"
    lines += [f"| Country | {c['name']} |", "|---|---|",
              f"| Region | {region or '—'} |",
              f"| Capital | {', '.join(c['capital']) or '—'} |",
              f"| Currency | {', '.join(c['currencies']) or '—'} |",
              f"| Population | {population} |",
              f"| vs UK | {vs_uk} |", ""]
    blurb = (facts.prose or {}).get("country_blurb")
    if blurb and facts.country_wiki:
        lines += [_paragraphs(blurb), ""]
    else:
        lines += [_unavailable("the country description"), ""]
    sources = []
    if blurb and facts.country_wiki:
        sources.append(f"[Wikipedia]({facts.country_wiki.url})")
    if facts.population:
        sources.append(f"[World Bank population]({world_bank_url('SP.POP.TOTL', c['cca2'])})")
    if sources:
        lines.append("Sources: " + " · ".join(sources))
    return "\n".join(lines).rstrip()


def _research_section(sel: Selection, facts: Facts) -> str:
    prose = facts.prose or {}
    if not (facts.research_wiki and prose.get("research_headline") and prose.get("research_body")):
        return f"## Research Fact: {sel.research['display_title']}\n\n{_unavailable('the research fact')}"
    return (f"## Research Fact: {prose['research_headline']}\n\n{_paragraphs(prose['research_body'])}\n\n"
            f"Source: [{facts.research_wiki.title} — Wikipedia]({facts.research_wiki.url})")


def _ai_section(sel: Selection, facts: Facts) -> str:
    theme = sel.ai_theme["name"]
    if not facts.ai:
        return f"## AI Engineering\n\n*Theme: {theme}*\n\n{_unavailable('the AI Engineering topic')}"
    ai = facts.ai
    why = "\n".join(f"- {item}" for item in ai["why_it_matters"])
    sources = "\n".join(f"- [{link_label(src['url'])}]({src['url']})" for src in ai["sources"])
    return (f"## AI Engineering: {ai['topic_title']}\n\n*Theme: {theme}*\n\n"
            f"### Concept\n\n{_paragraphs(ai['concept'])}\n\n"
            f"### Why it matters\n\n{why}\n\n### Sources\n\n{sources}")


def _economy_section(heading: str, econ: dict, cca2: str) -> str:
    if not econ:
        return f"## {heading}\n\n{_unavailable(heading.lower() + ' fact')}"
    ind = econ["indicator"]
    return (f"## {heading}\n\n**{ind['label']}:** {format_value(econ['value'], ind['format'])} "
            f"in {econ['year']}.\n\n{ind['meaning']}\n\n"
            f"Source: [World Bank — {ind['code']}]({world_bank_url(ind['code'], cca2)})")


def render_sections(sel: Selection, facts: Facts) -> dict:
    """{call C section key: rendered section}, in document order."""
    return {
        "flag": _flag_section(sel, facts),
        "research": _research_section(sel, facts),
        "ai": _ai_section(sel, facts),
        "uk_economy": _economy_section("UK Economy", facts.uk_econ, "GB"),
        "jamaica_economy": _economy_section("Jamaica Economy", facts.jm_econ, "JM"),
    }


def render_body(sel: Selection, facts: Facts, reviewer_notes: dict = None) -> str:
    """The five sections. reviewer_notes ({section key: [disagreement]}) adds
    call C's disagreements after the section's sourced content, which is
    unchanged; unavailable sections get none."""
    parts = []
    for key, text in render_sections(sel, facts).items():
        if _UNAVAILABLE not in text:
            text += "".join(f"\n\n{_reviewer_note(d['note'], d['url'])}"
                            for d in (reviewer_notes or {}).get(key, []))
        parts.append(text)
    return "\n\n".join(parts)


def run_check_box(review, diagnostics: list, disagreements: list = ()) -> str:
    """The warning blockquote shown under the title, or "" when all is well.
    Call C's notes come first; the raw diagnostics always follow, so an
    outage of the reviewer itself is still visible. Disagreements with
    sourced claims get a one-line pointer to the sections that carry them."""
    notes = list(review["notes"]) if review and review.get("status") == "warning" else []
    if review is None and diagnostics:
        notes.insert(0, "Run check unavailable; raw diagnostics below.")
    sections = {d["section"] for d in disagreements}
    pointer = ("AI reviewer disagrees with a sourced claim in: "
               + ", ".join(name for key, name in SECTION_NAMES.items() if key in sections) + "."
               if sections else "")
    flagged = bool(review) and review.get("status") == "warning"
    if not notes and not diagnostics and not pointer and not flagged:
        return ""
    headline = (" ".join(notes) if notes else "see diagnostics below." if diagnostics
                else pointer or "the reviewer flagged a problem but gave no details.")
    lines = ["> **Run check — warning:** " + headline]
    if pointer and headline != pointer:
        lines += [">", f"> {pointer}"]
    if diagnostics:
        lines += [">"] + [f"> - {d}" for d in diagnostics]
    return "\n".join(lines)


def assemble(date: datetime.date, box: str, body: str) -> str:
    parts = [f"# {display_title(date)}"] + ([box] if box else []) + [body]
    return "\n\n".join(parts) + "\n"


def allowed_links(sel: Selection, facts: Facts, disagreements: list = ()) -> set:
    """Every link target code may put in the document, including the
    tool-validated sources of call C's disagreements."""
    allowed = {world_bank_url("SP.POP.TOTL", sel.country["cca2"])}
    if facts.flag_file:
        allowed.add(facts.flag_file)
    allowed.update(w.url for w in (facts.country_wiki, facts.research_wiki) if w)
    allowed.update(world_bank_url(econ["indicator"]["code"], cca2)
                   for econ, cca2 in ((facts.uk_econ, "GB"), (facts.jm_econ, "JM")) if econ)
    if facts.ai:
        allowed.update(src["url"] for src in facts.ai["sources"])
    allowed.update(d["url"] for d in disagreements)
    return allowed


def check_links(document: str, allowed: set) -> None:
    """Defence in depth across fields, through the converter's own pipeline:
    every href/src must be one code put there, and nothing may be read as
    math (rendered later as images), else UnsafeDocumentError."""
    soup, math = converter_view(document)
    if math:
        raise UnsafeDocumentError("document contains math, which the converter renders as images")
    for link in rendered_links(soup):
        if link not in allowed:
            raise UnsafeDocumentError(f"unexpected link target {link!r}")


def build_document(sel: Selection, facts: Facts, client=None):
    """Return (markdown, warnings, disagreements) with the run-check box under
    the title. Call C reviews the body without reviewer notes; its
    disagreements with sourced claims are then added under their sections
    and are not counted as warnings. Raises UnsafeDocumentError if the final
    document links anywhere code did not put a link."""
    body = render_body(sel, facts)
    review = _llm(lambda: review_document(assemble(sel.date, "", body), facts.diagnostics, client=client),
                  facts, "Run check (call C)")
    sections = render_sections(sel, facts)
    disagreements = [d for d in (review["source_disagreements"] if review else [])
                     if _UNAVAILABLE not in sections[d["section"]]]
    reviewer_notes = {}
    for d in disagreements:
        reviewer_notes.setdefault(d["section"], []).append(d)
    body = render_body(sel, facts, reviewer_notes)
    box = run_check_box(review, facts.diagnostics, disagreements)
    warnings = list(review["notes"]) if review and review.get("status") == "warning" else []
    if review and review.get("status") == "warning" and not warnings and not disagreements:
        warnings = ["Run check flagged a problem without details."]
    document = assemble(sel.date, box, body)
    check_links(document, allowed_links(sel, facts, disagreements))
    return document, warnings + facts.diagnostics, disagreements


# ---------- state ----------

def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {"sent": {}}
    with open(path, encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("sent", {})
    return state


def save_state(path: str, state: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def recent_ai_topics(state: dict, limit: int = RECENT_AI_TOPICS) -> list:
    entries = sorted(state["sent"].items())
    return [e["ai_topic"] for _, e in entries if e.get("ai_topic")][-limit:]


def state_record(sel: Selection, facts: Facts, warnings: list, disagreements: list = ()) -> dict:
    return {
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "country": sel.country["cca3"],
        "research_title": sel.research["wikipedia_title"],
        "ai_theme": sel.ai_theme["name"],
        "ai_search_actions": facts.ai["search_actions"] if facts.ai else {},
        "ai_topic": facts.ai["topic_title"] if facts.ai else None,
        # "model_attribution" is the model's own note on what each page supports.
        "ai_sources": ([{"url": src["url"], "model_attribution": src["supports"]} for src in facts.ai["sources"]]
                       if facts.ai else []),
        "indicators": {"UK": facts.uk_econ["indicator"]["code"] if facts.uk_econ else None,
                       "JM": facts.jm_econ["indicator"]["code"] if facts.jm_econ else None},
        "warnings": warnings,
        "reviewer_disagreements": [{"section": d["section"], "note": d["note"], "url": d["url"]}
                                   for d in disagreements],
    }


# ---------- conversion + commands ----------

def write_input(date: datetime.date, document: str) -> str:
    os.makedirs(INPUT_DIR, exist_ok=True)
    path = os.path.join(INPUT_DIR, f"{slug(date)}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(document)
    return path


def run_converter(input_path: str, date: datetime.date, send: bool) -> int:
    output_path = os.path.join(OUTPUT_DIR, f"{slug(date)}.epub")
    cmd = [sys.executable, CONVERTER, input_path, output_path, "--title", display_title(date)]
    if not send:
        cmd.append("--no-send-to-kindle")
    return subprocess.run(cmd).returncode


def today_london() -> datetime.date:
    return datetime.datetime.now(LONDON).date()


def check_data(data_dir: str = DATA_DIR) -> int:
    """Validate the committed catalogues against Wikipedia (network; manual
    use after editing). Rewrites redirected titles to their canonical form,
    drops duplicates, and prints category counts."""
    path = os.path.join(data_dir, "research_topics.json")
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    kept, seen, problems = [], set(), 0
    for topic in doc["topics"]:
        try:
            canonical = wikipedia_summary(topic["wikipedia_title"]).title
        except FetchError as e:
            print(f"UNRESOLVED: {topic['wikipedia_title']} ({e})")
            problems += 1
            kept.append(topic)
            continue
        if canonical != topic["wikipedia_title"]:
            print(f"redirect: {topic['wikipedia_title']} -> {canonical}")
            topic["wikipedia_title"] = canonical
            topic["display_title"] = re.sub(r"\s*\(.*\)$", "", canonical)
        if canonical in seen:
            print(f"duplicate dropped: {canonical}")
            continue
        seen.add(canonical)
        kept.append(topic)
    doc["topics"] = kept
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
        f.write("\n")
    counts = {}
    for topic in kept:
        counts[topic["category"]] = counts.get(topic["category"], 0) + 1
    print(f"{len(kept)} research topics: {counts}")
    for country in load_data(data_dir)["countries"]:
        title = country.get("wikipedia_title", country["name"])
        try:
            description = json.loads(http_get(WIKIPEDIA_SUMMARY.format(
                title=urllib.parse.quote(title.replace(" ", "_"), safe="")))).get("description", "")
        except Exception as e:
            print(f"COUNTRY UNRESOLVED: {title} ({_describe(e)})")
            problems += 1
            continue
        finally:
            _sleep(0.3)  # Wikipedia rate-limits rapid bursts
        if not any(word in description.lower() for word in ("country", "state", "nation")):
            print(f"CHECK COUNTRY PAGE: {title} -> {description!r} (set wikipedia_title if wrong)")
    themes = load_data(data_dir)["ai_themes"]
    print(f"{len(themes)} AI themes: {[t['name'] for t in themes]}")
    return 1 if problems else 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true",
                      help="Build today's EPUB without sending it or touching state.")
    mode.add_argument("--dry-run", action="store_true",
                      help="Write today's Markdown only; no convert, send, or state change.")
    mode.add_argument("--check-data", action="store_true",
                      help="Validate daily_facts_data/ catalogues against Wikipedia (network).")
    parser.add_argument("--date", type=datetime.date.fromisoformat,
                        help="Date to build (YYYY-MM-DD; default: today in Europe/London).")
    parser.add_argument("--state", default=DEFAULT_STATE, help="Path to the delivery state JSON file.")
    return parser


def main(argv=None, client=None) -> int:
    args = build_parser().parse_args(argv)
    if args.check_data:
        return check_data()
    date = args.date or today_london()
    deliver = not (args.preview or args.dry_run)
    state = load_state(args.state) if deliver or os.path.exists(args.state) else {"sent": {}}
    if deliver and date.isoformat() in state["sent"]:
        print(f"{slug(date)} was already sent; nothing to do.")
        return EXIT_ALREADY_SENT

    data = load_data()
    sel = select(date, data)
    print(f"{slug(date)}: {sel.country['name']} · {sel.research['display_title']} · "
          f"{sel.ai_theme['name']} · UK {sel.uk_indicators[0]['code']} · JM {sel.jm_indicators[0]['code']}",
          flush=True)
    facts = gather(sel, data, recent_ai_topics(state), client=client)
    try:
        document, warnings, disagreements = build_document(sel, facts, client=client)
    except UnsafeDocumentError as e:
        print(f"error: {e}; nothing written, sent or recorded.", file=sys.stderr)
        return EXIT_UNSAFE_DOCUMENT
    input_path = write_input(date, document)
    print(f"Wrote {input_path} ({len(document.split())} words, {len(warnings)} warning(s))", flush=True)
    if args.dry_run:
        return EXIT_SENT

    rc = run_converter(input_path, date, send=deliver)
    if rc != 0:
        print(f"error: md_to_kindle.py exited {rc}; {date} left unrecorded for a rerun.", file=sys.stderr)
        return rc
    if not deliver:
        return EXIT_SENT
    state["sent"][date.isoformat()] = state_record(sel, facts, warnings, disagreements)
    save_state(args.state, state)
    print(f"Recorded {date} as delivered.")
    if warnings:
        print(f"::warning::Daily Facts {date} was sent with {len(warnings)} warning(s); "
              f"see the run check at the top of the document.")
    return EXIT_SENT


if __name__ == "__main__":
    sys.exit(main())
