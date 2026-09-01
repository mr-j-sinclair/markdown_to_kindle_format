# Plan: Add PDF export to markdown_to_kindle

## Goal
`md_to_kindle.py` should be able to output `.pdf` in addition to `.epub`,
with "Page X of Y" numbering and document metadata (title/author/date) in
headers/footers — reusing the good parts of the sibling `markdown_to_pdf`
project (`/Users/sinclairmacbook/code/markdown_to_pdf/md_to_pdf.py`), while
fixing that project's weak spots (no HTML input, poor image handling, no
Mermaid/sequence diagram support) by piggybacking on functionality
`md_to_kindle.py` already has. The existing EPUB output path must not
regress.

## Key architectural insight (already reused, not to be rebuilt)
`convert()` in md_to_kindle.py builds one resolved intermediate
representation before any output-format-specific rendering happens:

```
input file --loader--> soup (BeautifulSoup DOM)
  --> resolve_math_placeholders   (LaTeX -> <sup>/<sub> or <img class=eqimg>)
  --> wrap_qa_turns               (Q&A marker paragraphs -> styled <div>)
  --> resolve_local_images        (local <img src> -> embedded bytes)
  --> resolve_remote_images       (http(s) <img src> -> fetched + embedded)
  --> resolve_mermaid_diagrams    (```mermaid fence -> <img> via Graphviz/matplotlib)
=> resolved `soup` + `image_registry` (rel_path -> bytes, media_type)
```

Today only one consumer reads that resolved soup: `split_chapters()` +
`render_block()` -> XHTML -> `EpubBuilder` -> `.epub`. PDF export adds a
**second** consumer of the *same* resolved soup + image_registry: a new
reportlab-based renderer -> `.pdf`. This is why HTML input, images, math
images, and Mermaid diagrams "just work" for PDF too, with no new
extraction/parsing logic — they're already `<img>`/`<sup>`/`<sub>` tags in
the soup by the time a PDF renderer would see it.

## Plan
1. **CLI / dispatch**
   - Output format is chosen by the output file's extension (`.pdf` vs
     `.epub`), plus a new `--format {epub,pdf}` flag for when output is
     omitted (default stays `epub`, so no existing invocation changes
     behavior).
   - `convert()` gains a `output_format` branch: build the resolved
     soup/image_registry exactly as today, then either call the existing
     `EpubBuilder` path or a new `render_pdf(...)` path.

2. **New PDF renderer** (ported from `md_to_pdf.py`'s
   `MarkdownPdfRenderer`/`NumberedCanvas`, adapted to walk the *resolved*
   soup instead of raw markdown):
   - Headings h1-h4, paragraphs with inline b/i/code/a/**sup/sub** (new
     vs. md_to_pdf — needed for inline math).
   - Images: `<img>` tags (content images, math images, mermaid images all
     look the same at this stage) rendered via reportlab `Image` flowable,
     bytes pulled from `image_registry`, scaled to content width, capped
     height like the EPUB CSS does.
   - Code blocks: shaded/bordered monospace box (reuse md_to_pdf's
     Unicode-capable monospace font registration); Pygments highlighting
     is a nice-to-have, plain text is the safe baseline (matches
     md_to_pdf's current behavior).
   - Tables: reuse md_to_pdf's proportional column-width logic.
   - Lists: reuse md_to_pdf's nested bullet rendering.
   - Blockquote, hr, figure/figcaption: new, simple reportlab equivalents.
   - Q&A turn divs (`qa-turn`/`qa-question`/`qa-answer`): simple colored
     box / caption treatment, mirroring the EPUB CSS intent.
   - Math fallback (`code.math-fallback`) / image fallback
     (`code.img-fallback`): shaded box, same as EPUB's degraded case.

3. **Page numbering + header/footer metadata**
   - Port `NumberedCanvas` (defers footer draw to `save()` so it knows the
     final page count) for "Page X of Y" in the footer.
   - Add a **header** (new vs. md_to_pdf, which only had a footer): document
     title on the left/center, small rule underneath.
   - Footer: left = author/subtitle/date metadata (whichever is set),
     right = "Page X of Y".
   - All metadata (`--title`/`--author`/`--subtitle`) already flows into
     `convert()` today — just also pass it to the PDF path.

4. **Dependencies**
   - Add `reportlab` to `requirements.txt` (not currently a dependency of
     this project).

5. **Non-goals / explicitly out of scope**
   - No change to EPUB output behavior or CSS.
   - No attempt to make PDF pixel-identical to EPUB — different medium,
     different constraints (fixed page size vs. reflow).
   - Not porting md_to_pdf.py's own CLI/`main()` — it stays a separate,
     untouched project; only its *techniques* are reused/ported into
     md_to_kindle.py.

## Workflow for this task (per user instruction)
1. ~~Plan persisted (this file).~~
2. Sub-agent **design**: review this plan + both source files, propose a
   concrete, file-and-function-level design (exact new functions, where
   they live in md_to_kindle.py, exact CLI surface). I review and give
   feedback, iterating until the design is solid.
3. Sub-agent **implement**: build the reviewed design directly into
   `md_to_kindle.py` (+ `requirements.txt`, `README.md`).
4. Sub-agent **test**: verify (i) PDF output is correct — page X of Y,
   header/footer metadata, images/math/mermaid embedded — across a sample
   of real files in `inputs/`; (ii) EPUB output is unchanged/unbroken for
   the same sample; (iii) report findings back.
5. Iterate across agents 2-4 based on test findings until done.

## Finalized design (reviewed, implementation-ready)

### 1. New functions/classes
New banner-commented section between "EPUB assembly" (~line 2519-2576) and
"Orchestration" (~line 2578):

```python
def _register_mono_font() -> str: ...       # module-level, port of md_to_pdf.py lines 39-51
_PDF_MONO_FONT_NAME = _register_mono_font()

