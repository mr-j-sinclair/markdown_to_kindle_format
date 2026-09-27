#!/usr/bin/env python3
"""Fetch a Good News Translation passage from the Bible Society (UK) site as Markdown.

Usage:
    .venv/bin/python3 .claude/skills/order-of-service/scripts/fetch_gnt.py BOOK CHAPTER VERSES

    BOOK     USFM code: GEN EXO LEV NUM DEU JOS JDG RUT 1SA 2SA 1KI 2KI 1CH 2CH
             EZR NEH EST JOB PSA PRO ECC SNG ISA JER LAM EZK DAN HOS JOL AMO
             OBA JON MIC NAM HAB ZEP HAG ZEC MAL MAT MRK LUK JHN ACT ROM 1CO
             2CO GAL EPH PHP COL 1TH 2TH 1TI 2TI TIT PHM HEB JAS 1PE 2PE 1JN
             2JN 3JN JUD REV
    CHAPTER  e.g. 17
    VERSES   e.g. 1-7   or   1-4,12-16   or   5

Example:
    fetch_gnt.py EXO 17 1-7

Prints section headings, cross-reference lines, verse-numbered text (prose as
paragraphs, poetry as line-broken stanzas), footnotes, the source URL and the
GNT copyright line. Text is taken verbatim from the page; nothing is retyped.
Passages spanning two chapters: run once per chapter.
"""
import sys
import urllib.request

from bs4 import BeautifulSoup, NavigableString

URL = "https://www.biblesociety.org.uk/explore-the-bible/read/eng/GNB/{book}/{chapter}/"
COPYRIGHT = (
    "*Scripture: Good News Translation® (Today’s English Version, Second Edition) "
    "© 1992 American Bible Society. Anglicisation © The British and Foreign Bible "
    "Society 1976, 1994, 2004. Used for personal, non-commercial reading.*"
)
HEADING_CLASSES = {"s1", "s2", "s3", "ms", "ms1", "mr", "d"}
POETRY_PREFIX = "q"


def parse_verses(spec):
    wanted = []
    for part in spec.split(","):
        start, _, end = part.strip().partition("-")
        wanted.extend(range(int(start), int(end or start) + 1))
    return wanted


def plain_text(tag):
    return " ".join(tag.get_text().split())


def verse_of(tag):
    verse_id = tag.get("data-verse-id") if hasattr(tag, "get") else None
    return int(verse_id.split(".")[2].split("!")[0]) if verse_id else None


def render_paragraph(p, wanted, footnotes):
    """Return (text, verses_seen) for the in-range parts of one <p>."""
    pieces, seen = [], set()

    def walk(node, current):
        for child in node.children:
            if isinstance(child, NavigableString):
                if current in wanted:
                    pieces.append(str(child))
                continue
            classes = child.get("class") or []
            verse = verse_of(child) or current
            if "x" in classes:  # inline cross-reference -- omit
                continue
            if "f" in classes:  # footnote
                if verse in wanted:
                    footnotes.append(child.get_text("").strip())
                    pieces.append(f"<sup>[{chr(96 + len(footnotes))}]</sup>")
                continue
            if "v" in classes:  # verse number
                if verse in wanted:
                    seen.add(verse)
                    pieces.append(f"<sup>{child.get_text()}</sup> ")
                continue
            if verse in wanted:
                seen.add(verse)
            walk(child, verse)

    walk(p, None)
    return " ".join("".join(pieces).split()), seen


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    book, chapter, spec = sys.argv[1].upper(), sys.argv[2], sys.argv[3]
    wanted = parse_verses(spec)
    url = URL.format(book=book, chapter=chapter)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(urllib.request.urlopen(request, timeout=30).read(), "html.parser")

    first_verse = soup.find("span", class_="v")
    if first_verse is None:
        sys.exit(f"No verse markup found at {url} -- check the book code / chapter.")
    container = first_verse.find_parent("p").parent

    blocks, footnotes, pending, last_verse, all_seen = [], [], [], None, set()
    for p in container.find_all("p", recursive=False):
        cls = (p.get("class") or [""])[0]
        if cls in HEADING_CLASSES:
            for note in p.select(".f, .x"):  # e.g. "Hebrew title" footnotes
                note.decompose()
            pending = [f"**{plain_text(p)}**"]
            continue
        if cls == "r":
            pending.append(f"*{plain_text(p)}*")
            continue
        if cls == "b":
            if blocks and blocks[-1] != "":
                blocks.append("")
            continue
        text, seen = render_paragraph(p, wanted, footnotes)
        if not text:
            pending = []  # heading belonged to an out-of-range section
            continue
        all_seen |= seen
        first_seen = min(seen) if seen else last_verse
        if last_verse is not None and first_seen and first_seen > last_verse + 1:
            blocks += ["", "…", ""]  # gap in a split range such as 1-4,12-16
        if pending:
            for line in pending:
                blocks += ["", line, ""]
            pending = []
        if cls.startswith(POETRY_PREFIX):
            indent = "&nbsp;&nbsp;&nbsp;&nbsp;" if cls not in ("q", "q1") else ""
            blocks.append(f"{indent}{text}  ")  # two spaces = Markdown line break
        else:
            blocks += ["", text, ""]
        last_verse = max(seen) if seen else last_verse

    missing = sorted(set(wanted) - all_seen)
    out = "\n".join(blocks)
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    print(out.strip())
    if footnotes:
        print()
        for i, note in enumerate(footnotes):
            print(f"*[{chr(97 + i)}] {note}*  ")
    print(f"\nSource: [{book} {chapter} (GNB) — Bible Society]({url})")
    print(f"\n{COPYRIGHT}")
    if missing:
        print(f"\nWARNING: verses not found on page: {missing}", file=sys.stderr)


if __name__ == "__main__":
    main()
