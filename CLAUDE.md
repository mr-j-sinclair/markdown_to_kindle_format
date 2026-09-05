# md_to_kindle.py — conversion fidelity rules

## Core fidelity contract

- EPUB is always the default. Do not ask which format to use.
- Produce PDF only when the user explicitly requests PDF; "convert this" or "prepare this for Kindle" means EPUB.
- When preparing source Markdown, change only structures required for correct rendering, such as list markers, blank-line separation, oversized ASCII diagrams, and header-date formats.
- Do not alter spelling, grammar, wording, or phrasing unless explicitly requested; if correct rendering would require changing the author's wording, treat that as a converter bug and fix the heuristic instead.
- Treat non-structural edits as scope creep.
- Changes to `load_markdown()` or any preprocessing helper must preserve every guarantee in this file.

## Markdown normalization

### Ordered lists

- Both `N.` and `N)` markers must render as genuine `<ol>` lists via `normalize_paren_ordered_lists()`, run before `markdown.markdown()`.
- Keep this normalizer general; do not tailor it to one input file.
- Unless regression testing justifies a change, convert `N)` only when preceded by a blank line or another `N)` item.
- Verify `inputs/Second_brain_Introduction.md` after changing list detection; its `N)` and ordinary `1.` lists must both remain correct.

### Export timestamps

- Normalize chat-export `Created`, `Updated`, and `Exported` values (via `normalize_export_timestamps()` from `load_markdown()`) from hardcoded US-locale `M/D/YYYY H:MM:SS` to `YYYY-mm-dd HH:MM:SS`.
- Do not auto-detect the source locale; never leave the date component in `M/D/YYYY` form.
- Retain the time component because it may be the only difference among the three timestamps.
- Do not apply this chat-export normalization to ordinary email `From`/`Date` metadata.

### List presentation

- In authored documents, split label-plus-explanation bullets into a parent and nested bullet:
  `- **Header:**` followed by `  - explanation`.
- Apply this to a bold label followed by a colon or dash and substantial explanatory text, whether originally on the same line or the next.
- Do not split a short bold-only bullet with no colon/dash-separated elaboration.
- Indent the nested bullet by exactly two spaces; `load_markdown()` uses `tab_length=2`.
- Confirm the output XHTML contains a genuinely nested `<ul><li>` structure.
- For GPT/Claude exports, spot-check every "Question N" and "Prompt N" box for separated line items.
- Do not assume `normalize_paren_ordered_lists()` covers other possible markers such as bare `N`, `a.`, or `a)`.

## Diagrams and media

### Diagrams

- An authored ASCII flow or box diagram longer than six total lines must become a Mermaid flowchart.
- ASCII diagrams of six lines or fewer may remain in a plain code fence.
- Use only the supported Mermaid subset:
  - Headers: `flowchart TB|TD|BT|RL|LR`
  - Node IDs: `[A-Za-z_][\w-]*`
  - Shapes: `[rect]`, `{diamond}`, `((circle))`, `(rounded)`
  - Edges: `-->`, `-.->`, `==>`, `<-->`, `<==>`, optionally with `|edge label|`
- Keep node labels short; place detailed prose in adjacent regular bullets.
- Whenever authoring or replacing a diagram, use a separate sub-agent to confirm the general spirit/shape match the original and nothing important is conceptually wrong or missing; an exhaustive entity-by-entity audit is unnecessary.

### Images and videos

- For an image that cannot be embedded normally, try these fallbacks in order:
  1. Download it directly.
  2. If blocked, capture it with a browser screenshot and embed that file.
  3. If neither works, insert text at its original position stating that an image was present but could not be captured.
- Never silently omit an image.
- For a video, capture a representative fully loaded frame, avoiding spinners or play-button overlays where possible.
- Label the embedded frame explicitly as a screenshot from a video, not a photograph.

## Verification

- This repository has no automated test suite; inspect generated EPUB XHTML manually by unzipping the EPUB rather than relying only on visual appearance.
- After changing `load_markdown()` or a preprocessing helper, reconvert representative inputs covering:
  - Correct ordinary `1.` ordered lists
  - `Created`/`Updated`/`Exported` chat timestamps
  - Mermaid diagrams
- For label/detail bullets, verify real nested `<ul><li>` output.
- For chat exports, inspect rendered Question/Prompt boxes for intact line separation.
- For Mermaid work, inspect the rendered image for Kindle legibility and obtain the independent semantic check required above.

## Inputs and outputs

- Put every generated human-readable Markdown deliverable—summary, report, or write-up—in `inputs/`.
- Convert it using the virtual-environment interpreter, with an `.epub` or `.pdf` destination per the output-format rules above:
  `.venv/bin/python3 md_to_kindle.py inputs/<name>.md outputs/<name>.epub`
- Always use `.venv/bin/python3`; system Python lacks required dependencies such as Graphviz and may fail silently.
- `outputs/` contains only converter-rendered EPUB/PDF files; never place raw `.md` source there.
- Whether to append `--no-send-to-kindle` depends on what's being converted; see "Send-to-Kindle delivery."

## Send-to-Kindle delivery

