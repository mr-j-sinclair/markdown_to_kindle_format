#!/usr/bin/env python3
"""Deliver new "Read the Bible: The New Testament in a Year" (NTIY) episode
notes to Kindle.

Runs unattended (see .github/workflows/ntiy_daily.yml): reads the Podbean RSS
feed, picks the oldest episode GUID not yet in the state file, writes its
cleaned show notes to inputs/<slug>.html, and hands that to the existing
md_to_kindle.py CLI, which converts to EPUB and emails it via
kindle_delivery.py. The GUID is recorded only after the CLI exits 0, so
delivery is at-least-once: a failure is retried on the next run, and a crash
between "sent" and "state saved" can at worst produce one duplicate.

The feed's <content:encoded> already carries the full episode notes (the
episode web page adds nothing), so nothing is scraped. Cleaning is
deterministic and format-agnostic -- the show-notes layout has changed several
times over the feed's history -- dropping only the repeated title line, the
"Episode Notes:" label, the homepage/reading-plan/contact/Facebook/website
boilerplate lines and empty paragraphs; every other node passes through
verbatim.

Exit codes for --next: 0 = one episode delivered, 3 = nothing new,
4 = flood guard tripped (nothing sent), 1/2 = feed or converter failure
(2 = EPUB built but Kindle send failed, passed through from md_to_kindle.py).
"""

import argparse
import io
import datetime
import email.utils
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from PIL import Image, ImageEnhance, ImageFilter

FEED_URL = "https://feed.podbean.com/NewTestamentinaYear/feed.xml"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(SCRIPT_DIR, "inputs")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "outputs")
CONVERTER = os.path.join(SCRIPT_DIR, "md_to_kindle.py")
DEFAULT_MAX_SENDS = 5
# Same size as Podbean's own episode-page banner (og:image) of the cover art.
BANNER_SIZE = (1200, 628)

EXIT_DELIVERED = 0
EXIT_NOTHING_NEW = 3
EXIT_FLOOD_GUARD = 4

_NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
}

# "1 John 5. Day 198 - Read the Bible: ..." (current) and
# "Revelation 22 - Day 260 - The New Testament in a Year" (2025 cycle), with
# the occasional "NEW!  " / "UPDATED!  " prefix.
_TITLE_RE = re.compile(r"^(?:[A-Z]+!\s+)?(?P<ref>.+?)(?:\.|\s+-)\s+Day\s+(?P<day>\d+)\b")
_NOTES_LABEL_RE = re.compile(r"^Episode Notes:?$", re.IGNORECASE)
_SECTION_LABEL_RE = re.compile(r"^(?:In Today['’]s Episode|Episode (?:Highlights|Overview)):?$",
                               re.IGNORECASE)
_BOILERPLATE_RE = re.compile(
    r"^(?:Podcast Homepage|Bible Reading Plan|Contact Sean|(?:Follow )?NTIY (?:on Facebook|Website))\b",
    re.IGNORECASE)
_SCRIPTURE_LINE_RE = re.compile(r"^Today['’]s Scripture:", re.IGNORECASE)
# The episode's key verse, e.g. `"And this is ..." — 1 John 5:11` or
# `“Today, if you hear His voice ...” Hebrews 3:15`: a paragraph that opens
# with a quotation mark and ends with a chapter:verse reference.
_KEY_VERSE_RE = re.compile(
    r"^[\"“].+[\"”]\s*(?:[—–-]\s*)?(?:[1-3]\s)?[A-Z][a-z]+\s\d+:\d+(?:[–-]\d+)?\.?$")
_INLINE_BOILERPLATE_RE = re.compile(
    r"\s(?:Podcast Homepage|Bible Reading Plan|Contact Sean|(?:Follow )?NTIY (?:on Facebook|Website)):")


@dataclass
class Episode:
    guid: str
    title: str
    link: str
    pub_date: datetime.datetime
    content_html: str
    image_url: str = ""


# ---------- feed ----------

def fetch_feed(url: str = FEED_URL) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "markdown_to_kindle_format/ntiy_feed"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _itunes_image(element) -> str:
    image = element.find("itunes:image", _NS)
    return image.get("href", "").strip() if image is not None else ""


