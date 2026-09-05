#!/usr/bin/env python3
"""Convert Markdown / HTML / plain text / PDF into a Kindle-ready EPUB, or
into a paginated PDF.

Usage:
    python3 md_to_kindle.py INPUT[.md|.html|.htm|.txt|.pdf] [OUTPUT[.epub|.pdf]] \
        [--title "Doc Title"] [--author "Name"] [--subtitle "Subtitle"] \
        [--output-format {epub,pdf}]

If OUTPUT is omitted, it is written into the outputs/ folder (alongside this
script), using the input's basename, with a .epub extension by default (pass
--output-format pdf to default to .pdf instead). If OUTPUT is given, its own
extension (.epub/.pdf) always decides the output format.

EPUB output is a plain, reflowable document meant to be handed to the "Send
to Kindle" desktop app, which converts it to native Kindle format
server-side. PDF output is a fixed-layout, paginated document with running
page-number ("Page X of Y") and title footers/headers -- see the "PDF
export" section of README.md for what's and isn't supported there (no
syntax-highlighted code blocks yet, no SVG rasterization).

Fonts and layout are kept deliberately plain/conservative for e-ink
legibility: LaTeX math (`$$...$$`, `\\[...\\]`, `\\(...\\)`, `$...$`) is
rendered to cropped images unless it's simple enough (e.g. `x^2`, `H_2O`) to
render as plain <sup>/<sub> text; fenced code blocks get syntax highlighting
in a shaded, wrapped box (EPUB output only -- see above).
"""
import argparse
import datetime
import gzip
import html
import io
import os
import re
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

import markdown
from bs4 import BeautifulSoup, NavigableString, Tag
from PIL import Image

import matplotlib
matplotlib.use("Agg")
from matplotlib import mathtext
from matplotlib.figure import Figure
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Circle
from matplotlib.backends.backend_agg import FigureCanvasAgg

import fitz  # PyMuPDF

from pygments import highlight
from pygments.lexers import get_lexer_by_name, TextLexer
from pygments.formatters import HtmlFormatter
from pygments.util import ClassNotFound

from ebooklib import epub

import kindle_delivery

from reportlab.lib.pagesizes import LETTER as _PDF_LETTER, A4 as _PDF_A4
from reportlab.lib.units import inch as _pdf_inch
from reportlab.lib import colors as _pdf_colors
from reportlab.lib.styles import getSampleStyleSheet as _pdf_get_stylesheet, ParagraphStyle as _PdfParagraphStyle
from reportlab.lib.enums import TA_LEFT as _PDF_TA_LEFT, TA_CENTER as _PDF_TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate as _PdfSimpleDocTemplate,
    Paragraph as _PdfParagraph,
    Spacer as _PdfSpacer,
    Table as _PdfTable,
    TableStyle as _PdfTableStyle,
    Preformatted as _PdfPreformatted,
    HRFlowable as _PdfHRFlowable,
    Image as _PdfImage,
)
from reportlab.pdfbase import pdfmetrics as _pdf_metrics
from reportlab.pdfbase.ttfonts import TTFont as _PdfTTFont
from reportlab.pdfgen import canvas as _pdf_canvas_mod

# PROTOTYPE dependency for Mermaid-diagram image rendering -- see the
# "Mermaid diagram image rendering" section below for what depends on this
# and exactly how to remove it. Import is soft: if the `graphviz` package
# isn't installed, the feature just silently falls back to the old
# plain-text rendering of ```mermaid fences, so this never breaks a normal
# conversion.
try:
    import graphviz as _graphviz
except ImportError:
    _graphviz = None

# ---------- Default input/output folders ----------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(SCRIPT_DIR, "inputs")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "outputs")


def read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def escape_x(text: str) -> str:
    return html.escape(text, quote=False)


# =====================================================================
# Math handling: detect LaTeX delimiters, decide sup/sub vs. image, and
# render images via matplotlib's built-in mathtext engine (no system LaTeX
# install required/available).
# =====================================================================

DISPLAY_DOLLAR_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
DISPLAY_BRACKET_RE = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)
# ChatGPT-style copy/paste often flattens display math into a lone "["
# line ... content ... lone "]" line, instead of real \[...\] delimiters
# (an artifact of the source page's rendered-math DOM not round-tripping
# through plain-text copy). This is ambiguous with other bracket usage in
# general Markdown, so it's gated by looks_like_math() below, unlike the
# unambiguous $$/\[...\] delimiters above.
BARE_BRACKET_BLOCK_RE = re.compile(r"^\[[ \t]*\n(.*?)\n\][ \t]*$", re.DOTALL | re.MULTILINE)
# Another copy/paste artifact: a rendered fraction bar or setext-heading
# underline flattens to a lone run of "=" characters on its own line. It's
# not meaningful LaTeX on its own, but it's standing in for a "=" relation
# (e.g. "cosine(A,B)\n===========\n\nfrac{...}" means "cosine(A,B) = frac{...}"),
# so it's collapsed to a single "=" rather than dropped, to preserve meaning.
_PSEUDO_UNDERLINE_RE = re.compile(r"^[ \t]*=+[ \t]*$", re.MULTILINE)
INLINE_PAREN_RE = re.compile(r"\\\((.+?)\\\)", re.DOTALL)
# Conservative: no whitespace touching either delimiter, not preceded by
# another '$' (avoids re-matching inside $$...$$), not followed by a digit.
INLINE_DOLLAR_RE = re.compile(r"(?<!\$)\$(?!\s)([^$\n]+?)(?<!\s)\$(?!\d)")

CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

PLACEHOLDER_OPEN = ""
PLACEHOLDER_CLOSE = ""
PLACEHOLDER_RE = re.compile(re.escape(PLACEHOLDER_OPEN) + r"MATH(\d+)" + re.escape(PLACEHOLDER_CLOSE))

_SUBSUP_TOKEN_RE = re.compile(
    r"([A-Za-z0-9]{1,6})([\^_])(?:\{([A-Za-z0-9]{1,3})\}|([A-Za-z0-9]))"
)


def _mask_code_ranges(text: str):
    """Character ranges covered by fenced/inline code spans, so math
    detection never fires on LaTeX-looking text that's actually code."""
    ranges = []
    for pat in (CODE_FENCE_RE, INLINE_CODE_RE):
        for m in pat.finditer(text):
            ranges.append((m.start(), m.end()))
    return ranges


def looks_like_math(content: str) -> bool:
    """Gate for bare $...$ spans: reject plain currency amounts, require
    some actual math signal (a LaTeX command, ^/_/{}, or a relation)."""
    s = content.strip()
    if not s:
        return False
    if re.fullmatch(r"[\d,]+(\.\d+)?", s):
        return False
    if "\\" in s:
        return True
    if any(c in s for c in "^_{}|"):
        return True
    if re.search(r"[=<>]", s):
        return True
    return False


def _clean_bracket_math_content(content: str) -> str:
    return _PSEUDO_UNDERLINE_RE.sub("=", content)


def find_math_spans(text: str):
    """Return non-overlapping (start, end, latex, display) spans in
    precedence order: $$/\\[ (display), \\( (inline), then bare $ (inline)."""
    code_ranges = _mask_code_ranges(text)

    def in_code(pos):
        return any(s <= pos < e for s, e in code_ranges)

    spans = []
    search = text

    def blank(s, start, end):
        return s[:start] + ("￾" * (end - start)) + s[end:]

    for pattern in (DISPLAY_DOLLAR_RE, DISPLAY_BRACKET_RE):
        for m in list(pattern.finditer(search)):
            if in_code(m.start()):
                continue
            spans.append((m.start(), m.end(), text[m.start(1):m.end(1)], True))
            search = blank(search, m.start(), m.end())

    for m in list(BARE_BRACKET_BLOCK_RE.finditer(search)):
        if in_code(m.start()):
            continue
        content = _clean_bracket_math_content(text[m.start(1):m.end(1)])
        if looks_like_math(content):
            spans.append((m.start(), m.end(), content, True))
            search = blank(search, m.start(), m.end())

    for m in list(INLINE_PAREN_RE.finditer(search)):
        if in_code(m.start()):
            continue
        spans.append((m.start(), m.end(), text[m.start(1):m.end(1)], False))
        search = blank(search, m.start(), m.end())

    for m in list(INLINE_DOLLAR_RE.finditer(search)):
        if in_code(m.start()):
            continue
        content = text[m.start(1):m.end(1)]
        if looks_like_math(content):
            spans.append((m.start(), m.end(), content, False))
            search = blank(search, m.start(), m.end())

    spans.sort(key=lambda s: s[0])
    return spans


def protect_math_placeholders(text: str):
    """Replace math spans with opaque Private-Use-Area placeholder tokens
    BEFORE markdown parsing, so '_'/'*' inside math isn't read as emphasis."""
    spans = find_math_spans(text)
    if not spans:
        return text, {}
    placeholder_map = {}
    out = []
    last = 0
    for i, (start, end, latex, display) in enumerate(spans):
        token = f"{PLACEHOLDER_OPEN}MATH{i}{PLACEHOLDER_CLOSE}"
        out.append(text[last:start])
        out.append(token)
        placeholder_map[token] = (latex, display)
        last = end
    out.append(text[last:])
    return "".join(out), placeholder_map


def _subsup_sub(m):
    base, marker = m.group(1), m.group(2)
    # Braced form (x^{10}) can take up to 3 chars; bare form (x^2) binds to
    # exactly one following character, per standard LaTeX convention --
    # otherwise "H_2O" would wrongly swallow the "O" into the subscript.
    arg = m.group(3) if m.group(3) is not None else m.group(4)
    tag = "sup" if marker == "^" else "sub"
    return f"{escape_x(base)}<{tag}>{escape_x(arg)}</{tag}>"


def render_simple_or_none(latex: str):
    """If latex is simple enough (single-token ^/_ cases like x^2, H_2O,
    x_i, no backslash macros), return ready-to-insert <sup>/<sub> HTML.
    Otherwise return None so the caller renders an image instead. All or
    nothing per span -- a mixed/complex span never gets partial output."""
    if "\\" in latex:
        return None
    if latex.count("^") + latex.count("_") == 0:
        return None
    replaced, n = _SUBSUP_TOKEN_RE.subn(_subsup_sub, latex)
    if n == 0:
        return None
    leftover = _SUBSUP_TOKEN_RE.sub("", latex)
    if any(c in leftover for c in "^_\\{}"):
        return None
    return replaced


_MATH_DPI = 440
_math_ref_height_px = None


def _reference_math_height_px() -> int:
    """Pixel height of a one-line reference expression (with both an
    ascender and a descender) at _MATH_DPI, cached after first use. Every
    equation image's height is expressed as a multiple of this, in em, so
    glyph size stays visually consistent with body text regardless of how
    long/wide or how many text-lines tall a given equation is -- instead of
    the old approach of scaling each image to fill a fixed % of the page
    width, which made short equations balloon and long ones shrink."""
    global _math_ref_height_px
    if _math_ref_height_px is None:
        buf = io.BytesIO()
        mathtext.math_to_image("$Ag$", buf, dpi=_MATH_DPI, format="png", color="black")
        with Image.open(buf) as img:
            _math_ref_height_px = img.height
    return _math_ref_height_px


def math_image_height_em(png_bytes: bytes, display: bool) -> float:
    with Image.open(io.BytesIO(png_bytes)) as img:
        px = img.height
    em = px / _reference_math_height_px()
    em = max(1.0, round(em, 2))
    if not display:
        # Inline math sits within a line of prose -- cap how much it can
        # inflate the line height even if the expression itself is tall.
        em = min(em, 1.3)
    else:
        em = min(em, 6.0)
    return em


_BOXED_PREFIX = "\\boxed{"


def strip_boxed(latex: str):
    """mathtext doesn't support \\boxed{...} (a common "final answer"
    wrapper in GPT-style solutions) and would otherwise fail and fall back
    to a plain text box, losing the actual equation rendering. If latex is
    (after stripping whitespace) a single \\boxed{...} wrapper around the
    whole expression, return (inner_content, True) so the caller can render
    the inner math normally and draw the box itself via CSS instead.
    Otherwise return (latex, False) unchanged."""
    s = latex.strip()
    if not s.startswith(_BOXED_PREFIX) or not s.endswith("}"):
        return latex, False
    depth = 0
    open_brace = len(_BOXED_PREFIX) - 1
    for i in range(open_brace, len(s)):
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                # Only treat it as a pure wrapper if the closing brace is
                # the very last character -- "\boxed{x} and y" shouldn't
                # get boxed styling applied to the whole image.
                return (s[open_brace + 1:i], True) if i == len(s) - 1 else (latex, False)
    return latex, False


def render_math_image(latex: str):
    """Render LaTeX to a cropped PNG via matplotlib mathtext. Returns None
    on failure (unsupported macro/environment) so the caller can fall back
    to a shaded raw-LaTeX text box instead of crashing."""
    try:
        buf = io.BytesIO()
        mathtext.math_to_image(f"${latex}$", buf, dpi=_MATH_DPI, format="png", color="black")
        return buf.getvalue()
    except Exception:
        return None


# media_type -> file extension overrides for the cases where a naive
# "everything after the slash" split produces the wrong (or an invalid)
# extension -- e.g. "image/svg+xml" would otherwise become ".svg+xml".
_MEDIA_TYPE_EXT_OVERRIDES = {"image/svg+xml": "svg", "image/jpeg": "jpg"}


class ImageRegistry:
    def __init__(self):
        self._n = 0
        self.images = []  # list of (rel_path, content_bytes, media_type)

    def register(self, content: bytes, media_type: str = "image/png", prefix: str = "eq") -> str:
        self._n += 1
        ext = _MEDIA_TYPE_EXT_OVERRIDES.get(media_type)
        if ext is None:
            ext = media_type.split("/", 1)[1] if "/" in media_type else "png"
        rel_path = f"images/{prefix}_{self._n:04d}.{ext}"
        self.images.append((rel_path, content, media_type))
        return rel_path


def math_replacement_nodes(soup, latex, display, image_registry):
    """Build the bs4 node(s) that should replace a math placeholder token."""
    latex = re.sub(r"\s+", " ", latex.strip())
    if not display:
        simple = render_simple_or_none(latex)
        if simple is not None:
            frag = BeautifulSoup(simple, "html.parser")
            return list(frag.contents)

    render_latex, is_boxed = strip_boxed(latex)

    png = render_math_image(render_latex)
    if png is None:
        # <code>, not <pre>: the placeholder may land inside a <p>, and
        # <pre> is not valid paragraph content (block-in-inline nesting
        # gets silently corrected/split by the XHTML serializer downstream).
        fallback = soup.new_tag("code")
        fallback["class"] = ["math-fallback"]
        fallback.string = latex
        return [fallback]

    rel_path = image_registry.register(png)
    img = soup.new_tag("img")
    classes = ["eqimg" if display else "eqimg-inline"]
    if is_boxed:
        classes.append("eqimg-boxed")
    img["class"] = classes
    img["src"] = rel_path
    img["alt"] = f"[equation: {latex[:60]}]"
    height_em = math_image_height_em(png, display)
    img["style"] = f"height:{height_em}em;width:auto;"
    return [img]


def resolve_math_placeholders(soup, placeholder_map, image_registry):
    if not placeholder_map:
        return
    for node in list(soup.find_all(string=PLACEHOLDER_RE)):
        text = str(node)
        pieces = []
        last = 0
        for m in PLACEHOLDER_RE.finditer(text):
            if m.start() > last:
                pieces.append(NavigableString(text[last:m.start()]))
            token = m.group(0)
            latex, display = placeholder_map[token]
            pieces.extend(math_replacement_nodes(soup, latex, display, image_registry))
            last = m.end()
        if last < len(text):
            pieces.append(NavigableString(text[last:]))
        for piece in pieces:
            node.insert_before(piece)
        node.extract()


# =====================================================================
# Q&A turn grouping: a lone "Question"/"Answer" marker (from source like
# "**Question:**" on its own line, or a "## Prompt:"/"## Response:" heading
# -- the pattern several GPT-export tools produce) starts a labeled, styled
# turn running through the following content up to the next marker, an H1,
# or end of document -- so a multi-round pasted GPT transcript reads as
# clearly separated question/answer blocks instead of one undifferentiated
# wall of text. "Prompt"/"Response" headings are folded into the same
# question/answer styling rather than kept as a separate label, so a
# transcript can mix either convention and still render consistently.
# Purely additive: documents that never use these markers are completely
# unaffected.
# =====================================================================

_QA_MARKER_RE = re.compile(r"^(question|answer|prompt|response)\s*:?\s*$", re.IGNORECASE)
_QA_MARKER_KIND = {"question": "question", "prompt": "question", "answer": "answer", "response": "answer"}
_QA_MARKER_TAGS = {"p", "h2", "h3", "h4", "h5", "h6"}


def qa_marker_kind(tag):
    if not (isinstance(tag, Tag) and tag.name in _QA_MARKER_TAGS):
        return None
    m = _QA_MARKER_RE.match(tag.get_text(strip=True))
    return _QA_MARKER_KIND[m.group(1).lower()] if m else None


def wrap_qa_turns(soup):
    q_round = 0
    node = next((el for el in soup.find_all(recursive=False) if isinstance(el, Tag)), None)
    while node is not None:
        next_node = node.find_next_sibling(True)
        kind = qa_marker_kind(node)
        if kind is not None:
            if kind == "question":
                q_round += 1
            label = f"{kind.capitalize()} {q_round}" if q_round else kind.capitalize()

            wrapper = soup.new_tag("div")
            wrapper["class"] = ["qa-turn", f"qa-{kind}"]
            caption = soup.new_tag("p")
            caption["class"] = ["qa-label"]
            caption.string = label
            node.insert_before(wrapper)
            wrapper.append(caption)

            member = next_node
            while member is not None and member.name != "h1" and qa_marker_kind(member) is None:
                following = member.find_next_sibling(True)
                wrapper.append(member.extract())
                member = following
            node.extract()
            next_node = member  # resume scanning from the H1/next marker that stopped the group
        node = next_node


# =====================================================================
# Content images: embed local image files referenced via markdown
# ![alt](path)/HTML <img src="path"> so they actually show up in the EPUB
# instead of a broken image reference. Remote (http/https) sources are
# handled separately below by resolve_remote_images(). data: URIs are left
# untouched in both paths -- they're already inline, nothing to fetch.
# =====================================================================

IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    # SVG isn't a Pillow-decodable raster format, so it's handled separately
    # from PIL_FORMAT_MEDIA_TYPES below (no pixel-decode validation, just a
    # lightweight "does this actually look like an <svg> document" sniff) --
    # but it's a real, EPUB-legal image type and common for diagrams/icons
    # on web pages, so it's worth carrying all the way through rather than
    # silently failing to embed.
    ".svg": "image/svg+xml",
}

# Maps Pillow's Image.format string (set at Image.open() time, unaffected by
# a later .verify() call) to the media type used when registering a
# successfully-fetched remote image -- the authoritative check in
# resolve_remote_images(), after the fast Content-Type header pre-check.
PIL_FORMAT_MEDIA_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
}


