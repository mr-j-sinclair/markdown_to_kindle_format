---
name: kindle-doc-prep
description: Detailed Markdown-preparation procedures for this repo's (markdown_to_kindle_format) md_to_kindle.py -> EPUB/Kindle pipeline -- ordered-list normalization edge cases, the six-line ASCII-diagram-to-Mermaid threshold and supported Mermaid subset, image/video capture fallbacks, label-plus-explanation bullet splitting, full .eml email parsing and attachment handling, and the linked social-post/article two-section structure. This repo's CLAUDE.md requires this skill to be invoked explicitly -- not left to auto-triggering -- before any of that prep work: normalizing or restructuring a list, deciding whether a diagram needs to become Mermaid, ingesting a raw .eml file, embedding an image/video frame, or structuring a social post that links to an article. Always invoke this when working in or on behalf of the markdown_to_kindle_format project for that kind of content prep.
---

# Kindle document preparation

These are the judgment-heavy "how do I prepare this specific kind of content"
procedures for the `markdown_to_kindle_format` repo's conversion pipeline.
The always-loaded, non-negotiable rules (EPUB-vs-PDF default, never alter an
author's wording, folder/naming conventions, Send-to-Kindle delivery
defaults, copyright disclosure, sub-agent verification safety practice) live
in that repo's own `CLAUDE.md` and are not repeated here — read that file
first for the contract this skill operates under.

## Ordered lists

- Both `N.` and `N)` markers must render as genuine `<ol>` lists via
  `normalize_paren_ordered_lists()` in `md_to_kindle.py`, run automatically
  inside `load_markdown()` before `markdown.markdown()` — this is software
  behavior, not something to hand-edit.
- Keep that normalizer general if you ever touch it; don't tailor it to one
  input file.
- Unless regression testing justifies a change, it only converts `N)` to
  `N.` when the line is preceded by a blank line or another `N)` item — a
  parenthetical mid-sentence reference like "see step (2) above" is left
  alone by design.
- After changing list-detection logic, verify `inputs/Second_brain_Introduction.md`
  specifically; its mix of `N)` and ordinary `1.` lists must both keep
  rendering correctly.

## List presentation (manual — the software does not do this)

Unlike ordered-list normalization and export-timestamp rewriting (both
automated in `load_markdown()`), splitting label-plus-explanation bullets is
a judgment call you make by hand while preparing the source Markdown:

- In authored documents, split a label-plus-explanation bullet into a parent
  and nested bullet: `- **Header:**` followed by `  - explanation`.
- Apply this when a bullet (or numbered item) opens with a bold run-in
  heading followed by explanatory text — whether that text originally sat
  on the same line or the next one. A run-in heading is bold text that
  stands on its own:
  - a label ending in a colon or dash (`**Keys:** …`, `**Keys** – …`), or
  - a complete bold sentence/phrase ending in `.`/`?`/`!`
    (`**Offline fallback.** Setting …`).
  Keep the bold text, including its punctuation, as the parent line.
- For a numbered item, keep the `N.` parent and indent the nested bullet
  three spaces (`1. **Label:**` / `   - explanation`) so numbering stays
  intact.
- Do not split when the bold text is just the opening words of a sentence
  that continues past it (`**Facts are stored individually**, not as one
  blob.`, `**Live mode** makes calls to …`); that would break the author's
  sentence apart.
- Do not split a bold-only bullet with nothing after it.
- Indent the nested bullet by exactly two spaces (`load_markdown()` calls
  `markdown.markdown()` with `tab_length=2`, so a different indent won't
  nest correctly).
- After making this change, confirm the output XHTML actually contains a
  nested `<ul><li>` structure — don't assume the split rendered as intended.
- For GPT/Claude chat exports specifically, spot-check every "Question N"
  and "Prompt N" box for separated line items, since those boxes are where
  this pattern shows up most often.
- Don't assume `normalize_paren_ordered_lists()` (or any existing
  normalizer) covers other possible list markers such as bare `N`, `a.`, or
  `a)` — those aren't handled automatically.

## Diagrams

- An authored ASCII flow or box diagram longer than six total lines must
  become a Mermaid flowchart instead of staying as ASCII art, since ASCII
  diagrams that long tend to render illegibly on an e-ink Kindle screen.
- ASCII diagrams of six lines or fewer may remain in a plain code fence —
  don't convert those unnecessarily.
- When authoring a Mermaid flowchart, stay within the subset this repo's
  renderer actually supports:
  - Headers: `flowchart TB|TD|BT|RL|LR`
  - Node IDs: `[A-Za-z_][\w-]*`
  - Shapes: `[rect]`, `{diamond}`, `((circle))`, `(rounded)`
  - Edges: `-->`, `-.->`, `==>`, `<-->`, `<==>`, optionally with
    `|edge label|`
- Keep node labels short; put detailed prose in adjacent regular bullets
  rather than cramming it into the diagram itself.
