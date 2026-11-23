# The agent-only composer HAS an Add-ingredients surface — and @-mention chips already work there

**Date:** 2026-11-23 · **Issue:** #799 (agent-only composer), #639 (character references), #723 (character entities)
**Cost:** 0 credits. Two probe runs + one cleanup run: navigation, DOM reads, typing "@", attaching chips. No submit, no gate approval, no generate click. Balance adminmandala 50 before and after (verified via `gflow credits user`).
**Probe:** [`scripts/dev/spike_agent_only_add_ingredients.py`](../../../scripts/dev/spike_agent_only_add_ingredients.py) (+ `spike_agent_only_add_ingredients_cleanup.py`)
**Captures:** `scripts/dev/_spike_out/spike_agent_only_add_ingredients_20261123_160418.json` (final findings), `..._partial_*.json`, screenshots `..._at_sign_*`, `..._mention_chip_*`, `..._two_chips_*`, `..._picker_open_2_*`, `..._picker_search_*`, `..._picker_row_picked_*`, `..._cleaned_*`, `..._cleanup_20261123_160837.*` (gitignored)
**Account:** adminmandala, project `5471bc69-cb82-48c2-bfe8-306081348bda`, UI locale Indonesian (id). The spike stayed on adminmandala; Nando's manual video ran on azoolabit at the same time and was not touched.

## Question

Nando's screenshot (23 Nov 22:51) showed Flow's Koleksi asset picker with a
"Tambahkan ke perintah" (Add ingredients) button, and at 23:00 he generated a
character video manually in the agent-only composer by typing
"buat video @mbak_velfit review @sereal_velfit" — @ chosen with Enter from the
picker until it became a character chip — after which the agent asked for a
10-credit approval (Veo 3.1 - Lite) and started the video. The 2026-09-15 spike
note listed "i2v / r2v (attaching a start frame or references through Add
ingredients)" as never measured. What is the Add-ingredients surface on this
composer, what does attaching through it put in the prompt, and does the
@-mention path work here?

## What was observed

### (a) The Add-ingredients trigger exists, with a structural anchor

`flow-add-menu > button.add-menu-trigger`, ligature `add`, aria-label
"Tambahkan bahan ke kotak perintah", inside `flow-base-prompt-box` (itself inside
`flow-creative-agent-prompt-box`). Clicking it opens
`flow-add-menu-popover-content` — the Koleksi asset picker: a search input
(`#add-menu-input`), 7 filter tabs, 14 options (videos + characters), each
`button.asset-item[role='option']` carrying `.asset-title`. Searching
"mbak_velfit" narrows the list to exactly that character.

### (b) Picker row click: no `.mention-chip`, no confirm reached

Clicking the `mbak_velfit` row closes the popover and creates **no**
`.mention-chip` (`read_chips` = `[]`, editor text empty). The "Tambahkan ke
perintah" confirm button was never reached via a character row in either run:
after the row click there is no popover and no confirm surface. In Nando's
screenshot that button sits in the preview pane for a *video* asset; whether it
appears for character rows, and what it does, is unmeasured (see below).

The ingredient area did change across the row click: distinct
`flow-character-ingredient-chip` attachments went from 2 (`mbak_velfit`,
`roticelup_taro` — leftovers from the @-mention attaches below, whose editor
spans had been cleared) to 1 (`mbak_velfit`, woman thumbnail). Whether the row
click toggles, replaces, or re-renders the ingredient set is ambiguous from
this data; it did **not** add a second `mbak_velfit` chip.

### (e) "@" in the ProseMirror opens the picker; Enter commits a character chip

Typing "@" opens the same popover (`flow-add-menu-popover-content`, 14 options
including `mbak_velfit`, `roticelup_coklat`, and videos). Typing the name narrows
it to exactly that character. The driver's own `attach_character_entities`
(`agent_only_composer.py:1165`, via `_mention_character`, `:1096`) committed:

```html
<span class="mention-chip" data-mention-id="558c34b4-427a-41a4-a097-94ec34028b1d"
      data-reference-type="entity"
      data-entity-id="558c34b4-427a-41a4-a097-94ec34028b1d"
      contenteditable="false">mbak_velfit</span>
```

Entity id verified against `gflow character list`: `mbak_velfit`
`558c34b4-427a-41a4-a097-94ec34028b1d`, `roticelup_taro`
`f4ad6b40-f055-4011-959b-61562c206782`. The chip renders in the prompt box as a
thumbnail + name label (`flow-character-ingredient-chip`, cancel ligature,
aria "Bahan"); the ingredient area shows **distinct** entities (2 thumbnails
for 3 mention spans when one name repeats).

### (c) Two chips coexist — but the driver's multi-attach call raised

Final editor DOM held three `.mention-chip` spans — `mbak_velfit`,
`mbak_velfit`, `roticelup_taro` — all `reference_type="entity"` with the right
ids. Two *different* characters coexist. But
`attach_character_entities(names=(mbak_velfit, roticelup_taro), clear=True)`
raised `ReferenceNotFoundError` ("a reference attached out of order — it did
not land after the 2 reference(s) already in the prompt"), even though the
chips ended up in the right order. Two contributing defects, both measured:

1. `clear=True` does **not** remove a pre-existing chip: Ctrl+A+Delete leaves
   `contenteditable="false"` chips in place, so the call started from 1 chip
   instead of 0 (the duplicate `mbak_velfit` is the tell).
2. `require_appended` (`migrated_existing_refs.py:30`) compares the chip prefix
   and tripped on the second attach — the ordering check is flaky when a
   duplicate/pre-existing chip is present.

For a port: attach one character per call on a verified-clean editor, or fix
`clear` to remove chips via their cancel controls and re-measure the ordering
check with duplicates present.

### (d) The directive is already an instruction

`compose_video_directive` (`agent_only_composer.py:287`) produces
"Make me a 8 second video of [prompt] in a 9:16 aspect ratio." — an
instruction, not a bare scene description. Nando's "buat video ..." is the same
shape in Indonesian. The wording is **not** the gap.

### The updated diagnosis: the gates are the blocker, not the attach

The @-mention chip path in the agent-only driver **works** (measured above,
correct entity chips). What blocks video+character is purely gflow-side, in two
places that refuse before the driver ever runs:

- `_unported_form` (`_mc_common.py:497`): `reference_entities` + mode ≠ R2V
  → "character references" — written for the classic composer, where "nothing
  downstream attaches one" (`attach_start_frame` is i2v-only,
  `attach_references` is r2v-only). On the agent-only composer that premise is
  false: `attach_character_entities` attaches them.
