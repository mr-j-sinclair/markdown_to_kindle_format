# md_to_kindle.py — conversion fidelity rules

## Core fidelity contract

- EPUB is always the default; never ask which format. Produce PDF only when the user explicitly requests PDF ("convert this"/"prepare this for Kindle" means EPUB).
- When preparing source Markdown, change only structures required for correct rendering, such as list markers, blank-line separation, oversized ASCII diagrams, and header-date formats. Anything else is scope creep. The only content exceptions are those a skill explicitly lists (email-chrome omission, mojibake repair when both MIME parts agree, clearly labelled authored notes).
- Do not alter spelling, grammar, wording, or phrasing unless explicitly requested; if correct rendering would require changing the author's wording, treat that as a converter bug and fix the heuristic instead.

## Preparation procedures

Before preparing or modifying any document content for this pipeline — normalizing lists, redrawing a diagram, ingesting an email, structuring a linked post/article, or any other Markdown-prep judgment call — invoke the `kindle-doc-prep` Skill first. Do not rely on it auto-triggering; call it explicitly at the start of that work.

For a church Order of Service / notices email, also invoke the `order-of-service` Skill (after `kindle-doc-prep`); it owns that document's format, responsive-reading type preservation, and web-fetched GNT readings.

A request for the user's last/latest N LinkedIn posts means their LinkedIn *Saved* posts by default (even without "saved"); also invoke the `get-n-latest-linkedin-posts` Skill (after `kindle-doc-prep`).

The New Testament in a Year automation (`ntiy_feed.py`, `.github/workflows/ntiy_daily.yml`) is deterministic code with no Claude in the loop, so the Skill rule doesn't apply to its runs. Its cleaner may drop only boilerplate, repeated titles/labels, and empty content, and make structural/whitespace-only changes (headings, key-verse blockquotes); episode wording passes through verbatim. Its state lives on the `ntiy-state` branch; never commit it to `main`.

The Daily Facts automation (`daily_facts.py`, `.github/workflows/daily_facts.yml`) is likewise unattended code, so the Skill rule doesn't apply to its runs. Its OpenAI calls may only write prose grounded in supplied text or their own web-search results; tables, numbers and years come from code, and links are code-built or model-selected sources validated against the web-search tool's own URLs (never model-typed URLs rendered as-is). Its state lives on the `daily-facts-state` branch; never commit it to `main`.

## Verification

- Sub-agent checks: before spawning a sub-agent to verify an edited diagram or other structural change against its original, snapshot the untouched file to the session scratchpad first — before any Edit call touches the real file — then give the verifier (fork or fresh) both the snapshot and the edited file to read itself; never a paraphrase pasted into the prompt. An exhaustive entity-by-entity audit is unnecessary — confirm the general spirit/shape matches and nothing important is conceptually wrong or missing.
- After changing `load_markdown()` or any preprocessing helper (which must preserve every guarantee in this file), run `.venv/bin/python3 -m unittest discover -s tests` (normalizer + end-to-end/Kindle-safety tests). Then reconvert representative real inputs covering ordinary `1.` ordered lists, `Created`/`Updated`/`Exported` chat timestamps, and Mermaid diagrams, and inspect the generated EPUB XHTML by unzipping it rather than relying only on visual appearance.
- Inspect manually: real nested `<ul><li>` output for label/detail bullets; intact line separation in chat-export Question/Prompt boxes; Kindle legibility of rendered Mermaid images, plus the independent sub-agent check above.

## Inputs and outputs

- Put every generated human-readable Markdown deliverable in `inputs/` and convert it with the venv interpreter (`.pdf` destination only per the format rule):
  `.venv/bin/python3 md_to_kindle.py inputs/<name>.md outputs/<name>.epub`
- Always use `.venv/bin/python3`; system Python lacks the Python dependencies and may fail silently (Mermaid rendering also needs Graphviz's system `dot` binary).
- `outputs/` contains only converter-rendered EPUB/PDF files; never place raw `.md` source there.
- Name `<name>` after that item's own title/subject (slugified), never a generic or batch name (e.g. not `linkedin_posts_2026-09-05`); the filename becomes the Kindle email's attachment name.
- When re-sending an item already delivered to Kindle, write `outputs/<name>_V2.<ext>`, then `_V3`, … (check `outputs/` for the next number; keep the `inputs/` name).
- Several distinct sources in one request (e.g. multiple pasted links) → run the full pipeline per source: its own `inputs/<name>.md`, `outputs/<name>.epub`, and separate Kindle email. Never merge them, even if they share platform, date, or topic (e.g. never one combined file for unrelated LinkedIn posts). A post plus its linked article, or a combined document a skill defines, counts as one item.

## Send-to-Kindle delivery

- Delivery logic lives only in `kindle_delivery.py`; conversion code must never import `smtplib`/`keyring` directly.
- Never hardcode, log, print, or write the Gmail App Password anywhere—source, docs, tracebacks, or generated summaries.
- The software itself auto-sends after a successful EPUB conversion; PDF is never auto-sent, even with `--send-to-kindle`. No hidden retries or deduplication: a rerun re-sends by design.
- Suppress sending with `--no-send-to-kindle` only for conversions about the software itself (development/regression runs, the source-change summary doc below) or a scratchpad preview checked before the single real delivery run; any conversion the user actually asked for—article, social post, email, note, or other content—is a real delivery, so let it auto-send.

## Completion and source control

- After implementing and verifying a bug fix or feature in a source `.py` file, or making a `CLAUDE.md`-only change, commit it and push it to `origin/main` in the same turn without waiting to be asked.
- Other cases require explicit user direction unless another instruction covers them.
- Produce a converted summary document (`inputs/` → `outputs/`, referenced in the reply) only for changes to source `.py` files, dependencies, or the runtime/virtual environment; for anything else a chat reply is sufficient.

## Copyright-limited content (any source)

- If full text can't be reproduced for copyright reasons and is summarized or truncated, the first content immediately after the document's `# Title` must be a bold notice saying so, immediately followed by a verified, working, clickable link to the original source.
- Suffix both file names with `_summary` (`article_summary.md`, `article_summary.epub`).

Keep this file at or below 150 lines; when adding a rule, merge or remove equivalent prose or move task-specific procedures to on-demand guidance.