def resolve_local_images(soup, base_dir, image_registry):
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src or src.startswith(("http://", "https://", "data:")):
            continue
        cls = img.get("class") or []
        if "eqimg" in cls or "eqimg-inline" in cls:
            continue  # already embedded by the math renderer
        ext = os.path.splitext(src)[1].lower()
        media_type = IMAGE_MEDIA_TYPES.get(ext)
        if media_type is None:
            continue
        path = src if os.path.isabs(src) else os.path.join(base_dir, src)
        if not os.path.isfile(path):
            print(f"warning: image not found, leaving unembedded: {src}", file=sys.stderr)
            continue
        with open(path, "rb") as f:
            content = f.read()
        rel_path = image_registry.register(content, media_type=media_type, prefix="img")
        img["src"] = rel_path
        img["class"] = cls + ["content-img"]


REMOTE_IMAGE_USER_AGENT = "Mozilla/5.0 (compatible; md_to_kindle/1.0)"
REMOTE_IMAGE_MAX_BYTES = 20 * 1024 * 1024  # 20MB cap on a single fetched image
# Bounds each individual socket operation (connect/read), not the total time
# to pull the whole (size-capped) body -- a deliberate trade-off, not an
# oversight.
REMOTE_IMAGE_TIMEOUT_S = 10


def resolve_remote_images(soup, image_registry):
    """Fetch http(s) <img> sources and embed them exactly like local images
    (same ImageRegistry mechanism as resolve_local_images). On any failure
    (network error, timeout, non-image content, oversized body, corrupt
    image data, ...) the <img> is replaced with a small inline placeholder
    noting an image existed there, instead of silently dropping it."""
    for img in list(soup.find_all("img")):
        src = img.get("src", "")
        if not src.startswith(("http://", "https://")):
            continue
        cls = img.get("class") or []
        if "eqimg" in cls or "eqimg-inline" in cls:
            continue  # already embedded by the math renderer

        reason = None
        content = None
        media_type = None
        # SVG isn't Pillow-decodable, so a server that reports a generic/
        # wrong Content-Type for it (some do -- text/plain, octet-stream)
        # would otherwise be misclassified as "not an image" before the
        # body is even looked at. Extension is only a hint; the actual
        # gate is the content sniff below once the body is in hand.
        looks_like_svg_url = src.split("?", 1)[0].lower().endswith(".svg")
        try:
            req = urllib.request.Request(src, headers={"User-Agent": REMOTE_IMAGE_USER_AGENT})
            with urllib.request.urlopen(req, timeout=REMOTE_IMAGE_TIMEOUT_S) as resp:
                content_type = resp.headers.get("Content-Type", "")
                ctype = content_type.split(";", 1)[0].strip().lower()
                is_svg_ctype = ctype in ("image/svg+xml", "text/xml", "application/xml")
                if ctype and not ctype.startswith("image/") and not (looks_like_svg_url and is_svg_ctype):
                    reason = "not an image"
                else:
                    data = resp.read(REMOTE_IMAGE_MAX_BYTES + 1)
                    if len(data) > REMOTE_IMAGE_MAX_BYTES:
                        reason = "too large"
                    elif ctype == "image/svg+xml" or (looks_like_svg_url and is_svg_ctype):
                        if b"<svg" in data[:4096].lower():
                            media_type = "image/svg+xml"
                            content = data
                        else:
                            reason = "not an image"
                    else:
                        try:
                            pil_img = Image.open(io.BytesIO(data))
                            fmt = pil_img.format
                            pil_img.verify()
                        except Exception:
                            reason = "corrupt image data"
                        else:
                            media_type = PIL_FORMAT_MEDIA_TYPES.get(fmt)
                            if media_type is None:
                                reason = "not an image"
                            else:
                                content = data
        except urllib.error.HTTPError as e:
            reason = f"HTTP {e.code}"
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                reason = "timeout"
            else:
                reason = "connection error"
        except TimeoutError:
            reason = "timeout"
        except Exception:
            reason = "fetch error"

        if content is not None:
            rel_path = image_registry.register(content, media_type=media_type, prefix="img")
            img["src"] = rel_path
            img["class"] = cls + ["content-img"]
        else:
            print(f"warning: image fetch failed ({reason}), inserting placeholder: {src}", file=sys.stderr)
            label = img.get("alt", "").strip() or src
            fallback = soup.new_tag("code")
            fallback["class"] = ["img-fallback"]
            fallback.string = f"[Image could not be loaded: {label[:60]}]"
            img.replace_with(fallback)


# =====================================================================
# PROTOTYPE: Mermaid diagram image rendering.
#
# The established workflow for this tool has been to hand-convert
# ```mermaid fences into a plain ASCII diagram before conversion (see
# README), because rendering real Mermaid requires mermaid.js via a
# headless browser -- a heavy dependency this pure-Python tool has
# deliberately avoided. This section is a lighter-weight alternative:
# it hand-parses a *subset* of Mermaid syntax and renders it locally with
# tools already native to this codebase, in the same spirit as using
# Pygments for code and matplotlib for math -- no JS/browser involved:
#   - `flowchart`/`graph` diagrams render via Graphviz (the `dot` binary
#     + the `graphviz` pip package), which has a native concept of node/
#     edge graph layout.
#   - `stateDiagram`/`stateDiagram-v2` also renders via Graphviz -- a state
#     machine is just a directed graph too, with `[*]` start/end
#     pseudostates drawn as small filled circles instead of boxes.
#   - `sequenceDiagram` renders via a small purpose-built matplotlib
#     renderer (participant boxes, lifelines, message arrows) --
#     Graphviz has no native concept of a sequence diagram's lifelines,
#     so this doesn't try to force it into that shape; matplotlib is
#     already a hard dependency of this tool (used for math images), so
#     this adds no new dependency.
#   - `gitGraph` also renders via a small purpose-built matplotlib
#     renderer (one horizontal lane per branch, commits as dots in
#     chronological order, diagonal connectors for `branch`/`merge`) --
#     same reasoning as `sequenceDiagram`: no new dependency, and forcing
#     it through Graphviz's generic graph layout would lose the
#     lane-per-branch/chronological-x-axis shape that makes a git graph
#     readable.
# Any other diagram type (`classDiagram`, `erDiagram`, ...) or any parse
# failure falls straight back to the tool's original behaviour: the raw
# fence text shown as a plain shaded code block.
#
# Layout direction (LR/TD/RL/BT) is kept faithful to whatever the source
# diagram declares -- the reader also uses their Kindle in landscape, so a
# wide diagram isn't automatically a worse fit than a tall one, and forcing
# everything to portrait would misrepresent diagrams the author explicitly
# laid out horizontally.
#
# Since this renders a real (albeit colour, unlike ASCII) diagram, a
# ```mermaid fence that parses successfully doesn't need an accompanying
# hand-written ASCII version any more -- keep the ASCII fallback only for
# diagram types/syntax this parser doesn't understand (e.g.
# `sequenceDiagram`), where it's the only readable rendering available.
#
# ---- ROLLBACK -----------------------------------------------------
# To fully remove this feature:
#   1. Delete this whole section (down to the next banner comment).
#   2. Delete the `resolve_mermaid_diagrams(...)` call in `convert()`.
#   3. Delete the `--mermaid-images` CLI option in `main()` (and its
#      `mermaid_images=` pass-through into `convert()`).
#   4. Remove the `graphviz` import block near the top of the file.
#   5. Remove `graphviz` from requirements.txt.
#   6. Optionally `brew uninstall graphviz` (the system `dot` binary --
#      only if nothing else on the machine uses it).
# To just switch it off without editing code: run with
# `--mermaid-images off` (default is "on").
# =====================================================================

_MERMAID_NODE_ID = r"[A-Za-z_][\w-]*"
_MERMAID_SHAPE = r"(?:\[[^\[\]]*\]|\{[^{}]*\}|\(\([^()]*\)\)|\([^()]*\))"
_MERMAID_HEADER_RE = re.compile(r"^(flowchart|graph)\s+(TB|TD|BT|RL|LR)\b", re.IGNORECASE)
_MERMAID_NODE_RE = re.compile(rf"^({_MERMAID_NODE_ID})\s*({_MERMAID_SHAPE})?$")
_MERMAID_EDGE_RE = re.compile(
    rf"^({_MERMAID_NODE_ID})\s*({_MERMAID_SHAPE})?\s*"
    rf"(<-->|<==>|-\.->|-->|==>)\s*"
    rf"(?:\|([^|]*)\|\s*)?"
    rf"({_MERMAID_NODE_ID})\s*({_MERMAID_SHAPE})?\s*$"
)
_MERMAID_DOTTED_LABEL_EDGE_RE = re.compile(
    rf"^({_MERMAID_NODE_ID})\s*({_MERMAID_SHAPE})?\s*"
    rf"-\.\s*([^.]*?)\s*\.->\s*"
    rf"({_MERMAID_NODE_ID})\s*({_MERMAID_SHAPE})?\s*$"
)
# Directives this parser deliberately doesn't understand but shouldn't choke
# on either -- lines starting with these are silently skipped rather than
# aborting the whole diagram's parse.
_MERMAID_DIRECTIVE_PREFIXES = ("subgraph", "end", "classdef", "class ", "style ", "click ", "%%", "linkstyle")

# Colours chosen to match Mermaid's own "default" theme (the one GitHub
# renders ```mermaid fences with): light lavender fill, purple stroke,
# dark grey text/edges.
_MERMAID_FILL = "#ECECFF"
_MERMAID_STROKE = "#9370DB"
_MERMAID_TEXT = "#333333"
_MERMAID_EDGE_COLOR = "#333333"
_MERMAID_DPI = "150"

_MERMAID_SHAPE_STYLE = {
    "rect": ("box", "filled,rounded"),
    "rounded": ("box", "filled,rounded"),
    "diamond": ("diamond", "filled"),
    "circle": ("circle", "filled"),
    None: ("box", "filled,rounded"),
}


def _mermaid_shape_kind_and_label(token):
    """Split a Mermaid node token like '[Some label]' or '{Decision?}' into
    (shape_kind, raw_label_text). Returns (None, token) if it doesn't match
    a known shape delimiter pair."""
    if token is None:
        return None, None
    if token.startswith("((") and token.endswith("))"):
        return "circle", token[2:-2]
    if token.startswith("{") and token.endswith("}"):
        return "diamond", token[1:-1]
    if token.startswith("(") and token.endswith(")"):
        return "rounded", token[1:-1]
    if token.startswith("[") and token.endswith("]"):
        return "rect", token[1:-1]
    return None, token


def _mermaid_clean_label(text):
    if text is None:
        return None
    text = text.strip().strip('"')
    # Graphviz's own "\n" line-break escape, not a literal newline -- dot
    # only honours it as the two source characters backslash+n inside a
    # quoted label.
    text = re.sub(r"<br\s*/?>", "\\n", text, flags=re.IGNORECASE)
    return text


def parse_mermaid_flowchart(source: str):
    """Parse a minimal subset of Mermaid `flowchart`/`graph` syntax into
    (direction, nodes, edges). Returns None for anything this parser
    doesn't recognise as a flowchart at all (e.g. `sequenceDiagram`, or a
    fence whose first non-blank line isn't a flowchart/graph header) --
    callers fall back to the plain-text code block in that case. Lines
    within a recognised flowchart that this subset doesn't understand are
    skipped individually rather than aborting the whole parse, so a
    document using a directive this parser doesn't model (`classDef`,
    `style`, ...) still renders everything else."""
    direction = "TB"
    nodes = {}  # node_id -> {"label": str|None, "shape": str|None}
    edges = []  # (src_id, dst_id, label|None, "solid"|"dashed")
    saw_header = False

    def ensure_node(node_id, shape_token):
        info = nodes.setdefault(node_id, {"label": None, "shape": None})
        if shape_token:
            kind, label = _mermaid_shape_kind_and_label(shape_token)
            info["shape"] = kind
            info["label"] = _mermaid_clean_label(label)

    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not saw_header:
            m = _MERMAID_HEADER_RE.match(line)
            if not m:
                return None  # not a flowchart/graph diagram at all
            saw_header = True
            dir_token = m.group(2).upper()
            direction = "TB" if dir_token == "TD" else dir_token
            continue
        if line.lower().startswith(_MERMAID_DIRECTIVE_PREFIXES):
            continue

        m = _MERMAID_DOTTED_LABEL_EDGE_RE.match(line)
        if m:
            src_id, src_shape, label, dst_id, dst_shape = m.groups()
            ensure_node(src_id, src_shape)
            ensure_node(dst_id, dst_shape)
            edges.append((src_id, dst_id, (label or "").strip() or None, "dashed", False))
            continue

        m = _MERMAID_EDGE_RE.match(line)
        if m:
            src_id, src_shape, arrow, label, dst_id, dst_shape = m.groups()
            ensure_node(src_id, src_shape)
            ensure_node(dst_id, dst_shape)
            style = "dashed" if arrow == "-.->" else "solid"
            bidirectional = arrow in ("<-->", "<==>")
            edges.append((src_id, dst_id, (label or "").strip() or None, style, bidirectional))
            continue

        m = _MERMAID_NODE_RE.match(line)
        if m:
            node_id, shape = m.groups()
            ensure_node(node_id, shape)
            continue
        # Unrecognised line (e.g. a directive this parser doesn't model) --
        # skip it rather than failing the whole diagram.

    if not saw_header or not nodes:
        return None
    return direction, nodes, edges


def render_mermaid_flowchart_image(source: str):
    """Render a Mermaid flowchart to a colour PNG via Graphviz. Returns
    None if graphviz isn't installed, the source isn't a flowchart this
    parser understands, or rendering fails for any reason -- callers fall
    back to the existing plain shaded code block rather than crashing."""
    if _graphviz is None:
        return None
    parsed = parse_mermaid_flowchart(source)
    if parsed is None:
        return None
    direction, nodes, edges = parsed
    if not nodes:
        return None

    dot = _graphviz.Digraph(format="png")
    # rankdir mirrors the source's own declared direction -- see the module
    # banner above for why this isn't forced to portrait.
    dot.attr(rankdir=direction, bgcolor="white", nodesep="0.35", ranksep="0.45", dpi=_MERMAID_DPI)
    dot.attr("node", fontname="Helvetica", fontsize="14", fontcolor=_MERMAID_TEXT,
              fillcolor=_MERMAID_FILL, color=_MERMAID_STROKE, penwidth="1.4")
    dot.attr("edge", fontname="Helvetica", fontsize="12", fontcolor=_MERMAID_TEXT,
              color=_MERMAID_EDGE_COLOR, penwidth="1.2", arrowsize="0.8")

    for node_id, info in nodes.items():
        label = info["label"] or node_id
        shape, style = _MERMAID_SHAPE_STYLE.get(info["shape"], _MERMAID_SHAPE_STYLE[None])
        dot.node(node_id, label=label, shape=shape, style=style)

    for src, dst, label, style, bidirectional in edges:
        edge_kwargs = {}
        if style == "dashed":
            edge_kwargs["style"] = "dashed"
        if label:
            edge_kwargs["label"] = label
        if bidirectional:
            edge_kwargs["dir"] = "both"
        dot.edge(src, dst, **edge_kwargs)

    try:
        return dot.pipe()
    except Exception:
        return None


# ---- stateDiagram / stateDiagram-v2 support ----------------------------

_MERMAID_STATE_HEADER_RE = re.compile(r"^stateDiagram(?:-v2)?\b", re.IGNORECASE)
_MERMAID_STATE_DIRECTION_RE = re.compile(r"^direction\s+(TB|TD|BT|RL|LR)\b", re.IGNORECASE)
_MERMAID_STATE_ALIAS_RE = re.compile(r'^state\s+"([^"]+)"\s+as\s+(\w+)\s*$', re.IGNORECASE)
_MERMAID_STATE_ID = r"(?:\[\*\]|[A-Za-z_]\w*)"
_MERMAID_STATE_TRANSITION_RE = re.compile(
    rf"^({_MERMAID_STATE_ID})\s*-->\s*({_MERMAID_STATE_ID})\s*(?::\s*(.+))?$"
)
# Constructs this parser doesn't model (notes, composite/nested states,
# classDef/class/click styling, ...) -- skipped rather than aborting the
# whole diagram's parse, same as this module's other Mermaid parsers.
_MERMAID_STATE_SKIP_PREFIXES = ("note ", "note left", "note right", "classdef",
                                 "class ", "click ", "%%")


def parse_mermaid_state(source: str):
    """Parse a minimal subset of Mermaid `stateDiagram`/`stateDiagram-v2`
    syntax into (direction, nodes, edges). `nodes` maps node_id -> display
    label, where a label of `None` means "use the id as the label" and the
    sentinel `"__pseudo__"` marks a `[*]` start/end state (rendered as a
    small filled circle instead of a box). Every `[*]` occurrence gets its
    own synthetic node id rather than sharing one -- in real Mermaid each
    `[*]` is its own anonymous pseudostate, not a shared node, so treating
    them as one would wrongly merge unrelated start/end points into a
    single node with a confusing tangle of edges. Composite/nested states
    (`State {` ... `}`) aren't modelled -- their lines are skipped
    individually rather than aborting the whole parse. Returns None for
    anything that isn't a stateDiagram at all, or if nothing usable was
    found."""
    direction = "TB"
    nodes = {}
    edges = []
    aliases = {}
    saw_header = False
    pseudo_count = 0

    def ensure_node(token):
        nonlocal pseudo_count
        if token == "[*]":
            synthetic = f"__pseudo_{pseudo_count}__"
            pseudo_count += 1
            nodes[synthetic] = "__pseudo__"
            return synthetic
        nodes.setdefault(token, aliases.get(token))
        return token

    for raw_line in source.splitlines():
        line = raw_line.strip().rstrip(";")
        if not line:
            continue
        if not saw_header:
            if not _MERMAID_STATE_HEADER_RE.match(line):
                return None  # not a stateDiagram at all
            saw_header = True
            continue
        m = _MERMAID_STATE_DIRECTION_RE.match(line)
        if m:
            dir_token = m.group(1).upper()
            direction = "TB" if dir_token == "TD" else dir_token
            continue
        m = _MERMAID_STATE_ALIAS_RE.match(line)
        if m:
            label, node_id = m.groups()
            aliases[node_id] = label
            nodes[node_id] = label
            continue
        if line.lower().startswith(_MERMAID_STATE_SKIP_PREFIXES):
            continue
        if line in ("{", "}") or line.endswith("{"):
            continue  # composite/nested state block -- not modelled

        m = _MERMAID_STATE_TRANSITION_RE.match(line)
        if m:
            src, dst, label = m.groups()
            src_id = ensure_node(src)
            dst_id = ensure_node(dst)
            edges.append((src_id, dst_id, (label or "").strip() or None))
            continue
        # Unrecognised line -- skip it rather than failing the whole diagram.

    if not saw_header or not nodes:
        return None
    return direction, nodes, edges


