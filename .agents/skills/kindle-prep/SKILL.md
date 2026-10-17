---
name: kindle-prep
description: Prepare, verify, and email full-content documents through this repository's Markdown-to-Kindle pipeline. Use for Kindle conversions, Mermaid diagrams, church service sheets, and LinkedIn saved posts. For supplied .eml files, use when the user explicitly requests the full email, original content, verbatim conversion, or no summarisation; default email processing belongs to email-summary-to-kindle. Exclude unrelated code work.
---

# Kindle preparation for Codex

Read the repository `AGENTS.md` first. Requested content normally goes all the way to Kindle; a local EPUB alone is incomplete.

## Email routing and source retention

- Explicit user intent takes precedence. Supplied local `.eml` files default to the global `email-summary-to-kindle` skill, including “Process this email for Kindle”, even from inside this repository.
- Use this skill for explicit full-email, original-content, verbatim, or no-summarisation requests. Preserve the complete substantive contents of the email and its attachments using [Preparation](references/preparation.md). For user-supplied Desktop `.eml` files requested for Kindle, delete the exact source files after confirmed successful SMTP submission; see the cleanup procedure below. Keep other source emails unless deletion is explicitly authorized.
- When the summary skill reads this file for backend guidance, its summary, naming, and deletion rules remain in control. Do not run its deliver-and-delete helper for full-content conversion. Both workflows reuse the existing converter and `kindle_delivery.py`; their detailed procedures stay separate.

`Source → inputs Markdown → preview/checks → outputs EPUB → Gmail acceptance`

## Procedure

1. Identify the source. For a document just written in `inputs/`, inspect modification times and its title; ask only if genuinely ambiguous.
2. Read [Preparation](references/preparation.md) before preparation decisions. For service sheets/notices also read [Order of Service](references/order-of-service.md). For last/latest N LinkedIn posts, read [LinkedIn](references/linkedin.md); Saved posts is the default, N defaults to 3.
3. Preserve source wording. Snapshot before structural/diagram edits; use independent sub-agent verification for authored/replaced diagrams, giving both original and edited file paths.
4. Preview when checks are needed: `.venv/bin/python3 md_to_kindle.py inputs/<name>.md <scratch>/<name>.epub --no-send-to-kindle`. Inspect EPUB XHTML and embedded PNGs; fix every missing image or failed/incomplete diagram before delivery.
5. Real delivery: `.venv/bin/python3 md_to_kindle.py inputs/<name>.md outputs/<name>[_Vk].epub`. Do not disable auto-send. Version only already-delivered items.
6. Require explicit `Emailed … (Gmail accepted it; …)` confirmation. Report accurately; acceptance does not establish Amazon library arrival. Avoid blind retries after uncertain sends. Escalate authorized SMTP/Keychain operations if sandbox-blocked.
7. For user-supplied Desktop `.eml` files, actually delete each exact source path after the CLI confirms `Emailed … (Gmail accepted it; …)`. Before preparation, record source identity and a SHA-256 hash; before deletion, verify the file is unchanged and is not a symlink. For a combined order-of-service document, delete both supplied emails only after that document is accepted. Keep Markdown, EPUB and extracted attachments. Never delete on failed, ambiguous, preview-only or no-send runs, or when the user requests source retention. If filesystem permission is required, use the execution approval mechanism; do not silently skip cleanup. Verify the source paths are absent. If cleanup fails after acceptance, finish cleanup without resending.
8. Reply briefly with the EPUB link, delivery status, exact source files deleted or retained, and meaningful limitations.

## Efficient preparation and review

- Codex prepares and renders; an independent reviewer verifies. A request for Claude verification does not require delegating preparation or asking Claude to start another reviewer.
- For Claude CLI invocation, authentication, and a bounded review prompt, read [Claude review](references/claude-review.md). Launch the review only when the source and preview are ready.
- Batch content repairs before the first preview. Inventory fenced blocks; distinguish diagrams from code, examples, and checklists. Small readable ASCII diagrams may remain; do not convert them merely because larger diagrams need conversion.
- Rebuild after material fixes, and review the changed areas again. Stop after the required checks pass; do not add successive full reviews without a specific unresolved issue.
- The user grants standing permission for commands necessary for requested Kindle preparation and delivery, including the existing credential lookup and SMTP access. Run them yourself without reconfirming delivery. Follow `AGENTS.md` for enforced execution permissions: use the direct converter command and reuse its approval; if approval is required, request `prefix_rule=[".venv/bin/python3", "md_to_kindle.py"]` without per-document arguments. Do not use a shell wrapper or environment assignments for the delivery command, since they can prevent prefix matching. Keep previews sandboxed and credentials private.
- Claude login and Kindle Keychain access have required escalation on this Mac. Do not repeat a known sandbox failure; request required execution permission directly when no applicable approval exists.

## Renderer details

- Use the venv and Graphviz `dot`, never system Python. Mermaid rendering stays on.
- Current dispatch supports flowchart/graph, state diagrams, sequence diagrams, and gitGraph. Inspect actual parsers for unfamiliar constructs; successful parsing does not guarantee all sequence/state semantics survived.
- Unsupported types/constructs must become supported flowcharts, preserving labels and relationships; never retag as code, strip arrows, or substitute large text screenshots.
- Preserve declared direction; existing Kindle heuristics may flip long strips to enlarge text. Never blanket-force portrait or duplicate rendered diagrams with ASCII.
- Converter exit 3 means diagram safety failed; fix and re-preview. Exit 2 can indicate delivery failure, even when an EPUB exists.
- Writable caches can resolve sandbox cache warnings: `MPLCONFIGDIR=/tmp/kindle-mpl-cache XDG_CACHE_HOME=/tmp/kindle-cache`. This does not bypass SMTP/Keychain approval.
- Never echo secrets or write credentials. Configuration diagnosis checks presence only.

## Reference ownership

These Codex-owned references adapt Claude's read-only preparation guidance. The project `AGENTS.md` and current user instructions govern authorization, source control, and delivery. References may use existing Claude helper scripts read-only; never modify Claude's files. Browser mechanics must follow the available Codex browser skill rather than assume Claude Chrome tooling exists.