def parse_feed(xml_bytes: bytes):
    """Return (channel_info, episodes oldest->newest)."""
    channel = ET.fromstring(xml_bytes).find("channel")
    cover = channel.find("itunes:image", _NS)
    cover_url = cover.get("href", "").strip() if cover is not None else ""
    info = {
        "title": (channel.findtext("title") or "").strip(),
        "author": (channel.findtext("itunes:author", namespaces=_NS) or "").strip(),
    }
    episodes = []
    for item in channel.findall("item"):
        guid = (item.findtext("guid") or "").strip()
        if not guid:
            continue
        content = item.findtext("content:encoded", namespaces=_NS) or item.findtext("description") or ""
        episodes.append(Episode(
            guid=guid,
            title=(item.findtext("title") or "").strip(),
            link=(item.findtext("link") or "").strip(),
            pub_date=email.utils.parsedate_to_datetime(item.findtext("pubDate")),
            content_html=content,
            image_url=_itunes_image(item) or cover_url,
        ))
    # Feed order is newest-first; reverse before the stable sort so
    # same-timestamp items keep a sensible oldest-first order.
    episodes.reverse()
    episodes.sort(key=lambda e: e.pub_date)
    return info, episodes


# ---------- naming ----------

def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def episode_names(episode: Episode):
    """Return (slug, display_title). The slug becomes the inputs/outputs
    filename and so the Send-to-Kindle attachment name."""
    m = _TITLE_RE.match(episode.title)
    if m:
        ref = m.group("ref").strip()
        day = int(m.group("day"))
        return f"ntiy-day-{day:03d}-{_slugify(ref)}", f"{ref} — Day {day}"
    return f"ntiy-{_slugify(episode.title)[:60].rstrip('-')}", episode.title


# ---------- show-notes cleaning ----------

def _norm(text: str) -> str:
    return " ".join(text.split())


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _is_title_line(line: str, episode_title: str) -> bool:
    line_m, title_m = _TITLE_RE.match(line), _TITLE_RE.match(episode_title)
    if line_m and title_m:
        return (line_m.group("day") == title_m.group("day")
                and _alnum(line_m.group("ref")) == _alnum(title_m.group("ref")))
    a, b = _alnum(line), _alnum(episode_title)
    return bool(a) and (b.startswith(a) or a.startswith(b))


def _split_on_br(block: Tag):
    """Split a block's children into <br>-separated segments (lists of nodes)."""
    segments, current = [], []
    for child in list(block.contents):
        if isinstance(child, Tag) and child.name == "br":
            segments.append(current)
            current = []
        else:
            current.append(child)
    segments.append(current)
    return segments


def _segment_text(nodes) -> str:
    return _norm("".join(n.get_text() if isinstance(n, Tag) else str(n) for n in nodes))


def _truncate_inline_boilerplate(seg):
    """Some episodes (e.g. Days 192-194) run the boilerplate onto the same
    line as "Today's Scripture: <a>...</a>" with no <br>. Cut such a line at
    the first "<Label>:" -- only ever on the Scripture line. Returns the new
    node list, or None when nothing needed cutting."""
    if not _SCRIPTURE_LINE_RE.match(_segment_text(seg)):
        return None
    for i, node in enumerate(seg):
        if isinstance(node, NavigableString):
            m = _INLINE_BOILERPLATE_RE.search(str(node))
            if m:
                head = str(node)[:m.start()].rstrip()
                return seg[:i] + ([NavigableString(head)] if head else [])
    return None


def _strip_boilerplate_lines(block: Tag, soup):
    """Return the block minus any boilerplate <br>-separated lines, the
    untouched block when there were none, or None when nothing is left."""
    segments = _split_on_br(block)
    kept, changed = [], False
    for seg in segments:
        if _BOILERPLATE_RE.match(_segment_text(seg)):
            changed = True
            continue
        truncated = _truncate_inline_boilerplate(seg)
        if truncated is not None:
            seg, changed = truncated, True
        kept.append(seg)
    if not changed:
        return block
    while kept and not _segment_text(kept[-1]):
        kept.pop()
    if not any(_segment_text(seg) for seg in kept):
        return None
    rebuilt = soup.new_tag(block.name, attrs=dict(block.attrs))
    for i, seg in enumerate(kept):
        if i:
            rebuilt.append(soup.new_tag("br"))
        for node in seg:
            rebuilt.append(node.extract())
    return rebuilt


