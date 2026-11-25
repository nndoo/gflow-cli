r"""Why does an agent-only video submit produce no gate and no pending tile?

Cost: ``inventory`` and ``attach`` are $0 (navigation, DOM reads, settings pane
opened WITHOUT saving, mention chips attached and then cleared; no submit).
``submit-reject`` submits ONE directive through the production driver path and
REJECTS the credit gate if one appears, so it spends 0 credits when the gate
surface works. If a pending tile appears instead, generation has started and
credits may be spent; the spike stops and says so.

    python scripts/dev/spike_agent_only_gate_submit.py --profile <name> \
        --project <id> --mode inventory|attach|submit-reject \
        [--names mbak_velfit,roticelup_strawberry] [--prompt-file /tmp/prompt.txt]

Pre-registered readings (written before the first run):

- Submit button absent/disabled/occluded, or editor text does not land:
  the click path is the defect; a timeout after it is not evidence
  about Flow.
- Editor clears, a new chat bubble appears, live gate count rises:
  submit landed and the gate selector sees the gate; reject it and stop.
- Editor clears, a new chat bubble appears, gate rows exist but
  LIVE_GATE stays at baseline: the gate selector/baseline is the
  defect; record the row shape.
- Editor clears, a new chat bubble appears, no gate rows, no pending,
  turn goes idle: the agent answered without starting a generation;
  record the bubble outline and RPCs.
- Editor does not clear and no bubble appears: the submit click did
  not land as a send.
- Pending tile appears: generation started; the submit/gate path
  works on this run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from gflow_cli import auth as auth_mod
from gflow_cli.api.client import FlowApiClient
from gflow_cli.api.transports.agent_only_composer import (
    EDITOR,
    PANE,
    PANE_BACK,
    AgentOnlyComposer,
    _confirm_setting,  # noqa: PLC2701 - spike mirrors the production run
    _restore_quietly,  # noqa: PLC2701 - spike mirrors the production run
    compose_video_directive,
)
from gflow_cli.api.transports.migrated_composer import (
    ASPECT_LIGATURE,
    VIDEO_MODEL_MENU_MATCHERS,
    MigratedComposer,
)
from gflow_cli.api.video import Aspect, VideoModel

_SNAPSHOT_JS = r"""
() => {
  const ligs = (el) => [...el.querySelectorAll('mat-icon, i.google-symbols')]
    .map(i => (i.textContent || '').trim()).filter(Boolean);
  const visible = (e) => {
    const r = e.getBoundingClientRect();
    const cs = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const centerHit = (e) => {
    const r = e.getBoundingClientRect();
    const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
    return hit ? {tag: hit.tagName.toLowerCase(), classes: [...hit.classList].slice(0, 5),
      ligatures: ligs(hit)} : null;
  };
  const submits = [...document.querySelectorAll('flow-generate-icon-button button')]
    .map((b, i) => ({i, visible: visible(b), disabled: !!b.disabled,
      aria_disabled: b.getAttribute('aria-disabled'), classes: [...b.classList].slice(0, 6),
      ligatures: ligs(b), hit: centerHit(b)}));
  const gates = [...document.querySelectorAll('flow-permission-message')].map((g, i) => {
    const rows = [...g.querySelectorAll("div[role='radio'].option-row")];
    const title = (g.querySelector('p.title')?.textContent || '');
    return {i, rows: rows.length,
      live_rows: rows.filter(r => !r.classList.contains('read-only')).length,
      readonly_rows: rows.filter(r => r.classList.contains('read-only')).length,
      ligatures: ligs(g), title_len: title.trim().length,
      title_numbers: (title.match(/\d+/g) || []).map(Number), visible: visible(g)};
  });
  const liveGateCount = document.querySelectorAll(
    "flow-permission-message:has(div[role='radio'].option-row:not(.read-only))").length;
  const editors = [...document.querySelectorAll('div.ProseMirror')];
  const ed = editors.length ? editors[editors.length - 1] : null;
  const chips = ed ? [...ed.querySelectorAll('.mention-chip')].map(c => ({
    text: (c.textContent || '').trim(),
    reference_type: c.getAttribute('data-reference-type') || '',
    has_entity_id: !!c.getAttribute('data-entity-id')})) : [];
  const pending = document.querySelectorAll('flow-video-tile flow-pending-tile').length;
  const finished = [...document.querySelectorAll('flow-video-tile')]
    .filter(t => !t.querySelector('flow-pending-tile'));
  const head = finished.length ? finished[0] : null;
  const headSrc = head ? ((head.querySelector('video')?.currentSrc ||
    head.querySelector('video')?.src || head.querySelector('img')?.currentSrc ||
    head.querySelector('img')?.src || '')) : '';
  const headKind = headSrc.includes('flow-content.google/video/') ? 'cdn'
    : (headSrc.includes('/asb/') ? 'asb' : (headSrc ? 'other' : 'none'));
  const bubbles = [...document.querySelectorAll('flow-chat-bubble')];
  const last = bubbles.length ? bubbles[bubbles.length - 1] : null;
  const outline = last ? [...last.querySelectorAll('*')].map(e => ({
    tag: e.tagName.toLowerCase(), role: e.getAttribute('role'),
    classes: [...e.classList].slice(0, 4), ligatures: ligs(e),
    text_len: (e.innerText || '').trim().length}))
    .filter(x => x.tag.includes('-') || x.role || x.ligatures.length || x.classes.length)
    .slice(0, 80) : [];
  const custom = {};
  for (const t of ['flow-creative-agent-prompt-box', 'flow-generate-icon-button',
      'flow-stop-icon-button', 'flow-permission-message', 'flow-chat-bubble',
      'flow-video-tile', 'flow-pending-tile', 'flow-a2ui-video-option',
      'flow-unified-settings-view', 'flow-settings-view']) {
    custom[t] = document.querySelectorAll(t).length;
  }
  return {
    url_path: location.pathname.replace(/\/project\/[^/]+/, '/project/<id>'),
    editors: editors.length,
    editor_text_len: ed ? (ed.innerText || '').trim().length : 0,
    chips, submits,
    in_flight: document.querySelectorAll('flow-stop-icon-button').length,
    live_gate_count: liveGateCount, gates,
    pending, finished: finished.length, head_kind: headKind,
    options: document.querySelectorAll('flow-a2ui-video-option img').length,
    bubbles: bubbles.length, last_bubble_outline: outline, custom,
  };
}
"""

_SETTINGS_JS = r"""
() => {
  const pane = document.querySelector('flow-unified-settings-view, flow-settings-view');
  if (!pane) return {present: false};
  const groups = [...pane.querySelectorAll('mat-button-toggle-group')].map((g, gi) => {
    const radios = [...g.querySelectorAll("button[role='radio'], mat-button-toggle, button")];
    return {group: gi, radios: radios.length,
      checked_index: radios.findIndex(b => b.getAttribute('aria-checked') === 'true' ||
        b.classList.contains('mat-button-toggle-checked')),
      values: radios.map(b => b.getAttribute('value'))};
  });
  const inputs = [...pane.querySelectorAll("mat-radio-button input[type='radio']")]
    .map(i => ({value: i.getAttribute('value'), checked: !!i.checked}));
  const ligs = [...pane.querySelectorAll('mat-icon, i.google-symbols')]
    .map(i => (i.textContent || '').trim()).filter(Boolean);
  return {present: true, unified: !!document.querySelector('flow-unified-settings-view'),
    groups, confirm_inputs: inputs, ligatures: [...new Set(ligs)].sort(),
    text_len: (pane.innerText || '').trim().length};
}
"""


def _tap_network(page: Any, findings: dict[str, Any]) -> None:
    calls: list[dict[str, Any]] = findings.setdefault("rpc_calls", [])
    t0 = time.monotonic()

    async def on_response(response: Any) -> None:
        url = str(response.url)
        if "batchexecute" not in url:
            return
        rpcids = url.split("rpcids=", 1)[1].split("&", 1)[0] if "rpcids=" in url else "?"
        calls.append(
            {"t": round(time.monotonic() - t0, 1), "rpcids": rpcids, "status": response.status}
        )

    page.on("response", on_response)


async def _snapshot(page: Any) -> dict[str, Any]:
    return await page.evaluate(_SNAPSHOT_JS)


async def _clear_editor(page: Any) -> None:
    editor = page.locator(EDITOR).last
    await editor.click(timeout=8_000)
    await page.keyboard.press("ControlOrMeta+a")
    await page.keyboard.press("Delete")
    await page.wait_for_timeout(800)


async def _settings_snapshot(page: Any, composer: AgentOnlyComposer) -> dict[str, Any]:
    await composer._open_pane(page)  # noqa: SLF001 - spike reads the production pane
    await page.wait_for_timeout(1_500)
    snap = await page.evaluate(_SETTINGS_JS)
    backs = page.locator(PANE_BACK)
    if await backs.count():
        await backs.last.click(timeout=5_000)
    else:
        await page.keyboard.press("Escape")
    try:
        await page.locator(PANE).first.wait_for(state="hidden", timeout=8_000)
    except Exception:  # noqa: BLE001 - the snapshot records whether the pane stayed open
        snap["pane_still_open"] = True
    await page.wait_for_timeout(1_000)
    return snap


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument(
        "--mode",
        choices=["inventory", "attach", "submit-reject", "prod-submit-reject"],
        default="inventory",
    )
    ap.add_argument("--names", default="mbak_velfit,roticelup_strawberry")
    ap.add_argument("--prompt-file", default=None)
    ap.add_argument("--prompt", default="a small blue paper boat on a calm pond")
    ap.add_argument("--wait-s", type=float, default=180.0)
    ap.add_argument("--out", default="tmp/spike_agent_only_gate_submit.json")
    args = ap.parse_args()

    names = tuple(n.strip() for n in args.names.split(",") if n.strip())
    prompt = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else args.prompt
    directive = compose_video_directive(prompt, 8, "9:16")
    findings: dict[str, Any] = {
        "mode": args.mode,
        "names": list(names),
        "prompt_len": len(prompt),
        "directive_len": len(directive),
        "confirm_setting": _confirm_setting(),
    }
    composer = AgentOnlyComposer()
    profile_dir = auth_mod.profile_dir(args.profile)
    async with FlowApiClient(profile_dir=profile_dir, headless=False) as client:
        if args.mode == "prod-submit-reject":
            # Mirror production: the transport's shared page, then ensure_editor_for.
            page = client._pages[0]  # noqa: SLF001 - spike mirrors the production page
            findings["page_kind"] = "shared"
            _tap_network(page, findings)
            findings["before_ensure"] = await _snapshot(page)
            surface = await MigratedComposer().ensure_editor_for(
                page, args.project, needs_classic=False
            )
            findings["surface"] = surface
            findings["after_ensure"] = await _snapshot(page)
        else:
            page = await client._context.new_page()  # noqa: SLF001 - spike reads the live context
            findings["page_kind"] = "new"
            _tap_network(page, findings)
            await page.goto(
                f"https://flow.google.com/project/{args.project}",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            await page.wait_for_timeout(8_000)
            findings["initial"] = await _snapshot(page)
            findings["settings"] = await _settings_snapshot(page, composer)
            findings["after_settings"] = await _snapshot(page)

        if args.mode == "attach":
            await _clear_editor(page)
            findings["cleared"] = await _snapshot(page)
            for name in names:
                committed = await composer.attach_character_entities(
                    page, names=(name,), clear=False
                )
                findings[f"after_attach_{name}"] = {
                    "committed_count": len(committed),
                    "snapshot": await _snapshot(page),
                }
            await page.evaluate(
                "(sel) => { const all = document.querySelectorAll(sel);"
                " const ce = all.length ? all[all.length - 1] : null; if (!ce) return;"
                " ce.focus(); const r = document.createRange(); r.selectNodeContents(ce);"
                " r.collapse(false); const s = window.getSelection();"
                " s.removeAllRanges(); s.addRange(r); }",
                EDITOR,
            )
            await page.keyboard.insert_text(f" {directive}")
            await page.wait_for_timeout(1_000)
            findings["after_insert"] = await _snapshot(page)
            await _clear_editor(page)
            findings["cleaned"] = await _snapshot(page)

        if args.mode in ("submit-reject", "prod-submit-reject"):
            snapshot = await composer.apply_defaults(
                page,
                section="video",
                aspect_ligature=ASPECT_LIGATURE[Aspect.PORTRAIT],
                count=1,
                model=VIDEO_MODEL_MENU_MATCHERS[VideoModel.VEO_3_1_LITE],
                confirm=_confirm_setting(),
            )
            baseline = await _snapshot(page)
            findings["baseline"] = baseline
            baseline_live = int(baseline["live_gate_count"])
            baseline_bubbles = int(baseline["bubbles"])
            await composer._submit(page, directive, reference_entity_names=names)  # noqa: SLF001
            findings["just_after_submit"] = await _snapshot(page)
            timeline: list[dict[str, Any]] = []
            findings["timeline"] = timeline
            started = time.monotonic()
            saw_in_flight = False
            idle_since: float | None = None
            pending_seen = False
            while time.monotonic() - started < args.wait_s:
                await page.wait_for_timeout(2_000)
                snap = await _snapshot(page)
                elapsed = round(time.monotonic() - started, 1)
                timeline.append(
                    {
                        "t": elapsed,
                        "live_gate_count": snap["live_gate_count"],
                        "gate_live_rows": sum(g["live_rows"] for g in snap["gates"]),
                        "pending": snap["pending"],
                        "in_flight": snap["in_flight"],
                        "editor_text_len": snap["editor_text_len"],
                        "bubbles": snap["bubbles"],
                        "head_kind": snap["head_kind"],
                    }
                )
                if int(snap["live_gate_count"]) > baseline_live:
                    findings["gate_seen"] = snap
                    rejected = await composer._reject_gate(page, baseline_live)  # noqa: SLF001
                    findings["gate_rejected"] = rejected
                    await page.wait_for_timeout(3_000)
                    findings["after_reject"] = await _snapshot(page)
                    break
                if int(snap["pending"]) > 0:
                    findings["pending_seen"] = snap
                    pending_seen = True
                    break
                if int(snap["in_flight"]) > 0:
                    saw_in_flight = True
                    idle_since = None
                elif saw_in_flight or int(snap["bubbles"]) > baseline_bubbles or elapsed > 10:
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since > 20:
                        findings["idle"] = snap
                        break
            else:
                findings["wait_exhausted"] = await _snapshot(page)
            if pending_seen:
                findings["defaults_restored"] = False
                findings["defaults_note"] = "pending seen; defaults left applied"
            else:
                await _restore_quietly(composer, page, snapshot)
                findings["defaults_restored"] = True
                findings["after_restore"] = await _snapshot(page)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(findings, indent=2), encoding="utf-8")
    print(json.dumps(findings, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