- Delivery logic lives only in `kindle_delivery.py`; conversion code must never import `smtplib`/`keyring` directly.
- Never hardcode, log, print, or write the Gmail App Password anywhere—source, docs, tracebacks, or generated summaries.
- `--send-to-kindle`/`--no-send-to-kindle` default to sending after a successful EPUB conversion only; PDF output is never auto-sent, even if passed explicitly. This default is implemented in the software itself, not a Claude-side choice.
- No hidden retries and no send-deduplication; a rerun of the command re-sends by design.
- Suppress sending with `--no-send-to-kindle` only for conversions about the software itself (development/regression runs, the source-change summary doc from "Completion and source control"); any conversion the user actually asked for—an article, social post, email, note, or other content deliverable—is a real delivery, not a dev artifact, so let it auto-send.

## Completion and source control

- After implementing and verifying a bug fix or feature in a source `.py` file, or making a `CLAUDE.md`-only change, commit it and push it to `origin/main` in the same turn without waiting to be asked.
- Other cases require explicit user direction unless another instruction covers them.
- Produce a human-readable converted summary document (written to `inputs/`, converted into `outputs/`, and referenced in the reply) only for changes to source `.py` files, dependencies, or the runtime environment/virtual environment.
- Do not create a summary document for Markdown preparation, formatting/list/diagram fixes, reconversion, rendering-only edits, or `CLAUDE.md` edits; a chat reply is sufficient.

## Linked social posts and articles

- When a social post links to a separate detailed article, create two sections in this order:
  1. `## <Platform> Post`
  2. `## Actual Article`
- In the post section, include its title/headline, author, posted date, source URL, and complete post text verbatim.
- In the article section, independently determine its title, author, publication date, content, and images.
- Do not assume the post author and article author are the same person.
- Locate the actual article target from the original post/page first; the link-preview target is often more reliable than an inline shortened URL.
- Treat a user-supplied or shortened URL only as a fallback candidate; verify it resolves to the post's actual linked article, and never invent or guess a target URL.
- Apply the two-section structure even when the post and article are hosted on the same platform.

## Copyright-limited content

- Applies to any source (linked article, email attachment, etc.), not just social posts.
- If full text can't be reproduced for copyright reasons and is summarized or truncated, disclose that explicitly inside the output document.
- Name both the Markdown source and the converted output file to say so explicitly, e.g. append `_summary` (`article_summary.md`, `article_summary.epub`).
- Make the very first line of the output document an explicit warning that the content is summarized, immediately followed by a clickable link to the original source; don't bury this notice further down the page.
- Always include a verified working link to the original source inside the EPUB/PDF.

## Email ingestion

- Prefer the original `.eml` file over a PDF printout because PDF exports can lose attachments and alter layout.
- If only a PDF printout exists, proceed with best-effort text and link extraction under the same rules.
- Parse `.eml` directly with `BytesParser(policy=policy.default)` and traverse MIME parts with `msg.walk()`.
- Inspect `text/plain`, `text/html`, inline `Content-ID`/`cid:` images, and parts with filenames.
- Prefer `text/plain` for the body; strip `text/html` only when no plain-text body exists.
- A missing `To` or `Cc` header is normal for some group messages and is not a parsing failure.
- Correct an obviously corrupted character only when the same mojibake appears in both plain-text and HTML parts and the intended word is unambiguous.
- If only one MIME representation looks wrong while the other is readable, preserve the readable source rather than "correcting" it.
- Determine attachment handling from its actual content type and filename:
  - PDF: extract its full substantive text, page-ranging during inspection if necessary.
  - Attached `.eml` or `message/rfc822`: recursively apply this email workflow rather than flattening it.
  - `.docx`, `.xlsx`, `.pptx`, or another structured format: use the appropriate native parser or tool.
  - Image: embed it directly.
  - Unparseable format: use the global screenshot-then-placeholder fallback.
- For attached PDFs, extract hyperlinks from pypdf page annotations, using `/Annots` and `/A` → `/URI`; do not infer URLs from visible link text.
- Extract PDF-embedded images through `page.images`.
- Extract inline `cid:` images and download accessible plain or signed CDN images.
- Save extracted media beside the Markdown in `inputs/` and embed it with normal Markdown image syntax.
- For graphics such as QR-code charts with no extractable target, embed the graphic and reproduce its labels as plain text.
- Link only targets confirmed in email HTML `href` values, PDF annotations, or elsewhere in the source; mark unconfirmed targets plainly.
- Never invent a URL, silently discard an attachment, or omit an unhandled image.
- Reproduce substantive or actionable attachments in full, including reference numbers, deadlines, contact details, and instructions; do not summarize them away as marketing collateral.
- When an email has a substantive attachment, use this order:
  1. `## Body Text`
  2. `## Attachment: <filename or title>`
- Put `From`, `Date`, `Subject` or reference line, and the message itself in `Body Text`.
- Apply the same body/attachment structure recursively to attached emails.
- The following closed list may be silently omitted as email chrome: logos; spacer/tracking pixels; tokenized action buttons; boilerplate legal, confidentiality, or virus notices; VAT numbers.
- Preserve everything human-written or potentially actionable.

Keep this file at or below 150 lines; when adding a rule, merge or remove equivalent prose or move task-specific procedures to on-demand guidance.
