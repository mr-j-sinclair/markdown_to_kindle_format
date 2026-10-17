# Codex project instructions — Markdown to Kindle

## Email workflow routing

- Explicit user intent takes precedence over all defaults below, including when Codex starts inside this repository.
- Supplied local `.eml` files default to the global `email-summary-to-kindle` skill for processing or summarisation, including “Process this email for Kindle”. It summarises the email and substantive attachments, reuses this repository's conversion/delivery backend, and retains its deletion-after-success rules (subject to explicit overrides).
- Explicit requests for the full email, original content, verbatim conversion, or conversion without summarisation use `.agents/skills/kindle-prep/SKILL.md`, preserving the complete substantive contents of the email and attachments. For user-supplied Desktop `.eml` files requested for Kindle, delete the exact source files after confirmed successful SMTP submission, including full-content and order-of-service workflows. Retain them on failed, uncertain, preview-only, or no-send runs, or when the user asks to keep them.
- The full-text and explicit-summary requirements below govern `kindle-prep`; they do not override the `.eml` summary default. Reading `kindle-prep` for shared backend guidance does not switch workflows. Keep each skill's detailed workflow separate.

## Default: prepare, verify, and send

- The user expects requested content to be delivered to Kindle, without asking again. “Parse for Kindle”, “convert this”, and “prepare this for Kindle” authorize the full pipeline, including email delivery.
- Use EPUB by default. PDF only on explicit request; use `--margins narrow` for PDF unless told otherwise. PDF is never auto-sent.
- Read `.agents/skills/kindle-prep/SKILL.md` before any conversion or document-preparation task, even if skill discovery has not loaded it automatically.
- Run `.venv/bin/python3 md_to_kindle.py inputs/<name>.md outputs/<name>.epub`. Successful EPUB conversion auto-sends. Never use system Python for conversion.
- Use `--no-send-to-kindle` only when the user asks not to send, for development/regression work, or for a temporary preview before one real delivery. Creating a local EPUB alone does not complete a normal Kindle request.
- Verify a preview before sending when diagrams, images, or structural changes need inspection. Send the real output exactly once after checks pass.
- Delivery success requires the CLI's `Emailed … (Gmail accepted it; …)` confirmation, not merely `Wrote …`. Gmail acceptance does not confirm arrival in Amazon's library. Report failures accurately; avoid blind retries after ambiguous delivery.
- Standing user authorization (reaffirmed 2026-10-16): run the commands necessary to prepare, verify, convert, and email requested content to Kindle, including the converter's existing Keychain credential lookup and SMTP/network access. Run these commands yourself; do not ask the user to run them or reconfirm delivery.
- For real delivery on this Mac, use a direct `.venv/bin/python3 md_to_kindle.py ...` command from this repository with the required execution permission. Reuse existing approvals. If a sandbox approval is still required, request the reusable `prefix_rule=[".venv/bin/python3", "md_to_kindle.py"]`, not a rule containing document filenames or output paths. This covers subsequent documents without approving arbitrary Python commands. Keep previews sandboxed.
- Standing authorization does not override enforced sandbox restrictions. If the runtime still requires approval, explain that it is an execution-permission requirement; request it directly through the execution tool without asking again whether to send. Do not disable sandbox protections globally or expose credentials.

## Paths and fidelity

- Generated content Markdown belongs in `inputs/`; converter-rendered EPUB/PDF belongs in `outputs/`. Codex instruction/skill files are exempt from the content pipeline.
- Name each source after its own title/subject. For a resend of an already delivered item, use `_V2.epub`, `_V3.epub`, etc.; inspect existing outputs for the next version. An unsent preview does not require a version suffix.
- Multiple independent sources get separate Markdown files, EPUBs, and sends. A post plus its linked article, or the defined church service document, is one item.
- Preserve source wording, spelling, grammar, punctuation, and attribution. Change only structures necessary for rendering. Do not silently summarize content.
- Start content with its own `# Title`; do not add a manual title page or table of contents. New authored/exported Markdown includes title, author (`Codex`), creation date, and modified date; preserve original authorship when converting someone else's work.
- Only send a summary when the user explicitly requests or accepts one. For copyright-limited summaries, put a bold summary notice and verified original-source link immediately after the title; suffix both filenames `_summary`.

## Personal reading and source permissions

