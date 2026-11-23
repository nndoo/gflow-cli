"""Driver for #799's agent-only composer on flow.google.com: text-to-image and text-to-video.

Some accounts are served a composer with no classic arm — the only prompt box is a chat
agent. Evidence for every anchor below:
``docs/superpowers/spikes/2026-09-15-agent-only-composer-drive-surface.md``.

Shape of a run:

1. **Agent settings** carry aspect / count / model as account defaults. They are set to the
   request, saved, and restored afterwards (measured: Save persists across a reload, and a
   set -> Save -> restore -> Save round trip returns the account to its originals). The
   "Confirm before generating" radio follows ``GFLOW_CLI_AGENT_CONFIRM`` and is NOT
   restored — it is the user's standing choice.
2. A natural-language **directive** goes into the ProseMirror editor and is submitted.
3. At most **one credit gate** that appeared after our own submit is approved, and only
   after its title has been read: a gate quoting more clips than requested is rejected
   (#988). Gates from earlier sessions survive reloads, so a gate present before submit is
   never ours; a second gate means the agent asked for more than one generation, and the
   run stops without approving it.
4. Completion is observed in the **DOM**, not on the wire: this composer never fires the
   classic ``ogiZ0b`` image reply, and ``as29s`` records name uuids absent from the page.
   An image is a grid tile whose media id (``img[data-media-id]``; the src is an opaque
   ``flow.google.com/asb/...`` 512-px preview, fetched at original size via ``=s0``) was not
   in the pre-submit baseline. A video is ready when no ``flow-pending-tile`` is left, the newest
   finished tile has changed, and a new uuid is named by that tile or by the chat reply's
   poster — the tile's own media may be opaque ``/asb/`` (no uuid), and its ``<video>``
   then mounts only on hover. When every URL on the page is ``/asb/`` (#1354), the uuid
   comes from the project's ``Zzl0ze`` listing: the record whose poster shares the new
   tile's ``/asb/`` token, finished, not on the page before this run's submit, and created
   after it.

Every selector is structural (component tags, classes, ``mat-icon`` ligatures, roles).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import structlog
from playwright.async_api import Error as PlaywrightError

from gflow_cli.api.dto import GeneratedImage
from gflow_cli.api.image import Aspect as ImageAspect
from gflow_cli.api.transports._mc_common import (
    _OPTION_TITLES_JS,  # pyright: ignore[reportPrivateUsage]
    ARROW_SETTLE_MS,
    ARROW_STEP_MS,
    FRAME_SEARCH_ATTEMPTS,
    FRAME_SEARCH_RETRY_PAUSE_S,
    LISTING_READ_BUDGET_S,
    LISTING_RPC,
    PICKER_OPTION,
    PROMPT_MATCH_MARGIN_S,
)
from gflow_cli.api.transports._mc_submit_observe import read_project_listing
from gflow_cli.api.transports.batchexecute import asb_token, generation_record_by_poster
from gflow_cli.api.transports.migrated_composer import (
    ASPECT_LIGATURE,
    IMAGE_ASPECT_LIGATURE,
    IMAGE_MODEL_MENU_MATCHERS,
    VIDEO_MODEL_MENU_MATCHERS,
    ModelMenuMatcher,
    _raise_if_out_of_credits,  # pyright: ignore[reportPrivateUsage]
)
from gflow_cli.api.transports.migrated_existing_refs import require_appended
from gflow_cli.api.video import Aspect, Mode, VideoResult, VideoStarted, VideoStatus
from gflow_cli.errors import (
    ConfigurationError,
    FlowAgentUiError,
    FlowHostMigratedError,
    ReferenceNotFoundError,
    TransportTimeoutError,
    UiSelectorDriftError,
    WireFormatError,
    nothing_submitted,
)
from gflow_cli.redaction import redact_sensitive_text

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Awaitable, Callable

    from playwright.async_api import Locator, Page

    from gflow_cli.api.image import GenerateImageRequest
    from gflow_cli.api.transports.migrated_composer import ClipSaver
    from gflow_cli.api.video import GenerateVideoRequest, VideoStartedCallback

    #: Reads the project's `Zzl0ze` listing within a budget in seconds (#1354).
    ListingReader = Callable[[float], Awaitable[Any]]

log = structlog.get_logger(__name__)

Section = Literal["image", "video"]
Confirm = Literal["account", "always", "never"]

PANE = ":is(flow-unified-settings-view, flow-settings-view)"
SETTINGS_BUTTON = (
    "flow-agent-panel button:has(mat-icon:text-is('settings_2')), "
    "flow-creative-agent-prompt-box button:has(mat-icon:text-is('settings_2')), "
    "button:has(mat-icon:text-is('settings_2')), "
    "flow-creative-agent-prompt-box button:has(mat-icon:text-is('tune')), "
    "button#tune"
)
TOGGLE_GROUP = f"{PANE} mat-button-toggle-group"
TOGGLE_RADIO = "button[role='radio'], mat-button-toggle button, button.mat-button-toggle-button"
CONFIRM_INPUT = f"{PANE} mat-radio-button input[type='radio']"
SAVE_BUTTON = f"{PANE} button.settings-save-button"
#: Closes the pane without saving (measured 2026-09-15: `button.header-action` + `arrow_back`).
PANE_BACK = (
    "flow-agent-panel button:has(mat-icon:text-is('arrow_back')), "
    "flow-agent-panel button.header-action"
)
MODEL_PICKER = {
    "image": (
        f"{PANE} button.defaults-row:has(mat-icon:text-is('image')), "
        f"{PANE} .defaults-row:has(mat-icon:text-is('image')), "
        f"{PANE} button:has(mat-icon:text-is('image')), "
        f"{PANE} button.image-model-picker-button"
    ),
    "video": (
        f"{PANE} button.defaults-row:has(mat-icon:text-is('videocam')), "
        f"{PANE} .defaults-row:has(mat-icon:text-is('videocam')), "
        f"{PANE} button:has(mat-icon:text-is('videocam')), "
        f"{PANE} button.video-model-picker-button"
    ),
}
MENU_ITEM = (
    "[role='menuitem'], "
    "[role='option'], "
    ".cdk-overlay-pane button, "
    "flow-agent-panel label.radio-option, "
    "flow-agent-panel .radio-option, "
    "flow-agent-panel mat-radio-button, "
    "flow-agent-panel [role='radio'], "
    "flow-agent-panel mat-button-toggle, "
    "flow-agent-panel mat-list-option, "
    "flow-agent-panel mat-list-item, "
    "flow-agent-panel .mat-mdc-list-item, "
    "flow-agent-panel button.defaults-row, "
    "flow-agent-panel button.model-option, "
    "flow-agent-panel button.option-row, "
    "mat-list-item, "
    "button.model-option, "
    "label.radio-option"
)
EDITOR = "div.ProseMirror"
SUBMIT = "flow-generate-icon-button button"
IN_FLIGHT = "flow-stop-icon-button"
#: A gate still waiting for an answer. Answered or abandoned ones keep `.read-only`.
LIVE_GATE = "flow-permission-message:has(div[role='radio'].option-row:not(.read-only))"
#: Approve is the first `check` row; the second `check` is "Always approve".
APPROVE_ROW = "div[role='radio'].option-row:not(.read-only):has(mat-icon:text-is('check'))"
#: Reject is the only `close` row (#988, measured on en and ru, 2026-10-23).
REJECT_ROW = "div[role='radio'].option-row:not(.read-only):has(mat-icon:text-is('close'))"
#: The gate's question: "...kick off this 1 video generation, costing 10 credits?" (en),
#: "Сгенерировать 1 видео за бонусы (10)?" (ru). Its words are translated, its number tokens
#: are not: count first, credits last on both (#988). Like the duration radio's number
#: (#963) that meets the locale-invariance exception bar, and a video gate whose numbers
#: cannot be read is never approved.
GATE_TITLE = "p.title"
LAST_REPLY = "flow-chat-bubble"

#: Toggle groups in DOM order (measured): image aspect, image count, video aspect, video count.
_GROUP_BASE: dict[Section, int] = {"image": 0, "video": 2}
#: "Confirm before generating" radios by their input's own ``value``, not their position.
#: Measured 2026-09-16 (``scripts/dev/spike_agent_confirm_radios.py``): Always is
#: ``value="1"``, Never is ``value="2"``, read against their English labels.
_CONFIRM_VALUE = {"always": "1", "never": "2"}
#: The ``route`` every clip-download failure carries.
_VIDEO_ROUTE = "agent-only:video"

POLL_S = 1.0
#: Least seconds between two project-listing reads for one clip (#1354): each opens a tab.
LISTING_RETRY_S = 10.0
#: An image turn that ends with no gate and no tile for this long made nothing.
IDLE_S = 20.0
#: How long an approved gate gets to turn `.read-only` before a re-count.
GATE_SETTLE_S = 10.0
#: Measured: image at ~45 s after submit. Headroom for a busy queue.
IMAGE_BUDGET_S = 240.0
PANE_S = 8.0
#: How long a clicked radio gets to report `aria-checked="true"`.
RADIO_SETTLE_S = 3.0

#: For raises AFTER a submit: credits may already be spent, so FlowAgentUiError's default
#: advice ("try a different Chrome profile") would send an agent to pay a second time.
_AFTER_SUBMIT_HINT = (
    "Check the Flow project and the agent's last reply before submitting again - the request "
    "was already sent, and a re-run submits (and may bill) another one."
)
#: Said only once a rejected gate was seen to turn read-only.
_REJECTED_HINT = (
    "The credit confirmation was rejected before approval, so nothing was approved. Check "
    "the Flow balance before re-running, and word the prompt so the agent asks for exactly "
    "what was requested."
)
_NUMBER_RE = re.compile(r"\d+")

_CDN_MEDIA_RE = re.compile(
    r"^https://flow-content\.google/(image|video)/([0-9a-fA-F-]{36})(?:[?#]|$)"
)

_TILES_JS = r"""
() => [...document.querySelectorAll('flow-image-tile, flow-video-tile')].flatMap(t => {
  const img = t.querySelector('img');
  const vid = t.querySelector('video');
  return [{
    tile: t.tagName.toLowerCase(),
    poster: img ? (img.currentSrc || img.src || '') : '',
    video: vid ? (vid.currentSrc || vid.src || '') : '',
    id: img ? (img.dataset.mediaId || '') : '',
  }];
})
"""

#: Video state, measured 2026-09-15: a tile holds `flow-pending-tile` (queued, then a
#: percentage) until the finished tile REPLACES it. The finished tile's media is
#: `flow-content.google/video/<uuid>` on the session that made it and opaque `/asb/` once
#: the grid re-renders; the chat option's poster then named the uuid. Reported 2026-11-19
#: (#1354): the tile, its hover player and the chat option can all be `/asb/` from the start,
#: so no uuid is on the page at all and the project listing has to name the clip.
_VIDEO_JS = r"""
() => ({
  pending: document.querySelectorAll('flow-video-tile flow-pending-tile').length,
  finished: [...document.querySelectorAll('flow-video-tile')]
    .filter(t => !t.querySelector('flow-pending-tile'))
    .map(t => {
      const src = (e) => e ? (e.currentSrc || e.src || '') : '';
      return { video: src(t.querySelector('video')), img: src(t.querySelector('img')) };
    }),
  options: [...document.querySelectorAll('flow-a2ui-video-option img')]
    .map(i => i.currentSrc || i.src || ''),
})
"""
FINISHED_VIDEO_TILE = "flow-video-tile:not(:has(flow-pending-tile))"
#: `/asb/` clips redirect to Google's video CDN (measured: `*.googlevideo.com`).
_VIDEO_REDIRECT_HOST = "googlevideo.com"

_IMAGE_ASPECT_LABEL = {
    ImageAspect.PORTRAIT: "9:16",
    ImageAspect.LANDSCAPE: "16:9",
    ImageAspect.SQUARE: "1:1",
    ImageAspect.LANDSCAPE_FOUR_THREE: "4:3",
    ImageAspect.PORTRAIT_THREE_FOUR: "3:4",
}
_VIDEO_ASPECT_LABEL = {Aspect.PORTRAIT: "9:16", Aspect.LANDSCAPE: "16:9"}

#: Collapse selection to the END of the agent prompt editor (#723, #997).
_AGENT_CARET_TO_END_JS = (
    "(sel) => { const all = document.querySelectorAll(sel);"
    " const ce = all.length ? all[all.length - 1] : null; if (!ce) return;"
    " ce.focus(); const r = document.createRange(); r.selectNodeContents(ce);"
    " r.collapse(false); const s = window.getSelection();"
    " s.removeAllRanges(); s.addRange(r); }"
)


@dataclass(frozen=True)
class TileMedia:
    """One finished result on the project grid."""

    uuid: str
    src: str


@dataclass(frozen=True)
class DefaultsSnapshot:
    """What the pane held before a run changed it."""

    section: Section
    aspect_index: int
    count_index: int
    model_text: str


def compose_image_directive(prompt: str, count: int, aspect_label: str) -> str:
    subject = "a picture" if count == 1 else f"{count} pictures"
    return f"Make me {subject} of {prompt} in a {aspect_label} aspect ratio."


def compose_video_directive(prompt: str, duration: int | None, aspect_label: str) -> str:
    length = f"{duration} second " if duration else ""
    return f"Make me a {length}video of {prompt} in a {aspect_label} aspect ratio."


#: Measured 2026-10-04: image tiles now load from an opaque `flow.google.com/asb/...` URL and
#: carry the media id on the `<img data-media-id>` attribute instead of in the src.
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def _tile_uuid(tile: dict[str, Any], kind: str) -> str | None:
    uuid = _cdn_uuid(tile.get("poster", ""), kind)
    if uuid:
        return uuid
    media_id = tile.get("id") or ""
    return media_id.lower() if _UUID_RE.match(media_id) else None


#: A `/asb/` tile src ends in a size spec (measured: `=s512-rw`, a 512-px WebP preview). The
#: spec survives the redirect to `lh3.google.com/rd-asb/`, Google's image server, where `=s0`
#: means the original size. The classic composer never needs this: its ogiZ0b reply carries
#: a signed full-size URL, which the agent-only page never exposes.
_SIZE_SPEC_RE = re.compile(r"[A-Za-z0-9-]+")


def _full_size_src(src: str) -> str:
    """The original-size URL for an `/asb/` image src; any other src unchanged.

    String splits rather than one regex: `https://…/asb/<id>=<spec>` as a pattern backtracks
    super-linearly on a long src (Sonar S8786), and every step here is a single scan.
    """
    head, eq, spec = (src or "").rpartition("=")
    # The `/asb/` must follow the scheme and the head's last `=`, with an id after it.
    path = head[max(len("https://"), head.rfind("=") + 1) :]
    if (
        eq
        and head.startswith("https://")
        and "/asb/" in path[:-1]
        and "?" not in src
        and "#" not in src
        and _SIZE_SPEC_RE.fullmatch(spec)
    ):
        return f"{head}=s0"
    return src


def _cdn_uuid(src: str, kind: str) -> str | None:
    m = _CDN_MEDIA_RE.match(src or "")
    return m.group(2).lower() if m and m.group(1) == kind else None


def _picker_label(text: str) -> str:
    return (
        text.replace("arrow_drop_down", "")
        .replace("arrow_forward", "")
        .replace("radio_button_checked", "")
        .replace("radio_button_unchecked", "")
        .strip()
    )


def _clean_option_text(raw: str) -> str:
    cleaned = (
        raw.replace("radio_button_checked", "")
        .replace("radio_button_unchecked", "")
        .replace("arrow_drop_down", "")
        .replace("arrow_forward", "")
    )
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return ""
    # In live Flow sub-page, line 1 is the model name (later lines are deprecation notes).
    return lines[0]


async def _extract_candidate_text(cand: Locator) -> str:
    text_el = cand.locator(".radio-option-text, .radio-option-label")
    count = await text_el.count()
    if count > 0:
        for j in range(count):
            txt = (await text_el.nth(j).inner_text()).strip()
            clean = _clean_option_text(txt)
            if clean:
                return clean
    raw = (await cand.inner_text()).strip()
    return _clean_option_text(raw)


def _is_likely_model_option(text: str) -> bool:
    t = text.casefold()
    return any(
        kw in t
        for kw in (
            "veo",
            "omni",
            "nano",
            "banana",
            "imagen",
            "lite",
            "fast",
            "quality",
            "flash",
            "pro",
        )
    )


def _model_hit(text: str, wants: ModelMenuMatcher | str) -> bool:
    if isinstance(wants, str):
        cleaned = _picker_label(text)
        wants_clean = _picker_label(wants)
        if cleaned == wants_clean or wants_clean.casefold() in cleaned.casefold():
            return True
        if len(cleaned) >= 4 and cleaned.casefold() in wants_clean.casefold():
            return True
        return False
    return wants.matches(text)


async def _find_overlay_model_option(
    page: Page, wants: ModelMenuMatcher | str
) -> tuple[Locator | None, str, list[str]]:
    overlay_items = page.locator(
        ".cdk-overlay-pane [role='menuitem'], "
        ".cdk-overlay-pane [role='option'], "
        ".cdk-overlay-pane button, "
        "[role='menuitem'], "
        "[role='option']"
    )
    if not (await overlay_items.count() > 0 and await overlay_items.first.is_visible()):
        return None, "", []
    texts = [t.strip() for t in await overlay_items.all_text_contents() if t.strip()]
    hits = [i for i, t in enumerate(texts) if _model_hit(t, wants)]
    if len(hits) == 1:
        return overlay_items.nth(hits[0]), texts[hits[0]], texts
    return None, "", texts


async def _find_subpage_model_option(
    page: Page, wants: ModelMenuMatcher | str
) -> tuple[Locator | None, str, list[str]]:
    # In live flow-unified-settings-view, model options are rendered as label.radio-option
    sub_candidates = page.locator(
        "flow-agent-panel label.radio-option, flow-agent-panel .radio-option"
    )
    if await sub_candidates.count() == 0:
        sub_candidates = page.locator(
            "flow-agent-panel mat-radio-button, "
            "flow-agent-panel [role='radio'], "
            "flow-agent-panel mat-button-toggle, "
            "flow-agent-panel mat-list-option, "
            "flow-agent-panel mat-list-item, "
            "flow-agent-panel [mat-list-item], "
            "flow-agent-panel .mat-mdc-list-item, "
            "flow-agent-panel button.defaults-row, "
            "flow-agent-panel button.model-option, "
            "flow-agent-panel button.option-row, "
            "flow-agent-panel button:not(.header-action)"
        )
    count = await sub_candidates.count()
    if count == 0:
        return None, "", []
    sub_texts: list[str] = []
    sub_hits: list[int] = []
    for i in range(count):
        cand = sub_candidates.nth(i)
        if await cand.is_visible():
            txt = await _extract_candidate_text(cand)
            raw_txt = (await cand.inner_text()).strip()
            if txt:
                sub_texts.append(txt)
                if _model_hit(txt, wants) or (raw_txt and _model_hit(raw_txt, wants)):
                    sub_hits.append(i)
    if len(sub_hits) == 1:
        offered = [t for t in sub_texts if _is_likely_model_option(t)] or sub_texts
        return sub_candidates.nth(sub_hits[0]), sub_texts[sub_hits[0]], offered
    filtered = [t for t in sub_texts if _is_likely_model_option(t)]
    return None, "", (filtered if filtered else sub_texts)


async def _wait_option_selected(target_item: Locator, picker: Locator) -> None:
    if await picker.is_visible():
        return
    sel_deadline = time.monotonic() + RADIO_SETTLE_S
    while time.monotonic() < sel_deadline:
        if await picker.is_visible():
            break
        try:
            cls = (await target_item.get_attribute("class")) or ""
            txt = await target_item.inner_text()
            aria = (await target_item.get_attribute("aria-checked")) or ""
            if "radio-option-selected" in cls or "radio_button_checked" in txt or aria == "true":
                break
        except Exception:
            break
        await asyncio.sleep(0.05)


async def _discover_model_option(
    page: Page, picker: Locator, wants: ModelMenuMatcher | str, timeout_s: float = 5.0
) -> tuple[Locator | None, str, list[str]]:
    deadline = time.monotonic() + timeout_s
    last_offered: list[str] = []
    while time.monotonic() < deadline:
        target, text, offered = await _find_overlay_model_option(page, wants)
        if target is not None:
            return target, text, offered
        if offered:
            last_offered = offered

        if not await picker.is_visible():
            sub_target, sub_text, sub_offered = await _find_subpage_model_option(page, wants)
            if sub_target is not None:
                return sub_target, sub_text, sub_offered
            if sub_offered:
                last_offered = sub_offered

        await asyncio.sleep(0.1)
    return None, "", last_offered


async def _ensure_picker_visible(page: Page, picker: Locator) -> None:
    if await picker.is_visible():
        return
    await page.wait_for_timeout(200)
    if await picker.is_visible():
        return
    sub_back = page.locator(PANE_BACK).last
    if await sub_back.count() and await sub_back.is_visible():
        await sub_back.click(timeout=4000)
        try:
            await picker.wait_for(state="visible", timeout=int(PANE_S * 1000))
        except PlaywrightError:
            pass


async def _is_toggle_checked(target: Locator) -> bool:
    classes = (await target.get_attribute("class")) or ""
    if "mat-button-toggle-checked" in classes:
        return True
    if await target.get_attribute("aria-checked") == "true":
        return True
    btn = target.locator("button")
    if await btn.count() and await btn.first.get_attribute("aria-checked") == "true":
        return True
    return False


async def _set_legacy_confirm(page: Page, confirm: Confirm, val: str) -> bool:
    radio = page.locator(f"{CONFIRM_INPUT}[value='{val}']")
    if await radio.count() != 1:
        return False
    if not await radio.is_checked():
        try:
            await radio.check(timeout=4000)
        except PlaywrightError as exc:
            raise UiSelectorDriftError(
                detail=f"agent-only composer: could not check the {confirm!r} confirm radio"
            ) from exc
    return True


def _asb_tokens(state: dict[str, Any]) -> set[str]:
    """Every `/asb/` token the grid or the chat shows (#1354)."""
    srcs = [s for t in state["finished"] for s in (t["video"], t["img"])] + state["options"]
    return {token for src in srcs if (token := asb_token(src))}


def _video_uuids(state: dict[str, Any]) -> list[str]:
    """Every clip uuid the grid or the chat names, tiles first, in page order."""
    srcs = [t["video"] for t in state["finished"]] + [t["img"] for t in state["finished"]]
    seen: dict[str, None] = {}
    for src in [*srcs, *state["options"]]:
        m = _CDN_MEDIA_RE.match(src or "")
        if m:
            seen.setdefault(m.group(2).lower(), None)
    return list(seen)


def gate_quotes_more(title: str, count: int) -> bool | None:
    """Whether a credit gate's title quotes more than ``count`` generations (#988).

    None while the title is not readable: the count and the credits are both needed, so a
    title rendered halfway ("...kick off this 1") never parses. Every number but the last
    (the credits) is checked, so a digit the prompt slipped in can only refuse, not approve
    - as long as Flow writes the count as a digit, as it did on every measured title.
    """
    numbers = [int(n) for n in _NUMBER_RE.findall(title)]
    if len(numbers) < 2:
        return None
    return any(n > count for n in numbers[:-1])


def _tile_key(tile: dict[str, str]) -> str:
    return tile["video"] or tile["img"]


async def _click(target: Locator, what: str, timeout_ms: int = 4000) -> None:
    """A pre-submit click: a Playwright failure here is drift (exit 23), nothing was sent."""
    try:
        await target.click(timeout=timeout_ms)
    except PlaywrightError as exc:
        raise UiSelectorDriftError(detail=f"agent-only composer: could not click {what}") from exc


class AgentOnlyComposer:
    """Agent settings in and out, directive in, finished tiles out."""

    # --- Agent settings ------------------------------------------------------------------

    async def _open_pane(self, page: Page) -> None:
        pane = page.locator(PANE)
        if await pane.count() and await pane.first.is_visible():
            return
        button = page.locator(SETTINGS_BUTTON).last
        if not await button.count():
            raise UiSelectorDriftError(
                detail=f"agent-only composer: Agent settings button ({SETTINGS_BUTTON}) missing"
            )
        await _click(button, "the Agent settings button", timeout_ms=5000)
        try:
            await pane.first.wait_for(state="visible", timeout=int(PANE_S * 1000))
        except PlaywrightError as exc:
            raise UiSelectorDriftError(
                detail=f"agent-only composer: Agent settings ({PANE}) did not open"
            ) from exc

    async def _checked_index(self, page: Page, group: int) -> int:
        groups = page.locator(TOGGLE_GROUP)
        if await groups.count() <= group:
            return -1
        js = (
            "g => [...g.querySelectorAll(\"button[role='radio'], mat-button-toggle, button\")]"
            ".findIndex(b => b.getAttribute('aria-checked') === 'true' "
            "|| b.classList.contains('mat-button-toggle-checked'))"
        )
        index = await groups.nth(group).evaluate(js)
        return int(index)

    async def _check_radio(self, page: Page, group: int, radio: str | int, named: str) -> None:
        """Check one radio, by ligature (aspect) or by position (count, restore)."""
        groups = page.locator(TOGGLE_GROUP)
        if await groups.count() <= group:
            log.debug("agent_only.toggle_group_not_present", group=group, named=named)
            return
        radios = groups.nth(group).locator(TOGGLE_RADIO)
        if isinstance(radio, str):
            target = radios.filter(has=page.locator(f"mat-icon:text-is('{radio}')"))
        else:
            target = radios.nth(radio)
        if not await target.count():
            raise UiSelectorDriftError(
                detail=f"agent-only composer: no {named} radio {radio!r} in Agent settings"
            )
        first = target.first
        if await first.get_attribute("aria-checked") == "true":
            return
        await _click(first, f"the {named} radio {radio!r}")
        # Angular re-renders `aria-checked` after the click, not during it — a read in the
        # same tick saw the old value on a live restore (2026-09-15) and left the account
        # on the run's defaults.
        deadline = time.monotonic() + RADIO_SETTLE_S
        while await first.get_attribute("aria-checked") != "true":
            if time.monotonic() >= deadline:
                raise UiSelectorDriftError(
                    detail=f"agent-only composer: {named} radio {radio!r} did not become checked"
                )
            await asyncio.sleep(0.1)

    async def _choose_model(
        self, page: Page, section: Section, wants: ModelMenuMatcher | str
    ) -> None:
        picker = page.locator(MODEL_PICKER[section]).first
        if not await picker.count():
            raise UiSelectorDriftError(
                detail=f"agent-only composer: {section} model picker missing from Agent settings"
            )

        current_label = await self._read_picker_model(page, section)
        current_text = await picker.inner_text()
        if _model_hit(current_label, wants) or _model_hit(current_text, wants):
            return

        await picker.scroll_into_view_if_needed()
        await _click(picker, f"the {section} model picker")

        target_item, target_text, offered = await _discover_model_option(page, picker, wants)
        if target_item is None:
            await page.keyboard.press("Escape")
            if offered:
                hits = [t for t in offered if _model_hit(t, wants)]
                raise ConfigurationError(
                    detail=(
                        f"{section} model matched {len(hits)} Agent-settings entries; "
                        f"offered: {', '.join(offered)}"
                    ),
                    remediation_hint="Pass a --model that names one offered entry, or omit it.",
                )
            raise UiSelectorDriftError(
                detail=f"agent-only composer: the {section} model menu ({MENU_ITEM}) did not open"
            )

        await _click(target_item, f"the {section} model {target_text!r}")
        await _wait_option_selected(target_item, picker)
        await _ensure_picker_visible(page, picker)

        settle_deadline = time.monotonic() + RADIO_SETTLE_S
        while True:
            current_label = await self._read_picker_model(page, section)
            current_text = await picker.inner_text()
            if _model_hit(current_label, wants) or _model_hit(current_text, wants):
                break
            if time.monotonic() >= settle_deadline:
                raise UiSelectorDriftError(
                    detail=(
                        f"agent-only composer: chose {target_text!r} but the {section} "
                        "model picker did not change"
                    )
                )
            await asyncio.sleep(0.1)
        log.info("migrated.agent_only.model_selected", section=section, model=target_text)

    async def _discard_pane(self, page: Page) -> None:
        """Leave Agent settings WITHOUT saving, through the panel's own back arrow."""
        pane = page.locator(PANE).first
        for _ in range(2):
            if not await pane.count() or not await pane.is_visible():
                break
            back = page.locator(PANE_BACK).last
            try:
                if await back.count() and await back.is_visible():
                    await back.click(timeout=3000)
                    await page.wait_for_timeout(200)
            except Exception:
                break
        try:
            if await pane.count() and await pane.is_visible():
                await pane.wait_for(state="hidden", timeout=3000)
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask the real failure
            log.warning("migrated.agent_only.pane_discard_failed", error=str(exc)[:200])

    async def _save(self, page: Page) -> None:
        save = page.locator(SAVE_BUTTON).first
        if await save.count() and await save.is_visible():
            try:
                await save.click(timeout=4000)
                await page.locator(PANE).first.wait_for(state="hidden", timeout=int(PANE_S * 1000))
                return
            except Exception as exc:
                raise UiSelectorDriftError(
                    detail=(
                        f"agent-only composer: Agent settings did not close on Save ({SAVE_BUTTON})"
                    )
                ) from exc

        # In flow-unified-settings-view, settings are auto-saved; close pane through back arrow
        back = page.locator(PANE_BACK).last
        if await back.count() and await back.is_visible():
            try:
                await back.click(timeout=4000)
                await page.locator(PANE).first.wait_for(state="hidden", timeout=int(PANE_S * 1000))
            except Exception as exc:
                raise UiSelectorDriftError(
                    detail=(
                        f"agent-only composer: Agent settings did not close on Back ({PANE_BACK})"
                    )
                ) from exc

    async def _set_confirm(self, page: Page, confirm: Confirm) -> None:
        if confirm == "account":
            return
        val = _CONFIRM_VALUE[confirm]
        if await _set_legacy_confirm(page, confirm, val):
            return

        # Modern mat-button-toggle-group (flow-unified-settings-view)
        toggle_group = page.locator(f"{PANE} mat-button-toggle-group").first
        if not await toggle_group.count():
            raise UiSelectorDriftError(
                detail=(f"agent-only composer: no confirm toggle group in Agent settings ({PANE})")
            )

        val_toggle = toggle_group.locator(
            f"mat-button-toggle[value='{val}'], "
            f"button[role='radio'][value='{val}'], [value='{val}']"
        )
        toggles = toggle_group.locator("mat-button-toggle, button[role='radio']")
        target_idx = 0 if confirm == "always" else 1
        if await val_toggle.count() == 1:
            target = val_toggle.first
        elif await toggles.count() >= 2:
            target = toggles.nth(target_idx)
        else:
            raise UiSelectorDriftError(
                detail=(
                    f"agent-only composer: no single {confirm!r} confirm toggle in Agent settings"
                )
            )

        if await _is_toggle_checked(target):
            return

        btn = target.locator("button")
        click_target = btn.first if await btn.count() else target
        await _click(click_target, f"the {confirm!r} confirm toggle")

    async def _read_picker_model(self, page: Page, section: Section) -> str:
        picker = page.locator(MODEL_PICKER[section]).first
        if not await picker.count():
            return ""
        caption = picker.locator(".defaults-row-caption")
        if await caption.count() and await caption.first.is_visible():
            text = await caption.first.inner_text()
            return text.split(",")[0].strip()
        return _picker_label(await picker.inner_text())

    async def apply_defaults(
        self,
        page: Page,
        *,
        section: Section,
        aspect_ligature: str,
        count: int,
        model: ModelMenuMatcher | None,
        confirm: Confirm,
    ) -> DefaultsSnapshot:
        """Set this run's defaults, Save, and return what to restore."""
        with nothing_submitted():  # #1320: settings are bound before any submit
            await self._open_pane(page)
            is_unified = await page.locator("flow-unified-settings-view").count() > 0
            base = _GROUP_BASE[section]
            try:
                if is_unified:
                    snapshot = DefaultsSnapshot(
                        section=section,
                        aspect_index=-1,
                        count_index=-1,
                        model_text=await self._read_picker_model(page, section),
                    )
                else:
                    snapshot = DefaultsSnapshot(
                        section=section,
                        aspect_index=await self._checked_index(page, base),
                        count_index=await self._checked_index(page, base + 1),
                        model_text=await self._read_picker_model(page, section),
                    )
                    await self._check_radio(page, base, aspect_ligature, "aspect")
                    await self._check_radio(page, base + 1, count - 1, "count")
                if model is not None:
                    await self._choose_model(page, section, model)
                await self._set_confirm(page, confirm)
                await self._save(page)
            except Exception:
                # Unsaved radios left in an open pane would be snapshotted by the next run in
                # the session as the account's originals. A no-op once Save has closed it.
                await self._discard_pane(page)
                raise
        log.info(
            "migrated.agent_only.defaults_applied",
            section=section,
            aspect=aspect_ligature,
            count=count,
            confirm=confirm,
        )
        return snapshot

    async def restore_defaults(self, page: Page, snapshot: DefaultsSnapshot) -> None:
        """Put back aspect / count / model. Confirm is deliberately left as applied."""
        await self._open_pane(page)
        base = _GROUP_BASE[snapshot.section]
        try:
            if snapshot.aspect_index >= 0:
                await self._check_radio(page, base, snapshot.aspect_index, "aspect")
            if snapshot.count_index >= 0:
                await self._check_radio(page, base + 1, snapshot.count_index, "count")
            await self._choose_model(page, snapshot.section, snapshot.model_text)
            await self._save(page)
        except Exception:
            # The failed-apply hazard from the other end: left open, the next run would
            # take this run's values for the originals and "restore" them for good.
            await self._discard_pane(page)
            raise
        log.info("migrated.agent_only.defaults_restored", section=snapshot.section)

    # --- generation ----------------------------------------------------------------------

    async def _media(self, page: Page) -> dict[str, TileMedia]:
        """Finished images on the grid, by uuid (a tile and its chat twin share one)."""
        found: dict[str, TileMedia] = {}
        for tile in await page.evaluate(_TILES_JS):
            uuid = _tile_uuid(tile, "image") if tile["tile"] == "flow-image-tile" else None
            if uuid:
                found.setdefault(uuid, TileMedia(uuid, tile["poster"]))
        return found

    async def _hover_video_src(self, page: Page) -> str:
        """The newest finished tile mounts its `<video>` only while hovered (measured)."""
        tile = page.locator(FINISHED_VIDEO_TILE).first
        try:
            await tile.hover(timeout=5000)
            video = tile.locator("video").first
            await video.wait_for(state="attached", timeout=5000)
            return await video.get_attribute("src") or ""
        except Exception:  # noqa: BLE001 - a poster-only tile is not ready yet; poll again
            return ""
        finally:
            await page.mouse.move(0, 0)

    async def _any_uuids(self, page: Page) -> set[str]:
        uuids: set[str] = set()
        for tile in await page.evaluate(_TILES_JS):
            for src in (tile["poster"], tile["video"]):
                m = _CDN_MEDIA_RE.match(src or "")
                if m:
                    uuids.add(m.group(2).lower())
            if _UUID_RE.match(tile.get("id") or ""):
                uuids.add(tile["id"].lower())
        return uuids

    async def _last_reply(self, page: Page) -> str:
        try:
            bubbles = page.locator(LAST_REPLY)
            if await bubbles.count():
                return redact_sensitive_text((await bubbles.last.inner_text())[:300])
        except Exception:  # noqa: BLE001 - diagnostics only
            pass
        return ""

    async def _approve_gate(self, page: Page, baseline_gates: int) -> None:
        try:
            # The gate whose title was read, not ``.last``: with two live at once, the
            # newer one's count was never checked (#988 review).
            gate = page.locator(LIVE_GATE).nth(baseline_gates)
            await gate.locator(APPROVE_ROW).first.click(timeout=5000)
        except PlaywrightError as exc:
            # After the submit: whether the gate took the click is unknown, so never retry.
            raise FlowAgentUiError(
                detail="agent-only composer: could not approve the credit confirmation",
                retryable=False,
                remediation_hint=_AFTER_SUBMIT_HINT,
            ) from exc
        # The live-gate count is back at the baseline once the gate at that index is gone.
        settled = page.locator(LIVE_GATE).nth(baseline_gates)
        try:
            await settled.wait_for(state="detached", timeout=GATE_SETTLE_S * 1000)
        except PlaywrightError as exc:
            raise UiSelectorDriftError(
                detail="agent-only composer: an approved credit gate did not turn read-only"
            ) from exc
        log.info("migrated.agent_only.gate_approved")

    async def _gate_turn(self, page: Page, run: _Run) -> bool:
        """Enforce the budget and the one-approval rule; True when a gate was just approved."""
        if time.monotonic() >= run.deadline:
            raise TransportTimeoutError(
                detail=(
                    f"agent-only composer: no finished {run.kind} within {run.budget_s:.0f}s "
                    f"of submit (approvals={run.approvals})"
                    + (
                        f"; last poll: {run.lookup}; pending seen: {run.saw_pending}; "
                        f"listing reads: {run.listing_reads}"
                        if run.kind == "video"
                        else ""
                    )
                ),
                retryable=False,
                remediation_hint=(
                    "The generation may still finish in Flow — check the project before "
                    "re-running, because a re-run submits again (a video spends credits "
                    "again)."
                ),
            )
        live = await page.locator(LIVE_GATE).count()
        if live <= run.baseline_gates:
            return False
        # Two new gates at once: only one can be read before an approval (#988 review).
        if run.approvals or live > run.baseline_gates + 1:
            raise FlowAgentUiError(
                detail=(
                    "agent-only composer: the agent asked for a second credit "
                    "confirmation in one run — it queued more than was requested, so "
                    "the second one was not approved. Last reply: "
                    f"{await self._last_reply(page)}"
                ),
                retryable=False,
                remediation_hint=_AFTER_SUBMIT_HINT,
            )
        if not await self._gate_within_count(page, run):
            await asyncio.sleep(POLL_S)  # the title is still rendering
            return True
        await self._approve_gate(page, run.baseline_gates)
        run.approvals += 1
        return True

    async def _gate_within_count(self, page: Page, run: _Run) -> bool:
        """False while the gate's title is unreadable; raises when it must not be approved."""
        gate = page.locator(LIVE_GATE).nth(run.baseline_gates)
        try:
            title = await gate.locator(GATE_TITLE).first.inner_text(timeout=2000)
        except PlaywrightError:
            title = ""
        quotes_more = gate_quotes_more(title, run.count)
        shown = redact_sensitive_text(title[:200]) or "(no title)"
        if quotes_more:
            rejected = await self._reject_gate(page, run.baseline_gates)
            log.warning("migrated.agent_only.gate_rejected", count=run.count, settled=rejected)
            raise FlowAgentUiError(
                detail=(
                    "agent-only composer: the credit confirmation quotes more than the "
                    f"{run.count} requested, so it was rejected, not approved. It read: {shown}"
                ),
                retryable=False,
                remediation_hint=_REJECTED_HINT if rejected else _AFTER_SUBMIT_HINT,
            )
        if quotes_more is False:
            return True
        if run.gate_seen_at is None:
            run.gate_seen_at = time.monotonic()
        if time.monotonic() - run.gate_seen_at < GATE_SETTLE_S:
            return False
        if run.kind == "image":
            # Unmeasured title shape. Images cost no credits, and _await_images still
            # refuses a run that made more than requested.
            log.warning("migrated.agent_only.gate_count_unread", kind=run.kind)
            return True
        raise FlowAgentUiError(
            detail=(
                "agent-only composer: could not read how many videos the credit confirmation "
                f"would bill, so it was not approved. It read: {shown}"
            ),
            retryable=False,
            remediation_hint=(
                "Nothing was approved, but the confirmation is still open in the Flow "
                "project: reject it there. Please report its wording in an issue so gflow "
                "can read it."
            ),
        )

    async def _reject_gate(self, page: Page, baseline_gates: int) -> bool:
        """Click Reject; True once the gate is seen to turn read-only."""
        gate = page.locator(LIVE_GATE).nth(baseline_gates)
        try:
            await gate.locator(REJECT_ROW).first.click(timeout=5000)
            await (
                page.locator(LIVE_GATE)
                .nth(baseline_gates)
                .wait_for(state="detached", timeout=GATE_SETTLE_S * 1000)
            )
        except PlaywrightError:
            return False
        return True

    async def _video_baseline(self, page: Page) -> tuple[set[str], str | None]:
        """Clip uuids already named, and the newest finished tile, before the submit."""
        state0 = await page.evaluate(_VIDEO_JS)
        if state0["pending"]:
            # A clip still generating (a timed-out earlier run, say) would finish during
            # ours and pass every readiness check as this run's result.
            raise FlowAgentUiError(
                detail=(
                    "agent-only composer: a video is still generating in this project, so "
                    "a new one could not be told apart from it; nothing was submitted"
                ),
                remediation_hint="Wait for the pending clip in the Flow project, then re-run.",
                charged="no",  # #1320: raised before the submit
            )
        head = _tile_key(state0["finished"][0]) if state0["finished"] else None
        return set(_video_uuids(state0)) | _asb_tokens(state0), head

    async def read_chips(self, page: Page) -> list[dict[str, str]]:
        """Every mention now in the prompt, with the kind that decides its wire slot."""
        return await page.evaluate(
            "(sel) => { const all = document.querySelectorAll(sel);"
            " const ce = all.length ? all[all.length - 1] : document;"
            " return [...ce.querySelectorAll('.mention-chip')].map(c => ({"
            "  text: (c.textContent || '').trim(),"
            "  entity_id: c.getAttribute('data-entity-id') || '',"
            "  reference_type: c.getAttribute('data-reference-type') || '',"
            " })); }",
            EDITOR,
        )

    async def _caret_to_end(self, page: Page) -> list[dict[str, str]]:
        """Focus the editor with the caret at its END; return the chips already there."""
        editor = page.locator(EDITOR).last
        if not await editor.count():
            raise UiSelectorDriftError(detail=f"agent-only composer: editor ({EDITOR}) missing")
        await _click(editor, "the editor", timeout_ms=5000)
        await page.evaluate(_AGENT_CARET_TO_END_JS, EDITOR)
        return await self.read_chips(page)

    async def _require_chips_kept(self, page: Page, before: int, name: str) -> list[dict[str, str]]:
        """The chips after a failed query was cleared; refuse if one already attached is gone."""
        chips = await self.read_chips(page)
        if len(chips) < before:
            raise ReferenceNotFoundError(
                detail=(
                    f"migrated host: clearing the failed {name!r} query removed an "
                    f"already-attached reference ({before} chip(s) before, "
                    f"{len(chips)} after) — the prompt is no longer the one that was "
                    "built, so this run is abandoned rather than submitted"
                ),
                remediation_hint=(
                    "Nothing was submitted. The composer dropped a reference while a "
                    "mention was retried; re-run the command"
                ),
            )
        return chips

    async def _mention_character(self, page: Page, name: str, *, expect_chips: int) -> None:
        """Commit the picker option TITLED exactly *name* — one gesture, nothing on trial."""
        titles: list[str] = []
        before: list[dict[str, str]] = []
        for attempt in range(1, FRAME_SEARCH_ATTEMPTS + 1):
            before = await self._caret_to_end(page)
            await page.keyboard.type("@", delay=120)
            await page.wait_for_timeout(2200)
            await page.keyboard.type(name, delay=100)
            await page.wait_for_timeout(2500)
            titles = [str(t) for t in await page.evaluate(_OPTION_TITLES_JS, PICKER_OPTION)]
            if titles or attempt == FRAME_SEARCH_ATTEMPTS:
                break
            log.info(
                "migrated.agent_only.mention_miss",
                name_chars=len(name),
                attempt=attempt,
                offered=0,
            )
            for _ in range(len(name) + 1):
                await page.keyboard.press("Backspace")
            await page.keyboard.press("Escape")
            await self._require_chips_kept(page, len(before), name)
            await page.wait_for_timeout(FRAME_SEARCH_RETRY_PAUSE_S * 1000)
        exact = [i for i, t in enumerate(titles) if t.strip().casefold() == name.casefold()]
        offered = f"{len(titles)} option(s)"
        if len(exact) != 1:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(800)
            why = (
                "no option is titled exactly that"
                if not exact
                else f"{len(exact)} options are titled exactly that, and a character and a "
                "media asset cannot be told apart before one is committed"
            )
            raise ReferenceNotFoundError(
                detail=(
                    f"migrated host: asked for character {name!r} but {why}; the picker "
                    f"offered {offered}. Refusing before submit"
                ),
                remediation_hint=(
                    "Nothing was submitted. No picker option carried that name: check the "
                    "project's characters with `gflow character list --project <id>` (MCP: "
                    "gflow_character_list)"
                    if not exact
                    else "Nothing was submitted. Rename the colliding asset in Flow, or give "
                    "the character a unique one-word name"
                ),
            )
        index = exact[0]
        for _ in range(index):
            await page.keyboard.press("ArrowDown")
            await page.wait_for_timeout(ARROW_STEP_MS)
        if index:
            await page.wait_for_timeout(ARROW_SETTLE_MS)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(2500)
        chips = await self.read_chips(page)
        log.info("migrated.agent_only.character_option", option_index=index, offered=len(titles))
        if len(chips) != expect_chips:
            raise ReferenceNotFoundError(
                detail=(
                    f"migrated host: committing the option titled {name!r} committed no chip "
                    f"({len(chips)} chip(s), expected {expect_chips}); the picker offered "
                    f"{offered}"
                ),
            )
        require_appended(before, chips)

    async def attach_character_entities(
        self,
        page: Page,
        *,
        names: tuple[str, ...],
        entity_ids: tuple[str, ...] = (),
        clear: bool = True,
    ) -> tuple[str, ...]:
        """Mention each character by name, then prove the chip is that ENTITY (#723)."""
        if clear:
            editor = page.locator(EDITOR).last
            if not await editor.count():
                raise UiSelectorDriftError(detail=f"agent-only composer: editor ({EDITOR}) missing")
            await _click(editor, "the editor", timeout_ms=5000)
            await page.keyboard.press("ControlOrMeta+a")
            await page.keyboard.press("Delete")
            await page.wait_for_timeout(600)
        base = len(await self.read_chips(page))
        for i, name in enumerate(names):
            await self._mention_character(page, name, expect_chips=base + i + 1)

        chips = (await self.read_chips(page))[base:]
        not_entities = [c for c in chips if c.get("reference_type") != "entity"]
        if not_entities:
            got = ", ".join(f"{c.get('text')!r} ({c.get('reference_type')})" for c in not_entities)
            raise ReferenceNotFoundError(
                detail=(
                    f"migrated host: asked for character(s) {', '.join(names)} but the "
                    f"picker committed {got}. Flow offers characters and media under one "
                    f"search and does not rank them, so a file sharing the name can win. "
                    f"Refusing to spend credits on a generation that would carry the wrong "
                    f"reference"
                ),
            )
        got_names = sorted(str(c.get("text", "")).casefold() for c in chips)
        if got_names != sorted(n.casefold() for n in names):
            raise ReferenceNotFoundError(
                detail=(
                    f"migrated host: asked for {len(names)} character(s) by name but the "
                    "committed chips carry different names. Refusing to submit"
                ),
            )
        attached = {c.get("entity_id", "") for c in chips}
        missing = [e for e in entity_ids if e not in attached]
        if missing:
            raise ReferenceNotFoundError(
                detail=(
                    f"migrated host: the prompt carries entity chips {sorted(attached)} but "
                    f"{missing} was requested — a chip of the right kind is not proof it is "
                    f"the right character. Refusing to submit"
                ),
            )
        committed = tuple(c.get("entity_id", "") for c in chips)
        log.info(
            "migrated.agent_only.character_entities_attached",
            count=len(chips),
            names=list(names),
            entity_ids=list(committed),
        )
        return committed

    async def _submit(
        self,
        page: Page,
        directive: str,
        *,
        reference_entity_names: tuple[str, ...] = (),
    ) -> None:
        with nothing_submitted():  # #1320: the click below is the first send
            editor = page.locator(EDITOR).last
            if not await editor.count():
                raise UiSelectorDriftError(detail=f"agent-only composer: editor ({EDITOR}) missing")
            await _click(editor, "the editor", timeout_ms=5000)
            await page.keyboard.press("ControlOrMeta+a")
            await page.keyboard.press("Delete")
            if reference_entity_names:
                await self.attach_character_entities(
                    page, names=reference_entity_names, clear=False
                )
                await page.evaluate(_AGENT_CARET_TO_END_JS, EDITOR)
                await page.keyboard.insert_text(f" {directive}")
            else:
                await page.keyboard.insert_text(directive)
            submit = page.locator(SUBMIT).last
            if not await submit.count():
                await _raise_if_out_of_credits(page)
                raise UiSelectorDriftError(detail=f"agent-only composer: submit ({SUBMIT}) missing")
        await _click(submit, "submit", timeout_ms=5000)

    async def generate(
        self,
        page: Page,
        directive: str,
        *,
        kind: Section,
        count: int,
        budget_s: float,
        on_first_media: Callable[[str], object] | None = None,
        read_listing: ListingReader | None = None,
        reference_entity_names: tuple[str, ...] = (),
    ) -> list[TileMedia]:
        """Submit ``directive`` and return the ``count`` new results it produced.

        ``read_listing`` returns the project's ``Zzl0ze`` payload (or ``None``); a video
        whose page names no uuid is looked up there (#1354)."""
        baseline_gates = await page.locator(LIVE_GATE).count()
        if kind == "video":
            baseline, baseline_head = await self._video_baseline(page)
        else:
            baseline, baseline_head = await self._any_uuids(page), None
        submitted_at = time.time()
        await self._submit(page, directive, reference_entity_names=reference_entity_names)
        log.info("migrated.agent_only.submitted", kind=kind, count=count)
        run = _Run(kind, budget_s, time.monotonic() + budget_s, baseline_gates, count, submitted_at)
        run.seen_before = frozenset(baseline)
        if kind == "video":
            return await self._await_video(
                page, run, count, baseline, baseline_head, on_first_media, read_listing
            )
        return await self._await_images(page, run, count, baseline)

    async def _await_video(
        self,
        page: Page,
        run: _Run,
        count: int,
        baseline: set[str],
        baseline_head: str | None,
        on_first_media: Callable[[str], object] | None,
        read_listing: ListingReader | None,
    ) -> list[TileMedia]:
        started = False
        peak_pending = 0
        while True:
            if await self._gate_turn(page, run):
                continue
            state = await page.evaluate(_VIDEO_JS)
            named = [u for u in _video_uuids(state) if u not in baseline]
            peak_pending = max(peak_pending, state["pending"])
            run.saw_pending = run.saw_pending or bool(state["pending"])
            if peak_pending > count:
                # One gate can cover several clips (its title names the count), so the
                # one-approval rule alone does not stop an over-billed run.
                raise FlowAgentUiError(
                    detail=(
                        f"agent-only composer: {peak_pending} videos were queued where "
                        f"{count} was requested. Last reply: {await self._last_reply(page)}"
                    ),
                    retryable=False,
                    remediation_hint=_AFTER_SUBMIT_HINT,
                )
            if named and not started and on_first_media is not None:
                started = True
                await _maybe_await(on_first_media(named[0]))
            ready = await self._ready_video(page, state, named, baseline_head, run, read_listing)
            # #1354: the listing may name the clip before its player mounts, or the run may
            # time out waiting for it; the early report must not wait for the download.
            listed = ready.uuid if ready is not None else (run.listed and run.listed[1])
            if listed and not started and on_first_media is not None:
                started = True
                await _maybe_await(on_first_media(listed))
            if ready is not None:
                return [ready]
            await asyncio.sleep(POLL_S)

    async def _ready_video(
        self,
        page: Page,
        state: dict[str, Any],
        named: list[str],
        baseline_head: str | None,
        run: _Run,
        read_listing: ListingReader | None,
    ) -> TileMedia | None:
        """This run's finished clip, or None while it is not ready yet."""
        head = state["finished"][0] if state["finished"] else None
        # Nothing is still pending, the newest finished tile is not the one that was newest
        # before submit, and a uuid names THIS clip: from the page, or else from the listing
        # record checked in `_listed_uuid` (a re-render alone swaps a tile's cdn media for
        # /asb/, which changes the head without making a clip).
        if not head:
            run.lookup = "no finished video tile mounted"
            return None
        if state["pending"]:
            run.lookup = "a clip is pending"
            return None
        if _tile_key(head) == baseline_head:
            run.lookup = "the newest finished tile is the one from before the submit"
            return None
        if named:
            uuid = _cdn_uuid(head["video"], "video") or named[-1]
        else:
            uuid = await self._listed_uuid(head, run, read_listing)
            # The read takes seconds: the tile hovered below must still be the one it named.
            if uuid is not None and not await _head_carries(page, asb_token(head["img"])):
                run.lookup = "the newest tile changed during the listing read"
                return None
        if uuid is None:
            return None
        src = head["video"] or await self._hover_video_src(page)
        if not src:
            run.lookup = f"clip {uuid} named, its player not mounted"
            return None
        log.info("migrated.agent_only.video_ready", media_id=uuid)
        return TileMedia(uuid, src)

    @staticmethod
    async def _listed_uuid(
        head: dict[str, str], run: _Run, read_listing: ListingReader | None
    ) -> str | None:
        """The uuid of the listed record whose poster shares the tile's `/asb/` token (#1354).

        Only a finished record that was not on the page before the submit, and was created
        after it, counts: an idle agent can re-render the OLD tile to `/asb/`, and its record
        must not pass as this run's clip. The clock check is the second guard, not the first."""
        token = asb_token(head["img"])
        if run.listed is not None and run.listed[0] == token:
            return run.listed[1]  # named already; only the hover player was not mounted yet
        if read_listing is None or token is None:
            run.lookup = (
                "no listing reader"
                if read_listing is None
                else "the newest tile has no /asb/ token"
            )
            return None
        if token in run.seen_before:
            run.lookup = "the newest tile was on the page before the submit"
            return None
        if not run.saw_pending:  # a head change before our clip queues is not our clip
            run.lookup = "this run's clip was never seen pending"
            return None
        if time.monotonic() < run.listing_after:
            return None
        budget = max(1.0, min(LISTING_READ_BUDGET_S, run.deadline - time.monotonic()))
        listing = await read_listing(budget)
        run.listing_reads += 1
        run.listing_after = time.monotonic() + LISTING_RETRY_S
        found = (
            None if listing is None else generation_record_by_poster(LISTING_RPC, listing, token)
        )
        if found is None:
            run.lookup = "the listing " + (
                "did not answer" if listing is None else "has no record for the newest tile"
            )
            log.info("migrated.agent_only.video_not_listed_yet")
            return None
        record, created = found
        if (
            not record.is_done
            or record.media_id.lower() in run.seen_before
            or created is None
            # Second guard; the clock margin matches the classic composer's prompt match.
            or created < run.submitted_at - PROMPT_MATCH_MARGIN_S
        ):
            run.lookup = f"the listed record {record.media_id} is unfinished or not this run's"
            log.info(
                "migrated.agent_only.listed_video_not_ours",
                media_id=record.media_id,
                status=record.status,
                created=created,
            )
            return None
        log.info("migrated.agent_only.video_named_by_listing", media_id=record.media_id)
        run.listed = (token, record.media_id.lower())
        return run.listed[1]

    async def _await_images(
        self, page: Page, run: _Run, count: int, baseline: set[str]
    ) -> list[TileMedia]:
        idle_since: float | None = None
        while True:
            if await self._gate_turn(page, run):
                continue
            in_flight = bool(await page.locator(IN_FLIGHT).count())
            fresh = {u: m for u, m in (await self._media(page)).items() if u not in baseline}
            if len(fresh) >= count and not in_flight:
                if len(fresh) > count:
                    raise FlowAgentUiError(
                        detail=(
                            f"agent-only composer: {len(fresh)} {run.kind} results appeared "
                            f"where {count} were requested ({', '.join(sorted(fresh))})"
                        ),
                        retryable=False,
                        remediation_hint=_AFTER_SUBMIT_HINT,
                    )
                return list(fresh.values())
            busy = bool(run.approvals or in_flight or fresh)
            idle_since = await self._idle_turn(page, idle_since, busy=busy)
            await asyncio.sleep(POLL_S)

    async def _idle_turn(self, page: Page, since: float | None, *, busy: bool) -> float | None:
        """Fail an image turn that ended with no gate and no tile for ``IDLE_S``."""
        if busy:
            return None
        since = since or time.monotonic()
        if time.monotonic() - since >= IDLE_S:
            raise FlowAgentUiError(
                detail=(
                    "agent-only composer: the agent turn ended with no image and no "
                    f"credit gate. Last reply: {await self._last_reply(page)}"
                ),
                retryable=False,
                remediation_hint=_AFTER_SUBMIT_HINT,
            )
        return since


@dataclass
class _Run:
    """One generation's budget and gate bookkeeping, shared by both result loops."""

    kind: Section
    budget_s: float
    deadline: float
    baseline_gates: int
    count: int
    #: Wall-clock time just before the submit click: a listed clip created earlier is not ours.
    submitted_at: float
    approvals: int = 0
    #: When this run's gate was first seen with a title that could not be read yet.
    gate_seen_at: float | None = None
    #: Clip uuids and `/asb/` tokens on the page before the submit: never this run's clip.
    seen_before: frozenset[str] = frozenset()
    #: Whether this run's clip has shown as pending; the listing is not read before that.
    saw_pending: bool = False
    #: Monotonic time before which the project listing is not read again (#1354).
    listing_after: float = 0.0
    #: (tile token, media id) the listing already named: kept while the hover player mounts.
    listed: tuple[str, str] | None = None
    #: Why the last poll did not return a video, and how often the listing was read: the
    #: timeout names both, so an exit 9 says where the run stopped (#1354).
    lookup: str = "nothing polled yet"
    listing_reads: int = 0


async def _head_carries(page: Page, token: str | None) -> bool:
    """Whether the newest finished video tile still shows the `/asb/` ``token`` (#1354)."""
    finished = (await page.evaluate(_VIDEO_JS))["finished"]
    return bool(finished) and asb_token(finished[0]["img"]) == token


async def _maybe_await(value: object) -> None:
    if asyncio.iscoroutine(value):
        await value


def unported_image(request: GenerateImageRequest) -> str | None:
    if request.refs or request.ref_paths:
        return "image-to-image"
    if request.reference_entities or request.reference_entity_names:
        return "character references"
    if request.instructions:
        return "Agent instructions"
    if request.model not in IMAGE_MODEL_MENU_MATCHERS:
        return f"the {request.model.value} model"
    if not 1 <= request.count <= 4:
        return f"a count of {request.count}"
    return None


def unported_video(request: GenerateVideoRequest) -> str | None:
    if request.mode is not Mode.T2V:
        return f"{request.mode.value}"
    if request.reference_entities:
        return "character references"
    if request.count != 1:
        return "more than one video per request"
    if request.aspect not in _VIDEO_ASPECT_LABEL:
        return f"the {request.aspect.value} aspect ratio"
    if request.model is not None and request.model not in VIDEO_MODEL_MENU_MATCHERS:
        return f"the {request.model.value} model"
    if request.resolution is not None:
        return "an explicit resolution"
    return None


def refuse_unported(what: str) -> FlowHostMigratedError:
    return FlowHostMigratedError(
        detail=(
            "this account is served Flow's agent-only composer, where gflow drives text-to-image "
            f"and text-to-video; {what} is not ported to it yet (#799)"
        )
    )


def _confirm_setting() -> Confirm:
    from gflow_cli.config import get_settings  # noqa: PLC0415

    return get_settings().agent_confirm


async def _restore_quietly(
    composer: AgentOnlyComposer, page: Page, snapshot: DefaultsSnapshot
) -> None:
    """Restore must never mask the run's own outcome; a failure is logged with the originals."""
    try:
        await composer.restore_defaults(page, snapshot)
    except Exception as exc:  # noqa: BLE001 - reported and never raised over the real result
        log.warning(
            "migrated.agent_only.defaults_restore_failed",
            section=snapshot.section,
            aspect_index=snapshot.aspect_index,
            count_index=snapshot.count_index,
            model=snapshot.model_text,
            error=str(exc)[:200],
        )


async def download_video(page: Page, src: str, uuid: str, save: ClipSaver) -> Path:
    """Fetch a finished clip from its tile src, prove it is an MP4, and store it by ``save``.

    The src is refused unless it is on an allowed Google host, and the response unless it
    ENDED on one: `/asb/` answers with a redirect to Google's video CDN, so redirects are
    followed here (unlike the signed-URL path), and the final host is what is checked.
    """
    from gflow_cli.api.transports.ui_automation import (  # noqa: PLC0415 - cycle
        _is_allowed_download_host,  # pyright: ignore[reportPrivateUsage]
    )

    if not _is_allowed_download_host(src):
        raise WireFormatError(
            detail=(
                f"agent-only composer: refusing to download a clip from "
                f"{urlsplit(src).hostname!r} (not an allowed Google host)"
            ),
            route=_VIDEO_ROUTE,
        )
    resp = await page.request.get(src, timeout=180_000, max_redirects=5)
    final_host = urlsplit(str(resp.url)).hostname or ""
    on_video_cdn = final_host == _VIDEO_REDIRECT_HOST or final_host.endswith(
        f".{_VIDEO_REDIRECT_HOST}"
    )
    if not (on_video_cdn or _is_allowed_download_host(str(resp.url))):
        raise WireFormatError(
            detail=f"agent-only composer: clip download was redirected to {final_host!r}",
            route=_VIDEO_ROUTE,
        )
    if resp.status >= 300:
        raise WireFormatError(
            detail=f"agent-only composer: clip download answered HTTP {resp.status}",
            status=resp.status,
            route=_VIDEO_ROUTE,
        )
    body = await resp.body()
    if body[4:8] != b"ftyp":
        raise WireFormatError(
            detail=(
                f"agent-only composer: the finished clip did not download as an MP4 ({len(body)} B)"
            ),
            route=_VIDEO_ROUTE,
        )
    path = await save(uuid, body)
    log.info("migrated.agent_only.video_downloaded", path=str(path), bytes=len(body))
    return path


async def run_agent_images(
    page: Page,
    request: GenerateImageRequest,
    *,
    project_id: str,
) -> list[GeneratedImage]:
    """Text-to-image on the agent-only composer. Same return shape as the classic path."""
    unported = unported_image(request)
    if unported is not None:
        raise refuse_unported(unported)
    composer = AgentOnlyComposer()
    snapshot = await composer.apply_defaults(
        page,
        section="image",
        aspect_ligature=IMAGE_ASPECT_LIGATURE[request.aspect],
        count=request.count,
        model=IMAGE_MODEL_MENU_MATCHERS[request.model],
        confirm=_confirm_setting(),
    )
    try:
        media = await composer.generate(
            page,
            compose_image_directive(
                request.prompt, request.count, _IMAGE_ASPECT_LABEL[request.aspect]
            ),
            kind="image",
            count=request.count,
            budget_s=IMAGE_BUDGET_S,
        )
    finally:
        await _restore_quietly(composer, page, snapshot)
    log.info("migrated.agent_only.images_done", project_id=project_id, count=len(media))
    images: list[GeneratedImage] = []
    for m in media:
        src = _full_size_src(m.src)
        size = await _natural_size(page, src)
        _warn_on_aspect_mismatch(request.aspect, size)
        images.append(
            GeneratedImage(
                media_name=m.uuid,
                workflow_id="",
                seed=0,
                prompt=request.prompt,
                # #789/#1012: nothing here observes which model rendered, and the agent can
                # ignore the Agent-settings default, so echoing the request would be a guess.
                model_name_type=None,
                aspect_ratio=request.aspect.value,
                fife_url=src,
                dimensions=size,
            )
        )
    return images


#: The tile's `naturalWidth` is the 512-px grid preview's (#824 printed 382x512 for an
#: 896x1200 file), so size the URL the download fetches. 0x0 when it cannot load.
_NATURAL_SIZE_JS = r"""
async (src) => {
  const img = new Image();
  img.src = src;
  try { await img.decode(); } catch (e) { return [0, 0]; }
  return [img.naturalWidth, img.naturalHeight];
}
"""


async def _natural_size(page: Page, src: str) -> tuple[int, int]:
    try:
        w, h = await page.evaluate(_NATURAL_SIZE_JS, src)
    except PlaywrightError:  # the size is a report, never worth failing a billed run over
        w = h = 0
    if not (w and h):
        log.warning("migrated.agent_only.image_size_unknown", src_kind=urlsplit(src).hostname)
    return int(w), int(h)


def _orientation(w: int, h: int) -> int:
    return (w > h) - (w < h)


def _warn_on_aspect_mismatch(aspect: ImageAspect, size: tuple[int, int]) -> None:
    """The directive asks for an aspect the agent may ignore. Warn, don't fail: the run is
    already billed and the file is valid (PLAN decision 4)."""
    w, h = size
    a, b = (int(x) for x in _IMAGE_ASPECT_LABEL[aspect].split(":"))
    if w and h and _orientation(w, h) != _orientation(a, b):
        log.warning("migrated.agent_only.aspect_mismatch", requested=aspect.value, got=f"{w}x{h}")


async def run_agent_video(
    page: Page,
    request: GenerateVideoRequest,
    *,
    project_id: str,
    save: ClipSaver,
    poll_timeout_s: float,
    download: bool,
    on_started: VideoStartedCallback | None,
) -> VideoResult:
    """Text-to-video on the agent-only composer. Same return shape as the classic path."""
    unported = unported_video(request)
    if unported is not None:
        raise refuse_unported(unported)
    composer = AgentOnlyComposer()
    snapshot = await composer.apply_defaults(
        page,
        section="video",
        aspect_ligature=ASPECT_LIGATURE[request.aspect],
        count=1,
        model=VIDEO_MODEL_MENU_MATCHERS[request.model] if request.model else None,
        confirm=_confirm_setting(),
    )

    def started(media_id: str) -> object:
        if on_started is None:
            return None
        return on_started(VideoStarted(media_id=media_id, project_id=project_id))

    try:
        (media,) = await composer.generate(
            page,
            compose_video_directive(
                request.prompt, request.duration, _VIDEO_ASPECT_LABEL[request.aspect]
            ),
            kind="video",
            count=1,
            budget_s=poll_timeout_s,
            on_first_media=started,
            read_listing=lambda budget_s: read_project_listing(page, project_id, budget_s=budget_s),
            reference_entity_names=request.reference_entity_names,
        )
    finally:
        await _restore_quietly(composer, page, snapshot)
    local_path = await download_video(page, media.src, media.uuid, save) if download else None
    return VideoResult(
        status=VideoStatus(media_id=media.uuid, status="MEDIA_GENERATION_STATUS_SUCCESSFUL"),
        local_path=local_path,
        project_id=project_id,
    )
