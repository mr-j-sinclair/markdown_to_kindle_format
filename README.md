# markdown_to_kindle_format

Convert Markdown, HTML, plain text, or PDF into a plain, reflowable EPUB
ready to hand to Amazon's **Send to Kindle** app. Send to Kindle accepts
EPUB directly and converts it to native Kindle format on Amazon's servers,
so this tool's only job is producing a clean, well-formed EPUB — no
kindlegen/Calibre/KFX tooling required or used.

Styled for e-ink: plain fonts, shaded/wrapped code blocks with syntax
highlighting, and deliberate math handling — simple notation like `x^2` or
`H_2O` stays as real text, while deeper equations are rendered to cropped
images so they're guaranteed to be readable instead of the usual mangled
Kindle math rendering.

Send to Kindle only wants a well-formed EPUB, but most existing conversion
tools either over-engineer the problem with full Calibre/KFX tooling, or
under-support the kind of source material that actually needs converting —
math-heavy notes, scraped article HTML, images, Mermaid diagrams. This is a
single-file, dependency-light converter built specifically to fill that
gap: one script, no Calibre install, no KFX pipeline, just a clean EPUB (or
paginated PDF) out the other end.

## Setup

```bash
python3 -m pip install -r requirements.txt
```

## Usage

```bash
python3 md_to_kindle.py INPUT[.md|.html|.htm|.txt|.pdf] [OUTPUT.epub] \
    [--title "Doc Title"] [--author "Name"] [--subtitle "Subtitle"]
```

If `OUTPUT.epub` is omitted, it's written into `outputs/`, using the
input's basename. Title defaults to the input filename. This holds
regardless of how the content arrived — a file passed on the command line
or content pasted in via `--paste` (below) both land in `outputs/` by
default; pass an explicit output path (positional, or `-o`/`--output`) only
if you want to override that.

### Example

```bash
python3 md_to_kindle.py inputs/sample_cosine_similarity.md \
    --author "Me" --subtitle "A worked example"
```

## PDF export

Every input type this tool supports (Markdown/HTML/text/PDF, with all its
image/math/Mermaid-diagram/Q&A-turn handling) can also be exported as a
paginated PDF instead of an EPUB — pick it via the output path's extension,
or `--output-format` when the output path is omitted:

```bash
# extension decides the format:
python3 md_to_kindle.py inputs/sample_cosine_similarity.md outputs/sample.pdf

# or, with no explicit output path:
python3 md_to_kindle.py inputs/sample_cosine_similarity.md --output-format pdf
```

`--output-format` only matters when `OUTPUT` is omitted or has no
`.pdf`/`.epub` extension of its own — an explicit `OUTPUT.pdf` or
`OUTPUT.epub` always wins. Default is `epub`, so no existing invocation's
behavior changes. (`--output-format` is distinct from `--format`, which
controls how `--paste` content itself is interpreted.)

PDF output is a fixed-layout, paginated document (unlike EPUB's reflowable
pages), styled to match the EPUB output's own palette:

- `--page-size {letter,a4}` picks the page size (default: `a4`).
  Ignored for EPUB output.
- A running header on every page (including the title page) with the source
  filename, created/modified dates, and the author (only when `--author` is
  passed), and a footer with **"Page X of Y"** page numbering on the right.
  The footer's left-hand side is blank by default; pass `--footer "..."` to
  put free text there instead (e.g. `--footer "Internal draft -- do not
  distribute"`). Both header and footer are ignored for EPUB output.
- The same image/math-image/Mermaid-diagram embedding as EPUB — all three
  are already plain `<img>` tags by the time either output format renders
  them, so nothing needed re-implementing per format. Equation images are
  sized relative to body text, same as in the EPUB CSS, so a short equation
  and a long one both read at a consistent glyph size.
- Tables, nested lists, blockquotes (including one holding a nested list or
  code block, not just plain text), and multi-round Q&A transcript styling
  all render as their own PDF equivalent of the EPUB CSS treatment.

**Known limitations (v1):**

- **No syntax highlighting for code blocks.** EPUB's Pygments-highlighted
  code blocks become plain shaded/monospace text in PDF — reportlab has no
  native way to consume Pygments' HTML/CSS output. Still gets the same
  Unicode-capable monospace font (so box-drawing tree diagrams render
  correctly), just without color.