def render_mermaid_state_image(source: str):
    """Render a Mermaid stateDiagram/stateDiagram-v2 to a colour PNG via
    Graphviz -- a state machine is a directed graph like a flowchart, just
    with `[*]` pseudostates drawn as small filled circles instead of
    boxes. Returns None if graphviz isn't installed, the source isn't a
    stateDiagram this parser understands, or rendering fails for any
    reason -- callers fall back to the existing plain shaded code block."""
    if _graphviz is None:
        return None
    parsed = parse_mermaid_state(source)
    if parsed is None:
        return None
    direction, nodes, edges = parsed
    if not nodes:
        return None

    dot = _graphviz.Digraph(format="png")
    dot.attr(rankdir=direction, bgcolor="white", nodesep="0.35", ranksep="0.45", dpi=_MERMAID_DPI)
    dot.attr("node", fontname="Helvetica", fontsize="14", fontcolor=_MERMAID_TEXT,
              fillcolor=_MERMAID_FILL, color=_MERMAID_STROKE, penwidth="1.4")
    dot.attr("edge", fontname="Helvetica", fontsize="12", fontcolor=_MERMAID_TEXT,
              color=_MERMAID_EDGE_COLOR, penwidth="1.2", arrowsize="0.8")

    for node_id, label in nodes.items():
        if label == "__pseudo__":
            dot.node(node_id, label="", shape="circle", style="filled",
                      fillcolor=_MERMAID_STROKE, color=_MERMAID_STROKE,
                      width="0.16", height="0.16", fixedsize="true")
        else:
            dot.node(node_id, label=_mermaid_clean_label(label) or node_id,
                      shape="box", style="filled,rounded")

    for src, dst, label in edges:
        edge_kwargs = {}
        if label:
            edge_kwargs["label"] = _mermaid_clean_label(label)
        dot.edge(src, dst, **edge_kwargs)

    try:
        return dot.pipe()
    except Exception:
        return None


# ---- sequenceDiagram support ------------------------------------------

_MERMAID_PARTICIPANT_RE = re.compile(r"^(?:participant|actor)\s+(\w+)(?:\s+as\s+(.+))?$", re.IGNORECASE)
_MERMAID_SEQ_MESSAGE_RE = re.compile(r"^(\w+)\s*(-->>|->>|-->|->)\s*(\w+)\s*:\s*(.+)$")
# Block-directive keywords this parser doesn't model (loop/alt/notes/...) --
# skipped rather than aborting the parse. Each needs a trailing space (or is
# a bare terminator like "end") so it can't prefix-match real content, e.g.
# "par " must not swallow "participant ...".
_MERMAID_SEQ_SKIP_PREFIXES = ("note ", "note left", "note right", "activate ", "deactivate ",
                                "loop ", "alt ", "opt ", "else", "end", "%%", "par ", "and ",
                                "rect ", "autonumber", "critical ", "option ", "title", "box ")


def parse_mermaid_sequence(source: str):
    """Parse a minimal subset of Mermaid `sequenceDiagram` syntax into
    (participants, messages). `participants` maps id -> display label
    (from `participant X as Label`, or just X). `messages` is an ordered
    list of (src_id, dst_id, label, "solid"|"dashed") -- `->>`/`->` are
    solid, `-->>`/`-->` are dashed, matching Mermaid's call-vs-return
    convention. A participant referenced only in a message (never
    declared) is still picked up, in first-seen order, the same as real
    Mermaid does. Block constructs this parser doesn't model (loop/alt/
    opt/notes/activation bars/...) are skipped line-by-line rather than
    aborting the whole parse. Returns None if the fence isn't a
    sequenceDiagram at all, or nothing usable was found."""
    saw_header = False
    participants = {}
    messages = []

    def ensure_participant(pid):
        participants.setdefault(pid, pid)

    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not saw_header:
            if not line.lower().startswith("sequencediagram"):
                return None
            saw_header = True
            continue
        if line.lower().startswith(_MERMAID_SEQ_SKIP_PREFIXES):
            continue

        m = _MERMAID_PARTICIPANT_RE.match(line)
        if m:
            pid, label = m.groups()
            participants[pid] = (label or pid).strip()
            continue

        m = _MERMAID_SEQ_MESSAGE_RE.match(line)
        if m:
            src, arrow, dst, label = m.groups()
            ensure_participant(src)
            ensure_participant(dst)
            style = "dashed" if arrow.startswith("--") else "solid"
            messages.append((src, dst, label.strip(), style))
            continue
        # Unrecognised line -- skip rather than aborting the whole parse.

    if not saw_header or not participants or not messages:
        return None
    return participants, messages


def render_mermaid_sequence_image(source: str):
    """Render a Mermaid sequenceDiagram to a colour PNG with matplotlib
    (participant boxes, dashed lifelines, message arrows) -- Graphviz has
    no native notion of a sequence diagram's lifelines, so this is a
    small purpose-built renderer instead of trying to coax a
    node/edge-graph tool into that shape. Returns None if the source
    isn't a sequenceDiagram this parser understands, or on any rendering
    failure -- callers fall back to the existing plain shaded code
    block."""
    parsed = parse_mermaid_sequence(source)
    if parsed is None:
        return None
    participants, messages = parsed

    try:
        ids = list(participants.keys())
        n = len(ids)
        col_index = {pid: i for i, pid in enumerate(ids)}

        # A single uniform box width (sized to the longest label) keeps all
        # participant columns evenly spaced -- simpler than per-column
        # widths, and still fully legible.
        max_label_len = max(len(participants[pid]) for pid in ids)
        box_w = max(1.5, min(3.0, 0.11 * max_label_len + 0.5))
        col_w = box_w + 0.5

        header_h = 0.6
        row_h = 0.75
        margin = 0.3
        fig_w = margin * 2 + col_w * max(n - 1, 0) + box_w
        fig_h = margin * 2 + header_h + row_h * len(messages) + 0.3

        fig = Figure(figsize=(fig_w, fig_h), dpi=int(_MERMAID_DPI))
        canvas = FigureCanvasAgg(fig)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_xlim(0, fig_w)
        ax.set_ylim(0, fig_h)
        ax.axis("off")
        ax.invert_yaxis()  # time flows downward

        top_y = margin + header_h / 2
        bottom_y = fig_h - margin

        def col_x(pid):
            return margin + col_index[pid] * col_w + box_w / 2

        for pid in ids:
            x = col_x(pid)
            box = FancyBboxPatch(
                (x - box_w / 2, top_y - header_h / 2), box_w, header_h,
                boxstyle="round,pad=0.05,rounding_size=0.08",
                linewidth=1.4, edgecolor=_MERMAID_STROKE, facecolor=_MERMAID_FILL,
                zorder=3,
            )
            ax.add_patch(box)
            ax.text(x, top_y, participants[pid], ha="center", va="center", fontsize=11,
                     color=_MERMAID_TEXT, zorder=4)
            ax.plot([x, x], [top_y + header_h / 2, bottom_y], linestyle=(0, (4, 3)),
                     color=_MERMAID_STROKE, linewidth=1.1, zorder=1)

        for i, (src, dst, label, style) in enumerate(messages):
            y = top_y + header_h / 2 + row_h * (i + 1) - row_h / 2
            x_src, x_dst = col_x(src), col_x(dst)
            linestyle = "dashed" if style == "dashed" else "solid"
            if x_src == x_dst:
                # Self-message: a small loop out to the right and back.
                arrow = FancyArrowPatch(
                    (x_src, y - 0.12), (x_dst, y + 0.12), connectionstyle="arc3,rad=1.2",
                    arrowstyle="-|>", mutation_scale=12, linewidth=1.3,
                    linestyle=linestyle, color=_MERMAID_EDGE_COLOR, zorder=2,
                )
            else:
                arrow = FancyArrowPatch(
                    (x_src, y), (x_dst, y), arrowstyle="-|>", mutation_scale=14,
                    linewidth=1.3, linestyle=linestyle, color=_MERMAID_EDGE_COLOR, zorder=2,
                    shrinkA=0, shrinkB=0,
                )
            ax.add_patch(arrow)
            ax.text((x_src + x_dst) / 2, y - 0.08, label, ha="center", va="bottom",
                     fontsize=9.5, color=_MERMAID_TEXT, zorder=4,
                     bbox=dict(boxstyle="round,pad=0.15", facecolor="white",
                                edgecolor="none", alpha=0.92))

        buf = io.BytesIO()
        canvas.print_png(buf)
        return buf.getvalue()
    except Exception:
        return None


# ---- gitGraph support ---------------------------------------------------

_MERMAID_GITGRAPH_HEADER_RE = re.compile(r"^gitgraph\b", re.IGNORECASE)
_MERMAID_GITGRAPH_BRANCH_RE = re.compile(r'^branch\s+"?([A-Za-z_][\w-]*)"?', re.IGNORECASE)
_MERMAID_GITGRAPH_CHECKOUT_RE = re.compile(r'^(?:checkout|switch)\s+"?([A-Za-z_][\w-]*)"?', re.IGNORECASE)
_MERMAID_GITGRAPH_MERGE_RE = re.compile(r'^merge\s+"?([A-Za-z_][\w-]*)"?(.*)$', re.IGNORECASE)
_MERMAID_GITGRAPH_COMMIT_RE = re.compile(r"^commit\b(.*)$", re.IGNORECASE)
_MERMAID_GITGRAPH_ID_RE = re.compile(r'id:\s*"([^"]*)"')

# (fill, stroke) pairs, one per branch in creation order (cycled if there
# are more branches than colours). Index 0 matches this module's existing
# flowchart/state palette so `main` looks consistent with other diagrams.
_MERMAID_GITGRAPH_COLORS = [
    ("#ECECFF", "#9370DB"),
    ("#D5F5E3", "#2E8B57"),
    ("#FDEBD0", "#CC6600"),
    ("#D6EAF8", "#1E6FBA"),
    ("#FADBD8", "#B22222"),
    ("#E8DAEF", "#6C3483"),
]


def parse_mermaid_gitgraph(source: str):
    """Parse a minimal subset of Mermaid `gitGraph` syntax into
    (branch_order, branches, merges). `branch_order` is a list of branch
    names in creation order (`main` always first, created implicitly).
    `branches` maps name -> {"commits": [{"x", "label", "is_merge"}],
    "parent": name|None, "parent_x": int|None} where `x` is a global
    chronological commit index (branch/checkout don't advance it, only
    `commit`/`merge` do). `merges` is a list of {"from_branch", "from_x",
    "to_branch", "to_x"} used to draw the diagonal merge connector.
    Constructs this parser doesn't model (`cherry-pick`, `commit type:`/
    `tag:` beyond a plain id, `%%` comments) are skipped line-by-line
    rather than aborting the whole parse. Returns None if the fence isn't
    a gitGraph at all, or no commit was found."""
    saw_header = False
    branch_order = ["main"]
    branches = {"main": {"commits": [], "parent": None, "parent_x": None}}
    last_commit_x = {"main": None}
    current = "main"
    merges = []
    x_counter = 0

    for raw_line in source.splitlines():
        line = raw_line.strip().rstrip(";")
        if not line:
            continue
        if not saw_header:
            if not _MERMAID_GITGRAPH_HEADER_RE.match(line):
                return None  # not a gitGraph at all
            saw_header = True
            continue
        if line.startswith("%%"):
            continue

        m = _MERMAID_GITGRAPH_BRANCH_RE.match(line)
        if m:
            name = m.group(1)
            if name not in branches:
                branches[name] = {"commits": [], "parent": current,
                                    "parent_x": last_commit_x[current]}
                branch_order.append(name)
                last_commit_x[name] = None
            current = name  # `branch` also checks out the new branch
            continue

        m = _MERMAID_GITGRAPH_CHECKOUT_RE.match(line)
        if m:
            name = m.group(1)
            if name in branches:
                current = name
            continue

        m = _MERMAID_GITGRAPH_MERGE_RE.match(line)
        if m:
            src_name, rest = m.groups()
            if src_name not in branches or last_commit_x[src_name] is None:
                continue  # nothing on that branch to merge in -- skip
            from_x = last_commit_x[src_name]
            x_counter += 1
            x = x_counter
            id_m = _MERMAID_GITGRAPH_ID_RE.search(rest)
            label = id_m.group(1) if id_m else f"merge {src_name}"
            branches[current]["commits"].append({"x": x, "label": label, "is_merge": True})
            last_commit_x[current] = x
            merges.append({"from_branch": src_name, "from_x": from_x,
                            "to_branch": current, "to_x": x})
            continue

        m = _MERMAID_GITGRAPH_COMMIT_RE.match(line)
        if m:
            rest = m.group(1)
            x_counter += 1
            x = x_counter
            id_m = _MERMAID_GITGRAPH_ID_RE.search(rest)
            label = id_m.group(1) if id_m else None
            branches[current]["commits"].append({"x": x, "label": label, "is_merge": False})
            last_commit_x[current] = x
            continue
        # Unrecognised line (cherry-pick, tag:-only commit options, ...) --
        # skip it rather than failing the whole diagram.

    if not saw_header or x_counter == 0:
        return None
    return branch_order, branches, merges


def render_mermaid_gitgraph_image(source: str):
    """Render a Mermaid gitGraph to a colour PNG with matplotlib -- one
    horizontal lane per branch, commits as dots placed in chronological
    order along the x-axis, with diagonal connectors where a `branch` or
    `merge` crosses lanes. Returns None if the source isn't a gitGraph
    this parser understands, or on any rendering failure -- callers fall
    back to the existing plain shaded code block."""
    parsed = parse_mermaid_gitgraph(source)
    if parsed is None:
        return None
    branch_order, branches, merges = parsed

    try:
        row_of = {name: i for i, name in enumerate(branch_order)}
        colors = {name: _MERMAID_GITGRAPH_COLORS[i % len(_MERMAID_GITGRAPH_COLORS)]
                   for i, name in enumerate(branch_order)}

        max_x = max(
            (c["x"] for info in branches.values() for c in info["commits"]),
            default=0,
        )
        if max_x == 0:
            return None

        col_w = 1.3
        row_h = 0.9
        # Commit labels are drawn rotated 30 degrees up-and-right from their
        # dot (see below), so the longest one anywhere can overhang past the
        # top-right corner of a tightly-sized figure -- pad the headroom
        # above the top lane and to the right of the last column for the
        # worst case, rather than clipping it off in the saved PNG.
        all_labels = [c["label"] for info in branches.values() for c in info["commits"] if c["label"]]
        max_label_len = max((len(label) for label in all_labels), default=0)
        label_w = 0.075 * max_label_len
        bottom_margin = 0.6
        top_headroom = max(0.5, 0.3 + label_w * 0.5)
        right_margin = max(0.6, 0.3 + label_w * 0.87)
        left_margin = max(0.9, 0.11 * max(len(name) for name in branch_order) + 0.6)

        fig_w = left_margin + right_margin + col_w * max_x
        fig_h = bottom_margin + row_h * max(len(branch_order) - 1, 0) + top_headroom

        fig = Figure(figsize=(fig_w, fig_h), dpi=int(_MERMAID_DPI))
        canvas = FigureCanvasAgg(fig)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_xlim(0, fig_w)
        ax.set_ylim(0, fig_h)
        ax.axis("off")

        def px(x):
            return left_margin + x * col_w

        def py(row):
            return bottom_margin + row * row_h

        # Branch lanes (horizontal lines) and their name labels.
        for name in branch_order:
            info = branches[name]
            row = row_of[name]
            _, stroke = colors[name]
            start_x = info["parent_x"] if info["parent_x"] is not None else 0
            commit_xs = [c["x"] for c in info["commits"]]
            end_x = max(commit_xs) if commit_xs else start_x
            ax.plot([px(start_x), px(end_x)], [py(row), py(row)],
                     color=stroke, linewidth=2.0, zorder=1, solid_capstyle="round")
            ax.text(px(start_x) - 0.15, py(row), name, ha="right", va="center",
                     fontsize=10, fontweight="bold", color=stroke, zorder=4)

        # Diagonal connectors for branch creation.
        for name in branch_order[1:]:
            info = branches[name]
            if not info["commits"]:
                continue
            parent_row = row_of[info["parent"]]
            parent_x = info["parent_x"] if info["parent_x"] is not None else 0
            first_x = info["commits"][0]["x"]
            _, stroke = colors[name]
            ax.plot([px(parent_x), px(first_x)], [py(parent_row), py(row_of[name])],
                     color=stroke, linewidth=1.6, zorder=1)

        # Diagonal connectors for merges (coloured as the source branch).
        for merge in merges:
            _, stroke = colors[merge["from_branch"]]
            ax.plot([px(merge["from_x"]), px(merge["to_x"])],
                     [py(row_of[merge["from_branch"]]), py(row_of[merge["to_branch"]])],
                     color=stroke, linewidth=1.6, zorder=1)

        # Commit dots and id labels, fanned out diagonally so long commit
        # messages don't collide with the next dot along the same lane.
        for name in branch_order:
            row = row_of[name]
            fill, stroke = colors[name]
            for commit in branches[name]["commits"]:
                x, y = px(commit["x"]), py(row)
                ax.add_patch(Circle((x, y), 0.11, facecolor=fill, edgecolor=stroke,
                                      linewidth=1.3, zorder=3))
                if commit["is_merge"]:
                    ax.add_patch(Circle((x, y), 0.05, facecolor="white",
                                          edgecolor="none", zorder=3))
                if commit["label"]:
                    ax.text(x + 0.16, y + 0.13, commit["label"], ha="left", va="bottom",
                             fontsize=8.5, color=_MERMAID_TEXT, rotation=30,
                             rotation_mode="anchor", zorder=4,
                             bbox=dict(boxstyle="round,pad=0.1", facecolor="white",
                                        edgecolor="none", alpha=0.85))

        buf = io.BytesIO()
        canvas.print_png(buf)
        return buf.getvalue()
    except Exception:
        return None


def render_mermaid_image(source: str):
    """Dispatch a ```mermaid fence to the right renderer based on its
    declared diagram type. Returns None (falling back to the plain shaded
    code block) for any diagram type/syntax neither renderer understands."""
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    if not lines:
        return None
    if lines[0].lower().startswith("sequencediagram"):
        return render_mermaid_sequence_image(source)
    if _MERMAID_STATE_HEADER_RE.match(lines[0]):
        return render_mermaid_state_image(source)
    if _MERMAID_GITGRAPH_HEADER_RE.match(lines[0]):
        return render_mermaid_gitgraph_image(source)
    if _MERMAID_HEADER_RE.match(lines[0]):
        return render_mermaid_flowchart_image(source)
    return None


