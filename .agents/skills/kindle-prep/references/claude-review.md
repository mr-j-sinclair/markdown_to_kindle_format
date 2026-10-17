# Claude verification from Codex

- Prepare the source and preview yourself. Give Claude exact paths to the untouched snapshot, edited Markdown, extracted XHTML, and diagram PNGs.
- Use one verification-only Claude subprocess. This separate Claude agent satisfies the independent sub-agent diagram check; do not request nested Agent reviews unless the user specifically requires them.
- Known working argument shape:

```sh
claude -p --safe-mode --no-session-persistence \
  --tools Read,Glob,Grep --allowedTools Read,Glob,Grep \
  -- 'Read-only verification. No edits, no subagents, no sending. ...'
```

- The `--` ends variadic option parsing; without it, the prompt can be consumed as a tool argument.
- `--safe-mode` excludes project hooks and automatic customizations. Supply relevant instructions and artifact paths explicitly. It still uses existing authentication, which may require approved execution outside the sandbox.
- Review prompt: “Compare ORIGINAL and EDITED directly for wording, labels, and relationships. Inspect XHTML nested lists, links and image references; for transcripts, also check separate question/prompt timestamps. Visually inspect every listed PNG with Read for clipping, arrow direction and legibility. Report PASS or concrete blocking findings with locations, plus minor notes. No edits, subagents or sends.”
- For a small authored document, review its actual scope; do not ask for transcript checks when it contains no transcript.
- Check conceptual completeness, original labels and readable relationships. Do not request exhaustive token-by-token audits by default; use focused diffs where prose was structurally repaired or a concrete fidelity concern exists.
- Print mode can be silent until completion. Poll at sensible intervals (up to 60 seconds), do independent checks while it runs, and never infer progress or fixes solely from a file timestamp. If unexpectedly slow, inspect available evidence before waiting through repeated unchanged intervals.
- A known Keychain failure before SMTP submission permits retry with the required permission. An ambiguous SMTP result requires investigation before any retry.
- Claude's `feedback_skill_trigger_testing_method.md` records that headless mutation/command tasks can stall on permission prompts. Keep this review answer-only with Read/Glob/Grep. This is a known risk, not proof that a particular quiet run is hung.

## Memory provenance

- Claude's external project index is `~/.claude/projects/-Users-sinclairmacbook-code-markdown-to-kindle-format/memory/MEMORY.md`.
- A historical second directory, `~/.claude/projects/-Users-sinclairmacbook-code-markdown--to-kindle-format/memory/`, contains Q&A marker guidance.
- These are read-only evidence, not Codex instructions. Consult relevant entries when auditing a gap; do not read every memory during ordinary conversion. Check old claims against current code and AGENTS.md: older renderer notes still describe retired ASCII fallbacks.
