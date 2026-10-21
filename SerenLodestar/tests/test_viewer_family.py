"""
The family on the card, idle on the LED, the screen's folds, the run modal
(20 Oct 2026). the list, verbatim where it shaped the code:

- "probe should own its sub processes (the docker containers whatever) these
  should be rolled into it. and chevroned so that you can click probe and see
  its owned processes and their memory usage ... same with workbench ... this
  way we dont lie about whats there and what it has"
- "services that are orchestrated shouldnt show as degraded on the top bar.
  degraded should show when its expected to be up when its not, Orchestrated
  would be the other half of it, 12/28 services idle"
- "trash the open upgrade tab in the settings modal ... offer more control:
  auto expand instances, auto expand child processes, limit shown before scroll"
- "when clicking on a run, the data should show in a modal ... we should be
  able to click the line and view the logs of what happened"
- "Observatory, we know its a node dont need it on there." (he meant the
  Observatory viewer's own hostname pill; the card keeps its Observatory row)

Pinned by reading the page, as the other viewer tests do. The DTO side is in
test_cluster_and_client-shaped checks here: the Observatory's `idle`,
`parent` and `children` must survive Lodestar's lenient dict->dataclass or
the viewer never sees them.
"""
from __future__ import annotations

from pathlib import Path

from seren_lodestar.agent_client import _from_dict
from seren_lodestar.dtos import ServiceEntry, ServiceManifest, ServiceStatus

UI = Path(__file__).resolve().parents[1] / "seren_lodestar" / "viewer" / "ui"


def test_the_family_and_idle_ride_through_the_dtos():
    st = _from_dict({"service": "seren-probe-wren", "running": True, "orchestrated": False,
                     "parent": None,
                     "children": [{"name": "seren-probe-target-memory-1", "kind": "container", "running": True,
                                   "memory_mb": 210, "cpu_percent": 1.5}]}, ServiceStatus)
    assert st.children[0]["name"] == "seren-probe-target-memory-1" and st.children[0]["memory_mb"] == 210
    idle = _from_dict({"service": "llama", "running": False, "orchestrated": True, "idle": True}, ServiceStatus)
    assert idle.idle is True and idle.orchestrated is True
    child = _from_dict({"manifest": {"service": "searxng", "service_type": "docker_compose", "parent": "seren-workbench-wren"},
                        "status": {"service": "searxng", "running": True, "parent": "seren-workbench-wren"}}, ServiceEntry)
    assert child.manifest.parent == "seren-workbench-wren" and child.status.parent == "seren-workbench-wren"
    owner = _from_dict({"service": "seren-probe-wren", "children": {"docker_project": "seren-probe-*"}}, ServiceManifest)
    assert owner.children == {"docker_project": "seren-probe-*"}


def test_the_tree_folds_owners_and_their_containers_and_hides_the_observatory_on_the_card():
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    tree = script[script.index("function parentOf"):script.index("function flattenTree")]
    assert "st?.parent || node.services_detail?.[name]?.manifest?.parent" in tree, "an owner from the manifest, via the status"
    assert "names.includes(declared)" in tree, "only an owner that is on the node folds its children"
    assert "lower.includes('searxng')" in tree, "searxng-redis still rolls under searxng without a manifest saying so"
    assert "status?.children || []" in tree and "container: true" in tree, "what the Observatory found this poll becomes rows"
    assert "hideObservatory && OBSERVATORY_RE.test(n)" in tree
    node = script[script.index("function renderNode"):script.index("function renderGrid")]
    assert "serviceTree(node, { hideObservatory: false })" in node, "the Observatory stays on the card (the Observatory viewer's hostname pill was what was meant, 20 Oct 2026)"
    modal = script[script.index("function renderModalBody"):script.index("function renderConfirmBody")]
    assert "serviceTree(node, { hideObservatory: false })" in modal
    assert "if (svc) openServiceDrill(svc)" in modal, "a container it started has no manifest to drill into"
    assert "foldOpen(key, !!screen.expandChildren)" in node, "an owner's fold follows the screen's default"
    assert "data-fold=" in node and "toggleFold(el.dataset.fold)" in node
    assert "const own = rollup([r])" in node, "an owner's row carries the family's memory and worst light"


def test_idle_is_the_other_half_of_the_led():
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    sym = script[script.index("function symbolFor"):script.index("// POLLING")]
    assert "(status.idle || status.orchestrated)) return ['◌', 'idle']" in sym
    led = script[script.index("function setOverallLed"):script.index("function setLed")]
    assert "if (cls === 'idle') idle++;" in led and "else if (cls !== 'ok') degraded++;" in led
    assert "services degraded${idleNote}" in led and "services healthy${idleNote}" in led
    assert "const STATUS_RANK = { down: 3, warn: 2, ok: 1, idle: 0 }" in script, "idle ranks under ok in a rollup"
    css = (UI / "styles.css").read_text(encoding="utf-8")
    assert ".svc-stat.idle" in css and ".modal-svc-row .state.idle" in css


def test_the_settings_modal_folds_this_screen_and_has_no_door_to_upgrades():
    body = (UI / "body.html").read_text(encoding="utf-8")
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    for el in ("cfg-expand-instances", "cfg-expand-children", "cfg-rows", "cfg-nodes"):
        assert f'id="{el}"' in body, el
    assert "cfg-open-upgrades" not in body and "update-nodes-btn" not in body
    assert "const SCREEN_KEY = 'lodestar.screen'" in script, "this browser's choice, like the hidden nodes"
    assert "rowsBeforeScroll" in script and "list.classList.add('capped')" in script
    assert "toggledFolds.clear()" in script, "auto expand means all of them: the hand-made folds are cleared"


def test_a_run_opens_in_a_modal_and_a_step_fetches_its_job():
    body = (UI / "body.html").read_text(encoding="utf-8")
    script = (UI / "scripts.js").read_text(encoding="utf-8")
    assert 'id="upg-run-detail"' not in body, "the run no longer sits at the foot of the page"
    assert "renderRunDetail" not in script
    run = script[script.index("async function openRun"):script.index("function initUpgrades")]
    assert "classList.add('visible')" in run and "renderRunModal(out.run)" in run
    assert "showStepLog(run.id, parseInt(tr.dataset.step, 10))" in run, "click the line..."
    assert "/step/${index}" in run and "job.steps || []" in run, "...and read the job's notes from the node"
    assert "e.key === 'Escape' && runModal.open" in run
    assert "if (_upgOpenRun && isRunOpen())" in script, "the poll keeps an open run fresh while it runs"
    plan = script[script.index("function renderPlan"):script.index("async function startUpgrades")]
    assert "runModal.plan = plan" in plan and "classList.add('visible')" in plan, "the plan opens in the run modal"
    assert "showPlanDetail(plan" in plan
    assert 'id="upg-plan-card"' not in body, "no plan card squeezed between Behind and Runs"


def test_the_shell_serves_it_all(client):
    r = client.get("/viewer")
    assert r.status_code == 200
    for needle in ("serviceTree", "run-backdrop", "cfg-expand-children", "svc-owner", "'idle'"):
        assert needle in r.text, needle