def resolve_mermaid_diagrams(soup, image_registry, enabled=True):
    """Find ```mermaid fenced code blocks and, where the feature is on and
    the diagram parses, replace them with a rendered colour image --
    registered through the same ImageRegistry as every other embedded
    image. Left completely untouched (falls through to the normal
    plain-text code-block rendering) when the feature is off, graphviz
    isn't available, or a given diagram can't be parsed.

    Also catches a plain, *untagged* ``` fence (no `language-mermaid` class
    at all) whose content is unambiguously Mermaid syntax -- i.e.
    render_mermaid_image() recognises its header keyword and parses it
    successfully. This is a deliberate safety net: source HTML for blog
    articles often marks a code block's language via a `data-language`
    attribute on the outer element (e.g. Astro/Shiki's
    `<pre data-language="mermaid">`) rather than a `language-mermaid` class
    on `<code>`, so when that HTML is hand-transcribed into Markdown it's
    easy to drop the ```mermaid tag and paste a bare ``` fence instead. A
    fence explicitly tagged as some other language (```python, ```json,
    ...) is never reinterpreted -- only a fence with no language class at
    all gets this fallback check, and it only fires when the content
    parses as a real diagram (matches a Mermaid header keyword AND yields
    at least one node), so ordinary code is never misdetected."""
    if not enabled:
        return
    for pre in list(soup.find_all("pre")):
        code = pre.find("code")
        if code is None:
            continue
        classes = code.get("class") or []
        text = code.get_text()
        if "language-mermaid" not in classes and classes:
            continue
        png = render_mermaid_image(text)
        if png is None:
            continue
        rel_path = image_registry.register(png, media_type="image/png", prefix="mermaid")
        img = soup.new_tag("img")
        img["src"] = rel_path
        img["class"] = ["content-img", "mermaid-img"]
        img["alt"] = "[Mermaid diagram, rendered]"
        wrapper = soup.new_tag("p")
        wrapper.append(img)
        pre.replace_with(wrapper)


# =====================================================================
# Markdown text preprocessing, copied from the sibling markdown_to_pdf
# project (md_to_pdf.py) -- unchanged logic, still python-markdown input.
# =====================================================================

_FENCE_LINE_RE = re.compile(r"^\s*```")


def _line_fence_mask(lines):
    """True for each line inside a fenced ``` code block (including the
    fence marker lines themselves), so line-by-line preprocessing below
    doesn't mistake a diagram's bare "|" connector or "- "/"N. " prefixed
    line for a real list/table needing a spacing fixup."""
    mask = []
    in_fence = False
    for line in lines:
        if _FENCE_LINE_RE.match(line):
            in_fence = not in_fence
            mask.append(True)
        else:
            mask.append(in_fence)
    return mask


def ensure_blank_line_before_blocks(text: str) -> str:
    """python-markdown requires a preceding blank line for lists/tables to be
    recognized as their own block; source docs often omit it after a label
    line like 'Foo:\\n- item', which would otherwise collapse into prose."""
    lines = text.split("\n")
    fence_mask = _line_fence_mask(lines)
    list_re = re.compile(r"^\s*([-*+]|\d+\.)\s+")
    table_re = re.compile(r"^\s*\|")
    out = []
    for i, line in enumerate(lines):
        if fence_mask[i]:
            out.append(line)
            continue
        is_block_start = list_re.match(line) or table_re.match(line)
        if is_block_start:
            prev = out[-1] if out else ""
            prev_is_same = list_re.match(prev) or table_re.match(prev)
            if prev.strip() != "" and not prev_is_same:
                out.append("")
        out.append(line)
    return "\n".join(out)


_LIST_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d+\.)(\s+)(.*)$")
_DASH_CHARS = "-–—"  # hyphen, en dash, em dash
# Only a **bold**/**bold:** label counts as a "header" -- this must stay in
# sync with the bold-header-bullet rule in CLAUDE.md, which is specifically
# about a bold lead-in followed by a colon or dash, not any list item that
# happens to contain a mid-sentence dash (splitting those rewrites the
# author's own wording, which is out of scope -- see CLAUDE.md).
_BOLD_HEADER_RE = re.compile(r"^\*\*[^*]+\*\*:?$")


def _find_dash_split(content: str):
    """Locate the first standalone ' - ' / ' – ' / ' — ' delimiter in a list
    item's text that immediately follows a **bold** (or **bold:**) label
    and nothing else -- the shape CLAUDE.md documents as needing a
    sub-bullet split. Ignores dashes inside [] or () (so markdown link text
    isn't split), inside a **bold** span (so 'Label — detail.**' inside
    emphasis isn't torn in half, which would leave a dangling '**' and
    corrupt the markdown), and any dash not preceded by a bare bold label
    (an ordinary sentence with a dash in it, e.g. inside a numbered
    question, is left untouched). Returns the (start, end) span of the
    delimiter (including its surrounding spaces), or None."""
    depth_sq = depth_paren = 0
    in_bold = False
    n = len(content)
    i = 0
    while i < n:
        c = content[i]
        if content[i:i + 2] == "**":
            in_bold = not in_bold
            i += 2
            continue
        if c == "[":
            depth_sq += 1
        elif c == "]":
            depth_sq = max(0, depth_sq - 1)
        elif c == "(":
            depth_paren += 1
        elif c == ")":
            depth_paren = max(0, depth_paren - 1)
        elif (c in _DASH_CHARS and depth_sq == 0 and depth_paren == 0 and not in_bold
              and i >= 3 and content[i - 1] == " "
              and i + 1 < n and content[i + 1] == " "):
            head = content[:i - 1].rstrip()
            if _BOLD_HEADER_RE.match(head):
                return (i - 1, i + 2)
        i += 1
    return None


def promote_inline_dash_sublist(text: str) -> str:
    """Turn a list item like '- **RAG** - RAG uses vector indexes...' into
    a bullet with its own nested sub-bullet. Only fires when the text
    before the dash is a bare bold label (see _BOLD_HEADER_RE) -- an
    ordinary sentence that happens to contain a dash is left alone."""
    lines = text.split("\n")
    fence_mask = _line_fence_mask(lines)
    out = []
    for i, line in enumerate(lines):
        m = None if fence_mask[i] else _LIST_ITEM_RE.match(line)
        if m:
            indent, marker, spacing, content = m.groups()
            span = _find_dash_split(content)
            if span:
                head, tail = content[:span[0]], content[span[1]:]
                out.append(f"{indent}{marker}{spacing}{head}")
                out.append(f"{indent}  - {tail}")
                continue
        out.append(line)
    return "\n".join(out)


_PAREN_LIST_ITEM_RE = re.compile(r"^(\s*)(\d{1,9})\)(\s+)(\S.*)$")


def normalize_paren_ordered_lists(text: str) -> str:
    """python-markdown's list processors (sane_lists included) only treat
    'N.' as an ordered-list marker, not CommonMark's other valid form
    'N)' -- a common way people (and GPT/Claude transcripts) write numbered
    lists, which otherwise collapses into one run-on paragraph. Rewrite
    'N)' -> 'N.' at the start of a line, but only where it's genuinely
    opening/continuing a list (preceded by a blank line or another such
    marker line) so an incidental parenthesized reference elsewhere in
    prose is never touched."""
    lines = text.split("\n")
    fence_mask = _line_fence_mask(lines)
    out = []
    for i, line in enumerate(lines):
        m = None if fence_mask[i] else _PAREN_LIST_ITEM_RE.match(line)
        if m:
            prev = lines[i - 1] if i > 0 else ""
            prev_in_fence = fence_mask[i - 1] if i > 0 else False
            prev_is_boundary = (not prev_in_fence) and (
                prev.strip() == "" or _PAREN_LIST_ITEM_RE.match(prev) is not None
            )
            if prev_is_boundary:
                indent, num, spacing, content = m.groups()
                out.append(f"{indent}{num}.{spacing}{content}")
                continue
        out.append(line)
    return "\n".join(out)


_LEADING_HASHES_RE = re.compile(r"^#+")


def escape_bare_hashtags(text: str) -> str:
    """A line-leading '#' immediately followed by a non-space character
    (social-media style, e.g. '#RelationshipScience #EmotionalIntelligence'
    at the end of a LinkedIn post) is a hashtag, not an ATX heading.
    CommonMark requires a space after the '#', but python-markdown doesn't
    enforce that -- it happily parses such a line as an <h1>/<h2>/etc,
    which then also acts as a chapter-splitting boundary in
    split_chapters(), silently eating the leading '#' and misfiling the
    rest of the document under a bogus chapter title. Escape the leading
    marker so it renders as plain text instead; a genuine heading always
    has a space after its '#'s, so those are left untouched.

    Deliberately checks the character following the *whole* run of
    leading '#'s via plain string indexing rather than a regex lookahead:
    a lookahead of the form '#{1,6}(?=\\S)' lets the engine backtrack to
    matching just one '#' and then treat the next '#' in a multi-'#'
    heading (e.g. '## Heading') as satisfying \\S, wrongly flagging every
    real H2-H6 heading too."""
    lines = text.split("\n")
    fence_mask = _line_fence_mask(lines)
    out = []
    for i, line in enumerate(lines):
        if not fence_mask[i]:
            m = _LEADING_HASHES_RE.match(line)
            if m:
                rest = line[m.end():]
                if rest and not rest[0].isspace():
                    line = "\\" + line
        out.append(line)
    return "\n".join(out)


# =====================================================================
# --url: fetch a live web page and pull out its article content. This
# automates what was previously a manual workflow (curl the raw HTML,
# extract the <article> body by hand, save it into inputs/, then run the
# normal HTML loader on it) -- deliberately built the same way: a plain
# urllib fetch of the *raw* HTML (no headless browser, no JS execution, no
# summarizing model in the loop), so what ends up in the EPUB is the
# page's actual server-rendered markup, not an AI's paraphrase of it.
#
# Two problems specific to *scraped* HTML (as opposed to hand-authored
# input) that the rest of the pipeline doesn't otherwise need to worry
# about:
#   1. Extraction is heuristic, like the PDF loader's structure recovery:
#      most modern blog engines emit a semantic <article> or <main>
#      landmark for the real content, but not all do, and a site that
#      embeds a widget (a "subscribe"/"login" CTA, a comments box, related
#      posts) *inside* that landmark will have it ride along too -- the
#      fetched HTML is saved as a real file in inputs/ specifically so it
#      can be hand-trimmed before conversion if that happens, the same as
#      any other input.
#   2. Images are addressed relative to the *page's* URL, not to a file on
#      disk, and a fetch that never runs JavaScript will see whatever a
#      lazy-load library left in `src` before the real image loads --
#      typically a tiny placeholder `data:` URI or nothing at all, with
#      the real URL parked in a `data-src`/`srcset` attribute instead.
#      Both are resolved (relative -> absolute, lazy attr -> real src)
#      before resolve_local_images()/resolve_remote_images() ever see the
#      tags, since those two only understand a plain local path or a
#      ready-to-fetch http(s) URL.
# =====================================================================

URL_FETCH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
URL_FETCH_TIMEOUT_S = 15
URL_FETCH_MAX_BYTES = 20 * 1024 * 1024  # 20MB cap on the page itself

# Tags almost never part of the article body -- stripped whenever no
# <article>/<main> landmark is found and the fallback is "the whole
# <body>". Left alone when an <article>/<main> was found, since scoping to
# that landmark already excludes most of these by construction.
_CHROME_TAGS = ("nav", "header", "footer", "aside", "form", "iframe")

# Lazy-load libraries don't agree on one attribute name; check the common
# ones in order of how often they show up in the wild.
_LAZY_SRC_ATTRS = ("data-src", "data-lazy-src", "data-original")
_LAZY_SRCSET_ATTRS = ("data-srcset", "data-lazy-srcset")


