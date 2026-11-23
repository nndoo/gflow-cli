"""#799's agent-only cohort, discriminated against a real CSS engine.

The whole fix turns on one piece of third-party behaviour: whether Playwright reports
a `<button hidden="">` as present-but-not-visible. If it reported it visible, the
readiness gate would never have failed and none of this code would run; if it reported
it absent, the guard would fall through to selector drift and #799 would be unchanged.
A fake answers whatever it was written to answer, so it cannot settle that — only a
real browser can, and AGENTS.md asks for exactly this before wiring in an assumption
about someone else's runtime.

Provenance, precisely: the reporter's issue posts an elided `<button hidden="" ...
aria-label="Settings trigger" class="... settings-trigger-button ...">`, and those
attributes are theirs verbatim. The surrounding component chain
(`flow-creative-agent-prompt-box`, `agent-footer-actions`) comes from the #749 spike
that measured this host. That is not circular — neither selector under test touches
any of it; only the `hidden` attribute and the two class names are load-bearing.

What this does NOT prove is that Flow still serves it; that needs an account in that
cohort, which is a named external blocker on #799. It proves the discriminator is
sound on the DOM we were given, and that its negative controls do not fire.

Two of those controls were also measured against LIVE Flow at $0 (council D6), since
synthetic markup is not the live arm:

* healthy migrated composer (`ffroliva`, a real project page) — trigger present and
  visible, `button.agent-mode-chip` present and **un-pressed**, verdict `False`. This
  is the load-bearing one: a normal account on this host HAS a chip, so having none
  is genuinely anomalous rather than merely unmatched.
* live `/about` landing (`denon82`, #756) — trigger absent, verdict `False`, i.e. the
  `TRIGGER_GONE` arm reached on a real page rather than a written one.

No account, no network, no credits; skips when Chromium is absent.
"""

# ruff: noqa: E501 - fixtures include inline HTML/JS

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from gflow_cli.api import image as image_api
from gflow_cli.api import video as video_api
from gflow_cli.api.transports import agent_only_composer as aoc
from gflow_cli.api.transports.migrated_composer import (
    IMAGE_MODEL_MENU_MATCHERS,
    VIDEO_MODEL_MENU_MATCHERS,
    MigratedComposer,
)

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Page

#: The composer as #799 captured it: the trigger is in the page under a bare `hidden`,
#: and there is no agent-mode chip anywhere, because this account has no classic arm.
AGENT_ONLY = """
<flow-creative-agent-prompt-box>
  <button hidden="" aria-label="Settings trigger"
          class="mat-mdc-icon-button settings-trigger-button flow-icon-button-transparent">
    <mat-icon class="google-symbols">tune</mat-icon>
  </button>
  <div class="agent-footer-actions"><button class="agent-action-button">Settings</button></div>
</flow-creative-agent-prompt-box>
"""

#: #749's recoverable arm: same hidden trigger, but a chip exists to turn off. The
#: recovery owns this, and the guard must keep its hands off it.
AGENT_MODE_RECOVERABLE = """
<flow-creative-agent-prompt-box>
  <button hidden="" aria-label="Settings trigger" class="settings-trigger-button"></button>
  <button class="agent-mode-chip" aria-pressed="true">
    <span class="agent-mode-chip-label">Agent</span>
  </button>
</flow-creative-agent-prompt-box>
"""

#: Ordinary drift: the anchor is gone from the DOM entirely. Our bug, not a cohort.
TRIGGER_GONE = """
<flow-prompt-box><button class="some-renamed-button"></button></flow-prompt-box>
"""

#: A healthy classic composer — the trigger is right there and visible.
HEALTHY = """
<flow-prompt-box>
  <button aria-label="Settings trigger" class="settings-trigger-button">tune</button>
</flow-prompt-box>
"""