- `unported_video` (`agent_only_composer.py:1525`): T2V + `reference_entities`
  → "character references" → `refuse_unported` (#799).

`run_video` (`migrated_composer.py:376`) raises on the first when
`needs_classic` is true, and `run_agent_video` (`:1710`) raises on the second.
Nando's manual run proves the composer itself accepts chips + instruction and
generates. Two corrections to claims made before this spike:

1. "@ HARUS tekan Enter" is true — and `_mention_character` already does
   exactly that (type "@", wait, type the name, Enter). The video path never
   reached it because of the gates; no video run ever typed @names into the
   composer, so "gflow types @name as plain text" describes the refused path,
   not the driver's attach code.
2. "Prompt HARUS berisi perintah" is already satisfied by
   `compose_video_directive` (see (d)).

### Cleanup state

Ingredient chips (`flow-character-ingredient-chip`) survive editor Ctrl+A+Delete
**within a page session** but do **not** persist across page loads: a fresh load
after the spike showed 0 ingredient chips, 0 mention chips, empty editor
(verified by the cleanup script, same numbers). No contamination of later runs.
The spike's own cleanup left the composer empty on its final load.

### Run 1 crash (own goal, fixed in run 2)

Run 1 died on its confirm-click: it re-located the chosen button by
`chain[0]` across the whole document and clicked a grid-tile hover-footer
button (`flow-tile-hover-footer`), which is not visible. Run 2 scopes any
confirm search to the popover, reads chips before any confirm, and writes
partial findings after every phase so a crash cannot lose measurements.

## What was NOT measured

- Whether chips survive submit for **video** on the agent-only path — the
  decisive test. Needs one 10-credit live run with the gates relaxed (T2V,
  adminmandala, `mbak_velfit` + `roticelup_taro`), checking the output carries
  the characters. Out of scope for this spike (no submit by design).
- The "Tambahkan ke perintah" confirm flow: never reached for a character row;
  for a video row (preview pane) entirely unmeasured — what it attaches and
  which wire slot it lands in.
- Picker row-click semantics (toggle vs replace vs re-render): observed 2→1
  ingredient chips, ambiguous.
- Whether Nando's manual azoolabit video actually carries the characters (his
  run; not spiked, not downloaded here).
- Two-chip coexistence with `sereal_velfit` on azoolabit (same mechanism,
  different project — deliberately not touched while his run was in flight).
- Repeatability: N=1 account, N=1 project, two runs one day.

## Addendum — Nando's manual video, resolved to a media id (23 Nov 23:12 WIB)

Nando's manual run (azoolabit, "buat video @mbak_velfit review @sereal_velfit",
10 credits, Veo 3.1 - Lite) finished as the newest tile in project
`69644734-4247-43c4-8d35-90c3ecea5656`. The local `gflow data` catalog does not
know it; on a reloaded page the tile poster is an opaque `/asb/` URL with no
UUID. Resolved via the #1354 join: the tile's `/asb/` token matched against the
project's `Zzl0ze` listing (`read_project_listing` + `generation_record_by_poster`):

- **media_id: `53117631-8094-4f71-9e5c-8c6bd5f0569d`**, done, created
  2026-11-23T15:57:25Z (22:57 WIB)
- poster: `https://lh3.googleusercontent.com/asb/ANqvLOa4s7F4W9rr47_kkMZnF6vmlnyPncN_E3h_wVq5KWfs-vfHf4fLIYfn0r96ADt6BEQaaCaCMQwDIEKU1qPvERhqjh_kORp4lDEwkx1qipECtaD2ekvhTZFT7Zi8QxmtcnBheHW5wT0e8VQYau-gzyD1ykNlQo`
- cross-check: the older tile in the same grid resolved to
  `ba82e39f-439f-4dfa-ab38-3fe7111dde53` — the plain-prompt diagnostic clip from
  earlier the same evening, so the join is verified against a known media id.
- Download (0 credits): `gflow data download 53117631-8094-4f71-9e5c-8c6bd5f0569d --profile azoolabit`.

## Recommendation

1. **Port via @-mention, not via the picker.** Relax the two gates for the
   agent-only path (T2V + `reference_entities`) and route through the existing
   `attach_character_entities` + `_submit(reference_entity_names=...)` — the
   attach is implemented and now measured. Then live-verify with ONE 10-credit
   T2V run and inspect the output for the characters.
2. Attach one character per call on a verified-clean editor until the `clear`
   + ordering-check flakiness (above) is fixed and re-measured with duplicates.
3. The picker path is a separate, later port: first measure the confirm flow
   for media rows (HAR harness if the driver cannot reach it) and resolve the
   row-click semantics.
4. Pipeline next step: `/gflow:predict` on the gate-relaxation proposal, then
   `/gflow:scenario` (the observations above are the Given/When/Then), then
   `/gflow:plan`. The scenario must include a browser-bound e2e for the attach
   (the Iron Law) plus the 10-credit live-verify.
