# Codex project instructions — Markdown to Kindle

## Email workflow routing

- Process supplied email files sequentially in the user’s listed order. Complete each email end to end—inspect its body/attachments, create its Markdown input and EPUB output, verify, send, record acceptance, and perform authorized source cleanup—before starting the next. Do not batch-extract, batch-write, or parallelize different emails. If any stage errors, record the email, failed stage and concrete reason, retain its source unless sending succeeded and cleanup is safe, then continue immediately with the next email; do not hold the batch for troubleshooting or clarification. Never blindly retry an uncertain send. At the end, show unresolved errors in an on-screen Markdown table with columns `Email file | Stage | Sent? | Reason`; use `yes`, `no` or `uncertain` for `Sent?`, and distinguish send failures from cleanup failures after acceptance. If none errored, state that briefly. Missing images use the existing text fallback and are not delivery failures. The specialist Order of Service + same-week Notices pair is one combined item: finish its one delivery and cleanup of both sources before starting the next independent email.

- For email deliveries, use relevant images actually available in the body or attachments. Missing, inaccessible or unusable images, including referenced-but-absent profile images, must never delay sending or trigger a clarification: send the available text, add a brief omission note when material, and continue. Do not spend time hunting for replacement images. Preserve confirmed-send receipts and cleanup safeguards.

- Explicit user intent takes precedence over all defaults below, including when Codex starts inside this repository.
- Supplied local `.eml` files default to full-content preparation through `.agents/skills/kindle-prep/SKILL.md`, including “Process this email for Kindle”. Preserve the complete substantive email and open/include its attachments, useful images and diagrams. Summarise only when explicitly requested, using the global `email-summary-to-kindle` skill's intentionally short body-and-attachment summary workflow.
- After confirmed successful SMTP submission, delete the exact supplied source emails from any original folder, including full-content, summary and order-of-service workflows. Keep them on failed, uncertain, preview-only or no-send runs, or when the user asks to retain them. Persist sent evidence before cleanup; resume failed cleanup without resending.
- Reading shared backend guidance does not switch modes. The summary helper remains summary-only; full-content preparation uses the existing direct converter. Do not send through both workflows.

- **Short email summaries (user preference, 2026-10-20):**
    - Answer “What happened?” and “Any actions?” in a few concise bullets. No-action emails should be especially short: normally one or two summary bullets plus “No explicit action required.”
    - Follow the email-summary skill's length guidance. Add detail only when needed to understand the outcome or act; omit routine optional advice, empty sections and boilerplate. This preference does not shorten explicitly requested full-content conversions.

## Default: prepare, verify, and send

- The user expects requested content to be delivered to Kindle, without asking again. “Parse for Kindle”, “convert this”, and “prepare this for Kindle” authorize the full pipeline, including email delivery.
- Use EPUB by default. PDF only on explicit request; use `--margins narrow` for PDF unless told otherwise. PDF is never auto-sent.
- Read `.agents/skills/kindle-prep/SKILL.md` before any conversion or document-preparation task, even if skill discovery has not loaded it automatically.
- Run `.venv/bin/python3 md_to_kindle.py inputs/<name>.md outputs/<name>.epub`. Successful EPUB conversion auto-sends. Never use system Python for conversion.
- Use `--no-send-to-kindle` only when the user asks not to send, for development/regression work, or for a temporary preview before one real delivery. Creating a local EPUB alone does not complete a normal Kindle request.
- Verify a preview before sending when diagrams, images, or structural changes need inspection. Send the real output exactly once after checks pass.
- Delivery success requires the CLI's `Emailed … (Gmail accepted it; …)` confirmation, not merely `Wrote …`. Gmail acceptance does not confirm arrival in Amazon's library. Report failures accurately; avoid blind retries after ambiguous delivery.
- Standing user authorization (reaffirmed 2026-10-16): run the commands necessary to prepare, verify, convert, and email requested content to Kindle, including the converter's existing Keychain credential lookup and SMTP/network access. Run these commands yourself; do not ask the user to run them or reconfirm delivery.
- For real delivery on this Mac, use a direct `.venv/bin/python3 md_to_kindle.py ...` command from this repository and reuse existing approvals. If additional permission is required and the runtime supports requesting it, use `prefix_rule=[".venv/bin/python3", "md_to_kindle.py"]`. If requests are unavailable or rejected, report the blocked operation. Keep previews sandboxed.
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

## Kindle delivery permissions and Keychain access (verified 2026-10-19)

- **Requested Kindle work:**
    - Complete delivery through the existing backend and configured destination without asking again whether to send. This does not authorize unrelated messages or recipients.
- **Verified command-specific access:**
    - From `/Users/sinclairmacbook/code/markdown_to_kindle_format`, the existing approved credential-check command succeeded and direct `.venv/bin/python3 md_to_kindle.py ...` commands completed SMTP submission.
    - A general Python script using the same backend failed with `KeyringBackendError`. Success applies to the approved invocation; it does not establish unrestricted or permanent Keychain access.
    - Do not infer that delivery is blocked merely because a session cannot request new elevation. First check whether the required command already has approval.
- **Credential check:**
    - Run the exact check documented in the backend `AGENTS.md`, with the backend as the execution tool's working directory. It prints availability only.
    - A successful check does not grant the email-summary helper, a heredoc, an absolute Python invocation, or another wrapper the same execution permission.
- **Delivery and receipts:**
    - For ordinary converter deliveries, use the direct approved converter invocation. Avoid shell wrappers, environment assignments, or altered command prefixes that may prevent approval matching.
    - Email summaries retain their helper's persistent receipt and locking protocol. Obtain permission for the helper itself when needed; do not replace it with an untracked auto-send merely because the converter has approval.
    - Check receipts before sending. Never automatically retry an uncertain SMTP outcome or resend an accepted document to finish cleanup.
- **Separate filesystem permission:**
    - Successful Keychain lookup and Gmail acceptance do not grant permission to delete Desktop emails. On 2026-10-19, all three exact-source cleanup attempts were denied independently of successful delivery.
    - After acceptance, persist the sent receipt before cleanup. If cleanup is denied, retain sources, report the filesystem blocker, and resume cleanup without resending once permission is available.
- **Runtime boundaries:**
    - Reuse existing narrow approvals. Request additional execution permission only when required and supported by the current runtime; if unavailable or rejected, report the exact blocked operation.
    - Never expose, copy, or relocate credentials, disable sandbox protections, or claim that instructions themselves grant access.


Run this credential check as a direct command with the execution tool's working directory set to this repository. Reuse the existing approval for this exact invocation; never print the returned password.

```sh
.venv/bin/python3 -c 'import kindle_delivery; assert kindle_delivery.get_app_password(); print("Kindle credential available")'
```

For ordinary converter delivery, use the direct command below with the same working directory. Successful conversion auto-sends, so do not also invoke a separate sender. The transactional email-summary helper remains a separate workflow and needs its own execution permission.

```sh
.venv/bin/python3 md_to_kindle.py inputs/<name>.md outputs/<name>.epub
```
