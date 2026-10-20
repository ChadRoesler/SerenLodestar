"""
The glance screen (design note, 17 Oct 2026): a 1U touchscreen on the rack
is 1424x280, a letterbox. The shell's header and tabbar eat 110px of it at
desktop sizes, and once the cluster grew past four nodes the grid wrapped to
a second row that was simply off the screen. On a short viewport the viewer
now shrinks the chrome, lays the nodes out as one snapping horizontal row
(four wide, the rest a flick away), drops the actions card and puts settings
and refresh in the header strip. Which nodes sit on that row is the browser's
own choice (settings, "On this screen", localStorage), and a hidden node
comes back by itself while it is offline or has something down.

Pinned by reading the page, as the other viewer tests do.
"""
from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "seren_lodestar" / "viewer" / "ui"


def _short_viewport_block() -> str:
    css = (UI / "styles.css").read_text(encoding="utf-8")
    start = css.index("@media (max-height: 400px)")
    depth, i = 0, css.index("{", start)
    for j in range(i, len(css)):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[start:j + 1]
    raise AssertionError("short-viewport block never closes")


def test_the_short_viewport_lays_nodes_out_as_one_snapping_row():
    block = _short_viewport_block()
    assert "--glance-cols: 4" in block, "a default before the first render"
    grid = block[block.index(".grid {"):block.index(".grid::-webkit-scrollbar")]
    assert "display: flex" in grid and "overflow-x: auto" in grid and "scroll-snap-type: x mandatory" in grid
    card = block[block.index(".grid > .node {"):block.index(".node-name")]
    assert "var(--glance-cols)" in card and "scroll-snap-align: start" in card
    assert "min-height: 0" in card, "the 220px desktop minimum would not fit"
    assert ".node-ip { display: none; }" in block
    assert "overflow-x: hidden" in block and ".svc-list .svc-stat { width: auto; min-width: 1em; }" in block, "no scrollbar for a stopped service's glyph"
    assert ".actions-card { display: none; }" in block
    assert ".svc-list .svc-caret, .svc-list .svc-count { display: none; }" in block, "a group does not fold on the glance screen"
    assert "pointer-events: none" in block[block.index(".svc-list .svc-group"):], "...and the tap goes to the card"
    assert ".gaps { display: none !important; }" in block, "the LED says it"
    assert ".svc-list .svc-badge { display: none; }" in block, "an emoji badge is a hex box on a kiosk without an emoji font"


def test_the_row_is_as_wide_as_it_has_nodes_and_pages_by_arrow_past_five():
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    body = (UI / "body.html").read_text(encoding="utf-8")
    block = _short_viewport_block()
    assert "const GLANCE_MAX = 5" in script
    grid = script[script.index("function renderGrid"):script.index("function glanceStep")]
    assert "Math.min(Math.max(shown, 1), GLANCE_MAX)" in grid, "five nodes fill the row, four do not leave a hole"
    assert "classList.toggle('paged', shown > GLANCE_MAX)" in grid, "paging is decided from the count, never measured"
    for id_ in ("grid-row", "glance-prev", "glance-next"):
        assert f'id="{id_}"' in body, id_
    assert ".grid-row .glance-arrow { display: none; }" in block
    assert ".grid-row.paged .glance-arrow { display: flex; }" in block
    glance = script[script.index("function initGlance"):script.index("// ------", script.index("function initGlance"))]
    assert "scrollBy({ left: -glanceStep()" in glance and "scrollBy({ left: glanceStep()" in glance, "a tap moves one card"
    assert "initGlance();" in script


def test_the_short_viewport_thins_the_shell_chrome_it_does_not_own():
    block = _short_viewport_block()
    assert "header.seren-head {" in block and ".seren-head .subtitle { display: none; }" in block
    assert ".tabbar .tab {" in block
    assert "#views { max-width: none; }" in block, "the shell's 1100px cap would waste a third of 1424"


def test_settings_and_refresh_move_to_the_header_strip_on_the_glance_screen():
    css = (UI / "styles.css").read_text(encoding="utf-8")
    aside = (UI / "header_aside.html").read_text(encoding="utf-8")
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    # The shell's header.seren-head button sets display, so the hide needs
    # the same specificity (20 Oct 2026: the gear showed on every desk).
    assert re.search(r"\.glance-only, header\.seren-head \.glance-only\s*\{\s*display:\s*none;", css),         "hidden on a desktop: the header pair with the shell's specificity, and the grid's arrows with the plain class"
    assert "header.seren-head .glance-only { display: inline-flex" in _short_viewport_block()
    for id_ in ("aside-settings", "aside-refresh"):
        assert f'id="{id_}"' in aside and f'glance-only" id="{id_}"' in aside or f'glance-only icon" id="{id_}"' in aside, f"{id_} is glance only"
    assert "function eagerRefresh" in script, "one refresh shared by the card and the strip"
    assert "eagerRefresh(refreshBtn)" in script and "eagerRefresh(asideRefresh)" in script
    assert "asideSettings.addEventListener('click', openSettingsModal)" in script


def test_which_nodes_are_on_this_screen_is_the_browsers_choice():
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    body = (UI / "body.html").read_text(encoding="utf-8")
    assert "const HIDDEN_NODES_KEY = 'lodestar.hiddenNodes'" in script
    assert "localStorage.getItem(HIDDEN_NODES_KEY)" in script and "localStorage.setItem(HIDDEN_NODES_KEY" in script
    load = script[script.index("function loadHiddenNodes"):script.index("function saveHiddenNodes")]
    assert "try {" in load and "return new Set()" in load, "blocked storage is an empty choice, not an error"
    grid = script[script.index("function renderGrid"):script.index("function eagerRefresh")]
    assert "nodeShownHere(node, hidden)" in grid
    assert 'id="cfg-nodes"' in body and "On this screen" in body
    assert "function renderScreenNodes" in script
    opener = script[script.index("function openSettingsModal"):script.index("function closeSettingsModal")]
    assert "renderScreenNodes()" in opener, "the tick boxes are built from the last payload when the modal opens"


def test_a_hidden_node_stays_hidden_and_the_led_still_counts_it():
    """Until 20 Oct 2026 a hidden node came back while it needed you; then
    the desktop went down and Chad could not get it off the grid. Hidden is
    hidden now; the settings label still says when a hidden node needs you."""
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    needs = script[script.index("function nodeNeedsYou"):script.index("function nodeShownHere")]
    assert "if (!node.online) return true" in needs
    assert "symbolFor(s?.status)[1] === 'down'" in needs
    shown = script[script.index("function nodeShownHere"):script.index("function hostApi")]
    assert "return !hidden.has(node.name);" in shown and "nodeNeedsYou" not in shown
    settings = script[script.index("function renderScreenNodes"):script.index("function renderScreenPrefs")]
    assert "nodeNeedsYou(node)" in settings, "the tick box still says when a hidden node needs you"
    # the LED and the gaps strip never look at the hidden set
    for fn in ("function renderGaps", "function setOverallLed"):
        seg = script[script.index(fn):script.index("function", script.index(fn) + 10)]
        assert "loadHiddenNodes" not in seg and "nodeShownHere" not in seg, fn


def test_the_shell_serves_the_glance_screen(client):
    r = client.get("/viewer")
    assert r.status_code == 200
    for needle in ("aside-settings", "cfg-nodes", "HIDDEN_NODES_KEY", "--glance-cols"):
        assert needle in r.text, needle
