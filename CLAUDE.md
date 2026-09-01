# md_to_kindle.py — conversion fidelity rules

These are non-negotiable output-correctness rules for this converter.
Any change to the markdown-loading pipeline (`load_markdown` and its
preprocessing helpers, roughly lines 1420-1860) must preserve them.

## Numbered lists: both `N.` and `N)` markers must produce real `<ol>` lists
python-markdown's list processors only recognize `N.` natively. Source
markdown that uses `N)` (a common style, especially in GPT/Claude
transcripts and prompts) is normalized to `N.` by
`normalize_paren_ordered_lists()` before `markdown.markdown()` runs.
Do not remove this step or narrow it to a single input file's shape —
this pattern recurs across pasted AI transcripts generally, not just
one file. If you touch the list-marker detection heuristic (currently:
convert `N)` only when preceded by a blank line or another `N)` line),
verify against `inputs/Second_brain_Introduction.md`, which has both
broken paren-lists and working `1.`-style lists in the same file — both
must keep rendering correctly.

## Created/Updated/Exported dates must always be YYYY-mm-dd
The chat-export header block:
    **User:** Anonymous
    **Created:** 8/31/2026 9:44:08
    **Updated:** 8/31/2026 9:54:19
    **Exported:** 8/31/2026 9:54:27
has its Created/Updated/Exported values rewritten from source `M/D/YYYY
H:MM:SS` (US locale, hardcoded — do not auto-detect) to
`YYYY-mm-dd HH:MM:SS` by `normalize_export_timestamps()`, called from
`load_markdown()`. The date component must never be left as `M/D/YYYY`.
Time-of-day is intentionally kept (not truncated) since it's the only
thing distinguishing Created/Updated/Exported when they share a date.

## Don't regress the other input files
Before merging any change to `load_markdown`/its preprocessing helpers,
reconvert a sample from `inputs/` covering: a file with correct `1.`
lists, a file with the Created/Updated/Exported header, and any file
using mermaid diagrams — this repo has no automated test suite, so
manual EPUB inspection (unzip, check the XHTML) is the only check.
