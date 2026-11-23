r"""Does #799's agent-only composer have an "Add ingredients" surface, and what does
attaching through it put in the prompt? PLUS: the @-mention picker path Nando proved
manually (23 Nov 23:00) — "@" opens the picker, Enter commits a character chip, and
the agent then asks for a 10-credit approval for the video.

Cost: $0. Navigation, DOM reads, typing "@" and attaching chips only. This script
NEVER clicks generate/submit and NEVER approves a credit gate; it contains no submit
locator at all. Attaching a chip costs nothing; generated media stays untouched.
Run it, then verify `gflow credits user --profile <name>` is unchanged.

    python scripts/dev/spike_agent_only_add_ingredients.py --profile <name> \
        --project <flow.google.com project id>

Pre-registered readings (written before the first run):

| Observation | Reading |
|---|---|
| a trigger near the prompt box opens `flow-add-menu-popover-content` (or the Koleksi asset panel) | the Add-ingredients surface exists on this composer; record its structural anchor |
| picker search offers `mbak_velfit`; committing adds a `.mention-chip` with data-entity-id = the character's id and data-reference-type="entity" | picker attach == @-mention attach (same chip); the port can use either surface |
| picker offers only media rows, or committing adds a media chip (reference_type != "entity") | the picker is a different attach surface (media ingredients); character attach stays @-only |
| no add trigger exists anywhere in the composer (inventory lists every control) | positive absence on THIS surface; the screenshot is the classic composer; the port must use @-mention chips (already in the driver) + relax the gates |
| typing "@" + a name opens no picker / offers nothing | the @-mention path does not work on this account/locale either; both attach surfaces unmeasured-then-absent |
| a chip attaches but a later read shows it gone | attach does not stick; record exactly when it drops |
| two chips (mbak_velfit + roticelup_taro) coexist in the editor | multi-character prompts are attachable; the port can attach presenter + product together |

Run 1 (2026-11-23 15:59 UTC) already measured: "@" opens the picker (14 options,
mbak_velfit offered); the driver's attach committed chip mbak_velfit with the RIGHT
entity id (558c34b4-...) and reference_type="entity"; the add trigger is
`button.add-menu-trigger` (ligature "add", aria "Tambahkan bahan ke kotak perintah",
chain flow-add-menu > flow-base-prompt-box); picker search narrows to mbak_velfit and
the row click closes the popover. Run 1 crashed on its own confirm-click logic
before reading the chips after the row pick — run 2 answers that, plus two chips.

Characters under test (adminmandala project 5471bc69-cb82-48c2-bfe8-306081348bda):
mbak_velfit 558c34b4-427a-41a4-a097-94ec34028b1d,
roticelup_taro f4ad6b40-f055-4011-959b-61562c206782.
(Nando's manual proof used azoolabit + sereal_velfit; this spike stays on adminmandala
and does not touch that run.)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _spike_common import (  # noqa: E402, isort: skip
    build_client,
    default_out_path,
    resolve_profile_dir,
    step,
)

from gflow_cli.api.transports._mc_common import (  # noqa: E402
    _OPTION_TITLES_JS,
    PICKER,
    PICKER_OPTION,
)
from gflow_cli.api.transports.agent_only_composer import (  # noqa: E402
    EDITOR,
    AgentOnlyComposer,
    compose_video_directive,
)

MBAK_VELFIT = "mbak_velfit"
MBAK_VELFIT_ID = "558c34b4-427a-41a4-a097-94ec34028b1d"
ROTICELUP_TARO = "roticelup_taro"
ROTICELUP_TARO_ID = "f4ad6b40-f055-4011-959b-61562c206782"

# Copied from spike_agent_only_composer.py: the same page inventory, so a
# before/after diff means the same thing in both captures.
_INVENTORY_JS = r"""
() => {
  const visible = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const ligs = (el) => [...el.querySelectorAll('mat-icon, i')].map(i => (i.textContent || '').trim()).filter(Boolean);
  const chain = (el) => {
    const out = [];
    for (let n = el; n && n !== document.body && out.length < 6; n = n.parentElement)
      if (n.tagName.includes('-')) out.push(n.tagName.toLowerCase());
    return out;
  };
  const media = [...document.querySelectorAll('img, video, source')].map(e => ({
    tag: e.tagName.toLowerCase(), src: (e.currentSrc || e.src || '').slice(0, 300),
    w: e.naturalWidth || e.videoWidth || 0, h: e.naturalHeight || e.videoHeight || 0,
    chain: chain(e), visible: visible(e),
  }));
  const controls = [...document.querySelectorAll(
    "button, [role=tab], [role=radio], [role=option], [role=switch], [role=combobox], mat-select, mat-button-toggle, input, [contenteditable=true]"
  )].filter(visible).map(e => ({
    tag: e.tagName.toLowerCase(), role: e.getAttribute('role'), aria: e.getAttribute('aria-label'),
    pressed: e.getAttribute('aria-pressed'), selected: e.getAttribute('aria-selected'),
    checked: e.getAttribute('aria-checked'), ligatures: ligs(e),
    classes: [...e.classList].filter(c => !c.startsWith('mat-') && !c.startsWith('mdc-')).slice(0, 4),
    text: (e.innerText || '').trim().slice(0, 40), chain: chain(e),
  }));
  const custom = {};
  for (const e of document.querySelectorAll('*')) {
    const t = e.tagName.toLowerCase(); if (t.includes('-')) custom[t] = (custom[t] || 0) + 1;
  }
  return {
    url: location.href, lang: document.documentElement.lang,
    prosemirror: document.querySelectorAll('.ProseMirror').length,
    agent_chip: document.querySelectorAll('button.agent-mode-chip').length,
    dialogs: [...document.querySelectorAll('[role=dialog], [role=alert], [aria-live]')].filter(visible)
      .map(d => ({ role: d.getAttribute('role'), live: d.getAttribute('aria-live'), text: (d.innerText || '').slice(0, 300) })),
    chat_text: [...document.querySelectorAll('[class*=message], [class*=turn], [class*=chat]')]
      .filter(visible).map(e => (e.innerText || '').trim()).filter(Boolean).slice(-8).map(t => t.slice(0, 400)),
    media, controls, custom_tags: custom,
  };
}
"""

#: Every control inside the prompt-box containers, with the structural anchors a
#: port would click. The submit button is deliberately NOT listed here: this
#: spike never submits, so it never needs that locator.
_PROMPT_BOX_JS = r"""
() => {
  const visible = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const ligs = (el) => [...el.querySelectorAll('mat-icon, i')].map(i => (i.textContent || '').trim()).filter(Boolean);
  const chain = (el) => {
    const out = [];
    for (let n = el; n && n !== document.body && out.length < 6; n = n.parentElement)
      if (n.tagName.includes('-')) out.push(n.tagName.toLowerCase());
    return out;
  };
  const boxes = [...document.querySelectorAll(
    'flow-creative-agent-prompt-box, flow-base-prompt-box, flow-prompt-box'
  )];
  const out = [];
  for (const box of boxes) {
    for (const e of box.querySelectorAll('button, [role=button], [contenteditable=true], input')) {
      if (!visible(e)) continue;
      const r = e.getBoundingClientRect();
      out.push({
        tag: e.tagName.toLowerCase(), role: e.getAttribute('role'),
        aria: e.getAttribute('aria-label'), ligatures: ligs(e),
        classes: [...e.classList].filter(c => !c.startsWith('mat-') && !c.startsWith('mdc-')).slice(0, 4),
        text: (e.innerText || '').trim().slice(0, 40), chain: chain(e),
        box: box.tagName.toLowerCase(), x: Math.round(r.x), y: Math.round(r.y),
      });
    }
  }
  const editor = document.querySelectorAll('.ProseMirror');
  const last = editor.length ? editor[editor.length - 1] : null;
  return {
    boxes: boxes.map(b => b.tagName.toLowerCase()),
    editor_text: last ? (last.innerText || '').trim().slice(0, 200) : null,
    controls: out,
  };
}
"""

#: What an open picker/popover holds: option rows (titles are asset names, user
#: data), the search input, filter tabs, and every button in the panel with its
#: structural anchor — so the confirm control is identified by position and
#: ligature, never by its (translated) label.
_PICKER_JS = r"""
() => {
  const visible = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const ligs = (el) => [...el.querySelectorAll('mat-icon, i')].map(i => (i.textContent || '').trim()).filter(Boolean);
  const chain = (el) => {
    const out = [];
    for (let n = el; n && n !== document.body && out.length < 6; n = n.parentElement)
      if (n.tagName.includes('-')) out.push(n.tagName.toLowerCase());
    return out;
  };
  const pop = document.querySelector('flow-add-menu-popover-content');
  const options = [...document.querySelectorAll("button.asset-item[role='option']")].filter(visible)
    .map(o => {
      const r = o.getBoundingClientRect();
      const img = o.querySelector('img');
      return {
        title: ((o.querySelector('.asset-title') || {}).textContent || '').trim(),
        kind_text: (o.innerText || '').trim().replace(/\n+/g, ' | ').slice(0, 80),
        has_img: !!img, x: Math.round(r.x), y: Math.round(r.y),
      };
    });
  const scope = pop || document;
  const inputs = [...scope.querySelectorAll('input')].filter(visible).map(i => ({
    type: i.getAttribute('type'), id: i.getAttribute('id'),
    placeholder: (i.getAttribute('placeholder') || '').slice(0, 40),
    aria: i.getAttribute('aria-label'),
  }));
  const tabs = [...scope.querySelectorAll('[role=tab]')].filter(visible).map(t => ({
    text: (t.innerText || '').trim().slice(0, 30),
    selected: t.getAttribute('aria-selected'),
  }));
  const buttons = [...scope.querySelectorAll('button')].filter(visible).map(b => {
    const r = b.getBoundingClientRect();
    return {
      ligatures: ligs(b), text: (b.innerText || '').trim().slice(0, 40),
      classes: [...b.classList].filter(c => !c.startsWith('mat-') && !c.startsWith('mdc-')).slice(0, 3),
      chain: chain(b), x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width),
    };
  });
  return {
    popover_present: !!pop,
    option_count: options.length, options: options.slice(0, 20),
    inputs, tabs, buttons,
  };
}
"""

#: The attached chips as the DOM holds them: outer HTML (truncated) plus every
#: data attribute — the exact shape a port must produce and verify.
_CHIP_DOM_JS = r"""
() => {
  const chips = [...document.querySelectorAll('.mention-chip')].map(c => ({
    tag: c.tagName.toLowerCase(),
    text: (c.textContent || '').trim(),
    entity_id: c.getAttribute('data-entity-id') || '',
    reference_type: c.getAttribute('data-reference-type') || '',
    classes: [...c.classList].slice(0, 6),
    html: c.outerHTML.slice(0, 500),
  }));
  const editor = document.querySelectorAll('.ProseMirror');
  const last = editor.length ? editor[editor.length - 1] : null;
  return {
    chips,
    editor_html: last ? last.innerHTML.slice(0, 1500) : null,
  };
}
"""


class ProbeFailedError(RuntimeError):
    """A step this probe cannot complete. Never downgraded to a verdict."""


def _save_partial(findings: dict[str, Any]) -> None:
    """Write findings after every phase: a crash must not lose what was measured."""
    partial = default_out_path("spike_agent_only_add_ingredients_partial")
    partial.write_text(json.dumps(findings, indent=2), encoding="utf-8")


async def _snap(page: Any, tag: str, findings: dict[str, Any]) -> dict[str, Any]:
    inv = await page.evaluate(_INVENTORY_JS)
    shot = default_out_path(f"spike_agent_only_add_ingredients_{tag}", ".png")
    await page.screenshot(path=str(shot))
    inv["screenshot"] = shot.name
    findings[tag] = inv
    step(
        tag,
        f"controls={len(inv['controls'])} media={len(inv['media'])} "
        f"prosemirror={inv['prosemirror']} chip={inv['agent_chip']} dialogs={len(inv['dialogs'])}",
    )
    _save_partial(findings)
    return inv


async def _prompt_box(page: Any, findings: dict[str, Any], tag: str) -> dict[str, Any]:
    pb = await page.evaluate(_PROMPT_BOX_JS)
    findings[tag] = pb
    step(tag, f"boxes={pb['boxes']} controls={len(pb['controls'])} editor_text={pb['editor_text']!r}")
    _save_partial(findings)
    return pb


async def _picker_state(page: Any, findings: dict[str, Any], tag: str) -> dict[str, Any]:
    pk = await page.evaluate(_PICKER_JS)
    findings[tag] = pk
    step(
        tag,
        f"popover={pk['popover_present']} options={pk['option_count']} "
        f"inputs={len(pk['inputs'])} tabs={len(pk['tabs'])} buttons={len(pk['buttons'])}",
    )
    _save_partial(findings)
    return pk


async def _chip_dom(page: Any, findings: dict[str, Any], tag: str) -> dict[str, Any]:
    dom = await page.evaluate(_CHIP_DOM_JS)
    findings[tag] = dom
    step(tag, f"chips={len(dom['chips'])}")
    _save_partial(findings)
    return dom


async def _clear_editor(page: Any, composer: AgentOnlyComposer) -> list[dict[str, str]]:
    editor = page.locator(EDITOR).last
    await editor.click(timeout=8_000)
    await page.keyboard.press("ControlOrMeta+a")
    await page.keyboard.press("Delete")
    await page.wait_for_timeout(800)
    return await composer.read_chips(page)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--project", required=True)
    args = ap.parse_args()

    findings: dict[str, Any] = {
        "project": args.project,
        "question": "#799 agent-only composer: Add ingredients surface + @-mention chips",
        "characters": {
            MBAK_VELFIT: MBAK_VELFIT_ID,
            ROTICELUP_TARO: ROTICELUP_TARO_ID,
        },
        # (d) The driver's video directive shape, recorded without a browser:
        # an instruction, not a bare scene description.
        "directive_shape": compose_video_directive("[prompt]", 8, "9:16"),
    }
    url = f"https://flow.google.com/project/{args.project}"
    composer = AgentOnlyComposer()
    try:
        async with build_client(resolve_profile_dir(args.profile)) as client:
            page = await client._context.new_page()  # noqa: SLF001 - spike reads the live context
            step("goto", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(8_000)
            if "flow.google.com/project/" not in page.url:
                raise ProbeFailedError(f"did not land on a project page (got {page.url})")

            await _snap(page, "inventory", findings)
            pb = await _prompt_box(page, findings, "prompt_box_initial")
            if pb["editor_text"]:
                findings["draft_cleared"] = pb["editor_text"]
                await _clear_editor(page, composer)

            # --- (a)/(e) Typing "@" in the ProseMirror: does a picker open? ------
            editor = page.locator(EDITOR).last
            await editor.click(timeout=8_000)
            await page.keyboard.type("@", delay=120)
            await page.wait_for_timeout(2_200)
            at_open = await page.evaluate(_PICKER_JS)
            findings["at_sign_picker"] = at_open
            step(
                "at_sign",
                f"popover={at_open['popover_present']} options={at_open['option_count']} "
                f"titles={[o['title'] for o in at_open['options'][:5]]}",
            )
            await _snap(page, "at_sign", findings)
            await page.keyboard.type(MBAK_VELFIT, delay=100)
            await page.wait_for_timeout(2_500)
            titles = await page.evaluate(_OPTION_TITLES_JS, PICKER_OPTION)
            findings["at_mention_typed"] = {"query": MBAK_VELFIT, "titles": titles}
            step("at_mention_typed", f"titles={titles}")
            await _snap(page, "at_mention_typed", findings)
            # Clear the query WITHOUT committing: backspace the text, then Escape.
            for _ in range(len(MBAK_VELFIT) + 1):
                await page.keyboard.press("Backspace")
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(800)
            findings["chips_after_at_probe"] = await composer.read_chips(page)
            _save_partial(findings)

            # --- (b) Commit one @-mention chip via the driver's own path ---------
            try:
                committed = await composer.attach_character_entities(
                    page, names=(MBAK_VELFIT,), entity_ids=(MBAK_VELFIT_ID,), clear=True
                )
                findings["mention_chip_committed"] = {
                    "entity_ids": list(committed),
                    "chips": await composer.read_chips(page),
                }
                step("mention_chip", f"committed={committed} chips={findings['mention_chip_committed']['chips']}")
            except Exception as exc:  # noqa: BLE001 - a failed attach IS the finding
                findings["mention_chip_committed"] = {"error": f"{type(exc).__name__}: {exc}"[:500]}
                step("mention_chip", f"FAILED: {str(exc)[:200]}")
            await _snap(page, "mention_chip", findings)
            await _chip_dom(page, findings, "chip_dom_single")
            await _prompt_box(page, findings, "prompt_box_after_mention")

            # --- (c) Two chips side by side, via the driver's own path ----------
            try:
                committed2 = await composer.attach_character_entities(
                    page,
                    names=(MBAK_VELFIT, ROTICELUP_TARO),
                    entity_ids=(MBAK_VELFIT_ID, ROTICELUP_TARO_ID),
                    clear=True,
                )
                findings["two_chips_committed"] = {
                    "entity_ids": list(committed2),
                    "chips": await composer.read_chips(page),
                }
                step("two_chips", f"committed={committed2} chips={findings['two_chips_committed']['chips']}")
            except Exception as exc:  # noqa: BLE001 - a failed attach IS the finding
                findings["two_chips_committed"] = {"error": f"{type(exc).__name__}: {exc}"[:500]}
                step("two_chips", f"FAILED: {str(exc)[:200]}")
            await _snap(page, "two_chips", findings)
            await _chip_dom(page, findings, "chip_dom_two")
            await _prompt_box(page, findings, "prompt_box_after_two_chips")

            # --- Clear before the picker probe: its result must be unambiguous ---
            findings["chips_before_picker"] = await _clear_editor(page, composer)
            step("cleared_for_picker", f"chips={findings['chips_before_picker']}")
            _save_partial(findings)

            # --- (a) The Add-ingredients trigger: candidates from the prompt box --
            pb = await _prompt_box(page, findings, "prompt_box_before_picker")
            candidates: list[dict[str, Any]] = []
            for c in pb["controls"]:
                chain = c.get("chain") or []
                ligs = c.get("ligatures") or []
                # Skip the editor itself, the submit control, and the settings
                # pane triggers (tune / article_spark) — none opens the picker.
                if "flow-generate-icon-button" in chain or "flow-stop-icon-button" in chain:
                    continue
                if any(lig in ("tune", "article_spark", "arrow_forward", "stop") for lig in ligs):
                    continue
                if c["tag"] == "button" or c.get("role") == "button":
                    candidates.append(c)
            findings["picker_trigger_candidates"] = candidates
            step("candidates", f"{len(candidates)} buttons near the prompt box")
            _save_partial(findings)

            picker_opened: dict[str, Any] | None = None
            for i, cand in enumerate(candidates[:4]):
                chain = cand.get("chain") or []
                scope = chain[0] if chain else None
                loc = page.locator(scope).last if scope else None
                if loc is None or not await loc.count():
                    continue
                btn = loc.locator("button").first
                try:
                    await btn.click(timeout=5_000)
                except Exception as exc:  # noqa: BLE001 - a miss is evidence, not a crash
                    step(f"candidate_{i}", f"click FAILED: {str(exc)[:150]}")
                    continue
                await page.wait_for_timeout(2_500)
                pk = await _picker_state(page, findings, f"picker_after_candidate_{i}")
                if pk["popover_present"] or pk["option_count"]:
                    picker_opened = {"candidate": cand, "state": pk}
                    step("picker_opened", f"candidate {i}: {cand}")
                    await _snap(page, f"picker_open_{i}", findings)
                    break
                # Nothing opened: dismiss whatever this was and try the next one.
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(800)
            findings["picker_opened"] = picker_opened
            _save_partial(findings)

            # --- (b–d) Inside the picker: search, select, read chips ------------
            if picker_opened is not None:
                try:
                    search = page.locator("#add-menu-input").first
                    if not await search.count():
                        search = page.locator(PICKER).locator("input[type='text']").first
                    if await search.count():
                        await search.fill(MBAK_VELFIT)
                        await page.wait_for_timeout(2_500)
                    titles = await page.evaluate(_OPTION_TITLES_JS, PICKER_OPTION)
                    findings["picker_search_mbak"] = {"query": MBAK_VELFIT, "titles": titles}
                    step("picker_search", f"titles={titles}")
                    await _snap(page, "picker_search", findings)
                    # Select the row titled exactly mbak_velfit, if offered.
                    idx = next(
                        (n for n, t in enumerate(titles) if t.strip().casefold() == MBAK_VELFIT.casefold()),
                        None,
                    )
                    picked = idx is not None
                    if picked:
                        await page.locator(PICKER_OPTION).nth(idx).click(timeout=5_000)
                    findings["picker_row_picked"] = {"character": MBAK_VELFIT, "picked": picked}
                    step("picker_row", f"picked={picked}")
                    await page.wait_for_timeout(2_000)
                    # Read the chips FIRST: run 1 showed the popover closes on
                    # row click, and the row click alone may already attach.
                    findings["chips_after_row_pick"] = await composer.read_chips(page)
                    step("picker_chips", f"chips={findings['chips_after_row_pick']}")
                    await _snap(page, "picker_row_picked", findings)
                    await _chip_dom(page, findings, "chip_dom_after_row_pick")
                    await _prompt_box(page, findings, "prompt_box_after_row_pick")
                    if not findings["chips_after_row_pick"]:
                        # No chip yet: look for a confirm surface, scoped to the
                        # picker popover only — never the whole document (run 1's
                        # crash came from a grid-tile footer button).
                        pk = await _picker_state(page, findings, "picker_after_row")
                        if pk["popover_present"]:
                            confirms = [
                                b
                                for b in pk["buttons"]
                                if "close" not in (b["ligatures"] or [])
                                and "arrow_back" not in (b["ligatures"] or [])
                            ]
                            findings["picker_confirm_candidates"] = confirms
                            if confirms:
                                target = max(confirms, key=lambda b: b["y"])
                                findings["picker_confirm_chosen"] = target
                                chain = target.get("chain") or []
                                scope = chain[0] if chain else None
                                btn = (
                                    page.locator(PICKER).locator("button").last
                                    if scope
                                    else None
                                )
                                if btn is not None and await btn.count():
                                    try:
                                        await btn.click(timeout=5_000)
                                        await page.wait_for_timeout(2_000)
                                    except Exception as exc:  # noqa: BLE001 - recorded
                                        findings["picker_confirm_error"] = str(exc)[:400]
                            findings["chips_after_confirm"] = await composer.read_chips(page)
                            step("picker_confirm", f"chips={findings['chips_after_confirm']}")
                            await _snap(page, "picker_confirmed", findings)
                        else:
                            findings["picker_confirm_candidates"] = []
                            findings["picker_confirm_note"] = (
                                "popover closed after row pick and no chip appeared; "
                                "no confirm surface present"
                            )
                            step("picker_confirm", "no popover, no chip: nothing to confirm")
                except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
                    findings["picker_phase_error"] = f"{type(exc).__name__}: {exc}"[:500]
                    step("picker_phase", f"FAILED: {str(exc)[:200]}")
                _save_partial(findings)

            # --- Cleanup: leave the composer exactly as found --------------------
            try:
                findings["chips_after_cleanup"] = await _clear_editor(page, composer)
            except Exception as exc:  # noqa: BLE001 - cleanup failure is recorded
                findings["cleanup_error"] = str(exc)[:400]
                findings["chips_after_cleanup"] = await composer.read_chips(page)
            pb_final = await _prompt_box(page, findings, "prompt_box_cleaned")
            findings["cleanup_ok"] = (
                not findings["chips_after_cleanup"] and not (pb_final["editor_text"] or "").strip()
            )
            step("cleanup", f"ok={findings['cleanup_ok']} chips={findings['chips_after_cleanup']}")
            await _snap(page, "cleaned", findings)
    finally:
        out = default_out_path("spike_agent_only_add_ingredients")
        out.write_text(json.dumps(findings, indent=2), encoding="utf-8")
        step("done", f"wrote {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except ProbeFailedError as exc:
        print(f"[spike] PROBE FAILED: {exc}", file=sys.stderr, flush=True)
        sys.exit(3)
