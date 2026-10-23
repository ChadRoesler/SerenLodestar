"""
The Rack: the carabiners clipped on every node, and their settings
(seren_sinew.carabiners has the chain and the messages).

    GET  /api/v1/rack                                every node's carabiners, each with its
                                                     settings as the carabiner says them
    GET  /api/v1/carabiner/{name}/settings?node=N    one carabiner's settings
    POST /api/v1/carabiner/{name}/settings           SettingsRequest (node in the body) -> SettingsResult
    GET  /api/v1/carabiner/{name}/belay?node=N       does it still hold

Lodestar carries a change to the node's Observatory, which runs the .kbh
as the person whose yaml it is; the carabiner checks every value and says
what it refused and why. Nothing here knows what a setting means. Each box is
set on its own: nothing is copied from one node to another.
"""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from seren_sinew.carabiners import BELAY_PATH, LIST_PATH, SETTINGS_PATH, SettingsRequest

router = APIRouter(tags=["carabiners"])

HOLDER = "lodestar-rack"


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


async def _node_rack(name: str, agent, snapshot) -> dict[str, Any]:
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
        carabiners.append({**e, "node": name, "manifest": {**m, "node": name} if st == 200 and m.get("ok") else None,
                           "error": "" if st == 200 and m.get("ok") else str(m.get("error") or f"HTTP {st}")})
    return {"node": name, "online": True, "set_up": True, "carabiners": carabiners, "error": ""}


@router.get("/api/v1/rack")
async def rack(request: Request):
    cluster = request.app.state.cluster
    snapshots = cluster.get_snapshots()
    agents = cluster.agents
    nodes = await asyncio.gather(*[_node_rack(n, a, snapshots.get(n)) for n, a in agents.items()])
    ripple = getattr(request.app.state.config, "ripple", None)
    return {"ok": True, "wakes": {"target": getattr(ripple, "target", "") or ""}, "nodes": list(nodes)}


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