- **SVG images aren't rasterized.** An EPUB reader displays SVG natively;
  reportlab can't without an added `svglib` dependency, so an SVG image (a
  scraped page's icon/logo, most commonly) becomes a small
  `[Image not renderable in PDF (SVG): ...]` placeholder instead. Math and
  Mermaid-diagram images are always PNG, so this only affects rare local/
  remote SVG content images.

### Pasting content directly (e.g. from ChatGPT)

For content copied straight out of a chat app rather than saved to a file,
use `--paste` and pipe/type the content on stdin:

```bash
python3 md_to_kindle.py --paste --title "GPT reply on embeddings" <<'EOF'
# Whatever you copied

... paste the rest here ...
EOF
```

Or run it with no heredoc and paste interactively, then press `Ctrl-D` on a
new line when you're done. `--paste` content is **saved into `inputs/`
first** (timestamped, e.g. `inputs/pasted_20260818_142831.md`), then
converted exactly like a file-based input — so pasted content is kept
alongside file-based input rather than only ever existing as a transient
stdin stream. It's treated as Markdown by default (`--format md`), since
that's what chat-app copy/paste is almost always shaped like; pass
`--format html` or `--format txt` to interpret it differently. Use
`-o`/`--output` to name the output file explicitly (the positional output
slot isn't available in `--paste` mode, since there's no positional input
to go with it).

### Fetching a web page directly (`--url`)

```bash
python3 md_to_kindle.py --url "https://example.com/some/article" \
    --author "Someone"
```

Fetches the page's raw HTML (plain `urllib`, no headless browser, no JS
execution, and no summarizing model in the loop — what ends up in the EPUB
is the page's own server-rendered markup, not a paraphrase of it),
extracts its `<article>`/`<main>` content (falling back to `<body>` with
`<nav>`/`<header>`/`<footer>`/`<aside>` stripped if neither landmark is
present), and saves it into `inputs/` as a real HTML file before
conversion — timestamped and named after the article's own title, e.g.
`inputs/url_the-perfect-mapping-atlas-meets-gotcha_20260828_120000.html`
(falling back to a URL-path slug, e.g. `inputs/url_some-article_...html`,
only if the page has neither an `<h1>` nor a `<title>` tag). Title defaults
to the article's own `<h1>` (falling back to the page's `<title>` tag);
author defaults to a `<meta name="author">`/`article:author` tag if the
page has one. Both are overridden by `--title`/`--author` as usual.

The timestamp in that filename is normally the *fetch* time, but if the
page exposes a machine-readable publish date (`<meta
property="article:published_time">`, `<meta name="date">`, `<meta
name="dc.date">`, `<meta itemprop="datePublished">`, or a `<time
datetime="...">` element in the article body), that date is used instead
— two fetches of the same article on different days should still be
recognizable as the same article, which a pure fetch-time stamp can't do.

Two problems specific to scraped (as opposed to hand-authored) HTML are
handled automatically before the rest of the pipeline sees the markup:

- **Relative URLs.** A page's own `<img src="/foo.png">` or `<a href="../x">`
  only means something relative to *that page's* URL, not to a file in
  `inputs/`. These are rewritten to absolute URLs before conversion.
- **Lazy-loaded / responsive images.** A plain HTML fetch never runs the
  JavaScript that swaps a lazy-loaded `<img>`'s placeholder for its real
  source, so `src` is often a tiny `data:` URI or empty, with the real
  image parked in `data-src`/`data-lazy-src`/`data-original`, or in a
  `srcset`/`<picture><source>`. The real source is promoted into `src`
  first; when picking from a `srcset`, the highest-resolution candidate is
  used (e-ink legibility matters more here than shaving file size).

