"""
The package modal (Chad, 20 Oct 2026): "click a service, have it give the
current version and latest of its self, and for the meninges and sinew
packages as well and be able to update those packages from there". A row of
the Behind table opens it; the three packages sit against the index's
newest, each with an update; every button is a run of that one service
through postUpgrade, so nothing here has a restart path of its own. A
library alone pins the service where it is and names the library in `with`.

Pinned by reading the page, as the other viewer tests do.
"""
from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "seren_lodestar" / "viewer" / "ui"


def test_the_modal_exists_and_a_behind_row_opens_it():
    body = (UI / "body.html").read_text(encoding="utf-8")
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    for id_ in ("pkg-backdrop", "pkg-header-fields", "pkg-refresh", "pkg-close", "pkg-body"):
        assert f'id="{id_}"' in body, id_
    behind = script[script.index("function renderBehind"):script.index("const pkgModal")]
    assert 'upg-svc" data-row="${i}"' in behind, "each row knows which service it is"
    assert "l.behind === true" in behind, "a library behind is marked on the row"
    init = script[script.index("function initPkgModal"):script.index("initSettings();")]
    assert "closest('tr[data-row]')" in init and "openPkgModal(r.node, r.service)" in init
    assert "initPkgModal();" in script


def test_the_three_packages_and_what_each_button_sends():
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    render = script[script.index("function renderPkgModal"):script.index("async function updatePackage")]
    assert "Object.entries(r.libs || {})" in render, "the libraries come from the behind row, no second fetch"
    assert "p.behind === true" in render, "update only when the index has something newer"
    assert 'id="pkg-all"' in render
    update = script[script.index("async function updatePackage"):script.index("function initPkgModal")]
    assert "confirm(" in update and update.index("confirm(") < update.index("postUpgrade(")
    assert "only_behind: false" in update and "backup: true" in update
    assert "body.version = r.version; body.with = [pkg]" in update, "a library alone: the service pinned where it is"
    assert "body.version = null; body.with = []" in update, "the service alone: the libraries left where they are"
    assert "openRun(out.run.id)" in update, "and the run opens, as every other start does"


def test_the_shell_serves_it(client):
    r = client.get("/viewer")
    assert r.status_code == 200
    for needle in ("pkg-backdrop", "function renderPkgModal", ".pkg-modal"):
        assert needle in r.text, needle