- Whenever authoring or replacing a diagram, use a separate sub-agent to
  confirm the general spirit/shape matches the original and nothing
  important is conceptually wrong or missing — an exhaustive
  entity-by-entity audit is unnecessary. For exactly how to run that check
  safely (snapshotting the pre-edit file before it's touched, giving the
  verifier both versions to read itself rather than a paraphrase), follow
  the "Sub-agent verification" section of this repo's `CLAUDE.md` — that
  rule lives there because it's a cross-cutting safety practice, not
  specific to diagrams.

## Images and videos

- For an image that can't be embedded normally, try these fallbacks in
  order: download it directly; if that's blocked, capture it with a browser
  screenshot and embed that file instead; if neither works, insert text at
  its original position stating that an image was present but couldn't be
  captured.
- Never silently omit an image — one of the three fallbacks above always
  applies.
- For a video, capture a representative fully-loaded frame, avoiding
  loading spinners or play-button overlays where possible.
- Label the embedded frame explicitly as a screenshot from a video, not a
  photograph — the reader should know it's a still, not a photo.

## Linked social posts and articles

This two-section structure is for one post plus the single article it
links to — the same source, split into two views. It never justifies
bundling multiple *independent* posts/links the user pasted together into
one document (see this repo's `CLAUDE.md`, "Inputs and outputs" — one
combined file for unrelated posts is the mistake to avoid).

- When a social post links to a separate detailed article, create two
  sections in this order:
  1. `## <Platform> Post`
  2. `## Actual Article`
- In the post section: include its title/headline, author, posted date,
  source URL, and the complete post text verbatim.
- In the article section: independently determine its own title, author,
  publication date, content, and images — don't assume the post author and
  the article author are the same person.
- Locate the actual article target from the original post/page first; a
  link-preview target is often more reliable than an inline shortened URL.
  Treat any user-supplied or shortened URL only as a fallback candidate —
  verify it actually resolves to the post's linked article, and never
  invent or guess a target URL.
- Apply this two-section structure even when the post and article are
  hosted on the same platform.

## Email ingestion

Prefer the original `.eml` file over a PDF printout, since PDF exports can
lose attachments and alter layout. If only a PDF printout exists, proceed
with best-effort text and link extraction under the same rules below.

- Parse `.eml` directly with `BytesParser(policy=policy.default)` and
  traverse MIME parts with `msg.walk()`.
- Inspect `text/plain`, `text/html`, inline `Content-ID`/`cid:` images, and
  parts with filenames.
- Prefer `text/plain` for the body; strip `text/html` only when no
  plain-text body exists.
- A missing `To` or `Cc` header is normal for some group messages — that's
  not a parsing failure to work around.
- Correct an obviously corrupted character only when the same mojibake
  appears in *both* the plain-text and HTML parts and the intended word is
  unambiguous. If only one MIME representation looks wrong while the other
  is readable, preserve the readable source rather than "correcting" it.
- Determine attachment handling from its actual content type and filename:
  - PDF: extract its full substantive text, page-ranging during inspection
    if necessary.
  - Attached `.eml` or `message/rfc822`: recursively apply this same email
    workflow rather than flattening it into the parent message.
  - `.docx`, `.xlsx`, `.pptx`, or another structured format: use the
    appropriate native parser or tool for that format.
  - Image: embed it directly.
  - Unparseable format: use the global screenshot-then-placeholder
    fallback (see "Images and videos" above).
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
- Link only targets confirmed in email HTML `href` values, PDF annotations,
  or elsewhere in the source; mark any unconfirmed target plainly rather
  than guessing.
- Never invent a URL, silently discard an attachment, or omit an unhandled
  image.
- Reproduce substantive or actionable attachments in full — reference
  numbers, deadlines, contact details, instructions — rather than
  summarizing them away as marketing collateral.
- When an email has a substantive attachment, use this section order:
  1. `## Body Text`
  2. `## Attachment: <filename or title>`
- Put `From`, `Date`, `Subject` (or a reference line), and the message
  itself in `Body Text`.
- Apply this same body/attachment structure recursively to attached
  emails.
- The following closed list may be silently omitted as email chrome:
  logos; spacer/tracking pixels; tokenized action buttons; boilerplate
  legal, confidentiality, or virus notices; VAT numbers. Preserve
  everything else that's human-written or potentially actionable.

## Export timestamps (context — this part is fully automated)

Chat-export `Created`/`Updated`/`Exported` timestamps are already
auto-normalized by `normalize_export_timestamps()` inside `load_markdown()`
from hardcoded US-locale `M/D/YYYY H:MM:SS` (or the unambiguous
`YYYY/M/D H:MM:SS`) to `YYYY-mm-dd HH:MM:SS`. You don't need to hand-edit
these — just know it happens so you don't duplicate the work or second-guess
the output. This normalization must never be applied to ordinary email
`From`/`Date` metadata, and in practice it isn't: the underlying regex only
matches lines explicitly labeled `Created`/`Updated`/`Exported`.
