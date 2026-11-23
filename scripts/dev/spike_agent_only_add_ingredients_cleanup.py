r"""Cleanup after spike_agent_only_add_ingredients: remove any leftover
flow-character-ingredient-chip attachments from the prompt box.

Run 2 measured that Ctrl+A+Delete in the editor removes .mention-chip spans
but NOT flow-character-ingredient-chip attachments — one ingredient chip was
still attached after cleanup. This script removes each ingredient chip via its
own cancel control (ligature "cancel" inside flow-character-ingredient-chip),
then verifies zero ingredient chips, zero mention chips, empty editor.
$0 — no submit, no gate approval, no generate click.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _spike_common import (  # noqa: E402, isort: skip
    build_client,
    default_out_path,
    resolve_profile_dir,
    step,
)
from gflow_cli.api.transports.agent_only_composer import (  # noqa: E402
    EDITOR,
    AgentOnlyComposer,
)

INGREDIENT_CHIP = "flow-character-ingredient-chip"
CANCEL = "flow-character-ingredient-chip button:has(mat-icon:text-is('cancel'))"

_COUNT_JS = r"""
() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const ing = [...document.querySelectorAll('flow-character-ingredient-chip')].filter(vis).map(c => ({
    aria: c.getAttribute('aria-label'), text: (c.innerText || '').trim().slice(0, 40),
  }));
  const ed = document.querySelectorAll('.ProseMirror');
  const last = ed.length ? ed[ed.length - 1] : null;
  return {
    ingredient_chips: ing.length, ingredient_detail: ing,
    mention_chips: document.querySelectorAll('.mention-chip').length,
    editor_text: last ? (last.innerText || '').trim() : null,
  };
}
"""


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--project", required=True)
    args = ap.parse_args()
    findings: dict[str, Any] = {"project": args.project}
    composer = AgentOnlyComposer()
    url = f"https://flow.google.com/project/{args.project}"
    async with build_client(resolve_profile_dir(args.profile)) as client:
        page = await client._context.new_page()  # noqa: SLF001 - spike reads the live context
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(8_000)
        state = await page.evaluate(_COUNT_JS)
        findings["before"] = state
        step("before", f"ingredient={state['ingredient_chips']} mention={state['mention_chips']} text={state['editor_text']!r}")
        for attempt in range(5):
            state = await page.evaluate(_COUNT_JS)
            if state["ingredient_chips"] == 0:
                break
            cancel = page.locator(CANCEL).first
            if not await cancel.count():
                findings["cancel_missing"] = True
                step("cancel", "no cancel control found")
                break
            await cancel.click(timeout=5_000)
            await page.wait_for_timeout(1_500)
            step("cancel", f"clicked one (attempt {attempt + 1})")
        state = await page.evaluate(_COUNT_JS)
        findings["after_cancel"] = state
        step("after_cancel", f"ingredient={state['ingredient_chips']} mention={state['mention_chips']}")
        if state["ingredient_chips"]:
            # Fallback: the prompt box clear button (aria "Hapus perintah").
            clear = page.locator("flow-base-prompt-box button.clear-button").first
            if await clear.count():
                await clear.click(timeout=5_000)
                await page.wait_for_timeout(1_500)
                state = await page.evaluate(_COUNT_JS)
                findings["after_clear_button"] = state
                step("clear_button", f"ingredient={state['ingredient_chips']}")
        # Final editor clear (mention chips + text) for a fully clean composer.
        editor = page.locator(EDITOR).last
        await editor.click(timeout=8_000)
        await page.keyboard.press("ControlOrMeta+a")
        await page.keyboard.press("Delete")
        await page.wait_for_timeout(800)
        state = await page.evaluate(_COUNT_JS)
        findings["final"] = state
        findings["clean"] = (
            state["ingredient_chips"] == 0
            and state["mention_chips"] == 0
            and not (state["editor_text"] or "").strip()
        )
        step("final", f"clean={findings['clean']} {state}")
        shot = default_out_path("spike_agent_only_add_ingredients_cleanup", ".png")
        await page.screenshot(path=str(shot))
        findings["screenshot"] = shot.name
    out = default_out_path("spike_agent_only_add_ingredients_cleanup")
    out.write_text(json.dumps(findings, indent=2), encoding="utf-8")
    step("done", f"wrote {out}")
    return 0 if findings["clean"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
