---
name: order-of-service
description: Build the weekly church Order of Service document for Kindle from a forwarded Order-of-Service email (usually a .docx/.pdf attachment) plus a Notices email -- full order of service, any responsive reading reproduced exactly with its light/bold (Minister/Church) type preserved, every scripture reading in full in the Good News Translation (GNT) fetched from the web never from memory, this week's notices, and an Any Other Business section. Use whenever the user hands over an "ORDER OF SERVICE" / "OOS" email, church notices, a service sheet, or asks for the Sunday readings / service document, even if they don't say "skill". Works inside the markdown_to_kindle_format repo's md_to_kindle.py -> EPUB pipeline.
---

# Order of Service document

Recurring task: turn this week's service emails into one Kindle document.
The repo's `CLAUDE.md` still governs everything here (EPUB default, never
alter wording, `inputs/` → `outputs/`, Send-to-Kindle). Invoke the
`kindle-doc-prep` skill first as `CLAUDE.md` requires; this skill adds the
order-of-service-specific parts on top of its email-ingestion rules.

## 1. Extract everything (with formatting)

```bash
.venv/bin/python3 .claude/skills/order-of-service/scripts/extract_oos.py \
    <scratchpad>/oos <path/to/order-of-service.eml> <path/to/notices.eml>
```

- Prints headers, plain body, stripped-HTML body, and the text of `.docx` /
  `.pdf` attachments, saving attachments into the scratchpad dir.
- Formatting is shown inline: `**bold**`, `_italic_`, `{color:#RRGGBB}…{/color}`,
  `{highlight:…}…{/highlight}`. Bold is resolved through Word character and
  paragraph styles, so trust this output over a naive `run.bold` dump (which
  reports `None` for style-inherited bold).
- Notices emails often have a plain part whose line breaks collapsed; use
  the stripped-HTML block for line layout.
- The attachment is the authoritative order of service. The covering email
  usually repeats the theme, the reading references, the intercession
  response, and instructions ("projected on the screen", "arrange readers")
  — those instructions go in Any Other Business.
- Attachment type not handled by the script (e.g. legacy `.doc`, image):
  fall back to `kindle-doc-prep`'s email rules (on macOS,
  `textutil -convert html` keeps bold for `.doc`).

## 2. Output format (keep exactly this order)

File: `inputs/order_of_service_<weekday>_<dd>_<month>_<yyyy>.md`, e.g.
`order_of_service_sunday_27_september_2026.md`.

```markdown
# Order of Service — Sunday 27th September 2026

<Church name> · Theme: *<theme>*

## Order of Service
## Responsive Reading — <reference>        (only if its text is given; see §3)
## Reading 1 — <Book> | Chapter <n> | Verses <a–b> (GNT)
## Reading 2 — …                            (one section per scripture reading)
## Notices
## Any Other Business
```

### Order of Service section

- Reproduce the attachment in full, in order, wording/spelling/casing
  untouched (hymn titles like `434(stf) rock of ages` stay as written).
- Each service element heading (Call to Worship, hymns, prayers, Sermon,
  Blessing…) becomes a `###` heading; empty elements still get their
  heading.
- Congregational texts (call to worship, confession, intercession
  response, affirmations) keep their light/bold type using the §3 rules.
  Light/bold is how the congregation knows which lines to say.
- For the responsive reading and each scripture reading, leave the `###`
  heading in place with a pointer instead of the text, e.g.
  `- Exodus 17:1-7; *(full text below — Reading 1)*`.

## 3. Responsive reading (required rule)

If the order of service includes a responsive reading **and provides its
text**, add a `## Responsive Reading — <reference>` section and insert the
text **exactly as written**. Be careful to preserve how the responsive
reading is written:

- Service sheets usually mark who speaks by type: **light type** (leader)
  and **bold type** (congregation).
  - Light type = normal font.
  - Bold type = bold text, red text, or otherwise coloured/highlighted
    text. Kindle is e-ink, so render every "emphasised" style as Markdown
    `**bold**` and every light line as plain text.
- Keep speaker labels exactly as written (`Minister:`, `Church:`, `All:`,
  `Leader:`, `People:`…). If the sheet has no labels, do **not** add any;
  the light/bold contrast is the only cue, so it must survive.
- Bold the whole line when the source does, label included
  (`**Church: We will tell of His mighty acts…**`). Don't bold only the
  label; that loses who says the line.
- One speaker line = one Markdown paragraph (blank line between). Keep
  line breaks inside a response with a trailing two-space hard break.