def build_pdf_styles() -> StyleSheet1: ...   # port of build_styles() + MathFallback/ImgFallback/
                                              # Caption/QaLabel/QaQuestionBody/BlockquoteBody styles

class PdfRenderer:
    def __init__(self, styles, image_registry): ...
    def _image_bytes(self, rel_path) -> bytes | None: ...
    def render_inline(self, tag) -> str: ...                # port of inline_html + sup/sub (verify tag name!)
    def render_image(self, img_tag) -> Flowable: ...
    def render_table(self, tag) -> Flowable | None: ...      # + block-content-cell dispatch
    def render_code_block(self, tag) -> Flowable: ...        # ported verbatim, plain-shaded, no highlighting
    def render_list(self, tag, ordered=False, level=0) -> list[Flowable]: ...
    def render_blockquote(self, tag) -> Flowable: ...         # recurses build_story's own dispatch
    def render_qa_turn(self, tag) -> list[Flowable]: ...
    def render_figure(self, tag) -> list[Flowable]: ...
    def build_story(self, soup, title=None, subtitle=None, author=None) -> list[Flowable]: ...

class PdfNumberedCanvas(pdfcanvas.Canvas):
    doc_title = ""
    footer_left_text = ""
    def _draw_header(self, page_count): ...   # suppressed on page 1 (self._pageNumber > 1)
    def _draw_footer(self, page_count): ...

