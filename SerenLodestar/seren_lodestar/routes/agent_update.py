"""
Agent update routes — push a new seren-agent package to nodes.
"""
from __future__ import annotations

import os
import asyncio
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..cluster import JetsonClusterClient
from ..config import RuntimeConfig

API_VERSION = "v1"

router = APIRouter(tags=["agent"])


def _resolve_package_path(runtime: RuntimeConfig):
    if not runtime.agent_package_path:
        return None, JSONResponse({
            "ok": False, "error": "not_configured",
            "detail": "runtime.agent_package_path is not set in seren-lodestar.yaml",
        }, status_code=409)
    path_str = runtime.agent_package_path
    if path_str.startswith("~/"):
        home = Path.home()
        path_str = str(home / path_str[2:])
    resolved = Path(os.path.abspath(path_str))
    if not resolved.exists():
        return None, JSONResponse({
            "ok": False, "error": "package_not_found",
            "detail": f"seren-agent package not found at: {resolved}",
        }, status_code=409)
    return str(resolved), None


@router.post(f"/api/{API_VERSION}/system/agent-update")
async def broadcast_update(request: Request):
    cluster: JetsonClusterClient = request.app.state.cluster
    runtime: RuntimeConfig = request.app.state.config.runtime
    package_path, error = _resolve_package_path(runtime)
    if error is not None:
        return error
    agents = cluster.agents

    async def update_one(node_name: str, agent):
        if not agent.agent_update_path:
            return {
                "ok": False, "node": node_name, "message": None,
                "error": "agent_update_path not configured for this node",
            }
        data = await asyncio.to_thread(lambda: open(package_path, "rb").read())
        result = await agent.push_agent_update_async(data, "seren-agent.tar.gz",
                                                      agent.agent_update_path)
        if result is not None:
            return {
                "ok": result.ok, "node": node_name,
                "message": result.message, "error": result.error,
            }
        return {"ok": False, "node": node_name, "message": None, "error": "agent did not respond"}

    tasks = [update_one(name, agent) for name, agent in agents.items()]
    results = await asyncio.gather(*tasks)
    any_ok = any(r["ok"] for r in results)
    return {
        "ok": any_ok, "total": len(results),
        "succeeded": sum(1 for r in results if r["ok"]),
        "results": {
            r["node"]: {"ok": r["ok"], "message": r["message"], "error": r["error"]}
            for r in results
        },
    }


@router.post(f"/api/{API_VERSION}/node/{{node}}/agent-update")
async def per_node_update(request: Request, node: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    runtime: RuntimeConfig = request.app.state.config.runtime
    package_path, error = _resolve_package_path(runtime)
    if error is not None:
        return error
    agent = cluster.get_agent(node)
    if agent is None:
        return JSONResponse({
            "ok": False, "error": "unknown_node", "detail": f"'{node}' is not in the cluster config",
        }, status_code=404)
    if not agent.agent_update_path:
        return JSONResponse({
            "ok": False, "error": "not_configured",
            "detail": f"agent_update_path is not set for node '{node}'",
        }, status_code=409)
    data = await asyncio.to_thread(lambda: open(package_path, "rb").read())
    result = await agent.push_agent_update_async(data, "seren-agent.tar.gz",
                                                  agent.agent_update_path)
    if result is None:
        return JSONResponse({
            "ok": False, "node": node, "error": "agent did not respond",
        }, status_code=503)
    return {"ok": result.ok, "node": node, "message": result.message, "error": result.error}
