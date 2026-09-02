# md_to_kindle.py — conversion fidelity rules

## Default output format is EPUB
Every conversion defaults to EPUB. Don't ask the user which output
format they want — just build the EPUB. Only produce a PDF when the
user's own request explicitly names PDF; "convert this" or "prepare
this for Kindle" on its own always means EPUB.

## Editing scope: fix only what breaks rendering
When preparing a source markdown file for conversion, only touch what
the converter actually needs to render the file correctly — list
markers, blank-line separation, oversized ASCII diagrams, header date
formats, and the other structural issues this file documents.
Spelling, grammar, wording, and phrasing are never in scope unless the
user explicitly asks for them to be fixed — not even a single word
swapped for punctuation reasons. If a genuine rendering defect can
only be worked around by changing the author's own wording (e.g.
because a converter heuristic misfires on ordinary prose), that's a
bug in `md_to_kindle.py`, not a reason to rewrite the source text —
fix the heuristic instead. Preparing a file for Kindle should be fast;
treat any edit beyond a structural fix as scope creep.

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

## Any generated markdown deliverable goes through the tool like everything else
When a task (e.g. this one) calls for writing a new human-readable
document — a summary, a report, a write-up — as a deliverable, it is
NOT a special case that gets hand-placed in `outputs/` as a raw `.md`
file. Follow the same convention every other document in this repo
follows:
1. Write the markdown source into `inputs/` (not `outputs/`).
2. Convert it with `.venv/bin/python3 md_to_kindle.py inputs/<name>.md
   outputs/<name>.epub` (system `python3` lacks `graphviz` and other
   deps and fails silently — always use the venv one).
3. The rendered, human-ready EPUB/PDF is what lands in `outputs/`.
`outputs/` holds only converter-rendered files; raw `.md` source never
belongs there.

## Diagrams in authored documents: ASCII over 6 lines becomes Mermaid
A hand-written ASCII flow/box diagram longer than 6 lines total renders
badly on a Kindle screen (wraps, truncates, loses its shape — this is
what prompted the rule). If a diagram you're authoring for one of this
project's documents would exceed 6 lines as ASCII, write it as a
` ```mermaid ` flowchart instead — this converter already renders those
to embedded colour PNG images (see the `--mermaid-images` feature and
its ROLLBACK banner around md_to_kindle.py:670). A diagram at or under
6 lines may stay as a plain ASCII code fence.
Stick to the parser's supported subset: `flowchart TB|TD|BT|RL|LR`
header; node ids `[A-Za-z_][\w-]*`; shapes `[rect]`, `{diamond}`,
`((circle))`, `(rounded)`; edges `-->`, `-.->` (dashed), `==>`, `<-->`,
`<==>`, optionally with `|edge label|`. Favor short node labels over
cramming full sentences into the diagram — put detailed prose in
regular bullets next to the diagram instead, so the rendered image
stays legible rather than a dense wall of text in boxes.
Whenever replacing/authoring such a diagram, spawn a **separate
sub-agent** to compare the Mermaid version against the original (ASCII
sketch, or the intent being diagrammed) and confirm it preserves the
general spirit/shape of the diagram before treating it as final —
don't just self-check your own conversion. This is a sanity check, not
an exhaustive audit: the sub-agent doesn't need to verify every
entity, ordering, and connection matches exactly, just that nothing
important is conceptually wrong or missing.

## GPT/chat-export "Question" sections — manual vigilance
`normalize_paren_ordered_lists()` (see above) fixes the specific `N)`
marker case that caused this. But any numbered-question block inside a
GPT/Claude-transcript export is a place newlines can silently collapse
— python-markdown only breaks a paragraph on a *blank* line, so any
future export tool or transcript style that numbers questions
differently (bare `N `, lettered `a.`/`a)`, etc.) can hit the same
failure mode through a different marker. When converting this kind of
source, spot-check the rendered "Question N" / "Prompt N" boxes in the
output EPUB/PDF for intact, separated line items — don't assume the
existing fix covers every variant that might show up.

## Bulleted lists: a bold header + text needs an indented sub-bullet
A bullet shaped like `- **Header:** some more text` (or `- **Header**
— some more text`) — a short bold/label header followed by a colon or
dash and then a longer explanation on the same line — renders badly on
a Kindle: the bold lead-in and the body text run together and wrap
awkwardly, making the list hard to scan. Split it instead:
```
- **Header:**
  - some more text
```
so the header stands alone and the detail is a nested, indented
sub-bullet. Apply this whenever authoring a list of that shape for a
document this tool will convert — it's specifically the
label-then-elaboration list shape that needs splitting, not every
bullet that happens to start with bold text (a bullet that's just a
short bold term with no colon/dash-separated elaboration stays as-is).
**Indent the sub-bullet by exactly 2 spaces**, not 4 — `load_markdown()`
calls `markdown.markdown(..., tab_length=2)`, so a nested list needs
2-space indent to parse as nested; 4 spaces gets swallowed as a lazy
paragraph continuation of the parent `<li>` instead (confirmed by
testing directly against the converter, not just visual inspection —
always verify nesting renders as a real `<ul><li>` in the output
XHTML, since this indentation width is easy to get wrong by habit).

## After any code change: push
Any change to `md_to_kindle.py` (or other source `.py` files) that
fixes a bug or adds a feature — once implemented and verified — gets
committed and pushed to `origin/main` in the same turn, without
needing to be asked. (This mandatory-push rule is specifically for
code changes; changes to this file alone aren't required to trigger a
push by this rule, though keeping it in sync with the same discipline
is good practice.)

## After any completed task: deliver an output file
Every task — a code fix, a standing-instruction update, or otherwise —
ends with a human-readable summary document handed to the user: write
it to `inputs/`, convert it with the tool to `outputs/` (per the rule
above), and reference that file in the reply to the user. Include any
changes made to standing instructions (this file) in the summary,
described in plain language — never as a raw diff.