async def _verdict(page: Page, markup: str) -> bool:
    from gflow_cli.api.transports.migrated_composer import READY_ANCHOR

    await page.set_content(markup)
    trigger = page.locator(READY_ANCHOR).first
    return await MigratedComposer._is_agent_only_composer(page, trigger)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_a_bare_hidden_attribute_really_does_read_as_present_but_invisible(
    page: Page,
) -> None:
    """The load-bearing third-party assumption, measured rather than assumed."""
    from gflow_cli.api.transports.migrated_composer import READY_ANCHOR

    await page.set_content(AGENT_ONLY)
    trigger = page.locator(READY_ANCHOR).first

    assert await trigger.count() == 1, "the reporter's class list must still match"
    assert await trigger.is_visible() is False, "a bare `hidden` must not read as visible"


@pytest.mark.asyncio
async def test_the_reporters_captured_composer_is_recognised(page: Page) -> None:
    assert await _verdict(page, AGENT_ONLY) is True


@pytest.mark.asyncio
async def test_the_recoverable_agent_mode_is_left_to_its_recovery(page: Page) -> None:
    """A chip in the page means there IS a classic arm — #749 owns this, not #799."""
    assert await _verdict(page, AGENT_MODE_RECOVERABLE) is False


@pytest.mark.asyncio
async def test_a_vanished_trigger_stays_selector_drift(page: Page) -> None:
    assert await _verdict(page, TRIGGER_GONE) is False


@pytest.mark.asyncio
async def test_a_healthy_composer_is_never_called_a_cohort(page: Page) -> None:
    assert await _verdict(page, HEALTHY) is False


# --- readiness end to end on a real CSS engine: `ensure_editor` navigates, so the project
# URL is served the captured markup through `page.route`. ---------------------------------


async def _ensure_editor_on(page: Page, markup: str, *, timeout_s: float) -> tuple[str, float]:
    import time

    async def _serve(route: object) -> None:
        await route.fulfill(body=markup, content_type="text/html")  # type: ignore[attr-defined]

    await page.route("https://flow.google.com/**", _serve)
    t0 = time.monotonic()
    kind = await MigratedComposer().ensure_editor(page, "p1", timeout_s=timeout_s)
    return kind, time.monotonic() - t0


@pytest.mark.asyncio
async def test_the_agent_only_composer_is_reported_without_waiting_out_the_full_gate(
    page: Page,
) -> None:
    """Every agent-only run used to pay the whole 30 s readiness wait before the
    discriminator ran. It is reported after the short early wait instead."""
    from gflow_cli.api.transports.migrated_composer import AGENT_ONLY_EARLY_S

    kind, elapsed = await _ensure_editor_on(page, AGENT_ONLY, timeout_s=30.0)
    assert kind == "agent_only"
    assert elapsed < AGENT_ONLY_EARLY_S + 5


@pytest.mark.asyncio
async def test_a_healthy_composer_is_reported_classic_through_ensure_editor(page: Page) -> None:
    kind, _ = await _ensure_editor_on(page, HEALTHY, timeout_s=30.0)
    assert kind == "classic"


