---
name: kindle-doc-prep
description: Markdown-preparation procedures for this repo's (markdown_to_kindle_format) md_to_kindle.py -> EPUB/Kindle pipeline -- ordered-list edge cases, label-plus-explanation bullet splitting, the six-line ASCII-diagram-to-Mermaid threshold and supported Mermaid subset, image/video capture fallbacks, email (.eml) parsing and attachment handling, and the linked social-post/article two-section structure. CLAUDE.md requires invoking this explicitly before any Markdown-prep judgment call -- restructuring a list, converting a diagram, ingesting an email, embedding an image/video frame, or structuring a post that links to an article. For church service/notices documents, also invoke order-of-service afterward. Always invoke this when working in or on behalf of the markdown_to_kindle_format project for that kind of content prep.
---

# Kindle document preparation

Read this repo's `CLAUDE.md` first; it governs fidelity, conversion,
delivery, and verification. This skill supplies content-specific
preparation procedures.

## Ordered lists

- `load_markdown()` automatically normalizes genuine `N)`/`N\)` lists to
  `N.` (`normalize_paren_ordered_lists()`); leave those markers alone. It
  recognizes consecutive items even directly after prose, but leaves a
  lone `N)` right after prose and incidental references ("see step (2)
  above") untouched, and skips fenced code.
- If you change list detection, keep the normalizer general (not tailored
  to one file) and reconvert `inputs/Second_brain_Introduction.md`; its
  mixed `N)`/`N.` lists must still render correctly.
- Other markers such as bare `N`, `a.`, or `a)` are not normalized
  automatically.

## List presentation

`promote_inline_dash_sublist()` already splits a bare bold label followed
by a spaced dash (`- **Keys** – …`, `- **Keys:** - …`) into a parent and
nested bullet. Other label-plus-explanation forms must be split by hand:

- In authored documents, split a bullet or numbered item whose bold opening
  is a standalone heading from its explanation, whether the explanation
  follows on the same line or the next. Keep the bold text and its
  punctuation as the parent.
- A standalone heading is a label ending in a colon (`**Keys:** …`) or a
  complete bold phrase ending in `.`/`?`/`!` (`**Offline fallback.**
  Setting …`).
- Indent the child two spaces beneath `- **Label:**` and three spaces
  beneath `1. **Label:**` so numbering stays intact.
- Leave bold-only items and sentence fragments unchanged, e.g. `**Live
  mode** makes calls …` or `**Facts are stored individually**, not as one
  blob.` — splitting those would break the author's sentence.
- Then follow `CLAUDE.md`'s manual XHTML checks: confirm nested lists
  after splitting, and spot-check every chat-export "Question N"/"Prompt
  N" box for separated line items.

## Diagrams

- Convert ASCII flow/box diagrams longer than six lines to Mermaid
  flowcharts; ones of six lines or fewer may stay in plain code fences.
- This applies to authored and third-party text, including chat exports:
  the diagram is structure, not wording. Keep every label verbatim (`<br>`
  for line breaks, `#quot;` for a quote mark).
- Hard gate (`find_unconverted_diagrams()`): the CLI exits 3, writing and
  sending nothing, if an oversized untagged/```` ```text ```` diagram
  remains. With Mermaid rendering enabled (the default) it also exits if a
  ```` ```mermaid ```` fence doesn't render or a flowchart has a line the
  parser would drop. Fix the source; never re-tag a diagram as code or
  strip its arrows to get past the gate.
- Small diagrams wider than 40 columns or using box-drawing glyphs are
  drawn as a monospace image automatically. Prose that merely contains an
  arrow (`Same input → same output?`) stays text.
- For new or replacement flowcharts, use this supported subset:
  - Headers: `flowchart` or `graph`, followed by `TB|TD|BT|RL|LR`.
  - Node IDs: `[A-Za-z_][\w-]*`; shapes: `[rect]`, `{diamond}`,
    `((circle))`, `(rounded)`. Avoid nested shape delimiters in labels.
  - Edges: `-->`, `-.->`, `==>`, `<-->`, `<==>`, optionally with
    `|label|`; inline labels: `A -- label --> B`, `A == label ==> B`, or
    `A -. label .-> B`.
  - Subgraphs: `subgraph ID [Title]`, `subgraph "Title"`, or
    `subgraph Title words`, closed with `end`; nesting is supported.
  - `style`, `click`, `classDef`, `direction`, and `%%` lines are ignored;
    don't rely on them to convey meaning.
- In newly authored diagrams, keep labels short and put detailed prose in
  adjacent bullets. When replacing a source diagram, preserve its labels
  verbatim.
- For every authored or replaced diagram, get an independent sub-agent
  check of its general shape and conceptual completeness. Snapshot the
  file to the scratchpad before the first edit, and follow `CLAUDE.md` →
  "Verification" → "Sub-agent checks".

## Automated rendering

- Leave source symbols unchanged: EPUB conversion maps known status marks
  to text (✅ → `[Yes]`, …) and renders other emoji as inline images.
- Start with the document's own `# Title`; it becomes the opening page and
  the Kindle library title (unless `--title` overrides it). Don't add a
  title page or manual table of contents.
- For copyright-limited summaries, the bold summary notice and verified
  source link come immediately after that `# Title` (see `CLAUDE.md`).

## Images and videos

- If normal image embedding fails, try direct download, then a browser
  screenshot. If neither works, place an explicit image-unavailable notice
  at the original position; never silently omit an image except for the
  email-chrome exclusions below.
- For video, embed a representative fully loaded frame, avoiding
  spinners/play overlays where possible, and label it explicitly as a
  screenshot from a video, not a photograph.

## Linked social posts and articles

- For one social post and its linked article, including same-platform
  articles, use `## <Platform> Post` followed by `## Actual Article`. Keep
  independent posts separate per `CLAUDE.md`.
- Include the post's title/headline, author, posted date, source URL, and
  complete text verbatim.
- Independently establish the article's title, author, publication date,
  content, and images; don't assume shared authorship.
- Locate the target from the original post/page first, checking link
  previews. Use supplied/shortened URLs only as verified fallback
  candidates; never guess the target.

## Email ingestion

Prefer the original `.eml` file over a PDF printout, since PDF exports can
lose attachments and alter layout. If only a PDF printout exists, proceed
with best-effort text and link extraction under the same rules below.

- Parse `.eml` directly with `BytesParser(policy=policy.default)` and
  traverse MIME parts with `msg.walk()`.
- Inspect `text/plain`, `text/html`, inline `Content-ID`/`cid:` images, and
  parts with filenames.
- Prefer `text/plain` for the body, but inspect HTML for links, images, and
  fidelity differences. Use the readable representation when one is
  corrupted; for church notices with collapsed plain-text lines, follow
  `order-of-service`'s HTML-layout rule.
- A missing `To` or `Cc` header is normal for some group messages — that's
  not a parsing failure to work around.
- Correct an obviously corrupted character only when the same mojibake
  appears in *both* the plain-text and HTML parts and the intended word is
  unambiguous. If only one MIME representation looks wrong while the other
  is readable, preserve the readable source rather than "correcting" it.
- Determine attachment handling from its actual content type and filename:
  - PDF: extract its full substantive text, page-ranging during inspection
    if necessary.
  - Attached `.eml`/`message/rfc822`: recursively parse the message and
    preserve its body/attachment boundaries.
  - `.docx`, `.xlsx`, `.pptx`, or another structured format: use the
    appropriate native parser or tool for that format.
  - Image: embed it directly.
  - Unparseable: capture a rendered view if possible; otherwise keep its
    filename and an explicit unavailable-content notice.
- For attached PDFs, extract hyperlinks from pypdf page annotations, via
  `/Annots` → `/A` → `/URI` — don't infer URLs from visible link text, which
  can silently point somewhere else.
- Extract PDF-embedded images through `page.images`.
- Extract inline `cid:` images and download accessible plain or signed CDN
  images.
- Save extracted media beside the Markdown in `inputs/` and embed it with
  normal Markdown image syntax.
- For graphics such as QR-code charts with no extractable target, embed the
  graphic and reproduce its labels as plain text.
- Use only link targets confirmed in source HTML, PDF annotations, or other
  source material; plainly mark unconfirmed targets. Never invent a URL.
- Account for every attachment and non-exempt image. Reproduce substantive
  or actionable attachments in full — reference numbers, deadlines, contact
  details, instructions — rather than summarizing them away as marketing
  collateral.
- For generic emails with substantive attachments, use `## Body Text`, then
  one `## Attachment: <filename or title>` section per attachment. Put
  `From`, `Date`, `Subject` (or a reference line), and the message in
  `Body Text`; apply recursively to attached emails.
- For church service/notices documents, `order-of-service` owns the final
  section order.
- The following closed list may be silently omitted as email chrome:
  logos; spacer/tracking pixels; tokenized action buttons (including their
  personalised URLs); boilerplate legal, confidentiality, or virus notices;
  VAT numbers. Preserve everything else that's human-written or potentially
  actionable.

## Export timestamps

`load_markdown()` automatically normalizes chat-export
`Created`/`Updated`/`Exported` timestamps from US `M/D/YYYY` or `YYYY/M/D`
to `YYYY-MM-DD HH:MM:SS`. Don't duplicate this manually; it never touches
ordinary email `From`/`Date` metadata.