def fetch_url(url: str) -> str:
    """Fetch a page's raw HTML (decompressing gzip if the server used it,
    since urllib doesn't do this automatically the way a browser would)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": URL_FETCH_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(req, timeout=URL_FETCH_TIMEOUT_S) as resp:
        data = resp.read(URL_FETCH_MAX_BYTES + 1)
        if len(data) > URL_FETCH_MAX_BYTES:
            raise ValueError(f"page too large (>{URL_FETCH_MAX_BYTES // (1024 * 1024)}MB)")
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            data = gzip.decompress(data)
        encoding = resp.headers.get_content_charset() or "utf-8"
    return data.decode(encoding, errors="replace")


_SRCSET_CANDIDATE_RE = re.compile(r"([\d.]+)([wx])$")


def _best_srcset_candidate(srcset: str):
    """Pick the highest-resolution candidate out of a `srcset` list
    ("url1 480w, url2 800w, url3 1200w" or "url1 1x, url2 2x"). E-ink
    legibility is worth more here than shaving file size, and the
    existing 20MB per-image cap in resolve_remote_images already bounds
    the worst case regardless."""
    best_url, best_score = None, -1.0
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        score = 0.0
        if len(bits) > 1:
            m = _SRCSET_CANDIDATE_RE.match(bits[1])
            if m:
                score = float(m.group(1))
        if score >= best_score:
            best_url, best_score = bits[0], score
    return best_url


def _promote_lazy_image(img):
    """A lazy-load library only swaps in an <img>'s real `src` once JS
    runs and the element scrolls into view; a plain HTML fetch instead
    sees whatever placeholder was left behind. Promote the best real
    source available (a data-src-style attribute, this element's own
    srcset, or a sibling <source>'s srcset inside a <picture>) into `src`
    before anything else looks at this tag -- in place, since every
    caller just wants a normal `<img src="...">` afterwards."""
    src = (img.get("src") or "").strip()
    if src and not src.startswith("data:"):
        return
    for attr in _LAZY_SRC_ATTRS:
        val = (img.get(attr) or "").strip()
        if val and not val.startswith("data:"):
            img["src"] = val
            return
    for attr in ("srcset",) + _LAZY_SRCSET_ATTRS:
        val = img.get(attr)
        if val:
            best = _best_srcset_candidate(val)
            if best:
                img["src"] = best
                return
    picture = img.find_parent("picture")
    if picture is not None:
        for source in picture.find_all("source"):
            for attr in ("srcset",) + _LAZY_SRCSET_ATTRS:
                val = source.get(attr)
                if val:
                    best = _best_srcset_candidate(val)
                    if best:
                        img["src"] = best
                        return


def resolve_relative_urls(soup, base_url):
    """Rewrite every relative <img src>/<a href> against the page's own
    URL, and promote lazy-loaded image sources first -- resolve_local_images
    and resolve_remote_images only understand a plain local file path or an
    already-absolute http(s) URL, neither of which a scraped page's markup
    reliably provides on its own."""
    for img in soup.find_all("img"):
        _promote_lazy_image(img)
        src = img.get("src")
        if src and not src.startswith(("http://", "https://", "data:")):
            img["src"] = urllib.parse.urljoin(base_url, src)
    for a in soup.find_all("a"):
        href = a.get("href")
        if href and not href.startswith(("http://", "https://", "mailto:", "#")):
            a["href"] = urllib.parse.urljoin(base_url, href)


_COOKIE_SIGNIN_RE = re.compile(
    r"(we use cookies|this (?:site|website) uses cookies|"
    r"accept (?:all )?cookies|cookie (?:policy|consent|settings|preferences|notice)|"
    r"manage (?:your )?cookies|by (?:continuing|using this site)[^.]{0,40}cookies|"
    r"sign in to (?:continue|comment|read|access|save)|"
    r"log in to (?:continue|comment|read|access|save)|"
    r"sign in with (?:google|github|facebook|twitter|apple)|"
    r"please sign in|please log in|create an account to)",
    re.I,
)
# Block-level tags a cookie banner or sign-in gate is typically wrapped in.
_PROMPT_BLOCK_TAGS = ("div", "section", "aside", "form", "p", "button")
# Conservative size cap: a real paragraph that merely *mentions* cookies or
# signing in in passing runs much longer than an actual banner/gate, which
# is almost always a short blurb plus a button.
_PROMPT_MAX_CHARS = 300


def strip_cookie_and_signin_prompts(content):
    """Remove cookie-consent banners and 'sign in / log in to keep reading'
    gates from scraped article content -- boilerplate chrome that isn't
    part of the article, but that (unlike nav/header/footer) can land
    *inside* an <article>/<main> landmark and so isn't caught by the
    landmark-scoping in extract_article_html()."""
    candidates = [
        tag for tag in content.find_all(_PROMPT_BLOCK_TAGS)
        if (text := tag.get_text(" ", strip=True))
        and len(text) <= _PROMPT_MAX_CHARS
        and _COOKIE_SIGNIN_RE.search(text)
    ]
    # Smallest text first: a nested match (e.g. the banner's own <p>) is
    # decomposed before its (larger) wrapping <div> is considered, so the
    # wrapper's later decompose() just cleans up what's left rather than
    # double-handling the same nodes.
    candidates.sort(key=lambda t: len(t.get_text()))
    for tag in candidates:
        if tag.parent is not None:
            tag.decompose()


_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _extract_article_date(soup, content):
    """Best-effort publish-date lookup, tried in order of reliability:
    explicit machine-readable metadata first, a human-readable <time>
    element second. Returns 'YYYY-MM-DD' or None -- callers use this
    (the date *in* the article) in preference to the fetch timestamp for
    naming, since that's what actually identifies the piece to a reader
    (two fetches of the same article on different days should still be
    recognizable as the same article)."""
    for attrs in ({"property": "article:published_time"}, {"name": "date"},
                  {"name": "dc.date"}, {"itemprop": "datePublished"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            m = _ISO_DATE_RE.match(tag["content"].strip())
            if m:
                return "-".join(m.groups())
    time_tag = content.find("time") if content else None
    if time_tag and time_tag.get("datetime"):
        m = _ISO_DATE_RE.match(time_tag["datetime"].strip())
        if m:
            return "-".join(m.groups())
    return None


def extract_article_html(html_text: str, url: str):
    """Best-effort isolation of a fetched page's article content -- see
    the module-level comment above this section for the two scraped-HTML-
    specific problems this (and resolve_relative_urls) handles. Returns
    (article_html, title, author, date); title/author/date are None if
    nothing usable was found (the caller falls back to its own defaults)."""
    soup = BeautifulSoup(html_text, "html.parser")

    title_tag = soup.find("title")
    page_title = title_tag.get_text(strip=True) if title_tag else None

    meta_author = (soup.find("meta", attrs={"name": "author"})
                   or soup.find("meta", attrs={"property": "article:author"}))
    author = meta_author["content"].strip() if meta_author and meta_author.get("content") else None

    content = soup.find("article") or soup.find("main")
    if content is None:
        content = soup.find("body") or soup
        for t in content.find_all(_CHROME_TAGS):
            t.decompose()
    for t in content.find_all(["script", "style", "noscript"]):
        t.decompose()
    strip_cookie_and_signin_prompts(content)

    date = _extract_article_date(soup, content)

    resolve_relative_urls(content, url)

    h1 = content.find("h1")
    title = (h1.get_text(strip=True) if h1 else None) or page_title
    # decode_contents(), not str(content): the latter would include
    # `content` itself as an outer wrapper tag (<article>...</article>, or
    # worse, a literal <body>...</body> in the no-landmark fallback case),
    # which then ends up nested a second time inside the fresh <body> this
    # gets embedded into by save_fetched_url() -- a real, structurally
    # invalid <body><body>...</body></body> in that fallback case.
    return content.decode_contents(), title, author, date


def _slugify_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", parsed.path.strip("/") or parsed.netloc).strip("-").lower()
    return slug[:60] or "page"


def _slugify_title(text: str):
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return slug[:60] or None


def save_fetched_url(url: str) -> tuple:
    """Fetch `url`, extract its article content, and save it into inputs/
    as a real HTML file -- mirroring save_pasted_input()'s reasoning: kept
    alongside file-based input rather than only ever existing transiently,
    and inspectable/editable by hand before conversion (see the module
    comment above about content that rides along inside <article>).
    Returns (input_path, title, author)."""
    html_text = fetch_url(url)
    article_html, title, author, date = extract_article_html(html_text, url)
    os.makedirs(INPUT_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    # Name the file after the article's own title (h1, falling back to the
    # page's <title> tag -- see extract_article_html) when one was found, so
    # files in inputs/ are recognizable at a glance rather than all reading
    # as a URL-path slug. Falls back to the URL slug only when no usable
    # title/h1 exists on the page at all.
    slug = _slugify_title(title) if title else None
    if not slug:
        slug = _slugify_url(url)
    # Prefer the article's own publish date over the fetch timestamp when
    # available (see _extract_article_date) -- still append the fetch
    # timestamp too, so re-fetching the same article twice in one day
    # doesn't collide.
    date_part = f"{date}_" if date else ""
    input_path = os.path.join(INPUT_DIR, f"url_{slug}_{date_part}{ts}.html")
    doc = (f'<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
           f'<title>{html.escape(title or url)}</title></head>\n<body>\n{article_html}\n</body></html>')
    with open(input_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return input_path, title, author


# =====================================================================
# Input loaders: each returns (BeautifulSoup, placeholder_map).
# =====================================================================

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)


def strip_yaml_frontmatter(text: str) -> str:
    """Remove the leading `---`/`---` fence lines from a YAML frontmatter
    block, only when the first one opens the very first line of the file --
    the standard convention (Jekyll, Hugo, ...), which also means a `---`
    used later as a real horizontal rule is left untouched. The key/value
    lines between the fences are kept, not discarded.

    Without the hard-break fixup below, the Markdown parser (no `meta`
    extension is loaded) would merge every one of those lines into a
    single `<p>`: Markdown only treats a *blank* line as a paragraph
    break, so plain newlines between e.g.
        title: "..."
        author: "..."
    collapse into whitespace when rendered, reading as one run-on line.
    Appending a Markdown hard-break ("  \\n", two trailing spaces) to each
    line keeps them stacked as separate lines in the output instead."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return text
    interior_lines = m.group(1).splitlines()
    rendered = "  \n".join(interior_lines)
    return text[:m.start()] + rendered + "\n" + text[m.end():]


_TIMESTAMP_LABEL_RE = re.compile(
    r'^(?P<prefix>\*\*(?:Created|Updated|Exported):\*\*[ \t]*)'
    r'(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4})'
    r'[ \t]+(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})'
    r'(?P<suffix>[ \t]*)$',
    re.MULTILINE,
)


def normalize_export_timestamps(text: str) -> str:
    """Rewrite '**Created:** 8/31/2026 9:44:08' (and Updated/Exported)
    header lines -- the timestamp block emitted by the user's chat-export
    tool -- to 'YYYY-mm-dd HH:MM:SS'. Source dates are unambiguously US
    M/D/YYYY (confirmed against real exports, e.g. 8/31/2026 = Aug 31);
    that locale is hardcoded rather than auto-detected. Seconds-precision
    time is kept, not dropped: Created/Updated/Exported on the same
    document always share the same calendar date in practice, so the
    time-of-day is the only thing distinguishing them."""
    def _replace(m):
        y, mo, d = int(m.group('year')), int(m.group('month')), int(m.group('day'))
        h, mi, s = int(m.group('hour')), int(m.group('minute')), int(m.group('second'))
        try:
            dt = datetime.datetime(y, mo, d, h, mi, s)
        except ValueError:
            return m.group(0)  # not a real calendar date/time -- leave untouched
        return f"{m.group('prefix')}{dt.strftime('%Y-%m-%d %H:%M:%S')}{m.group('suffix')}"
    return _TIMESTAMP_LABEL_RE.sub(_replace, text)


_METADATA_LABEL_LINE_RE = re.compile(r'^\*\*[^*\n]+:\*\*(?:\s|$)')


def insert_metadata_line_breaks(text: str) -> str:
    """A run of consecutive '**Label:** value' lines (Author/Posted/Source,
    Title/Author/Published/Publication/Source, etc.) with no blank line
    between them collapses into one run-on paragraph -- python-markdown
    only breaks paragraphs on a blank line. Append a markdown hard break
    (two trailing spaces) to each such line that is immediately followed
    by another matching line, so they render as stacked separate lines
    within one grouped paragraph -- matching how the Created/Updated/
    Exported header block already renders (those source lines already
    happen to carry trailing double-spaces) -- rather than blank-line-
    separating them into distinct <p> blocks with extra paragraph spacing.
    Anchored at column 0, so a bulleted '- **Header:** text' sub-list item
    (which always has a leading '- ') is never matched. A no-op on lines
    that already end with two trailing spaces, so re-running this (or
    running it over already-hard-broken input like the Created/Updated/
    Exported block) never doubles up breaks."""
    lines = text.split("\n")
    fence_mask = _line_fence_mask(lines)
    n = len(lines)
    out = []
    for i, line in enumerate(lines):
        if fence_mask[i] or not _METADATA_LABEL_LINE_RE.match(line):
            out.append(line)
            continue
        next_matches = (
            i + 1 < n
            and not fence_mask[i + 1]
            and _METADATA_LABEL_LINE_RE.match(lines[i + 1])
        )
        if next_matches and not line.endswith("  "):
            out.append(line + "  ")
        else:
            out.append(line)
    return "\n".join(out)


def load_markdown(path: str):
    text = read_file(path)
    text = strip_yaml_frontmatter(text)
    text = normalize_export_timestamps(text)
    text = insert_metadata_line_breaks(text)
    text = normalize_paren_ordered_lists(text)
    text = escape_bare_hashtags(text)
    text = promote_inline_dash_sublist(text)
    text = ensure_blank_line_before_blocks(text)
    protected, placeholder_map = protect_math_placeholders(text)
    html_text = markdown.markdown(
        protected,
        # fenced_code's fence regex only matches at column 0, so a fence
        # indented inside a list item (standard, valid Markdown) never
        # matches and falls through to inline-code handling instead.
        # superfences handles fences nested in list items/blockquotes too;
        # use_pygments=False keeps render_code_block() as the sole place
        # doing Pygments highlighting, so output shape stays unchanged for
        # top-level code blocks.
        extensions=["tables", "pymdownx.superfences", "pymdownx.highlight", "pymdownx.magiclink", "sane_lists", "toc"],
        extension_configs={"pymdownx.highlight": {"use_pygments": False}},
        tab_length=2,
    )
    soup = BeautifulSoup(html_text, "html.parser")
    return soup, placeholder_map


def load_html(path: str):
    soup = BeautifulSoup(read_file(path), "html.parser")
    # A full document (as --url/--paste save, or any hand-authored file
    # with its own <html><head>...</head>) has a <title>/<meta>/<link> in
    # <head> that must NOT ride into the render pipeline as if it were
    # body content -- render_block's generic-wrapper fallback now surfaces
    # a leaf tag's own text rather than silently dropping it (see
    # render_mixed_content), which is exactly right for something like a
    # scraped page's <time> byline but would otherwise also resurrect the
    # document's own <title> text as a bogus leading paragraph. A
    # body-less fragment (the tool's original assumption, still the common
    # case for a hand-pasted snippet) has no <body> to find, so it's used
    # as-is, unchanged from before.
    body = soup.find("body")
    if body is not None:
        return BeautifulSoup(body.decode_contents(), "html.parser"), {}
    return soup, {}


def load_txt(path: str):
    text = read_file(path)
    protected, placeholder_map = protect_math_placeholders(text)
    paragraphs = re.split(r"\n\s*\n", protected)
    parts = []
    for p in paragraphs:
        if not p.strip():
            continue
        escaped = html.escape(p).replace("\n", "<br/>")
        parts.append(f"<p>{escaped}</p>")
    soup = BeautifulSoup("".join(parts), "html.parser")
    return soup, placeholder_map


# ---------- PDF: heuristic structure recovery via PyMuPDF ----------

FLAG_BOLD = 1 << 4  # fitz span flags bit for bold


def _summarize_block(block):
    lines_text = []
    sizes = []
    bold_flags = []
    for line in block.get("lines", []):
        line_text = "".join(s.get("text", "") for s in line.get("spans", []))
        lines_text.append(line_text)
        for s in line.get("spans", []):
            if s.get("text", "").strip():
                sizes.append(s.get("size", 0.0))
                bold_flags.append(bool(s.get("flags", 0) & FLAG_BOLD))
    text = "\n".join(t for t in lines_text if t.strip())
    if not sizes:
        return text, 0.0, False
    size = statistics.median(sizes)
    bold = sum(bold_flags) > len(bold_flags) / 2
    return text, size, bold


def _classify_heading(size, bold, median_body):
    if median_body <= 0:
        return None
    ratio = size / median_body
    if ratio >= 1.5:
        return 1
    if ratio >= 1.25:
        return 2
    if ratio >= 1.1 or (bold and ratio >= 1.0):
        return 3
    return None


def _table_overlap_index(bbox, table_bboxes, threshold=0.5):
    bx0, by0, bx1, by1 = bbox
    barea = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    if barea <= 0:
        return None
    for i, tb in enumerate(table_bboxes):
        tx0, ty0, tx1, ty1 = tb
        ix0, iy0 = max(bx0, tx0), max(by0, ty0)
        ix1, iy1 = min(bx1, tx1), min(by1, ty1)
        iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
        if (iw * ih) / barea > threshold:
            return i
    return None


def _rows_to_table_html(rows):
    if not rows:
        return ""
    out = ["<table><tbody>"]
    for r in rows:
        cells = "".join(f"<td>{escape_x(c or '')}</td>" for c in r)
        out.append(f"<tr>{cells}</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _elements_to_soup(elements):
    parts = []
    for el in elements:
        kind = el[0]
        if kind == "heading":
            _, level, text = el
            tag = f"h{min(level, 4)}"
            parts.append(f"<{tag}>{escape_x(text)}</{tag}>")
        elif kind == "para":
            _, text = el
            escaped = escape_x(text).replace("\n", "<br/>")
            parts.append(f"<p>{escaped}</p>")
        elif kind == "table":
            _, rows = el
            parts.append(_rows_to_table_html(rows))
    return BeautifulSoup("".join(parts), "html.parser")


def load_pdf(path: str):
    doc = fitz.open(path)
    pages_data = []
    body_sizes = []
    for page in doc:
        d = page.get_text("dict")
        for block in d.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for s in line.get("spans", []):
                    if s.get("text", "").strip():
                        body_sizes.append(s.get("size", 0.0))
        try:
            tabs = page.find_tables()
        except Exception:
            tabs = None
        table_infos = []
        if tabs is not None:
            for t in tabs.tables:
                try:
                    rows = t.extract()
                except Exception:
                    rows = None
                if rows:
                    table_infos.append((tuple(t.bbox), rows))
        pages_data.append((d, table_infos))

    median_body = statistics.median(body_sizes) if body_sizes else 10.0

    elements = []
    for d, table_infos in pages_data:
        table_bboxes = [tb for tb, _ in table_infos]
        page_elements = []  # (y0, element)
        for block in d.get("blocks", []):
            if block.get("type") != 0:
                continue
            bbox = block.get("bbox", (0, 0, 0, 0))
            if _table_overlap_index(bbox, table_bboxes) is not None:
                continue
            text, size, bold = _summarize_block(block)
            if not text.strip():
                continue
            level = _classify_heading(size, bold, median_body)
            el = ("heading", level, text) if level else ("para", text)
            page_elements.append((bbox[1], el))
        for bbox, rows in table_infos:
            page_elements.append((bbox[1], ("table", rows)))
        page_elements.sort(key=lambda x: x[0])
        elements.extend(e for _, e in page_elements)

    return _elements_to_soup(elements), {}


def load_image_file(path: str):
    """A bare image file (e.g. a pasted screenshot or a photo) as the
    whole input -- wraps it in a single <img>, which resolve_local_images
    then embeds the same way as an image referenced from within a
    markdown/HTML document."""
    alt = os.path.splitext(os.path.basename(path))[0].replace("_", " ")
    html_text = f'<p><img src="{html.escape(os.path.abspath(path), quote=True)}" alt="{html.escape(alt, quote=True)}"/></p>'
    return BeautifulSoup(html_text, "html.parser"), {}


# =====================================================================
# XHTML rendering: soup -> chapter body strings, mirroring md_to_pdf.py's
# dispatch-table shape but emitting markup directly instead of reportlab
# flowables (tables/nested lists can mostly pass through verbatim here).
# =====================================================================

# "native": terminal-style dark background (#202020) with a warm, muted
# palette (orange keywords, green strings, ...).
_PYGMENTS_FORMATTER = HtmlFormatter(cssclass="codehilite", nowrap=False, style="native")


def render_code_block(pre_tag) -> str:
    code_tag = pre_tag.find("code")
    lang = None
    if code_tag is not None:
        for cls in code_tag.get("class", []):
            if cls.startswith("language-"):
                lang = cls[len("language-"):]
                break
    text = (code_tag or pre_tag).get_text()
    try:
        lexer = get_lexer_by_name(lang) if lang else TextLexer()
    except ClassNotFound:
        lexer = TextLexer()
    return highlight(text, lexer, _PYGMENTS_FORMATTER)


def _render_img_tag(node) -> str:
    src = node.get("src", "")
    alt = node.get("alt", "")
    cls = node.get("class")
    style = node.get("style")
    cls_attr = f' class="{" ".join(cls)}"' if cls else ""
    style_attr = f' style="{html.escape(style, quote=True)}"' if style else ""
    return (
        f'<img{cls_attr} src="{html.escape(src, quote=True)}" '
        f'alt="{html.escape(alt, quote=True)}"{style_attr}/>'
    )


def _walk_inline(node, parts):
    if isinstance(node, NavigableString):
        parts.append(escape_x(str(node)))
        return
    if not isinstance(node, Tag):
        return
    name = node.name
    if name in ("strong", "b"):
        parts.append("<strong>")
        for c in node.children:
            _walk_inline(c, parts)
        parts.append("</strong>")
    elif name in ("em", "i"):
        parts.append("<em>")
        for c in node.children:
            _walk_inline(c, parts)
        parts.append("</em>")
    elif name == "code":
        cls = node.get("class") or []
        if "math-fallback" in cls:
            code_class = "math-fallback"
        elif "img-fallback" in cls:
            code_class = "img-fallback"
        else:
            code_class = "inline-code"
        parts.append(f'<code class="{code_class}">{escape_x(node.get_text())}</code>')
    elif name == "a":
        href = node.get("href", "")
        parts.append(f'<a href="{html.escape(href, quote=True)}">')
        for c in node.children:
            _walk_inline(c, parts)
        parts.append("</a>")
    elif name == "br":
        parts.append("<br/>")
    elif name in ("sup", "sub"):
        parts.append(f"<{name}>{escape_x(node.get_text())}</{name}>")
    elif name == "img":
        parts.append(_render_img_tag(node))
    else:
        for c in node.children:
            _walk_inline(c, parts)


def inline_xhtml(tag) -> str:
    parts = []
    for c in tag.children:
        _walk_inline(c, parts)
    return "".join(parts)


def inline_xhtml_single(tag) -> str:
    parts = []
    _walk_inline(tag, parts)
    return "".join(parts)


# Loose list items (blank-line-separated paragraphs, or a fenced code
# block/table nested inside a list item) contain these as real block-level
# children rather than pure inline content. They need render_block(), not
# inline_xhtml_single() — otherwise the block tag is silently dropped by
# _walk_inline()'s generic recursive fallback and separate paragraphs run
# together with nothing but a collapsible whitespace newline between them.
_LI_BLOCK_TAGS = {"p", "pre", "table", "blockquote", "hr", "h1", "h2", "h3", "h4", "figure"}


def render_list(tag) -> str:
    name = tag.name  # ul or ol
    items = []
    for li in tag.find_all("li", recursive=False):
        nested = li.find_all(["ul", "ol"], recursive=False)
        own_children = [c for c in li.children
                         if not (isinstance(c, Tag) and c.name in ("ul", "ol"))]
        content_parts = []
        for c in own_children:
            if isinstance(c, NavigableString):
                content_parts.append(escape_x(str(c)))
            elif isinstance(c, Tag) and c.name in _LI_BLOCK_TAGS:
                content_parts.append(render_block(c))
            elif isinstance(c, Tag):
                content_parts.append(inline_xhtml_single(c))
        text = "".join(content_parts).strip()
        nested_html = "".join(render_list(n) for n in nested)
        items.append(f"<li>{text}{nested_html}</li>")
    return f"<{name}>{''.join(items)}</{name}>"


# Tag names render_block treats as its own block, independent of
# _LI_BLOCK_TAGS above (which is scoped to render_list's own li-vs-nested-
# list split and deliberately excludes ul/ol -- nested lists there are
# pulled out and handled separately). ul/ol belong here: a bullet list
# buried inside a wrapper div/table cell is real block content, not
# something safe to flatten into a run of inline text.
_BLOCK_TAG_NAMES = {"p", "pre", "table", "blockquote", "hr", "h1", "h2", "h3", "h4", "ul", "ol", "figure"}


def _contains_block(node) -> bool:
    """True if `node` either is, or (recursively, at any depth) contains,
    a tag render_block treats as its own block. Generated site markup
    commonly wraps real content in several layers of styling-only <div>s
    (e.g. a card wrapper around a "prose" wrapper around the actual
    paragraphs/headings) -- checking only immediate children would call
    all of that "no block content" and flatten the real structure inside
    it down to plain text."""
    return isinstance(node, Tag) and (
        node.name in _BLOCK_TAG_NAMES or node.find(list(_BLOCK_TAG_NAMES)) is not None
    )


def render_mixed_content(tag) -> str:
    """Render a container that may hold real block-level content (possibly
    wrapped arbitrarily deep in styling-only divs/sections) interleaved
    with loose inline content -- used for render_block's fallback on
    unfamiliar wrapper tags (a scraped page's <div>/<section>/<time>/
    <figcaption>/..., none of which carry meaning worth preserving on
    their own) and, via render_table_cell(), for table cells that hold
    more than plain inline content (e.g. a bullet list inside a <td>).
    Consecutive inline-only children (loose text, <em>/<strong>/<a>/..., or
    a wrapper with nothing but inline content inside) are grouped and
    flattened into one shared paragraph so formatting/spacing across them
    survives normally (an <em>-then-text caption doesn't get split into
    two disconnected paragraphs); a child that is or contains real block
    content is instead handed to render_block(), which recurses through
    this same function for any further wrapper layers."""
    parts = []
    buffer = []

    def flush():
        if buffer:
            joined = "".join(buffer).strip()
            if joined:
                parts.append(f"<p>{joined}</p>")
            buffer.clear()

    for c in tag.children:
        if isinstance(c, NavigableString):
            buffer.append(escape_x(str(c)))
        elif isinstance(c, Tag):
            if _contains_block(c):
                flush()
                parts.append(render_block(c))
            else:
                buffer.append(inline_xhtml_single(c))
    flush()
    return "".join(parts)


def render_table_cell(td) -> str:
    """Plain inline_xhtml() for the common case (a cell with nothing but
    text/inline formatting), matching the tool's original cell rendering
    exactly -- render_mixed_content's <p>-wrapping is only worth paying
    for (and only visually appropriate) once a cell actually holds real
    block content, e.g. a bullet list in a feature-comparison table."""
    if td.find(list(_BLOCK_TAG_NAMES)) is None:
        return inline_xhtml(td)
    return render_mixed_content(td)


def _cell_span_attrs(cell) -> str:
    attrs = []
    for name in ("colspan", "rowspan"):
        val = cell.get(name)
        if val and val.isdigit() and val != "1":
            attrs.append(f' {name}="{val}"')
    return "".join(attrs)


def render_table(tag) -> str:
    parts = ["<table>"]
    thead = tag.find("thead", recursive=False)
    all_rows = tag.find_all("tr", recursive=False)
    header_row = None
    if thead:
        parts.append("<thead>")
        for tr in thead.find_all("tr", recursive=False):
            cells = tr.find_all(["th", "td"], recursive=False)
            parts.append("<tr>" + "".join(
                f"<th{_cell_span_attrs(c)}>{render_table_cell(c)}</th>" for c in cells
            ) + "</tr>")
        parts.append("</thead>")
    elif all_rows and all_rows[0].find_all("th", recursive=False) \
            and not all_rows[0].find_all("td", recursive=False):
        # No <thead> wrapper, but the first row is made entirely of <th> --
        # treat it as the header anyway. Real-world/hand-authored HTML
        # (unlike Markdown's own "tables" extension output) doesn't always
        # bother with the wrapper tag.
        header_row = all_rows[0]
        cells = header_row.find_all("th", recursive=False)
        parts.append("<thead><tr>" + "".join(
            f"<th{_cell_span_attrs(c)}>{render_table_cell(c)}</th>" for c in cells
        ) + "</tr></thead>")
    tbody = tag.find("tbody", recursive=False)
    if tbody:
        body_rows = tbody.find_all("tr", recursive=False)
    else:
        body_rows = [r for r in all_rows if r.find_parent("thead") is None and r is not header_row]
    parts.append("<tbody>")
    for tr in body_rows:
        cells = tr.find_all(["td", "th"], recursive=False)
        parts.append("<tr>" + "".join(
            f"<td{_cell_span_attrs(c)}>{render_table_cell(c)}</td>" for c in cells
        ) + "</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def render_block(tag) -> str:
    if not isinstance(tag, Tag):
        return ""
    name = tag.name
    if name in ("h1", "h2", "h3", "h4"):
        return f"<{name}>{inline_xhtml(tag)}</{name}>"
    if name == "p":
        content = inline_xhtml(tag)
        if not content.strip():
            return ""
        cls = tag.get("class")
        cls_attr = f' class="{" ".join(cls)}"' if cls else ""
        return f"<p{cls_attr}>{content}</p>"
    if name == "pre":
        return render_code_block(tag)
    if name == "table":
        return render_table(tag)
    if name in ("ul", "ol"):
        return render_list(tag)
    if name == "hr":
        return "<hr/>"
    if name == "blockquote":
        inner = "".join(render_block(c) for c in tag.find_all(recursive=False))
        return f"<blockquote>{inner}</blockquote>"
    if name == "div" and "qa-turn" in (tag.get("class") or []):
        cls = " ".join(tag.get("class"))
        inner = "".join(render_block(c) for c in tag.find_all(recursive=False))
        return f'<div class="{cls}">{inner}</div>'
    if name == "img":
        # A bare <img> reachable directly as one of a container's
        # find_all(recursive=False) children (typically inside <figure> --
        # a scraped page commonly has <figure><img .../><figcaption>...
        # with no <p> wrapping the image). render_mixed_content's fallback
        # can't handle this: it iterates tag.children looking for content
        # to render, but <img> is a void element with no children, so
        # calling it directly on a bare img silently produced "" -- the
        # image file still got registered into the EPUB manifest by the
        # earlier resolve_local_images/resolve_remote_images pass, but no
        # reference to it ever made it into the rendered page.
        return _render_img_tag(tag)
    if name == "figure":
        inner = "".join(render_block(c) for c in tag.find_all(recursive=False))
        return f"<figure>{inner}</figure>" if inner else ""
    if name == "figcaption":
        content = inline_xhtml(tag)
        return f'<p class="figcaption">{content}</p>' if content.strip() else ""
    # Unknown wrapper tag (a scraped page's <div>/<section>/<header>/<time>/
    # <dt>/<dd>/..., none of which carry meaning worth preserving on their
    # own): render_mixed_content recurses into any real block-level
    # children, or -- if there are none -- falls back to treating the whole
    # tag as flattened inline content. Either way, this is what keeps a
    # bare <time>2026-03-01</time> byline or an <em>-and-text figcaption
    # from silently vanishing, which a naive "only recurse into Tag
    # children" fallback would do (any NavigableString sitting next to a
    # Tag sibling had nowhere to go).
    return render_mixed_content(tag)


def split_chapters(soup, fallback_title):
    """Split into (chapter_title, body_xhtml) pairs at top-level H1
    boundaries, falling back to a single chapter if no H1 is present. Any
    content before the first H1 (e.g. an intro paragraph, or H1s used only
    as internal subsection headers partway into the document) becomes its
    own untitled leading chapter instead of being silently discarded."""
    top_level = [el for el in soup.find_all(recursive=False) if isinstance(el, Tag)]
    h1_indices = [i for i, el in enumerate(top_level) if el.name == "h1"]
    if not h1_indices:
        body = "".join(render_block(el) for el in top_level)
        return [(fallback_title, body)]
    chapters = []
    if h1_indices[0] > 0:
        lead = top_level[: h1_indices[0]]
        chapters.append((fallback_title, "".join(render_block(el) for el in lead)))
    for idx, start in enumerate(h1_indices):
        end = h1_indices[idx + 1] if idx + 1 < len(h1_indices) else len(top_level)
        group = top_level[start:end]
        chapter_title = group[0].get_text(strip=True) or fallback_title
        body = "".join(render_block(el) for el in group)
        chapters.append((chapter_title, body))
    return chapters


# =====================================================================
# CSS
# =====================================================================

def build_css() -> str:
    pygments_css = _PYGMENTS_FORMATTER.get_style_defs(".codehilite")
    return f"""
/* font-size below is the "four sizes smaller" baseline (16px-equivalent
   default -> 12px-equivalent, a factor of 0.75). Headings are defined in
   em relative to this, so they shrink proportionally along with body text
   automatically -- no separate heading-size edit needed. Code blocks and
   math are pinned to their original absolute size by compensating with
   the inverse factor (1/0.75) wherever they'd otherwise inherit this same
   smaller base -- see .codehilite pre, code.math-fallback, and
   .eqimg/.eqimg-inline below. code.inline-code is the one exception: it's
   deliberately left at 1em so it tracks body size instead of staying
   pinned (see the Font sizes section of the README). */
body {{
    font-family: Georgia, "Bookerly", serif;
    font-size: 0.75em;
    line-height: 1.4;
    color: #1c1c1c;
}}
h1, h2, h3, h4 {{
    font-family: Helvetica, Arial, sans-serif;
    color: #1a2b4a;
    line-height: 1.2;
}}
h1 {{ font-size: 1.6em; margin: 0.6em 0 0.4em; }}
h2 {{ font-size: 1.35em; color: #2f6fed; margin: 1em 0 0.4em; border-bottom: 1px solid #d8dee9; padding-bottom: 0.2em; }}
h3 {{ font-size: 1.15em; margin: 0.9em 0 0.3em; }}
h4 {{ font-size: 1.05em; margin: 0.8em 0 0.3em; }}
p {{ margin: 0 0 0.7em; }}
p.subtitle {{ color: #4a4a4a; font-style: italic; }}
a {{ color: #2f6fed; }}
hr {{ border: none; border-top: 1px solid #d8dee9; margin: 1em 0; }}
blockquote {{
    margin: 0.6em 0;
    padding: 0.2em 0.8em;
    border-left: 3px solid #2f6fed;
    color: #4a4a4a;
}}
/* Multi-round Q&A: a lone "Question"/"Answer" marker paragraph in the
   source starts one of these turns (see wrap_qa_turns). Question gets a
   tinted bubble so it reads as distinct from the surrounding prose;
   Answer just gets a small caption, so the answer itself still reads as
   normal flowing body text rather than being boxed in too. */
.qa-turn {{ margin: 1em 0; }}
.qa-label {{
    margin: 0 0 0.35em;
    font-family: Helvetica, Arial, sans-serif;
    font-size: 0.76em;
    font-weight: bold;
    letter-spacing: 0.03em;
    text-transform: uppercase;
}}
.qa-question {{
    background-color: #eef2fb;
    border-left: 3px solid #2f6fed;
    border-radius: 4px;
    padding: 0.7em 0.9em 0.8em;
}}
.qa-question .qa-label {{ color: #2f6fed; }}
.qa-question p:last-child {{ margin-bottom: 0; }}
.qa-answer {{ padding: 0 0.1em; }}
.qa-answer .qa-label {{ color: #4a4a4a; }}
ul, ol {{ margin: 0.4em 0 0.8em; padding-left: 1.4em; }}
li {{ margin: 0.2em 0; }}
table {{
    width: 100%;
    table-layout: auto;
    border-collapse: collapse;
    margin: 0.8em 0;
    font-size: 0.85em;
}}
th, td {{
    border: 1px solid #d8dee9;
    padding: 0.35em 0.5em;
    text-align: left;
    vertical-align: top;
}}
th {{ background-color: #1a2b4a; color: #ffffff; }}
tr:nth-child(even) td {{ background-color: #f4f6fa; }}
code.inline-code {{
    font-family: "Courier New", monospace;
    background-color: #f4f6fa;
    color: #a3123b;
    padding: 0.05em 0.3em;
    border-radius: 2px;
    font-size: 1em; /* matches body text size, unlike the block-level code/math
                        elements below which stay pinned to their original
                        absolute size */
}}
/* background intentionally omitted here -- the pygments_css block emitted
   below sets .codehilite's background/per-token colors directly, and its
   rule needs to come after this one so it wins for those properties. */
.codehilite {{
    border: 1px solid #444444;
    border-radius: 3px;
    padding: 0.5em;
    margin: 0.6em 0;
}}
.codehilite pre {{
    margin: 0;
    font-family: "Courier New", monospace;
    font-size: 1.04em; /* 0.78 / 0.75: cancels body's smaller base, stays same absolute size */
    line-height: 1.35;
    white-space: pre-wrap;
    word-break: break-word;
    overflow-wrap: anywhere;
}}
/* Actual size is set per-image via an inline height:em style (computed in
   math_image_height_em) so glyph size stays consistent with body text
   regardless of an equation's width/complexity -- max-width here is only
   an overflow safety net for unusually long single-line equations.
   font-size here (1/0.75) cancels body's smaller base so that em-height
   still resolves to the original absolute size -- math stays unchanged
   even though body text was reduced. */
.eqimg {{
    display: block;
    max-width: 100%;
    margin: 0.6em auto;
    font-size: 1.333em;
}}
.eqimg-inline {{
    max-width: 100%;
    vertical-align: middle;
    font-size: 1.333em;
}}
/* Stands in for \boxed{...}, which mathtext can't render directly --
   the inner expression is rendered normally and boxed here instead. */
.eqimg-boxed {{
    border: 1.5px solid #1a2b4a;
    border-radius: 2px;
    padding: 0.3em 0.6em;
    background-color: #f4f6fa;
}}
.content-img {{
    display: block;
    max-width: 100%;
    height: auto;
    margin: 0.8em auto;
    border: 1px solid #d8dee9;
    border-radius: 4px;
}}
figure {{ margin: 0.8em 0; }}
p.figcaption {{
    font-size: 0.85em;
    color: #4a4a4a;
    font-style: italic;
    text-align: center;
    margin: 0.2em 0 0.8em;
}}
code.math-fallback {{
    background-color: #f4f6fa;
    border: 1px solid #d8dee9;
    border-radius: 3px;
    padding: 0.1em 0.4em;
    font-family: "Courier New", monospace;
    font-size: 1.133em; /* 0.85 / 0.75: cancels body's smaller base, stays same absolute size */
    white-space: pre-wrap;
    word-break: break-word;
}}
code.img-fallback {{
    display: inline-block;
    background-color: #f4f6fa;
    border: 1px solid #d8dee9;
    border-radius: 3px;
    padding: 0.15em 0.5em;
    font-style: italic;
    color: #6a6a6a;
    font-size: 1em; /* tracks body text size, like code.inline-code -- this is a
                        short inline caption-style note, not displayed code/math,
                        so unlike code.math-fallback it isn't pinned to the
                        original absolute size */
}}
{pygments_css}
""".strip()


# =====================================================================
# EPUB assembly (ebooklib 0.20 API)
# =====================================================================

class EpubBuilder:
    def __init__(self, title, author=None, language="en"):
        self.book = epub.EpubBook()
        self.book.set_identifier(str(uuid.uuid4()))
        self.book.set_title(title)
        self.book.set_language(language)
        if author:
            self.book.add_author(author)
        self.css_item = epub.EpubItem(
            uid="style_main", file_name="style/main.css",
            media_type="text/css", content=build_css(),
        )
        self.book.add_item(self.css_item)
        self.title_chapter = None
        self.chapters = []

    def add_title_page(self, title, subtitle=None):
        parts = [f"<h1>{escape_x(title)}</h1>"]
        if subtitle:
            parts.append(f'<p class="subtitle">{escape_x(subtitle)}</p>')
        c = epub.EpubHtml(uid="titlepage", file_name="titlepage.xhtml", title=title, lang="en")
        c.content = "".join(parts)
        c.add_item(self.css_item)
        self.book.add_item(c)
        self.title_chapter = c

    def add_chapter(self, chapter_title, body_html, index):
        c = epub.EpubHtml(
            uid=f"chap{index}", file_name=f"chap_{index:02d}.xhtml",
            title=chapter_title, lang="en",
        )
        c.content = body_html
        c.add_item(self.css_item)
        self.book.add_item(c)
        self.chapters.append(c)

    def add_image(self, content, rel_path, media_type="image/png"):
        img = epub.EpubImage(
            uid=f"img_{rel_path}", file_name=rel_path,
            media_type=media_type, content=content,
        )
        self.book.add_item(img)

    def finalize(self, output_path):
        self.book.toc = tuple(epub.Link(c.file_name, c.title, c.id) for c in self.chapters)
        self.book.add_item(epub.EpubNcx())
        self.book.add_item(epub.EpubNav())
        spine = ["nav"]
        if self.title_chapter is not None:
            spine.append(self.title_chapter)
        spine.extend(self.chapters)
        self.book.spine = spine
        epub.write_epub(output_path, self.book)


# =====================================================================
# PDF rendering: soup -> reportlab flowables. This is a second consumer
# of the exact same resolved `soup` + `ImageRegistry` built once in
# convert() (above/below) that EPUB assembly reads -- HTML input, local/
# remote image embedding, math-to-image, and Mermaid-to-image all "just
# work" here too, for free, since by this point they're already plain
# <img>/<sup>/<sub> tags in the soup.
#
# Page numbering ("Page X of Y"), the Unicode-capable monospace font
# registration, and the table/list rendering approach are ported from
# the sibling markdown_to_pdf project (md_to_pdf.py) -- that project is
# left completely untouched; only its proven techniques are reused here.
#
# Known limitations (v1): no Pygments syntax highlighting for PDF code
# blocks (reportlab has no native way to consume Pygments' HTML/CSS
# output; plain shaded text, matching md_to_pdf.py's own baseline). SVG
# images fall back to a text placeholder (reportlab can't rasterize SVG
# without an added `svglib` dependency; math/mermaid images are always
# PNG, so this only affects rare local/remote SVG content images).
# =====================================================================

_PDF_MONO_FONT_CANDIDATES = [
    "/System/Library/Fonts/Monaco.ttf",
    "/System/Library/Fonts/Supplemental/Andale Mono.ttf",
]


def _register_pdf_mono_font() -> str:
    """Base-14 PDF fonts (Courier) lack glyphs for tree-diagram/box-drawing
    characters ("├── └── ─ │"), which render as black boxes. macOS's Monaco
    (or Andale Mono) has them. Falls back to Courier if neither is present."""
    for path in _PDF_MONO_FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                _pdf_metrics.registerFont(_PdfTTFont("PdfMonoUnicode", path))
                return "PdfMonoUnicode"
            except Exception:
                pass
    return "Courier"


_PDF_MONO_FONT_NAME = _register_pdf_mono_font()

# Palette matches this tool's own EPUB CSS (build_css()) and md_to_pdf.py's
# palette, so a document's PDF and EPUB renderings feel like the same
# product rather than two unrelated designs.
_PDF_NAVY = _pdf_colors.HexColor("#1a2b4a")
_PDF_ACCENT = _pdf_colors.HexColor("#2f6fed")
_PDF_CODE_BG = _pdf_colors.HexColor("#f4f6fa")
_PDF_CODE_BORDER = _pdf_colors.HexColor("#d8dee9")
_PDF_TABLE_HEAD_BG = _pdf_colors.HexColor("#1a2b4a")
_PDF_TABLE_ALT_BG = _pdf_colors.HexColor("#f4f6fa")
_PDF_GREY_TEXT = _pdf_colors.HexColor("#4a4a4a")
_PDF_RULE_COLOR = _pdf_colors.HexColor("#d8dee9")
_PDF_QA_QUESTION_BG = _pdf_colors.HexColor("#eef2fb")

_PDF_PAGE_MARGIN = 0.75 * _pdf_inch
_PDF_TOP_MARGIN = 1.0 * _pdf_inch  # extra headroom for the running header
_PDF_BOTTOM_MARGIN = 0.85 * _pdf_inch
_PDF_CONTENT_WIDTH = _PDF_LETTER[0] - 2 * _PDF_PAGE_MARGIN
_PDF_MAX_IMAGE_HEIGHT = 7.3 * _pdf_inch


def build_pdf_styles():
    styles = _pdf_get_stylesheet()

    def add(name, **kwargs):
        styles.add(_PdfParagraphStyle(name=name, **kwargs))

    add("PdfDocTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=22, leading=26, textColor=_PDF_NAVY, spaceAfter=4, alignment=_PDF_TA_LEFT)
    add("PdfDocSubtitle", parent=styles["Normal"], fontName="Helvetica",
        fontSize=10.5, leading=14, textColor=_PDF_GREY_TEXT, spaceAfter=18)
    add("PdfH1", parent=styles["Heading1"], fontName="Helvetica-Bold",
        fontSize=15.5, leading=19, textColor=_PDF_NAVY, spaceBefore=20, spaceAfter=10)
    add("PdfH2", parent=styles["Heading2"], fontName="Helvetica-Bold",
        fontSize=12.5, leading=16, textColor=_PDF_ACCENT, spaceBefore=14, spaceAfter=7)
    add("PdfH3", parent=styles["Heading3"], fontName="Helvetica-Bold",
        fontSize=11, leading=14, textColor=_PDF_NAVY, spaceBefore=10, spaceAfter=5)
    add("PdfBody", parent=styles["Normal"], fontName="Helvetica",
        fontSize=9.6, leading=14, textColor=_pdf_colors.HexColor("#1c1c1c"),
        spaceAfter=7, alignment=_PDF_TA_LEFT)
    add("PdfBulletBody", parent=styles["PdfBody"], leftIndent=0, spaceAfter=3, leading=13.2)
    add("PdfCodeBlock", parent=styles["Normal"], fontName=_PDF_MONO_FONT_NAME, fontSize=6.9,
        leading=9.2, textColor=_pdf_colors.HexColor("#1a1a2e"))
    add("PdfTableCell", parent=styles["PdfBody"], fontSize=8.3, leading=11, spaceAfter=0)
    add("PdfTableHeadCell", parent=styles["PdfBody"], fontSize=8.3, leading=11,
        spaceAfter=0, textColor=_pdf_colors.white, fontName="Helvetica-Bold")
    add("PdfCaption", parent=styles["PdfBody"], fontSize=8.2, leading=11,
        textColor=_PDF_GREY_TEXT, alignment=_PDF_TA_CENTER, spaceBefore=2, spaceAfter=10)
    add("PdfQaLabel", parent=styles["PdfBody"], fontName="Helvetica-Bold",
        fontSize=7.6, leading=10, spaceAfter=4)
    add("PdfBlockquoteBody", parent=styles["PdfBody"], textColor=_PDF_GREY_TEXT)
    return styles


# ---- Inline markup: soup inline nodes -> reportlab Paragraph mini-markup ----
# Mirrors inline_xhtml/_walk_inline (EPUB path) in shape, but emits reportlab's
# markup dialect instead of XHTML. reportlab's Paragraph markup can't embed
# in-memory image bytes inline the way XHTML can, so an <img> is always
# pulled out into its own flowable one level up, by
# PdfRenderer._render_inline_run() (used for paragraphs, table cells, and
# list items -- everywhere an <img> can land next to real text: inline math
# with a LaTeX macro, e.g. `$A \cdot B$`, commonly ends up inside a table
# cell or list item, not only a bare top-level paragraph). The "[image]"
# fallback text in _pdf_walk_inline's own `img` branch below is only a
# last-resort safety net for the rare case of an <img> nested two or more
# levels deep in inline content (e.g. inside an <a>/<em> wrapper), which
# _render_inline_run's one-level split doesn't reach.

_PDF_EQIMG_HEIGHT_EM_RE = re.compile(r"height:\s*([\d.]+)em")


def _pdf_walk_inline(node, parts):
    if isinstance(node, NavigableString):
        parts.append(escape_x(str(node)))
        return
    if not isinstance(node, Tag):
        return
    name = node.name
    if name in ("strong", "b"):
        parts.append("<b>")
        for c in node.children:
            _pdf_walk_inline(c, parts)
        parts.append("</b>")
    elif name in ("em", "i"):
        parts.append("<i>")
        for c in node.children:
            _pdf_walk_inline(c, parts)
        parts.append("</i>")
    elif name == "code":
        cls = node.get("class") or []
        if "img-fallback" in cls:
            parts.append('<i><font size="8.5" color="#6a6a6a">')
            parts.append(escape_x(node.get_text()))
            parts.append("</font></i>")
        else:
            # Covers both plain inline code and math-fallback (raw LaTeX
            # shown as text when mathtext couldn't render it) -- same
            # shaded-monospace treatment as the EPUB CSS gives both.
            parts.append(f'<font face="{_PDF_MONO_FONT_NAME}" size="8.4" color="#a3123b" backColor="#f4f6fa">')
            parts.append(escape_x(node.get_text()))
            parts.append("</font>")
    elif name == "a":
        parts.append('<font color="#2f6fed"><u>')
        for c in node.children:
            _pdf_walk_inline(c, parts)
        parts.append("</u></font>")
    elif name == "br":
        parts.append("<br/>")
    elif name in ("sup", "sub"):
        # Confirmed empirically against the installed reportlab version:
        # <super>/<sub> render as true superscript/subscript (raised/
        # lowered baseline + smaller font), not just accepted-and-ignored.
        rl_tag = "super" if name == "sup" else "sub"
        parts.append(f"<{rl_tag}>")
        for c in node.children:
            _pdf_walk_inline(c, parts)
        parts.append(f"</{rl_tag}>")
    elif name == "img":
        parts.append('<i><font size="8" color="#6a6a6a">[image]</font></i>')
    else:
        for c in node.children:
            _pdf_walk_inline(c, parts)


def _pdf_inline_markup(nodes) -> str:
    parts = []
    for c in nodes:
        _pdf_walk_inline(c, parts)
    return "".join(parts)


def render_inline_pdf(tag) -> str:
    return _pdf_inline_markup(tag.children)


def _split_children_and_images(nodes):
    """Split a list of sibling nodes into ordered ('text', [nodes]) /
    ('img', img_tag) segments. A <p>/<td>/<li> holding a mix of prose and
    an inline-math or content <img> (which reportlab's Paragraph markup
    can't embed the way XHTML can) renders as adjacent flowables in the
    same order instead of the image silently degrading to a placeholder --
    used anywhere an <img> can appear nested one level below a text
    container, not just at the top-level <p> this was first written for
    (inline math with a LaTeX macro, e.g. `$A \\cdot B$`, commonly lands
    inside a table cell or list item, not only a bare paragraph)."""
    segments = []
    buffer = []
    for c in nodes:
        if isinstance(c, Tag) and c.name == "img":
            if buffer:
                segments.append(("text", buffer))
                buffer = []
            segments.append(("img", c))
        else:
            buffer.append(c)
    if buffer:
        segments.append(("text", buffer))
    return segments


_PDF_BLOCK_TAGS = {"p", "pre", "table", "blockquote", "hr", "h1", "h2", "h3", "h4",
                   "ul", "ol", "figure", "img"}


def _pdf_contains_block(node) -> bool:
    return isinstance(node, Tag) and (
        node.name in _PDF_BLOCK_TAGS or node.find(list(_PDF_BLOCK_TAGS)) is not None
    )


class PdfRenderer:
    def __init__(self, styles, image_registry, content_width=_PDF_CONTENT_WIDTH):
        self.styles = styles
        self.content_width = content_width
        self._images_by_path = {
            rel_path: (content, media_type)
            for rel_path, content, media_type in image_registry.images
        }

    def _image_bytes(self, rel_path):
        return self._images_by_path.get(rel_path, (None, None))

    def _stack(self, flowables, width=None):
        """Combine multiple flowables into one (a Table cell/wrapper Table
        can only hold a single object) by nesting them in a borderless,
        zero-padding single-column Table -- the same "flowable-in-a-cell"
        trick already used for shaded code/image boxes below."""
        if not flowables:
            return _PdfSpacer(1, 0)
        if len(flowables) == 1:
            return flowables[0]
        col_widths = [width] if width else None
        tbl = _PdfTable([[f] for f in flowables], colWidths=col_widths)
        tbl.setStyle(_PdfTableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        return tbl

    def _render_inline_run(self, nodes, style):
        """Render a run of sibling inline nodes (a <p>'s, <td>'s, or <li>'s
        direct children) into a list of flowables, splitting out any <img>
        (inline math with a LaTeX macro, a content image, ...) into its own
        real image flowable via render_image() rather than letting it
        degrade to the "[image]" placeholder text _pdf_walk_inline falls
        back to for images nested deeper than this."""
        flowables = []
        for kind, payload in _split_children_and_images(nodes):
            if kind == "text":
                text = _pdf_inline_markup(payload)
                if text.strip():
                    flowables.append(_PdfParagraph(text, style))
            else:
                img = self.render_image(payload)
                if img:
                    flowables.append(img)
        return flowables

    def render_image(self, img_tag):
        rel_path = img_tag.get("src", "")
        content, media_type = self._image_bytes(rel_path)
        label = (img_tag.get("alt", "") or "").strip() or rel_path
        if content is None:
            return self._image_fallback(f"[Image could not be loaded: {label[:60]}]")
        if media_type == "image/svg+xml":
            return self._image_fallback(f"[Image not renderable in PDF (SVG): {label[:60]}]")

        try:
            with Image.open(io.BytesIO(content)) as pil_img:
                img_w, img_h = pil_img.size
        except Exception:
            return self._image_fallback(f"[Image could not be loaded: {label[:60]}]")
        if not img_w or not img_h:
            return self._image_fallback(f"[Image could not be loaded: {label[:60]}]")

        cls = img_tag.get("class") or []
        style = img_tag.get("style", "") or ""
        m = _PDF_EQIMG_HEIGHT_EM_RE.search(style)
        if ("eqimg" in cls or "eqimg-inline" in cls) and m:
            # Reuse the same em-height signal math_image_height_em already
            # computed for the EPUB CSS, so an equation is sized relative
            # to body text here too, instead of a generic pixel scale that
            # would make a short equation balloon or a long one shrink.
            em = float(m.group(1))
            body_pt = self.styles["PdfBody"].fontSize
            height_pt = em * body_pt
            width_pt = height_pt * (img_w / img_h)
        else:
            # Treat the raster's own pixel dimensions as if at 96 DPI (a
            # common assumption for web-sourced content images) -- this is
            # only a starting size before the fit-to-page scale below, so
            # it doesn't need to be exact.
            width_pt = img_w * 72.0 / 96.0
            height_pt = img_h * 72.0 / 96.0

        scale = min(1.0, self.content_width / width_pt, _PDF_MAX_IMAGE_HEIGHT / height_pt)
        width_pt *= scale
        height_pt *= scale

        rl_img = _PdfImage(io.BytesIO(content), width=width_pt, height=height_pt)
        wrapper = _PdfTable([[rl_img]], colWidths=[self.content_width])
        wrapper.setStyle(_PdfTableStyle([
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return wrapper

    def _image_fallback(self, text):
        return _PdfParagraph(escape_x(text), self.styles["PdfBody"])

    def render_code_block(self, tag):
        code_tag = tag.find("code") if tag.name == "pre" else tag
        text = (code_tag.get_text() if code_tag is not None else tag.get_text()).rstrip("\n")
        pre = _PdfPreformatted(text, self.styles["PdfCodeBlock"], maxLineLength=250)
        wrapper = _PdfTable([[pre]], colWidths=[self.content_width])
        wrapper.setStyle(_PdfTableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), _PDF_CODE_BG),
            ("BOX", (0, 0), (-1, -1), 0.6, _PDF_CODE_BORDER),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        return wrapper

    _BULLET_CHARS = ["•", "-", "·"]  # bullet, hyphen, middle dot; repeats for level >= 2

    def _bullet_style(self, level):
        cache = self.__dict__.setdefault("_bullet_style_cache", {})
        if level in cache:
            return cache[level]
        left_indent = 16 + level * 18
        style = _PdfParagraphStyle(
            name=f"PdfBulletLevel{level}",
            parent=self.styles["PdfBulletBody"],
            leftIndent=left_indent,
            bulletIndent=max(left_indent - 13, 0),
            bulletFontName="Helvetica",
            bulletFontSize=8.5,
        )
        style.bulletColor = _PDF_ACCENT if level == 0 else _PDF_GREY_TEXT
        cache[level] = style
        return style

    def render_list(self, tag, ordered=False, level=0):
        flowables = []
        counter = 1
        style = self._bullet_style(level)
        for li in tag.find_all("li", recursive=False):
            nested_lists = li.find_all(["ul", "ol"], recursive=False)
            own_children = [c for c in li.children
                             if not (isinstance(c, Tag) and c.name in ("ul", "ol"))]
            bullet_text = f"{counter}." if ordered else self._BULLET_CHARS[min(level, len(self._BULLET_CHARS) - 1)]
            # Split out any <img> (inline math with a LaTeX macro, e.g.
            # `$A \cdot B$`, is a common case here) into its own real image
            # flowable, same as a paragraph/table cell -- the bullet marker
            # attaches to whichever flowable comes first in the item.
            bullet_used = False
            for kind, payload in _split_children_and_images(own_children):
                if kind == "text":
                    text = _pdf_inline_markup(payload).strip()
                    if text:
                        bt = bullet_text if not bullet_used else ""
                        flowables.append(_PdfParagraph(text, style, bulletText=bt))
                        bullet_used = True
                else:
                    if not bullet_used:
                        flowables.append(_PdfParagraph("", style, bulletText=bullet_text))
                        bullet_used = True
                    img = self.render_image(payload)
                    if img:
                        flowables.append(img)
            if not bullet_used:
                # Item had no renderable content at all (rare) -- still
                # emit the bullet marker rather than dropping it silently.
                flowables.append(_PdfParagraph("", style, bulletText=bullet_text))
            counter += 1
            for nl in nested_lists:
                flowables.extend(self.render_list(nl, ordered=(nl.name == "ol"), level=level + 1))
        return flowables

    def _cell_flowables(self, cell, is_head=False):
        style = self.styles["PdfTableHeadCell"] if is_head else self.styles["PdfTableCell"]
        block_tags = ("ul", "ol", "table", "pre", "blockquote")
        if cell.find(list(block_tags)) is not None:
            # Mirrors EPUB's render_table_cell/render_mixed_content split:
            # a cell holding more than plain inline text (e.g. a bullet
            # list in a feature-comparison table) renders that block
            # content in place instead of flattening it into a run of text.
            loose = [c for c in cell.children
                     if not (isinstance(c, Tag) and c.name in _PDF_BLOCK_TAGS)]
            flowables = self._render_inline_run(loose, style)
            for c in cell.find_all(recursive=False):
                if isinstance(c, Tag) and c.name in _PDF_BLOCK_TAGS:
                    flowables.extend(self._render_element(c))
            if not flowables:
                return _PdfParagraph("", style)
            return self._stack(flowables)
        # Plain cell: still split out any <img> (inline math with a LaTeX
        # macro commonly lands directly in a table cell, e.g. `$A \cdot B$`
        # in a comparison table) into a real image flowable rather than a
        # "[image]" placeholder.
        flowables = self._render_inline_run(list(cell.children), style)
        if not flowables:
            return _PdfParagraph("", style)
        return flowables[0] if len(flowables) == 1 else self._stack(flowables)

    def render_table(self, tag):
        thead = tag.find("thead")
        tbody = tag.find("tbody")
        header_cells = []
        if thead:
            for tr in thead.find_all("tr"):
                header_cells = list(tr.find_all(["th", "td"]))
        if tbody:
            body_rows = tbody.find_all("tr")
        else:
            body_rows = [r for r in tag.find_all("tr") if r.find_parent("thead") is None]

        ncols = len(header_cells) if header_cells else (
            len(body_rows[0].find_all(["td", "th"])) if body_rows else 0
        )
        if ncols == 0:
            return None

        data = []
        if header_cells:
            data.append([self._cell_flowables(c, True) for c in header_cells])
        for tr in body_rows:
            cells = tr.find_all(["td", "th"])
            data.append([self._cell_flowables(c) for c in cells])
        if not data:
            return None

        avail_width = self.content_width
        if header_cells:
            raw_lens = [len(c.get_text()) for c in header_cells]
            floor_w = [_pdf_metrics.stringWidth(c.get_text(), "Helvetica-Bold", 8.3) + 12
                       for c in header_cells]
            for ci in range(ncols):
                for tr in body_rows:
                    cells = tr.find_all(["td", "th"])
                    if ci < len(cells):
                        cell_text = cells[ci].get_text()
                        raw_lens[ci] = max(raw_lens[ci], len(cell_text))
                        longest_token = max(
                            (_pdf_metrics.stringWidth(tok, "Helvetica", 8.3)
                             for tok in cell_text.split()), default=0
                        )
                        floor_w[ci] = max(floor_w[ci], longest_token + 12)
            weights = [max(l, 1) for l in raw_lens]
            total_w = sum(weights)
            col_widths = [avail_width * (w / total_w) for w in weights]
            for _ in range(ncols):
                deficit_idx = [i for i in range(ncols) if col_widths[i] < floor_w[i]]
                if not deficit_idx:
                    break
                for i in deficit_idx:
                    col_widths[i] = floor_w[i]
                free_idx = [i for i in range(ncols) if i not in deficit_idx]
                fixed_total = sum(col_widths[i] for i in deficit_idx)
                remaining = avail_width - fixed_total
                free_weight = sum(weights[i] for i in free_idx) or 1
                for i in free_idx:
                    col_widths[i] = remaining * (weights[i] / free_weight)
            scale = avail_width / sum(col_widths)
            if scale < 1:
                col_widths = [w * scale for w in col_widths]
        else:
            col_widths = [avail_width / ncols] * ncols

        tbl = _PdfTable(data, colWidths=col_widths, repeatRows=1 if header_cells else 0)
        style_cmds = [
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("GRID", (0, 0), (-1, -1), 0.5, _PDF_RULE_COLOR),
        ]
        if header_cells:
            style_cmds.append(("BACKGROUND", (0, 0), (-1, 0), _PDF_TABLE_HEAD_BG))
            for r in range(1, len(data)):
                if r % 2 == 0:
                    style_cmds.append(("BACKGROUND", (0, r), (-1, r), _PDF_TABLE_ALT_BG))
        tbl.setStyle(_PdfTableStyle(style_cmds))
        return tbl

    def render_blockquote(self, tag):
        # The wrapper below stacks content 18pt narrower than
        # self.content_width (12pt LEFTPADDING + 6pt RIGHTPADDING), so any
        # render_image() reached while building inner_flowables here must
        # see that same narrower width, or an embedded image overflows the
        # wrapper's actual rendered width (same bug shape as render_qa_turn).
        self.content_width -= 18
        try:
            inner_flowables = []
            for c in tag.find_all(recursive=False):
                inner_flowables.extend(self._render_element(c))
        finally:
            self.content_width += 18
        if not inner_flowables:
            return _PdfSpacer(1, 0)
        content = self._stack(inner_flowables, width=self.content_width - 18)
        wrapper = _PdfTable([[content]], colWidths=[self.content_width])
        wrapper.setStyle(_PdfTableStyle([
            ("LINEBEFORE", (0, 0), (0, -1), 3, _PDF_ACCENT),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return wrapper

    def render_qa_turn(self, tag):
        cls = tag.get("class") or []
        kind = "question" if "qa-question" in cls else "answer"
        label_tag = tag.find("p", class_="qa-label")
        label_text = label_tag.get_text(strip=True) if label_tag else kind.capitalize()
        narrowed = kind == "question"
        if narrowed:
            # The question box below wraps its content 20pt narrower than
            # self.content_width (10pt LEFTPADDING + 10pt RIGHTPADDING), so
            # any render_image() reached while building body_flowables here
            # must see that same narrower width, or an embedded image
            # overflows the box's actual rendered width.
            self.content_width -= 20
        try:
            body_flowables = []
            for c in tag.find_all(recursive=False):
                if c is label_tag:
                    continue
                body_flowables.extend(self._render_element(c))
        finally:
            if narrowed:
                self.content_width += 20

        if kind == "question":
            label_style = _PdfParagraphStyle("PdfQaLabelQ", parent=self.styles["PdfQaLabel"],
                                              textColor=_PDF_ACCENT)
            label_p = _PdfParagraph(escape_x(label_text.upper()), label_style)
            stacked = self._stack([label_p] + body_flowables, width=self.content_width - 20)
            wrapper = _PdfTable([[stacked]], colWidths=[self.content_width])
            wrapper.setStyle(_PdfTableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), _PDF_QA_QUESTION_BG),
                ("LINEBEFORE", (0, 0), (0, -1), 3, _PDF_ACCENT),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]))
            return [_PdfSpacer(1, 6), wrapper, _PdfSpacer(1, 6)]

        label_style = _PdfParagraphStyle("PdfQaLabelA", parent=self.styles["PdfQaLabel"],
                                          textColor=_PDF_GREY_TEXT)
        label_p = _PdfParagraph(escape_x(label_text.upper()), label_style)
        return [_PdfSpacer(1, 4), label_p] + body_flowables

    def render_figure(self, tag):
        flowables = []
        for c in tag.find_all(recursive=False):
            if not isinstance(c, Tag):
                continue
            if c.name == "img":
                img = self.render_image(c)
                if img:
                    flowables.append(img)
            elif c.name == "figcaption":
                text = render_inline_pdf(c)
                if text.strip():
                    flowables.append(_PdfParagraph(text, self.styles["PdfCaption"]))
        return flowables

    def _render_paragraph(self, tag):
        return self._render_inline_run(list(tag.children), self.styles["PdfBody"])

    def _render_mixed(self, tag):
        """Fallback for a wrapper tag this renderer doesn't otherwise
        recognize (a scraped page's <div>/<section>/<time>/..., which the
        resolved soup can still contain -- this PDF path reads the same
        soup EPUB's split_chapters()/render_block() do, before/independent
        of that function's own XHTML-string normalization). Mirrors
        render_mixed_content's EPUB-path behavior: recurse into any real
        block-level content at any depth, flatten everything else into
        shared paragraphs."""
        flowables = []
        buffer = []

        def flush():
            if buffer:
                joined = _pdf_inline_markup(buffer).strip()
                if joined:
                    flowables.append(_PdfParagraph(joined, self.styles["PdfBody"]))
                buffer.clear()

        for c in tag.children:
            if isinstance(c, NavigableString):
                buffer.append(c)
            elif isinstance(c, Tag):
                if c.name == "img":
                    flush()
                    img = self.render_image(c)
                    if img:
                        flowables.append(img)
                elif _pdf_contains_block(c):
                    flush()
                    flowables.extend(self._render_element(c))
                else:
                    buffer.append(c)
        flush()
        return flowables

    _HEADING_STYLES = {"h1": "PdfH1", "h2": "PdfH2", "h3": "PdfH3", "h4": "PdfH3"}

    def _render_element(self, tag):
        if not isinstance(tag, Tag):
            return []
        name = tag.name
        if name in self._HEADING_STYLES:
            text = render_inline_pdf(tag)
            flowables = [_PdfParagraph(text, self.styles[self._HEADING_STYLES[name]])]
            if name == "h2":
                flowables.append(_PdfHRFlowable(width="100%", thickness=0.6,
                                                 color=_PDF_RULE_COLOR, spaceAfter=8))
            return flowables
        if name == "p":
            return self._render_paragraph(tag)
        if name == "pre":
            return [_PdfSpacer(1, 4), self.render_code_block(tag), _PdfSpacer(1, 10)]
        if name == "table":
            tbl = self.render_table(tag)
            return [_PdfSpacer(1, 4), tbl, _PdfSpacer(1, 12)] if tbl else []
        if name in ("ul", "ol"):
            flowables = self.render_list(tag, ordered=(name == "ol"))
            flowables.append(_PdfSpacer(1, 4))
            return flowables
        if name == "hr":
            return [_PdfHRFlowable(width="100%", thickness=0.6, color=_PDF_RULE_COLOR,
                                    spaceBefore=6, spaceAfter=10)]
        if name == "blockquote":
            return [self.render_blockquote(tag)]
        if name == "img":
            img = self.render_image(tag)
            return [img] if img else []
        if name == "figure":
            return self.render_figure(tag)
        if name == "figcaption":
            text = render_inline_pdf(tag)
            return [_PdfParagraph(text, self.styles["PdfCaption"])] if text.strip() else []
        if name == "div" and "qa-turn" in (tag.get("class") or []):
            return self.render_qa_turn(tag)
        return self._render_mixed(tag)

    def build_story(self, soup, title=None, subtitle=None, author=None):
        story = []
        if title:
            story.append(_PdfParagraph(escape_x(title), self.styles["PdfDocTitle"]))
        if subtitle:
            story.append(_PdfParagraph(escape_x(subtitle), self.styles["PdfDocSubtitle"]))
        if title or subtitle:
            story.append(_PdfHRFlowable(width="100%", thickness=1.2, color=_PDF_NAVY, spaceAfter=16))
        for el in soup.find_all(recursive=False):
            if isinstance(el, Tag):
                story.extend(self._render_element(el))
        return story


class PdfNumberedCanvas(_pdf_canvas_mod.Canvas):
    """Canvas that defers header/footer drawing until save(), so it can
    print 'Page X of Y' once the total page count is known. Ported from
    the sibling markdown_to_pdf project's NumberedCanvas, plus a running
    header (new here -- that project only had a footer)."""
    header_text = ""
    footer_left_text = ""
    page_size = _PDF_LETTER

    def __init__(self, *args, **kwargs):
        _pdf_canvas_mod.Canvas.__init__(self, *args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        page_count = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_header(page_count)
            self._draw_footer(page_count)
            _pdf_canvas_mod.Canvas.showPage(self)
        _pdf_canvas_mod.Canvas.save(self)

    def _draw_header(self, page_count):
        self.saveState()
        self.setFont("Helvetica-Bold", 9)
        self.setFillColor(_PDF_NAVY)
        self.drawString(_PDF_PAGE_MARGIN, self.page_size[1] - 0.55 * _pdf_inch, self.header_text or "")
        self.setStrokeColor(_PDF_RULE_COLOR)
        self.setLineWidth(0.5)
        self.line(_PDF_PAGE_MARGIN, self.page_size[1] - 0.65 * _pdf_inch,
                  self.page_size[0] - _PDF_PAGE_MARGIN, self.page_size[1] - 0.65 * _pdf_inch)
        self.restoreState()

    def _draw_footer(self, page_count):
        self.saveState()
        self.setStrokeColor(_PDF_RULE_COLOR)
        self.setLineWidth(0.5)
        self.line(_PDF_PAGE_MARGIN, 0.65 * _pdf_inch, self.page_size[0] - _PDF_PAGE_MARGIN, 0.65 * _pdf_inch)
        self.setFont("Helvetica", 8)
        self.setFillColor(_PDF_GREY_TEXT)
        self.drawString(_PDF_PAGE_MARGIN, 0.5 * _pdf_inch, self.footer_left_text)
        self.drawRightString(self.page_size[0] - _PDF_PAGE_MARGIN, 0.5 * _pdf_inch,
                              f"Page {self._pageNumber} of {page_count}")
        self.restoreState()


def convert_to_pdf(soup, image_registry, output_path, title=None, author=None,
                    subtitle=None, source_filename=None, page_size="a4", footer=None,
                    created_date=None, modified_date=None):
    resolved_page_size = _PDF_A4 if page_size == "a4" else _PDF_LETTER
    content_width = resolved_page_size[0] - 2 * _PDF_PAGE_MARGIN

    styles = build_pdf_styles()
    renderer = PdfRenderer(styles, image_registry, content_width=content_width)
    story = renderer.build_story(soup, title=title, subtitle=subtitle, author=author)

    PdfNumberedCanvas.footer_left_text = footer or ""
    header_bits = [source_filename or "", f"Created: {created_date}", f"Modified: {modified_date}"]
    if author:
        header_bits.append(f"Author: {author}")
    PdfNumberedCanvas.header_text = " · ".join(b for b in header_bits if b)
    PdfNumberedCanvas.page_size = resolved_page_size

    doc = _PdfSimpleDocTemplate(
        output_path,
        pagesize=resolved_page_size,
        leftMargin=_PDF_PAGE_MARGIN,
        rightMargin=_PDF_PAGE_MARGIN,
        topMargin=_PDF_TOP_MARGIN,
        bottomMargin=_PDF_BOTTOM_MARGIN,
        title=title or (source_filename or ""),
    )
    doc.build(story, canvasmaker=PdfNumberedCanvas)


# =====================================================================
# Orchestration
# =====================================================================

_LOADERS = {
    ".md": load_markdown,
    ".markdown": load_markdown,
    ".html": load_html,
    ".htm": load_html,
    ".txt": load_txt,
    ".pdf": load_pdf,
}
_LOADERS.update({ext: load_image_file for ext in IMAGE_MEDIA_TYPES})


def convert(input_path, output_path, title=None, author=None, subtitle=None, mermaid_images=True,
            output_format="epub", page_size="a4", footer=None):
    ext = os.path.splitext(input_path)[1].lower()
    loader = _LOADERS.get(ext)
    if loader is None:
        raise ValueError(f"Unsupported input file extension: {ext}")

    soup, placeholder_map = loader(input_path)

    image_registry = ImageRegistry()
    resolve_math_placeholders(soup, placeholder_map, image_registry)
    wrap_qa_turns(soup)
    resolve_local_images(soup, os.path.dirname(os.path.abspath(input_path)), image_registry)
    resolve_remote_images(soup, image_registry)
    # PROTOTYPE, easy to remove -- see the "Mermaid diagram image
    # rendering" section's ROLLBACK note above resolve_mermaid_diagrams().
    resolve_mermaid_diagrams(soup, image_registry, enabled=mermaid_images)

    doc_title = title or os.path.splitext(os.path.basename(input_path))[0].replace("_", " ")

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    if output_format == "pdf":
        _stat = os.stat(input_path)
        created_date = datetime.datetime.fromtimestamp(
            getattr(_stat, "st_birthtime", _stat.st_ctime)).strftime("%Y-%m-%d")
        modified_date = datetime.datetime.fromtimestamp(_stat.st_mtime).strftime("%Y-%m-%d")
        convert_to_pdf(soup, image_registry, output_path, title=doc_title, author=author,
                        subtitle=subtitle, source_filename=os.path.basename(input_path),
                        page_size=page_size, footer=footer,
                        created_date=created_date, modified_date=modified_date)
        return

    builder = EpubBuilder(title=doc_title, author=author)
    builder.add_title_page(doc_title, subtitle)

    for rel_path, content, media_type in image_registry.images:
        builder.add_image(content, rel_path, media_type)

    for i, (chapter_title, body_html) in enumerate(split_chapters(soup, doc_title)):
        builder.add_chapter(chapter_title, body_html, i)

    builder.finalize(output_path)


_PASTE_EXTENSIONS = {"md": ".md", "html": ".html", "txt": ".txt"}


def save_pasted_input(text: str, fmt: str, title: str = None) -> str:
    """Save pasted content (e.g. copied straight out of a ChatGPT reply)
    into inputs/ as a real file, timestamped, so it's kept alongside any
    file-based inputs rather than only ever existing as a stdin stream.

    When a title is known (--title was passed alongside --paste), the file
    is named after it instead of the generic "pasted_" prefix, since this
    filename flows through unchanged as the output filename and, in turn,
    the Send-to-Kindle email attachment name -- see kindle_delivery.py."""
    os.makedirs(INPUT_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = _PASTE_EXTENSIONS[fmt]
    slug = _slugify_title(title) if title else None
    stem = slug if slug else "pasted"
    input_path = os.path.join(INPUT_DIR, f"{stem}_{ts}{ext}")
    with open(input_path, "w", encoding="utf-8") as f:
        f.write(text)
    return input_path


def build_parser():
    parser = argparse.ArgumentParser(
        description="Convert Markdown/HTML/Text/PDF into a Kindle-ready EPUB, or a paginated PDF."
    )
    parser.add_argument("input", nargs="?",
                         help="Path to the source .md/.html/.htm/.txt/.pdf file "
                              "(omit when using --paste/--url)")
    parser.add_argument("output", nargs="?", help="Path to the output .epub (default: outputs/ folder)")
    parser.add_argument("--paste", action="store_true",
                         help="Read pasted content from stdin instead of a file (e.g. text copied "
                              "from a ChatGPT reply) -- it's saved into inputs/ automatically before "
                              "conversion, so pasted content is kept just like file-based input")
    parser.add_argument("--url",
                         help="Fetch a web page and convert its article content directly -- the raw "
                              "HTML is fetched (no JS execution, no summarizing model), the <article>/"
                              "<main> content is extracted, and it's saved into inputs/ automatically "
                              "before conversion, just like --paste. Title/author are auto-detected "
                              "from the page (its <h1>/<title>, and an author <meta> tag) unless "
                              "--title/--author override them.")
    parser.add_argument("--format", choices=sorted(_PASTE_EXTENSIONS), default="md",
                         help="How to interpret --paste content (default: md, since chat-app copy/paste "
                              "is almost always Markdown-shaped)")
    parser.add_argument("--title", help="Book title (default: derived from the input filename)")
    parser.add_argument("--author", help="Author name")
    parser.add_argument("--subtitle", help="Subtitle shown on the title page")
    parser.add_argument("--footer",
                         help="Free-text override for the PDF footer's left-hand text (overrides "
                              "the default author/subtitle-derived footer). Ignored for EPUB output.")
    parser.add_argument("-o", "--output", dest="output_flag",
                         help="Path to the output .epub (equivalent to the positional OUTPUT; required "
                              "form when using --paste, since there's no positional input slot free)")
    parser.add_argument("--output-format", choices=["epub", "pdf"], default=None,
                         help="Output format to use when OUTPUT is omitted or has no "
                              "recognized extension (default: epub, so no existing "
                              "invocation's behavior changes). If OUTPUT ends in .pdf "
                              "or .epub, that extension always wins over --output-format. "
                              "(Not to be confused with --format, which controls how "
                              "--paste content is interpreted.)")
    parser.add_argument("--page-size", choices=["letter", "a4"], default="a4",
                         help="PDF page size (default: a4). Ignored for EPUB output.")
    parser.add_argument("--mermaid-images", choices=["on", "off"], default="on",
                         help="PROTOTYPE: render ```mermaid fences as colour diagram images via "
                              "Graphviz, keeping the source's own layout direction (default: on). "
                              "Pass 'off' to fully roll back to the tool's original behaviour "
                              "(```mermaid fences shown as a plain shaded text block). "
                              "Requires the Graphviz `dot` binary and `pip install graphviz`; silently "
                              "falls back to the old behaviour per-diagram if either is missing or a "
                              "given diagram doesn't parse.")
    parser.add_argument("--send-to-kindle", dest="send_to_kindle",
                         action=argparse.BooleanOptionalAction, default=None,
                         help="Email the generated EPUB to your Kindle after conversion "
                              "(default: on for EPUB output, off for PDF -- Send-to-Kindle "
                              "delivery never applies to PDF, even if passed explicitly). "
                              "--no-send-to-kindle disables it explicitly.")
    parser.add_argument("--set-kindle-password", action="store_true",
                         help="Prompt for the Gmail App Password used for Send-to-Kindle delivery "
                              "and store it in the OS keyring, then exit without converting anything.")
    parser.add_argument("--clear-kindle-password", action="store_true",
                         help="Remove the stored Gmail App Password from the OS keyring, then exit "
                              "without converting anything.")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.set_kindle_password:
        import getpass
        password = getpass.getpass("Gmail App Password (input hidden): ")
        if not password.strip():
            print("error: App Password cannot be empty", file=sys.stderr)
            sys.exit(1)
        try:
            kindle_delivery.set_app_password(password)
        except kindle_delivery.KindleDeliveryError as e:
            print(f"error: could not store credential: {e}", file=sys.stderr)
            sys.exit(1)
        print("Stored.")
        return

    if args.clear_kindle_password:
        try:
            kindle_delivery.clear_app_password()
        except kindle_delivery.KindleDeliveryError as e:
            print(f"error: could not clear credential: {e}", file=sys.stderr)
            sys.exit(1)
        print("Cleared.")
        return

    sources_given = sum([bool(args.input), args.paste, bool(args.url)])
    if sources_given > 1:
        parser.error("pass only one of: an input file, --paste, or --url")
    if sources_given == 0:
        parser.error("an input file path is required unless --paste or --url is given")

    if args.url:
        try:
            input_path, detected_title, detected_author = save_fetched_url(args.url)
        except Exception as e:
            print(f"error: failed to fetch {args.url}: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Saved fetched page to {input_path}")
        if args.title is None:
            args.title = detected_title
        if args.author is None:
            args.author = detected_author
    elif args.paste:
        if not sys.stdin.isatty():
            text = sys.stdin.read()
        else:
            print("Paste your content below, then press Ctrl-D on a new line when finished:",
                  file=sys.stderr)
            text = sys.stdin.read()
        if not text.strip():
            parser.error("no content received on stdin for --paste")
        input_path = save_pasted_input(text, args.format, title=args.title)
        print(f"Saved pasted input to {input_path}")
    else:
        if not os.path.exists(args.input):
            print(f"error: input file not found: {args.input}", file=sys.stderr)
            sys.exit(1)
        input_path = args.input

    output_path = args.output_flag or args.output
    if output_path:
        out_ext = os.path.splitext(output_path)[1].lower()
        if out_ext == ".pdf":
            output_format = "pdf"
        elif out_ext == ".epub":
            output_format = "epub"
        else:
            output_format = args.output_format or "epub"
    else:
        output_format = args.format or "epub"
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        basename = os.path.splitext(os.path.basename(input_path))[0]
        output_path = os.path.join(OUTPUT_DIR, basename + (".pdf" if output_format == "pdf" else ".epub"))

    convert(input_path, output_path, title=args.title, author=args.author, subtitle=args.subtitle,
            mermaid_images=(args.mermaid_images == "on"), output_format=output_format,
            page_size=args.page_size, footer=args.footer)
    print(f"Wrote {output_path}")

    if kindle_delivery.should_send_to_kindle(args.send_to_kindle, output_format):
        try:
            kindle_delivery.send_to_kindle(output_path)
            print(f"Emailed {output_path} to {kindle_delivery.get_dest_email()} "
                  f"(Gmail accepted it; Amazon converts/delivers it into your "
                  f"Kindle library separately)")
        except kindle_delivery.KindleDeliveryError as e:
            print(f"warning: {output_path} was generated successfully, but "
                  f"Kindle delivery failed: {e}", file=sys.stderr)
            sys.exit(2)
    elif output_format != "epub" and args.send_to_kindle:
        print("note: Send-to-Kindle only applies to EPUB output; "
              "skipping delivery for PDF.", file=sys.stderr)


if __name__ == "__main__":
    main()
