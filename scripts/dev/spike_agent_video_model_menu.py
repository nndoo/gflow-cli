r"""Inspect video model options in the agent settings panel on flow.google.com ($0).

Zero credits: navigates to the project, opens Agent settings, clicks the video model row,
reads every offered model menu option, captures screenshot and DOM summary, then presses
Escape and closes the pane via Back without saving or submitting anything.

Usage:
    python scripts/dev/spike_agent_video_model_menu.py --profile <name> --project <id>
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
    MODEL_PICKER,
    PANE,
    PANE_BACK,
    SETTINGS_BUTTON,
)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--project", required=True)
    args = ap.parse_args()

    url = f"https://flow.google.com/project/{args.project}"
    summary: dict[str, Any] = {
        "profile": args.profile,
        "project": args.project,
        "url": url,
        "offered_video_models": [],
        "has_omni_1_2_flash": False,
    }

    async with build_client(resolve_profile_dir(args.profile)) as client:
        page = await client._context.new_page()  # noqa: SLF001
        step("navigate", f"goto {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(6_000)

        # Open settings pane
        pane = page.locator(PANE)
        if not await pane.count() or not await pane.first.is_visible():
            btn = page.locator(SETTINGS_BUTTON).last
            step("open_pane", f"clicking settings button {SETTINGS_BUTTON}")
            await btn.click(timeout=8_000)
            await pane.first.wait_for(state="visible", timeout=10_000)
            await page.wait_for_timeout(1_000)

        # Video model row
        picker = page.locator(MODEL_PICKER["video"]).first
        current_text = (await picker.inner_text()).strip()
        summary["current_video_picker_text"] = current_text
        step("video_picker", f"current picker text: {current_text!r}")

        # Click video picker to open model menu or sub-page
        step("open_menu", "clicking video model picker")
        await picker.click(timeout=5_000)
        await page.wait_for_timeout(1_500)

        # Introspect DOM for both overlay menus and sub-page elements
        dom_dump_js = """() => {
            const data = {
                overlays: [],
                panelElements: [],
                subPageDetected: false,
            };
            const overlayNodes = document.querySelectorAll(
                '.cdk-overlay-pane [role="menuitem"], ' +
                '.cdk-overlay-pane [role="option"], ' +
                '.cdk-overlay-pane button'
            );
            overlayNodes.forEach(el => {
                const text = (el.innerText || '').trim();
                if (text) data.overlays.push({
                    tag: el.tagName.toLowerCase(),
                    role: el.getAttribute('role') || '',
                    class: el.className || '',
                    text: text,
                });
            });

            const panel = document.querySelector('flow-agent-panel');
            if (panel) {
                const cands = panel.querySelectorAll(
                    'label.radio-option, .radio-option, .radio-option-text, ' +
                    'mat-radio-button, [role="radio"], mat-button-toggle, ' +
                    'mat-list-option, mat-list-item, .mat-mdc-list-item, ' +
                    'button.defaults-row, button.model-option, ' +
                    'button.option-row, button:not(.header-action)'
                );
                cands.forEach(el => {
                    const cleanSpan = el.querySelector
                        ? el.querySelector('.radio-option-text')
                        : null;
                    const rawText = (
                        (cleanSpan ? cleanSpan.innerText : el.innerText) || ''
                    ).trim();
                    let text = rawText
                        .replace(/radio_button_(?:un)?checked/g, '')
                        .replace(/arrow_(?:drop_down|forward)/g, '')
                        .split('\n')[0]
                        .trim();
                    if (!text) text = rawText;
                    if (text && text.length < 120) data.panelElements.push({
                        tag: el.tagName.toLowerCase(),
                        role: el.getAttribute('role') || '',
                        class: el.className || '',
                        text: text,
                        rawText: rawText,
                        ariaChecked: el.getAttribute('aria-checked') || '',
                    });
                });
                const headerBack = panel.querySelector('button:has(mat-icon)');
                if (headerBack) data.subPageDetected = true;
            }
            return data;
        }"""
        dom_info: dict[str, Any] = await page.evaluate(dom_dump_js)
        summary["dom_dump"] = dom_info

        offered: list[str] = []
        if dom_info["overlays"]:
            offered = [o["text"] for o in dom_info["overlays"]]
        elif dom_info["panelElements"]:
            radio_options = [
                p["text"]
                for p in dom_info["panelElements"]
                if "radio-option" in p["class"] or p["tag"] == "label"
            ]
            if radio_options:
                offered = radio_options
            else:
                keywords = ("veo", "omni", "flash", "lite", "fast", "quality", "banana", "imagen")
                offered = [
                    p["text"]
                    for p in dom_info["panelElements"]
                    if any(k in p["text"].lower() for k in keywords)
                ]
            if not offered:
                offered = [p["text"] for p in dom_info["panelElements"]]

        summary["offered_video_models"] = offered
        step("menu_options", f"offered models: {offered}")

        # Check for Omni 1.2 Flash
        has_omni = any("Omni 1.2" in m for m in offered)
        summary["has_omni_1_2_flash"] = has_omni
        step("verdict", f"Omni 1.2 Flash present: {has_omni}")

        # Capture screenshot & JSON summary to tmp / spike out
        out_png = default_out_path("spike_agent_video_model_menu", ".png")
        await page.screenshot(path=str(out_png))
        step("screenshot", f"saved to {out_png}")

        out_json = default_out_path("spike_agent_video_model_menu", ".json")
        out_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        step("summary", f"saved to {out_json}")

        # Close sub-page and pane cleanly without saving
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)
        # In sub-page navigation, first back returns to main settings, second back closes panel
        for _ in range(2):
            if not await pane.count() or not await pane.first.is_visible():
                break
            back = page.locator(PANE_BACK).last
            if await back.count() and await back.is_visible():
                await back.click(timeout=3_000)
                await page.wait_for_timeout(300)
        step("cleanup", "closed menu and pane")

    print("\nRESULT SUMMARY:")
    print(json.dumps(summary, indent=2))
    return 0 if summary["has_omni_1_2_flash"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
