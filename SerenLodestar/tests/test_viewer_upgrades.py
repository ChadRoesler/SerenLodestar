"""
The viewer's Upgrades tab (16 Oct 2026). /api/v1/upgrade had its routes and
its Workbench tools for weeks and the viewer showed nothing; whoever opened
the dashboard could not tell what was behind, let alone move it.

Pinned here, by reading the page rather than driving a browser:
- the tab exists and has a view of the same name for the shell to switch to;
- the script reaches only the upgrade routes it is meant to (behind, the
  describe, a run by id, and POST for a plan or a run) - the viewer SHOWS
  Lodestar's decisions, it does not make its own;
- a run is behind a confirm, and it only ever asks for what is behind;
- the shell serves the fragments.
"""
from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "seren_lodestar" / "viewer" / "ui"


def test_the_tab_and_its_view_exist():
    tabs = (UI / "tabs.html").read_text(encoding="utf-8")
    body = (UI / "body.html").read_text(encoding="utf-8")
    assert 'data-tab="upgrades"' in tabs
    assert 'class="view" id="upgrades"' in body
    for el in ("upg-behind", "upg-plan", "upg-go", "upg-runs", "upg-run-detail", "upg-refresh"):
        assert f'id="{el}"' in body, el


def test_the_script_uses_only_the_upgrade_routes_it_should():
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    upgrade_calls = set(re.findall(r"hostApi\(\s*[`'\"](/api/v1/upgrade[^`'\"]*)", script))
    assert upgrade_calls == {"/api/v1/upgrade/behind", "/api/v1/upgrade", "/api/v1/upgrade/${encodeURIComponent(id)}"}, upgrade_calls
    assert "function refreshUpgrades" in script and "function initUpgrades" in script
    assert "initUpgrades();" in script, "wired at start, like the other panels"
    assert "if (upgradesVisible()) refreshUpgrades();" in script, "the poll keeps a running run fresh"


def test_a_run_is_behind_a_confirm_and_asks_only_for_what_is_behind():
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    start = script[script.index("async function startUpgrades"):script.index("function renderRuns")]
    assert "confirm(" in start and start.index("confirm(") < start.index("hostApi(")
    assert "only_behind: true" in start and "backup: true" in start
    assert "dry_run" not in start
    plan = script[script.index("async function planUpgrades"):script.index("function renderPlan")]
    assert "dry_run: true" in plan and "confirm(" not in plan


def test_the_shell_serves_the_fragments(client):
    r = client.get("/viewer")
    assert r.status_code == 200
    assert 'data-tab="upgrades"' in r.text and 'id="upgrades"' in r.text
