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
    # The shell styles tabs and activates the first; the CLICK is each tab's
    # own. Without onclick the tab was a tab that did nothing (16 Oct 2026).
    for tab in re.findall(r"<button class=\"tab[^>]*>", tabs):
        assert "onclick=\"showTab(" in tab, tab
    for el in ("upg-behind", "upg-plan", "upg-go", "upg-runs", "upg-refresh", "run-backdrop", "run-steps", "run-log"):
        assert f'id="{el}"' in body, el


def test_the_script_uses_only_the_upgrade_routes_it_should():
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    upgrade_calls = set(re.findall(r"hostApi\(\s*[`'\"](/api/v1/upgrade[^`'\"]*)", script))
    assert upgrade_calls == {"/api/v1/upgrade/behind", "/api/v1/upgrade", "/api/v1/upgrade/${encodeURIComponent(id)}",
                             "/api/v1/upgrade/${encodeURIComponent(runId)}/step/${index}"}, upgrade_calls
    assert script.count("fetch('/api/v1/upgrade'") == 1, "every POST goes through postUpgrade, which keeps the 409's reason"
    assert "function refreshUpgrades" in script and "function initUpgrades" in script
    assert "initUpgrades();" in script, "wired at start, like the other panels"
    assert "if (upgradesVisible()) refreshUpgrades();" in script, "the poll keeps a running run fresh"


def test_a_run_is_behind_a_confirm_and_asks_only_for_what_is_behind():
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    start = script[script.index("async function startUpgrades"):script.index("function renderRuns")]
    assert "confirm(" in start and start.index("confirm(") < start.index("postUpgrade(")
    assert "only_behind: true" in start and "backup: true" in start
    assert "dry_run" not in start
    plan = script[script.index("async function planUpgrades"):script.index("function renderPlan")]
    assert "dry_run: true" in plan and "confirm(" not in plan


def test_the_node_card_upgrades_that_node_and_the_tarball_push_is_gone():
    """The old Update Node / update all nodes pushed an Observatory tarball to a
    path in the yaml; no pip install has one, so both only ever answered
    "not configured". The node card now starts an upgrade run narrowed to
    its node. Settings no longer has a door to the tab either: the tab is
    right there (20 Oct 2026: "trash the open upgrade tab in the
    settings modal")."""
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    body = (UI / "body.html").read_text(encoding="utf-8")
    assert "/agent-update" not in script and "fireAgentUpdate" not in script
    node = script[script.index("async function upgradeNode"):script.index("async function postUpgrade")]
    assert "nodes: [nodeName]" in node and "only_behind: true" in node and "backup: true" in node
    confirm = script[script.index("function renderUpdateConfirmBody"):script.index("function renderCountdownBody")]
    assert "upgradeNode(node.name)" in confirm and "Upgrade" in confirm
    assert 'id="cfg-open-upgrades"' not in body and 'id="cfg-update-nodes"' not in body
    assert "cfg-open-upgrades" not in script


def test_the_shell_serves_the_fragments(client):
    r = client.get("/viewer")
    assert r.status_code == 200
    assert 'data-tab="upgrades"' in r.text and 'id="upgrades"' in r.text


def test_the_open_steps_log_follows_the_run_until_it_has_ended():
    """20 Oct 2026: "log portion of the modal ... is not refreshing as
    new data comes in". The poll re-asks the open step quietly (no
    placeholder, scroll kept) while the step or its job is still going."""
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    poll = script[script.index("async function refreshUpgrades"):script.index("function renderBehind")]
    assert "showStepLog(fresh.id, runModal.step, { quiet: true })" in poll
    assert "!runModal.logSettled" in poll, "a step that has ended is not asked again every five seconds"
    show = script[script.index("async function showStepLog"):script.index("function initRunModal")]
    assert "if (!quiet) log.innerHTML" in show, "quiet: the log is not blanked while the node is asked"
    assert "if (runModal.step !== index || !isRunOpen()) return;" in show, "a late answer for another step is dropped"
    assert "runModal.logSettled = !STEP_LIVE.includes(step.state) && !(out.job && out.job.terminal === false)" in show
    assert "if (atBottom) log.scrollTop = log.scrollHeight" in show