def convert_to_pdf(soup, image_registry, output_path, title=None, author=None, subtitle=None) -> None: ...
```

### 2. CLI / dispatch
- `main()`: add `--format {epub,pdf}` (default `None`). Output format resolves
  from the output path's **extension first**; `--format` is only the fallback
  when output is omitted/extensionless. Default is always `"epub"` — every
  existing invocation is behavior-identical.
- `convert()` gains `output_format="epub"`. Soup/image_registry construction
  (math, QA, images, mermaid) stays one identical code path regardless of
  format; only the tail branches into `convert_to_pdf(...)` vs the existing
  `EpubBuilder` code (untouched).

### 3. Element -> flowable mapping
h1-h4 -> Paragraph (h4->H3 style); p (b/i/code/a/sup/sub) -> Paragraph via
render_inline; img (content/math/mermaid, indistinguishable at this stage)
-> render_image; pre/code fence -> shaded Table-wrapped Preformatted, no
syntax color (v1); table -> render_table w/ block-content-cell dispatch;
ul/ol -> render_list (nested); blockquote -> render_blockquote (recurses
build_story's dispatch, NOT a `<p>`-only loop); hr -> HRFlowable;
figure/figcaption -> image + italic caption; div.qa-turn.qa-question ->
label + tinted boxed Table; div.qa-turn.qa-answer -> label + plain
paragraphs; code.math-fallback/img-fallback -> handled inline (nested in
`<p>`) via a shaded `<font>` span in render_inline.

### 4. Header/footer
Port `NumberedCanvas`'s defer-to-`save()` mechanism verbatim. Add
`_draw_header` alongside `_draw_footer`. Header: doc title, left-aligned,
8-9pt, thin rule below, **suppressed on page 1** (DocTitle paragraph
already carries it there); `topMargin` increases (~1.0in) for header room.
Footer: left = author/subtitle metadata (falls back to input filename),
right = "Page X of Y" — same layout as existing NumberedCanvas.

### 5. Image scaling
`_image_bytes(rel_path)`: dict from `image_registry.images`. Math images
(`eqimg`/`eqimg-inline`): reuse the existing inline `style="height:{em}em"`
signal already set by `math_replacement_nodes` — regex the em value,
multiply by Body style's point size, derive width from PNG aspect ratio
(PIL). Everything else: `scale = min(1.0, CONTENT_WIDTH/w, MAX_IMAGE_HEIGHT/h)`,
never upscale. New `MAX_IMAGE_HEIGHT` constant (~8.5in). Center via 1-cell
Table + HALIGN (same trick as code blocks). SVG: no `svglib` dependency —
falls back to the same `[Image could not be loaded]` placeholder as other
failure paths; narrow blast radius (math/mermaid images are always PNG).

### 6. markdown_to_pdf/ project
Stays completely untouched — separate, independently-runnable project.
This task is 100% additive to md_to_kindle.py alone (duplication of
promote_inline_dash_sublist/ensure_blank_line_before_blocks already exists
there deliberately, per that code's own banner comment, and isn't
something to dedupe as part of this task).

### 7. Dependencies
Add `reportlab` to requirements.txt (no version pin).

### 8. Implementation notes / must-not-regress
1. Blockquote must recurse through build_story's own per-element dispatch
   (matching EPUB's `render_block`), not a simplified `<p>`-only loop.
2. **Verify reportlab's sup/sub markup tag empirically** (small standalone
   test render) before wiring into render_inline — don't assume `<super>`
   vs `<sup>`.
3. Table cells with block content (nested list/multiple paragraphs) need
   the same inline-vs-block dispatch EPUB's render_table_cell has — bounded
   to cell-content extraction only, column-width logic stays as ported.
4. No Pygments syntax highlighting for PDF code blocks in v1 — known
   limitation, must be noted in the implementation summary AND README.md.
5. Header suppressed on page 1 via `if self._pageNumber > 1`.
6. Long tables/lists across page breaks: no special handling needed
   (reportlab Table's default splitByRow=True covers it).

## Status
- [x] Plan written
- [x] Design reviewed (finalized above, implementation-ready)
- [x] Implemented (see "Implementation notes" below)
- [x] Tested / validated (see "Validation notes" below)

## Validation notes (independent test pass)

Independently verified (not just re-running the implementer's own smoke
tests): 5 diverse real inputs (math-heavy w/ tables, scraped HTML w/
image, 14-diagram Mermaid doc, single-page Q&A transcript, blockquote+
table doc) through both dispatch mechanisms (`.pdf` extension and
`--output-format pdf`), plus 2 synthetic follow-ups for paths no real
input happened to exercise.

- Page X of Y: `Y` matched true page count exactly on every doc (3/3,
  18/18, 1/1, 8/8, ...).
- Header suppressed on page 1, present from page 2 on, on every
  multi-page doc.
- Footer metadata: "Author · Subtitle" / single value / filename
  fallback all correct; no leaked `"None"` string in any case.
- Images (content, math, Mermaid) all embedded and byte-valid, no
  placeholder leakage.
- Tables (incl. header-row repeat across a page break), code blocks,
  Q&A labels, blockquotes all verified via direct text extraction.
- Table cell with a nested list: renders as real distinct bullets inside
  the cell (synthetic test — PASS).
- Local SVG image: falls back to `[Image not renderable in PDF (SVG): ...]`
  placeholder, no crash, no silent rasterization attempt (synthetic
  test — PASS).
- EPUB regression check: all EPUB outputs re-parse via
  `ebooklib.epub.read_epub`; diffed against pre-existing `outputs/`
  files — the only differences found were test-harness artifacts (a
  test run omitting `--subtitle`, `--mermaid-images off` intentionally
  passed) or a pre-existing discrepancy in old cached output unrelated
  to this change (an old `outputs/AI Memory Overview.epub` had extra
  blank lines in a code block not present in the source `.md` — new
  output is actually more faithful to source, and predates/is unrelated
  to the PDF work).
- `--paste`, `--mermaid-images off`, and plain positional output paths
  all still work.

**Verdict: ready to ship.**

## Implementation notes (post-implementation)

Built exactly per the finalized design, with one deviation found and fixed
during my own smoke-testing (not a design flaw, an implementation-time
catch):

- **Deviation from the literal design text:** the design's §3 table
  described `<img>` handling as "pulls bytes from image_registry ... scaled"
  without distinguishing where an `<img>` can appear. In practice, inline
  math with a LaTeX macro (e.g. `$A \cdot B$` in a table, common in the
  sample inputs) renders as an `<img class="eqimg-inline">` *nested inside
  a `<td>` or `<li>`*, not only inside a top-level `<p>`. My first pass only
  special-cased image-splitting in the top-level paragraph handler, and
  those nested cases silently degraded to a `[image]` placeholder instead
  of the real equation image. Fixed by generalizing the "split inline
  children around any `<img>`" logic into a shared
  `PdfRenderer._render_inline_run()` helper, used by paragraphs, table
  cells (both branches), and list items alike. Confirmed fixed via a
  PyMuPDF-based check on the actual generated PDF (no `[image]` placeholder
  text remains; 10 real embedded images where there were previously ~1).
- **Sup/sub tag** (§8.2): empirically confirmed against reportlab 5.0.1
  (verified via PyMuPDF glyph-baseline inspection, not guessed) — both
  `<super>` and `<sup>` render as true superscript, `<sub>` as true
  subscript. Used the canonical `<super>`/`<sub>` pair in `render_inline`.
- Everything else (blockquote recursion through the shared per-element
  dispatch, table cell block-content dispatch, header suppressed on page 1,
  no Pygments/no SVG for v1, `reportlab` added to requirements.txt,
  `markdown_to_pdf/` left untouched) implemented as specified.
- New CLI flag is `--output-format {epub,pdf}`, not `--format` as an
  earlier draft assumed — `--format` was already taken (controls how
  `--paste` content is interpreted as md/html/txt). Extension-based
  resolution still takes priority; default is still `epub`.
- Smoke-tested (not exhaustive — a dedicated validation pass should still
  cover more inputs) across: a math-heavy doc (table-cell + list-item +
  paragraph inline math, block equations), a Mermaid-diagram-heavy doc (14
  images across 18 pages), a raw scraped-HTML article (content images +
  SVG icon fallback), a Q&A-transcript doc, and a blockquote-containing
  doc — all produced valid PDFs with correct page numbering
  ("Page X of Y"), header/footer metadata, and no crashes. EPUB output
  re-verified unchanged (re-parses cleanly via `ebooklib.epub.read_epub`)
  for the same inputs.
