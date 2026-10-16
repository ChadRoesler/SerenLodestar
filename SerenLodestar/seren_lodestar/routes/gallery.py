"""
Gallery routes - the pictures every node's Workbench kept, through the cluster.

    GET  /api/v1/gallery                       every node with a Workbench: its days and counts
    GET  /api/v1/gallery/{node}/{day}          one day's pictures on one node, each with its note
    GET  /api/v1/gallery/{node}/{day}/{name}   the file itself, streamed through

make_image keeps what it draws in the gallery of the Workbench that drew it,
on that box. A Symposium on another box wants the picture at full size; a
store on another box (a keepsake, an eidetic picture, a figure) wants to keep
it. Lodestar knows every node and the Observatory on each relays to the
Workbench with that service's own bearer (seren_observatory.stores_routes),
so this is the one address: Lodestar's, with Lodestar's token. 15 Oct 2026.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

API_VERSION = "v1"
router = APIRouter(tags=["gallery"])

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _workbench_of(cluster: Any, node: str) -> Optional[str]:
    """The Workbench service a node reports (seren-workbench-<instance>, or
    SerenWorkbench-<instance> on Windows), or None."""
    snap = cluster.get_snapshots().get(node)
    for svc in sorted(getattr(snap, "installed_services", None) or []):
        if "workbench" in svc.lower():
            return svc
    return None


def _node(request: Request, node: str) -> tuple[Any, Optional[str], Optional[JSONResponse]]:
    cluster = request.app.state.cluster
    agent = cluster.get_agent(node)
    if agent is None:
        return None, None, JSONResponse({"error": "unknown_node", "detail": f"'{node}' is not in the cluster config"},
                                        status_code=404)
    svc = _workbench_of(cluster, node)
    if svc is None:
        return None, None, JSONResponse({"error": "no_gallery", "detail": f"'{node}' reports no Workbench"},
                                        status_code=404)
    return agent, svc, None


@router.get(f"/api/{API_VERSION}/gallery")
async def gallery(request: Request):
    """Every online node with a Workbench: its gallery's days and counts."""
    cluster = request.app.state.cluster
    out = []
    for name, snap in cluster.get_snapshots().items():
        if not getattr(snap, "online", False):
            continue
        svc = _workbench_of(cluster, name)
        agent = cluster.get_agent(name)
        if svc is None or agent is None:
            continue
        status, body = await agent.relay_json_async(f"api/v1/service/{svc}/gallery")
        if status == 200 and isinstance(body, dict):
            out.append({"node": name, "service": svc, "days": body.get("days") or [], "count": body.get("count") or 0})
        else:
            out.append({"node": name, "service": svc, "error": (body or {}).get("detail") or f"HTTP {status}"})
    return {"nodes": out, "count": sum(n.get("count") or 0 for n in out)}


@router.get(f"/api/{API_VERSION}/gallery/{{node}}/{{day}}")
async def gallery_day(request: Request, node: str, day: str):
    if not _DAY.match(day):
        return JSONResponse({"error": "bad_day", "detail": "a day is YYYY-MM-DD"}, status_code=400)
    agent, svc, err = _node(request, node)
    if err:
        return err
    status, body = await agent.relay_json_async(f"api/v1/service/{svc}/gallery/{day}")
    if status != 200:
        return JSONResponse({"error": "gallery", "detail": (body or {}).get("detail") or f"HTTP {status}"},
                            status_code=status if 400 <= status < 600 else 502)
    return {"node": node, **body}


@router.get(f"/api/{API_VERSION}/gallery/{{node}}/{{day}}/{{name}}")
async def gallery_picture(request: Request, node: str, day: str, name: str):
    """The file, streamed through the node's Observatory: never held here whole."""
    if not _DAY.match(day) or not _NAME.match(name) or ".." in name:
        return JSONResponse({"error": "bad_name", "detail": "a day is YYYY-MM-DD and a name has no path in it"},
                            status_code=400)
    agent, svc, err = _node(request, node)
    if err:
        return err
    resp = await agent.open_relay_async(f"api/v1/service/{svc}/gallery/{day}/{name}")
    if resp is None:
        return JSONResponse({"error": "unreachable", "detail": f"'{node}' did not answer"}, status_code=502)
    if resp.status_code != 200:
        body = await resp.aread()
        await resp.aclose()
        try:
            detail = __import__("json").loads(body).get("detail")
        except Exception:  # noqa: BLE001
            detail = None
        return JSONResponse({"error": "gallery", "detail": detail or f"HTTP {resp.status_code}"},
                            status_code=resp.status_code if 400 <= resp.status_code < 600 else 502)

    async def body():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
    keep = ("content-type", "content-length", "content-disposition", "x-content-type-options", "content-security-policy")
    return StreamingResponse(body(), media_type=resp.headers.get("content-type") or "application/octet-stream",
                             headers={k: v for k, v in resp.headers.items() if k.lower() in keep and k.lower() != "content-type"})