@pytest.mark.asyncio
async def test_a_chip_that_mounts_late_is_not_called_agent_only(
    page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review: both facts are absences, so a slow classic mount (hidden trigger now, chip a
    second later) passed as agent-only and would have had account defaults written to it."""
    from gflow_cli.api.transports import _mc_editor_gate

    monkeypatch.setattr(_mc_editor_gate, "AGENT_ONLY_SETTLE_S", 0.6)
    late_chip = AGENT_ONLY + (
        "<script>setTimeout(() => document.body.insertAdjacentHTML('beforeend',"
        ' \'<button class="agent-mode-chip" aria-pressed="true"></button>\'), 200)</script>'
    )
    assert await _verdict(page, late_chip) is False


@pytest.mark.asyncio
async def test_agent_only_verdict_without_tune_icon(page: Page) -> None:
    """Flow removed the tune icon; agent-only composer must still be recognized."""
    without_tune = AGENT_ONLY.replace('<mat-icon class="google-symbols">tune</mat-icon>', "")
    assert await _verdict(page, without_tune) is True


@pytest.mark.asyncio
async def test_no_agent_anchor_is_not_agent_only(page: Page) -> None:
    """The verdict needs one presence: the agent composer's anchor element."""
    without_anchor = AGENT_ONLY.replace("flow-creative-agent-prompt-box", "flow-prompt-box")
    assert await _verdict(page, without_anchor) is False


# --- flow-unified-settings-view: live DOM (21 Nov 2026) regression tests -----------------

UNIFIED_SETTINGS_FIXTURE = """
<flow-creative-agent-prompt-box>
  <button class="settings-trigger" id="tune"><mat-icon>settings_2</mat-icon></button>
</flow-creative-agent-prompt-box>
<div id="panel-container"></div>
<script>
window.settingsState = {
  confirm: "1",
  videoModel: "Veo 3.1 - Lite",
  imageModel: "Nano Banana 2.1",
  closedCount: 0,
};
function renderUnifiedPane() {
  const c = document.getElementById('panel-container');
  c.innerHTML = `
    <flow-agent-panel>
      <div class="agent-panel-header">
        <button class="header-action" aria-label="Kembali"><mat-icon>arrow_back</mat-icon></button>
        <h2 class="header-title">Setelan agen</h2>
      </div>
      <div class="agent-panel-content">
        <flow-unified-settings-view>
          <p class="settings-disclaimer">Setelan untuk agen dalam project ini.</p>
          <div class="settings-section">
            <mat-button-toggle-group>
              <mat-button-toggle value="1" class="${window.settingsState.confirm === '1' ? 'mat-button-toggle-checked' : ''}">
                <button class="mat-button-toggle-button" aria-checked="${window.settingsState.confirm === '1'}">Selalu</button>
              </mat-button-toggle>
              <mat-button-toggle value="2" class="${window.settingsState.confirm === '2' ? 'mat-button-toggle-checked' : ''}">
                <button class="mat-button-toggle-button" aria-checked="${window.settingsState.confirm === '2'}">Jangan pernah</button>
              </mat-button-toggle>
            </mat-button-toggle-group>
          </div>
          <div class="settings-section">
            <button class="defaults-row" id="row-video">
              <span class="defaults-row-content">
                <mat-icon>videocam</mat-icon>
                <span class="defaults-row-text">
                  <span class="defaults-row-title">Default video</span>
                  <span class="defaults-row-caption">${window.settingsState.videoModel}, 16:9, 1 output</span>
                </span>
              </span>
              <mat-icon>arrow_forward</mat-icon>
            </button>
            <button class="defaults-row" id="row-image">
              <span class="defaults-row-content">
                <mat-icon>image</mat-icon>
                <span class="defaults-row-text">
                  <span class="defaults-row-title">Default gambar</span>
                  <span class="defaults-row-caption">🍌 ${window.settingsState.imageModel}, 16:9, 2 output</span>
                </span>
              </span>
              <mat-icon>arrow_forward</mat-icon>
            </button>
          </div>
          <div class="settings-section">Memori</div>
        </flow-unified-settings-view>
      </div>
    </flow-agent-panel>
  `;
  c.querySelectorAll('mat-button-toggle').forEach(t => {
    t.onclick = () => {
      const v = t.getAttribute('value');
      window.settingsState.confirm = v;
      renderUnifiedPane();
    };
  });
  const setupRow = (id, kind, options) => {
    const btn = c.querySelector(id);
    btn.onclick = () => {
      const menu = document.createElement('div');
      menu.className = 'cdk-overlay-pane';
      menu.innerHTML = options.map(opt => `<button role="menuitem">${opt}</button>`).join('');
      menu.querySelectorAll('[role=menuitem]').forEach(item => {
        item.onclick = () => {
          if (kind === 'video') window.settingsState.videoModel = item.textContent;
          else window.settingsState.imageModel = item.textContent;
          menu.remove();
          renderUnifiedPane();
        };
      });
      document.body.appendChild(menu);
    };
  };
  setupRow('#row-video', 'video', ['Veo 3.1 - Lite', 'Veo 3.1 - Fast', 'Veo 3.1 - Quality', 'Omni 1.2 Flash']);
  setupRow('#row-image', 'image', ['Nano Banana 2.1', 'Nano Banana Pro', 'Imagen 3.5']);
  c.querySelector('button.header-action').onclick = () => {
    window.settingsState.closedCount++;
    c.innerHTML = '';
  };
}
document.getElementById('tune').onclick = renderUnifiedPane;
</script>
"""


@pytest.mark.asyncio
async def test_unified_settings_view_video_defaults_apply_and_restore(page: Page) -> None:
    """Verifies that AgentOnlyComposer interacts seamlessly with flow-unified-settings-view:
    - Opens settings through settings_2 button
    - Reads model from defaults-row with videocam icon
    - Changes confirmation in mat-button-toggle-group
    - Selects new model from role=menuitem
    - Auto-saves and closes via back arrow button
    - Restores previous model on restore_defaults
    """
    await page.set_content(UNIFIED_SETTINGS_FIXTURE)
    composer = aoc.AgentOnlyComposer()

    snap = await composer.apply_defaults(
        page,
        section="video",
        aspect_ligature="crop_16_9",
        count=1,
        model=VIDEO_MODEL_MENU_MATCHERS[video_api.VideoModel.VEO_3_1_FAST],
        confirm="never",
    )

    assert snap.section == "video"
    assert snap.model_text == "Veo 3.1 - Lite"

    state: dict[str, Any] = await page.evaluate("window.settingsState")
    assert state["confirm"] == "2"  # "never" mapped to value 2
    assert state["videoModel"] == "Veo 3.1 - Fast"
    assert state["closedCount"] == 1
    assert await page.locator(aoc.PANE).count() == 0

    await composer.restore_defaults(page, snap)

    restored_state: dict[str, Any] = await page.evaluate("window.settingsState")
    assert restored_state["videoModel"] == "Veo 3.1 - Lite"
    assert restored_state["confirm"] == "2"  # Standing choice, not reverted
    assert restored_state["closedCount"] == 2
    assert await page.locator(aoc.PANE).count() == 0


@pytest.mark.asyncio
async def test_unified_settings_view_image_defaults_apply(page: Page) -> None:
    """Verifies image defaults row with image icon is targeted in flow-unified-settings-view."""
    await page.set_content(UNIFIED_SETTINGS_FIXTURE)
    composer = aoc.AgentOnlyComposer()

    snap = await composer.apply_defaults(
        page,
        section="image",
        aspect_ligature="crop_16_9",
        count=2,
        model=IMAGE_MODEL_MENU_MATCHERS[image_api.Model.GEM_PIX_2],  # "Nano Banana Pro"
        confirm="always",
    )

    assert snap.section == "image"
    assert "Nano Banana 2.1" in snap.model_text

    state: dict[str, Any] = await page.evaluate("window.settingsState")
    assert state["confirm"] == "1"  # "always" mapped to value 1
    assert state["imageModel"] == "Nano Banana Pro"
    assert state["closedCount"] == 1
    assert await page.locator(aoc.PANE).count() == 0


@pytest.mark.asyncio
async def test_unified_settings_view_video_defaults_apply_omni_1_2_flash(page: Page) -> None:
    """Verifies Omni 1.2 Flash can be selected and restored in flow-unified-settings-view."""
    await page.set_content(UNIFIED_SETTINGS_FIXTURE)
    composer = aoc.AgentOnlyComposer()

    snap = await composer.apply_defaults(
        page,
        section="video",
        aspect_ligature="crop_9_16",
        count=1,
        model=VIDEO_MODEL_MENU_MATCHERS[video_api.VideoModel.OMNI_1_2_FLASH],
        confirm="never",
    )

    assert snap.section == "video"
    assert snap.model_text == "Veo 3.1 - Lite"

    state: dict[str, Any] = await page.evaluate("window.settingsState")
    assert state["confirm"] == "2"
    assert state["videoModel"] == "Omni 1.2 Flash"
    assert state["closedCount"] == 1
    assert await page.locator(aoc.PANE).count() == 0

    await composer.restore_defaults(page, snap)

    restored_state: dict[str, Any] = await page.evaluate("window.settingsState")
    assert restored_state["videoModel"] == "Veo 3.1 - Lite"
    assert restored_state["closedCount"] == 2
    assert await page.locator(aoc.PANE).count() == 0


UNIFIED_SETTINGS_SUBPAGE_FIXTURE = """
<flow-creative-agent-prompt-box>
  <button id="tune" aria-label="Settings trigger">
    <mat-icon class="google-symbols">settings_2</mat-icon>
  </button>
</flow-creative-agent-prompt-box>
<div id="panel-container"></div>
<script>
window.settingsState = {
  confirm: '1',
  videoModel: 'Veo 3.1 - Lite',
  imageModel: 'Nano Banana 2.1',
  closedCount: 0,
  page: 'main',
};
function renderSubpagePane() {
  const c = document.getElementById('panel-container');
  if (window.settingsState.page === 'main') {
    c.innerHTML = `
      <flow-agent-panel>
        <div class="panel-header">
          <button class="header-action"><mat-icon>arrow_back</mat-icon></button>
          <span>Setelan agen</span>
        </div>
        <div class="panel-content">
          <flow-unified-settings-view>
            <div class="settings-section">
              <mat-button-toggle-group>
                <mat-button-toggle value="1">
                  <button class="mat-button-toggle-button" aria-checked="${window.settingsState.confirm === '1'}">Selalu</button>
                </mat-button-toggle>
                <mat-button-toggle value="2">
                  <button class="mat-button-toggle-button" aria-checked="${window.settingsState.confirm === '2'}">Jangan pernah</button>
                </mat-button-toggle>
              </mat-button-toggle-group>
            </div>
            <div class="settings-section">
              <button class="defaults-row" id="row-video">
                <span class="defaults-row-content">
                  <mat-icon>videocam</mat-icon>
                  <span class="defaults-row-text">
                    <span class="defaults-row-title">Default video</span>
                    <span class="defaults-row-caption">${window.settingsState.videoModel}, 16:9, 1 output</span>
                  </span>
                </span>
                <mat-icon>arrow_forward</mat-icon>
              </button>
              <button class="defaults-row" id="row-image">
                <span class="defaults-row-content">
                  <mat-icon>image</mat-icon>
                  <span class="defaults-row-text">
                    <span class="defaults-row-title">Default gambar</span>
                    <span class="defaults-row-caption">🍌 ${window.settingsState.imageModel}, 16:9, 2 output</span>
                  </span>
                </span>
                <mat-icon>arrow_forward</mat-icon>
              </button>
            </div>
            <div class="settings-section">Memori</div>
          </flow-unified-settings-view>
        </div>
      </flow-agent-panel>
    `;
    c.querySelectorAll('mat-button-toggle').forEach(t => {
      t.onclick = () => {
        window.settingsState.confirm = t.getAttribute('value');
        renderSubpagePane();
      };
    });
    c.querySelector('#row-video').onclick = () => {
      window.settingsState.page = 'video-defaults';
      renderSubpagePane();
    };
    c.querySelector('button.header-action').onclick = () => {
      window.settingsState.closedCount++;
      c.innerHTML = '';
    };
  } else if (window.settingsState.page === 'video-defaults') {
    c.innerHTML = `
      <flow-agent-panel>
        <div class="panel-header">
          <button class="header-action"><mat-icon>arrow_back</mat-icon></button>
          <span>Default video</span>
        </div>
        <div class="panel-content">
          <flow-unified-settings-view>
            <div class="model-selection-list">
              <mat-radio-group>
                <mat-radio-button value="Veo 3.1 - Lite" aria-checked="${window.settingsState.videoModel === 'Veo 3.1 - Lite'}">
                  <span class="label">Veo 3.1 - Lite</span>
                </mat-radio-button>
                <mat-radio-button value="Veo 3.1 - Fast" aria-checked="${window.settingsState.videoModel === 'Veo 3.1 - Fast'}">
                  <span class="label">Veo 3.1 - Fast</span>
                </mat-radio-button>
                <mat-radio-button value="Veo 3.1 - Quality" aria-checked="${window.settingsState.videoModel === 'Veo 3.1 - Quality'}">
                  <span class="label">Veo 3.1 - Quality</span>
                </mat-radio-button>
                <mat-radio-button value="Omni 1.2 Flash" aria-checked="${window.settingsState.videoModel === 'Omni 1.2 Flash'}">
                  <span class="label">Omni 1.2 Flash</span>
                </mat-radio-button>
              </mat-radio-group>
            </div>
          </flow-unified-settings-view>
        </div>
      </flow-agent-panel>
    `;
    c.querySelectorAll('mat-radio-button').forEach(rb => {
      rb.onclick = () => {
        window.settingsState.videoModel = rb.getAttribute('value');
        renderSubpagePane();
      };
    });
    c.querySelector('button.header-action').onclick = () => {
      window.settingsState.page = 'main';
      renderSubpagePane();
    };
  }
}
document.getElementById('tune').onclick = () => {
  window.settingsState.page = 'main';
  renderSubpagePane();
};
</script>
"""


@pytest.mark.asyncio
async def test_unified_settings_subpage_video_defaults_apply_and_restore_omni(page: Page) -> None:
    """Verifies that selecting a model via sub-page navigation inside flow-unified-settings-view works.

    In flow-unified-settings-view, clicking the video defaults row navigates into a sub-page
    containing mat-radio-button options (or similar drill-down list). Selecting Omni 1.2 Flash
    and returning to main view updates the caption and snapshot cleanly.
    """
    await page.set_content(UNIFIED_SETTINGS_SUBPAGE_FIXTURE)
    composer = aoc.AgentOnlyComposer()

    snap = await composer.apply_defaults(
        page,
        section="video",
        aspect_ligature="crop_9_16",
        count=1,
        model=VIDEO_MODEL_MENU_MATCHERS[video_api.VideoModel.OMNI_1_2_FLASH],
        confirm="never",
    )

    assert snap.section == "video"
    assert snap.model_text == "Veo 3.1 - Lite"

    state: dict[str, Any] = await page.evaluate("window.settingsState")
    assert state["confirm"] == "2"
    assert state["videoModel"] == "Omni 1.2 Flash"
    assert state["closedCount"] == 1
    assert await page.locator(aoc.PANE).count() == 0

    await composer.restore_defaults(page, snap)

    restored_state: dict[str, Any] = await page.evaluate("window.settingsState")
    assert restored_state["videoModel"] == "Veo 3.1 - Lite"
    assert restored_state["closedCount"] == 2
    assert await page.locator(aoc.PANE).count() == 0


@pytest.mark.asyncio
async def test_unified_settings_subpage_discard_pane_closes_from_subpage(page: Page) -> None:
    """Verifies that _discard_pane cleanly closes the panel even when located inside a sub-page."""
    await page.set_content(UNIFIED_SETTINGS_SUBPAGE_FIXTURE)
    composer = aoc.AgentOnlyComposer()

    # Open panel and navigate into subpage
    await page.click("#tune")
    await page.click("#row-video")
    state: dict[str, Any] = await page.evaluate("window.settingsState")
    assert state["page"] == "video-defaults"

    # Discard pane should pop the sub-page and close the panel
    await composer._discard_pane(page)  # pyright: ignore[reportPrivateUsage]

    assert await page.locator(aoc.PANE).count() == 0
    closed_state: dict[str, Any] = await page.evaluate("window.settingsState")
    assert closed_state["closedCount"] == 1