def clean_notes(content_html: str, episode_title: str) -> list:
    """Return the episode's show-notes body as a list of top-level Tags."""
    soup = BeautifulSoup(content_html, "html.parser")
    out = []
    first = True
    for node in list(soup.contents):
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            text = _norm(str(node))
            if not text:
                continue
            # Bare top-level text (e.g. the "In Today's Episode" marker)
            # would otherwise be dropped by md_to_kindle's split_chapters.
            block = soup.new_tag("p")
            block.string = text
        elif isinstance(node, Tag):
            block = node
        else:
            continue

        text = _norm(block.get_text())
        if not text and block.find("img") is None:
            continue
        if first:
            first = False
            first_line = _segment_text(_split_on_br(block)[0])
            if _is_title_line(first_line, episode_title):
                continue
        if _NOTES_LABEL_RE.match(text):
            continue
        if _SECTION_LABEL_RE.match(text):
            heading = soup.new_tag("h2")
            heading.string = text
            out.append(heading)
            continue
        if block.name in ("ul", "ol"):
            for li in block.find_all("li", recursive=False):
                if _BOILERPLATE_RE.match(_norm(li.get_text())):
                    li.decompose()
            if not block.find("li"):
                continue
        else:
            block = _strip_boilerplate_lines(block, soup)
            if block is None:
                continue
            if block.name == "p" and _KEY_VERSE_RE.match(_norm(block.get_text())):
                # Structural only: the paragraph itself is kept verbatim.
                quote = soup.new_tag("blockquote")
                quote.append(block)
                block = quote
        out.append(block)
    return out


