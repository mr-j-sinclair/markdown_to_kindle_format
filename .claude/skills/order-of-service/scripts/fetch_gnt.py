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
import json
import re
import sys
import urllib.request

URL = "https://www.biblesociety.org.uk/explore-the-bible/read/eng/GNB/{book}/{chapter}/"
# The site's URL slug for each USFM code, as listed in its own book menu.
ROUTE_CODES = {
    "GEN": "Gen", "EXO": "Exod", "LEV": "Lev", "NUM": "Num", "DEU": "Deut",
    "JOS": "Josh", "JDG": "Judg", "RUT": "Ruth", "1SA": "1Sam", "2SA": "2Sam",
    "1KI": "1Kgs", "2KI": "2Kgs", "1CH": "1Chr", "2CH": "2Chr", "EZR": "Ezra",
    "NEH": "Neh", "EST": "Esth", "JOB": "Job", "PSA": "Ps", "PRO": "Prov",
    "ECC": "Eccl", "SNG": "Song", "ISA": "Isa", "JER": "Jer", "LAM": "Lam",
    "EZK": "Ezek", "DAN": "Dan", "HOS": "Hos", "JOL": "Joel", "AMO": "Amos",
    "OBA": "Obad", "JON": "Jonah", "MIC": "Mic", "NAM": "Nah", "HAB": "Hab",
    "ZEP": "Zeph", "HAG": "Hag", "ZEC": "Zech", "MAL": "Mal", "MAT": "Matt",
    "MRK": "Mark", "LUK": "Luke", "JHN": "John", "ACT": "Acts", "ROM": "Rom",
    "1CO": "1Cor", "2CO": "2Cor", "GAL": "Gal", "EPH": "Eph", "PHP": "Phil",
    "COL": "Col", "1TH": "1Thess", "2TH": "2Thess", "1TI": "1Tim", "2TI": "2Tim",
    "TIT": "Titus", "PHM": "Phlm", "HEB": "Heb", "JAS": "James", "1PE": "1Pet",
    "2PE": "2Pet", "1JN": "1John", "2JN": "2John", "3JN": "3John", "JUD": "Jude",
    "REV": "Rev",
}
COPYRIGHT = (
    "*Scripture: Good News Translation® (Today’s English Version, Second Edition) "
    "© 1992 American Bible Society. Anglicisation © The British and Foreign Bible "
    "Society 1976, 1994, 2004. Used for personal, non-commercial reading.*"
)
# The page is a Next.js app: the passage arrives as JSON inside
# self.__next_f.push([1, "<JS string>"]) script chunks.
NEXT_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')


def parse_verses(spec):
    wanted = []
    for part in spec.split(","):
        start, _, end = part.strip().partition("-")
        wanted.extend(range(int(start), int(end or start) + 1))
    return wanted


def load_passage(html):
    """Return the page's structured passage: {"reference", "sections": [...]}."""
    for match in NEXT_CHUNK.finditer(html):
        chunk = json.loads(match.group(1))
        start = chunk.find('"passage":{')
        if start != -1:
            passage, _ = json.JSONDecoder().raw_decode(chunk, start + len('"passage":'))
            return passage
    return None


def render_verses(verses, wanted, footnotes):
    """Return (text, verses_seen) for the in-range verses of one section."""
    pieces, seen = [], set()
    for verse in verses:
        number = int(verse["number"])
        if number not in wanted:
            continue
        seen.add(number)
        # The site's data keeps the space left by small-caps LORD markup ("LORD 's").
        text = re.sub(r"\bLORD (['’]s)\b", r"LORD\1", verse["text"])
        if not verse.get("isContinuation"):
            text = f"<sup>{verse['number']}</sup> {text}"
        for note in verse["notes"]:
            if note["type"] == "footnote":  # cross-references are omitted
                footnotes.append(note["text"])
                text += f"<sup>[{chr(96 + len(footnotes))}]</sup>"
        pieces.append(text)
    return " ".join(" ".join(pieces).split()), seen


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    book, chapter, spec = sys.argv[1].upper(), sys.argv[2], sys.argv[3]
    if book not in ROUTE_CODES:
        sys.exit(f"Unknown book code {book!r}.\n{__doc__}")
    wanted = parse_verses(spec)
    url = URL.format(book=ROUTE_CODES[book], chapter=chapter)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(request, timeout=30).read().decode("utf-8")

    passage = load_passage(html)
    if not passage or not passage.get("sections"):
        sys.exit(f"No passage data found at {url} -- check the book code / chapter, "
                 "or the site's page format has changed.")

    blocks, footnotes, pending, last_verse, all_seen = [], [], [], None, set()
    for section in passage["sections"]:
        kind, style = section["type"], section.get("style", "")
        if kind == "heading":
            pending = [f"**{section['text']}**"]
            continue
        if kind == "text":  # e.g. the "(Num 20.1–13)" parallel-passage line
            pending.append(f"*{section['text']}*")
            continue
        if kind == "spacer":
            if blocks and blocks[-1] != "":
                blocks.append("")
            continue
        text, seen = render_verses(section.get("verses", []), wanted, footnotes)
        if not text:
            pending = []  # heading belonged to an out-of-range section
            continue
        all_seen |= seen
        first_seen = min(seen)
        if last_verse is not None and first_seen > last_verse + 1:
            blocks += ["", "…", ""]  # gap in a split range such as 1-4,12-16
        if pending:
            for line in pending:
                blocks += ["", line, ""]
            pending = []
        if kind == "poetry":
            indent = "&nbsp;&nbsp;&nbsp;&nbsp;" if style not in ("q", "q1") else ""
            blocks.append(f"{indent}{text}  ")  # two spaces = Markdown line break
        else:
            blocks += ["", text, ""]
        last_verse = max(seen)

    missing = sorted(set(wanted) - all_seen)
    out = "\n".join(blocks)
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    print(out.strip())
    if footnotes:
        print()
        for i, note in enumerate(footnotes):
            print(f"*[{chr(97 + i)}] {note}*  ")
    print(f"\nSource: [{passage.get('reference', f'{book} {chapter}')} (GNB) — Bible Society]({url})")
    print(f"\n{COPYRIGHT}")
    if missing:
        print(f"\nWARNING: verses not found on page: {missing}", file=sys.stderr)


if __name__ == "__main__":
    main()
