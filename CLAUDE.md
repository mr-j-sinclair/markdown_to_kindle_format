# md_to_kindle.py — conversion fidelity rules

## Core fidelity contract

- EPUB is always the default. Do not ask which format to use.
- Produce PDF only when the user explicitly requests PDF; "convert this" or "prepare this for Kindle" means EPUB.
- When preparing source Markdown, change only structures required for correct rendering, such as list markers, blank-line separation, oversized ASCII diagrams, and header-date formats.
- Do not alter spelling, grammar, wording, or phrasing unless explicitly requested; if correct rendering would require changing the author's wording, treat that as a converter bug and fix the heuristic instead.
- Treat non-structural edits as scope creep.
- Changes to `load_markdown()` or any preprocessing helper must preserve every guarantee in this file.

## Preparation procedures

Before preparing or modifying any document content for this pipeline — normalizing lists, redrawing a diagram, ingesting an email, structuring a linked post/article, or any other Markdown-prep judgment call — invoke the `kindle-doc-prep` Skill first. Do not rely on it auto-triggering; call it explicitly at the start of that work, the same way this file is always read at the start of a session.

It covers: `N.`/`N)` ordered-list normalization edge cases, the six-line ASCII-diagram-to-Mermaid threshold and the supported Mermaid subset, image/video capture fallback order, label-plus-explanation bullet splitting, full `.eml` parsing and attachment handling, and the linked-post/article two-section structure.

## Sub-agent verification

- Before spawning a sub-agent to verify an edited diagram or other structural change against its original, snapshot the untouched file to the session scratchpad first — before any Edit call touches the real file — then give the verifier (fork or fresh) both the snapshot and the edited file to read itself; never a paraphrase pasted into the prompt. An exhaustive entity-by-entity audit is unnecessary — confirm the general spirit/shape matches and nothing important is conceptually wrong or missing.

## Verification

- `tests/` holds automated regression tests for `load_markdown()`'s deterministic normalizers (export timestamps, ordered lists, blank-line spacing, metadata line breaks) plus one end-to-end conversion check; run via `.venv/bin/python3 -m unittest discover -s tests` after changing any preprocessing helper. Visual/legibility checks (rendered EPUB appearance, Kindle readability, Mermaid image quality) remain manual.
- After changing `load_markdown()` or a preprocessing helper, also reconvert representative real inputs covering: ordinary `1.` ordered lists, `Created`/`Updated`/`Exported` chat timestamps, and Mermaid diagrams — inspect the generated EPUB XHTML by unzipping it rather than relying only on visual appearance.
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
- Name `<name>` after that item's own title/subject (slugified), never a generic or shared batch name (e.g. not `linkedin_posts_2026-09-05`); this filename becomes the Send-to-Kindle email's attachment name via `os.path.basename()`, so a generic name ships a generic attachment.
- When the user hands over several distinct sources in one request (e.g. multiple pasted links), run the full pipeline separately per source: its own `inputs/<name>.md`, its own `outputs/<name>.epub`, and, once eligible, its own separate Send-to-Kindle email. Never merge independent sources into one Markdown file or one email, even if they share a platform, date, or topic — one combined file for unrelated LinkedIn posts is the canonical mistake to avoid.

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

## Copyright-limited content

- Applies to any source (linked article, email attachment, etc.), not just social posts.
- If full text can't be reproduced for copyright reasons and is summarized or truncated, disclose that explicitly inside the output document.
- Name both the Markdown source and the converted output file to say so explicitly, e.g. append `_summary` (`article_summary.md`, `article_summary.epub`).
- Make the very first line of the output document an explicit warning that the content is summarized, immediately followed by a clickable link to the original source; don't bury this notice further down the page.
- Always include a verified working link to the original source inside the EPUB/PDF.

Keep this file at or below 150 lines; when adding a rule, merge or remove equivalent prose or move task-specific procedures to on-demand guidance.