- Formatting noise: a bold/coloured run that is only punctuation or
  whitespace (e.g. a bold comma at the end of a light line) takes the
  line's dominant style.
- Two speaker lines run together in one paragraph (e.g. `…God provided
  Minister: He brought streams…`): split them into separate lines (a
  structural change, not a wording one). Give each part the style the
  sheet uses for that speaker, and mention the split in Any Other
  Business.
- A responsive text given in the sheet is usually the church's own
  paraphrase. Never replace it with GNT text.
- If only a reference is given with no text: there is no responsive
  section. Treat it as a scripture reading (§4) and say in Any Other
  Business that the responsive wording wasn't supplied.

## 4. Scripture readings (GNT, from the web)

- Readings are the ones listed under "Scripture Readings" (and any other
  passage the sheet says will be read). Each gets its own section headed
  `Book | Chapter | Verse(s)`, in service order.
- Always web-search each reference first (`WebSearch` "<ref> Good News
  Translation GNT"). Never write scripture from memory.
- Fetch the text with the helper (Bible Society UK = the anglicised GNT
  / "Good News Bible" read in British churches):

  ```bash
  .venv/bin/python3 .claude/skills/order-of-service/scripts/fetch_gnt.py EXO 17 1-7
  .venv/bin/python3 .claude/skills/order-of-service/scripts/fetch_gnt.py PSA 78 1-4,12-16
  ```

  - USFM book codes are listed in the script's `--help`. Run it once per
    chapter for readings that cross chapters.
  - Output is ready-to-paste Markdown: section headings, cross-reference
    line, `<sup>` verse numbers, poetry stanzas, lettered footnotes,
    source link, and the GNT copyright line. Paste it verbatim.
  - A stderr `WARNING: verses not found` may be real. The GNT omits some
    verses (e.g. Matthew 21:44) or puts them in footnotes; check the page
    before assuming a failure, and say which in Any Other Business.
- Cross-check each reading against a second GNT source (e.g.
  `biblestudytools.com/gnt/...`, which is the US edition):
  - Expected US/UK differences: "camped" vs "made camp", "older" vs
    "elder", "Lord" vs "LORD", "What right do you have" vs "What right
    have you".
  - Bible Study Tools also garbles nested quotes (`"From God,'`). Keep the
    Bible Society text.
  - Anything beyond such edition differences means a wrong passage or
    range: stop and resolve it.
- If the helper fails (site changed or blocked), fetch the Bible Society
  page another way and copy the text exactly. If no trustworthy GNT text
  can be obtained, say so plainly in Any Other Business rather than
  substituting another translation.
- A reading that includes a whole psalm used responsively is still
  handled by §3 when its text is given.

## 5. Notices

- Source: the notices email/sheet for the same week. Keep its wording,
  times and typos (e.g. `begiven`, `Autumn Far`) as written; point the
  typos out in the chat reply, not in the document.
- Include "this week": the service date through the following Sunday.
  Later dated events on the same sheet are still relevant; include them
  marked `Coming up:`. Prayer requests / news items are notices too.
- Structure: parent bullet per day/event (`- **Wednesday 30th
  September**`) with details as two-space-indented child bullets
  (`kindle-doc-prep` list rules). Confirm nested `<ul><li>` in the XHTML.
- Standing boilerplate (minister's contact details, general prayer line,
  website) goes to Any Other Business, not Notices.

## 6. Any Other Business (always present)

- Anything else relevant from either email: instructions from the covering
  email (what is projected on screen, readers to arrange), the minister's
  standing contact details, website link (confirmed `href` only), who sent
  what and when.
- If nothing else is relevant, write that explicitly, e.g. "Nothing else
  relevant in this week's emails." Never drop the section.
- Always add a **Notes on the readings** bullet with: where the text came
  from, the cross-check result, your confidence, and any trouble finding
  or matching a reading. Also note any structural fixes made to the
  order of service (e.g. a split speaker line).

## 7. Convert, verify, deliver

- `.venv/bin/python3 md_to_kindle.py inputs/<name>.md outputs/<name>.epub`.
  This is a real delivery, so let it auto-send. To check it first, convert
  to a scratchpad `.epub` with `--no-send-to-kindle`, inspect, then run the
  real command once.
- Unzip the EPUB and check the XHTML:
  - every expected verse number is present as `<sup>n</sup>`;
  - congregational/bold lines are `<strong>` and leader lines are not;
  - notice sub-bullets are nested `<ul><li>`;
  - curly/nested quotes are intact.
- Reply briefly: the file paths, whether it was sent, the readings used and
  your confidence in them, and any source typos or structural fixes.
