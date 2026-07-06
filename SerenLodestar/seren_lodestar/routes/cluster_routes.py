"""
Cluster routes — /api/v1/cluster/* topology + rediscovery.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..cluster import JetsonClusterClient

API_VERSION = "v1"

router = APIRouter(tags=["cluster"])


@router.post(f"/api/{API_VERSION}/cluster/refresh")
async def refresh_all(request: Request):
    cluster: JetsonClusterClient = request.app.state.cluster
    summary = await cluster.refresh_async()
    return {
        "ok": True,
        "total": summary.total_nodes,
        "online": summary.online_nodes,
        "nodes": {
            name: {
                "online": snap.online,
                "installed_services": snap.installed_services,
                "last_error": snap.last_error,
                "last_probed": snap.last_probed,
            }
            for name, snap in summary.per_node.items()
        },
    }


@router.post(f"/api/{API_VERSION}/cluster/refresh/{{node}}")
async def refresh_node(request: Request, node: str):
    cluster: JetsonClusterClient = request.app.state.cluster
    snap = await cluster.refresh_node_async(node)
    if snap is None:
        return JSONResponse(
            {"ok": False, "error": f"unknown node: {node}"},
            status_code=404,
        )
    return {
        "ok": True,
        "node": node,
        "online": snap.online,
        "installed_services": snap.installed_services,
        "last_error": snap.last_error,
        "last_probed": snap.last_probed,
    }


@router.get(f"/api/{API_VERSION}/cluster/capabilities")
async def capabilities(request: Request):
    cluster: JetsonClusterClient = request.app.state.cluster
    caps = cluster.get_capabilities()
    return {"capabilities": caps}
