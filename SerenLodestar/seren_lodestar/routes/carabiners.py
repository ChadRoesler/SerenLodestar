"""
The Rack: the carabiners clipped on every node, and their settings
(seren_sinew.carabiners has the chain and the messages).

    GET  /api/v1/rack                                every node's carabiners, each with its
                                                     settings as the carabiner says them
    GET  /api/v1/carabiner/{name}/settings?node=N    one carabiner's settings
    POST /api/v1/carabiner/{name}/settings           SettingsRequest (node in the body) -> SettingsResult
    GET  /api/v1/carabiner/{name}/belay?node=N       does it still hold
    GET  /api/v1/rack/shelf                          the .kbh files Lodestar can hand out
    POST /api/v1/rack/shelf/fetch                    {"tag": "v5.1.3"} optional: a release onto the shelf
    POST /api/v1/carabiner/{name}/upgrade            {"node", "downgrade"?, "reason"?}: the shelf's
                                                     <name>.kbh, sent to the node and swapped in there

Lodestar carries a change to the node's Observatory, which runs the .kbh
as the person whose yaml it is; the carabiner checks every value and says
what it refused and why. Nothing here knows what a setting means. Each box is
set on its own: nothing is copied from one node to another.

Upgrades are the one thing that does travel (23 Oct 2026): Lodestar pulls a
.kbh onto its shelf (seren_lodestar.shelf) and sends the file, so no node has
to reach the internet. Each carabiner in the Rack says how it stands against
the shelf: current, behind, ahead, different, or none.
"""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from seren_sinew.carabiners import BELAY_PATH, LIST_PATH, SETTINGS_PATH, UPGRADE_PATH, SettingsRequest, UpgradeRequest

from ..shelf import Shelf, status as shelf_status

router = APIRouter(tags=["carabiners"])

HOLDER = "lodestar-rack"
UPGRADE_TIMEOUT = 900.0          # the node belays twice; its own limit is 600s


def _shelf(request: Request) -> Shelf:
    return request.app.state.shelf


def _path(template: str, name: str) -> str:
    return template.format(carabiner=quote(name, safe=""))


def _agent(request: Request, node: str):
    if not node:
        return None, JSONResponse({"ok": False, "error": "say which node: ?node= (or node in the body)"},
                                  status_code=400)
    agent = request.app.state.cluster.get_agent(node)
    if agent is None:
        return None, JSONResponse({"ok": False, "error": f"'{node}' is not a node in this cluster"}, status_code=404)
    return agent, None


def _named(status: int, data: dict[str, Any], node: str) -> JSONResponse:
    """The Observatory's answer, with the node as Lodestar knows it."""
    return JSONResponse({**data, "node": node}, status_code=status)


async def _node_rack(name: str, agent, snapshot, shelf: dict[str, Any]) -> dict[str, Any]:
    if snapshot is not None and not snapshot.online:
        return {"node": name, "online": False, "set_up": False, "carabiners": [], "error": "offline"}
    status, data = await agent.carabiner_call_async("GET", LIST_PATH, timeout=15.0)
    if status != 200 or not data.get("ok"):
        return {"node": name, "online": status != 502, "set_up": False, "carabiners": [],
                "error": str(data.get("error") or f"HTTP {status}")}
    entries = [e for e in data.get("carabiners") or [] if isinstance(e, dict) and e.get("carabiner")]
    answers = await asyncio.gather(*[agent.carabiner_call_async("GET", _path(SETTINGS_PATH, e["carabiner"]))
                                     for e in entries])
    carabiners = []
    for e, (st, m) in zip(entries, answers):
        on_shelf = shelf.get(e["carabiner"])
        carabiners.append({**e, "node": name, "manifest": {**m, "node": name} if st == 200 and m.get("ok") else None,
                           "update": shelf_status(str(e.get("version") or ""), str(e.get("sha256") or ""), on_shelf),
                           "shelf_version": (on_shelf or {}).get("version", ""),
                           "error": "" if st == 200 and m.get("ok") else str(m.get("error") or f"HTTP {st}")})
    return {"node": name, "online": True, "set_up": True, "carabiners": carabiners, "error": ""}


@router.get("/api/v1/rack")
async def rack(request: Request):
    cluster = request.app.state.cluster
    snapshots = cluster.get_snapshots()
    agents = cluster.agents
    shelf = await asyncio.to_thread(_shelf(request).entries)
    nodes = await asyncio.gather(*[_node_rack(n, a, snapshots.get(n), shelf) for n, a in agents.items()])
    ripple = getattr(request.app.state.config, "ripple", None)
    return {"ok": True, "wakes": {"target": getattr(ripple, "target", "") or ""}, "nodes": list(nodes),
            "shelf": list(shelf.values())}


@router.get("/api/v1/carabiner/{name}/settings")
async def carabiner_settings(request: Request, name: str, node: str = ""):
    agent, refused = _agent(request, node)
    if refused is not None:
        return refused
    status, data = await agent.carabiner_call_async("GET", _path(SETTINGS_PATH, name))
    return _named(status, data, node)


@router.post("/api/v1/carabiner/{name}/settings")
async def change_carabiner_settings(request: Request, name: str):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    req = SettingsRequest.from_dict(body if isinstance(body, dict) else {})
    agent, refused = _agent(request, req.node)
    if refused is not None:
        return refused
    req.holder = req.holder or HOLDER
    status, data = await agent.carabiner_call_async("POST", _path(SETTINGS_PATH, name), req.to_dict())
    return _named(status, data, req.node)


@router.get("/api/v1/carabiner/{name}/belay")
async def carabiner_belay(request: Request, name: str, node: str = ""):
    agent, refused = _agent(request, node)
    if refused is not None:
        return refused
    status, data = await agent.carabiner_call_async("GET", _path(BELAY_PATH, name), timeout=60.0)
    return _named(status, data, node)


@router.get("/api/v1/rack/shelf")
async def rack_shelf(request: Request):
    shelf = _shelf(request)
    entries = await asyncio.to_thread(shelf.entries)
    return {"ok": True, "repo": shelf.repo, "folder": str(shelf.root), "shelf": list(entries.values())}


@router.post("/api/v1/rack/shelf/fetch")
async def rack_shelf_fetch(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    tag = str((body or {}).get("tag") or "") if isinstance(body, dict) else ""
    # A release that cannot be read is an answer with its reason, like a
    # carabiner's refusal: the page shows it rather than a bare HTTP code.
    return await _shelf(request).fetch(tag)


@router.post("/api/v1/carabiner/{name}/upgrade")
async def upgrade_carabiner(request: Request, name: str):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    node = str(body.get("node") or "")
    agent, refused = _agent(request, node)
    if refused is not None:
        return refused
    try:
        data, entry = await asyncio.to_thread(_shelf(request).get, name)
    except LookupError as e:
        return {"ok": False, "carabiner": name, "node": node, "error": str(e)}
    req = UpgradeRequest.carrying(data, node=node, version=entry["version"], source=entry["source"],
                                  downgrade=bool(body.get("downgrade")), holder=HOLDER,
                                  reason=str(body.get("reason") or "")[:200])
    status_code, out = await agent.carabiner_call_async("POST", _path(UPGRADE_PATH, name), req.to_dict(),
                                                        timeout=UPGRADE_TIMEOUT)
    return _named(status_code, out, node)