def make_banner(image_bytes: bytes) -> bytes:
    """Recreate Podbean's 1200x628 episode-page banner from the square feed
    cover art: the art scaled to full height and centred over a blurred,
    darkened copy of itself. Returns JPEG bytes (~60 KB vs ~1.1 MB PNG)."""
    width, height = BANNER_SIZE
    art = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    scale = max(width / art.width, height / art.height)
    background = art.resize((round(art.width * scale), round(art.height * scale)), Image.LANCZOS)
    left, top = (background.width - width) // 2, (background.height - height) // 2
    background = background.crop((left, top, left + width, top + height))
    background = ImageEnhance.Brightness(background.filter(ImageFilter.GaussianBlur(30))).enhance(0.6)
    foreground = art.resize((round(art.width * height / art.height), height), Image.LANCZOS)
    background.paste(foreground, ((width - foreground.width) // 2, 0))
    out = io.BytesIO()
    background.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


def prepare_cover(episode: Episode, slug: str):
    """Download the cover art and save it as an inputs/ banner JPEG beside
    the episode HTML. Returns its filename (relative to inputs/), or None on
    any failure -- build_document then falls back to the remote URL, which
    md_to_kindle.py fetches as-is or replaces with a placeholder note."""
    if not episode.image_url:
        return None
    try:
        req = urllib.request.Request(episode.image_url,
                                     headers={"User-Agent": "markdown_to_kindle_format/ntiy_feed"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            banner = make_banner(resp.read())
    except Exception as e:
        print(f"warning: could not prepare cover banner ({e}); using the original image URL",
              file=sys.stderr)
        return None
    os.makedirs(INPUT_DIR, exist_ok=True)
    filename = f"{slug}-cover.jpg"
    with open(os.path.join(INPUT_DIR, filename), "wb") as f:
        f.write(banner)
    return filename


def build_document(episode: Episode, image_src: str = None) -> tuple:
    """Return (slug, display_title, html_document) for one episode.
    image_src overrides the cover image (e.g. a local banner from
    prepare_cover); otherwise the feed's image URL is used."""
    slug, display_title = episode_names(episode)
    soup = BeautifulSoup("", "html.parser")
    heading = soup.new_tag("h1")
    heading.string = display_title
    footer = soup.new_tag("p")
    footer.append("Original episode: ")
    link = soup.new_tag("a", href=episode.link)
    link.string = episode.title
    footer.append(link)
    footer.append(soup.new_tag("br"))
    footer.append(f"Published: {episode.pub_date.day} {episode.pub_date:%B %Y}")
    parts = [heading]
    image_src = image_src or episode.image_url
    if image_src:
        # md_to_kindle.py embeds a local file via resolve_local_images, or
        # fetches a URL via resolve_remote_images (placeholder on failure).
        parts.append(soup.new_tag("img", attrs={"src": image_src, "alt": "Podcast cover art"}))
    parts += [*clean_notes(episode.content_html, episode.title), footer]
    body = "\n".join(str(p) for p in parts)
    doc = (f'<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
           f'<title>{html.escape(display_title)}</title></head>\n<body>\n{body}\n</body></html>\n')
    return slug, display_title, doc


# ---------- state ----------

def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {"processed": {}}
    with open(path, encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("processed", {})
    return state


def save_state(path: str, state: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def mark_processed(state: dict, episode: Episode, how: str) -> None:
    state["processed"][episode.guid] = {
        "title": episode.title,
        "how": how,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }


def unseen_episodes(episodes, state):
    return [e for e in episodes if e.guid not in state["processed"]]


# ---------- conversion ----------

def write_input(slug: str, doc: str) -> str:
    os.makedirs(INPUT_DIR, exist_ok=True)
    path = os.path.join(INPUT_DIR, f"{slug}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path


def run_converter(input_path, slug, display_title, channel, send=True) -> int:
    output_path = os.path.join(OUTPUT_DIR, f"{slug}.epub")
    cmd = [sys.executable, CONVERTER, input_path, output_path, "--title", display_title]
    if channel.get("author"):
        cmd += ["--author", channel["author"]]
    if channel.get("title"):
        cmd += ["--subtitle", channel["title"]]
    if not send:
        cmd.append("--no-send-to-kindle")
    return subprocess.run(cmd).returncode


# ---------- commands ----------

def cmd_next(episodes, channel, state, state_path, max_sends, dry_run) -> int:
    unseen = unseen_episodes(episodes, state)
    if not unseen:
        print("No new episodes.")
        return EXIT_NOTHING_NEW
    if len(unseen) > max_sends:
        print(f"error: {len(unseen)} unseen episodes exceeds --max-sends {max_sends}; "
              f"sending nothing. If the feed's GUIDs changed, re-run --seed; otherwise "
              f"raise --max-sends for one run.", file=sys.stderr)
        return EXIT_FLOOD_GUARD
    episode = unseen[0]
    slug, display_title, doc = build_document(
        episode, prepare_cover(episode, episode_names(episode)[0]))
    input_path = write_input(slug, doc)
    print(f"{len(unseen)} unseen; processing {episode.title!r} -> {input_path}", flush=True)
    if dry_run:
        print("Dry run: not converting, sending, or recording state.")
        return EXIT_DELIVERED
    rc = run_converter(input_path, slug, display_title, channel, send=True)
    if rc != 0:
        print(f"error: md_to_kindle.py exited {rc}; {episode.guid} left unprocessed "
              f"for the next run to retry.", file=sys.stderr)
        return rc
    mark_processed(state, episode, "sent")
    save_state(state_path, state)
    print(f"Recorded {episode.guid} as delivered.")
    return EXIT_DELIVERED


def cmd_seed(episodes, state, state_path, leave_newest) -> int:
    to_mark = episodes[:len(episodes) - leave_newest] if leave_newest else episodes
    added = 0
    for episode in to_mark:
        if episode.guid not in state["processed"]:
            mark_processed(state, episode, "seeded")
            added += 1
    save_state(state_path, state)
    print(f"Seeded {added} episode(s) as already processed; "
          f"{len(unseen_episodes(episodes, state))} left unseen.")
    return 0


def cmd_preview(episodes, channel) -> int:
    episode = episodes[-1]
    slug, display_title, doc = build_document(
        episode, prepare_cover(episode, episode_names(episode)[0]))
    input_path = write_input(slug, doc)
    print(f"Previewing newest episode {episode.title!r} -> {input_path} (no send, no state change)", flush=True)
    return run_converter(input_path, slug, display_title, channel, send=False)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--next", action="store_true",
                      help="Deliver the oldest unseen episode (at most one) and record it.")
    mode.add_argument("--seed", action="store_true",
                      help="Mark every current feed episode as processed without sending (first-time setup).")
    mode.add_argument("--preview", action="store_true",
                      help="Build an EPUB of the newest episode without sending or touching state.")
    parser.add_argument("--state", default=os.path.join(SCRIPT_DIR, "state", "ntiy_state.json"),
                        help="Path to the processed-GUID JSON state file.")
    parser.add_argument("--feed", default=FEED_URL, help="RSS feed URL, or a local file path.")
    parser.add_argument("--max-sends", type=int, default=DEFAULT_MAX_SENDS,
                        help="Refuse to send anything when more episodes than this are unseen.")
    parser.add_argument("--leave-newest", type=int, default=0,
                        help="With --seed: leave the newest N episodes unseen.")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --next: write the input file only; no convert, send, or state change.")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if os.path.exists(args.feed):
        with open(args.feed, "rb") as f:
            xml_bytes = f.read()
    else:
        xml_bytes = fetch_feed(args.feed)
    channel, episodes = parse_feed(xml_bytes)
    if not episodes:
        print("error: feed contains no episodes", file=sys.stderr)
        return 1
    if args.preview:
        return cmd_preview(episodes, channel)
    state = load_state(args.state)
    if args.seed:
        return cmd_seed(episodes, state, args.state, args.leave_newest)
    return cmd_next(episodes, channel, state, args.state, args.max_sends, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