- All Kindle deliveries in this project are for the user's personal, non-commercial reading, not publication, resale, or public redistribution.
- Default to the complete source text verbatim, with attribution, links, and substantive images preserved. A summary does not complete a request to send an article or document.
- Before raising a copyright objection for a linked website or PDF, check the source's actual license, terms of use, and relevant reuse permissions. Do not assume that a copyright notice prohibits reuse; copyrighted content can be licensed for personal or non-commercial use.
- Honor permissions that cover the requested use, including applicable personal/non-commercial licenses, and retain required attribution and notices. BibleProject's terms grant personal, non-commercial use; check the current terms and apply the relevant conditions instead of automatically substituting a summary.
- Do not assume every source grants reuse permissions merely because it is freely accessible or intended for personal reading. Personal use is the task context, not an unconditional override of source permissions or governing requirements.
- If an actual restriction prevents full-text conversion, explain the specific limitation before sending anything. Never silently replace the source with a summary, send an unsolicited summary, or report the original request as complete. Where applicable, offer to convert a copy supplied by the user.

## Diagrams and verification

- Codex owns preparation and rendering. When Claude verification is requested, use a bounded read-only Claude review of the prepared source and preview; avoid delegating the entire job plus nested reviewers. Follow `.agents/skills/kindle-prep/references/claude-review.md` for the known CLI invocation and permission lessons.

- Every ASCII flow/box/tree diagram longer than six lines, including in chat exports, must become a supported Mermaid diagram with verbatim labels. Prefer Mermaid for smaller box trees too.
- Keep Mermaid rendering on. Never bypass failed diagram checks by re-tagging fences as code, stripping arrows, or replacing large diagrams with text screenshots.
- Before editing a diagram or other structure requiring independent verification, snapshot the untouched file in session scratch space. Give a verifier sub-agent the original and edited paths to read directly. Check conceptual completeness and visual readability.
- Independent sub-agent verification is explicitly requested for authored/replaced diagrams. If delegation is unavailable, compare the original and edited files yourself and disclose that limitation.
- Inspect embedded diagram PNGs for labels, arrows, clipping, and legibility; inspect EPUB XHTML for real nested lists and separated chat Question/Prompt lines. Parsing success alone can miss unsupported sequence/state constructs.

## Code, credentials, and ownership

- All delivery logic belongs in `kindle_delivery.py`. Never print, log, hardcode, or write the Gmail App Password. Sender/destination are configured through environment variables, credentials through the existing Keychain/environment mechanism.
- Treat `CLAUDE.md`, `.claude/`, and Claude's external memory as read-only. Codex owns this file and `.agents/skills/kindle-prep/`; do not adopt Claude-specific commit/push automation or tool names.
- Do not create branches, commits, tags, or pushes unless the user explicitly requests them.
- After editing `load_markdown()` or preprocessing, run `.venv/bin/python3 -m unittest discover -s tests`, reconvert representative ordered-list, timestamp, and Mermaid inputs with sending disabled, and inspect EPUB XHTML.
- Runtime/source/dependency changes warrant a converted summary document with sending disabled; instruction-only changes need a chat reply.
- Automation state stays on its own branch: `ntiy-state` or `daily-facts-state`, never `main`. NTIY keeps episode wording verbatim; Daily Facts prose must be grounded in supplied/search evidence, with code-owned numbers/tables/years and validated links.

## Kindle delivery preference and Keychain diagnosis (2026-10-17)

- User preference: complete requested Kindle email and email-summary deliveries autonomously through the existing configured backend and destination; avoid asking again whether to send content the user has requested for Kindle. This applies only to requested Kindle work, not unrelated messages, recipients, mailbox access, or background sending.
- Verified observation: sandboxed macOS Keychain lookup failed with `KeyringBackendError` (underlying status -50), while the same backend lookup succeeded with elevated execution. For real delivery, use the tool's elevated-execution approval mechanism when available and explain the requested Kindle task in the justification. Do not repeat a known sandbox failure or suggest broad Full access when a narrowly approved execution works.
- Check credential availability without displaying the credential before beginning a delivery transaction. Keep the existing backend, receipt safeguards, and source-retention/deletion rules. Never automatically resend an uncertain SMTP outcome.
- This is workflow memory, not a permission grant: current user scope, macOS permissions, and runtime approval controls still apply. If elevation is unavailable or rejected, report the exact blocker; never bypass it or claim permanent Keychain access.

