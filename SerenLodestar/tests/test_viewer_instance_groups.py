"""
Instance groups on the node cards (design note, 16 Oct 2026): a node carrying
seren-loci-wren, seren-memory-wren, seren-margin-wren ... was ten rows
wearing the instance name ten times. The card now folds one install's
services under the instance, collapsed, each shown as the app it is, and
leaves anything without an instance suffix (llama, kokoro, searxng, a bare
seren-theatre) where it was.

Pinned by reading the page, as the other viewer tests do; the grouping
itself is a pure function, pinned with a Python mirror of its regex so a
change to one shows up against the other.
"""
from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "seren_lodestar" / "viewer" / "ui"


def _js_regex() -> re.Pattern[str]:
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    m = re.search(r"const INSTANCE_RE = /(.+?)/;", script)
    assert m, "INSTANCE_RE not found"
    return re.compile(m.group(1).replace("(?:", "(?:"))


def test_the_regex_splits_an_instance_suffix_and_leaves_the_rest_alone():
    rx = _js_regex()
    assert rx.match("seren-loci-wren").groups() == ("loci", "wren")
    assert rx.match("seren-corpus-callosum-wren").groups() == ("corpus-callosum", "wren")
    assert rx.match("SerenObservatory-wren").groups() == ("Observatory", "wren")
    assert rx.match("seren-symposium-wren").groups() == ("symposium", "wren")
    for flat in ("llama", "kokoro", "searxng", "seren-theatre", "comfy", "whisper"):
        assert rx.match(flat) is None, flat


def test_the_card_groups_collapsed_by_default_and_remembers_what_was_opened():
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    assert "function groupServices" in script and "function instanceOf" in script
    node = script[script.index("function renderNode"):script.index("function renderGrid")]
    assert "serviceTree(node, { hideObservatory: false })" in node, "the card draws the tree: groups, owners, what they own"
    assert "seren-chev" in node and "' open'" in node,         "a chevron says which way the group is: the shell's, drawn from borders, so no kiosk font can lose it"
    assert "foldOpen(key, !!screen.expandInstances)" in node, "open state is per node and instance, against this screen's default"
    assert "const toggledFolds = new Set()" in script, "...and survives the poll's re-render"
    assert "svcRow(r, 1)" in node and "instanceOf(k)?.app || k" in script, "inside a group a service is shown as its app"
    assert "rollup(g.rows)" in node and "STATUS_RANK[cls]" in script and "total += r.status.memory_mb" in script, \
        "a collapsed group carries the worst status and the summed memory"
    toggle = script[script.index("function toggleFold"):script.index("// RENDER")]
    assert "window._lastStatus" in toggle, "a toggle re-renders from the last payload, not a new fetch"


def test_the_styles_exist_for_the_group_row_and_its_members():
    css = (UI / "styles.css").read_text(encoding="utf-8")
    for cls in (".svc-group", ".svc-caret", ".svc-count", ".svc-in-group", ".svc-owner", ".svc-list.capped"):
        assert cls in css, cls


def test_the_shell_serves_the_grouped_card(client):
    r = client.get("/viewer")
    assert r.status_code == 200 and "groupServices" in r.text and "svc-group" in r.text