Cookie-consent banners and "sign in"/"log in to keep reading" gates are
also stripped automatically. These are common chrome on scraped pages that
(unlike nav/header/footer) can land *inside* the `<article>`/`<main>`
landmark, so landmark-scoping alone doesn't catch them: any short
(<300-character) block matching a cookie/sign-in phrase (e.g. "we use
cookies", "accept all cookies", "sign in to continue", "please log in") is
removed. The size cap keeps this conservative — a real paragraph that just
mentions cookies or signing in in passing runs longer than an actual
banner/gate and is left alone.

This is a heuristic extraction, the same way the PDF loader's structure
recovery is: most modern blog/CMS engines emit a semantic `<article>` or
`<main>` landmark for the real content, but a site that embeds something
else (a "subscribe"/"login" widget, a comments box, related-posts links)
*inside* that landmark will have it ride along too. Since the fetched page
is saved as a real file, it's fine to open it and manually trim anything
unwanted before running the conversion, same as any other input.

## Mermaid diagrams

Fenced ` ```mermaid ` code blocks are rendered to embedded colour images
rather than left as plain text: flowchart, state, sequence, and gitGraph
diagram types are parsed and drawn via Graphviz, then embedded the same way
a local/remote image is (see "Images" below) — so a diagram pasted out of a
chat app or written by hand shows up as an actual picture on Kindle instead
of raw ` ```mermaid ` source text.

- **Requires the system `dot` binary** (`brew install graphviz`) to
  actually render as images. This is a separate install from the Python
  `graphviz` package in `requirements.txt` — that package is just a thin
  wrapper that calls out to the `dot` binary, it doesn't ship a renderer
  itself.
- **Degrades gracefully** if either the system `dot` binary or the Python
  `graphviz` package isn't available: the diagram falls back to a plain
  shaded monospace code fence (the raw Mermaid source, styled like any
  other code block) instead of failing the conversion. This is why
  `graphviz` in `requirements.txt` is commented as a soft/optional
  dependency — see the comment directly above it in that file for the
  rollback story.
- Use `--mermaid-images {on,off}` to control the behavior explicitly
  (default: `on`). `on` attempts image rendering and still falls back to
  plain text per-diagram if Graphviz isn't available or a given diagram
  can't be parsed; `off` always uses the plain-text fallback, even with
  Graphviz installed.

## Math handling

Standard LaTeX delimiters are recognized: `$$...$$`, `\[...\]` (block),
`\(...\)`, and `$...$` (inline). One messy-copy-paste pattern is also
recognized specifically, since it's what ChatGPT-style copy/paste commonly
produces instead of real delimiters: a lone `[` on its own line, then
content, then a lone `]` on its own line. That pattern is ambiguous with
other legitimate Markdown bracket usage (a multi-line array or checklist
outside a code fence, say), so it's only treated as math when the content
also contains an actual math signal (a LaTeX command, `^`/`_`/`{}`/`|`, or a
relation) — the same conservative gate used for bare `$...$`. A rendered
fraction bar or setext-heading underline that got flattened to a lone run
of `=` characters inside one of these blocks (e.g. `cosine(A,B)\n=====\n\nfrac{...}`)
is collapsed to a single `=` rather than dropped, to preserve what it
actually meant. No other bracket-adjacent guessing is attempted beyond this
one specific, recognizable pattern.

Beyond that, there's no attempt to guess-parse arbitrary messy math — if
your source doesn't match one of the recognized patterns, wrap it in
`$$...$$` yourself before converting.

- **Block math** (`$$`, `\[...\]`) always renders as a cropped image via
  matplotlib's built-in `mathtext` engine (no system LaTeX install needed).
  Every equation image is sized in `em` relative to a reference glyph
  render, not scaled to fill a percentage of the page width — so a short
  equation like `8` and a long one render at the *same* glyph size, matching
  body text, rather than the short one ballooning to fill the container and
  the long one shrinking to fit. A fraction or other multi-line expression
  is proportionally taller (e.g. `~1.35em`) than a flat one-line equation,
  the same way it would be in real typeset math — it isn't forced to a
  single fixed height.
- **Inline math** (`\(...\)`, `$...$`) renders as plain `<sup>`/`<sub>` text
  when it's simple enough (`x^2`, `x_i`, `H_2O`) — anything with a LaTeX
  macro (`\frac`, `\sum`, `\sqrt`, ...) or multiple operators falls back to
  a small inline image instead.
- Bare `$...$` is deliberately conservative about currency: `$5$` alone
  won't be treated as math (pure numbers are rejected), but wrapping a
  price so it also contains a math signal (a backslash, `^`, `_`, `{}`,
  `|`, or a relational operator) could misfire — avoid bare `$` for
  currency where ambiguity is possible.
- `\boxed{...}` (GPT's common "final answer" wrapper) is handled
  specially, since `mathtext` doesn't support the macro directly: the inner
  expression is rendered normally and boxed with a CSS border instead, so
  it still comes out as a real equation image rather than falling back to
  raw text.
- If `mathtext` still can't render a particular expression (it doesn't
  support `align`/`cases` environments or some other macros), the raw
  LaTeX source is shown instead in a small shaded monospace box rather than
  crashing the conversion. GPT-style output typically already breaks
  multi-step derivations into independent `$$...$$` blocks rather than a
  single `align` block, so this is a rare fallback in practice.
- HTML input is passed through as-is with **no** math-delimiter scanning
  (it's assumed to already be final markup).

## Font sizes

Body text and headings are set four sizes smaller than the default (`body`'s
`font-size: 0.75em`, roughly 16px → 12px equivalent; headings are already
`em`-relative to body, so they scale down proportionally with no separate
edit needed). Code blocks, math-fallback text, and equation images are each
pinned back to their *original* absolute size with a compensating
`font-size` (the inverse factor, `1/0.75`) — that's the `/* cancels body's
smaller base */` comments next to `.codehilite pre`, `code.math-fallback`,
and `.eqimg`/`.eqimg-inline` in `build_css()`. To adjust further, change
`body`'s `font-size` and recompute those three compensating values as
`original_em / new_body_factor`. Inline code (`code.inline-code`) is the one
exception — it's set to `1em` deliberately, so short inline snippets like
`` `demo_passthrough_chain()` `` sit flush with the surrounding body text
instead of visually popping out larger.

## Code blocks

Fenced code blocks (` ```python `) get Pygments syntax highlighting in a
bordered box (the `native` Pygments style — a terminal-dark background
with a warm, muted palette: orange keywords, green strings, ...) with
wrapped (not scrolling) long lines — EPUB reflow has no horizontal-scroll
mechanism, so wrapping is the only sane choice on a narrow e-ink screen. To
use a different Pygments style, change the `style="native"` argument to
`HtmlFormatter(...)` in `md_to_kindle.py` (any name from
`pygments.styles.get_all_styles()` works). If you switch between a
light-background and dark-background style, also check the `.codehilite`
border color a few lines below it in `build_css()` — it's set by hand to
whatever reads well against the current style's background, not derived
automatically.

## Images

Local image files (PNG, JPEG, GIF, WEBP, BMP, SVG) referenced from Markdown
(`![alt](photo.png)`) or HTML (`<img src="photo.png">`) are embedded into
the EPUB, so they actually show up on Kindle instead of a broken image
reference. Relative paths resolve against the input file's own directory,
so a pasted screenshot works as long as its image file sits next to the
`.md`/`.html` file that references it (e.g. both in `inputs/`). A missing
local file is left unembedded with a warning printed to stderr rather than
failing the whole conversion.

Remote `http(s)://` images are fetched (10s timeout, 20MB cap, a
browser-like User-Agent) and embedded the same way as a local image if the
fetch succeeds and the response actually validates as an image. If the
fetch fails for any reason (network error, timeout, HTTP error, non-image
content, oversized or corrupt data), the conversion isn't failed either —
a small inline placeholder (`[Image could not be loaded: ...]`, using the
`alt` text when present) is inserted in its place, along with a warning
printed to stderr, so a broken remote reference is visible instead of
silently vanishing. `data:` URIs are still **not** fetched/embedded.

SVG is handled as its own case rather than through the same PNG/JPEG/GIF/
WEBP/BMP path: it isn't a Pillow-decodable raster format, so there's no
pixel-decode validation for it (local or remote) — a remote `.svg` is
instead validated with a lightweight "does this actually look like an
`<svg>` document" content sniff, since some servers report a generic or
wrong `Content-Type` for it. An inline `<svg>...</svg>` element embedded
directly in a page's markup (as opposed to referenced via `<img src>`) is
**not** handled — see the `--url` section above for what that means in
practice (it's usually decorative site chrome, not article content, so
this is rarely a real loss).

You can also point the converter directly at a bare image file
(`python3 md_to_kindle.py photo.png`) to wrap it in a minimal one-page
EPUB. In practice this is rarely necessary, since Send to Kindle already
accepts JPEG/PNG/GIF files directly without any conversion — it's mainly
useful when an image needs to sit alongside other converted content.

## Tables and nested lists

Nested/indented bullet or numbered lists carry through at any depth for
both Markdown (`sane_lists`) and HTML input, since HTML's own `<ul>`/`<ol>`
nesting maps directly — this matters most for `--url`/hand-saved HTML
input, where nesting is whatever the source page's real markup says
rather than something inferred from indentation.

Tables preserve `colspan`/`rowspan`, and a header row is recognized even
without an explicit `<thead>` wrapper as long as its cells are all `<th>`
(common in hand-authored or CMS-exported HTML, which doesn't always bother
with the wrapper tag the way Markdown's own table renderer does). A table
cell holding more than plain text/inline formatting — a paragraph plus a
bullet list, say, which shows up in real-world feature-comparison tables —
renders that block content in place inside the cell instead of flattening
it into a run of undifferentiated text.

More generally, a wrapper tag the converter doesn't otherwise recognize
(a scraped page's `<div>`/`<section>`/`<time>`/`<figcaption>`/...) is
searched for real block-level content at any depth before being given up
on — generated site markup commonly wraps the actual paragraphs/headings
in several layers of styling-only `<div>`s, and a check that only looked
at immediate children would misjudge all of that as "no content here."

## Multi-round Q&A transcripts

If the source is a pasted question/answer transcript (e.g. copied out of a
GPT conversation), mark each turn with a standalone `**Question:**` or
`**Answer:**` paragraph (the colon is optional, case doesn't matter):

```markdown
**Question:**

What is composition in OOP?

**Answer:**

Composition means one object contains another as an attribute...

**Question:**

How is that different from inheritance?

**Answer:**

Inheritance describes an "is-a" relationship instead...
```

Each marker starts a labeled, styled turn running through the content that
follows it, up to the next marker (or an H1, or the end of the document) —
Questions render as `Question 1`, `Question 2`, ... in a light callout
bubble; Answers get a small `Answer 1`, `Answer 2`, ... caption and
otherwise render as normal flowing text (see `inputs/qa_multi_round_example.md`
for a worked example). This is purely additive: a document that never uses
these markers renders exactly as it did before — no marker, no grouping.

`## Prompt:` / `## Response:` headings (what some GPT-export tools produce
instead of `**Question:**`/`**Answer:**`) are recognized the same way, at
any heading level (`##`–`######`) — no manual find/replace needed before
converting. They're folded into the same `Question N` / `Answer N` styling
rather than kept as a separate "Prompt"/"Response" label, so a transcript
can mix both conventions and still render consistently. `#` (H1) is
excluded since that's the hard chapter boundary above.

## Chapters

Every top-level H1 (`# Heading`) starts a new EPUB chapter (used for the
table of contents); content stays in one chapter until the next H1. If the
document has no H1 at all, everything is a single chapter. **H1 is
therefore a hard chapter boundary, not just a heading style** — a
transcript pasted with internal `# Subsection` headers that were only ever
meant as visual dividers within one answer (common in chat-tool exports)
will each start their own chapter. If you don't want that, demote those
headers to `##`/`###` before converting. Content appearing before the
first H1 (an intro paragraph, metadata, etc.) becomes its own untitled
leading chapter rather than being dropped.

## PDF input

PDF structure recovery is heuristic, not exact: headings are inferred from
font-size/bold jumps relative to the document's median body text size, and
tables use PyMuPDF's built-in `find_tables()` detector. This works well for
PDFs with visible table rules but can under-detect borderless/whitespace
tables, and can occasionally misread stray characters (e.g. a literal `|`
in body text) as a column boundary. No LaTeX/math scanning is done for PDF
input — math in a PDF stays as extracted text.

## Verifying output

- A quick structural sanity check: re-open the file with
  `ebooklib.epub.read_epub(path)` and confirm it parses without error.
- Visual check: `open outputs/whatever.epub` previews it in macOS's Books
  app. This is a **different renderer than Kindle's own** (KFX, produced
  server-side by Send to Kindle), so it validates structure and rough
  layout, not pixel-identical Kindle rendering — the real test is sending
  the file through Send to Kindle to an actual device.
- CSS is kept to a conservative, well-supported subset (no flexbox/grid/
  custom fonts) for the same reason: there's no local way to test against
  Send to Kindle's actual KFX conversion. If something looks off after a
  real on-device test, the CSS in `build_css()` is the first place to look.
