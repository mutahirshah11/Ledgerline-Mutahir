# Register page transcription (read B, column by column, prompt v1)

## Your task

[PR-01] You are transcribing one photographed page of a handwritten goods receiving register.
Use only what is visible on this image. You have no other documents, lists or records, and you
must not draw on any.

Your answer is a faithful transcription, not a judgement about what the writer meant. When you
are not certain, say so using the statuses below. A wrong value given as certain is the worst
possible answer; an honest UNCLEAR or ILLEGIBLE is always acceptable.

## Copying rules

[PR-02] Copy exactly what is written, character for character, including spelling,
abbreviations, case and symbols. For example, write "H202" if that is what the page shows, "yds"
if the writer wrote "yds", and "Qasr Pkg" if the writer wrote "Qasr Pkg". Keep the writer's
spacing, punctuation, slashes and hyphens.

[PR-03] Never convert units, expand abbreviations, correct spelling, or reformat dates or
numbers. A date keeps the writer's order and separators, a number keeps its commas, decimal
points and any letters written with it, and a unit written in lower case stays in lower case.

[PR-04] Never infer a value from other rows, from what is usual, or from context. Each cell is
read on its own. Ditto marks, arrows or "same as above" are copied as written (for example `"`),
never replaced by the value they point to. Do not complete a partly written value from a similar
value elsewhere on the page.

## Cell statuses

Every cell has five fields: `value`, `status`, `struck`, `candidates` and `note`. The status is
one of CLEAR, UNCLEAR, CORRECTED or ILLEGIBLE.

- CLEAR: every character is read with certainty. `value` is the text (or null for a blank cell,
  PR-05), `struck` is null, `candidates` is empty.

[PR-05] A blank cell is value null with status CLEAR (`struck` null, `candidates` empty).

[PR-06] A value struck through with a replacement written nearby is status CORRECTED: value =
the replacement, struck = the struck-through value. `candidates` is empty. This applies only when
both the replacement and the struck-through value are read with certainty. If either one is not
certain, or there is no replacement, apply PR-07 or PR-08 instead (status UNCLEAR or ILLEGIBLE,
value null) and say "struck through" in note.

[PR-07] If any character of a value cannot be read with certainty, the cell is not CLEAR: use
UNCLEAR with at least one candidate (your possible readings, value null), or ILLEGIBLE (value
null) if you cannot propose any reading. Each candidate is a complete reading of the whole cell.
Do not pick one reading and mark it CLEAR; never guess.

[PR-08] If any character is physically covered (ink blot, smudge, tear, fold), the cell is
ILLEGIBLE with value null and NO candidates, even when other characters are visible. Put visible
fragments only in note (e.g. "leading 2 visible, rest covered").

[PR-09] Always say what you see in note when a cell is not CLEAR ("ink blot", "overwritten",
"faint"). For a CLEAR cell, note is null unless there is something worth reporting.

## Page header

[PR-10] Report the Month and Page No. exactly as handwritten in the header, in `month_written`
and `page_no_written`. Use null for either one if nothing is written there, or if it cannot be
read with certainty; never guess.

## Grid layout

[PR-11] Number rows by printed slot from 1 at the top of the grid (16 slots). Include only slots
with something written; count empty slots in blank_row_slots. The `row_index` of each row is its
printed slot number, so a written slot below an empty one keeps its own slot number. The number
of rows plus blank_row_slots must equal 16.

[PR-12] Give table_top as the top edge of data slot 1 and table_bottom as the bottom edge of data
slot 16, each as a fraction of the image height; exclude the printed header row. Both are numbers
from 0 (top of the image) to 1 (bottom of the image). Either edge may be null if that edge is not
visible on the image.

[PR-13] Columns, in order: Date, GRN No., Supplier, Item / Description, Lot No., Qty, Unit,
Recd by → keys date, grn_no, supplier, item, lot_no, qty, unit, recd_by. Every row has all eight
keys.

## Reading order

[PR-B1] Read column by column: read the whole Date column top to bottom, then GRN No., and so
on; then assemble rows by slot number. Read each column from slot 1 down to slot 16 before
starting the next column, in the column order given above, and place each cell in the row whose
printed slot it sits in.

## Output

[PR-14] Answer only in the required JSON format. Do not add any text, explanation or markdown
outside the JSON object.
